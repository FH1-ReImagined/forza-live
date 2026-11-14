"""The stored files' totals (limits.Stored, #64): kept in memory and counted in a thread, never
by a walk on the event loop; the disk quota still refuses when it is full and gets its room
back from a delete; uploads before the first count are bounded; a count puts a wrong total
right; a restart finds what the running total said. The videos' budget (the same Stored) has
its tests with the videos (tests/content/test_videos.py), and the upload tests of every module
check the running totals against the files at their end (tests.support.DiskTotals).

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.content import market
from forza_live.content import photos
from forza_live.content import videos
from forza_live.core import limits
from forza_live.social import clubs
from tests.content import test_community as tc
from tests.support import fill, fresh_count, no_walks_here

ALICE = tc.ALICE
FILES = 4000  # "several thousand small files"


def fill_store(data_dir, files=FILES):
    """A store of `files` small files over the folders of the disk quota, most of them ghosts
    (the live server's shape), some a folder deeper (ghosts/review): their bytes."""
    each = files // 20
    total = fill(os.path.join(data_dir, "ghosts"), files - 7 * each)
    total += fill(os.path.join(data_dir, "ghosts", "review"), 2 * each, size=11)
    for sub in limits.BLOB_DIRS[1:]:
        total += fill(os.path.join(data_dir, sub), each, size=3)
    return total


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(limits.configure, limits._data_dir)
        limits.configure(self.dir.name)

    def path(self, *parts):
        return os.path.join(self.dir.name, *parts)


class CountTest(Base):
    def test_a_count_finds_every_file(self):
        total = fill_store(self.dir.name)
        fill(self.path("players"), 5)  # not a folder of the quota
        fill(self.path("videos", "public"), 5)  # the videos have their own budget
        self.assertEqual((limits.usage(), limits.STORED.counted), (0, False))
        self.assertEqual(limits.count(), (FILES, total, None))  # the first count: no drift to tell
        self.assertEqual((limits.usage(), limits.STORED.counted, limits.STORED.files),
                         (total, True, FILES))
        self.assertEqual(limits.count(), (FILES, total, 0))

    def test_a_count_follows_no_link_out_of_a_video_folder(self):
        outside = self.path("elsewhere.bin")
        with open(outside, "wb") as f:
            f.write(bytes(5000))
        os.makedirs(self.path("videos"))
        try:
            os.symlink(outside, self.path("videos", "link.mp4"))
        except (OSError, NotImplementedError):
            self.skipTest("no symbolic links here")
        with mock.patch.object(videos, "DATA_DIR", self.dir.name):
            videos.STORED.reset()
            self.addCleanup(videos.STORED.reset)
            videos.STORED.count()
            self.assertLess(videos.usage(), 5000)  # the link, not the file it names

    def test_the_quota_refuses_when_full_and_a_delete_gives_the_room_back(self):
        total = fill_store(self.dir.name)
        limits.count()
        with mock.patch.object(limits, "DISK_CAP", total + 100):
            limits.accept(60)
            with self.assertRaisesRegex(ValueError, "server disk quota reached"):
                limits.accept(60)
            self.assertEqual(limits.usage(), total + 60)  # a refused upload is not counted
            limits.remove(self.path("ghosts", "old-0.bin"))  # 7 bytes: 47 are free, not 60
            self.assertRaises(ValueError, limits.accept, 60)
            for n in range(1, 3):
                limits.remove(self.path("ghosts", "old-%d.bin" % n))
            limits.accept(60)
            self.assertEqual(limits.usage(), total + 120 - 21)
            self.assertRaises(ValueError, limits.accept, 2)

    def test_one_blob_s_own_limit(self):
        self.assertRaises(ValueError, limits.accept, limits.MAX_BLOB + 1)
        limits.accept(limits.MAX_BLOB + 1, limits.MAX_BLOB + 1)
        self.assertEqual(limits.usage(), limits.MAX_BLOB + 1)

    def test_removing_a_file_that_is_not_there_changes_nothing(self):
        limits.accept(10)
        self.assertRaises(OSError, limits.remove, self.path("ghosts", "none.bin"))
        self.assertEqual(limits.usage(), 10)
        limits.release(50)  # never below nothing
        self.assertEqual(limits.usage(), 0)

    def test_before_the_first_count_uploads_are_bounded_by_the_allowance(self):
        fill_store(self.dir.name, 200)  # a store whose size is not known yet
        with mock.patch.object(limits, "START_ALLOWANCE", 1000):
            limits.accept(600)
            limits.accept(400)
            with self.assertRaisesRegex(ValueError, "still being counted"):
                limits.accept(1)
            self.assertEqual(limits.usage(), 1000)
            found = limits.count()[1]
            self.assertEqual(limits.usage(), found)  # the count is what the files say...
            limits.accept(5000)  # ...and the quota is the only limit from then on
        with mock.patch.object(limits, "START_ALLOWANCE", 1000), \
                mock.patch.object(limits, "DISK_CAP", 300):
            limits.configure(self.dir.name)
            with self.assertRaisesRegex(ValueError, "quota reached"):
                limits.accept(301)  # a quota smaller than the allowance still holds

    def test_the_total_after_a_restart_is_what_the_running_total_said(self):
        total = fill_store(self.dir.name, 400)
        limits.count()
        clubs.configure(self.dir.name, lambda xuid: {})
        market.configure(self.dir.name)
        clubs.write_blob("car7_livery.bin", bytes(900))
        clubs.write_blob("car7_livery.bin", bytes(500))  # saved again: written over
        market.MARKET.write_blob("auction3_tuning.bin", bytes(120))
        market.MARKET.write_blob("auction3_tuning.bin", bytes(130))
        limits.remove(self.path("photos", "old-0.bin"))
        limits.wrote(fill(self.path("designs", "made"), 3, size=40))
        self.assertEqual(limits.usage(), total + 500 + 130 - 3 + 120)
        said = limits.usage()
        limits.configure(self.dir.name)  # the restart
        self.assertEqual(limits.usage(), 0)
        limits.count()
        self.assertEqual(limits.usage(), said)

    def test_what_is_taken_while_a_count_walks_is_kept(self):
        fill_store(self.dir.name, 400)
        started, go = threading.Event(), threading.Event()
        real = os.stat

        def held(path, *args, **kw):  # the count's thread stops at its first file
            if threading.current_thread() is not threading.main_thread():
                started.set()
                go.wait(30)
            return real(path, *args, **kw)

        with mock.patch.object(limits.STORED, "_stat", held):
            thread = threading.Thread(target=limits.count)
            thread.start()
            self.assertTrue(started.wait(30))
            limits.accept(1000)
            with open(self.path("ghosts", "new.bin"), "wb") as f:
                f.write(bytes(1000))
            limits.remove(self.path("clubs", "old-0.bin"))  # 3 bytes the count may have seen
            go.set()
            thread.join(30)
        files = fresh_count(limits.STORED)
        # every byte added meanwhile on top of what was found, nothing given back for the
        # delete: at most that much too full, never too empty, until the next count
        self.assertGreaterEqual(limits.usage(), files)
        self.assertLessEqual(limits.usage(), files + 1000 + 3)
        limits.count()
        self.assertEqual(limits.usage(), files)

    def test_a_count_of_a_folder_that_is_no_longer_the_store_is_dropped(self):
        fill_store(self.dir.name, 400)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        started, go, result = threading.Event(), threading.Event(), []
        real = os.stat

        def held(path, *args, **kw):
            started.set()
            go.wait(30)
            return real(path, *args, **kw)

        with mock.patch.object(limits.STORED, "_stat", held):
            thread = threading.Thread(target=lambda: result.append(limits.count()))
            thread.start()
            self.assertTrue(started.wait(30))
            limits.configure(other.name)  # another data folder meanwhile
            limits.accept(5)
            go.set()
            thread.join(30)
        self.assertEqual(result, [None])
        self.assertEqual((limits.usage(), limits.STORED.counted), (5, False))

    def test_close_ends_a_count(self):
        fill_store(self.dir.name, 400)
        started, go, result, stats = threading.Event(), threading.Event(), [], []
        real = os.stat

        def held(path, *args, **kw):
            stats.append(path)
            started.set()
            go.wait(30)
            return real(path, *args, **kw)

        with mock.patch.object(limits.STORED, "_stat", held):
            thread = threading.Thread(target=lambda: result.append(limits.count()))
            thread.start()
            self.assertTrue(started.wait(30))
            limits.STORED.close()
            go.set()
            thread.join(30)
        self.assertEqual(result, [None])
        self.assertEqual(len(stats), 1)  # it did not walk on through the 400 files


class LoopTest(Base, unittest.IsolatedAsyncioTestCase):
    """The event loop never walks: the count is another thread's."""

    async def counted(self, *stores):
        for _ in range(4000):
            if all(s.counted for s in stores):
                return
            await asyncio.sleep(0.005)
        self.fail("the stored files were not counted")

    async def test_a_count_refuses_to_run_on_the_event_loop(self):
        with self.assertRaisesRegex(RuntimeError, "never on the event loop"):
            limits.count()
        self.assertEqual(await asyncio.to_thread(limits.count), (0, 0, None))

    async def test_accept_walks_no_folder_on_the_event_loop(self):
        total = fill_store(self.dir.name)
        clubs.configure(self.dir.name, lambda xuid: {})
        with no_walks_here() as walks, self.assertLogs("forza-live", "INFO") as logs:
            task = asyncio.create_task(srv.keep_counted((("uploads", limits.STORED),), every=3600))
            clubs.write_blob("car1_tuning.bin", bytes(100))  # answered before any count
            await self.counted(limits.STORED)
            with mock.patch.object(limits, "DISK_CAP", total + 1000):
                for n in range(2, 11):
                    clubs.write_blob("car%d_tuning.bin" % n, bytes(100))
                self.assertRaises(ValueError, limits.accept, 100)  # full: still refused
                limits.remove(self.path("ghosts", "old-0.bin"))
                limits.remove(self.path("ghosts", "old-1.bin"))
                self.assertRaises(ValueError, limits.accept, 100)
                clubs.write_blob("car11_tuning.bin", bytes(14))  # the room the two deletes gave back
                self.assertRaises(ValueError, limits.accept, 1)
            task.cancel()
        self.assertEqual(walks.here, [])
        self.assertTrue(walks.elsewhere)  # the count did walk, in its thread
        self.assertEqual(limits.usage(), total + 1000)
        self.assertEqual(await asyncio.to_thread(fresh_count, limits.STORED), total + 1000)
        self.assertRegex(" ".join(logs.output),
                         r"disk usage: uploads: %d files, 0\.0 MB \(counted in" % (FILES + 1))

    async def test_uploads_are_answered_while_the_count_is_still_walking(self):
        total = fill_store(self.dir.name)
        loop_thread, go, started = threading.get_ident(), threading.Event(), threading.Event()
        real = os.stat

        def slow(path, *args, **kw):  # the count's thread waits at its first file
            if threading.get_ident() != loop_thread:
                started.set()
                go.wait(30)
            return real(path, *args, **kw)

        with mock.patch.object(limits.STORED, "_stat", slow), no_walks_here() as walks:
            task = asyncio.create_task(srv.keep_counted((("uploads", limits.STORED),), every=3600))
            await asyncio.to_thread(started.wait, 30)
            photos.configure(self.dir.name)
            clubs.configure(self.dir.name, lambda xuid: {})
            fid = photos.create(ALICE, 5000, False, "photo.jpg", "large")  # a player's upload
            clubs.write_blob("car9_livery.bin", bytes(700))
            photos.forget(fid)
            self.assertFalse(limits.STORED.counted)  # all of it while the count had not moved
            self.assertEqual(limits.usage(), 700)
            go.set()
            await self.counted(limits.STORED)
            task.cancel()
        self.assertEqual(walks.here, [])
        # What came and went while the count walked is on top of what it found, whether it saw
        # it or not: on the full side by at most that, and exact again after the next count.
        self.assertEqual(await asyncio.to_thread(fresh_count, limits.STORED), total + 700)
        self.assertGreaterEqual(limits.usage(), total + 700)
        self.assertLessEqual(limits.usage(), total + 700 + 5000 + 700)
        await asyncio.to_thread(limits.count)
        self.assertEqual(limits.usage(), total + 700)

    async def test_the_recount_corrects_a_wrong_total(self):
        total = fill_store(self.dir.name, 400)
        with self.assertLogs("forza-live", "INFO") as logs:
            task = asyncio.create_task(srv.keep_counted((("uploads", limits.STORED),), every=0.02))
            await self.counted(limits.STORED)
            self.assertEqual(limits.usage(), total)
            limits.wrote(3 << 20)  # counted, never written: the total is 3 MB too full
            total += fill(self.path("market", "copied"), 10, size=100)  # and files nobody told it of
            self.assertEqual(limits.usage(), total - 1000 + (3 << 20))
            for _ in range(4000):
                if limits.usage() == total:
                    break
                await asyncio.sleep(0.005)
            task.cancel()
        self.assertEqual(limits.usage(), total)
        self.assertRegex(" ".join(logs.output), r"the running total was \+3\.0 MB off")

    async def test_a_count_that_fails_is_made_again(self):
        fill_store(self.dir.name, 100)
        real, calls = limits.STORED.count, []

        def count():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("the disk did not answer")
            return real()

        with mock.patch.object(limits.STORED, "count", count), \
                self.assertLogs("forza-live", "ERROR") as logs:
            task = asyncio.create_task(srv.keep_counted((("uploads", limits.STORED),), every=0.02))
            await self.counted(limits.STORED)
            task.cancel()
        self.assertIn("counting the uploads failed", logs.output[0])
        self.assertGreaterEqual(len(calls), 2)

    async def test_the_server_counts_both_stores(self):
        total = fill_store(self.dir.name, 100)
        clips = fill(self.path("videos", "public"), 30, size=50)
        with mock.patch.object(videos, "DATA_DIR", self.dir.name):
            videos.STORED.reset()
            self.addCleanup(videos.STORED.reset)
            task = asyncio.create_task(srv.keep_counted(every=3600))
            await self.counted(limits.STORED, videos.STORED)
            task.cancel()
            self.assertEqual((limits.usage(), videos.usage()), (total, clips))


class UploadTest(tc.Base, unittest.IsolatedAsyncioTestCase):
    """A player's upload through the title services, with thousands of files stored: the event
    loop walks nothing, and the totals are the files'."""

    async def test_a_photo_upload_walks_no_folder_on_the_event_loop(self):
        stored = fill_store(self.dir.name)
        with no_walks_here() as walks:
            task = asyncio.create_task(srv.keep_counted(every=3600))
            first = self.upload(ALICE, tc.make_jpeg())  # before the first count
            for _ in range(4000):
                if limits.STORED.counted and videos.STORED.counted:
                    break
                await asyncio.sleep(0.005)
            second = self.upload(tc.BOB, tc.make_jpeg(800, 448))  # and after it
            task.cancel()
        self.assertEqual(walks.here, [])
        self.assertTrue(walks.elsewhere)
        self.assertTrue(first and second)
        self.assertEqual(len(self.records()), 2)  # both published as community photos
        self.assertGreater(limits.usage(), stored)
        # the files against the running totals: tc.Base's tearDown (DiskTotals)


if __name__ == "__main__":
    unittest.main()
