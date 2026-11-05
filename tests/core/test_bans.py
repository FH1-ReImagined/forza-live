"""A ban from the whole server (core/bans.py): the banned XUID cannot sign in (UDP HELLO, Logon2),
is dropped when it is online, writes nothing on any path (title services, datagrams, the signed
HTTP routes, the stores themselves), and its rows and progress are listed to nobody until the ban
is lifted, which shows them again.

Run: python3 -m unittest discover -s tests -t .
"""

import argparse
import asyncio
import json
import os
import struct
import tempfile
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

from forza_live import server as srv
from forza_live.competition import leaderboards
from forza_live.competition import objectives
from forza_live.competition import scores
from forza_live.competition import stats
from forza_live.content import community
from forza_live.content import designs
from forza_live.content import market
from forza_live.content import owners
from forza_live.content import shader_lists
from forza_live.core import auth
from forza_live.core import bans
from forza_live.core import ed25519
from forza_live.network import xtsw as x
from forza_live.network import xtsw_all as A
from forza_live.panel import audit
from forza_live.panel import routes
from forza_live.social import friends
from forza_live.title_services import lsp
from forza_live.world import coop_ledger
from tests.support import FakeTransport, hello

NAME = "ban.test"
TITLE = "/Services.Horizon/o.xtsw"
BAD, GOOD, OTHER = 0x0009000000000BAD, 0x0009000000000600, 0x0009000000000700
NAMES = {BAD: "Cheater", GOOD: "Honest", OTHER: "Other"}
SEEDS = {xuid: bytes([n + 1]) * 32 for n, xuid in enumerate(NAMES)}
ADDR = {xuid: ("203.0.113.%d" % (n + 1), 3074) for n, xuid in enumerate(NAMES)}
TOKEN = "ban-test-token"
LAN = ("192.168.1.20", 50000)


def entry(xuid, score, car=1398):
    return {"gameScoreboardId": 199, "isServerTimeTrial": False, "score": score, "carId": car,
            "carClassId": 5, "carPerformanceIndex": 600.0, "usedSTM": 0, "usedTCS": 0,
            "usedABS": 0, "usedAutoBrake": 0, "usedAutoShifting": 0, "usedClutch": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir.name, "--server-name", NAME,
                                         "--daily-challenges", "--daily-challenges-seed", "s"]))
        auth._nonces.clear()
        self.server = srv.ForzaLive("test", 64)
        self.transport = FakeTransport()
        self.server.transport = self.transport
        self.addCleanup(self.server.events.db.close)
        for xuid, name in NAMES.items():
            lsp.save_player(xuid, {"gamertag": name, "lastLogon": 1790000000})
            self.server.friends.store.seen(xuid, name)
            self.server.friends.store.get(xuid)["pub"] = ed25519.public_key(SEEDS[xuid]).hex()
        coop_ledger.LEDGER = coop_ledger.Ledger(os.path.join(self.dir.name, "coop_ledger.json"))
        stats.STORE.__init__()
        stats.STORE.pending.clear()
        market.MARKET.__init__()

    def tearDown(self):
        coop_ledger.LEDGER = coop_ledger.Ledger()
        stats.STORE.__init__()
        objectives.ENABLED = False
        objectives.STORE.__init__()
        market.MARKET.__init__()
        scores.STORE.__init__()
        lsp.DATA_DIR = self.old_dir
        bans.configure(self.old_dir)
        self.dir.cleanup()

    def ban(self, on=True, xuid=BAD):
        self.assertTrue(bans.set_ban(xuid, on))

    # --- the game's calls ---

    def raw(self, xuid, cls, method, ptype=None, params=None, signed=True, userid=None):
        full = cls if cls.startswith("Xls.") else "Forza.WebServices." + cls
        headers = {"x-classname": full + ", Forza.WebServices", "x-method": method,
                   "x-userid": "0x%x" % (userid or xuid)}
        body = bytes(x.encode(ptype, params or {})) if ptype else b""
        if signed:
            headers[auth.HEADER] = auth.auth_header(SEEDS[userid or xuid], NAME, userid or xuid,
                                                    "POST", TITLE, body, headers)
        return lsp.service_call(headers, body, self.server, "POST", TITLE)

    def refused(self, reply):
        """A failure that says the caller is banned (code 5, as a forged call's)."""
        status, code, n = struct.unpack_from(">III", reply)
        return (status, code, reply[12:12 + n]) == (2, 5, lsp.BANNED.encode())

    def call(self, xuid, cls, method, ptype=None, params=None, rtype=None, signed=True,
             userid=None):
        """A title service call; the decoded results, {} for a success without results, None for
        a failure."""
        reply = self.raw(xuid, cls, method, ptype, params, signed, userid)
        if reply[:4] != bytes(4):
            return None
        return x.decode(rtype, reply, 12)[0] if rtype else {}

    def logon(self, xuid, userid=None):
        login = {"qwXuid": xuid, "wzGamerTag": NAMES[xuid], "currentCareerLevel": 3}
        return self.call(xuid, "User", "Logon2", x.UserLogon2_Params, {"loginData": login},
                         x.UserLogon2_Results, userid=userid)

    def http(self, xuid, method, path, body=b""):
        """A signed request to the server's other HTTP routes: (status, JSON)."""
        headers = {auth.HEADER: auth.auth_header(SEEDS[xuid], NAME, xuid, method, path, body)}
        status, _, data, *_ = lsp.handle(self.server, method, path, headers, body,
                                         ("198.51.100.9", 1))
        return status, json.loads(data)

    def submit(self, xuid, score):
        """A score as the server's own (trusted, outside a call): a ranked row."""
        scores.submit(xuid, NAMES[xuid], scores.EVENT_TIME, entry(xuid, score))

    def rows(self, viewer):
        r = self.call(viewer, "Scoreboard", "GetRows", x.ScoreboardGetRows_Params,
                      {"type": scores.EVENT_TIME, "view": 0, "gameScoreboardId": 199,
                       "isServerTimeTrial": False, "isClubBoard": False, "startAt": 1,
                       "maxResults": 50}, x.ScoreboardGetRows_Results)
        return [row["user"]["wzGamerTag"] for row in r["rows"]]

    def public_board(self):
        leaderboards.forget_answers()
        status, _, data, *_ = leaderboards.handle("GET", "/leaderboards/event-199")
        return [r["gamertag"] for r in json.loads(data)["rows"]] if status == 200 else status

    # --- the UDP side ---

    def say_hello(self, xuid, features=1):
        data = bytearray(hello(xuid, xuid, NAMES[xuid]))
        data[-1] = features
        self.server.datagram_received(bytes(data), ADDR[xuid])

    def errors(self, xuid):
        out = []
        for addr, d in self.transport.sent:
            if addr == ADDR[xuid] and d[4] == srv.ERROR:
                code, n = struct.unpack_from(">BH", d, 5)
                out.append((code, d[8:8 + n].decode()))
        return out

    def online(self, xuid):
        return [p for p in self.server.peers.values() if p.xuid == xuid]


class SignInTest(Base):
    def test_hello_refused_with_error_4(self):
        self.ban()
        self.say_hello(BAD)
        self.assertEqual(self.online(BAD), [])
        self.assertEqual(self.errors(BAD), [(4, "banned from this server")])
        self.say_hello(GOOD)
        self.assertEqual(len(self.online(GOOD)), 1)

    def test_logon2_refused_as_a_forged_one_is(self):
        self.assertIsNotNone(self.logon(BAD))
        self.ban()
        self.assertIsNone(self.logon(BAD))
        self.assertIsNone(self.logon(BAD, userid=GOOD))  # another X-UserId, its own XUID inside
        login = {"loginData": {"qwXuid": BAD, "wzGamerTag": "Cheater"}}
        self.assertTrue(self.refused(self.raw(BAD, "User", "Logon2", x.UserLogon2_Params, login,
                                              signed=False)))
        self.assertTrue(self.refused(self.raw(BAD, "User", "Logon2", x.UserLogon2_Params, login,
                                              userid=GOOD)))
        self.assertIsNotNone(self.logon(GOOD))
        self.ban(False)
        self.assertIsNotNone(self.logon(BAD))

    def test_online_player_is_dropped_with_its_lobby_and_chat(self):
        self.say_hello(BAD, features=srv.FEATURE_CHAT)
        self.say_hello(GOOD)
        peer = self.online(BAD)[0]
        peer.match = (7, srv.SESSION_INFO_SIZE * b"\1" + bytes(36), time.monotonic())
        peer.chat_sessions = {1}
        self.assertIn("Cheater", json.dumps(self.server.status()))
        self.ban()
        self.assertEqual(self.online(BAD), [])
        self.assertNotIn("Cheater", json.dumps(self.server.status()))
        self.assertNotIn(ADDR[BAD], self.server.by_addr)
        self.assertNotIn("Cheater", [p.name for p in self.server.chat.chatters()])
        self.assertEqual(len(self.online(GOOD)), 1)
        # its game tries again: refused
        self.say_hello(BAD)
        self.assertEqual(self.online(BAD), [])
        self.assertIn((4, "banned from this server"), self.errors(BAD))

    def test_hand_edited_list_drops_at_the_next_tick(self):
        self.say_hello(BAD)
        with open(bans.path(), "w") as f:
            json.dump(["%016X" % BAD], f)
        os.utime(bans.path(), (time.time() + 5, time.time() + 5))
        self.server.expire()
        self.assertEqual(self.online(BAD), [])

    def test_datagrams_of_a_banned_peer_are_dropped(self):
        self.say_hello(BAD)
        peer = self.online(BAD)[0]
        bans._state["xuids"] = frozenset({BAD})  # as the tick would have read it
        self.addCleanup(bans._state.update, mtime=None)
        body = struct.pack(">BHB", 1, 1, 4) + b"Hone"  # a friends search
        self.server.datagram_received(srv.MAGIC + bytes([srv.FRIEND]) + body, ADDR[BAD])
        self.assertNotIn(peer.id, self.server.peers)

    def test_restart_does_not_restore_a_banned_player(self):
        self.server.state_path = os.path.join(self.dir.name, srv.STATE_FILE)
        self.say_hello(BAD)
        self.say_hello(GOOD)
        self.server.save_state()
        self.ban()
        again = srv.ForzaLive("test", 64)
        self.addCleanup(again.events.db.close)
        again.state_path = self.server.state_path
        self.assertEqual(again.load_state(), 1)
        self.assertEqual([p.xuid for p in again.peers.values()], [GOOD])


class WriteTest(Base):
    def setUp(self):
        super().setUp()
        self.ban()

    def test_every_title_service_call_fails(self):
        calls = [
            ("Scoreboard", "SubmitScore", x.ScoreboardSubmitScore_Params,
             {"type": scores.EVENT_TIME, "data": entry(BAD, 60.0)}),
            ("Scoreboard", "UploadFile", x.ScoreboardUploadFile_Params, {"data": b"ghost"}),
            ("Scoreboard", "GetBestScores", x.ScoreboardGetBestScores_Params,
             {"scoreType": 3, "gameScoreboardId": 199}),  # a score token's start
            ("Challenge", "ChallengeCompleted", A.ForzaChallengeChallengeCompleted_Params,
             {"carId": 1398, "challengeThumbprint": 1}),
            ("User", "WriteRaceStats", None, None),
            ("OnlineProfile", "UpdateAll", None, None),
            ("Auction", "Create", A.ForzaAuctionCreate_Params, {"durationMs": 3600000}),
            ("Gifting", "SendCarGift", A.ForzaGiftingSendCarGift_Params, {"recipientXuid": GOOD}),
            ("StorefrontLivery", "Create", None, None),
            ("StorefrontPhoto", "Create", None, None),
        ]
        calls += [("Xls.WebServices.Clan", "CreateClan", None, None),
                  ("Xls.WebServices.Notifications", "RetrieveForUser", None, None),
                  ("Hopper", "GetListEx", None, None), ("Nothing", "AtAll", None, None)]
        for cls, method, ptype, params in calls:
            self.assertTrue(self.refused(self.raw(BAD, cls, method, ptype, params)), method)
            self.assertTrue(self.refused(self.raw(BAD, cls, method, ptype, params, signed=False)),
                            method)
        self.assertFalse(self.refused(self.raw(GOOD, "Hopper", "GetListEx")))
        scores.STORE.load()
        self.assertEqual((scores.STORE.boards, scores.STORE.unranked, scores.STORE.club),
                         ({}, {}, {}))
        self.assertEqual(market.MARKET.auctions, {})
        self.assertEqual(market.MARKET.gifts, {})

    def test_the_stores_refuse_it_too(self):
        self.submit(BAD, 50.0)  # not through a call: scores itself refuses it
        self.assertEqual(scores.STORE.boards, {})
        write = struct.pack(">QQB", 1, BAD, 1) + struct.pack(">IB", 0x1234, 1) + \
            struct.pack(">IB", 1, 1) + struct.pack(">q", 5)
        stats.write(BAD, "Cheater", write)  # its own
        stats.write(GOOD, "Honest", write)  # a host writing for it
        self.assertEqual(stats.STORE.players, {})
        kind = objectives.STORE.current("daily", time.time())[1]["objectives"][0]["type"]
        self.assertFalse(objectives.step(BAD, kind, lambda obj: True))
        self.assertTrue(objectives.step(GOOD, kind, lambda obj: True))
        self.assertNotIn("%016X" % BAD, json.dumps(objectives.STORE.data))

    def test_coop_ledger(self):
        lg = coop_ledger.LEDGER
        event = SimpleNamespace(id=12, code="FR05", drivers=7)
        rid = lg.start_round(b"world-01", event, GOOD, "Honest", {GOOD: "Honest", BAD: "Cheater"})
        self.assertEqual(list(lg.rounds[rid]["members"]), ["%016X" % GOOD])
        self.assertIsNone(lg.report(BAD, b"world-01", 1, True))
        self.assertEqual(lg.report(GOOD, b"world-01", 2, True), rid)
        lg.host_score(GOOD, int(b"world-01".hex(), 16), BAD, 7)
        self.assertNotIn("%016X" % BAD, lg.rounds[rid]["host"])

    def test_signed_http_routes_take_it_for_nobody(self):
        self.assertIsNone(shader_lists.authenticate(
            self.server.friends.store,
            {auth.HEADER: auth.auth_header(SEEDS[BAD], NAME, BAD, "POST", "/shader-lists/upload",
                                           b"{}")}, "POST", "/shader-lists/upload", b"{}"))
        self.assertEqual(self.http(BAD, "POST", "/shader-lists/upload", b"{}")[0], 403)
        self.assertEqual(self.http(BAD, "GET", "/coop/results")[0], 403)
        self.assertEqual(self.http(BAD, "POST", "/coop/results/applied", b'{"ids": []}')[0], 403)
        self.assertEqual(self.http(BAD, "GET", "/challenges/progress")[0], 403)
        self.assertEqual(self.http(GOOD, "GET", "/coop/results")[0], 200)
        self.assertEqual(self.http(GOOD, "GET", "/challenges/progress")[0], 200)

    def test_website_delete_refused(self):
        with mock.patch.object(owners, "OWNER_TOKEN", "owner-token"):
            status, _, data, *_ = owners.handle(
                "POST", "/owner/photos/AAAAAAAAAAAA/delete", {"x-owner-token": "owner-token"},
                json.dumps({"owner": "%016X" % BAD}).encode(), False)
        self.assertEqual((status, json.loads(data)), (403, {"error": "banned from this server"}))

    def test_bid_and_banned_sellers_auction(self):
        self.ban(False)
        aid = self.call(BAD, "Auction", "Create", A.ForzaAuctionCreate_Params,
                        {"durationMs": 3600000, "openingPrice": 100},
                        A.ForzaAuctionCreate_Results)["auctionId"]
        self.ban()
        bid = {"auctionId": aid, "amount": 500}
        self.assertIsNone(self.call(GOOD, "Auction", "PlaceBid", A.ForzaAuctionPlaceBid_Params,
                                    bid))
        self.assertEqual(market.MARKET.auctions[aid]["bids"], [])


class ListingTest(Base):
    """What a banned player left is kept, listed to nobody, and back when the ban is lifted."""

    def setUp(self):
        super().setUp()
        self.submit(BAD, 50.0)
        self.submit(GOOD, 60.0)

    def test_leaderboards(self):
        self.assertEqual(self.rows(OTHER), ["Cheater", "Honest"])
        self.assertEqual(self.public_board(), ["Cheater", "Honest"])
        self.ban()
        self.assertEqual(self.rows(OTHER), ["Honest"])
        self.assertEqual(self.public_board(), ["Honest"])
        self.assertIn("%016X" % BAD, scores.STORE.boards["3:199:0"])  # kept
        rival = self.call(OTHER, "Scoreboard", "GetRival", x.ScoreboardGetRival_Params,
                          {"gameScoreboardId": 199, "rivalXuid": BAD}, x.ScoreboardGetRival_Results)
        self.assertEqual(rival["rival"]["scoreData"]["user"]["wzGamerTag"], "Honest")
        status, _, data, *_ = leaderboards.handle("GET", "/leaderboards")
        self.assertEqual(json.loads(data)["boards"][0]["players"], 1)
        self.ban(False)
        self.assertEqual(self.rows(OTHER), ["Cheater", "Honest"])
        self.assertEqual(self.public_board(), ["Cheater", "Honest"])

    def test_club_rollup_and_online_xp(self):
        scores.STORE.club["%016X" % BAD] = {"xuid": "%016X" % BAD, "gamertag": "Cheater",
                                            "cars": {"1398": [1, 2]}}
        lsp.save_player(BAD, {"gamertag": "Cheater", "profile": {"onlineXP": 900}})
        lsp.save_player(GOOD, {"gamertag": "Honest", "profile": {"onlineXP": 100}})
        self.ban()
        self.assertEqual(scores.club_entries(), [])
        self.assertEqual([e["gamertag"] for e in scores.rollup_entries(1, -1)], ["Honest"])
        self.assertEqual([e["gamertag"] for e in scores.online_xp_entries()], ["Honest"])
        self.ban(False)
        self.assertEqual([e["gamertag"] for e in scores.club_entries()], ["Cheater"])
        self.assertEqual([e["gamertag"] for e in scores.online_xp_entries()],
                         ["Cheater", "Honest"])

    def test_stats_ranks(self):
        for xuid in (BAD, GOOD):
            stats.STORE.view(xuid, 0x1234, NAMES[xuid], create=True)
        self.ban()
        ranks, total = stats.ranking(0x1234)
        self.assertEqual((list(ranks), total), (["%016X" % GOOD], 1))
        self.ban(False)
        self.assertEqual(stats.ranking(0x1234)[1], 2)

    def test_challenge_counts(self):
        kind = objectives.STORE.current("daily", time.time())[1]["objectives"][0]["type"]
        target = objectives.STORE.current("daily", time.time())[1]["objectives"][0]["target"]
        for n in range(target):
            for xuid in (BAD, GOOD):
                objectives.step(xuid, kind, lambda obj: "board %d" % n)
        counted = lambda: objectives.document()["sets"][0]["objectives"][0]["completed"]  # noqa
        self.assertEqual(counted(), 2)
        self.ban()
        self.assertEqual(counted(), 1)
        self.ban(False)
        self.assertEqual(counted(), 2)

    def test_auctions(self):
        aid = self.call(BAD, "Auction", "Create", A.ForzaAuctionCreate_Params,
                        {"durationMs": 3600000, "openingPrice": 100},
                        A.ForzaAuctionCreate_Results)["auctionId"]
        search = lambda: self.call(GOOD, "Auction", "SearchForAuction",  # noqa: E731
                                   A.ForzaAuctionSearchForAuction_Params,
                                   {"maxResultIds": 10, "maxResults": 10},
                                   A.ForzaAuctionSearchForAuction_Results)["auctionIds"]
        self.assertEqual(search(), [aid])
        self.ban()
        self.assertEqual(search(), [])
        self.ban(False)
        self.assertEqual(search(), [aid])

    def test_friend_requests_and_search(self):
        store = self.server.friends.store
        store.ask(BAD, GOOD)
        self.say_hello(GOOD, features=friends.FEATURE)
        peer = self.online(GOOD)[0]

        def found():
            self.transport.sent.clear()
            self.server.friends.search(peer, 1, "Cheat")
            return self.transport.sent[-1][1][5 + 3]  # rows in the answer (R_RESULTS)

        self.assertEqual((len(self.server.friends.entries(GOOD)), found()), (1, 1))
        self.ban()
        self.assertEqual((len(self.server.friends.entries(GOOD)), found()), (0, 0))
        self.assertIn("%016X" % BAD, store.get(GOOD)["in"])  # kept
        self.ban(False)
        self.assertEqual((len(self.server.friends.entries(GOOD)), found()), (1, 1))

    def test_coop_results_wait(self):
        lg = coop_ledger.LEDGER
        event = SimpleNamespace(id=12, code="FR05", drivers=7)
        rid = lg.start_round(b"world-01", event, BAD, "Cheater", {BAD: "Cheater"})
        lg.report(BAD, b"world-01", 1, True)
        self.assertEqual([r["id"] for r in lg.pending(BAD)], [rid])
        self.ban()
        self.assertEqual(lg.pending(BAD), [])
        self.assertEqual(lg.applied(BAD, [rid]), 0)
        self.ban(False)
        self.assertEqual([r["id"] for r in lg.pending(BAD)], [rid])


class PanelTest(Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(lsp, "ADMIN_TOKEN", TOKEN)
        patch.start()
        self.addCleanup(patch.stop)
        routes._fails["total"].clear()
        routes._fails["ip"].clear()

    def panel(self, method, path, body=None):
        h = {"host": NAME, "x-admin-token": TOKEN}
        data = b""
        if method == "POST":
            h["content-type"] = "application/json"
            data = json.dumps(body or {}).encode()
        status, ctype, out, _ = asyncio.run(routes.handle(self.server, method, routes.API + path,
                                                          h, data, LAN))
        return status, json.loads(out)

    def test_ban_from_the_panel_applies_everywhere(self):
        self.say_hello(BAD)
        self.submit(BAD, 50.0)
        self.assertFalse(self.panel("GET", "player?xuid=%016X" % BAD)[1]["banned"])
        status, d = self.panel("POST", "bans", {"xuid": "%016X" % BAD, "ban": True,
                                                "reason": "aimbot"})
        self.assertEqual((status, d["banned"]), (200, True))
        self.assertEqual(self.online(BAD), [])
        self.assertEqual(self.rows(OTHER), [])
        page = self.panel("GET", "player?xuid=%016X" % BAD)[1]
        self.assertTrue(page["banned"])
        self.assertEqual(page["scores"]["ranked"], 1)  # the operator still sees the row
        with open(os.path.join(self.dir.name, audit.FILE)) as f:
            line = json.loads(f.readlines()[-1])
        self.assertEqual((line["action"], line["target"]), ("ban", "%016X" % BAD))
        self.assertEqual(self.panel("POST", "bans", {"xuid": "%016X" % BAD, "ban": False})[0], 200)
        self.assertFalse(self.panel("GET", "player?xuid=%016X" % BAD)[1]["banned"])
        self.assertEqual(self.rows(OTHER), ["Cheater"])
        self.say_hello(BAD)
        self.assertEqual(len(self.online(BAD)), 1)

    def test_the_curl_endpoint_drops_too(self):
        self.say_hello(BAD)
        status, _, data, *_ = community.admin_handle(
            "POST", urllib.parse.urlsplit("/admin/photos/ban/%016X" % BAD),
            {}, b"", lambda: True)
        self.assertEqual(status, 200)
        self.assertEqual(self.online(BAD), [])
        self.assertTrue(designs.banned(BAD))


if __name__ == "__main__":
    unittest.main()
