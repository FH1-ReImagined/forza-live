"""firstScore in players/<XUID>.json: when the player first had a result on this server's boards.
The website accounts service links a website account only to a key whose record has it, and
counts that key's likes. A result from a call carrying the player's own key sets it (a speed
trap, speed zone, event time or 1000 Club medal, a best or not); an unverified caller's does not.
Players who had results before the server kept it get it at the start, from the rows the boards
already rank and the unranked rows of games from before signed requests ("unverified"), not the
too-fast or refused-ghost ones (lsp.backfill_first_scores), so they need no new upload.

The data below has the shapes the server's files have (scoreboards.json with the boards by
"type:sbid:stt", a board from before the type was in the key, rows from before the tags, unranked
rows, 1000 Club entries put back by hand without "xuid") with made-up players."""

import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

from forza_live.competition import scores
from forza_live.title_services import lsp
from forza_live.network import xtsw_all as A
from tests.competition import test_leaderboard_integrity as tli

A_XUID, B_XUID = tli.A_XUID, tli.B_XUID
TRAP = 230   # the I-70 speed trap


def path_of(folder, xuid):
    return os.path.join(folder, "players", "%016X.json" % xuid)


def write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f)


def read(path):
    with open(path) as f:
        return json.load(f)


class Live(tli.Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, lsp, "DATA_DIR", lsp.DATA_DIR)
        self.addCleanup(scores.configure, self.dir.name, "")
        lsp.DATA_DIR = self.dir.name
        scores.configure(self.dir.name, "main-1", scored=lsp.first_score)
        lsp._scored.clear()
        self.addCleanup(lsp._scored.clear)
        for xuid in (A_XUID, B_XUID):  # Logon2 made their records
            write(path_of(self.dir.name, xuid), {"gamertag": tli.TAGS[xuid], "logons": 1})

    def record(self, xuid=A_XUID):
        return read(path_of(self.dir.name, xuid))

    def test_a_signed_in_player_s_first_speed_trap(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertEqual(self.record(), {"gamertag": "DriverA", "logons": 1, "firstScore": 1_800_000_000})
        with mock.patch("time.time", return_value=1_800_009_999):
            self.submit(A_XUID, 50.0, board=TRAP, kind=scores.SPEED_TRAP)
            lsp._scored.clear()  # a new process: the record is read again, and kept
            self.submit(A_XUID, 51.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertEqual(self.record()["firstScore"], 1_800_000_000)

    def test_zones_event_times_and_a_result_that_is_not_a_best(self):
        self.submit(A_XUID, 38.0, board=101, kind=scores.SPEED_ZONE)
        self.submit(B_XUID, 107.228)
        self.assertIn("firstScore", self.record(A_XUID))
        self.assertIn("firstScore", self.record(B_XUID))
        # A slower time than the row the player has: not a best, but a result all the same.
        os.remove(path_of(self.dir.name, B_XUID))
        write(path_of(self.dir.name, B_XUID), {"gamertag": "DriverB"})
        lsp._scored.clear()
        self.submit(B_XUID, 120.0)
        self.assertEqual(round(scores.STORE.boards[tli.BOARD]["%016X" % B_XUID]["score"], 3), 107.228)
        self.assertIn("firstScore", self.record(B_XUID))

    def test_an_unverified_caller_s_result_waits_for_the_next_start(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP, trusted=False)
        self.assertEqual(self.record(), {"gamertag": "DriverA", "logons": 1})
        # Its unranked row is a game's from before signed requests: counted at the next start (a
        # link still needs a request signed by the key the XUID is bound to).
        self.assertEqual(scores.first_results(), {"%016X" % A_XUID: 1_800_000_000})
        self.assertEqual(lsp.backfill_first_scores(), 1)
        self.assertEqual(self.record()["firstScore"], 1_800_000_000)

    def test_a_too_fast_result_sets_nothing(self):
        with mock.patch.object(scores.score_token, "check", return_value="12.0 s after the start"), \
                mock.patch.object(scores.score_token, "MODE", "enforce"):
            self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertTrue(scores.STORE.unranked)
        self.assertNotIn("firstScore", self.record())
        self.assertEqual(scores.first_results(), {})
        self.assertEqual(lsp.backfill_first_scores(), 0)

    def test_a_failing_hook_does_not_fail_the_result(self):
        with mock.patch.object(lsp, "save_player", side_effect=OSError(28, "No space left on device")), \
                self.assertLogs("forza-live", "WARNING") as logs:
            self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertIn("%016X" % A_XUID, scores.STORE.boards["1:%d:0" % TRAP])
        self.assertIn("firstScore of %016X not kept: [Errno 28]" % A_XUID, "\n".join(logs.output))
        self.submit(A_XUID, 46.0, board=TRAP, kind=scores.SPEED_TRAP)  # the next one writes it
        self.assertIn("firstScore", self.record())

    def test_no_record_or_an_empty_one_gets_no_file(self):
        missing = 0x0009000000000077
        self.submit(missing, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertFalse(os.path.exists(path_of(self.dir.name, missing)))
        write(path_of(self.dir.name, A_XUID), {})
        self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertEqual(self.record(), {})

    def test_a_record_put_back_without_it_gets_it_again(self):
        with mock.patch("time.time", return_value=1_800_000_000):
            self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertIn(A_XUID, lsp._scored)
        write(path_of(self.dir.name, A_XUID), {"gamertag": "DriverA", "logons": 2})  # a restore
        with mock.patch("time.time", return_value=1_800_000_500):
            self.submit(A_XUID, 46.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertEqual(self.record(), {"gamertag": "DriverA", "logons": 2, "firstScore": 1_800_000_500})
        with mock.patch.object(lsp, "load_player", side_effect=AssertionError("read again")):
            self.submit(A_XUID, 47.0, board=TRAP, kind=scores.SPEED_TRAP)  # unchanged: a stat only

    def test_a_1000_club_medal(self):
        done = self.svc[("Challenge", "ChallengeCompleted")]
        params = {"carId": tli.AVENTADOR, "challengeThumbprint": 0xAE6828D3}
        self.call(done, A_XUID, A.ForzaChallengeChallengeCompleted_Params, params, trusted=False)
        self.assertNotIn("firstScore", self.record())
        self.call(done, A_XUID, A.ForzaChallengeChallengeCompleted_Params, params)
        self.assertIn("firstScore", self.record())

    def test_without_the_hook_nothing_is_written(self):
        scores.configure(self.dir.name, "main-1")  # tools and tests that use the boards alone
        self.submit(A_XUID, 45.0, board=TRAP, kind=scores.SPEED_TRAP)
        self.assertNotIn("firstScore", self.record())


def legacy_data(folder):
    """A data folder as the public server has it before firstScore (made-up players)."""
    def row(xuid, score, at, **tags):
        return dict({"xuid": xuid, "gamertag": "P" + xuid[-2:], "type": 1, "score": score, "submitted": at,
                     "scoreId": "00" * 16, "carId": 1398, "carClassId": 7, "carPerformanceIndex": 0.86,
                     "ghostFileId": -1}, **tags)
    write(os.path.join(folder, "scoreboards.json"), {
        "boards": {
            # a speed trap: a row from before the tags, and a tagged one
            "1:230:0": {"0009000000000011": row("0009000000000011", 60.0, 1_790_000_500),
                        "0009000000000012": row("0009000000000012", 61.0, 1_790_000_900,
                                                server="main-1", build="0.2.0/tu4", verified=True)},
            # an event time on a board from before the type was in the key ("sbid:stt")
            "199:0": {"0009000000000011": dict(row("0009000000000011", 107.2, 1_790_000_100), type=3)},
            # a speed zone of a player who never signed in here (no record), and a banned one's
            "2:101:0": {"0009000000000099": row("0009000000000099", 38.0, 1_790_000_000),
                        "0009000000000013": row("0009000000000013", 39.0, 1_790_000_200)},
        },
        "unranked": {"1:230:0": {"0009000000000014": dict(row("0009000000000014", 70.0, 1_790_000_300),
                                                          unranked="unverified"),
                                 # kept off for a time faster than the start allows
                                 "0009000000000018": dict(row("0009000000000018", 90.0, 1_790_000_400),
                                                          unranked="start: 3.0 s after the start")},
                     "3:199:0": {"0009000000000019": dict(row("0009000000000019", 50.0, 1_790_000_400),
                                                          type=3, unranked="ghost: wrong track")},
                     # an unverified row older than the player's ranked one
                     "2:101:0": {"0009000000000011": dict(row("0009000000000011", 37.0, 1_790_000_050),
                                                          unranked="unverified")}},
        # put back by hand after an outage: no "xuid"; one with no medals at all
        "club1000": {"0009000000000015": {"gamertag": "P15", "cars": {"1398": [2926979283]}},
                     "0009000000000016": {"gamertag": "P16", "cars": {}}},
        "nextFileId": 7,
    })
    signed = {"firstSigned": 1_790_000_000, "signedDays": 3, "lastSignedDay": 20717}
    for n, rec in {
        0x11: dict(signed, gamertag="P11", logons=40),
        0x12: {"gamertag": "P12", "logons": 2},                  # only unsigned sign-ins so far
        0x13: dict(signed, gamertag="P13"),
        0x14: dict(signed, gamertag="P14"),                       # an unverified unranked row only
        0x15: dict(signed, gamertag="P15"),
        0x16: dict(signed, gamertag="P16"),
        0x17: dict(signed, gamertag="P17", firstScore=1_700_000_000),  # already has it
        0x18: dict(signed, gamertag="P18"),                       # a too-fast unranked row only
        0x19: dict(signed, gamertag="P19"),                       # a refused ghost's row only
    }.items():
        write(path_of(folder, 0x0009000000000000 + n), rec)


class Backfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.addCleanup(setattr, lsp, "DATA_DIR", lsp.DATA_DIR)
        self.addCleanup(scores.configure, self.dir, "")
        self.addCleanup(lsp._scored.clear)
        legacy_data(self.dir)

    def first(self, n):
        return read(path_of(self.dir, 0x0009000000000000 + n)).get("firstScore")

    def start(self):
        lsp.DATA_DIR = self.dir
        scores.configure(self.dir, "main-1", scored=lsp.first_score)
        lsp._scored.clear()
        return lsp.backfill_first_scores()

    def test_players_with_results_qualify_without_a_new_upload(self):
        before = read(path_of(self.dir, 0x0009000000000011))
        with self.assertLogs("forza-live", "INFO") as logs:
            self.assertEqual(self.start(), 5)
        self.assertIn("firstScore kept for 5 players", "\n".join(logs.output))
        self.assertEqual(self.first(0x11), 1_790_000_050)   # its earliest row, an unverified one
        self.assertEqual(self.first(0x12), 1_790_000_900)   # also without a signed sign-in (see below)
        self.assertEqual(self.first(0x13), 1_790_000_200)   # banned or not: the accounts service checks bans
        self.assertIsNotNone(self.first(0x15))              # a 1000 Club medal (no time: the start's)
        self.assertEqual(self.first(0x14), 1_790_000_300)   # a game's from before signed requests
        self.assertIsNone(self.first(0x18))                 # too fast for its start
        self.assertIsNone(self.first(0x19))                 # its ghost refused
        self.assertIsNone(self.first(0x16))                 # a club entry without medals
        self.assertEqual(self.first(0x17), 1_700_000_000)   # kept as it was
        self.assertFalse(os.path.exists(path_of(self.dir, 0x0009000000000099)))  # no record made
        after = read(path_of(self.dir, 0x0009000000000011))
        self.assertEqual(after, dict(before, firstScore=1_790_000_050))  # nothing else changes

    def test_every_start_after_the_first_writes_nothing(self):
        self.assertEqual(self.start(), 5)
        stamp = os.stat(path_of(self.dir, 0x0009000000000011)).st_mtime_ns
        self.assertEqual(self.start(), 0)
        self.assertEqual(os.stat(path_of(self.dir, 0x0009000000000011)).st_mtime_ns, stamp)

    def test_the_server_s_start_runs_it(self):
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir]))
        self.assertEqual(self.first(0x11), 1_790_000_050)
        self.assertIs(scores.SCORED, lsp.first_score)

    def test_no_scoreboards_file(self):
        os.remove(os.path.join(self.dir, "scoreboards.json"))
        self.assertEqual(self.start(), 0)


if __name__ == "__main__":
    unittest.main()
