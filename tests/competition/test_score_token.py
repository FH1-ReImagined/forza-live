"""Score tokens (FH1 #517): a result counts only when the time since its event's start (the
game's GetBestScores as a race loads, a speed zone's GetRows as the car enters it) is at least
what it claims. With --score-tokens enforce a result faster than that, or without a start, is
kept unranked (the game is never told an error); with log, the default, it is only logged and
counted; off skips the tokens.

Run: python3 -m unittest discover -p 'test_*.py'
"""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.competition import score_token
from forza_live.competition import scores
from forza_live.title_services import lsp
from forza_live.network import xtsw as x
from tests.competition import test_ghost as tg
from tests.competition import test_leaderboard_integrity as li
from tests.competition.test_leaderboard_integrity import A_XUID, AVENTADOR, B_XUID, BOARD, C_XUID
from tests.core import test_auth as ta
from tests.support import RaceClock

LAP = 107.228


def mode(test, name):
    """score_token.MODE for the test."""
    test.addCleanup(setattr, score_token, "MODE", score_token.MODE)
    score_token.MODE = name


class Base(li.Base):
    MODE = "enforce"

    def setUp(self):
        super().setUp()
        mode(self, self.MODE)
        self.clock.race(score_token.NO_START_GRACE)   # the server has been up a while

    def start(self, xuid=A_XUID, board=199, stt=False, **source):
        """GetBestScores as the event's race loads."""
        self.call(self.svc[("Scoreboard", "GetBestScores")], xuid, x.ScoreboardGetBestScores_Params,
                  {"scoreType": scores.EVENT_TIME, "gameScoreboardId": board, "isServerTimeTrial": stt,
                   "carId": AVENTADOR, "carClassId": 7, "carDriveTypeId": 1}, **source)

    def enter_zone(self, xuid=A_XUID, board=105, view=scores.FRIENDS_AND_CLUB, **source):
        """A speed zone's HUD board, as the car enters the zone."""
        self.call(self.svc[("Scoreboard", "GetRows")], xuid, x.ScoreboardGetRows_Params, {
            "type": scores.SPEED_ZONE, "view": view, "gameScoreboardId": board,
            "isServerTimeTrial": False, "isClubBoard": False, "startAt": 1, "maxResults": 200},
            **source)

    def ranked(self, board=BOARD):
        return {k: round(e["score"], 3) for k, e in scores.STORE.boards.get(board, {}).items()}


class TokenTest(Base):
    """--score-tokens enforce."""

    def test_a_race_after_its_start_ranks(self):
        self.start()
        self.clock.race(4 * 120.0)   # 4 laps; the best one is submitted
        reply = self.submit(A_XUID, LAP)
        self.assertTrue(reply["uploadFiles"])   # its ghost is asked for, as before
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.assertEqual(scores.STORE.unranked, {})

    def test_a_time_faster_than_its_start_is_unranked(self):
        self.start()
        self.clock.race(30.0)
        with self.assertLogs("forza-live", "WARNING") as logs:
            reply = self.submit(A_XUID, LAP)
        self.assertIn("30.0 s after its start, it needs 107.2 s", logs.output[0])
        self.assertFalse(reply["uploadFiles"])   # a success to the game, no ghost asked for
        self.assertEqual(self.ranked(), {})
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (LAP, "start: 30.0 s after its start, "
                                                                    "it needs 107.2 s")})
        self.assertEqual(self.public(), [])

    def test_the_tolerance(self):
        # TOLERANCE s and SLACK of the claim: 2 + 2.1 s for this lap
        self.start()
        self.clock.race(LAP - 4.0)
        self.submit(A_XUID, LAP)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.start(B_XUID)
        self.clock.race(LAP - 4.3)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(B_XUID, LAP)
        self.assertEqual(list(self.unranked()), ["%016X" % B_XUID])

    def test_each_result_spends_its_time(self):
        self.start()
        self.clock.race(150.0)
        self.submit(A_XUID, LAP)
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.submit(A_XUID, 100.0)   # right after: the two need 207.2 s
        self.assertIn("150.0 s after its start, it needs 207.2 s (107.2 s of it for earlier "
                      "results)", logs.output[0])
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.assertEqual(list(self.unranked()), ["%016X" % A_XUID])
        self.clock.race(120.0)
        self.submit(A_XUID, 99.0)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: 99.0})

    def test_restart_from_the_results(self):
        # Seen in the game: RESTART on a Rivals event's results screen races it again without a
        # new GetBestScores. The second lap is paid for by the time since the first start.
        self.start()
        self.clock.race(286.0)   # the menu, the lap, FINISH
        self.submit(A_XUID, 138.781)
        self.clock.race(150.0)   # the restarted lap
        self.submit(A_XUID, 136.5)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: 136.5})
        self.assertEqual(scores.STORE.unranked, {})

    def test_a_restart_counts_from_the_new_start(self):
        self.start()
        self.clock.race(200.0)
        self.start()   # the race again: its own start
        self.clock.race(50.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)
        self.assertEqual(self.ranked(), {})

    def test_another_board_s_start_does_not_count(self):
        self.start(board=230)
        self.start(board=199, stt=True)   # a Monthly Rivals board of the same id
        self.clock.race(300.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)
        self.assertEqual(self.ranked(), {})

    def test_only_a_signed_in_caller_starts(self):
        # an unverified call starts nothing, and a forged one does not move the player's start
        self.start()
        self.clock.race(100.0)
        self.start(trusted=False)
        self.clock.race(20.0)
        self.submit(A_XUID, LAP)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.start(B_XUID, trusted=False)
        self.clock.race(300.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(B_XUID, LAP)
        self.assertEqual(self.unranked(), {"%016X" % B_XUID: (LAP, "start: no start")})

    def test_an_old_token_expires(self):
        self.start()
        self.clock.race(score_token.TTL + 1)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (LAP, "start: no start")})

    def test_the_server_s_own_restart(self):
        # tokens live in memory: a race that started before the restart still counts for a while
        self.start()
        self.clock.race(60.0)
        scores.configure(self.dir.name, "main-1")
        self.clock.race(200.0)
        self.submit(A_XUID, LAP)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.clock.race(score_token.NO_START_GRACE)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, 100.0)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})

    def test_a_signed_in_player_is_answered_its_ranked_best(self):
        # an unranked time does not stop the game from submitting a real one after it
        self.start()
        self.clock.race(300.0)
        self.submit(A_XUID, LAP)
        self.start()
        self.clock.race(10.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, 60.0)
        reply = self.call(self.svc[("Scoreboard", "GetBestScores")], A_XUID,
                          x.ScoreboardGetBestScores_Params,
                          {"scoreType": 3, "gameScoreboardId": 199, "isServerTimeTrial": False,
                           "carId": AVENTADOR, "carClassId": 7, "carDriveTypeId": 1})
        best = x.decode(x.ScoreboardGetBestScores_Results, reply, 12)[0]["bestScore"]["score"]
        self.assertEqual(round(best, 3), LAP)

    def test_an_unranked_time_is_ranked_again_by_an_operator(self):
        self.start()
        self.clock.race(10.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)
        _, done = self.admin("POST", "/admin/scores/rank", reason="start")
        self.assertEqual(done["changed"], 1)
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})

    def test_a_ghost_after_a_start(self):
        # the ghost checks follow as before
        self.start()
        self.clock.race(150.0)
        reply = self.submit(A_XUID, LAP)
        self.upload(A_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=LAP), tg.replay_data(lap=LAP)))
        self.assertGreater(scores.STORE.boards[BOARD]["%016X" % A_XUID]["ghostFileId"], 0)

    def test_a_speed_zone(self):
        # Rt. 20 (board 105, 1010 m between its gates), seen in the game at 34.137 m/s: GetRows
        # at the entry, SubmitScore 31.1 s later
        zone = "2:105:0"
        self.enter_zone()
        self.clock.race(31.1)
        self.submit(A_XUID, 34.137, board=105, kind=scores.SPEED_ZONE)
        self.assertEqual(self.ranked(zone), {"%016X" % A_XUID: 34.137})
        # 5 s after the entry: 202 m/s claimed, 29.6 s needed
        self.enter_zone(B_XUID)
        self.clock.race(5.0)
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.submit(B_XUID, 34.137, board=105, kind=scores.SPEED_ZONE)
        self.assertIn("5.0 s after its start, it needs 29.6 s", logs.output[0])
        self.assertEqual(list(self.unranked(zone)), ["%016X" % B_XUID])
        # the map's board of the zone (view 2) is no entry
        self.enter_zone(C_XUID, view=2)
        self.clock.race(60.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(C_XUID, 30.0, board=105, kind=scores.SPEED_ZONE)
        self.assertEqual(self.unranked(zone)["%016X" % C_XUID], (30.0, "start: no start"))

    def test_a_zone_without_its_distance_and_a_speed_trap(self):
        # nothing to time: no start needed
        self.submit(A_XUID, 40.0, board=103, kind=scores.SPEED_ZONE)
        self.submit(A_XUID, 45.0, board=230, kind=scores.SPEED_TRAP)
        self.assertEqual(self.ranked("2:103:0"), {"%016X" % A_XUID: 40.0})
        self.assertEqual(self.ranked("1:230:0"), {"%016X" % A_XUID: 45.0})

    def test_an_unverified_result_stays_unranked_as_before(self):
        self.submit(B_XUID, LAP, trusted=False)
        self.assertEqual(self.unranked(), {"%016X" % B_XUID: (LAP, "unverified")})

    def test_open_tokens_are_bounded(self):
        self.addCleanup(setattr, score_token, "MAX_TOKENS", score_token.MAX_TOKENS)
        score_token.MAX_TOKENS = 3
        for n in range(5):
            self.start(A_XUID + n)
        self.assertEqual(len(score_token._tokens), 3)
        self.clock.race(300.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)   # the oldest went
        self.submit(A_XUID + 4, LAP)
        self.assertEqual(self.ranked(), {"%016X" % (A_XUID + 4): LAP})


class SignedTokenTest(ta.Base):
    """The whole path through lsp.service_call with a signing game."""

    score = li.SignedScoresTest.score
    best = li.SignedScoresTest.best

    def setUp(self):
        super().setUp()
        mode(self, "enforce")
        self.clock = RaceClock(self)
        scores.configure(self.tmp.name, "main-1")
        self.addCleanup(scores.configure, self.tmp.name, "")
        self.clock.race(score_token.NO_START_GRACE)

    def test_a_signed_race_and_a_forged_too_fast_one(self):
        self.sign_in()
        self.best(ta.XUID)
        self.clock.race(400.0)
        self.assertEqual(self.status(self.score(ta.XUID, LAP)), (0, 0))
        self.assertEqual(round(scores.STORE.boards[BOARD]["%016X" % ta.XUID]["score"], 3), LAP)
        # the same key, a lap of 60 s sent 5 s after its start: a success to it, unranked
        self.best(ta.XUID)
        self.clock.race(5.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(self.status(self.score(ta.XUID, 60.0)), (0, 0))
        self.assertEqual(round(scores.STORE.boards[BOARD]["%016X" % ta.XUID]["score"], 3), LAP)
        row = scores.STORE.unranked[BOARD]["%016X" % ta.XUID]
        self.assertEqual((row["unranked"], row["verified"]),
                         ("start: 5.0 s after its start, it needs 60.0 s", True))
        # a caller with someone else's key starts nothing for the player
        self.best(ta.XUID, seed=ta.SEED_B)
        self.clock.race(5.0)
        with self.assertLogs("forza-live", "WARNING"):
            self.score(ta.XUID, 50.0)
        self.assertEqual(scores.STORE.unranked[BOARD]["%016X" % ta.XUID]["unranked"],
                         "start: 10.0 s after its start, it needs 50.0 s")



class LogModeTest(Base):
    """--score-tokens log (the default): the tokens run the same, a failed check is only logged
    (the board and the player's public id, never a gamertag or an XUID) and counted."""

    MODE = "log"

    def test_a_failed_check_stays_ranked_and_is_logged(self):
        self.start()
        self.clock.race(30.0)
        with self.assertLogs("forza-live", "WARNING") as logs:
            reply = self.submit(A_XUID, LAP)
        self.assertTrue(reply["uploadFiles"])   # as any new best: its ghost is asked for
        self.assertEqual(self.ranked(), {"%016X" % A_XUID: LAP})
        self.assertEqual(scores.STORE.unranked, {})
        line, = logs.output
        self.assertIn("score token: board 3:199:0, player %s, score 107.228: too fast, 30.0 s "
                      "after its start, it needs 107.2 s; ranked" % scores.public_id(A_XUID), line)
        self.assertNotIn("%016X" % A_XUID, line.upper())
        self.assertNotIn("DriverA", line)
        # no start, on a speed zone
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.submit(B_XUID, 34.0, board=105, kind=scores.SPEED_ZONE)
        self.assertIn("board 2:105:0, player %s, score 34.000: no start; ranked"
                      % scores.public_id(B_XUID), logs.output[0])
        self.assertEqual(self.ranked("2:105:0"), {"%016X" % B_XUID: 34.0})
        counts = score_token.status()["score_tokens"]
        self.assertEqual({k: counts[k] for k in ("mode", "checked", "failed", "no_start", "too_fast")},
                         {"mode": "log", "checked": 2, "failed": 2, "no_start": 1, "too_fast": 1})
        self.assertEqual(counts["boards"], {"2:105:0": {"no_start": 1, "too_fast": 0},
                                            "3:199:0": {"no_start": 0, "too_fast": 1}})

    def test_a_passed_check_is_counted_only(self):
        self.start()
        self.clock.race(300.0)
        with self.assertNoLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)
        counts = score_token.status()["score_tokens"]
        self.assertEqual((counts["checked"], counts["failed"], counts["boards"]), (1, 0, {}))

    def test_counted_boards_are_bounded(self):
        self.addCleanup(setattr, score_token, "MAX_COUNTED_BOARDS", score_token.MAX_COUNTED_BOARDS)
        score_token.MAX_COUNTED_BOARDS = 2
        with self.assertLogs("forza-live", "WARNING"):
            for board in (199, 200, 201, 199):
                self.submit(A_XUID, 100.0 - board / 10, board=board)
        counts = score_token.status()["score_tokens"]
        self.assertEqual(counts["no_start"], 4)
        self.assertEqual(counts["boards"], {"3:199:0": {"no_start": 2, "too_fast": 0},
                                            "3:200:0": {"no_start": 1, "too_fast": 0}})


class OffModeTest(Base):
    """--score-tokens off: no tokens, no checks."""

    MODE = "off"

    def test_nothing_is_checked(self):
        self.call(self.svc[("Scoreboard", "GetBestScores")], A_XUID, x.ScoreboardGetBestScores_Params,
                  {"scoreType": 3, "gameScoreboardId": 199, "isServerTimeTrial": False,
                   "carId": AVENTADOR, "carClassId": 7, "carDriveTypeId": 1})
        self.assertEqual(score_token._tokens, {})
        with self.assertNoLogs("forza-live", "WARNING"):
            self.submit(A_XUID, LAP)          # 0 s after its start
            self.submit(B_XUID, LAP)          # no start
        self.assertEqual(sorted(scores.STORE.boards[BOARD]), ["%016X" % A_XUID, "%016X" % B_XUID])
        self.assertEqual(scores.STORE.unranked, {})
        self.assertEqual(score_token.status()["score_tokens"]["checked"], 0)


class ConfigureTest(unittest.TestCase):
    """--score-tokens / FORZA_SCORE_TOKENS through lsp.configure, and /status."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        mode(self, score_token.MODE)
        self.addCleanup(setattr, lsp, "DATA_DIR", lsp.DATA_DIR)
        self.addCleanup(scores.configure, self.dir.name, "")

    def configure(self, *argv):
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir.name] + list(argv)))

    def parse(self, *argv):
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        return parser.parse_args(list(argv)).score_tokens

    def test_the_default_is_log(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORZA_SCORE_TOKENS", None)
            self.assertEqual(self.parse(), "log")
            os.environ["FORZA_SCORE_TOKENS"] = "Enforce"
            self.assertEqual(self.parse(), "enforce")
            self.assertEqual(self.parse("--score-tokens", "off"), "off")   # the flag wins

    def test_modes(self):
        for name in ("enforce", "OFF", "log"):
            self.configure("--score-tokens", name)
            self.assertEqual(score_token.MODE, name.lower())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.parse("--score-tokens", "strict")
        with self.assertRaises(ValueError):   # FORZA_SCORE_TOKENS=strict stops the start
            score_token.configure("strict")

    def test_status(self):
        self.configure("--score-tokens", "enforce")
        server = srv.ForzaLive("test", 64)
        got = json.loads(json.dumps(server.status()))["score_tokens"]
        self.assertEqual((got["mode"], got["checked"], got["failed"]), ("enforce", 0, 0))
