"""Converted worlds (core/rooms.py): a game in Forza Horizon 2's or Fast & Furious's map is in a
room that ends in "+fh2" or "+anthem", and the server keeps nothing of Colorado's from it: no
scores (events, speed traps, speed zones, Rivals), ghosts, rival messages, 1000 Club medals, race
stats, online profile, daily and weekly challenge progress, firstScore, co-op campaign results
or Xbox LIVE stats. Each call is answered as an accepted one. The same calls from a player in
Colorado still work.

Two signed games on one server: DriverA in Fast & Furious's world ("+anthem#TU4"), DriverB in
Colorado ("#TU4"). Every world call is checked against the whole data folder and the stores in
memory before and after it.

Run: python3 -m unittest discover -s tests -t .
"""

import hashlib
import json
import os
import struct
import time
import unittest
from types import SimpleNamespace

from forza_live import server as srv
from forza_live.competition import objectives as ob
from forza_live.competition import score_token
from forza_live.competition import scores
from forza_live.competition import stats
from forza_live.content import market
from forza_live.core import auth
from forza_live.core import rooms
from forza_live.network import xtsw as x
from forza_live.network import xtsw_all as A
from forza_live.social import notifications
from forza_live.title_services import lsp
from forza_live.world import coop_ledger as ledger
from forza_live.world import world
from tests.competition import test_ghost as tg
from tests.core import test_auth as ta
from tests.support import RaceClock
from tests.world.test_campaign import CoopWorldCase

WORLD_ROOM, COLORADO_ROOM = "+anthem#TU4", "#TU4"
A_XUID, B_XUID = ta.XUID, ta.OTHER   # DriverA (ta.SEED), DriverB (ta.SEED_B)
A_ADDR, B_ADDR = ("127.0.0.1", 20001), ("127.0.0.1", 20002)
EVENT, TRAP, ZONE = 199, 230, 101
AVENTADOR = 1398
OBJECTIVES = [ob.objective("race", "Finish a race", 1), ob.objective("trap", "A speed trap", 1),
              ob.objective("zone", "A speed zone", 1), ob.objective("medal", "A medal", 1)]


def score_data(board, score):
    return {"gameScoreboardId": board, "isServerTimeTrial": False, "score": score,
            "carId": AVENTADOR, "carClassId": 7, "carPerformanceIndex": 0.864, "usedSTM": False,
            "usedTCS": False, "usedABS": False, "usedAutoBrake": False, "usedAutoShifting": False,
            "usedClutch": False}


def status(reply):
    return struct.unpack_from(">II", reply)


class WorldsCase(ta.Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        for mod in (scores, stats, notifications, market):
            self.addCleanup(setattr, mod, "DATA_DIR", mod.DATA_DIR)
        self.clock = RaceClock(self)
        scores.configure(d, "main-1", scored=lsp.first_score)
        self.addCleanup(scores.configure, d, "")
        lsp._scored.clear()
        self.addCleanup(lsp._scored.clear)
        self.addCleanup(setattr, scores, "RACE_TRACKS", scores.RACE_TRACKS)
        scores.RACE_TRACKS = {EVENT: {tg.CLEAR_SPRINGS}}
        stats.configure(d)
        notifications.configure(d)
        market.configure(d)
        for name in ("SEED", "ENABLED", "GIFTS", "RACE_GAP", "choose"):
            self.addCleanup(setattr, ob, name, getattr(ob, name))
        self.addCleanup(ob.STORE.__init__)
        ob.configure(d, enabled=True)
        ob.RACE_GAP = 0
        ob.choose = lambda kind, pid, seed=None: [dict(o) for o in OBJECTIVES]
        self.addCleanup(setattr, ledger, "LEDGER", ledger.LEDGER)
        ledger.LEDGER = ledger.Ledger(os.path.join(d, ledger.FILE))
        self.addCleanup(setattr, stats, "on_skill_write", stats.on_skill_write)
        stats.on_skill_write = ledger.LEDGER.host_score
        self.addCleanup(score_token._tokens.clear)
        score_token._tokens.clear()
        rooms._told.clear()
        for xuid, tag in ((A_XUID, "DriverA"), (B_XUID, "DriverB")):  # Logon2 made their records
            lsp.save_player(xuid, {"gamertag": tag, "logons": 1})
        self.a, _ = self.sign_in(seed=ta.SEED, addr=A_ADDR, xuid=A_XUID, token=1, tag=b"DriverA",
                                 room=WORLD_ROOM)
        self.b, _ = self.sign_in(seed=ta.SEED_B, addr=B_ADDR, xuid=B_XUID, token=2, tag=b"DriverB",
                                 room=COLORADO_ROOM)
        self.assertTrue(self.a.friends_ok and self.b.friends_ok)

    @staticmethod
    def hello_body(xuid=A_XUID, token=1, tag=b"DriverA", room=COLORADO_ROOM):
        room = room.encode()
        body = struct.pack(">HQQIH", srv.VERSION, token, xuid, 0, 0)
        return body + bytes([len(room)]) + room + bytes([len(tag)]) + tag + bytes([ta.ALL])

    # the calls, from A (the world) or B (Colorado)

    def as_(self, who):
        return {"seed": ta.SEED, "xuid": A_XUID} if who == "a" else {"seed": ta.SEED_B, "xuid": B_XUID}

    def submit(self, who, kind, board, score):
        if kind == scores.EVENT_TIME:
            reply = self.call("Scoreboard", "SubmitScoreEx", x.ScoreboardSubmitScoreEx_Params,
                              {"type": kind, "data": {"base": score_data(board, score),
                                                      "numCarsPassed": 0}}, **self.as_(who))
        else:
            reply = self.call("Scoreboard", "SubmitScore", x.ScoreboardSubmitScore_Params,
                              {"type": kind, "data": score_data(board, score)}, **self.as_(who))
        self.assertEqual(status(reply), (0, 0))
        return x.decode(x.ScoreboardSubmitScore_Results, reply, 12)[0]

    def upload(self, who, score_id, data):
        return self.call("Scoreboard", "UploadFile", x.ScoreboardUploadFile_Params,
                         {"scoreId": score_id, "data": data}, **self.as_(who))

    def medal(self, who, car=AVENTADOR, thumb=0x1234ABCD):
        return self.call("Challenge", "ChallengeCompleted", A.ForzaChallengeChallengeCompleted_Params,
                         {"carId": car, "challengeThumbprint": thumb}, **self.as_(who))

    def race(self, who, place=1, hopper=1):
        stats_ = {"finished": True, "userFinishPosition": place, "maxPlaces": 8, "hopperId": hopper,
                  "userLevel": 12, "userXP": 34000, "currentCredits": 99000, "onlineWinnings": 500}
        return self.call("User", "WriteRaceStatsEx", x.UserWriteRaceStats_Head,
                         {"id": 1, "raceStats": stats_}, **self.as_(who))

    def profile(self, who, online_xp=5000):
        return self.call("OnlineProfile", "UpdateAll", x.OnlineProfileUpdateAll_Params,
                         {"playerRaceHistory": [{"level": 12, "onlineXP": online_xp,
                                                 "numOnlineRaces": 3}],
                          "playerHistory": [{"credits": 99000}]}, **self.as_(who))

    def notify(self, who, rival, board=EVENT):
        return self.call("Scoreboard", "NotifyRival", x.ScoreboardNotifyRival_Params,
                         {"gameScoreboardId": board, "isServerTimeTrial": False,
                          "rivalXuid": rival, "type": scores.EVENT_TIME}, **self.as_(who))

    def best_scores(self, who, board=EVENT):
        reply = self.call("Scoreboard", "GetBestScores", x.ScoreboardGetBestScores_Params,
                          {"scoreType": scores.EVENT_TIME, "gameScoreboardId": board,
                           "isServerTimeTrial": False, "carId": AVENTADOR, "carClassId": 7},
                          **self.as_(who))
        return x.decode(x.ScoreboardGetBestScores_Results, reply, 12)[0]["bestScore"]["score"]

    # what the server keeps

    def state(self):
        """Every file of the data folder (its hash) and the stores in memory."""
        files = {}
        for root, _, names in os.walk(self.tmp.name):
            for name in names:
                path = os.path.join(root, name)
                with open(path, "rb") as f:
                    files[os.path.relpath(path, self.tmp.name)] = hashlib.sha256(f.read()).hexdigest()
        memory = json.dumps([scores.STORE.boards, scores.STORE.unranked, scores.STORE.club,
                             sorted(scores.STORE.ghosts), ob.STORE.data, stats.STORE.players,
                             sorted(map(repr, stats.STORE.pending)), ledger.LEDGER.rounds,
                             notifications.STORE.data, sorted(map(repr, score_token._tokens))],
                            sort_keys=True, default=repr)
        return files, memory

    def assert_nothing_kept(self, call):
        before = self.state()
        reply = call()
        self.assertEqual(self.state(), before)
        return reply

    def record(self, xuid):
        return lsp.load_player(xuid)

    def restart(self):
        """The server restarts: a new one restores the players from the state file."""
        self.server.state_path = os.path.join(self.tmp.name, srv.STATE_FILE)
        self.assertTrue(self.server.save_state())
        players = self.store.players
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = SimpleNamespace(sendto=lambda d, a: self.sent.append((a, d)))
        self.server.friends.store.players = players
        self.store = self.server.friends.store
        self.server.state_path = os.path.join(self.tmp.name, srv.STATE_FILE)
        return self.server.load_state()

    def progress(self, xuid):
        doc = ob.document(xuid)
        return [o["progress"] for s in doc["sets"] if s["kind"] == "daily" for o in s["objectives"]]


class LookupTest(WorldsCase):
    def test_the_caller_s_room_comes_from_its_session(self):
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 1, WORLD_ROOM))
        self.assertIsNone(self.server.player_world(B_XUID))
        self.assertIsNone(self.server.player_world(0x0009000000000099))   # not online
        self.assertIsNone(self.server.player_world(0))

    def test_a_session_that_left_counts_for_a_while_then_a_new_one_decides(self):
        self.server.drop(self.a, "left")
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 1, WORLD_ROOM))
        self.server.left_worlds[A_XUID] = self.server.left_worlds[A_XUID][:3] + (
            time.monotonic() - rooms.LEFT_KEEP - 1,)
        self.assertIsNone(self.server.player_world(A_XUID))
        # back in the world, then the game restarted in Colorado (a new token)
        self.sign_in(seed=ta.SEED, addr=A_ADDR, xuid=A_XUID, token=3, tag=b"DriverA", room=WORLD_ROOM)
        self.assertEqual(self.server.player_world(A_XUID)[0], "anthem")
        a2, _ = self.sign_in(seed=ta.SEED, addr=A_ADDR, xuid=A_XUID, token=4, tag=b"DriverA",
                             room=COLORADO_ROOM)
        self.assertEqual(a2.room, COLORADO_ROOM)
        self.assertIsNone(self.server.player_world(A_XUID))   # the world session left with it
        self.server.drop(a2, "left")
        self.assertIsNone(self.server.player_world(A_XUID))   # its last session was Colorado's

    def test_a_second_game_in_a_world_takes_the_player_there(self):
        b2, _ = self.sign_in(seed=ta.SEED_B, addr=("127.0.0.1", 20003), xuid=B_XUID, token=5,
                             tag=b"DriverB", room="club+fh2")
        self.assertEqual(self.server.player_world(B_XUID), ("fh2", 5, "club+fh2"))
        self.server.drop(b2, "left")
        self.assertIsNone(self.server.player_world(B_XUID))   # its Colorado game is live

    def test_an_unproven_session_in_a_world_does_not_count(self):
        """Anyone can say HELLO with another player's XUID: in a world's room it must not take that
        player's Colorado results off the boards."""
        self.hello(b"", ("127.0.0.1", 20009), xuid=B_XUID, token=9, tag=b"DriverB", room=WORLD_ROOM)
        fake = self.server.by_addr[("127.0.0.1", 20009)]
        self.assertFalse(fake.friends_ok)
        self.assertIsNone(self.server.player_world(B_XUID))
        self.submit("b", scores.SPEED_TRAP, TRAP, 90.0)
        self.assertIn("%016X" % B_XUID, scores.STORE.boards["1:%d:0" % TRAP])
        # an XUID with no key on record: nothing can prove it, its session counts
        self.hello(b"", ("127.0.0.1", 20010), xuid=0x0009000000000077, token=10, tag=b"Old",
                   room="+fh2")
        self.assertEqual(self.server.player_world(0x0009000000000077)[0], "fh2")

    def test_a_game_s_first_hello_is_its_world_s_before_it_proves_the_xuid(self):
        """A signing game's first HELLO cannot prove the XUID (it is signed before the WELCOME
        tells the challenge); the next one, 5 s later, does. With none of the player's sessions
        proven, its unproven session in a world decides: the calls in between are the world's."""
        self.server.drop(self.a, "left")
        self.server.left_worlds.clear()   # e.g. the player's first game of the day
        peer, _ = self.hello(b"", ("127.0.0.1", 20031), xuid=A_XUID, token=31, tag=b"DriverA",
                             room=WORLD_ROOM)
        self.assertFalse(peer.friends_ok)
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 31, WORLD_ROOM))
        self.assert_nothing_kept(lambda: self.submit("a", scores.SPEED_TRAP, TRAP, 70.0))
        # an unproven session in Colorado is not one: with no other the call is Colorado's
        self.server.drop(peer, "left")
        self.server.left_worlds.clear()
        peer, _ = self.hello(b"", ("127.0.0.1", 20032), xuid=A_XUID, token=32, tag=b"DriverA",
                             room=COLORADO_ROOM)
        self.assertFalse(peer.friends_ok)
        self.assertIsNone(self.server.player_world(A_XUID))

    def test_after_a_restart_a_world_session_s_first_hello_keeps_the_world(self):
        """A restored session is proven until its first HELLO after the restart, which is signed
        with the old server's challenge and proves nothing until the next one: a score in
        between must still be the world's."""
        self.assertEqual(self.restart(), 2)
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 1, WORLD_ROOM))
        stale = auth.hello_block(ta.SEED, ta.MAIN, A_XUID, bytes(16),
                                 self.hello_body(room=WORLD_ROOM))
        peer, _ = self.hello(stale, A_ADDR, xuid=A_XUID, token=1, tag=b"DriverA", room=WORLD_ROOM)
        self.assertFalse(peer.friends_ok)
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 1, WORLD_ROOM))
        self.assert_nothing_kept(lambda: self.submit("a", scores.SPEED_TRAP, TRAP, 70.0))
        self.submit("b", scores.SPEED_TRAP, TRAP, 60.0)   # Colorado's restored game: kept
        self.assertEqual(list(scores.STORE.boards["1:%d:0" % TRAP]), ["%016X" % B_XUID])

    def test_a_world_session_that_left_before_a_restart_still_counts_after_it(self):
        self.server.drop(self.a, "left")
        self.assertEqual(self.restart(), 1)   # B's session
        self.assertEqual(self.server.player_world(A_XUID), ("anthem", 1, WORLD_ROOM))
        self.assert_nothing_kept(lambda: self.submit("a", scores.SPEED_TRAP, TRAP, 70.0))
        self.assertIsNone(self.server.player_world(B_XUID))

    def test_a_state_file_s_unreadable_left_worlds_are_left_out(self):
        self.server.drop(self.a, "left")
        self.server.state_path = os.path.join(self.tmp.name, srv.STATE_FILE)
        state = self.server.snapshot()
        self.assertEqual([r[:4] for r in state["left_worlds"]], [[A_XUID, "anthem", 1, WORLD_ROOM]])
        state["left_worlds"] = [[A_XUID, "fh2", 1, WORLD_ROOM, time.time()], [B_XUID], "x",
                                [7, "anthem", 1, "club", time.time()],
                                [8, None, 1, "club", time.time()]]
        with open(self.server.state_path, "w") as f:
            json.dump(state, f)
        self.server = srv.ForzaLive("test", 64)
        self.server.state_path = os.path.join(self.tmp.name, srv.STATE_FILE)
        self.assertEqual(self.server.load_state(), 1)   # B's session is restored all the same
        self.assertEqual(self.server.left_worlds, {})


class ScoresTest(WorldsCase):
    def test_event_times_go_nowhere_and_the_game_is_not_asked_for_a_ghost(self):
        reply = self.assert_nothing_kept(lambda: self.submit("a", scores.EVENT_TIME, EVENT, 115.13))
        self.assertEqual((len(reply["scoreId"]), reply["uploadFiles"]), (16, False))
        reply = self.submit("b", scores.EVENT_TIME, EVENT, 115.13)
        self.assertTrue(reply["uploadFiles"])
        self.assertEqual(round(scores.STORE.boards["3:%d:0" % EVENT]["%016X" % B_XUID]["score"], 2),
                         115.13)
        self.assertNotIn("%016X" % A_XUID, scores.STORE.boards["3:%d:0" % EVENT])

    def test_speed_traps_and_zones_go_nowhere(self):
        for kind, board in ((scores.SPEED_TRAP, TRAP), (scores.SPEED_ZONE, ZONE)):
            self.assert_nothing_kept(lambda: self.submit("a", kind, board, 50.0))
            self.submit("b", kind, board, 50.0)
            self.assertEqual(list(scores.STORE.boards["%d:%d:0" % (kind, board)]), ["%016X" % B_XUID])
        self.assertNotIn("firstScore", self.record(A_XUID))
        self.assertIn("firstScore", self.record(B_XUID))
        self.assertEqual(self.progress(A_XUID), [0, 0, 0, 0])
        self.assertEqual(self.progress(B_XUID), [0, 1, 1, 0])
        self.assertEqual(scores.STORE.unranked, {})   # not kept unranked either

    def test_a_ghost_is_not_taken(self):
        reply = self.assert_nothing_kept(lambda: self.upload("a", os.urandom(16), b"ghost"))
        self.assertEqual(status(reply), (0, 0))
        ghost = tg.ghost(tg.replay_init(lap=115.13, name="DriverB"), tg.replay_data(lap=115.13))
        sid = self.submit("b", scores.EVENT_TIME, EVENT, 115.13)["scoreId"]
        # A's call with B's score: the world's game never had it, and it changes nothing
        self.assert_nothing_kept(lambda: self.upload("a", sid, ghost))
        self.assertEqual(status(self.upload("b", sid, ghost)), (0, 0))
        row = scores.STORE.boards["3:%d:0" % EVENT]["%016X" % B_XUID]
        self.assertTrue(os.path.isfile(scores.ghost_path(row["ghostFileId"])))

    def test_a_beaten_rival_is_not_told(self):
        # A's row from Colorado, B's too
        token = scores.set_source(True, "0.2.8/tu4")
        try:
            scores.submit(A_XUID, "DriverA", scores.EVENT_TIME, score_data(EVENT, 120.0))
        finally:
            scores.reset_source(token)
        self.submit("b", scores.EVENT_TIME, EVENT, 110.0)
        self.assertEqual(status(self.assert_nothing_kept(lambda: self.notify("a", B_XUID))), (0, 0))
        self.assertNotIn("%016X" % B_XUID, notifications.STORE.data["byRecipient"])
        self.notify("b", A_XUID)
        [n] = notifications.STORE.data["byRecipient"]["%016X" % A_XUID]
        self.assertEqual(n["type"], notifications.RIVALS_BEATEN)

    def test_reads_are_answered_and_a_world_race_s_start_is_no_score_token(self):
        self.submit("b", scores.EVENT_TIME, EVENT, 110.0)
        self.assertEqual(self.assert_nothing_kept(lambda: self.best_scores("a")), scores.FLT_MAX)
        self.assertAlmostEqual(self.best_scores("b"), 110.0, places=3)
        self.assertEqual([k[0] for k in score_token._tokens], [B_XUID])

    def test_the_1000_club_takes_no_medal(self):
        self.assertEqual(status(self.assert_nothing_kept(lambda: self.medal("a"))), (0, 0))
        self.assertEqual(status(self.medal("b")), (0, 0))
        self.assertEqual(list(scores.STORE.club), ["%016X" % B_XUID])
        self.assertEqual(self.progress(B_XUID), [0, 0, 0, 1])
        self.assertNotIn("firstScore", self.record(A_XUID))


class RaceAndProfileTest(WorldsCase):
    def test_race_stats_and_their_challenge_progress_are_not_kept(self):
        self.assertEqual(status(self.assert_nothing_kept(lambda: self.race("a"))), (0, 0))
        self.assertEqual(self.record(A_XUID), {"gamertag": "DriverA", "logons": 1})
        self.assertEqual(status(self.race("b")), (0, 0))
        b = self.record(B_XUID)
        self.assertEqual((b["races"], b["userXP"], b["lastRace"]["verified"]), (1, 34000, True))
        self.assertEqual(self.progress(B_XUID), [1, 0, 0, 0])

    def test_the_online_profile_is_not_kept(self):
        """Online XP feeds Race Central's ONLINE XP board (GetRows type 4)."""
        self.assertEqual(status(self.assert_nothing_kept(lambda: self.profile("a"))), (0, 0))
        self.profile("b")
        self.assertEqual(self.record(B_XUID)["profile"]["onlineXP"], 5000)
        token = scores.set_source(True, "")
        try:
            body = bytes(x.encode(x.ScoreboardGetRows_Params, {
                "type": scores.ONLINE_XP_TYPE, "view": 0, "gameScoreboardId": 1,
                "isServerTimeTrial": False, "isClubBoard": False, "startAt": 1, "maxResults": 50}))
            rows = x.decode(x.ScoreboardGetRows_Results, scores.get_rows(B_XUID, body), 12)[0]["rows"]
        finally:
            scores.reset_source(token)
        self.assertEqual([r["user"]["qwXuid"] for r in rows], [B_XUID])

    def test_signing_in_and_reads_stay(self):
        before = self.record(A_XUID)["logons"]
        logon = self.call("User", "Logon2", x.UserLogon2_Params, {"loginData": {
            "qwXuid": A_XUID, "wzGamerTag": "DriverA", "currentCareerLevel": 12}}, **self.as_("a"))
        self.assertEqual(status(logon), (0, 0))
        self.assertEqual(self.record(A_XUID)["logons"], before + 1)
        self.assertEqual(status(self.read(seed=ta.SEED, xuid=A_XUID)), (0, 0))   # Hopper.GetList


class ChallengesTest(WorldsCase):
    def test_no_step_from_a_world_reaches_the_challenges(self):
        """The sink every challenge step goes through (a published photo too) takes nothing from
        a world's call."""
        token = rooms.set_call(("anthem", 1, WORLD_ROOM), A_XUID)
        source = scores.set_source(True, "0.2.8/tu4")
        try:
            with self.assertLogs("forza-live", "INFO") as logs:
                self.assertFalse(ob.photo(A_XUID))
                self.assertFalse(ob.medal(A_XUID, AVENTADOR, 7))
        finally:
            scores.reset_source(source)
            rooms.reset_call(token)
        self.assertEqual(len([l for l in logs.output if "challenge progress" in l]), 1)
        self.assertEqual(self.progress(A_XUID), [0, 0, 0, 0])


class StatsTest(WorldsCase):
    def test_xbox_live_stats_writes_from_a_world_are_not_kept(self):
        def write(peer):
            body = struct.pack(">BQQB", 1, 0x80AB0000000000CD, peer.xuid, 1)
            body += struct.pack(">IB", 0xFFFF0000, 1)
            body += struct.pack(">IBi", stats.RELATIVE_SCORE, stats.TYPE_INT32, 7) + bytes(4)
            self.server.on_stats(peer, body)
        self.assert_nothing_kept(lambda: write(self.a))
        self.assertEqual(stats.STORE.pending, {})
        write(self.b)
        self.assertEqual(len(stats.STORE.pending), 1)


class LogTest(WorldsCase):
    def test_one_line_per_session_and_kind(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            for _ in range(3):
                self.submit("a", scores.SPEED_TRAP, TRAP, 80.0)
                self.race("a")
            self.submit("b", scores.SPEED_TRAP, TRAP, 80.0)
        told = [line for line in logs.output if "world: " in line]
        self.assertEqual(len(told), 2, told)
        self.assertIn("%016X in room '+anthem#TU4' (Fast & Furious's world): not kept, as "
                      "Colorado's: scores (first: Scoreboard.SubmitScore;" % A_XUID, told[0])
        self.assertIn("as Colorado's: race stats (first: User.WriteRaceStatsEx;", told[1])
        # a new session of the same player is told again
        self.sign_in(seed=ta.SEED, addr=A_ADDR, xuid=A_XUID, token=7, tag=b"DriverA", room=WORLD_ROOM)
        with self.assertLogs("forza-live", "INFO") as logs:
            self.submit("a", scores.SPEED_TRAP, TRAP, 80.0)
        self.assertEqual(len([line for line in logs.output if "world: " in line]), 1)


class CoopRoomCase(CoopWorldCase):
    """tests/world/test_campaign's Co-op Festival world (A hosts, B is in it), its players in
    ROOM, and the ledger in the data folder."""

    ROOM = b"#TU4"

    def setUp(self):
        super().setUp()
        ledger.LEDGER = ledger.Ledger(os.path.join(self.tmp.name, ledger.FILE))
        stats.on_skill_write = ledger.LEDGER.host_score
        stats.STORE.pending.clear()
        stats.STORE.rated.clear()
        rooms._told.clear()
        self.sid = self.coop_world()

    def tearDown(self):
        stats.on_skill_write = None
        stats.STORE.pending.clear()
        stats.STORE.rated.clear()
        ledger.LEDGER = ledger.Ledger()
        super().tearDown()

    def join(self, n, features=srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT
             | lsp.COOP_FEATURE):
        body = struct.pack(">HQQIH", srv.VERSION, n, 0x0009000000000000 + n, 0, 0)
        tag = b"Driver%d" % n
        body += bytes([len(self.ROOM)]) + self.ROOM + bytes([len(tag)]) + tag + bytes([features])
        body += bytes([16]) + bytes(15) + bytes([n])
        addr = ("127.0.0.1", 20000 + n)
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, addr)
        return self.server.by_addr.get(addr)


class CoopInAWorldTest(CoopRoomCase):
    """Every player of the Co-op Festival world in Fast & Furious's world: its campaign rounds are
    raced, but the ledger keeps neither the round nor a result."""

    ROOM = b"+anthem#TU4"

    def test_a_campaign_round_in_a_world_is_raced_but_never_the_career_s(self):
        self.assertEqual(self.server.player_world(self.a.xuid)[0], "anthem")
        self.say(self.a, "/event FR05")
        self.assertTrue(any("no result is kept for your career" in n for n in self.notices(self.a)))
        self.server.world_tick()
        event = self.game_options(self.a, lsp.COOP_HOPPER)       # the event is the next round
        self.assertIsNotNone(event.get("TrackId"))
        self.game_options(self.a, lsp.COOP_HOPPER)               # its own prefetch: it started
        self.assertIn(self.sid, world.WORLDS.racing)
        body = struct.pack(">BQQB", 1, int(self.sid.hex(), 16), self.b.xuid, 1)
        body += struct.pack(">IB", 0xFFFF0000, 1)
        body += struct.pack(">IBi", stats.RELATIVE_SCORE, stats.TYPE_INT32, 5) + bytes(4)
        self.server.on_stats(self.a, body)
        self.assertEqual(status(self.race_report(self.a, 3, self.sid)), (0, 0))
        self.assertEqual(status(self.race_report(self.b, 5, self.sid)), (0, 0))
        self.game_options(self.a, lsp.COOP_HOPPER)               # back in the free roam
        self.assertEqual(ledger.LEDGER.rounds, {})
        self.assertEqual(ledger.LEDGER.pending(self.a.xuid), [])
        self.assertEqual(ledger.LEDGER.pending(self.b.xuid), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ledger.FILE)))


class CoopInColoradoTest(CoopRoomCase):
    """The same round in Colorado: the ledger has it and each player's result."""

    def test_a_campaign_round_in_colorado_is_the_career_s(self):
        self.assertIsNone(self.server.player_world(self.a.xuid))
        self.say(self.a, "/event FR05")
        self.assertFalse(any("career" in n for n in self.notices(self.a)))
        self.server.world_tick()
        self.game_options(self.a, lsp.COOP_HOPPER)
        self.game_options(self.a, lsp.COOP_HOPPER)
        self.race_report(self.a, 3, self.sid)
        self.assertEqual([(e["code"], e["place"]) for e in ledger.LEDGER.pending(self.a.xuid)],
                         [("FR05", 3)])


if __name__ == "__main__":
    unittest.main()
