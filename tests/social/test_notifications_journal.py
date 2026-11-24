"""notifications.json and its journal. With a file of the public server's size (11 MB) a
notification sent, read or cleared costs the event loop milliseconds, and the whole file is only
ever written by a thread; the server killed at any moment loses nothing anybody was told was
kept; the first start reads the previous version's file as it is, and the file written is one
the previous version reads; expired notifications leave memory and the file, and nothing else
does; the stop writes the file and says so in the log. And the smaller stores written whole on
the loop (clubs, challenges, stats, the market) are written with one call of the C encoder.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import contextlib
import copy
import io
import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.competition import objectives, scores, stats
from forza_live.content import market
from forza_live.core import durable
from forza_live.core import journal
from forza_live.network import xtsw as x
from forza_live.social import clubs, friends
from forza_live.social import notifications as notices
from tests.support import ROOT, FakeTransport, loop_steps_under

LIVE_MB = 11.2        # notifications.json on the public server
PLAYER = 0x0009000100000000  # the tests' own players: none of the generated file's recipients
A, B, C = PLAYER + 1, PLAYER + 2, PLAYER + 3
# What a notification may take of the event loop with a file of that size. Writing the file at
# every change took 270 ms on a fast desktop; a line of the journal takes 2 ms there, most of it
# the disk's flush. The bounds are the boards' (test_scores_journal), room for a slow CI disk.
LOOP_MEDIAN, LOOP_WORST = 0.05, 0.25
LIVE = None  # (its folder, the file): made once, by setUpModule


def record(nid, ntype, params, sent, read=False, keep_days=notices.KEEP_DAYS):
    """A notification as send() keeps it."""
    return {"id": nid, "type": ntype, "params": params, "sent": sent,
            "expires": sent + keep_days * 86400, "read": read}


def live_file(megabytes=LIVE_MB, seed=11, now=None):
    """notifications.json of about `megabytes` as the previous version wrote it (json.dump,
    indent 1): 4000 recipients, Rivals messages, club news, gifts and news sent over the last
    30 days, 70 % of them read; 5 broadcasts, the broadcast state of 3000 players, 1500
    receipts."""
    rnd, now = random.Random(seed), time.time() if now is None else now
    sample = notices.samples(0x0009000000000ABC, "SomeGamerTag12")
    sample[notices.GIFT_CAR] = {"GiftId": 2, "MakeId": 12, "ModelId": 1398, "SenderXuid": 0,
                                "SenderGamerTag": "Forza Live", "Reason": 0}
    kinds = [notices.RIVALS_BEATEN] * 45 + [notices.CLUB_NEW_MEMBER] * 23 + \
        [notices.CLUB_INVITE] * 12 + [notices.GIFT_CAR] * 8 + [notices.NEWS] * 12
    people = ["%016X" % (0x0009000000100000 + i) for i in range(4000)]
    data = {"nextId": 1, "byRecipient": {}}
    for _ in range(int(megabytes * 1e6 / 382)):
        t, sent = rnd.choice(kinds), now - rnd.random() * 29.9 * 86400
        data["byRecipient"].setdefault(rnd.choice(people), []).append(
            record(data["nextId"], t, dict(sample[t]), sent, rnd.random() < 0.7))
        data["nextId"] += 1
    data["receipts"] = {"event:%032x" % rnd.getrandbits(128): rnd.randrange(1, data["nextId"])
                        for _ in range(1500)}
    data["broadcasts"] = [{"id": data["nextId"] + i, "type": notices.NEWS,
                           "params": {"Message": "News %d" % i}, "sent": now - i * 86400,
                           "expires": now + (30 - i) * 86400} for i in range(5)]
    data["nextId"] += 5
    ids = [b["id"] for b in data["broadcasts"]]
    data["broadcastState"] = {who: {"read": ids[:rnd.randrange(1, 6)], "cleared": ids[:1]}
                              for who in people[:3000]}
    return data


def setUpModule():
    global LIVE
    folder = tempfile.TemporaryDirectory()
    path = os.path.join(folder.name, "notifications.json")
    with open(path, "w") as f:
        json.dump(live_file(), f, indent=1)  # the previous version's save
    assert 10.5e6 < os.path.getsize(path) < 12e6, os.path.getsize(path)
    LIVE = folder, path


def tearDownModule():
    LIVE[0].cleanup()


def as_the_previous_version_reads_it(folder):
    """notifications.json alone, read as the version before the journal read it (Store.load:
    the file's JSON on top of an empty store)."""
    data = {"nextId": 1, "byRecipient": {}}
    with open(os.path.join(folder, "notifications.json")) as f:
        data.update(json.load(f))
    return data


def previous_pending(data, xuid, now=None):
    """What the previous version's RetrieveForUser gave a player (its pending(), as it was)."""
    now, who = time.time() if now is None else now, "%016X" % xuid
    mine = [n for n in data["byRecipient"].get(who, []) if n["expires"] > now]
    seen = data.get("broadcastState", {}).get(who, {})
    cleared, read = set(seen.get("cleared", [])), set(seen.get("read", []))
    return mine + [dict(b, read=b["id"] in read) for b in data.get("broadcasts", [])
                   if b["expires"] > now and b["id"] not in cleared]


def mark(xuid, nid, clear=False):
    notices.update(xuid, bytes(x.encode(x.NotificationsId_Params, {"notificationId": nid})),
                   clear)


def poll(xuid):
    return notices.retrieve(xuid, bytes(x.encode(x.NotificationsRetrieveForUser_Params, {
        "nonce": 1, "maxResults": 512, "cbParameterBlockSize": 0xC800})))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        old = notices.DATA_DIR
        self.addCleanup(lambda: notices.configure(old))
        notices.configure(self.dir.name)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def restart(self):
        """A new server on the folder as it is: what a kill at this moment leaves."""
        notices.configure(self.dir.name)
        return notices.STORE.load()

    def journal_lines(self):
        with open(self.path("notifications.journal"), "rb") as f:
            return [json.loads(line) for line in f.read().split(b"\n")[1:-1]]

    def news(self, xuid, text="hello", **kw):
        return notices.send(xuid, notices.NEWS, {"Message": text}, **kw)


class WholeFile:
    """Inside it the whole of notifications.json may be written by any thread but the calling
    one (an async test's event loop): there notifications.dump, the journal's checkpoint, the
    previous version's json.dump of the store and a rename over the file all fail. `threads`
    are those that did write it."""

    def __init__(self):
        self.loop, self.threads, self.here = threading.get_ident(), [], []

    def guard(self, real, what, counts=lambda *args: True):
        def call(*args, **kw):
            if counts(*args):
                if threading.get_ident() == self.loop:
                    self.here.append(what)
                    raise AssertionError("%s on the event loop's thread" % what)
                self.threads.append(threading.get_ident())
            return real(*args, **kw)
        return call

    def __enter__(self):
        self.patches = [
            mock.patch.object(notices, "dump", self.guard(notices.dump, "notifications.dump")),
            mock.patch.object(journal.Journal, "checkpoint",
                              self.guard(journal.Journal.checkpoint, "the journal's checkpoint")),
            mock.patch.object(json, "dump", self.guard(
                json.dump, "json.dump of the notifications",
                lambda obj, *rest: isinstance(obj, dict) and "byRecipient" in obj)),
            mock.patch.object(durable, "replace", self.guard(
                durable.replace, "a rename over notifications.json",
                lambda tmp, path, *rest: path.endswith("notifications.json")))]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


class LiveSizeTest(Base, unittest.IsolatedAsyncioTestCase):
    """A file of the public server's size, on an event loop."""

    def setUp(self):
        super().setUp()
        shutil.copy(LIVE[1], self.path("notifications.json"))
        with self.assertNoLogs("forza-live", "WARNING"):
            self.restart()

    async def asyncSetUp(self):
        # asyncio's slow-callback warning measures wall time: off here, answering() measures the
        # CPU time of every step instead.
        asyncio.get_running_loop().slow_callback_duration = 3600

    def answering(self):
        """Inside it no step of the event loop may use LOOP_WORST of CPU or more, and asyncio
        logs no other warning."""
        stack = contextlib.ExitStack()
        stack.enter_context(self.assertNoLogs("asyncio", "WARNING"))
        stack.enter_context(loop_steps_under(LOOP_WORST))
        return stack

    async def written(self):
        for _ in range(6000):
            if not notices.STORE.journal.behind():
                return
            await asyncio.sleep(0.005)
        self.fail("notifications.json was not written")

    async def actions(self):
        """What players do: a Rivals message, its MarkRead and Clear, a club's news to 30
        members, and the operator's broadcast. How long each call held the loop."""
        took, sent = [], []

        self.held = []  # the same calls in wall time: what the loop waited, a flush or a lock too

        def timed(fn, *args):
            began, wall = time.thread_time(), time.perf_counter()
            result = fn(*args)
            took.append(time.thread_time() - began)
            self.held.append(time.perf_counter() - wall)
            return result

        rival = notices.samples(B, "DriverB")[notices.RIVALS_BEATEN]
        for n in range(8):
            nid = timed(notices.send, PLAYER + n, notices.RIVALS_BEATEN, rival)
            sent.append(nid)
            await asyncio.sleep(0.002)
            timed(mark, PLAYER + n, nid)
            await asyncio.sleep(0.002)
            if n % 2:
                timed(mark, PLAYER + n, nid, True)
            await asyncio.sleep(0.002)
            timed(notices.send_many, [PLAYER + 100 + m for m in range(30)],
                  notices.CLUB_NEW_MEMBER, {"clanName": "Kings", "clanTag": "KNG",
                                            "newMemberXuid": PLAYER + n,
                                            "newMemberGamertag": "Driver%d" % n})
            await asyncio.sleep(0.002)
        timed(notices.broadcast, notices.NEWS, {"Message": "Server news"})
        return took, sent

    async def test_a_notification_costs_the_loop_milliseconds(self):
        with WholeFile() as whole, self.answering():
            task = asyncio.create_task(notices.keep_written(every=0.05))
            took, sent = await self.actions()
            await self.written()
            task.cancel()
        self.assertEqual(whole.here, [])
        self.assertTrue(whole.threads)  # the file was written, by a thread
        self.assertLess(statistics.median(took), LOOP_MEDIAN)
        # a wait on the loop (the flush of a journal line, a lock the writing thread holds) uses
        # no CPU: the median of the calls' wall time sees it, and a few calls the runner did not
        # schedule do not move a median
        self.assertLess(statistics.median(self.held), LOOP_MEDIAN)
        self.assertLess(max(took), LOOP_WORST)
        kept = copy.deepcopy(notices.STORE.data)
        self.restart()
        self.assertEqual(notices.STORE.data, kept)
        for n, nid in enumerate(sent):
            got = {m["id"]: m for m in notices.pending(PLAYER + n)}
            if n % 2:
                self.assertNotIn(nid, got)
            else:
                self.assertTrue(got[nid]["read"])

    async def test_the_file_written_whole_is_what_a_start_and_the_previous_version_read(self):
        with WholeFile():
            await self.actions()
            await asyncio.to_thread(notices.STORE.write, notices.STORE.snapshot())
        previous = as_the_previous_version_reads_it(self.dir.name)
        self.assertEqual(previous, notices.STORE.data)
        for who in list(notices.STORE.data["byRecipient"])[:200] + ["%016X" % (PLAYER + 1)]:
            self.assertEqual(previous_pending(previous, int(who, 16)),
                             notices.pending(int(who, 16)))
        # smaller than the previous version's: a notification to a line
        self.assertLess(os.path.getsize(self.path("notifications.json")),
                        os.path.getsize(LIVE[1]))

    def test_the_dump_writes_in_short_steps(self):
        """The thread pauses for the loop only between two of the dump's writes (journal.Paced):
        none of them is more than a player's notifications, or a receipt, a player's
        broadcast state or a broadcast."""
        writes = []

        class File:
            def write(self, text):
                writes.append(len(text))
        notices.dump(File(), notices.STORE.snapshot()[2], time.time())
        biggest = max(len(json.dumps(mine)) + 64 * len(mine)
                      for mine in notices.STORE.data["byRecipient"].values())
        self.assertGreater(sum(writes), 8e6)
        self.assertLess(max(writes), biggest)
        self.assertLess(max(writes), 16384)

    async def test_write_refuses_to_run_on_the_loop(self):
        with self.assertRaisesRegex(RuntimeError, "never on the event loop"):
            notices.STORE.write()


class FirstStartTest(Base):
    """The first start of this version, on the file the previous version left."""

    def test_the_previous_versions_file_is_read_as_it_is(self):
        shutil.copy(LIVE[1], self.path("notifications.json"))
        before = durable.digest(self.path("notifications.json"))
        data = self.restart()
        self.assertEqual(data, as_the_previous_version_reads_it(self.dir.name))
        self.assertFalse(os.path.exists(self.path("notifications.journal")))  # nothing changed
        self.assertEqual(durable.digest(self.path("notifications.json")), before)
        nid = self.news(A)
        with open(self.path("notifications.journal")) as f:
            head = json.loads(f.readline())
        self.assertEqual(head["continues"], {before: 0})  # the journal continues that file
        self.restart()
        self.assertEqual(notices.pending(A)[0]["id"], nid)

    def test_a_folder_without_the_file_starts_empty_and_writes_nothing(self):
        self.assertEqual(self.restart(), {"nextId": 1, "byRecipient": {}})
        notices.STORE.stop()
        self.assertEqual(os.listdir(self.dir.name), [])


class CrashTest(Base):
    """A kill at any moment loses nothing anybody was told was kept."""

    def test_every_change_is_on_disk_when_the_call_returns(self):
        first = self.news(A, "first")
        second = self.news(A, "second")
        gift = self.news(B, "event reward", idempotency_key="event:1")
        mark(A, first)
        mark(A, second, True)
        casts = notices.broadcast(notices.NEWS, {"Message": "everyone"})
        mark(C, casts)
        club = notices.send_many([A, B, C], notices.CLUB_NEW_MEMBER, {
            "clanName": "Kings", "clanTag": "KNG", "newMemberXuid": PLAYER,
            "newMemberGamertag": "New"})
        kept = copy.deepcopy(notices.STORE.data)
        self.assertFalse(os.path.exists(self.path("notifications.json")))  # the journal alone
        self.restart()  # a kill now
        self.assertEqual(notices.STORE.data, kept)
        self.assertEqual([(n["id"], n["read"]) for n in notices.pending(A)],
                         [(first, True), (club[0], False), (casts, False)])
        self.assertEqual(self.news(B, "event reward", idempotency_key="event:1"), gift)
        self.assertEqual([n["id"] for n in notices.pending(C)], [club[2], casts])
        self.assertTrue(notices.pending(C)[-1]["read"])

    def test_a_club_news_is_one_line(self):
        notices.send_many([A, B, C], notices.CLUB_ACCEPTED, {"clanName": "K", "clanTag": "K"})
        self.assertEqual(len(self.journal_lines()), 1)

    def test_a_write_that_failed_goes_with_the_next_one(self):
        with mock.patch.object(journal.Journal, "append", side_effect=OSError("disk full")):
            self.assertRaises(OSError, self.news, A, "retried", idempotency_key="event:2")
        nid = notices.STORE.data["receipts"]["event:2"]
        self.assertEqual(self.news(A, "retried", idempotency_key="event:2"), nid)  # the retry
        self.restart()
        self.assertEqual([n["id"] for n in notices.pending(A)], [nid])

    def test_a_held_change_the_file_already_has_is_not_applied_twice(self):
        """A send, a read and a clear whose journal line failed stay in memory; a whole write
        then puts them in the file, the next save adds them to the journal after the file's
        place, and a kill comes: the start applies that line on a file that has it already.
        Each notification is there once, read and cleared as it was."""
        first, second = self.news(A, "first"), self.news(A, "second")
        with mock.patch.object(journal.Journal, "append", side_effect=OSError("disk full")):
            self.assertRaises(OSError, self.news, A, "held")
            self.assertRaises(OSError, mark, A, first)          # read, held
            self.assertRaises(OSError, mark, A, second, True)   # cleared, held
        held = notices.STORE.data["nextId"] - 1
        notices.STORE.write(notices.STORE.snapshot())  # the thread's write: the file has them
        last = self.news(A, "after")                   # the held changes go in this line
        line = self.journal_lines()[-1]
        self.assertEqual([n["id"] for n in line["add"]["%016X" % A]], [held, last])
        self.assertEqual((line["read"], line["clear"]), ({"%016X" % A: [first]},
                                                         {"%016X" % A: [second]}))
        kept = copy.deepcopy(notices.STORE.data)
        self.restart()  # a kill now: the last line is applied on the file
        self.assertEqual(notices.STORE.data, kept)
        self.assertEqual([(n["id"], n["read"]) for n in notices.pending(A)],
                         [(first, True), (held, False), (last, False)])

    def test_killed_at_each_step_of_a_whole_write(self):
        """The checkpoint stopped before and after each of its steps: the start has it all."""
        self.news(A, "in the file")
        notices.STORE.write()
        mark(A, notices.pending(A)[0]["id"])
        self.news(B, "in the journal")
        steps = [(journal.Journal, "_rewrite"), (durable, "replace")]
        for owner, name in steps:
            for n in range(3):
                with self.subTest(step=name, call=n):
                    real, calls = getattr(owner, name), []

                    def killed(*args, **kw):
                        calls.append(1)
                        if len(calls) > n:
                            raise KeyboardInterrupt  # the kill
                        return real(*args, **kw)
                    self.news(C, "%s %d" % (name, n))
                    kept = copy.deepcopy(notices.STORE.data)
                    with mock.patch.object(owner, name, killed):
                        try:
                            notices.STORE.write()
                        except KeyboardInterrupt:
                            pass
                    self.restart()
                    self.assertEqual(notices.STORE.data, kept)

    def test_a_real_kill_while_the_file_is_written_every_20_ms(self):
        shutil.copy(LIVE[1], self.path("notifications.json"))
        rnd, told = random.Random(11), []
        errors = open(self.path("child.log"), "w+")
        self.addCleanup(errors.close)
        start = 0
        for kill in range(4):
            child = subprocess.Popen([sys.executable, "-c", CHILD, self.dir.name, str(PLAYER),
                                      str(start)], cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=errors, text=True)
            lines, after = [], rnd.randrange(5, 120)
            for line in child.stdout:
                lines.append(line)
                if len(lines) >= after:
                    break
            time.sleep(rnd.random() * 0.1)
            child.kill()
            lines += child.stdout.read().splitlines(keepends=True)
            child.stdout.close()
            child.wait()
            errors.seek(0)
            self.assertGreaterEqual(len(lines), after, errors.read())
            told += [tuple(line.split()) for line in lines if line.endswith("\n")]
            start += len(lines) + 1
            self.restart()  # the next start: never refused
            mine, clearing = {}, set((nid, who) for what, nid, who in told
                                     if what == "clearing")
            for n in range(40):
                for m in notices.pending(PLAYER + n):
                    mine[m["id"]] = m
            for what, nid, who in told:
                nid = int(nid)
                if what == "sent":
                    self.assertTrue(nid in mine or (str(nid), who) in clearing, (nid, kill))
                elif what == "read":
                    self.assertTrue(mine[nid]["read"])
                elif what == "clear":
                    self.assertNotIn(nid, mine)
        self.assertGreater(len(told), 20)
        notices.STORE.stop()
        self.assertEqual(as_the_previous_version_reads_it(self.dir.name), notices.STORE.data)


CHILD = r"""
import asyncio, sys
from forza_live.network import xtsw as x
from forza_live.social import notifications as notices

folder, player, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
notices.configure(folder)
notices.STORE.load()


def mark(xuid, nid, clear):
    notices.update(xuid, bytes(x.encode(x.NotificationsId_Params, {"notificationId": nid})), clear)


async def main():
    global n
    writing = asyncio.create_task(notices.keep_written(every=0.02))
    while True:
        who = player + n % 40
        nid = notices.send(who, notices.NEWS, {"Message": "message %d" % n})
        print("sent %d %d" % (nid, who), flush=True)  # the caller has its answer
        if n % 3 == 0:
            mark(who, nid, False)
            print("read %d %d" % (nid, who), flush=True)
        elif n % 3 == 1:
            print("clearing %d %d" % (nid, who), flush=True)  # it may be gone without "clear"
            mark(who, nid, True)
            print("clear %d %d" % (nid, who), flush=True)
        n += 1
        await asyncio.sleep(0)

asyncio.run(main())
"""


class PruneTest(Base):
    """Expired notifications leave memory and the file; nothing else does."""

    def setUp(self):
        super().setUp()
        now = time.time()
        self.now = now
        data = {"nextId": 10, "byRecipient": {
            "%016X" % A: [record(1, notices.NEWS, {"Message": "expired, read"}, now - 31 * 86400,
                                 read=True),
                          record(2, notices.NEWS, {"Message": "expired, unread"},
                                 now - 30 * 86400 - 1),
                          record(3, notices.NEWS, {"Message": "read, 29 days"}, now - 29 * 86400,
                                 read=True),
                          record(4, notices.NEWS, {"Message": "unread"}, now - 60)],
            "%016X" % B: [record(5, notices.NEWS, {"Message": "expired"}, now - 40 * 86400)]},
            "receipts": {"event:old": 5}}
        with open(self.path("notifications.json"), "w") as f:
            json.dump(data, f, indent=1)
        self.shown = {who: previous_pending(data, who) for who in (A, B)}

    def test_a_start_leaves_out_what_the_game_is_no_longer_shown(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            data = self.restart()
        self.assertIn("3 expired ones left out", logs.output[-1])
        self.assertEqual([n["id"] for n in data["byRecipient"]["%016X" % A]], [3, 4])
        self.assertNotIn("%016X" % B, data["byRecipient"])
        self.assertEqual(data["receipts"], {"event:old": 5})  # receipts stay
        for who in (A, B):  # what a poll gives is the same
            self.assertEqual(notices.pending(who), self.shown[who])
        self.news(C)
        notices.STORE.write()
        on_disk = as_the_previous_version_reads_it(self.dir.name)
        self.assertEqual([n["id"] for n in on_disk["byRecipient"]["%016X" % A]], [3, 4])
        self.assertNotIn("%016X" % B, on_disk["byRecipient"])

    def test_read_notifications_stay_until_they_expire(self):
        self.restart()
        mark(A, 4)
        notices.STORE.write()
        self.restart()
        self.assertEqual([(n["id"], n["read"]) for n in notices.pending(A)],
                         [(3, True), (4, True)])

    def test_one_that_expires_while_the_server_runs_leaves_the_file(self):
        self.restart()
        later = self.now + 86400 + 1  # notification 3 has expired by then
        self.news(C)
        with mock.patch.object(notices.time, "time", return_value=later):
            self.assertEqual([n["id"] for n in notices.pending(A)], [4])
            notices.STORE.write()
        self.assertEqual([n["id"] for n in as_the_previous_version_reads_it(
            self.dir.name)["byRecipient"]["%016X" % A]], [4])

    def test_a_journal_line_about_a_pruned_one_changes_nothing(self):
        self.restart()
        notices.STORE.changes = {"read": {"%016X" % A: [1]}, "clear": {"%016X" % B: [5]}}
        notices.STORE.save()
        self.restart()
        self.assertEqual(notices.pending(A), self.shown[A])

    def test_a_new_one_drops_its_recipients_expired_ones(self):
        notices.STORE.loaded = True  # memory as the file has it, expired ones too
        notices.STORE.data = as_the_previous_version_reads_it(self.dir.name)
        notices.STORE.journal = journal.Journal(notices.STORE.path())
        notices.STORE.journal.load()
        nid = self.news(A)
        self.assertEqual([n["id"] for n in notices.STORE.data["byRecipient"]["%016X" % A]],
                         [3, 4, nid])


class SnapshotTest(Base):
    """What lets a thread write the file while the loop goes on: a snapshot is never changed,
    whatever the loop does after it is taken (the Store's docstring)."""

    def test_a_snapshot_is_what_the_store_was_when_it_was_taken(self):
        read_one, clear_one = self.news(A, "to read"), self.news(A, "to clear")
        self.news(B, "kept", idempotency_key="event:1")
        casts = notices.broadcast(notices.NEWS, {"Message": "everyone"})
        mark(C, casts)
        snapshot = notices.STORE.snapshot()
        tables = snapshot[2]
        then = copy.deepcopy(tables)
        records = {who: [id(n) for n in mine] for who, mine in tables["byRecipient"].items()}
        # what the loop does meanwhile: a new recipient, a read, a clear, a broadcast's read and
        # clear, a receipt, a new broadcast and one taken back, and an expired one pruned
        self.news(PLAYER + 9, "a new recipient", idempotency_key="event:2")
        mark(A, read_one)
        mark(A, clear_one, True)
        mark(B, casts)
        mark(C, casts, True)
        notices.delete_broadcast(notices.broadcast(notices.NEWS, {"Message": "taken back"}))
        notices.send(B, notices.NEWS, {"Message": "gone"}, keep_days=-1)
        notices.STORE.prune()
        self.assertNotEqual(notices.STORE.data, then)
        self.assertEqual(tables, then)  # deep-equal: nothing in it changed in place
        self.assertEqual({who: [id(n) for n in mine] for who, mine in
                          tables["byRecipient"].items()}, records)
        f = io.StringIO()
        notices.dump(f, tables, time.time())
        self.assertEqual(json.loads(f.getvalue()), then)  # what the thread writes from it
        self.assertEqual(notices.STORE.write(snapshot), True)
        self.restart()  # the file the snapshot made, and the journal's lines after its place
        mine = [(n["id"], n["read"]) for n in notices.pending(A)]
        self.assertEqual(mine, [(read_one, True), (casts, False)])


class StopTest(Base):
    """The stop writes the file whole and says so; the server's stop writes it before the
    boards."""

    def test_the_log_says_what_the_stop_did_with_the_file(self):
        self.news(A)
        with self.assertLogs("forza-live", "INFO") as logs:
            notices.STORE.stop()
        self.assertRegex(logs.output[-1], r"notifications: notifications\.json written whole at "
                                          r"the stop in \d+\.\d\d s: it has every change: 1 "
                                          r"notification for 1 player, 0 broadcasts$")
        self.assertEqual(as_the_previous_version_reads_it(self.dir.name), notices.STORE.data)
        with self.assertLogs("forza-live", "INFO") as logs:
            notices.STORE.stop()
        self.assertEqual(logs.output, ["INFO:forza-live:notifications: nothing to write at the "
                                       "stop: notifications.json has every change: 1 "
                                       "notification for 1 player, 0 broadcasts"])

    def test_a_stop_that_cannot_write_the_file_says_so_and_loses_nothing(self):
        self.news(A)
        notices.STORE.stop()
        nid = self.news(B)
        with mock.patch.object(notices, "dump", side_effect=OSError("no space left")), \
                self.assertLogs("forza-live", "INFO") as logs:
            notices.STORE.stop()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("not written at the stop (no space left); its journal has every change",
                      logs.output[0])
        self.restart()
        self.assertEqual([n["id"] for n in notices.pending(B)], [nid])

    def server(self):
        server = srv.ForzaLive("test", 4)
        server.transport = FakeTransport()
        server.state_path = self.path(srv.STATE_FILE)
        server.friends.store.path = self.path(friends.FILE)
        self.addCleanup(server.events.db.close)
        return server

    def test_the_server_writes_it_before_the_boards_whatever_happens_to_it(self):
        old = scores.DATA_DIR
        self.addCleanup(scores.configure, old)
        scores.configure(self.dir.name, "main-1")
        scores.STORE.load()
        order = []
        with mock.patch.object(notices.STORE, "stop", side_effect=lambda: order.append("n")), \
                mock.patch.object(scores.STORE, "stop", side_effect=lambda: order.append("s")):
            srv.save_at_stop(self.server())
        self.assertEqual(order, ["n", "s"])
        with mock.patch.object(notices.STORE, "stop", side_effect=RuntimeError("a bug")), \
                mock.patch.object(scores.STORE, "stop") as boards, \
                self.assertLogs("forza-live", "ERROR") as logs:
            srv.save_at_stop(self.server())
        boards.assert_called_once_with()
        self.assertIn("stop: the notifications could not be written", logs.output[0])

    def test_a_second_and_third_signal_during_the_writes_change_nothing(self):
        """docker stop again while notifications.json is written whole at the stop, and once
        more while the boards are (#78: the signals are held for the whole stop). Neither write
        is cut short, and the log says the signals were ignored."""
        old = scores.DATA_DIR
        self.addCleanup(scores.configure, old)
        scores.configure(self.dir.name, "main-1")
        scores.STORE.load()
        nid = self.news(A, "only in the journal")
        real = notices.dump

        def dump(*args):
            signal.raise_signal(signal.SIGTERM)  # docker stop again, in the middle of it
            return real(*args)
        boards_stop = scores.STORE.stop

        def boards():
            signal.raise_signal(signal.SIGINT)  # and Ctrl+C while the boards are written
            return boards_stop()
        for number in srv.STOP_SIGNALS:  # the worst handler there could be outside the stop
            self.addCleanup(signal.signal, number, signal.signal(number, signal.default_int_handler))
        with mock.patch.object(notices, "dump", dump), \
                mock.patch.object(scores.STORE, "stop", side_effect=boards) as stopped, \
                self.assertLogs("forza-live", "INFO") as logs:
            try:
                srv.save_at_stop(self.server())
            except KeyboardInterrupt:
                self.fail("a signal cut the stop's writes short")
        stopped.assert_called_once_with()
        self.assertIn("INFO:forza-live:stop: SIGTERM again while notifications.json and "
                      "scoreboards.json were written: ignored until they were", logs.output)
        self.assertFalse(any(line.startswith("WARNING") for line in logs.output), logs.output)
        with open(self.path("notifications.json")) as f:
            self.assertIn("only in the journal", f.read())  # written whole: alone it is complete
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)
        self.restart()
        self.assertEqual([n["id"] for n in notices.pending(A)], [nid])

    def test_rolling_back_without_the_fold(self):
        """What docs/operating.md says of a rollback after a kill with no fold: the previous
        version reads the file alone, so the notifications only in the journal are not there,
        and its next id is one they had: it gives their ids out again."""
        self.news(A, "in the file")
        notices.STORE.write()
        lost = self.news(B, "only in the journal")
        previous = as_the_previous_version_reads_it(self.dir.name)  # a kill, then the rollback
        self.assertEqual(previous_pending(previous, B), [])
        self.assertEqual(previous["nextId"], lost)  # the previous version's next notification

    def test_the_server_does_not_start_on_a_file_it_cannot_read(self):
        os.mkdir(self.path("notifications.json"))  # a read error no test runner's root passes
        done = subprocess.run([sys.executable, os.path.join(ROOT, "forza_live_server.py"),
                               "--host", "127.0.0.1", "--port", "0", "--data-dir", self.dir.name],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        last = done.stderr.strip().splitlines()[-1]
        self.assertRegex(last, r"notifications\.json cannot be read .* not started")
        self.assertNotIn("Traceback", done.stderr)

    def test_rolling_back_after_the_fold(self):
        """The command of docs/operating.md: a start and a stop with no server, after a kill,
        leave a file that has everything for the previous version."""
        shutil.copy(LIVE[1], self.path("notifications.json"))
        self.restart()
        nid = self.news(A, "only in the journal")
        self.assertNotIn("%016X" % A, as_the_previous_version_reads_it(self.dir.name)["byRecipient"])
        done = subprocess.run([sys.executable, "-c", "import logging; logging.basicConfig("
                               "level=logging.INFO); from forza_live.social import notifications;"
                               " notifications.configure(%r); notifications.STORE.load(); "
                               "notifications.STORE.stop()" % self.dir.name],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertRegex(done.stderr.strip().splitlines()[-1],
                         r"notifications\.json written whole at the stop")
        previous = as_the_previous_version_reads_it(self.dir.name)
        self.assertEqual([n["id"] for n in previous_pending(previous, A)][:1], [nid])


class SmallStoresTest(unittest.TestCase):
    """clubs.json, objectives.json, stats.json and the market's files: written whole on the
    loop, now with one call of the C encoder (durable.write_json), read as before."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_each_is_written_without_the_pure_python_encoder(self):
        folder = self.dir.name
        club, stat, shop, challenges = clubs.Clubs(), stats.Store(), market.Market(), \
            objectives.Store()
        challenges.path = os.path.join(folder, "objectives.json")
        with mock.patch.object(clubs, "DATA_DIR", folder), \
                mock.patch.object(stats, "DATA_DIR", folder), \
                mock.patch.object(market, "DATA_DIR", folder):
            club.load()["members"]["%016X" % A] = {"club": 1, "role": 3, "since": 1.5}
            stat.load()
            stat.players["%016X" % A] = {"gamertag": "A", "views": {"1": {"wins": 2}}}
            shop.load()
            shop.gifts[7] = {"id": 7, "type": "credits", "retrieved": False}
            challenges.data["completed"]["%016X" % A] = {"daily": 3}
            with mock.patch.object(json, "dump", side_effect=AssertionError("json.dump")):
                for save in (club.save, stat.save, shop.save, challenges.save):
                    save()
        for name, data in (("clubs.json", club.data), ("stats.json", {"players": stat.players}),
                           ("gifts.json", {"gifts": {"7": shop.gifts[7]}, "nextId": 1}),
                           ("objectives.json", challenges.data)):
            with open(os.path.join(folder, name)) as f:  # as every version reads it
                self.assertEqual(json.load(f), data, name)

if __name__ == "__main__":
    unittest.main()
