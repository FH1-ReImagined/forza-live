"""Daily and weekly challenges (objectives.py, forza-live #1): the rotation from the seed, each
player's progress from races, speed traps and zones, 1000 Club medals and big shots, only from
signed-in players, and /challenges and /challenges/progress.

Run: python3 -m unittest discover -s tests -t .
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from forza_live.competition import objectives as ob
from forza_live.competition import scores
from forza_live.network import xtsw as x
from forza_live.network import xtsw_all as A
from forza_live.title_services import lsp

A_XUID, B_XUID = 0x0009000000000A01, 0x0009000000000B02
WED = 1793793600            # 2026-11-04 12:00 UTC, a Wednesday (ISO week 45)
B_CLASS, S_CLASS = 4, 6
RT29, RT6, GLADSTONE = 230, 228, 201   # speed traps: records 348.3, 348.9 and 423.1 km/h


def kmh(v):
    return v / 3.6


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        scores.configure(self.dir.name, "main-1")
        ob.configure(self.dir.name)
        self.addCleanup(ob.STORE.__init__)       # no later test writes into this folder
        self.addCleanup(setattr, ob, "SEED", ob.SEED)
        self.addCleanup(setattr, ob, "ENABLED", True)
        self.addCleanup(setattr, ob, "RACE_GAP", ob.RACE_GAP)
        ob.RACE_GAP = 0
        self.addCleanup(setattr, lsp, "DATA_DIR", lsp.DATA_DIR)
        lsp.DATA_DIR = self.dir.name

    def use(self, daily=(), weekly=()):
        """The objectives of every new period, in place of the rotation's."""
        sets = {"daily": [dict(o) for o in daily], "weekly": [dict(o) for o in weekly]}
        patcher = mock.patch.object(ob, "choose", lambda kind, pid, seed=None: [dict(o) for o in sets[kind]])
        patcher.start()
        self.addCleanup(patcher.stop)

    def as_caller(self, trusted=True, build="0.2.0/tu4"):
        token = scores.set_source(trusted, build)
        self.addCleanup(scores.reset_source, token)

    def progress(self, xuid=A_XUID, now=WED):
        doc = ob.document(xuid, now)
        return {s["kind"]: [(o["progress"], bool(o["done"])) for o in s["objectives"]]
                for s in doc["sets"]}

    def race(self, place=1, places=8, cls=B_CLASS, finished=True, humans=1, xuid=A_XUID,
             trusted=True, scored=True):
        """User.WriteRaceStatsEx through lsp, as the game sends it at a race's end."""
        stats = {"finished": finished, "userFinishPosition": place, "maxPlaces": places,
                 "numHumans": humans, "numAI": places - humans, "eventId": 199}
        params = {"id": 1, "raceStats": stats}
        ptype = x.UserWriteRaceStats_Head
        if scored:
            ptype = A.ForzaUserWriteRaceStatsEx_Params
            params["scoreData"] = {"base": {"carId": 1398, "carClassId": cls}, "numCarsPassed": 0}
        caller = lsp._caller.set(lsp.TRUSTED if trusted else lsp.UNVERIFIED)
        source = scores.set_source(trusted, "0.2.0/tu4")
        try:
            with mock.patch("time.time", return_value=WED):
                return lsp.user_write_race_stats(xuid, bytes(x.encode(ptype, params)))
        finally:
            scores.reset_source(source)
            lsp._caller.reset(caller)

    def trap(self, board, speed, cls=S_CLASS, kind=scores.SPEED_TRAP, xuid=A_XUID, trusted=True):
        """A speed trap or zone pass: Scoreboard.SubmitScore as the game sends it."""
        data = {"gameScoreboardId": board, "isServerTimeTrial": False, "score": kmh(speed),
                "carId": 1398, "carClassId": cls, "carPerformanceIndex": 0.8}
        svc = scores.services(lambda xuid: "Driver")
        source = scores.set_source(trusted, "0.2.0/tu4")
        try:
            with mock.patch("time.time", return_value=WED):
                svc[("Scoreboard", "SubmitScore")](
                    xuid, bytes(x.encode(x.ScoreboardSubmitScore_Params, {"type": kind, "data": data})))
        finally:
            scores.reset_source(source)


WIN_B = ob.objective("race", "Win 2 races in a B class car", 2, place=1, cls=B_CLASS)
PODIUM = ob.objective("race", "Finish 3 races on the podium", 3, place=3)
ONLINE = ob.objective("race", "Finish 2 races against other players", 2, humans=2)
TRAPS = ob.objective("trap", "Hit 250 km/h at 2 different speed traps", 2, kmh=250, distinct=True)
ZONE = ob.objective("zone", "Average 190 km/h through 1 speed zone", 1, kmh=190, distinct=True)
MEDALS = ob.objective("medal", "Earn 2 1000 Club medals", 2, distinct=True)
PHOTO = ob.objective("photo", "Upload a Big Shot", 1)


class RotationTest(Base):
    def test_every_player_and_restart_gets_the_same_set(self):
        first = ob.choose("daily", "daily-2026-11-04")
        self.assertEqual(first, ob.choose("daily", "daily-2026-11-04"))
        self.assertEqual(len(first), ob.PER_SET)
        self.assertNotEqual([ob.choose("daily", "daily-2026-11-%02d" % d) for d in range(1, 8)],
                            [ob.choose("daily", "daily-2026-11-%02d" % d, seed="other") for d in range(1, 8)])

    def test_a_set_takes_each_group_once(self):
        for d in range(60):
            for kind in ob.KINDS:
                chosen = ob.choose(kind, "%s-%d" % (kind, d))
                groups = [next(g for g, (_, variants) in ob.POOLS[kind].items() if o in variants)
                          for o in chosen]
                self.assertEqual(len(set(groups)), ob.PER_SET, chosen)

    def test_the_choice_does_not_depend_on_python(self):
        # sha256 of "forza-live/daily-2026-11-04/group/0", not random: a fixed answer
        self.assertEqual(ob.pick("forza-live", "daily-2026-11-04", "group", 0),
                         int.from_bytes(__import__("hashlib").sha256(
                             b"forza-live/daily-2026-11-04/group/0").digest()[:8], "big"))

    def test_periods_are_utc_days_and_iso_weeks_from_monday(self):
        self.assertEqual(ob.period("daily", WED), ("daily-2026-11-04", 1793750400, 1793836800))
        week = ob.period("weekly", WED)
        self.assertEqual(week[0], "weekly-2026-W45")
        self.assertEqual(__import__("time").gmtime(week[1])[:7], (2026, 11, 2, 0, 0, 0, 0))  # Monday
        self.assertEqual(week[2] - week[1], 7 * 86400)
        self.assertEqual(ob.period("weekly", week[2])[0], "weekly-2026-W46")
        self.assertEqual(ob.period("daily", 1793836800)[0], "daily-2026-11-05")

    def test_a_period_keeps_its_set_when_the_pool_changes(self):
        before = ob.document(None, WED)
        with mock.patch.dict(ob.POOLS, {"daily": {"race": (1, [PODIUM])},
                                        "weekly": {"race": (1, [PODIUM])}}):
            ob.configure(self.dir.name)  # a restart with a new pool, the same day
            self.assertEqual(ob.document(None, WED)["sets"], before["sets"])
            later = ob.document(None, WED + 86400)["sets"][0]  # tomorrow: the new pool
        self.assertEqual([o["title"] for o in later["objectives"]], [PODIUM["title"]])

    def test_only_this_and_the_last_period_of_each_kind_are_kept(self):
        for day in range(5):
            ob.document(None, WED + day * 86400)
        kept = sorted(ob.STORE.data["periods"])
        self.assertEqual(kept, ["daily-2026-11-07", "daily-2026-11-08", "weekly-2026-W45"])


class ProgressTest(Base):
    def test_races_count_wins_in_the_class_and_podiums(self):
        self.use(daily=[WIN_B, PODIUM])
        self.race(place=1)
        self.race(place=2)
        self.race(place=1, cls=S_CLASS)
        self.assertEqual(self.progress()["daily"], [(1, False), (3, True)])
        self.race(place=1)
        self.assertEqual(self.progress()["daily"], [(2, True), (3, True)])
        self.race(place=1)   # done: no more
        self.assertEqual(self.progress()["daily"], [(2, True), (3, True)])

    def test_a_win_needs_a_finish_and_someone_to_beat(self):
        self.use(daily=[WIN_B])
        self.race(place=1, finished=False)   # retired
        self.race(place=1, places=1)         # alone on the grid
        self.race(place=0)
        self.assertEqual(self.progress()["daily"], [(0, False)])

    def test_last_of_the_grid_is_no_podium(self):
        self.use(daily=[PODIUM])
        self.race(place=2, places=2)   # a Rivals race lost
        self.race(place=3, places=3)
        self.assertEqual(self.progress()["daily"], [(0, False)])
        self.race(place=3, places=4)
        self.assertEqual(self.progress()["daily"], [(1, False)])

    def test_a_race_without_score_data_has_no_class(self):
        self.use(daily=[WIN_B, PODIUM])
        self.race(place=1, scored=False)
        self.assertEqual(self.progress()["daily"], [(0, False), (1, False)])

    def test_races_against_other_players(self):
        self.use(weekly=[ONLINE])
        self.race(place=5, humans=1)
        self.race(place=5, humans=3)
        self.assertEqual(self.progress()["weekly"], [(1, False)])

    def test_a_race_reported_again_at_once_counts_once(self):
        ob.RACE_GAP = 20.0
        self.use(daily=[PODIUM])
        self.race(place=1)
        self.race(place=1)
        self.assertEqual(self.progress()["daily"], [(1, False)])

    def test_speed_traps_count_distinct_traps_at_the_speed(self):
        self.use(daily=[TRAPS])
        self.trap(RT29, 260)
        self.trap(RT29, 270)                  # the same trap again
        self.trap(RT6, 240)                   # too slow
        self.assertEqual(self.progress()["daily"], [(1, False)])
        self.trap(RT6, 255)
        self.assertEqual(self.progress()["daily"], [(2, True)])

    def test_a_speed_above_the_traps_record_does_not_count(self):
        self.use(daily=[TRAPS])
        self.trap(RT29, 360)          # Rt. 29's record is 348.3 km/h: logged only on the board
        self.trap(GLADSTONE, 480)     # above any car
        self.assertEqual(self.progress()["daily"], [(0, False)])
        self.assertIn("%016X" % A_XUID, scores.STORE.boards["1:230:0"])  # the board as before

    def test_zones_are_not_traps(self):
        self.use(daily=[TRAPS, ZONE])
        self.trap(105, 200, kind=scores.SPEED_ZONE)
        self.assertEqual(self.progress()["daily"], [(0, False), (1, True)])

    def test_a_speed_in_a_class(self):
        self.use(daily=[ob.objective("trap", "x", 1, kmh=200, cls=B_CLASS, distinct=True)])
        self.trap(RT29, 220, cls=S_CLASS)
        self.assertEqual(self.progress()["daily"], [(0, False)])
        self.trap(RT29, 220, cls=B_CLASS)
        self.assertEqual(self.progress()["daily"], [(1, True)])

    def test_1000_club_medals_count_each_challenge_once(self):
        self.use(weekly=[MEDALS])
        svc = scores.services(lambda xuid: "Driver")
        self.as_caller()

        def complete(car, thumb):
            body = x.encode(A.ForzaChallengeChallengeCompleted_Params,
                            {"carId": car, "challengeThumbprint": thumb})
            with mock.patch("time.time", return_value=WED):
                svc[("Challenge", "ChallengeCompleted")](A_XUID, bytes(body))
        complete(1398, 0xAE6828D3)
        complete(1398, 0xAE6828D3)   # done again: the same thumbprint
        self.assertEqual(self.progress()["weekly"], [(1, False)])
        complete(1029, 0xAE6828D3)
        self.assertEqual(self.progress()["weekly"], [(2, True)])

    def test_big_shots(self):
        self.use(daily=[PHOTO])
        self.as_caller()
        ob.photo(A_XUID, WED)
        self.assertEqual(self.progress()["daily"], [(1, True)])

    def test_daily_and_weekly_move_together(self):
        self.use(daily=[PODIUM], weekly=[PODIUM])
        self.race(place=2)
        self.assertEqual(self.progress(), {"daily": [(1, False)], "weekly": [(1, False)]})

    def test_progress_and_builds_are_kept_across_restarts(self):
        self.use(daily=[PODIUM])
        self.race(place=2)
        ob.configure(self.dir.name)
        self.assertEqual(self.progress()["daily"], [(1, False)])
        rec = ob.STORE.data["periods"]["daily-2026-11-04"]["players"]["%016X" % A_XUID]
        self.assertEqual(rec["builds"], ["0.2.0/tu4"])
        with open(os.path.join(self.dir.name, ob.FILE)) as f:
            self.assertNotIn("Driver", f.read())   # no gamertags kept

    def test_a_new_day_starts_from_nothing(self):
        self.use(daily=[PODIUM])
        self.race(place=2)
        self.assertEqual(self.progress(now=WED + 86400)["daily"], [(0, False)])


class TrustTest(Base):
    def test_unverified_callers_do_not_count(self):
        self.use(daily=[PODIUM, TRAPS, PHOTO])
        self.race(place=1, trusted=False)
        self.trap(RT29, 260, trusted=False)
        self.as_caller(trusted=False)
        ob.photo(A_XUID, WED)
        self.assertEqual(self.progress()["daily"], [(0, False), (0, False), (0, False)])
        self.assertEqual(ob.STORE.data["periods"]["daily-2026-11-04"]["players"], {})

    def test_off(self):
        self.use(daily=[PODIUM])
        ob.ENABLED = False
        self.race(place=1)
        self.assertIsNone(ob.handle(None, "GET", "/challenges", {}, b"", lambda *a: A_XUID))
        ob.ENABLED = True
        self.assertEqual(self.progress()["daily"], [(0, False)])

    def test_players_are_apart(self):
        self.use(daily=[PODIUM])
        self.race(place=1, xuid=A_XUID)
        self.assertEqual(self.progress(B_XUID)["daily"], [(0, False)])


class HttpTest(Base):
    def get(self, path, xuid=None, method="GET"):
        with mock.patch("time.time", return_value=WED):
            status, ctype, body, *extra = ob.handle(None, method, path, {}, b"", lambda *a: xuid)
        return status, json.loads(body)

    def test_the_public_sets(self):
        self.use(daily=[PODIUM, PHOTO], weekly=[TRAPS])
        self.race(place=1)
        self.race(place=1, xuid=B_XUID)
        self.race(place=1, xuid=B_XUID)
        self.race(place=1, xuid=B_XUID)
        status, doc = self.get("/challenges")
        self.assertEqual(status, 200)
        self.assertEqual((doc["version"], doc["server"], doc["now"]), (1, "main-1", WED))
        daily, weekly = doc["sets"]
        self.assertEqual((daily["kind"], daily["id"], daily["starts"], daily["ends"]),
                         ("daily", "daily-2026-11-04", 1793750400, 1793836800))
        self.assertEqual(daily["objectives"][0], {"id": "daily-2026-11-04.0", "type": "race",
                                                  "title": PODIUM["title"], "target": 3,
                                                  "completed": 1})
        self.assertEqual(weekly["id"], "weekly-2026-W45")
        text = json.dumps(doc)
        self.assertNotIn("%016X" % B_XUID, text)
        self.assertNotIn("progress", text)

    def test_a_players_own_progress(self):
        self.use(daily=[PODIUM, PHOTO], weekly=[PODIUM])
        for _ in range(3):
            self.race(place=1)
        status, doc = self.get("/challenges/progress", A_XUID)
        self.assertEqual(status, 200)
        daily = doc["sets"][0]["objectives"]
        self.assertEqual((daily[0]["progress"], daily[0]["done"]), (3, WED))
        self.assertEqual((daily[1]["progress"], daily[1]["done"]), (0, None))
        self.assertEqual(doc["player"], {"completed": {"daily": 1, "weekly": 1}})

    def test_progress_needs_a_signed_in_player(self):
        self.assertEqual(self.get("/challenges/progress", None), (403, {"error": "unknown player"}))

    def test_reads_only(self):
        self.assertEqual(self.get("/challenges", method="POST")[0], 405)

    def test_other_paths_are_not_ours(self):
        self.assertIsNone(ob.handle(None, "GET", "/challengesx", {}, b"", lambda *a: None))

    def test_served_on_the_http_port(self):
        with mock.patch("time.time", return_value=WED):
            status, ctype, body, extra = lsp.handle(None, "GET", "/challenges", {}, b"", ("127.0.0.1", 1))
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertIn("Cache-Control: public, max-age=60", extra)
        self.assertEqual(len(json.loads(body)["sets"]), 2)


if __name__ == "__main__":
    unittest.main()
