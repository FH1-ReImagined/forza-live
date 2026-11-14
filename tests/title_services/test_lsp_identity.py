"""Who a title service call is from (lsp.verify_caller): the game names its player
in X-UserId, which anyone can write, and our game adds the friends key the XUID was bound to at
its HELLO (X-Friends-Key). Forged calls are refused when they change anything; calls without a
key (older games) are accepted but unverified. Drives the server's handlers directly."""

import json
import os
import struct
import tempfile
import unittest

from forza_live.core import auth
from forza_live.title_services import lsp
from forza_live import server as srv
from forza_live.network import xtsw as x

ALL = srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT
KEY = bytes(15) + bytes([7])   # DriverA's friends key (as in its live_identity.txt)
XUID = 0x0009000000000001


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        lsp.DATA_DIR = self.tmp.name
        lsp._unverified_logged.clear()
        auth._logged["told"].clear()
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = type("T", (), {"sendto": lambda *_: None})()
        body = struct.pack(">HQQIH", srv.VERSION, 1, XUID, 0, 0)
        body += bytes([4]) + b"#TU4" + bytes([7]) + b"DriverA" + bytes([ALL]) + bytes([16]) + KEY
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, ("127.0.0.1", 20001))
        self.assertTrue(self.server.by_addr[("127.0.0.1", 20001)].friends_ok)

    def tearDown(self):
        lsp.DATA_DIR = self.old_dir
        self.tmp.cleanup()

    def call(self, cls, method, params_type, params, key=None, xuid=XUID):
        headers = {"x-classname": "Forza.WebServices.%s, Forza.WebServices" % cls,
                   "x-method": method, "x-userid": "0x%x" % xuid}
        if key is not None:
            headers["x-friends-key"] = key
        return lsp.service_call(headers, bytes(x.encode(params_type, params)), self.server)

    def race_stats(self, place, key=None, xuid=XUID):
        stats = {"finished": True, "userFinishPosition": place, "maxPlaces": 8, "hopperId": 3}
        return self.call("User", "WriteRaceStatsEx", x.UserWriteRaceStats_Head,
                         {"id": 1, "raceStats": stats}, key, xuid)

    def last_race(self, xuid=XUID):
        path = os.path.join(self.tmp.name, "players", "%016X.json" % xuid)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f).get("lastRace")

    @staticmethod
    def status(reply):
        return struct.unpack_from(">II", reply)

    def test_the_player_s_own_key_is_trusted(self):
        self.assertEqual(self.status(self.race_stats(3, KEY.hex().upper())), (0, 0))
        race = self.last_race()
        self.assertEqual((race["userFinishPosition"], race["verified"]), (3, True))

    def test_another_key_cannot_write_for_the_player(self):
        self.race_stats(3, KEY.hex())
        forged = self.race_stats(1, ("AB" * 16))
        self.assertEqual(self.status(forged), (2, 5))
        self.assertEqual(self.last_race()["userFinishPosition"], 3)
        self.assertEqual(self.status(self.race_stats(1, "not hex")), (2, 5))
        self.assertEqual(self.status(self.race_stats(1, "00" * 15)), (2, 5))
        # scores and the profile are writes too; the next round's options change a world
        submit = self.call("Scoreboard", "SubmitScore", x.ScoreboardSubmitScore_Params,
                           {}, "CD" * 16)
        self.assertEqual(self.status(submit), (2, 5))
        options = self.call("Hopper", "GetGameOptions", x.HopperGetGameOptions_Params,
                            {"hopperId": 3, "roundNum": 1, "cchOptionXmlMax": 0x8000}, "CD" * 16)
        self.assertEqual(self.status(options), (2, 5))

    def test_reads_with_another_key_are_answered(self):
        reply = self.call("Hopper", "GetList", x.HopperGetList_Params,
                          {"maxCategoryResults": 2, "maxHopperResults": 32}, "EF" * 16)
        self.assertEqual(self.status(reply), (0, 0))

    def test_calls_without_a_key_cannot_write_for_a_player_with_one(self):
        # DriverA signed in with a key, and its game sends it on every call: a write without
        # it is someone else using the XUID (public on /status), so it is refused.
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.assertEqual(self.status(self.race_stats(2)), (2, 5))
        self.assertIsNone(self.last_race())
        self.assertTrue(any("unsigned" in m for m in logs.output))
        # reads are still answered
        reply = self.call("Hopper", "GetList", x.HopperGetList_Params,
                          {"maxCategoryResults": 2, "maxHopperResults": 32})
        self.assertEqual(self.status(reply), (0, 0))

    def test_calls_without_a_key_are_accepted_unverified_for_xuids_without_one(self):
        # an XUID that never signed in with a key (an older game) cannot be checked
        other = 0x0009000000000099
        with self.assertLogs("forza-live", "INFO") as logs:
            self.assertEqual(self.status(self.race_stats(4, None, other)), (0, 0))
            self.race_stats(4, None, other)
        self.assertEqual(self.last_race(other)["verified"], False)
        self.assertEqual(sum("accepted as unverified" in m for m in logs.output), 1)
        self.assertEqual(self.status(self.race_stats(4, "AB" * 16, other)), (0, 0))

    # --- the sign-in writes the record of the XUID the call was checked for ---

    def logon(self, named, key=None, xuid=XUID, tag="Renamed", userid=True):
        login = {"qwXuid": named, "wzGamerTag": tag, "currentCareerLevel": 9, "currentBadgeId": 0,
                 "currentPlayerTitleId": 0}
        headers = {"x-classname": "Forza.WebServices.User, Forza.WebServices", "x-method": "Logon2"}
        if userid:
            headers["x-userid"] = "0x%x" % xuid
        if key is not None:
            headers["x-friends-key"] = key
        return lsp.service_call(headers, bytes(x.encode(x.UserLogon2_Params, {"loginData": login})), self.server)

    def record(self, xuid=XUID):
        path = os.path.join(self.tmp.name, "players", "%016X.json" % xuid)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def test_a_sign_in_cannot_name_another_player_in_its_body(self):
        """Logon2 was checked for its X-UserId and wrote the record of the XUID in its body: one
        unsigned call renamed any player."""
        self.assertEqual(self.status(self.logon(XUID, KEY.hex(), tag="DriverA")), (0, 0))
        mine = self.record()
        self.assertEqual((mine["gamertag"], mine["logons"]), ("DriverA", 1))
        stranger, other_key = 0x0009000000000066, bytes(15) + bytes([9])
        body = struct.pack(">HQQIH", srv.VERSION, 2, stranger, 0, 0)
        body += bytes([4]) + b"#TU4" + bytes([7]) + b"Mallory" + bytes([ALL]) + bytes([16]) + other_key
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, ("127.0.0.1", 20002))
        with self.assertLogs("forza-live", "WARNING") as logs:
            for kw in (dict(xuid=0x0009000000000077),         # an XUID nobody has, unsigned
                       dict(userid=False),                    # no X-UserId at all
                       dict(xuid=stranger, key=other_key.hex())):  # the sender's own XUID and key
                self.assertEqual(self.status(self.logon(XUID, **kw)), (2, 5), kw)
        self.assertEqual(self.record(), mine)
        self.assertIsNone(self.record(0x0009000000000077))
        self.assertTrue(all("names 0009000000000001 in its body" in line for line in logs.output))
        # the player's own sign-in is what it was
        self.assertEqual(self.status(self.logon(XUID, KEY.hex(), tag="DriverA2")), (0, 0))
        self.assertEqual((self.record()["gamertag"], self.record()["logons"]), ("DriverA2", 2))
        self.assertEqual(self.status(self.logon(stranger, other_key.hex(), xuid=stranger)), (0, 0))

    def test_callers_without_a_key_make_few_new_records(self):
        """Calls that name made-up XUIDs made a record each, up to the cap of player records:
        from then on no new player's record was kept."""
        with self.assertLogs("forza-live", "WARNING") as logs:
            for i in range(50):
                made_up = 0x0009000000010000 + i
                reply = self.logon(made_up, xuid=made_up) if i % 2 else self.race_stats(1, None, made_up)
                self.assertEqual(self.status(reply), (0, 0))  # answered all the same
        folder = os.path.join(self.tmp.name, "players")
        self.assertEqual(len(os.listdir(folder)), lsp.NEW_UNVERIFIED[1])
        self.assertEqual(len([line for line in logs.output if "records are not kept" in line]), 1)
        # a record that exists is kept up as before, key or not
        self.assertEqual(self.status(self.logon(0x0009000000010000, xuid=0x0009000000010000, tag="Again")), (0, 0))
        self.assertEqual(self.record(0x0009000000010000)["gamertag"], "Again")
        # and a player who proves the XUID gets a record whenever it comes
        self.assertIsNone(self.record())
        self.assertEqual(self.status(self.logon(XUID, KEY.hex(), tag="DriverA")), (0, 0))
        self.assertEqual(self.record()["gamertag"], "DriverA")

    def test_caller_trusted_is_per_call(self):
        seen = []
        real = lsp.SERVICES[("User", "WriteRaceStatsEx")]
        lsp.SERVICES[("User", "WriteRaceStatsEx")] = lambda xuid, body: (seen.append(lsp.caller_trusted()), real(xuid, body))[1]
        try:
            self.race_stats(1, KEY.hex())
            self.race_stats(1, None, 0x0009000000000099)  # an XUID with no key on record
        finally:
            lsp.SERVICES[("User", "WriteRaceStatsEx")] = real
        self.assertEqual(seen, [True, False])
        self.assertFalse(lsp.caller_trusted())


if __name__ == "__main__":
    unittest.main()
