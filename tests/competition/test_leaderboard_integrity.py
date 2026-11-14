"""Leaderboard integrity (FH1 #517): new rows carry the server and the build, the boards take
results only from players signed in here, a Rivals ghost that fails its checks leaves its score
unranked (kept for review, the previous row back), and /admin/scores finds rows by source.

Run: python3 -m unittest discover -p 'test_*.py'
"""

import json
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

from forza_live.core import auth
from forza_live.competition import leaderboards as lb
from forza_live.core import limits
from forza_live.title_services import lsp
from forza_live import server as srv
from forza_live.competition import scores
from forza_live.network import xtsw as x
from forza_live.network import xtsw_all as A
from tests.core import test_auth as ta
from tests.competition import test_ghost as tg
from tests.support import DiskTotals, RaceClock

ALL = srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT
KEY = bytes(15) + bytes([7])
A_XUID, B_XUID, C_XUID = 0x0009000000000001, 0x0009000000000B02, 0x0009000000000C03
TAGS = {A_XUID: "DriverA", B_XUID: "DriverB", C_XUID: "DriverC"}
AVENTADOR, S7 = 1398, 432
BOARD = "3:199:0"
NO_TIME = round(scores.FLT_MAX, 3)   # the game's "no time yet" (scores.no_best)


def score_data(board, score, car=AVENTADOR):
    return {"gameScoreboardId": board, "isServerTimeTrial": False, "score": score, "carId": car,
            "carClassId": 7, "carPerformanceIndex": 0.864, "usedSTM": False, "usedTCS": False,
            "usedABS": False, "usedAutoBrake": False, "usedAutoShifting": False,
            "usedClutch": False}


def status(reply):
    return struct.unpack_from(">II", reply)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = RaceClock(self)
        scores.configure(self.dir.name, "main-1")
        lb.configure()
        self.svc = scores.services(lambda xuid: TAGS.get(xuid, "%016X" % xuid))
        self.addCleanup(self.dir.cleanup)
        # the ghosts count toward the disk quota: counted here, forgotten after
        self.addCleanup(limits.configure, limits._data_dir)
        limits.configure(self.dir.name)
        limits.count()
        # every ghost written, moved or deleted is counted: checked at each test's end
        self.addCleanup(DiskTotals().check, self)
        self.addCleanup(setattr, scores, "RACE_TRACKS", scores.RACE_TRACKS)
        scores.RACE_TRACKS = {199: {156}}   # GoPro Millennia Masters: Clear Springs Circuit
        self.addCleanup(setattr, scores, "EVENTS", scores.EVENTS)
        scores.EVENTS = {}                  # no game database: every board is in a rollup

    def call(self, fn, xuid, params_type, params, trusted=True, build="0.2.0/tu4"):
        token = scores.set_source(trusted, build)
        try:
            return fn(xuid, bytes(x.encode(params_type, params)))
        finally:
            scores.reset_source(token)

    def submit(self, xuid, score, board=199, kind=scores.EVENT_TIME, car=AVENTADOR, **source):
        reply = self.call(self.svc[("Scoreboard", "SubmitScoreEx")], xuid,
                          x.ScoreboardSubmitScoreEx_Params,
                          {"type": kind, "data": {"base": score_data(board, score, car),
                                                  "numCarsPassed": 0}}, **source)
        return x.decode(x.ScoreboardSubmitScore_Results, reply, 12)[0]

    def upload(self, xuid, score_id, data, **source):
        return self.call(self.svc[("Scoreboard", "UploadFile")], xuid,
                         x.ScoreboardUploadFile_Params, {"scoreId": score_id, "data": data},
                         **source)

    def rows(self, xuid=C_XUID, board=199, kind=scores.EVENT_TIME):
        reply = self.call(self.svc[("Scoreboard", "GetRows")], xuid, x.ScoreboardGetRows_Params, {
            "type": kind, "view": 0, "gameScoreboardId": board, "isServerTimeTrial": False,
            "isClubBoard": False, "startAt": 1, "maxResults": 50})
        r = x.decode(x.ScoreboardGetRows_Results, reply, 12)[0]
        return [(row["user"]["wzGamerTag"], round(row["score"], 3), row["ghostFileId"])
                for row in r["rows"]]

    def public(self):
        lb.forget_answers()
        status_, _, body = lb.handle("GET", "/leaderboards/snapshot")
        return [(b["id"], r["gamertag"], r["score"]) for b in json.loads(body)["boards"]
                for r in b["rows"]]

    def unranked(self, board=BOARD):
        return {k: (round(e["score"], 3), e["unranked"])
                for k, e in scores.STORE.unranked.get(board, {}).items()}

    def admin(self, method, path, **query):
        status_, ctype, body = scores.admin(method, path, {k: [v] for k, v in query.items()})
        return status_, json.loads(body) if ctype == "application/json" else body


class TagsTest(Base):
    def test_new_rows_carry_the_server_and_the_build(self):
        self.submit(A_XUID, 107.228)
        self.submit(B_XUID, 45.0, board=230, kind=scores.SPEED_TRAP, build="git-908ea56c5b/tu4")
        self.submit(C_XUID, 38.0, board=101, kind=scores.SPEED_ZONE, build="")
        a = scores.STORE.boards[BOARD]["%016X" % A_XUID]
        b = scores.STORE.boards["1:230:0"]["%016X" % B_XUID]
        c = scores.STORE.boards["2:101:0"]["%016X" % C_XUID]
        self.assertEqual((a["server"], a["build"], a["verified"]), ("main-1", "0.2.0/tu4", True))
        self.assertEqual((b["server"], b["build"]), ("main-1", "git-908ea56c5b/tu4"))
        self.assertEqual(c["build"], "")  # a game before X-Forza-Build
        # kept across restarts
        scores.configure(self.dir.name, "main-1")
        scores.STORE.load()
        self.assertEqual(scores.STORE.boards[BOARD]["%016X" % A_XUID]["build"], "0.2.0/tu4")

    def test_the_server_id_is_kept_without_the_option(self):
        scores.configure(self.dir.name, "")
        made = scores.server_id()
        self.assertRegex(made, r"^server-[0-9a-f]{8}$")
        scores.configure(self.dir.name, "")
        self.assertEqual(scores.server_id(), made)
        scores.configure(self.dir.name, "eu/1 (test)")
        self.assertEqual(scores.server_id(), "eu/1__test_")

    def test_rows_from_before_the_tags_stay(self):
        old = {"xuid": "%016X" % B_XUID, "gamertag": "DriverB", "type": 3, "score": 99.0,
               "submitted": 1, "scoreId": "00" * 16, "carId": 1, "carClassId": 7,
               "carPerformanceIndex": 0.5, "ghostFileId": -1, "usedSTM": 0, "usedTCS": 0,
               "usedABS": 0, "usedAutoBrake": 0, "usedAutoShifting": 0, "usedClutch": 0}
        with open(os.path.join(self.dir.name, "scoreboards.json"), "w") as f:
            json.dump({"boards": {BOARD: {old["xuid"]: old}}}, f)
        scores.configure(self.dir.name, "main-1")
        self.submit(A_XUID, 107.228)
        self.assertEqual(self.rows(), [("DriverB", 99.0, -1), ("DriverA", 107.228, -1)])
        self.assertEqual(self.public(), [("event-199", "DriverB", 99.0), ("event-199", "DriverA", 107.228)])
        self.assertEqual([e["xuid"] for e in scores.rollup_entries(3, -1)][:1], [old["xuid"]])
        self.assertNotIn("server", scores.STORE.boards[BOARD][old["xuid"]])
        # the old row is its player's best for the game, signed in or not, and a better time ranks
        for trusted in (True, False):
            self.assertEqual(scores.own_best(B_XUID, 3, 199, False, trusted), 99.0)
        _, listed = self.admin("GET", "/admin/scores", build="none")
        self.assertEqual([r["xuid"] for r in listed["rows"]], [old["xuid"]])
        self.submit(B_XUID, 98.5)
        self.assertEqual(self.rows()[0], ("DriverB", 98.5, -1))
        self.assertEqual(scores.STORE.boards[BOARD][old["xuid"]]["server"], "main-1")


class SignedInOnlyTest(Base):
    def test_an_unverified_result_is_kept_off_the_boards(self):
        self.submit(B_XUID, 120.0)
        reply = self.submit(A_XUID, 90.0, trusted=False, build="0.1.36/tu4")
        self.assertFalse(reply["uploadFiles"])
        self.assertEqual(self.rows(), [("DriverB", 120.0, -1)])
        self.assertEqual(self.public(), [("event-199", "DriverB", 120.0)])
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (90.0, "unverified")})
        row = scores.STORE.unranked[BOARD]["%016X" % A_XUID]
        self.assertEqual((row["server"], row["build"], row["verified"]), ("main-1", "0.1.36/tu4", False))
        # the player's own row is not there either; a slower unverified one keeps the best
        self.assertEqual(self.rows(A_XUID), [("DriverB", 120.0, -1)])
        self.submit(A_XUID, 95.0, trusted=False)
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (90.0, "unverified")})
        # speed traps and zones too, and no board appears for them
        self.submit(A_XUID, 45.0, board=230, kind=scores.SPEED_TRAP, trusted=False)
        self.assertNotIn("1:230:0", scores.STORE.boards)
        self.assertEqual(self.unranked("1:230:0"), {"%016X" % A_XUID: (45.0, "unverified")})

    def test_the_1000_club_counts_signed_in_players_only(self):
        params = {"carId": AVENTADOR, "challengeThumbprint": 0xAE6828D3}
        done = self.svc[("Challenge", "ChallengeCompleted")]
        self.call(done, A_XUID, A.ForzaChallengeChallengeCompleted_Params, params, trusted=False)
        self.assertEqual(scores.club_entries(), [])
        self.call(done, A_XUID, A.ForzaChallengeChallengeCompleted_Params, params)
        entry = scores.STORE.club["%016X" % A_XUID]
        self.assertEqual((scores.club_medals(entry), entry["server"], entry["build"], entry["verified"]),
                         (1, "main-1", "0.2.0/tu4", True))


class GhostTest(Base):
    def best(self, xuid, score, car=AVENTADOR, ghost=None):
        """A new best and its ghost (one of that lap unless given): the upload's reply."""
        reply = self.submit(xuid, score, car=car)
        self.assertTrue(reply["uploadFiles"])
        data = ghost if ghost is not None else tg.ghost(
            tg.replay_init(lap=score, car=car, name=TAGS[xuid]), tg.replay_data(lap=score))
        return self.upload(xuid, reply["scoreId"], data)

    def download(self, file_id):
        reply = self.call(self.svc[("Scoreboard", "DownloadFile")], C_XUID,
                          x.ScoreboardDownloadFile_Params, {"id": file_id, "maxSize": 256000})
        return x.decode(x.ScoreboardDownloadFile_Results, reply, 12)[0]["data"]

    def test_a_good_ghost_is_kept_without_the_driver_s_name(self):
        self.assertEqual(status(self.best(A_XUID, 115.13)), (0, 0))
        [(tag, score, file_id)] = self.rows()
        self.assertEqual((tag, score), ("DriverA", 115.13))
        kept = self.download(file_id)
        self.assertNotIn("DriverA".encode("utf-16-be"), b"".join(
            scores.ghostfile.inflate(tg.blocks(kept)[0], 1 << 16)))
        self.assertIsNone(tg.problem(kept, 115.13))

    def test_a_ghost_that_fails_leaves_the_score_unranked_and_the_previous_row(self):
        self.best(A_XUID, 115.13)
        [(_, _, old_ghost)] = self.rows()
        # a faster time with the ghost of the old lap, then a time in another car
        with self.assertLogs("forza-live", "WARNING") as logs:
            reply = self.best(A_XUID, 93.294, ghost=tg.ghost(tg.replay_init(lap=115.13),
                                                             tg.replay_data(lap=115.13)))
        self.assertEqual(status(reply)[0], 2)  # a failure the game carries on from
        self.assertIn("lap time 115.130 s, the score is 93.294 s", logs.output[0])
        self.assertEqual(self.rows(), [("DriverA", 115.13, old_ghost)])
        self.assertEqual(self.download(old_ghost)[:4], self.download(old_ghost)[:4])
        self.assertEqual(self.unranked(),
                         {"%016X" % A_XUID: (93.294, "ghost: lap time 115.130 s, the score is 93.294 s")})
        review = scores.STORE.unranked[BOARD]["%016X" % A_XUID]
        self.assertTrue(os.path.isfile(scores.review_ghost_path(review["scoreId"])))
        self.assertEqual(self.public(), [("event-199", "DriverA", 115.13)])
        # a first score whose ghost fails leaves nothing on the board
        self.best(B_XUID, 100.0, car=S7, ghost=tg.ghost(tg.replay_init(lap=100.0)))
        self.assertEqual(self.rows(), [("DriverA", 115.13, old_ghost)])
        self.assertIn("%016X" % B_XUID, self.unranked())

    def test_the_board_s_track(self):
        self.best(A_XUID, 115.13, ghost=tg.ghost(tg.replay_init(lap=115.13, track=107),
                                                tg.replay_data(lap=115.13)))
        self.assertEqual(self.unranked()["%016X" % A_XUID][1], "ghost: track 107, the board's is 156")
        scores.RACE_TRACKS = {}  # without the game's database the track is not checked
        self.best(B_XUID, 115.13, ghost=tg.ghost(tg.replay_init(lap=115.13, track=107),
                                                tg.replay_data(lap=115.13)))
        self.assertEqual(self.rows(), [("DriverB", 115.13, 1)])

    def test_only_the_submitter_uploads_its_ghost(self):
        reply = self.submit(A_XUID, 115.13)
        stranger = self.upload(B_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=115.13),
                                                                  tg.replay_data(lap=115.13)))
        self.assertEqual(status(stranger), (0, 0))
        self.assertEqual(self.rows(), [("DriverA", 115.13, -1)])
        self.upload(A_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=115.13),
                                                      tg.replay_data(lap=115.13)))
        self.assertEqual(self.rows(), [("DriverA", 115.13, 1)])

    def test_a_new_best_keeps_the_old_ghost_until_its_own_passes(self):
        self.best(A_XUID, 115.13)
        first = scores.ghost_path(1)
        reply = self.submit(A_XUID, 110.0)
        self.assertTrue(os.path.isfile(first))  # still the row to come back to
        self.upload(A_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=110.0),
                                                      tg.replay_data(lap=110.0)))
        self.assertFalse(os.path.isfile(first))
        self.assertEqual(self.rows(), [("DriverA", 110.0, 2)])


    def test_the_disk_quota_follows_a_ghost_off_the_board_and_back(self):
        self.best(A_XUID, 115.13)
        kept = os.path.getsize(scores.ghost_path(1))
        self.assertEqual(limits.usage(), kept)  # what is kept of the upload, not the upload
        player = "%016X" % A_XUID
        self.admin("POST", "/admin/scores/unrank", player=player)  # its ghost goes to review
        review = scores.review_ghost_path(scores.STORE.unranked[BOARD][player]["scoreId"])
        self.assertFalse(os.path.exists(scores.ghost_path(1)))
        self.assertEqual(limits.usage(), os.path.getsize(review))
        self.admin("POST", "/admin/scores/rank", player=player)  # and back, as a new file
        self.assertEqual(self.rows(), [("DriverA", 115.13, 2)])
        self.assertFalse(os.path.exists(review))
        self.assertEqual(limits.usage(), os.path.getsize(scores.ghost_path(2)))
        self.best(A_XUID, 110.0)  # a better lap: its ghost in the old one's place
        self.assertEqual(limits.usage(), os.path.getsize(scores.ghost_path(3)))
        self.admin("POST", "/admin/scores/delete", player=player)
        self.assertEqual(limits.usage(), 0)

    def test_a_full_quota_keeps_the_row_without_its_ghost(self):
        self.best(B_XUID, 120.0)
        used = limits.usage()
        reply = self.submit(A_XUID, 115.13)
        data = tg.ghost(tg.replay_init(lap=115.13, name="DriverA"), tg.replay_data(lap=115.13))
        with mock.patch.object(limits, "DISK_CAP", used + len(data) - 1):
            # the title services answer a ValueError with "bad parameters": the game carries on
            with self.assertRaisesRegex(ValueError, "disk quota reached"):
                self.upload(A_XUID, reply["scoreId"], data)
        self.assertEqual(self.rows(), [("DriverA", 115.13, -1), ("DriverB", 120.0, 1)])
        self.assertEqual(limits.usage(), used)


class AdminTest(Base):
    def setUp(self):
        super().setUp()
        self.submit(A_XUID, 107.228, build="0.2.0/tu4")
        self.submit(B_XUID, 93.294, build="0.1.36/tu4")
        self.submit(B_XUID, 45.0, board=230, kind=scores.SPEED_TRAP, build="0.1.36/tu4")
        self.submit(C_XUID, 80.0, trusted=False, build="0.1.36/tu4")

    def test_list_by_source(self):
        code, listed = self.admin("GET", "/admin/scores", build="0.1.36/tu4")
        self.assertEqual(code, 200)
        self.assertEqual(sorted((r["state"], r["board"], r["xuid"]) for r in listed["rows"]),
                         [("ranked", "1:230:0", "%016X" % B_XUID), ("ranked", BOARD, "%016X" % B_XUID),
                          ("unranked", BOARD, "%016X" % C_XUID)])
        self.assertEqual(listed["server"], "main-1")
        _, listed = self.admin("GET", "/admin/scores", server="main-1", verified="0")
        self.assertEqual([r["xuid"] for r in listed["rows"]], ["%016X" % C_XUID])
        _, listed = self.admin("GET", "/admin/scores", board="event-199", state="ranked")
        self.assertEqual(sorted(r["xuid"] for r in listed["rows"]), ["%016X" % A_XUID, "%016X" % B_XUID])
        _, listed = self.admin("GET", "/admin/scores", player="%X" % A_XUID)
        self.assertEqual(listed["matched"], 1)
        self.assertEqual(self.admin("GET", "/admin/scores", bogus="1")[0], 400)
        self.assertEqual(self.admin("GET", "/admin/scores", board="nope-1")[0], 400)
        self.assertEqual(self.admin("GET", "/admin/scores", state="maybe")[0], 400)

    def test_a_bad_build_is_unranked_ranked_again_or_deleted(self):
        self.assertEqual(self.admin("GET", "/admin/scores/unrank", build="0.1.36/tu4")[0], 405)
        self.assertEqual(self.admin("POST", "/admin/scores/unrank", state="ranked")[0], 400)
        code, done = self.admin("POST", "/admin/scores/unrank", build="0.1.36/tu4", state="ranked")
        self.assertEqual((code, done["changed"]), (200, 2))
        self.assertEqual(self.rows(), [("DriverA", 107.228, -1)])
        self.assertEqual(self.public(), [("event-199", "DriverA", 107.228)])
        self.assertEqual(self.unranked()["%016X" % B_XUID], (93.294, "operator"))
        # after review: back on the board
        code, done = self.admin("POST", "/admin/scores/rank", player="%016X" % B_XUID)
        self.assertEqual(done["changed"], 2)
        self.assertEqual(self.rows(), [("DriverB", 93.294, -1), ("DriverA", 107.228, -1)])
        # or gone
        _, done = self.admin("POST", "/admin/scores/delete", build="0.1.36/tu4")
        self.assertEqual(done["changed"], 3)
        self.assertEqual(self.rows(), [("DriverA", 107.228, -1)])
        self.assertEqual(scores.STORE.unranked, {})
        self.assertEqual(self.admin("GET", "/admin/scores")[1]["matched"], 1)

    def test_ranking_again_keeps_a_better_row(self):
        self.admin("POST", "/admin/scores/unrank", player="%016X" % A_XUID)
        self.submit(A_XUID, 100.0)
        _, done = self.admin("POST", "/admin/scores/rank", player="%016X" % A_XUID)
        self.assertEqual(done["changed"], 0)  # the 107.228 under review is not better
        self.assertEqual(self.rows(), [("DriverB", 93.294, -1), ("DriverA", 100.0, -1)])


class OwnBestTest(Base):
    """Unranked rows and GetBestScores' own best (FH1 #569): the game submits a time only when it
    beats the answer, so the answer is what the server will do with the caller's next result."""

    def best(self, xuid, board=199, kind=scores.EVENT_TIME, **source):
        reply = self.call(self.svc[("Scoreboard", "GetBestScores")], xuid,
                          x.ScoreboardGetBestScores_Params,
                          {"scoreType": kind, "gameScoreboardId": board, "isServerTimeTrial": False,
                           "carId": AVENTADOR, "carClassId": 7, "carDriveTypeId": 1}, **source)
        self.clock.race()   # the race that follows its start
        return round(x.decode(x.ScoreboardGetBestScores_Results, reply, 12)[0]["bestScore"]["score"], 3)

    def rival(self, xuid, board=199, rival=0, with_best=True, **source):
        fn = self.svc[("Scoreboard", "GetRivalAndPersonalBest" if with_best else "GetRival")]
        reply = self.call(fn, xuid, x.ScoreboardGetRival_Params,
                          {"gameScoreboardId": board, "isServerTimeTrial": False, "rivalXuid": rival},
                          **source)
        r = x.decode(x.ScoreboardGetRivalAndPersonalBest_Results if with_best
                     else x.ScoreboardGetRival_Results, reply, 12)[0]
        # (the rival's gamertag, or None for the empty board's row of the caller at 0 s; the
        # caller's personal best)
        data = r["rival"]["scoreData"]
        return (data["user"]["wzGamerTag"] if data["score"] else None,
                round(r["personalBest"]["score"], 3) if with_best else None)

    def test_an_unverified_caller_is_answered_its_own_unranked_best(self):
        self.submit(B_XUID, 120.0)
        self.assertEqual(self.best(A_XUID, trusted=False), NO_TIME)
        self.submit(A_XUID, 90.0, trusted=False)
        # the game now submits only a better time; the server keeps only that one in any case
        self.assertEqual(self.best(A_XUID, trusted=False), 90.0)
        self.submit(A_XUID, 95.0, trusted=False)
        self.assertEqual(self.best(A_XUID, trusted=False), 90.0)
        self.submit(A_XUID, 80.0, trusted=False)
        self.assertEqual(self.best(A_XUID, trusted=False), 80.0)
        # nobody else is told: not the other unverified callers, not the signed-in ones, not the
        # board, a rival or the rollup
        self.assertEqual(self.best(C_XUID, trusted=False), NO_TIME)
        self.assertEqual(self.best(C_XUID), NO_TIME)
        self.assertEqual(self.best(B_XUID, trusted=False), 120.0)   # its own ranked row
        self.assertEqual(self.rows(C_XUID), [("DriverB", 120.0, -1)])
        self.assertEqual(self.rival(C_XUID), ("DriverB", NO_TIME))
        # asked for A, who is not on the board: the rival it would get anyway
        self.assertEqual(self.rival(C_XUID, rival=A_XUID), ("DriverB", NO_TIME))
        self.assertEqual([e["xuid"] for e in scores.rollup_entries(3, -1)], ["%016X" % B_XUID])

    def test_a_signed_in_caller_is_answered_its_ranked_best_only(self):
        # A result it sent before it signed in here is unranked; it must not keep the game from
        # submitting the time that ranks now.
        self.submit(A_XUID, 90.0, trusted=False)
        self.assertEqual(self.best(A_XUID), NO_TIME)
        self.assertEqual(self.rival(A_XUID), (None, NO_TIME))   # the empty board's row
        self.assertTrue(self.submit(A_XUID, 100.0)["uploadFiles"])
        self.assertEqual(self.best(A_XUID), 100.0)
        self.assertEqual(self.rival(A_XUID, with_best=True), ("DriverA", 100.0))   # its own
        self.assertEqual(self.rows(), [("DriverA", 100.0, -1)])
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (90.0, "unverified")})
        # the same player unverified again: its better result, wherever it is kept
        self.assertEqual(self.best(A_XUID, trusted=False), 90.0)
        self.submit(A_XUID, 110.0, trusted=False)
        self.assertEqual(self.best(A_XUID, trusted=False), 90.0)
        self.assertEqual(self.best(A_XUID), 100.0)

    def test_an_unverified_caller_with_a_ranked_row_is_not_asked_for_slower_times(self):
        self.submit(A_XUID, 100.0)
        self.assertEqual(self.best(A_XUID, trusted=False), 100.0)
        self.submit(A_XUID, 105.0, trusted=False)   # the game would not send it
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (105.0, "unverified")})
        self.assertEqual(self.best(A_XUID, trusted=False), 100.0)
        self.submit(A_XUID, 95.0, trusted=False)
        self.assertEqual(self.best(A_XUID, trusted=False), 95.0)
        self.assertEqual(self.rows(), [("DriverA", 100.0, -1)])

    def test_speeds_are_better_when_bigger(self):
        self.submit(A_XUID, 40.0, board=230, kind=scores.SPEED_TRAP)
        self.submit(A_XUID, 45.0, board=230, kind=scores.SPEED_TRAP, trusted=False)
        self.assertEqual(self.best(A_XUID, 230, scores.SPEED_TRAP, trusted=False), 45.0)
        self.assertEqual(self.best(A_XUID, 230, scores.SPEED_TRAP), 40.0)
        self.submit(B_XUID, 30.0, board=230, kind=scores.SPEED_TRAP, trusted=False)
        self.submit(B_XUID, 20.0, board=230, kind=scores.SPEED_TRAP, trusted=False)
        self.assertEqual(self.best(B_XUID, 230, scores.SPEED_TRAP, trusted=False), 30.0)
        self.assertEqual(self.best(B_XUID, 230, scores.SPEED_TRAP), 0.0)   # a speed's "none"

    def test_a_refused_ghost_leaves_the_previous_best_as_the_answer(self):
        reply = self.submit(A_XUID, 115.13)
        self.upload(A_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=115.13),
                                                      tg.replay_data(lap=115.13)))
        reply = self.submit(A_XUID, 60.0)
        self.assertEqual(self.best(A_XUID), 60.0)   # until its ghost is checked
        with self.assertLogs("forza-live", "WARNING"):
            self.upload(A_XUID, reply["scoreId"], tg.ghost(tg.replay_init(lap=115.13),
                                                          tg.replay_data(lap=115.13)))
        self.assertEqual(self.best(A_XUID), 115.13)
        self.assertEqual(self.rival(A_XUID), ("DriverA", 115.13))
        self.assertEqual(self.unranked()["%016X" % A_XUID][0], 60.0)

    def test_an_unverified_caller_does_not_tell_a_rival(self):
        from forza_live.social import notifications
        notifications.configure(self.dir.name)
        self.submit(B_XUID, 120.0)
        self.submit(A_XUID, 125.0)   # a ranked row from before
        notify = self.svc[("Scoreboard", "NotifyRival")]
        params = {"gameScoreboardId": 199, "isServerTimeTrial": False, "rivalXuid": B_XUID,
                  "type": scores.EVENT_TIME, "myScoreId": bytes(16)}
        self.call(notify, A_XUID, x.ScoreboardNotifyRival_Params, params, trusted=False)
        self.assertEqual(notifications.pending(B_XUID), [])
        self.call(notify, A_XUID, x.ScoreboardNotifyRival_Params, params)
        self.assertEqual(len(notifications.pending(B_XUID)), 1)


class RivalsTest(Base):
    """GetRivalAndPersonalBest always answers a rival row (FH1 #591): with the rival's XUID 0 the
    game ends X RIVALS in "There was an error communicating with the Turn 10 server" and the
    player can neither race nor set a time. Unranked rows (#517) stay their player's own."""

    def answer(self, xuid, board=199, rival=0, with_best=True, **source):
        fn = self.svc[("Scoreboard", "GetRivalAndPersonalBest" if with_best else "GetRival")]
        reply = self.call(fn, xuid, x.ScoreboardGetRival_Params,
                          {"gameScoreboardId": board, "isServerTimeTrial": False, "rivalXuid": rival},
                          **source)
        self.assertEqual(status(reply), (0, 0))
        r = x.decode(x.ScoreboardGetRivalAndPersonalBest_Results if with_best
                     else x.ScoreboardGetRival_Results, reply, 12)[0]
        data = r["rival"]["scoreData"]
        got = {"xuid": data["user"]["qwXuid"], "tag": data["user"]["wzGamerTag"],
               "score": round(data["score"], 3), "position": data["position"],
               "ghost": data["ghostFileId"], "bounty": r["rival"]["bounty"]}
        if with_best:
            got["best"] = round(r["personalBest"]["score"], 3)
        return got

    def best(self, xuid, board=199, **source):
        reply = self.call(self.svc[("Scoreboard", "GetBestScores")], xuid,
                          x.ScoreboardGetBestScores_Params,
                          {"scoreType": scores.EVENT_TIME, "gameScoreboardId": board,
                           "isServerTimeTrial": False, "carId": AVENTADOR, "carClassId": 7,
                           "carDriveTypeId": 1}, **source)
        self.clock.race()   # the race that follows its start
        return round(x.decode(x.ScoreboardGetBestScores_Results, reply, 12)[0]["bestScore"]["score"], 3)

    def lap(self, xuid, lap, **source):
        """A Rivals lap submitted with its ghost; returns the ghost's file id."""
        reply = self.submit(xuid, lap, **source)
        self.assertTrue(reply["uploadFiles"])
        self.upload(xuid, reply["scoreId"], tg.ghost(tg.replay_init(lap=lap), tg.replay_data(lap=lap)),
                    **source)
        return scores.STORE.boards[BOARD]["%016X" % xuid]["ghostFileId"]

    def test_an_empty_board_answers_the_caller_with_no_target(self):
        # Seen in the game: YOUR RIVAL <the player>, TARGET ---, BOUNTY 0 Cr; the race starts and
        # FINISH says the rival is beaten. No time of its own is the game's FLT_MAX ("---"), so
        # the lap is submitted and becomes the board's first row.
        empty = {"xuid": A_XUID, "tag": "DriverA", "score": 0.0, "position": 0, "ghost": -1,
                 "bounty": 0, "best": NO_TIME}
        self.assertEqual(self.answer(A_XUID), empty)
        self.assertEqual(self.answer(A_XUID, with_best=False),
                         {k: v for k, v in empty.items() if k != "best"})
        self.assertEqual(self.best(A_XUID), NO_TIME)
        ghost = self.lap(A_XUID, 138.108)
        self.assertEqual(self.rows(), [("DriverA", 138.108, ghost)])
        # a second player gets it as its rival, ghost and bounty with it
        self.assertEqual(self.answer(B_XUID), {"xuid": A_XUID, "tag": "DriverA", "score": 138.108,
                                               "position": 1, "ghost": ghost,
                                               "bounty": scores.RIVAL_BOUNTY, "best": NO_TIME})

    def test_only_the_callers_own_time_is_its_rival(self):
        # Turn 10's service gave the player at the top its own time: Rivals against its own
        # ghost, "You beat <player>!", the bounty, a better time.
        ghost = self.lap(A_XUID, 138.108)
        self.assertEqual(self.answer(A_XUID), {"xuid": A_XUID, "tag": "DriverA", "score": 138.108,
                                               "position": 1, "ghost": ghost,
                                               "bounty": scores.RIVAL_BOUNTY, "best": 138.108})
        self.assertEqual(self.best(A_XUID), 138.108)
        better_ghost = self.lap(A_XUID, 121.558)
        self.assertEqual(self.answer(A_XUID)["ghost"], better_ghost)
        # another player on the board: the rivals of a normal board
        b_ghost = self.lap(B_XUID, 137.948)
        self.assertEqual(self.answer(B_XUID)["tag"], "DriverA")   # the time just better
        self.assertEqual(self.answer(A_XUID)["tag"], "DriverB")   # the best: the next one down
        self.assertEqual(self.answer(A_XUID)["ghost"], b_ghost)

    def test_the_rival_asked_for(self):
        self.lap(A_XUID, 100.0)
        self.lap(B_XUID, 110.0)
        self.submit(C_XUID, 120.0)
        self.assertEqual(self.answer(C_XUID)["tag"], "DriverB")
        self.assertEqual(self.answer(C_XUID, rival=A_XUID)["tag"], "DriverA")
        # itself, or a player not on the board (any more): the rival it would get otherwise
        self.assertEqual(self.answer(C_XUID, rival=C_XUID)["tag"], "DriverB")
        self.assertEqual(self.answer(C_XUID, rival=0x0009000000000D04)["tag"], "DriverB")

    def test_an_unranked_time_is_a_rival_for_its_player_alone(self):
        # #517 keeps an unverified caller's times off the boards; it is still told its own best,
        # and now races it. Nobody else is ever given that row.
        self.submit(A_XUID, 90.0, trusted=False)
        self.assertEqual(self.unranked(), {"%016X" % A_XUID: (90.0, "unverified")})
        self.assertEqual(self.answer(A_XUID, trusted=False),
                         {"xuid": A_XUID, "tag": "DriverA", "score": 90.0, "position": 0,
                          "ghost": -1, "bounty": scores.RIVAL_BOUNTY, "best": 90.0})
        for xuid, trusted in ((B_XUID, True), (C_XUID, False)):
            got = self.answer(xuid, trusted=trusted)
            self.assertEqual((got["xuid"], got["score"], got["best"]), (xuid, 0.0, NO_TIME))
            self.assertEqual(self.answer(xuid, rival=A_XUID, trusted=trusted)["xuid"], xuid)
        self.assertEqual(self.rows(), [])
        # signed in now: its ranked best only (none), so its next time ranks
        self.assertEqual(self.answer(A_XUID)["score"], 0.0)
        self.assertEqual(self.best(A_XUID), NO_TIME)
        # a signed-in player's ranked time is the rival of the unverified one
        self.submit(B_XUID, 95.0)
        self.assertEqual(self.answer(A_XUID, trusted=False)["tag"], "DriverB")
        self.assertEqual(self.answer(B_XUID)["tag"], "DriverB")   # its own: A's is not shown


class SignedScoresTest(ta.Base):
    """The whole path with signed requests (FH1 #515): a game that signs, a game before it (the
    friends key itself, until --raw-key-cutoff) and a caller nobody signed in with."""

    def setUp(self):
        super().setUp()
        scores.configure(self.tmp.name, "main-1")
        self.addCleanup(scores.configure, self.tmp.name, "")

    def score(self, xuid, score, build="0.2.1/tu4", method="SubmitScoreEx", **kw):
        def named(headers, body):
            if build is not None:
                headers["x-forza-build"] = build
            return body
        return self.call("Scoreboard", method, x.ScoreboardSubmitScoreEx_Params,
                         {"type": 3, "data": {"base": score_data(199, score), "numCarsPassed": 0}},
                         xuid=xuid, tamper=named, **kw)

    def best(self, xuid, **kw):
        reply = self.call("Scoreboard", "GetBestScores", x.ScoreboardGetBestScores_Params,
                          {"scoreType": 3, "gameScoreboardId": 199, "isServerTimeTrial": False,
                           "carId": AVENTADOR, "carClassId": 7, "carDriveTypeId": 1},
                          xuid=xuid, **kw)
        return round(x.decode(x.ScoreboardGetBestScores_Results, reply, 12)[0]["bestScore"]["score"], 3)

    def test_a_signing_game_is_verified_and_ranks(self):
        self.sign_in()
        self.assertEqual(self.status(self.score(ta.XUID, 107.228)), (0, 0))
        row = scores.STORE.boards[BOARD]["%016X" % ta.XUID]
        self.assertEqual((row["server"], row["build"], row["verified"]), ("main-1", "0.2.1/tu4", True))
        self.assertEqual(self.best(ta.XUID), 107.228)
        self.assertEqual(scores.STORE.unranked, {})

    def test_a_game_before_signing_ranks_until_the_cutoff(self):
        self.hello(ta.KEY)
        self.assertEqual(self.status(self.score(ta.XUID, 110.0, seed=None, raw=ta.KEY.hex(),
                                                build=None)), (0, 0))
        row = scores.STORE.boards[BOARD]["%016X" % ta.XUID]
        self.assertEqual((row["build"], row["verified"]), ("", True))   # no X-Forza-Build yet
        auth.RAW_KEY_CUTOFF = int(time.time()) - 1
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(self.status(self.score(ta.XUID, 100.0, seed=None, raw=ta.KEY.hex())),
                             (2, 5))
        self.assertEqual(scores.STORE.boards[BOARD]["%016X" % ta.XUID]["score"], 110.0)

    def test_a_caller_nobody_signed_in_with_stays_unranked(self):
        self.sign_in()
        self.score(ta.XUID, 107.228)
        # an XUID with no key on record: the call is accepted, its result kept off the boards
        self.assertEqual(self.status(self.score(ta.OTHER, 90.0, seed=None)), (0, 0))
        row = scores.STORE.unranked[BOARD]["%016X" % ta.OTHER]
        self.assertEqual((row["unranked"], row["verified"], row["build"]), ("unverified", False, "0.2.1/tu4"))
        self.assertEqual(list(scores.STORE.boards[BOARD]), ["%016X" % ta.XUID])
        # it is answered its own best only; the signed-in player sees the board as before
        self.assertEqual(self.best(ta.OTHER, seed=None), 90.0)
        self.assertEqual(self.best(ta.THIRD, seed=None), scores.FLT_MAX)
        self.assertEqual(self.best(ta.XUID), 107.228)
        # someone else's signature, or none for an XUID with a key: refused, nothing kept
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(self.status(self.score(ta.XUID, 50.0, seed=None)), (2, 5))
            self.assertEqual(self.status(self.score(ta.XUID, 50.0, seed=ta.SEED_B)), (2, 5))
        self.assertEqual(round(scores.STORE.boards[BOARD]["%016X" % ta.XUID]["score"], 3), 107.228)
        self.assertEqual(list(scores.STORE.unranked[BOARD]), ["%016X" % ta.OTHER])


class CallerTest(unittest.TestCase):
    """Through lsp.service_call: the caller's key decides, the build is the header's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_dir = lsp.DATA_DIR
        lsp.DATA_DIR = self.tmp.name
        self.addCleanup(setattr, lsp, "DATA_DIR", self.old_dir)
        scores.configure(self.tmp.name, "main-1")
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = type("T", (), {"sendto": lambda *_: None})()
        body = struct.pack(">HQQIH", srv.VERSION, 1, A_XUID, 0, 0)
        body += bytes([4]) + b"#TU4" + bytes([7]) + b"DriverA" + bytes([ALL]) + bytes([16]) + KEY
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, ("127.0.0.1", 20001))

    def submit(self, xuid, score, headers):
        h = {"x-classname": "Forza.WebServices.Scoreboard, Forza.WebServices",
             "x-method": "SubmitScoreEx", "x-userid": "0x%x" % xuid, **headers}
        body = x.encode(x.ScoreboardSubmitScoreEx_Params,
                        {"type": 3, "data": {"base": score_data(199, score), "numCarsPassed": 0}})
        return lsp.service_call(h, bytes(body), self.server)

    def test_sources(self):
        signed = {"x-friends-key": KEY.hex(), "x-forza-build": " 0.2.0/tu4 "}
        self.assertEqual(status(self.submit(A_XUID, 107.228, signed)), (0, 0))
        row = scores.STORE.boards[BOARD]["%016X" % A_XUID]
        self.assertEqual((row["build"], row["verified"], row["server"]), ("0.2.0/tu4", True, "main-1"))
        # forza-live #7: a keyless write for an XUID with a key on record is refused
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(status(self.submit(A_XUID, 50.0, {})), (2, 5))
        self.assertEqual(round(scores.STORE.boards[BOARD]["%016X" % A_XUID]["score"], 3), 107.228)
        self.assertEqual(scores.STORE.unranked, {})
        # an XUID nobody signed in with: kept unranked, with what its game named
        self.assertEqual(status(self.submit(B_XUID, 60.0, {"x-forza-build": "evil\x7f<b>"})), (0, 0))
        self.assertEqual(list(scores.STORE.boards[BOARD]), ["%016X" % A_XUID])
        row = scores.STORE.unranked[BOARD]["%016X" % B_XUID]
        self.assertEqual((row["unranked"], row["build"], row["verified"]), ("unverified", "evil__b_", False))
        self.submit(C_XUID, 61.0, {"x-forza-build": "0.2.0" + "x" * 100})
        self.assertEqual(len(scores.STORE.unranked[BOARD]["%016X" % C_XUID]["build"]), lsp.BUILD_MAX)
        # outside a call nothing is left of the last one's source
        self.assertEqual(scores.source(), (True, ""))


if __name__ == "__main__":
    unittest.main()
