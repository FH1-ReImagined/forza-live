"""scoreboards.json and its journal (#67). With boards of the public server's size (26 MB) a
score and a ghost cost the event loop milliseconds, and the whole file is only ever written by
a thread; the server killed at any moment loses nothing a player was told was kept and always
starts again; a restart reads the same boards; the previous version's file loads, and the file
written is one the previous version reads; an operator's changes go the same way; the stop
writes the boards last and says in the log what it did.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import contextlib
import copy
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
from forza_live.competition import scores
from forza_live.core import durable
from forza_live.core import journal
from forza_live.core import limits
from forza_live.network import xtsw_all as A
from forza_live.social import friends
from forza_live.world import coop_ledger
from tests.competition import test_ghost as tg
from tests.competition import test_leaderboard_integrity as tli
from tests.support import ROOT, FakeTransport, loop_steps_under

A_XUID, B_XUID, C_XUID, BOARD = tli.A_XUID, tli.B_XUID, tli.C_XUID, tli.BOARD
TRAP = "1:230:0"
PLAYER = 0x0009000100000000  # the tests' own players: none of the generated boards' rows
LIVE_MB = 26                 # scoreboards.json on the public server
# What a score or a ghost may take of the event loop with boards of that size. Writing the file
# at every change took 500 ms on a fast desktop and 1.7 s on the public server; a line of the
# journal takes 2 to 5 ms, most of it the disk's flush. Measured in the loop thread's CPU time
# (see support.loop_steps_under), the bounds leave room for a slow CI machine. That leaves out the
# disk's flush of a journal line: a wait on the loop is seen by the wake-up lags (wall time, the
# only check here that sees it) and, for the whole file, by the thread guards of WholeFile.
LOOP_MEDIAN, LOOP_WORST = 0.05, 0.25
LIVE = None  # (its folder, the file): made once, by setUpModule
# The end of the stop's line: what the boards hold
HELD = r": \d+ boards?, \d+ rows?, \d+ unranked rows?, \d+ players? in the 1000 Club$"


def live_boards(megabytes=LIVE_MB, seed=67):
    """Boards of about `megabytes` as the previous version wrote them (json.dump, indent 1):
    some 160 boards, 6000 players, a row in 25 unranked, 1500 players in the 1000 Club."""
    rnd = random.Random(seed)
    players = ["%016X" % (0x0009000000000000 + rnd.randrange(1 << 32)) for _ in range(6000)]
    keys = ["1:%d:0" % n for n in range(201, 231)] + ["2:%d:0" % n for n in range(101, 113)] + \
           ["3:%d:0" % n for n in range(1, 120)]
    tables = {"boards": {}, "unranked": {}, "club1000": {}, "nextFileId": 60000}
    for n in range(megabytes * 1048576 // 601):
        key, player = rnd.choice(keys), rnd.choice(players)
        row = {"xuid": player, "gamertag": "Driver%s" % player[-5:], "type": int(key[0]),
               "score": rnd.uniform(40.0, 90.0), "submitted": 1790000000 + rnd.randrange(4000000),
               "scoreId": "%032x" % rnd.getrandbits(128), "carId": rnd.randrange(200, 2000),
               "carClassId": rnd.randrange(7), "carPerformanceIndex": rnd.uniform(100, 999),
               "ghostFileId": rnd.randrange(1, 60000) if key[0] == "3" else -1,
               "usedSTM": True, "usedTCS": False, "usedABS": True, "usedAutoBrake": False,
               "usedAutoShifting": True, "usedClutch": False,
               "server": "main-1", "build": "0.2.8/tu4", "verified": True}
        if n % 25 == 0:
            row.update(unranked="unverified", unrankedAt=row["submitted"], verified=False)
        tables["unranked" if n % 25 == 0 else "boards"].setdefault(key, {})[player] = row
    for player in players[:1500]:
        tables["club1000"][player] = {
            "xuid": player, "gamertag": "Driver%s" % player[-5:], "carId": 300,
            "submitted": 1790000000, "server": "main-1", "build": "0.2.8/tu4", "verified": True,
            "cars": {str(car): [rnd.getrandbits(32) for _ in range(5)]
                     for car in rnd.sample(range(200, 420), 12)}}
    return tables


def setUpModule():
    global LIVE
    folder = tempfile.TemporaryDirectory()
    path = os.path.join(folder.name, "scoreboards.json")
    with open(path, "w") as f:
        json.dump(live_boards(), f, indent=1)  # the previous version's save
    assert 24 << 20 < os.path.getsize(path) < 28 << 20, os.path.getsize(path)
    LIVE = folder, path


def tearDownModule():
    LIVE[0].cleanup()


def tables():
    """A copy of everything the store keeps on disk."""
    store = scores.STORE
    return copy.deepcopy({"boards": store.boards, "unranked": store.unranked,
                          "club1000": store.club, "nextFileId": store.next_file})


def as_a_previous_version_reads_it(folder):
    """scoreboards.json alone, read as the version before the journal read it."""
    with open(os.path.join(folder, "scoreboards.json")) as f:
        data = json.load(f)
    return {"boards": scores.migrate(data.get("boards", {})), "unranked": data.get("unranked", {}),
            "club1000": data.get("club1000", {}), "nextFileId": data.get("nextFileId", 1)}


def lap_ghost(lap):
    return tg.ghost(tg.replay_init(lap=lap), tg.replay_data(lap=lap))


class Base(tli.Base):
    def path(self, name):
        return os.path.join(self.dir.name, name)

    def restart(self):
        """A new server on the folder as it is: what a kill at this moment leaves."""
        scores.configure(self.dir.name, "main-1")
        scores.STORE.load()

    def trap(self, xuid, speed, **source):
        return self.submit(xuid, speed, board=230, kind=scores.SPEED_TRAP, **source)

    def lap(self, xuid, lap, ghost=None, **source):
        """An event time and its ghost: the upload's reply."""
        reply = self.submit(xuid, lap, **source)
        self.assertTrue(reply["uploadFiles"])
        return self.upload(xuid, reply["scoreId"], ghost or lap_ghost(lap), **source)

    def medal(self, xuid, car, thumb):
        return self.call(self.svc[("Challenge", "ChallengeCompleted")], xuid,
                         A.ForzaChallengeChallengeCompleted_Params,
                         {"carId": car, "challengeThumbprint": thumb})

    def journal_lines(self):
        with open(self.path("scoreboards.journal"), "rb") as f:
            return [json.loads(line) for line in f.read().split(b"\n")[1:-1]]


class WholeFile:
    """Inside it the whole of scoreboards.json may be written by any thread but the calling one
    (an async test's event loop): there scores.dump, the journal's checkpoint, the previous
    version's json.dump of the boards and a rename over the file all fail. `threads` are those
    that did write it."""

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
            mock.patch.object(scores, "dump", self.guard(scores.dump, "scores.dump")),
            mock.patch.object(journal.Journal, "checkpoint",
                              self.guard(journal.Journal.checkpoint, "the journal's checkpoint")),
            mock.patch.object(json, "dump", self.guard(
                json.dump, "json.dump of the boards",
                lambda obj, *rest: isinstance(obj, dict) and "boards" in obj)),
            mock.patch.object(durable, "replace", self.guard(
                durable.replace, "a rename over scoreboards.json",
                lambda tmp, path, *rest: path.endswith("scoreboards.json")))]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


class LiveSizeTest(Base, unittest.IsolatedAsyncioTestCase):
    """Boards of the public server's size, on an event loop."""

    def setUp(self):
        super().setUp()
        shutil.copy(LIVE[1], self.path("scoreboards.json"))
        with self.assertNoLogs("forza-live", "WARNING"):
            self.restart()
        self.rows_before = sum(len(b) for b in scores.STORE.boards.values())
        self.laps = [(100.0 + n, lap_ghost(100.0 + n)) for n in range(8)]

    async def asyncSetUp(self):
        # The test's loop runs in asyncio's debug mode, whose slow-callback warning measures wall
        # time: off here, answering() measures the CPU time of every step instead.
        asyncio.get_running_loop().slow_callback_duration = 3600

    def answering(self):
        """Inside it no step of the event loop may use LOOP_WORST of CPU or more, and asyncio
        logs no other warning."""
        stack = contextlib.ExitStack()
        stack.enter_context(self.assertNoLogs("asyncio", "WARNING"))
        stack.enter_context(loop_steps_under(LOOP_WORST))
        return stack

    async def races(self, lags=None):
        """8 players' event times, their ghosts and speed traps: how long each call held the
        loop. Between them the loop sleeps, and `lags` gets how late it woke."""
        took = []

        def timed(fn, *args):
            began = time.thread_time()
            result = fn(*args)
            took.append(time.thread_time() - began)
            return result

        async def breathe():
            for _ in range(5):
                began = time.perf_counter()
                await asyncio.sleep(0.002)
                if lags is not None:
                    lags.append(time.perf_counter() - began - 0.002)

        for n, (lap, ghost) in enumerate(self.laps):
            reply = timed(self.submit, PLAYER + n, lap)
            self.assertTrue(reply["uploadFiles"])
            await breathe()
            self.assertEqual(tli.status(timed(self.upload, PLAYER + n, reply["scoreId"], ghost)), (0, 0))
            await breathe()
            timed(self.trap, PLAYER + n, 60.0 + n)
            await breathe()
        return took

    async def written(self):
        for _ in range(6000):
            if not scores.STORE.journal.behind():
                return
            await asyncio.sleep(0.005)
        self.fail("scoreboards.json was not written")

    def check_races(self):
        for n, (lap, _) in enumerate(self.laps):
            player = "%016X" % (PLAYER + n)
            row = scores.STORE.boards[BOARD][player]
            self.assertEqual((round(row["score"], 3), row["ghostFileId"]), (lap, 60000 + n))
            self.assertTrue(os.path.isfile(scores.ghost_path(60000 + n)))
            self.assertEqual(round(scores.STORE.boards[TRAP][player]["score"], 3), 60.0 + n)

    async def test_a_score_and_a_ghost_cost_the_loop_milliseconds(self):
        lags = []
        with WholeFile() as whole, self.answering():
            task = asyncio.create_task(scores.keep_written(every=0.05))
            took = await self.races(lags)
            await self.written()
            task.cancel()
        self.assertEqual(whole.here, [])
        self.assertTrue(whole.threads)  # the file was written, by a thread
        self.assertLess(statistics.median(took), LOOP_MEDIAN)
        self.assertLess(max(took), LOOP_WORST)
        self.assertLess(max(lags), LOOP_WORST)  # nor does the thread's work hold the loop
        self.check_races()
        before = tables()
        self.restart()
        self.assertEqual(tables(), before)
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), before)

    async def test_the_loop_goes_on_while_a_slow_disk_takes_the_file(self):
        loop, real = threading.get_ident(), os.fsync
        flushing, go, lags = threading.Event(), threading.Event(), []

        def slow(fd):  # the thread's flush of the file takes as long as the test wants
            if threading.get_ident() != loop and not go.is_set():
                flushing.set()
                go.wait(60)
            return real(fd)

        self.trap(PLAYER + 100, 50.0)  # a change for the file to lack
        old = durable.digest(self.path("scoreboards.json"))
        killed = tempfile.TemporaryDirectory()
        self.addCleanup(killed.cleanup)
        with mock.patch.object(os, "fsync", slow), WholeFile() as whole, self.answering():
            task = asyncio.create_task(scores.keep_written(every=0.01))
            self.assertTrue(await asyncio.to_thread(flushing.wait, 60))
            took = await self.races(lags)  # all of it while the disk has not answered
            self.assertFalse(go.is_set())
            # a kill now, the new file half way to the disk
            await asyncio.to_thread(shutil.copytree, self.dir.name, os.path.join(killed.name, "data"))
            go.set()
            await self.written()
            task.cancel()
        self.assertEqual(durable.digest(os.path.join(killed.name, "data", "scoreboards.json")), old)
        self.assertEqual(whole.here, [])
        self.assertLess(statistics.median(took), LOOP_MEDIAN)
        self.assertLess(max(took), LOOP_WORST)
        self.assertLess(max(lags), LOOP_WORST)
        self.check_races()
        before = tables()
        self.assertEqual(sum(len(b) for b in before["boards"].values()), self.rows_before + 17)
        self.restart()
        self.assertEqual(tables(), before)
        scores.configure(os.path.join(killed.name, "data"), "main-1")
        scores.STORE.load()
        self.assertEqual(tables(), before)  # the kill lost none of the 25 answered calls

    async def test_the_whole_file_is_never_written_on_the_event_loop(self):
        self.trap(PLAYER, 50.0)
        with self.assertRaisesRegex(RuntimeError, "never on the event loop"):
            scores.STORE.write()
        self.assertTrue(scores.STORE.journal.behind())
        with self.answering():
            self.assertTrue(await asyncio.to_thread(scores.STORE.write, scores.STORE.snapshot()))
        self.assertFalse(scores.STORE.journal.behind())

    async def test_a_snapshot_takes_the_loop_a_few_milliseconds(self):
        began = time.thread_time()
        for _ in range(5):
            scores.STORE.snapshot()
        # 0.3 ms a snapshot on a desktop: a pointer per row (all five timed together: the clock
        # of a thread ticks every 15 ms on Windows)
        self.assertLess((time.thread_time() - began) / 5, 0.02)

    async def test_a_write_that_fails_is_made_again_and_loses_nothing(self):
        self.trap(PLAYER, 50.0)
        real, calls = scores.dump, []

        def dump(f, data):
            calls.append(1)
            if len(calls) == 1:
                f.write("{\n \"boards\": {")
                raise OSError("no space left on the device")
            return real(f, data)

        with mock.patch.object(scores, "dump", dump), self.answering(), \
                self.assertLogs("forza-live", "ERROR") as logs:
            task = asyncio.create_task(scores.keep_written(every=0.01))
            await self.written()
            task.cancel()
        self.assertIn("writing %s failed" % self.path("scoreboards.json"), logs.output[0])
        self.assertEqual(len(calls), 2)
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())

    async def test_the_stop_leaves_a_file_that_has_everything(self):
        task = asyncio.create_task(scores.keep_written(every=3600))
        self.lap(PLAYER, 101.5)
        self.medal(PLAYER, 1398, 0xAE6828D3)
        self.assertNotEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        task.cancel()
        scores.STORE.stop()  # on the loop, as the server's stop does it
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        self.assertFalse(scores.STORE.journal.behind())
        with open(self.path("scoreboards.json")) as f:
            text = f.read()
        self.assertEqual(text.count("\n"), 9 + len(scores.STORE.boards) * 2 + len(
            scores.STORE.unranked) * 2 + len(scores.STORE.club) + sum(
            len(b) for b in list(scores.STORE.boards.values()) + list(scores.STORE.unranked.values())))


class PreviousFormatTest(Base):
    def test_the_previous_version_s_file_loads_as_it_is(self):
        with open(LIVE[1]) as f:
            old = json.load(f)
        shutil.copy(LIVE[1], self.path("scoreboards.json"))
        self.restart()
        self.assertEqual(tables(), old)
        self.assertFalse(os.path.exists(self.path("scoreboards.journal")))  # nothing changed yet
        self.trap(A_XUID, 50.0)
        self.restart()
        self.assertEqual(scores.STORE.boards[TRAP]["%016X" % A_XUID]["score"], 50.0)
        self.assertEqual(len(scores.STORE.boards[TRAP]), len(old["boards"][TRAP]) + 1)

    def test_the_file_written_is_plain_json_with_the_same_tables(self):
        self.trap(A_XUID, 50.0)
        self.trap(B_XUID, 51.0, trusted=False)
        self.medal(A_XUID, 1398, 7)
        scores.STORE.write()
        with open(self.path("scoreboards.json")) as f:
            text = f.read()
        self.assertEqual(sorted(json.loads(text)), ["boards", "club1000", "nextFileId", "unranked"])
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        self.assertEqual(len(text.splitlines()), 16)  # a row to a line
        for empty in ({"boards": {}, "unranked": {}, "club1000": {}, "nextFileId": 1},
                      {"boards": {"1:230:0": {}}, "unranked": {}, "club1000": {}, "nextFileId": 3}):
            with open(self.path("empty.json"), "w") as f:
                scores.dump(f, empty)
            with open(self.path("empty.json")) as f:
                self.assertEqual(json.load(f), empty)

    def test_boards_from_before_the_type_was_in_the_key(self):
        row = dict(live_boards(1)["boards"][TRAP].popitem()[1], type=1)
        with open(self.path("scoreboards.json"), "w") as f:
            json.dump({"boards": {"230:0": {row["xuid"]: row}}}, f)
        self.restart()
        self.trap(A_XUID, 50.0)
        self.restart()
        self.assertEqual(sorted(scores.STORE.boards[TRAP]), sorted([row["xuid"], "%016X" % A_XUID]))
        scores.STORE.stop()
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())


class RestartTest(Base):
    """The same boards after a kill at every step, whatever changed them."""

    def killed_here(self):
        before = tables()
        self.restart()
        self.assertEqual(tables(), before)

    def test_every_kind_of_change_is_there_after_a_kill(self):
        self.trap(A_XUID, 50.0)
        self.killed_here()
        self.trap(A_XUID, 55.0)  # a better one replaces it
        self.trap(B_XUID, 48.0, trusted=False)  # an unverified caller's: unranked
        self.killed_here()
        self.assertEqual(tli.status(self.lap(A_XUID, 115.13)), (0, 0))
        self.killed_here()
        self.assertEqual(scores.STORE.boards[BOARD]["%016X" % A_XUID]["ghostFileId"], 1)
        reply = self.submit(A_XUID, 110.0)  # a better lap, killed before its ghost came
        self.killed_here()
        # (the row is kept; which rows wait for a ghost is not, as before: this one stays without)
        self.assertEqual(tli.status(self.upload(A_XUID, reply["scoreId"], lap_ghost(110.0))), (0, 0))
        self.assertEqual(scores.STORE.boards[BOARD]["%016X" % A_XUID]["ghostFileId"], -1)
        with self.assertLogs("forza-live", "WARNING"):
            self.lap(B_XUID, 93.294, ghost=lap_ghost(115.13))  # refused: unranked, nothing ranked
        self.killed_here()
        self.assertNotIn("%016X" % B_XUID, scores.STORE.boards[BOARD])
        self.assertEqual(scores.STORE.unranked[BOARD]["%016X" % B_XUID]["unranked"],
                         "ghost: lap time 115.130 s, the score is 93.294 s")
        self.lap(C_XUID, 120.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.lap(C_XUID, 100.0, ghost=lap_ghost(120.0))  # refused: the row before is back
        self.killed_here()
        self.assertEqual(round(scores.STORE.boards[BOARD]["%016X" % C_XUID]["score"], 3), 120.0)
        self.medal(A_XUID, 1398, 0xAE6828D3)
        self.medal(A_XUID, 1398, 0x12345678)
        self.medal(B_XUID, 1029, 1)
        self.killed_here()
        self.assertEqual(scores.STORE.club["%016X" % A_XUID]["cars"], {"1398": [0xAE6828D3, 0x12345678]})
        self.assertEqual(scores.STORE.next_file, 3)
        # all of it from the journal alone; and the file, once written, has all of it too
        self.assertFalse(os.path.exists(self.path("scoreboards.json")))
        scores.STORE.stop()
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        self.killed_here()

    def test_an_operator_s_changes_and_deletions(self):
        logs = self.assertLogs("forza-live", "WARNING")  # each change is in the log
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)
        self.lap(A_XUID, 115.13)
        self.trap(A_XUID, 50.0)
        self.trap(B_XUID, 60.0)
        self.trap(C_XUID, 70.0, trusted=False)
        scores.STORE.write()  # all of that in the file: what follows is the journal's
        player = "%016X" % A_XUID
        self.assertEqual(self.admin("POST", "/admin/scores/unrank", player=player)[1]["changed"], 2)
        self.killed_here()
        self.assertNotIn(BOARD, scores.STORE.boards)
        review = scores.review_ghost_path(scores.STORE.unranked[BOARD][player]["scoreId"])
        self.assertTrue(os.path.isfile(review))
        self.assertEqual(self.admin("POST", "/admin/scores/rank", player=player)[1]["changed"], 2)
        self.killed_here()
        self.assertEqual(self.rows(), [("DriverA", 115.13, 2)])  # its ghost back, a new file
        self.assertFalse(os.path.exists(review))
        self.assertEqual(self.admin("POST", "/admin/scores/delete",
                                    player="%016X" % C_XUID)[1]["changed"], 1)
        self.assertEqual(self.admin("POST", "/admin/scores/delete", board=BOARD)[1]["changed"], 1)
        self.killed_here()
        self.assertEqual((sorted(scores.STORE.boards), scores.STORE.unranked), ([TRAP], {}))
        self.assertEqual(sorted(scores.STORE.boards[TRAP]), [player, "%016X" % B_XUID])
        self.assertFalse(os.path.exists(scores.ghost_path(2)))
        # each of the three changes was one line of the journal, the deletions as nulls
        deleted = self.journal_lines()[-1]
        self.assertEqual(deleted, {"boards": {BOARD: {player: None}}})
        self.assertEqual(self.admin("GET", "/admin/scores")[1]["matched"], 2)

    def test_a_better_unranked_row_ranked_over_the_board_s_only_row(self):
        self.trap(A_XUID, 50.0)
        self.trap(A_XUID, 60.0, trusted=False)
        with self.assertLogs("forza-live", "WARNING"):
            self.admin("POST", "/admin/scores/rank", player="%016X" % A_XUID)
        self.assertEqual(scores.STORE.boards[TRAP]["%016X" % A_XUID]["score"], 60.0)
        self.killed_here()

    def test_impossible_speeds_removed_at_the_start_stay_removed(self):
        with self.assertLogs("forza-live", "WARNING"):
            self.trap(A_XUID, 122.2)  # 439.9 km/h on a trap whose record is 348.3
        self.trap(B_XUID, 55.0)
        scores.STORE.write()
        self.addCleanup(setattr, scores, "ENFORCE_TRAP_LIMITS", scores.ENFORCE_TRAP_LIMITS)
        scores.ENFORCE_TRAP_LIMITS = True
        with self.assertLogs("forza-live", "WARNING"):
            self.restart()
        self.assertEqual(list(scores.STORE.boards[TRAP]), ["%016X" % B_XUID])
        self.assertEqual(self.journal_lines()[-1], {"boards": {TRAP: {"%016X" % A_XUID: None}}})
        with self.assertNoLogs("forza-live", "WARNING"):
            self.restart()  # removed for good, by the journal
        self.assertEqual(list(scores.STORE.boards[TRAP]), ["%016X" % B_XUID])

    def test_the_file_is_the_boards_as_they_were_when_the_copy_was_taken(self):
        self.trap(A_XUID, 50.0)
        self.lap(B_XUID, 115.13)
        then, copied = tables(), scores.STORE.snapshot()
        self.trap(A_XUID, 55.0)   # while a thread writes the copy: a row replaced,
        self.trap(C_XUID, 60.0)   # a row more on a board it has,
        self.lap(C_XUID, 111.0)   # a ghost,
        self.medal(A_XUID, 1398, 1)  # a table that was empty,
        with self.assertLogs("forza-live", "WARNING"):  # and a board that goes
            self.admin("POST", "/admin/scores/delete", player="%016X" % B_XUID)
        now = tables()
        self.assertTrue(scores.STORE.write(copied))
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), then)
        self.restart()  # the journal has the rest
        self.assertEqual(tables(), now)

    def test_a_row_is_never_changed_once_kept(self):
        seen = []  # (the row itself, what it was when first seen)

        def look():
            store = scores.STORE
            rows = [row for table in (store.boards, store.unranked) for board in table.values()
                    for row in board.values()] + list(store.club.values())
            known = {id(row) for row, _ in seen}
            seen.extend((row, copy.deepcopy(row)) for row in rows if id(row) not in known)

        waiting = []  # a lap's row is seen before its ghost comes, and after

        def ghost(lap=None):
            xuid, score, reply = waiting.pop()
            self.upload(xuid, reply["scoreId"], lap_ghost(lap or score))

        steps = [lambda: self.trap(A_XUID, 50.0), lambda: self.trap(A_XUID, 55.0),
                 lambda: waiting.append((A_XUID, 115.13, self.submit(A_XUID, 115.13))), ghost,
                 lambda: waiting.append((A_XUID, 110.0, self.submit(A_XUID, 110.0))), ghost,
                 lambda: waiting.append((B_XUID, 93.294, self.submit(B_XUID, 93.294))),
                 lambda: ghost(115.13),  # refused
                 lambda: self.trap(B_XUID, 48.0, trusted=False),
                 lambda: self.medal(A_XUID, 1398, 1), lambda: self.medal(A_XUID, 1398, 2),
                 lambda: self.medal(A_XUID, 1029, 1), lambda: self.medal(A_XUID, 1029, 1),
                 lambda: self.admin("POST", "/admin/scores/unrank", player="%016X" % A_XUID),
                 lambda: self.admin("POST", "/admin/scores/rank", player="%016X" % A_XUID),
                 lambda: self.admin("POST", "/admin/scores/delete", player="%016X" % B_XUID)]
        with self.assertLogs("forza-live", "WARNING"):
            for step in steps:
                step()
                look()
        self.assertGreaterEqual(len(seen), 14)
        for row, was in seen:
            self.assertEqual(row, was)  # a thread writing the file reads these very dicts

    def test_a_new_name_alone_is_saved_with_the_next_change(self):
        self.medal(A_XUID, 1398, 1)
        tli.TAGS[A_XUID] = "Renamed"
        self.addCleanup(tli.TAGS.__setitem__, A_XUID, "DriverA")
        self.medal(A_XUID, 1398, 1)  # already counted: nothing to save, the name in memory
        self.assertEqual(scores.STORE.club["%016X" % A_XUID]["gamertag"], "Renamed")
        self.assertEqual(len(self.journal_lines()), 1)
        self.trap(B_XUID, 50.0)
        self.killed_here()
        self.assertEqual(scores.STORE.club["%016X" % A_XUID]["gamertag"], "Renamed")

    def test_a_change_that_cannot_be_written_is_not_acknowledged_and_goes_with_the_next(self):
        self.trap(A_XUID, 50.0)
        with mock.patch.object(os, "fsync", side_effect=OSError("no space left on the device")):
            with self.assertRaises(OSError):  # the game gets an error, not a score id
                self.trap(B_XUID, 60.0)
        self.trap(C_XUID, 70.0)
        self.killed_here()
        self.assertEqual(sorted(scores.STORE.boards[TRAP]),
                         sorted("%016X" % p for p in (A_XUID, B_XUID, C_XUID)))


class KillTest(Base):
    """What a kill leaves on the disk, cut wherever it can be cut."""

    def setUp(self):
        super().setUp()
        self.lap(A_XUID, 115.13)
        self.trap(A_XUID, 50.0)
        scores.STORE.write()       # the file before the one in place
        self.trap(B_XUID, 60.0)
        self.medal(B_XUID, 1398, 1)
        scores.STORE.write()       # the file in place
        self.lap(C_XUID, 111.0)    # and what only the journal has
        self.trap(A_XUID, 52.0)
        self.kept = tables()
        with open(self.path("scoreboards.journal"), "rb") as f:
            self.journal = f.read()

    def half_a_line(self):
        """The journal as a kill leaves it in the middle of an append."""
        line = json.dumps({"boards": {TRAP: {"%016X" % B_XUID: dict(
            self.kept["boards"][TRAP]["%016X" % B_XUID], score=99.0)}}}).encode()
        with open(self.path("scoreboards.journal"), "ab") as f:
            f.write(line[:len(line) // 2])

    def half_a_file(self, name="scoreboards.json"):
        with open(self.path(name), "r+") as f:
            f.truncate(os.path.getsize(self.path(name)) // 2)

    def starts_with_everything(self):
        self.restart()
        self.assertEqual(tables(), self.kept)
        self.trap(C_XUID, 80.0)  # and goes on: the next change and the next file are whole
        after = tables()
        self.restart()
        self.assertEqual(tables(), after)
        scores.STORE.stop()
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), after)
        self.restart()
        self.assertEqual(tables(), after)

    def test_a_journal_line_half_written(self):
        self.half_a_line()
        with self.assertLogs("forza-live", "INFO") as logs:
            self.starts_with_everything()
        self.assertIn("cut short", logs.output[0])

    def test_every_cut_of_the_last_line(self):
        last = self.journal.rstrip(b"\n").rindex(b"\n") + 1
        for size in range(last, len(self.journal)):
            with open(self.path("scoreboards.journal"), "wb") as f:
                f.write(self.journal[:size])
            self.restart()  # never refuses, and has all but the line that was being written
            self.assertEqual(scores.STORE.boards[TRAP]["%016X" % A_XUID]["score"], 50.0, size)
            self.assertEqual(scores.STORE.boards[BOARD], self.kept["boards"][BOARD], size)

    def test_the_new_file_half_written(self):
        shutil.copy(self.path("scoreboards.json"), self.path("scoreboards.json.tmp"))
        self.half_a_file("scoreboards.json.tmp")  # killed while a thread wrote it
        self.starts_with_everything()

    def test_the_file_in_place_cut_short(self):
        # what the rename's fsync is there to prevent (durable): the previous copy and the
        # journal, which reaches back to it, are the same boards
        self.half_a_file()
        with self.assertLogs("forza_live", "WARNING") as logs:
            self.starts_with_everything()
        self.assertIn("using its previous copy", logs.output[0])

    def test_both_cut(self):
        self.half_a_line()
        self.half_a_file()
        shutil.copy(self.path("scoreboards.json"), self.path("scoreboards.json.tmp"))
        with self.assertLogs("forza_live", "WARNING"):
            self.starts_with_everything()

    def test_lines_that_make_no_sense_are_skipped(self):
        with open(self.path("scoreboards.journal"), "ab") as f:
            f.write(b'{"boards": 5}\n{"club1000": []}\n{"nextFileId": "x"}\n[1]\n\x00\x00\n')
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.restart()
        self.assertIn("2 damaged lines skipped", logs.output[0])
        self.assertIn("3 that cannot be read skipped", logs.output[1])
        self.assertEqual(tables(), self.kept)

    def test_neither_file_readable_still_starts(self):
        for name in ("scoreboards.json", "scoreboards.json.bak"):
            with open(self.path(name), "w") as f:
                f.write("{\"boards\": {")
        with self.assertLogs("forza-live", "ERROR") as logs:
            self.restart()
        self.assertIn("cannot be read", logs.output[0])
        self.assertEqual(tables(), {"boards": {}, "unranked": {}, "club1000": {}, "nextFileId": 1})
        self.assertTrue(os.path.exists(self.path("scoreboards.journal.unused.1")))
        self.trap(A_XUID, 50.0)  # and keeps what it is given from there
        with self.assertLogs("forza-live", "ERROR"):
            self.restart()
        self.assertEqual(list(scores.STORE.boards[TRAP]), ["%016X" % A_XUID])


class RollbackTest(Base):
    def previous_version_saves(self, data):
        with open(self.path("scoreboards.json.tmp"), "w") as f:
            json.dump(data, f, indent=1)
        durable.replace(self.path("scoreboards.json.tmp"), self.path("scoreboards.json"))

    def test_after_a_clean_stop_the_previous_version_has_everything(self):
        self.lap(A_XUID, 115.13)
        self.trap(B_XUID, 60.0)
        self.medal(B_XUID, 1398, 1)
        scores.STORE.stop()
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())

    def test_after_a_kill_it_lacks_only_what_came_since_the_last_whole_file(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()  # the server's task, every FULL_EVERY seconds
        written = tables()
        self.trap(B_XUID, 60.0)  # within the last minute: the journal alone has it
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), written)
        self.assertLessEqual(scores.FULL_EVERY, 60)

    def test_what_the_previous_version_wrote_is_kept_when_the_new_one_comes_back(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()
        self.lap(B_XUID, 111.0)  # ghost 1, journal only: killed, and rolled back
        old = as_a_previous_version_reads_it(self.dir.name)
        row = dict(old["boards"][TRAP]["%016X" % A_XUID], xuid="%016X" % C_XUID, score=77.0)
        old["boards"][TRAP]["%016X" % C_XUID] = row
        old["nextFileId"] = 5  # it took ghosts of its own meanwhile
        self.previous_version_saves(old)
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.restart()
        self.assertRegex(logs.output[0], "scoreboards.journal does not continue the "
                                         "scoreboards.json that is here")
        self.assertEqual(tables(), old)  # as it left them: no row or ghost id of before comes back
        self.assertTrue(os.path.exists(self.path("scoreboards.journal.unused.1")))
        self.trap(B_XUID, 61.0)
        before = tables()
        self.restart()
        self.assertEqual(tables(), before)

    def test_a_file_edited_by_hand_after_a_clean_stop(self):
        self.trap(A_XUID, 50.0)
        self.trap(B_XUID, 60.0)
        scores.STORE.stop()
        edited = as_a_previous_version_reads_it(self.dir.name)
        del edited["boards"][TRAP]["%016X" % B_XUID]
        with open(self.path("scoreboards.json"), "w") as f:
            json.dump(edited, f)
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.restart()
        self.assertEqual(tables(), edited)  # the file as it was edited, the journal put aside
        self.assertIn("cannot tell whether the file here has them. Kept as %s"
                      % self.path("scoreboards.journal.unused.1"), logs.output[0])

    def test_the_command_of_the_operator_s_guide_puts_the_journal_in_the_file(self):
        guide = os.path.join(ROOT, "docs", "operating.md")
        if not os.path.exists(guide):
            self.skipTest("no docs here")
        with open(guide) as f:
            [line] = [line for line in f if "scores.STORE.stop()" in line]
        code = line.strip().split(' -c "', 1)[1].rsplit('"', 1)[0]  # python3 -c "<this>"
        self.assertEqual(code.count("'/data'"), 1)
        code = code.replace("'/data'", repr(self.dir.name))
        self.lap(A_XUID, 115.13)
        scores.STORE.write()
        self.trap(B_XUID, 60.0)   # what only the journal has when the server is killed
        self.lap(A_XUID, 110.0)
        self.medal(B_XUID, 1398, 1)
        with self.assertLogs("forza-live", "WARNING"):
            self.admin("POST", "/admin/scores/delete", player="%016X" % B_XUID, board=TRAP)
        kept = tables()
        self.assertNotEqual(as_a_previous_version_reads_it(self.dir.name), kept)
        said = []
        for _ in range(2):  # with the server stopped; the second time there is nothing to do
            done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                  text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            said.append(done.stderr.strip().splitlines()[-1])
        self.assertRegex(said[0], r"scoreboards\.json written whole at the stop in .* s: it has "
                                  r"every change" + HELD)
        self.assertRegex(said[1], r"nothing to write at the stop: scoreboards\.json has every "
                                  r"change" + HELD)
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), kept)
        self.restart()
        self.assertEqual(tables(), kept)

    def test_the_previous_copy_put_back_needs_the_journal_gone_too(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()
        self.trap(B_XUID, 60.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.admin("POST", "/admin/scores/delete", player="%016X" % A_XUID)  # by mistake
        scores.STORE.stop()
        with open(self.path("scoreboards.json.bak")) as f:
            wanted = json.load(f)  # the boards before the mistake
        self.assertIn("%016X" % A_XUID, wanted["boards"][TRAP])
        shutil.copy(self.path("scoreboards.json.bak"), self.path("scoreboards.json"))
        self.restart()  # the journal continues that copy: the delete is applied again
        self.assertNotIn("%016X" % A_XUID, scores.STORE.boards[TRAP])
        scores.configure(self.dir.name, "main-1")
        shutil.copy(self.path("scoreboards.json.bak"), self.path("scoreboards.json"))
        os.remove(self.path("scoreboards.journal"))  # as the operator's guide says
        self.restart()
        self.assertEqual(tables()["boards"], wanted["boards"])


class StopTest(Base):
    """The server's stop (server.save_at_stop): the boards last, and a line in the log."""

    def server(self):
        server = srv.ForzaLive("test", 4)
        server.transport = FakeTransport()
        server.state_path = self.path(srv.STATE_FILE)
        server.friends.store.path = self.path(friends.FILE)
        return server

    def test_the_stores_without_a_journal_are_saved_before_the_boards(self):
        server, ledger = self.server(), mock.Mock(dirty=True)
        server.friends.store.seen(A_XUID, "DriverA")
        self.trap(A_XUID, 50.0)
        # the boards' last write ended by an exception (no longer a stop signal: those are held)
        with mock.patch.object(coop_ledger, "LEDGER", ledger), \
                mock.patch.object(scores.STORE, "write", side_effect=KeyboardInterrupt):
            self.assertRaises(KeyboardInterrupt, srv.save_at_stop, server)
        ledger.save.assert_called_once_with()
        again = friends.Store(server.friends.store.path)
        again.load()
        self.assertEqual(again.name(A_XUID), "DriverA")
        self.assertTrue(os.path.exists(server.state_path))
        self.restart()  # and the boards lost nothing: the journal had the score
        self.assertEqual(scores.STORE.boards[TRAP]["%016X" % A_XUID]["score"], 50.0)

    def test_the_log_says_what_the_stop_did_with_the_file(self):
        self.trap(A_XUID, 50.0)
        with self.assertLogs("forza-live", "INFO") as logs:
            srv.save_at_stop(self.server())
        self.assertRegex(logs.output[-1], r"scoreboards: scoreboards\.json written whole at the "
                                          r"stop in \d+\.\d\d s: it has every change: 1 board, "
                                          r"1 row, 0 unranked rows, 0 players in the 1000 Club$")
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        with self.assertLogs("forza-live", "INFO") as logs:
            scores.STORE.stop()  # stopped again with nothing new
        self.assertEqual(logs.output, ["INFO:forza-live:scoreboards: nothing to write at the "
                                       "stop: scoreboards.json has every change: 1 board, 1 row, "
                                       "0 unranked rows, 0 players in the 1000 Club"])

    def test_a_stop_that_cannot_write_the_file_says_so_and_loses_nothing(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.stop()
        self.trap(B_XUID, 60.0)
        with mock.patch.object(scores, "dump", side_effect=OSError("no space left on the device")), \
                self.assertLogs("forza-live", "INFO") as logs:
            scores.STORE.stop()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("not written at the stop (no space left on the device); its journal has "
                      "every change", logs.output[0])
        self.assertNotIn("%016X" % B_XUID, as_a_previous_version_reads_it(self.dir.name)["boards"][TRAP])
        self.restart()
        self.assertEqual(scores.STORE.boards[TRAP]["%016X" % B_XUID]["score"], 60.0)

    def test_a_store_that_was_never_read_writes_nothing_at_the_stop(self):
        scores.configure(self.dir.name, "main-1")
        with self.assertNoLogs("forza-live", "INFO"):
            scores.STORE.stop()
        self.assertEqual(os.listdir(self.dir.name), [])


class TwoFaultsTest(Base):
    """The cases the reviews of #69 left (#70): the boards' start and stop with two faults."""

    def server(self):
        server = StopTest.server(self)
        self.addCleanup(server.events.db.close)
        return server

    def unreadable(self):
        """scoreboards.json there but impossible to read: a folder in its place (a read error,
        as a disk or the file's permissions give, that no test runner's root gets past)."""
        os.remove(self.path("scoreboards.json"))
        os.mkdir(self.path("scoreboards.json"))

    def test_a_file_that_cannot_be_read_falls_back_to_its_copy(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()
        self.trap(B_XUID, 60.0)
        scores.STORE.write()
        self.trap(C_XUID, 70.0)  # in the journal only
        kept = tables()
        real = open

        def failing(name, *args, **kw):  # a disk's read error, on the file in place only
            if os.path.abspath(name) == self.path("scoreboards.json"):
                raise OSError(5, "Input/output error", name)
            return real(name, *args, **kw)
        with mock.patch.object(durable, "open", failing, create=True), \
                self.assertLogs("forza_live", "WARNING") as logs:
            self.restart()
        self.assertIn("cannot be read", logs.output[0])
        self.assertEqual(tables(), kept)
        scores.STORE.write()  # and the copy it read is kept, not the file that failed
        self.assertEqual(durable.load(self.path("scoreboards.json"))["boards"], kept["boards"])
        with open(self.path("scoreboards.json.bak")) as f:
            self.assertEqual(list(json.load(f)["boards"][TRAP]), ["%016X" % A_XUID])
        self.restart()  # and the next start reads the new file, with nothing put aside
        self.assertEqual(tables(), kept)
        self.assertEqual([n for n in os.listdir(self.dir.name) if "unused" in n], [])

    def test_a_file_that_cannot_be_read_and_no_copy_refuses_the_start(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()
        self.unreadable()
        scores.configure(self.dir.name, "main-1")
        with self.assertLogs("forza-live", "ERROR") as logs, \
                self.assertRaises(OSError) as raised:
            scores.STORE.load()
        self.assertIsInstance(raised.exception, journal.Unreadable)
        self.assertIn("not started", logs.output[-1])
        self.assertFalse(scores.STORE.loaded)  # not a store of empty boards
        with self.assertRaises(journal.Unreadable), self.assertLogs("forza-live", "ERROR"):
            scores.STORE.board(scores.SPEED_TRAP, 230, 0)  # nor later
        self.assertTrue(os.path.isdir(self.path("scoreboards.json")))
        self.assertTrue(os.path.exists(self.path("scoreboards.journal")))

    def test_a_damaged_file_and_a_copy_that_cannot_be_read_refuses_the_start(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.write()
        self.trap(B_XUID, 60.0)
        scores.STORE.write()
        self.trap(C_XUID, 70.0)  # in the journal only
        kept = tables()
        size = os.path.getsize(self.path("scoreboards.json"))
        with open(self.path("scoreboards.json"), "wb") as f:  # zeros, as after a power cut
            f.write(b"\0" * size)
        with open(self.path("scoreboards.json.bak"), "rb") as f:
            bak = f.read()
        real = open

        def failing(name, *args, **kw):  # and the .bak's mode, or the disk, refuses it
            if os.path.abspath(name) == self.path("scoreboards.json.bak"):
                raise PermissionError(13, "Permission denied", name)
            return real(name, *args, **kw)
        scores.configure(self.dir.name, "main-1")
        with mock.patch.object(durable, "open", failing, create=True), \
                self.assertLogs("forza-live", "ERROR") as logs, \
                self.assertRaises(journal.Unreadable):
            scores.STORE.load()
        self.assertRegex(logs.output[-1], r"scoreboards\.json is damaged .* its previous copy "
                                          r"scoreboards\.json\.bak .* cannot be read .* not started")
        self.assertFalse(scores.STORE.loaded)
        with open(self.path("scoreboards.json.bak"), "rb") as f:
            self.assertEqual(f.read(), bak)  # not replaced by the zeros
        self.assertEqual([n for n in os.listdir(self.dir.name) if "unused" in n], [])
        with self.assertLogs("forza_live", "WARNING"):  # readable again: every score is back
            self.restart()
        self.assertEqual(tables(), kept)

    def test_a_store_that_fails_at_the_stop_logs_its_own_error_only(self):
        server = self.server()

        def friends_save():
            try:
                raise ValueError("the friends' own first error")
            except ValueError:
                raise OSError(28, "No space left on device")
        server.friends.store.dirty = True
        server.friends.store.save = friends_save
        self.trap(A_XUID, 50.0)
        with self.assertLogs("forza-live", "INFO") as logs:
            try:  # the stop runs in main()'s finally, while the signal is being handled
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                srv.save_at_stop(server)
        [failed] = [line for line in logs.output if "could not be written" in line]
        self.assertIn("No space left on device", failed)
        self.assertIn("the friends' own first error", failed)  # its own chain stays
        self.assertNotIn("KeyboardInterrupt", failed)
        self.assertEqual(failed.count("During handling"), 1, failed)  # its own, only
        self.assertRegex(logs.output[-1], "written whole at the stop")

    def test_the_server_does_not_start_on_a_file_it_cannot_read(self):
        os.mkdir(self.path("scoreboards.json"))
        done = subprocess.run([sys.executable, os.path.join(ROOT, "forza_live_server.py"),
                               "--host", "127.0.0.1", "--port", "0", "--data-dir", self.dir.name],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(done.returncode, 0)
        last = done.stderr.strip().splitlines()[-1]
        self.assertRegex(last, r"scoreboards\.json cannot be read .* not started")
        self.assertNotIn("listening", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_start_that_fails_leaves_the_store_unread(self):
        self.trap(A_XUID, 50.0)
        scores.configure(self.dir.name, "main-1")
        with mock.patch.object(journal.Journal, "load", side_effect=RuntimeError("a bug")):
            self.assertRaises(RuntimeError, scores.STORE.load)
        self.assertFalse(scores.STORE.loaded)
        scores.STORE.load()  # the next one reads the boards
        self.assertEqual(scores.STORE.boards[TRAP]["%016X" % A_XUID]["score"], 50.0)

    def test_an_earlier_save_that_fails_never_skips_the_boards(self):
        for n, failing in enumerate(("save_state", "ledger", "friends")):
            with self.subTest(failing=failing):
                server, ledger = self.server(), mock.Mock(dirty=True)
                server.friends.store.seen(A_XUID, "DriverA")
                self.trap(A_XUID, 50.0 + n)  # a new best each time
                if failing == "save_state":
                    server.save_state = mock.Mock(side_effect=TypeError("a bug"))
                elif failing == "ledger":
                    ledger.save.side_effect = ValueError("a bug")
                else:
                    server.friends.store.save = mock.Mock(side_effect=RuntimeError("a bug"))
                with mock.patch.object(coop_ledger, "LEDGER", ledger), \
                        self.assertLogs("forza-live", "INFO") as logs:
                    srv.save_at_stop(server)
                self.assertTrue(any("a bug" in line for line in logs.output), logs.output)
                self.assertRegex(logs.output[-1], "written whole at the stop")
                self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
                if failing != "ledger":
                    ledger.save.assert_called_once_with()
                if failing != "friends":
                    again = friends.Store(server.friends.store.path)
                    again.load()
                    self.assertEqual(again.name(A_XUID), "DriverA")

    def test_a_second_stop_signal_waits_for_the_small_stores(self):
        server, ledger = self.server(), mock.Mock(dirty=True)
        server.friends.store.seen(A_XUID, "DriverA")
        self.trap(A_XUID, 50.0)
        saved = server.save_state

        def save_state():
            signal.raise_signal(signal.SIGTERM)  # docker stop again, at the first save
            return saved()
        server.save_state = save_state
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler)  # as run() has it
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with mock.patch.object(coop_ledger, "LEDGER", ledger), \
                self.assertLogs("forza-live", "INFO") as logs:
            try:
                srv.save_at_stop(server)
            except KeyboardInterrupt:
                self.fail("the second signal cut the stores' saves short")
        ledger.save.assert_called_once_with()
        again = friends.Store(server.friends.store.path)
        again.load()
        self.assertEqual(again.name(A_XUID), "DriverA")
        self.assertTrue(os.path.exists(server.state_path))
        self.assertIn("INFO:forza-live:stop: SIGTERM again while the registrations, co-op ledger "
                      "and friends were written: ignored until they were", logs.output)
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        # and after the stop's writes the handler is the one from before them again
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)

    def test_the_stop_tells_the_truth_when_the_journal_cannot_take_the_last_change(self):
        self.trap(A_XUID, 50.0)
        scores.STORE.stop()
        self.trap(B_XUID, 60.0)
        scores.STORE.put("boards", TRAP, "%016X" % C_XUID,
                         dict(scores.STORE.boards[TRAP]["%016X" % B_XUID], xuid="%016X" % C_XUID))
        full = OSError(28, "No space left on device")
        with mock.patch.object(journal.Journal, "append", side_effect=full), \
                mock.patch.object(scores, "dump", side_effect=full), \
                self.assertLogs("forza-live", "INFO") as logs:
            scores.STORE.stop()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertNotIn("has every change", logs.output[0])
        self.assertIn("could not be added to its journal", logs.output[0])
        # and when only the journal fails, the whole file still takes the change
        with mock.patch.object(journal.Journal, "append", side_effect=full), \
                self.assertLogs("forza-live", "INFO") as logs:
            scores.STORE.stop()
        self.assertRegex(logs.output[-1], "written whole at the stop .*: it has every change")
        self.assertIn("%016X" % C_XUID, as_a_previous_version_reads_it(self.dir.name)["boards"][TRAP])

    def fold(self, folder):
        """The last line of the operator's guide's command that puts the journal in the file."""
        with open(os.path.join(ROOT, "docs", "operating.md")) as f:
            [line] = [line for line in f if "scores.STORE.stop()" in line]
        code = line.strip().split(' -c "', 1)[1].rsplit('"', 1)[0].replace("'/data'", repr(folder))
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stderr.strip().splitlines()[-1]

    def test_the_fold_command_says_what_it_read(self):
        if not os.path.exists(os.path.join(ROOT, "docs", "operating.md")):
            self.skipTest("no docs here")
        wrong = os.path.join(self.dir.name, "not-the-data-folder")
        os.mkdir(wrong)
        last = self.fold(wrong)
        self.assertNotIn("has every change", last)
        self.assertRegex(last, r"no scoreboards\.json .* nothing to write")
        self.trap(A_XUID, 50.0)
        self.lap(B_XUID, 115.13)
        self.medal(B_XUID, 1398, 1)
        scores.STORE.stop()
        last = self.fold(self.dir.name)
        self.assertRegex(last, r"nothing to write at the stop: scoreboards\.json has every change: "
                               r"2 boards, 2 rows, 0 unranked rows, 1 player in the 1000 Club$")


CHILD = r"""
import asyncio, sys
from forza_live.competition import scores
from forza_live.core import limits
from forza_live.network import xtsw as x
from tests.competition import test_ghost as tg
from tests.competition.test_leaderboard_integrity import score_data

folder, n, player = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
limits.configure(folder)
scores.configure(folder, "main-1")
scores.RACE_TRACKS, scores.EVENTS = {199: {156}}, {}
svc = scores.services(lambda xuid: "Driver")
scores.STORE.load()


def call(name, xuid, params_type, params):
    return svc[("Scoreboard", name)](xuid, bytes(x.encode(params_type, params)))


async def main():
    global n
    writing = asyncio.create_task(scores.keep_written(every=0.02))
    while True:
        xuid = player + n % 40
        speed = 50.0 + n * 0.001
        call("SubmitScore", xuid, x.ScoreboardSubmitScore_Params,
             {"type": 1, "data": score_data(230, speed)})
        print("trap %d %.3f" % (xuid, speed), flush=True)  # the game has its answer
        if n % 4 == 0:
            lap = 90.0 - n * 0.001
            reply = call("SubmitScoreEx", xuid, x.ScoreboardSubmitScoreEx_Params,
                         {"type": 3, "data": {"base": score_data(199, lap), "numCarsPassed": 0}})
            score_id = x.decode(x.ScoreboardSubmitScore_Results, reply, 12)[0]["scoreId"]
            print("lap %d %.3f" % (xuid, lap), flush=True)
            call("UploadFile", xuid, x.ScoreboardUploadFile_Params, {"scoreId": score_id,
                 "data": tg.ghost(tg.replay_init(lap=lap), tg.replay_data(lap=lap))})
            print("ghost %d %.3f" % (xuid, lap), flush=True)
        n += 1
        await asyncio.sleep(0)

asyncio.run(main())
"""


class RealKillTest(Base):
    """A server process on boards of the public server's size, killed while it takes scores and
    ghosts and writes the file every 20 ms: five times over, each start on what the kill left."""

    def test_killed_at_any_moment_nothing_acknowledged_is_lost(self):
        shutil.copy(LIVE[1], self.path("scoreboards.json"))
        rnd, told, done, replayed = random.Random(67), {}, 0, 0
        errors = open(self.path("child.log"), "w+")
        self.addCleanup(errors.close)
        for kill in range(5):
            child = subprocess.Popen([sys.executable, "-c", CHILD, self.dir.name, str(done),
                                      str(PLAYER)], cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=errors, text=True)
            lines, after = [], rnd.randrange(2, 150)
            for line in child.stdout:
                lines.append(line)
                if len(lines) >= after:
                    break
            time.sleep(rnd.random() * 0.05)
            child.kill()
            lines += child.stdout.read().splitlines(keepends=True)
            child.stdout.close()
            errors.seek(0)
            self.assertEqual(child.wait(), -9 if os.name == "posix" else child.returncode,
                             errors.read())
            self.assertGreaterEqual(len(lines), after, errors.read())
            for line in lines:
                if line.endswith("\n"):  # the kill may cut the last one
                    what, xuid, value = line.split()
                    told[what, int(xuid)] = float(value)
                    done += what == "trap"
            self.restart()  # the next start: never refused
            replayed += scores.STORE.journal.behind()
            for (what, xuid), value in told.items():
                player = "%016X" % xuid
                if what == "trap":
                    self.assertGreaterEqual(scores.STORE.boards[TRAP][player]["score"], value - 0.002)
                    continue
                row = scores.STORE.boards[BOARD][player]
                self.assertLessEqual(row["score"], value + 0.002)
                if what == "ghost" and abs(row["score"] - value) < 0.002:
                    self.assertGreater(row["ghostFileId"], 0)
                    self.assertTrue(os.path.isfile(scores.ghost_path(row["ghostFileId"])))
        self.assertGreater(len(told), 20)
        self.assertGreater(replayed, 0)  # kills that left changes in the journal alone
        scores.STORE.stop()
        self.assertEqual(as_a_previous_version_reads_it(self.dir.name), tables())
        limits.count()  # the ghosts the killed servers wrote are the store's


if __name__ == "__main__":
    unittest.main()
