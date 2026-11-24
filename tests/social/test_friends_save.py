"""friends.json (#67): the main loop's save of a changed store is a thread's, from a copy made
on the loop; the file is the one it always was; a write that fails is made again; a thread's
older copy never lands on a later save; save() still answers when the file is on disk.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import json
import os
import pickle
import random
import statistics
import tempfile
import threading
import time
import unittest
from unittest import mock

from forza_live.core import durable
from forza_live.social import friends as fr
from tests.support import loop_steps_under

LOOP_WORST = 0.25  # s: what the save of a store of the public server's size may hold the loop


def players(megabytes=1.7, seed=67):
    """A store of about the public server's size: 6700 players with a key, a few friends each."""
    rnd = random.Random(seed)
    keys = ["%016X" % (0x0009000000000000 + rnd.randrange(1 << 32)) for _ in range(int(megabytes * 3950))]
    return {k: {"name": "Driver%s" % k[-5:], "seen": 1790000000.5 + rnd.random() * 1e6, "key": "",
                "pub": "%064x" % rnd.getrandbits(256),
                "friends": {rnd.choice(keys): 1790000000.25 for _ in range(rnd.randrange(4))},
                "in": {}, "out": {}, "blocked": {}} for k in keys}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = fr.Store(os.path.join(self.dir.name, fr.FILE))
        self.store.players = players()
        self.store.dirty = True

    def on_disk(self):
        with open(self.store.path) as f:
            return json.load(f)


class SaveTest(Base):
    def test_the_file_is_what_it_always_was(self):
        self.assertTrue(self.store.save())
        self.assertFalse(self.store.dirty)
        self.assertEqual(self.on_disk(), {"version": 1, "players": self.store.players})
        self.assertTrue(1.5 < os.path.getsize(self.store.path) / 1048576 < 1.9)
        self.assertEqual(self.store.keyed, len(self.store.players))
        again = fr.Store(self.store.path)
        again.load()
        self.assertEqual(again.players, self.store.players)

    def test_without_an_event_loop_the_save_is_made_at_once(self):
        self.store.maybe_save()  # a tool, a test
        self.assertEqual((self.store.dirty, self.store.saving), (False, False))
        self.assertEqual(self.on_disk()["players"], self.store.players)

    def test_an_older_copy_never_lands_on_a_later_save(self):
        self.store._saves += 1
        older = self.store._saves, pickle.dumps(self.store.players)  # as maybe_save takes it
        self.store.players["0009000000000001"] = {"name": "Later", "seen": 1.0, "key": "",
                                                  "friends": {}, "in": {}, "out": {}, "blocked": {}}
        self.assertTrue(self.store.save())  # an enrollment, or the stop, before the thread ran
        self.assertTrue(self.store.write(*older))
        self.assertIn("0009000000000001", self.on_disk()["players"])


class LoopTest(Base, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # asyncio's slow-callback warning measures wall time: off, loop_steps_under counts CPU
        asyncio.get_running_loop().slow_callback_duration = 3600

    async def saved(self):
        for _ in range(6000):
            if not self.store.saving:
                return
            await asyncio.sleep(0.005)
        self.fail("friends.json was not written")

    async def test_the_main_loop_s_save_is_a_thread_s(self):
        loop, wrote, real_dump, real_replace = threading.get_ident(), [], json.dump, durable.replace

        def elsewhere(real):
            def call(*args, **kw):
                wrote.append(threading.get_ident())
                if threading.get_ident() == loop:
                    raise AssertionError("friends.json written on the event loop's thread")
                return real(*args, **kw)
            return call

        with mock.patch.object(json, "dump", elsewhere(real_dump)), \
                mock.patch.object(durable, "replace", elsewhere(real_replace)), \
                self.assertNoLogs("asyncio", "WARNING"), self.assertNoLogs("forza-live", "WARNING"), \
                loop_steps_under(LOOP_WORST):
            began = time.thread_time()
            self.store.maybe_save()  # the server's tick
            took = time.thread_time() - began
            self.assertEqual((self.store.dirty, self.store.saving), (False, True))
            self.store.maybe_save()  # the next tick, the thread still writing: nothing more
            await self.saved()
        self.assertEqual(len(wrote), 2)  # one file, written and renamed, by a thread
        self.assertLess(took, LOOP_WORST)
        self.assertFalse(self.store.dirty)
        self.assertEqual(self.on_disk(), {"version": 1, "players": self.store.players})

    async def test_the_tick_waits_for_nothing_on_the_loop(self):
        """The tick's wall time, five saves: a flush or a lock on the loop uses no CPU, which
        loop_steps_under does not see; the median of five is not moved by a descheduled one."""
        held = []
        for _ in range(5):
            self.store.dirty, self.store.saved = True, 0.0
            began = time.perf_counter()
            self.store.maybe_save()
            held.append(time.perf_counter() - began)
            await self.saved()
        self.assertLess(statistics.median(held), LOOP_WORST)

    async def test_what_changes_while_the_thread_writes_goes_with_the_next_save(self):
        writing, go, real = threading.Event(), threading.Event(), durable.replace

        def held(*args, **kw):
            writing.set()
            go.wait(30)
            return real(*args, **kw)

        with mock.patch.object(durable, "replace", held):
            self.store.maybe_save()
            await asyncio.to_thread(writing.wait, 30)
            self.store.befriend(0x0009000000000001, 0x0009000000000002)  # after the copy
            self.assertTrue(self.store.dirty)
            go.set()
            await self.saved()
        self.assertTrue(self.store.dirty)  # still to be written
        self.assertNotIn("0009000000000001", self.on_disk()["players"])
        self.store.saved = 0.0  # two seconds later
        self.store.maybe_save()
        await self.saved()
        self.assertFalse(self.store.dirty)
        self.assertEqual(self.on_disk()["players"]["0009000000000001"]["friends"],
                         self.store.players["0009000000000001"]["friends"])

    async def test_a_write_that_fails_leaves_the_store_changed(self):
        with mock.patch.object(durable, "replace", side_effect=OSError("no space left")), \
                self.assertLogs("forza-live", "WARNING") as logs:
            self.store.maybe_save()
            await self.saved()
        self.assertIn("cannot write", logs.output[0])
        self.assertTrue(self.store.dirty)
        self.store.saved = 0.0
        self.store.maybe_save()
        await self.saved()
        self.assertFalse(self.store.dirty)
        self.assertEqual(len(self.on_disk()["players"]), len(self.store.players))

    async def test_the_stop_saves_what_a_thread_was_still_writing(self):
        self.store.maybe_save()
        self.assertTrue(self.store.saving)  # server.main's stop: dirty or saving
        self.store.players["0009000000000003"] = {"name": "Last", "seen": 1.0, "key": "",
                                                  "friends": {}, "in": {}, "out": {}, "blocked": {}}
        self.assertTrue(self.store.save())
        await self.saved()
        self.assertIn("0009000000000003", self.on_disk()["players"])


if __name__ == "__main__":
    unittest.main()
