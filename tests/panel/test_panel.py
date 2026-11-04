"""The operator panel (panel/routes.py): who reaches it (the home network only, never through the
tunnel or a proxy, where every panel URL is 404), the token, the Origin rules and the failed-token
limit, every route's input checks and the audit line each change writes, the page's headers, and
the /admin/broadcasts routes it added for curl.

Run: python3 -m unittest discover -s tests -t .
"""

import argparse
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from forza_live import server as srv
from forza_live.competition import objectives
from forza_live.content import designs
from forza_live.content import market
from forza_live.core import limits
from forza_live.core import relays
from forza_live.core import server_list
from forza_live.panel import audit
from forza_live.panel import routes
from forza_live.social import discord_status
from forza_live.social import notifications
from forza_live.title_services import lsp
from tests.content import test_community as tc
from tests.content import test_design_feed as td
from tests.support import FakeTransport

TOKEN = "panel-test-token"
LAN = ("192.168.1.20", 50000)
ALICE, BOB = 0x0009000000000A01, 0x0009000000000B02
JSON = {"Content-Type": "application/json"}

# One request for every panel route (READS and ACTIONS): test_every_route_is_covered checks it.
READ_PATHS = ["overview", "broadcasts", "gifts", "players?q=Al", "player?xuid=0009000000000A01",
              "bans", "chat", "scores", "photos", "photos/queue", "photos/img/AAAAAAAAAAAA.jpg",
              "designs", "designs/queue", "designs/img/%s.png" % ("A" * designs.WEB_ID_LEN),
              "servers", "relays", "challenges", "events", "events/x-1/attendance", "audit"]
ACTION_PATHS = ["broadcasts", "broadcasts/delete", "notify", "gifts", "bans", "chat/mute",
                "chat/kick", "scores/unrank", "scores/rank", "scores/delete",
                "photos/AAAAAAAAAAAA/hide", "designs/%s/hide" % ("A" * designs.WEB_ID_LEN),
                "servers/save", "servers/remove", "servers/check", "relays/add", "relays/remove",
                "events", "events/enroll", "events/x-1/start", "events/x-1/close",
                "events/x-1/award", "events/x-1/revoke", "world-event"]
PAGES = ["/operator", "/operator/", "/operator/panel.js", "/operator/panel.css",
         "/operator/nothing-here", "/operator/api/", "/operator/api/nothing"]


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir.name, "--server-name", "panel.test"]))
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = FakeTransport()
        self.addCleanup(self.server.events.db.close)
        for xuid, name in ((ALICE, "Alice"), (BOB, "Bob")):
            lsp.save_player(xuid, {"gamertag": name, "lastLogon": 1790000000})
            self.server.friends.store.seen(xuid, name)
        self.token = mock.patch.object(lsp, "ADMIN_TOKEN", TOKEN)
        self.token.start()
        routes._fails["total"].clear()
        routes._fails["ip"].clear()

    def tearDown(self):
        self.token.stop()
        limits.configure_proxies()
        lsp.DATA_DIR = self.old_dir
        self.dir.cleanup()

    def call(self, method, path, body=None, headers=None, peer=LAN, token=TOKEN, raw=False):
        h = {"host": "panel.test"}
        if token is not None:
            h["x-admin-token"] = token
        if method == "POST":
            h["content-type"] = "application/json"
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode() \
            if method == "POST" else b""
        path = path if path.startswith("/") else routes.API + path
        answer = run(routes.handle(self.server, method, path, h, data, peer))
        if raw:
            return answer
        status, ctype, out, _ = answer
        return status, (json.loads(out) if ctype == "application/json" else out)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)

    def audit_lines(self):
        try:
            with open(os.path.join(self.dir.name, audit.FILE)) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def last_audit(self):
        return self.audit_lines()[-1]


class ReachTest(Base):
    def test_who_reaches_it(self):
        self.assertTrue(routes.reachable(LAN, {}))
        for ip in ("127.0.0.1", "10.1.2.3", "172.20.0.5", "192.168.0.9", "::1", "fd00::5",
                   "fe80::1", "::ffff:192.168.1.7", "169.254.3.4"):
            self.assertTrue(routes.reachable((ip, 1), {}), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2001:4860::8888", "::ffff:8.8.8.8", "100.64.0.1", "?",
                   "not an address", ""):
            self.assertFalse(routes.reachable((ip, 1), {}), ip)
        for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
            self.assertFalse(routes.reachable(LAN, {header: "203.0.113.9"}), header)
        limits.configure_proxies("192.168.1.20")  # the tunnel agent's address
        self.assertFalse(routes.reachable(LAN, {}))
        limits.configure_proxies()
        with mock.patch.object(lsp, "ADMIN_TOKEN", ""):  # no token: no panel
            self.assertFalse(routes.reachable(LAN, {}))

    def test_every_route_is_covered(self):
        for table, paths in ((routes.READS, READ_PATHS), (routes.ACTIONS, ACTION_PATHS)):
            for pattern, _ in table:
                self.assertTrue(any(routes.match(((pattern, 1),), p.split("?")[0]) for p in paths),
                                pattern)

    def every_url(self):
        for p in PAGES:
            yield "GET", p
        for p in READ_PATHS:
            yield "GET", routes.API + p
        for p in ACTION_PATHS:
            yield "POST", routes.API + p
            yield "GET", routes.API + p

    def test_every_panel_url_is_404_through_a_proxy(self):
        def check(**kw):
            for method, path in self.every_url():
                status, _ = self.call(method, path, {"to": "Alice", "credits": 1, "reason": "r"}
                                      if method == "POST" else None, **kw)
                self.assertEqual(status, 404, (method, path, kw))
        for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
            check(headers={header: "203.0.113.9"})
        check(peer=("8.8.8.8", 4000))  # a port forwarded to the internet, no tunnel
        limits.configure_proxies("192.168.1.0/24")
        check()  # from the tunnel agent's address
        limits.configure_proxies()
        with mock.patch.object(lsp, "ADMIN_TOKEN", ""):
            check()
        self.assertEqual(self.audit_lines(), [])  # nothing was done
        self.assertEqual(notifications.broadcasts(), [])

    def test_through_the_http_handler(self):
        async def request(path, headers=()):
            server = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                                "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n"
                              % (path, "".join(h + "\r\n" for h in headers))).encode())
                raw = await reader.read()
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
            head, body = raw.split(b"\r\n\r\n", 1)
            return head.decode(), body
        head, body = run(request("/operator/"))
        self.assertIn("HTTP/1.1 200", head)
        self.assertIn("Content-Type: text/html", head)
        self.assertIn("Content-Security-Policy: default-src 'none'; script-src 'self'", head)
        self.assertIn("X-Frame-Options: DENY", head)
        self.assertIn("Cache-Control: no-store", head)
        self.assertNotIn("Access-Control-Allow-Origin", head)
        self.assertIn(b"/operator/panel.js", body)
        self.assertNotIn(TOKEN.encode(), body)
        head, _ = run(request("/operator/api/overview", ["X-Admin-Token: " + TOKEN]))
        self.assertIn("HTTP/1.1 200", head)
        self.assertNotIn("Access-Control-Allow-Origin", head)
        for extra in (["X-Forwarded-For: 203.0.113.9"], ["X-Real-IP: 203.0.113.9"]):
            for path in ("/operator/", "/operator/panel.js", "/operator/api/overview"):
                head, body = run(request(path, extra + ["X-Admin-Token: " + TOKEN]))
                self.assertIn("HTTP/1.1 404", head, (path, extra))
        limits.configure_proxies("127.0.0.1")  # the tunnel agent on this machine
        head, _ = run(request("/operator/api/overview", ["X-Admin-Token: " + TOKEN]))
        self.assertIn("HTTP/1.1 404", head)
        # the other endpoints keep their answers (the status page, the game's routes)
        head, _ = run(request("/status"))
        self.assertIn("HTTP/1.1 200", head)


class AuthTest(Base):
    def test_the_page_needs_no_token_but_the_api_does(self):
        status, body = self.call("GET", "/operator/", token=None)
        self.assertEqual(status, 200)
        self.assertNotIn(TOKEN.encode(), body)
        status, ctype, js, extra = self.call("GET", "/operator/panel.js", token=None, raw=True)
        self.assertEqual((status, ctype), (200, "text/javascript; charset=utf-8"))
        for sink in (b"innerHTML", b"outerHTML", b"insertAdjacentHTML", b"document.write",
                     b"eval(", b"new Function"):
            self.assertNotIn(sink, js)  # values reach the page as text only
        self.assertIn("X-Content-Type-Options: nosniff", extra)
        self.assertEqual(self.call("GET", "overview", token=None)[0], 401)
        self.assertEqual(self.call("GET", "overview", token="")[0], 401)
        self.assertEqual(self.call("GET", "overview", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "overview")[0], 200)
        # never in the URL
        self.assertEqual(self.call("GET", "overview?token=" + TOKEN, token=None)[0], 401)
        self.assertEqual(self.call("POST", "broadcasts", {"text": "x"}, token=None)[0], 401)
        self.assertEqual(notifications.broadcasts(), [])

    def test_failed_tokens_are_limited(self):
        for _ in range(routes.FAILS_PER_IP):
            self.assertEqual(self.call("GET", "overview", token="guess")[0], 401)
        self.assertEqual(self.call("GET", "overview")[0], 429)  # even the right one, for now
        self.assertEqual(self.call("GET", "overview", peer=("192.168.1.21", 1))[0], 200)
        # a missing header is not a guess
        for _ in range(routes.FAILS_PER_IP + 2):
            self.call("GET", "overview", token=None, peer=("192.168.1.22", 1))
        self.assertEqual(self.call("GET", "overview", peer=("192.168.1.22", 1))[0], 200)
        # the window passes
        with mock.patch.object(routes.time, "monotonic", return_value=1e12):
            self.assertEqual(self.call("GET", "overview")[0], 200)

    def test_everyone_together_is_limited_too(self):
        for i in range(routes.FAILS_TOTAL):
            self.call("GET", "overview", token="guess", peer=("10.0.%d.%d" % (i // 200, i % 200), 1))
        self.assertEqual(self.call("GET", "overview", peer=("10.9.9.9", 1))[0], 429)

    def test_origin(self):
        self.assertEqual(self.call("GET", "overview", headers={"Origin": "http://panel.test"})[0], 200)
        self.assertEqual(self.call("GET", "overview", headers={"Origin": "https://panel.test"})[0], 200)
        for origin in ("https://evil.example", "null", "http://panel.test.evil.example",
                       "http://other.test"):
            self.assertEqual(self.call("GET", "overview", headers={"Origin": origin})[0], 403, origin)
            self.assertEqual(self.post("broadcasts", {"text": "x"}, headers={"Origin": origin})[0],
                             403, origin)
        for site in ("cross-site", "same-site"):
            self.assertEqual(self.call("GET", "overview", headers={"Sec-Fetch-Site": site})[0], 403)
        self.assertEqual(self.call("GET", "overview", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(notifications.broadcasts(), [])

    def test_a_change_is_json(self):
        status, _ = self.call("POST", "broadcasts", b'{"text": "x"}',
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.call("POST", "broadcasts", b'{"text": "x"}',
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertEqual(self.post("broadcasts", b"not json")[0], 400)
        self.assertEqual(self.post("broadcasts", b"[1, 2]")[0], 400)
        self.assertEqual(self.post("broadcasts", b"{" + b" " * routes.MAX_BODY + b"}")[0], 413)
        self.assertEqual(self.call("PUT", "broadcasts")[0], 405)
        self.assertEqual(self.post("no/such/action")[0], 404)
        self.assertEqual(self.call("GET", "no/such/read")[0], 404)
        self.assertEqual(notifications.broadcasts(), [])


class MessagesTest(Base):
    def test_broadcast_list_and_delete(self):
        status, d = self.post("broadcasts", {"type": "0x0100", "text": "Server <b>news</b>", "days": 3})
        self.assertEqual(status, 200)
        nid = d["id"]
        line = self.last_audit()
        self.assertEqual((line["who"], line["action"], line["target"], line["status"], line["from"]),
                         ("operator", "broadcast", "everyone", 200, LAN[0]))
        self.assertEqual(line["detail"]["text"], "Server <b>news</b>")
        status, d = self.call("GET", "broadcasts")
        self.assertEqual([(b["id"], b["text"], b["name"]) for b in d["broadcasts"]],
                         [(nid, "Server <b>news</b>", "news")])
        self.assertIn("0x0100", d["text_types"])
        self.assertEqual(self.post("broadcasts/delete", {"id": nid}), (200, {"deleted": nid}))
        self.assertEqual(self.last_audit()["action"], "broadcast delete")
        self.assertEqual(self.post("broadcasts/delete", {"id": nid})[0], 404)
        self.assertEqual(notifications.pending(ALICE), [])

    def test_broadcast_input(self):
        for body in ({"text": ""}, {"text": "x" * 513}, {"text": "x", "days": 0},
                     {"text": "x", "days": 31}, {"text": "x", "type": "0x9999"},
                     {"text": "x", "type": "nope"}, {"text": 5}, {"text": "x", "days": "many"},
                     {"text": "x\x00y"}):
            self.assertEqual(self.post("broadcasts", body)[0], 400, body)
        self.assertEqual(self.post("broadcasts/delete", {"id": "x"})[0], 400)
        self.assertEqual(notifications.broadcasts(), [])
        self.assertEqual({line["status"] for line in self.audit_lines()}, {400})

    def test_notify_one_player(self):
        status, d = self.post("notify", {"to": "Alice", "type": "0x0100", "text": "Hi"})
        self.assertEqual((status, d["to"]), (200, "%016X" % ALICE))
        self.assertEqual([n["params"]["Message"] for n in notifications.pending(ALICE)], ["Hi"])
        self.assertEqual(self.post("notify", {"to": "Alice", "type": "0x0100"})[0], 400)  # no text
        status, d = self.post("notify", {"to": "Bob", "type": "sample"})
        self.assertEqual((status, len(d["ids"])), (200, len(notifications.pending(BOB))))
        self.assertEqual(self.post("notify", {"to": "Nobody", "text": "Hi"})[0], 404)
        self.assertEqual(self.post("notify", {"to": "", "text": "Hi"})[0], 400)
        self.assertEqual((self.last_audit()["action"], self.last_audit()["status"]), ("notify", 400))


class GiftTest(Base):
    def test_a_gift(self):
        status, d = self.post("gifts", {"to": "alice", "credits": 50000, "reason": "Event winner"})
        self.assertEqual((status, d["to"]), (200, "%016X" % ALICE))
        gift = market.MARKET.gifts[d["giftId"]]
        self.assertEqual((gift["credits"], gift["recipient"]), (50000, "%016X" % ALICE))
        n, = notifications.pending(ALICE)
        self.assertEqual(n["params"]["UserSuppliedReason"], "Event winner")
        line = self.last_audit()
        self.assertEqual((line["action"], line["target"], line["detail"]["credits"]),
                         ("gift", "alice", 50000))
        status, d = self.call("GET", "gifts")
        self.assertEqual([(g["to"], g["credits"], g["gamertag"]) for g in d["gifts"]],
                         [("%016X" % ALICE, 50000, "Alice")])

    def test_the_cap_and_the_reason(self):
        for body in ({"to": "Alice", "credits": routes.GIFT_MAX + 1, "reason": "x"},
                     {"to": "Alice", "credits": 0, "reason": "x"},
                     {"to": "Alice", "credits": -5, "reason": "x"},
                     {"to": "Alice", "credits": 1.5, "reason": "x"},
                     {"to": "Alice", "credits": True, "reason": "x"},
                     {"to": "Alice", "credits": 100},
                     {"to": "Alice", "credits": 100, "reason": "  "},
                     {"to": "Alice", "credits": 100, "reason": "x" * 201},
                     {"credits": 100, "reason": "x"}):
            self.assertEqual(self.post("gifts", body)[0], 400, body)
        self.assertEqual(self.post("gifts", {"to": "Nobody", "credits": 1, "reason": "x"})[0], 404)
        market.MARKET.load()
        self.assertEqual(market.MARKET.gifts, {})
        self.assertEqual(self.post("gifts", {"to": "Alice", "credits": routes.GIFT_MAX,
                                             "reason": "x"})[0], 200)


class PlayerTest(Base):
    def test_search(self):
        status, d = self.call("GET", "players?q=ali")
        self.assertEqual([(p["xuid"], p["gamertag"]) for p in d["players"]], [("%016X" % ALICE, "Alice")])
        status, d = self.call("GET", "players?q=0009000000000B")
        self.assertEqual([p["gamertag"] for p in d["players"]], ["Bob"])
        lsp.save_player(0x0009000000000C03, {"gamertag": "Carol"})  # a record only
        self.assertEqual([p["gamertag"] for p in self.call("GET", "players?q=car")[1]["players"]],
                         ["Carol"])
        self.assertEqual(self.call("GET", "players?q=")[0], 400)
        self.assertEqual(self.call("GET", "players?q=" + "x" * 40)[0], 400)

    def test_a_players_page(self):
        self.server.friends.store.get(ALICE)["pub"] = "ab" * 32
        self.server.friends.store.befriend(ALICE, BOB)
        self.post("gifts", {"to": "Alice", "credits": 10, "reason": "x"})
        self.post("bans", {"xuid": "%016X" % ALICE, "ban": True, "reason": "cheating"})
        status, p = self.call("GET", "player?xuid=%016X" % ALICE)
        self.assertEqual(status, 200)
        self.assertEqual((p["gamertag"], p["key"]["state"], p["friends"]["friends"]),
                         ("Alice", "signed", 1))
        self.assertEqual((p["bans"]["content"], p["bans"]["content_reason"]), (True, "cheating"))
        self.assertEqual([g["credits"] for g in p["gifts"]], [10])
        self.assertNotIn("ab" * 32, json.dumps(p))  # only the key's start
        for bad in ("xyz", "", "0009", "0009000000000A01X"):
            self.assertEqual(self.call("GET", "player?xuid=" + bad)[0], 400, bad)
        self.assertEqual(self.call("GET", "player?xuid=0009000000000FFF")[0], 404)

    def test_accounts_admin_link(self):
        with mock.patch.object(routes, "ACCOUNTS_ADMIN", "https://accounts.example.org/admin"):
            p = self.call("GET", "player?xuid=%016X" % ALICE)[1]
            self.assertEqual(p["accounts_admin"],
                             "https://accounts.example.org/admin/members/user/?q=%016X" % ALICE)
        for bad in ("javascript:alert(1)", "https://x/\"><script>", "ftp://x/"):
            with mock.patch.object(routes, "ACCOUNTS_ADMIN", bad):
                self.assertEqual(self.call("GET", "overview")[1]["panel"]["accounts_admin"], "")


class BanTest(Base):
    def test_ban_and_unban(self):
        x = "%016X" % BOB
        self.assertEqual(self.post("bans", {"xuid": x, "ban": True})[0], 400)  # no reason
        status, d = self.post("bans", {"xuid": x.lower(), "ban": True, "reason": "slurs in titles"})
        self.assertEqual((status, d["banned"], d["changed"]), (200, True, True))
        self.assertTrue(designs.banned(BOB))
        line = self.last_audit()
        self.assertEqual((line["action"], line["target"], line["detail"]["reason"]),
                         ("ban", x, "slurs in titles"))
        self.assertEqual(self.call("GET", "bans")[1]["content"],
                         [{"xuid": x, "gamertag": "Bob", "reason": "slurs in titles"}])
        self.assertEqual(self.post("bans", {"xuid": x, "ban": False})[0], 200)
        self.assertFalse(designs.banned(BOB))
        self.assertEqual(self.last_audit()["action"], "unban")
        for body in ({"xuid": "nope", "reason": "x"}, {"xuid": 5, "reason": "x"},
                     {"xuid": x, "ban": "yes", "reason": "x"}):
            self.assertEqual(self.post("bans", body)[0], 400, body)

    def test_sign_in_bans_and_mutes(self):
        status, d = self.post("chat/kick", {"who": "Bob", "minutes": 30, "reason": "griefing"})
        self.assertEqual((status, d["players"][0]["xuid"]), (200, "%016X" % BOB))
        self.assertTrue(self.server.chat.banned(BOB))
        self.assertEqual((self.last_audit()["action"], self.last_audit()["target"]), ("chat kick", "Bob"))
        self.post("chat/mute", {"who": "Alice", "minutes": 5, "reason": "spam"})
        timed = self.call("GET", "bans")[1]["timed"]
        self.assertEqual(sorted((t["kind"], t["gamertag"]) for t in timed),
                         [("mute", "Alice"), ("sign-in", "Bob")])
        self.assertEqual(self.post("chat/kick", {"who": "Bob", "minutes": 0})[0], 200)  # lifted
        self.assertFalse(self.server.chat.banned(BOB))
        for body in ({"who": "Bob", "minutes": 10}, {"who": "Bob", "minutes": -1, "reason": "x"},
                     {"who": "Bob", "minutes": 10 ** 7, "reason": "x"}, {"minutes": 1, "reason": "x"}):
            self.assertEqual(self.post("chat/kick", body)[0], 400, body)
        self.assertEqual(self.post("chat/mute", {"who": "Nobody", "minutes": 1, "reason": "x"})[0], 404)
        self.assertEqual(self.call("GET", "chat")[0], 200)


class ScoreTest(Base):
    def test_rows_by_source(self):
        status, d = self.call("GET", "scores?build=none")
        self.assertEqual((status, d["matched"], d["score_tokens"]["mode"]), (200, 0, "log"))
        self.assertEqual(self.call("GET", "scores?state=bogus")[0], 400)
        self.assertEqual(self.post("scores/unrank", {"filters": {}})[0], 400)  # a filter is needed
        self.assertEqual(self.post("scores/unrank", {"filters": {"state": "ranked"}})[0], 400)
        self.assertEqual(self.post("scores/unrank", {"filters": {"nope": "1"}})[0], 400)
        self.assertEqual(self.post("scores/unrank", {"filters": "build=x"})[0], 400)
        status, d = self.post("scores/unrank", {"filters": {"build": "0.2.1/tu4"}})
        self.assertEqual((status, d["action"], d["changed"]), (200, "unrank", 0))
        line = self.last_audit()
        self.assertEqual((line["action"], line["target"]), ("scores unrank", '{"build": "0.2.1/tu4"}'))
        self.assertEqual(self.post("scores/promote", {"filters": {"build": "x"}})[0], 404)


class PhotoPanelTest(tc.Base):
    """The panel's photo moderation goes through community.admin_handle."""

    def panel(self, method, path, body=None):
        h = {"host": "panel.test", "x-admin-token": tc.TOKEN, "content-type": "application/json"}
        data = json.dumps(body or {}).encode() if method == "POST" else b""
        server = SimpleNamespace(friends=self.server.friends)
        status, ctype, out, extra = run(routes.handle(server, method, routes.API + path, h, data, tc.LAN))
        return status, (json.loads(out) if ctype == "application/json" else out), ctype, extra

    def setUp(self):
        super().setUp()
        routes._fails["total"].clear()
        routes._fails["ip"].clear()
        self.upload(tc.ALICE, tc.make_jpeg())
        self.pid = self.only_photo()[0]

    def test_queue_actions_and_image(self):
        status, d, _, _ = self.panel("GET", "photos")
        self.assertEqual([p["id"] for p in d["photos"]], [self.pid])
        self.assertEqual(self.panel("GET", "photos?status=bogus")[0], 400)
        status, data, ctype, extra = self.panel("GET", "photos/img/%s.jpg" % self.pid)
        self.assertEqual((status, ctype), (200, "image/jpeg"))
        self.assertIn("Cache-Control: no-store", extra)
        status, d, _, _ = self.panel("POST", "photos/%s/hide" % self.pid, {"note": "nsfw"})
        self.assertEqual((status, d["status"], d["note"]), (200, "hidden", "nsfw"))
        self.assertEqual(self.panel("POST", "photos/%s/release" % self.pid)[0], 409)
        self.assertEqual(self.panel("POST", "photos/%s/explode" % self.pid)[0], 404)
        self.assertEqual(self.panel("POST", "photos/%s/ban" % self.pid)[0], 400)  # no reason
        self.assertEqual(self.panel("POST", "photos/%s/delete" % self.pid)[0], 200)
        self.assertEqual(self.records(), {})
        with open(os.path.join(self.dir.name, audit.FILE)) as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual([(line["action"], line["target"], line["status"]) for line in lines],
                         [("photo hide", self.pid, 200), ("photo release", self.pid, 409),
                          ("photo explode", self.pid, 404), ("photo ban", self.pid, 400),
                          ("photo delete", self.pid, 200)])
        self.assertNotIn(tc.TOKEN, json.dumps(lines))


class DesignPanelTest(td.Base):
    def setUp(self):
        super().setUp()
        routes._fails["total"].clear()
        routes._fails["ip"].clear()
        self.fid = self.share(td.ALICE)

    def panel(self, method, path, body=None):
        h = {"host": "panel.test", "x-admin-token": td.TOKEN, "content-type": "application/json"}
        data = json.dumps(body or {}).encode() if method == "POST" else b""
        status, ctype, out, _ = run(routes.handle(self.server, method, routes.API + path, h, data,
                                                  td.LAN))
        return status, (json.loads(out) if ctype == "application/json" else out)

    def test_queue_and_actions(self):
        status, d = self.panel("GET", "designs/queue")
        (design,) = d["designs"]
        wid = design["id"]
        self.assertEqual(self.panel("GET", "designs?state=bogus")[0], 400)
        self.assertEqual(self.panel("GET", "designs?state=pending")[1]["designs"][0]["owner"],
                         "%016X" % td.ALICE)
        status, d = self.panel("POST", "designs/%s/release" % wid, {"expect": "pending"})
        self.assertEqual((status, d["state"]), (200, "clear"))
        self.assertEqual(self.panel("POST", "designs/%s/release" % wid, {"expect": "pending"})[0], 409)
        self.assertEqual(self.panel("POST", "designs/%s/hide" % wid)[0], 200)
        self.assertEqual(self.panel("POST", "designs/%s/delete" % wid)[0], 200)
        self.assertEqual(self.panel("POST", "designs/%s/delete" % wid)[0], 404)
        with open(os.path.join(self.dir.name, audit.FILE)) as f:
            actions = [json.loads(line)["action"] for line in f]
        self.assertEqual(actions, ["design release", "design release", "design hide",
                                   "design delete", "design delete"])


class ServerListTest(Base):
    def entry(self, **kw):
        return dict({"name": "Example community", "address": "play.example.org:21200",
                     "region": "Europe", "contact": "owner@example.org", "verified": True}, **kw)

    def saved(self):
        with open(os.path.join(self.dir.name, server_list.LIST_FILE)) as f:
            return json.load(f)

    def test_add_edit_remove(self):
        self.assertEqual(self.call("GET", "servers")[1]["file"], False)
        status, d = self.post("servers/save", {"slot": "new", "before": None, "entry": self.entry()})
        self.assertEqual(status, 200)
        self.assertEqual(self.saved()["servers"][0]["address"], "play.example.org:21200")
        self.assertEqual((self.last_audit()["action"], self.last_audit()["target"]),
                         ("server list save", "play.example.org:21200"))
        self.post("servers/save", {"slot": "self", "before": None,
                                   "entry": {"name": "Main", "address": "main.example.org"}})
        d = self.call("GET", "servers")[1]
        self.assertEqual([e["name"] for e in d["listed"]], ["Main", "Example community"])
        self.assertEqual(d["problems"], [])
        # an edit made on what the page showed
        self.assertEqual(self.post("servers/save", {"slot": 0, "before": "old.example.org",
                                                    "entry": self.entry(name="New")})[0], 409)
        self.assertEqual(self.post("servers/save", {"slot": 0, "before": "play.example.org:21200",
                                                    "entry": self.entry(name="New")})[0], 200)
        self.assertEqual(self.saved()["servers"][0]["name"], "New")
        # a duplicate address, this server's own, bad entries
        self.assertEqual(self.post("servers/save", {"slot": "new", "entry": self.entry()})[0], 409)
        self.assertEqual(self.post("servers/save", {"slot": "new", "entry": self.entry(
            address="main.example.org")})[0], 409)
        for entry in (self.entry(address="not an address"), self.entry(name=""),
                      self.entry(name="x" * 49), self.entry(verified="yes"),
                      self.entry(region="a\x01b"), dict(self.entry(), evil=1), "x"):
            self.assertEqual(self.post("servers/save", {"slot": "new", "entry": entry})[0], 400, entry)
        for slot in (7, -1, "x", True, None):
            self.assertEqual(self.post("servers/save", {"slot": slot, "entry": self.entry(
                address="b.example.org")})[0], 400, slot)
        self.assertEqual(len(self.saved()["servers"]), 1)
        self.assertEqual(self.post("servers/remove", {"slot": 0, "before": "x"})[0], 409)
        self.assertEqual(self.post("servers/remove", {"slot": "self"})[0], 400)
        self.assertEqual(self.post("servers/remove", {"slot": 0, "before": "play.example.org:21200"})[0],
                         200)
        self.assertEqual(self.saved()["servers"], [])
        self.assertEqual(self.saved()["self"]["name"], "Main")

    def test_a_damaged_file_is_left_alone(self):
        with open(os.path.join(self.dir.name, server_list.LIST_FILE), "w") as f:
            f.write("[not, json")
        self.assertEqual(self.post("servers/save", {"slot": "new", "entry": self.entry()})[0], 409)
        self.assertEqual(self.call("GET", "servers")[0], 200)
        with open(os.path.join(self.dir.name, server_list.LIST_FILE)) as f:
            self.assertEqual(f.read(), "[not, json")

    def test_status_checks(self):
        self.post("servers/save", {"slot": "new", "entry": self.entry()})
        asked = []

        def fetch(address, timeout=4):
            asked.append(address)
            return {"server": "forza-live", "protocol": 1, "players": 3, "worlds": {},
                    "rooms": {"#TU4": [{"gamertag": "A"}, {"gamertag": "B"}, {"gamertag": "C"}]}} \
                if address.startswith("play.") else None
        with mock.patch.object(discord_status, "fetch_status", fetch):
            status, d = self.post("servers/check", {"address": "play.example.org:21200"})
            self.assertEqual((status, d["checks"][0]["up"], d["checks"][0]["players"]), (200, True, 3))
            status, d = self.post("servers/check", {"address": "down.example.org"})
            self.assertEqual(d["checks"][0]["up"], False)
            status, d = self.post("servers/check", {})
            self.assertEqual([c["address"] for c in d["checks"]], ["play.example.org:21200"])
            self.assertEqual(self.post("servers/check", {"address": "bad address!"})[0], 400)
        self.assertEqual(asked, ["play.example.org:21200", "down.example.org", "play.example.org:21200"])
        self.assertEqual(self.last_audit()["action"], "server check")


class RelayTest(Base):
    def test_add_and_remove(self):
        status, d = self.post("relays/add", {"id": "eu-1", "name": "Example relay",
                                             "address": "relay.example.org:21200", "region": "Europe"})
        self.assertEqual((status, d["secret_file"]), (200, os.path.join("relay_secrets", "eu-1.txt")))
        secret = relays.read_secret(relays.secret_path("eu-1", self.dir.name))
        self.assertNotIn(secret.hex(), json.dumps(d))  # never in the page
        self.assertNotIn(secret.hex(), json.dumps(self.audit_lines()))
        self.assertNotIn(self.dir.name, json.dumps(d))  # no data path either
        d = self.call("GET", "relays")[1]
        self.assertEqual([e["id"] for e in d["entries"]], ["eu-1"])
        for body in ({"id": "EU 1", "name": "x", "address": "relay.example.org:1"},
                     {"id": "eu-2", "name": "", "address": "relay.example.org:1"},
                     {"id": "eu-2", "name": "x", "address": "bad address!"}):
            self.assertEqual(self.post("relays/add", body)[0], 400, body)
        self.assertEqual(self.post("relays/remove", {"id": "eu-1"}), (200, {"revoked": "eu-1"}))
        self.assertEqual(self.post("relays/remove", {"id": "eu-1"})[0], 404)
        self.assertEqual([line["action"] for line in self.audit_lines()],
                         ["relay add", "relay add", "relay add", "relay add", "relay remove",
                          "relay remove"])


class ChallengeEventWorldTest(Base):
    def test_challenges(self):
        self.assertEqual(self.call("GET", "challenges")[1], {"enabled": False, "gifts": False,
                                                            "gift_credits": objectives.GIFT_CREDITS,
                                                            "gift_after": objectives.GIFT_AFTER})
        with mock.patch.object(objectives, "ENABLED", True):
            d = self.call("GET", "challenges")[1]
            self.assertEqual([s["kind"] for s in d["sets"]], ["daily", "weekly"])
            self.assertEqual(d["gifts_sent"], [])

    def test_events(self):
        import time
        now = time.time()
        event = dict(id="panel-event", name="Cruise", description="Local test", activity="Free roam",
                     session="0123456789abcdef", starts=now - 1, ends=now + 300, rewards=[1197],
                     organizer="Test organizer")
        self.assertEqual(self.post("events", {"event": event})[0], 200)
        self.assertEqual([e["id"] for e in self.call("GET", "events")[1]["events"]], ["panel-event"])
        self.assertEqual(self.post("events/panel-event/start")[0], 200)
        self.assertEqual(self.call("GET", "events/panel-event/attendance")[1], {"attendees": []})
        self.assertEqual(self.post("events", {"event": "x"})[0], 400)
        self.assertEqual(self.post("events", {"event": dict(event, id="Bad Id")})[0], 400)
        self.assertEqual(self.post("events/enroll", {"xuid": "nope"})[0], 400)
        self.assertEqual(self.post("events/enroll", {"xuid": "%016X" % ALICE})[0], 400)  # not online
        self.assertEqual(self.post("events/panel-event/award", {"xuid": "%016X" % ALICE})[0], 400)
        self.assertEqual(self.post("events/panel-event/close")[0], 200)
        self.assertEqual([line["action"] for line in self.audit_lines()],
                         ["event create", "event start", "event create", "event create",
                          "event enroll", "event enroll", "event award", "event close"])

    def test_world_event(self):
        self.assertEqual(self.post("world-event", {"session": "all", "event": 0}),
                         (200, {"requested": [], "worlds": []}))
        for body in ({"session": "zz"}, {"session": "all", "event": -1}, {"event": "x"}):
            self.assertEqual(self.post("world-event", body)[0], 400, body)


class AuditTest(Base):
    def test_the_log(self):
        self.post("broadcasts", {"text": "one"})
        self.post("broadcasts", {"text": "two", "token": TOKEN, "secret": "s"})
        entries = self.call("GET", "audit")[1]["entries"]
        self.assertEqual([e["detail"]["text"] for e in entries], ["two", "one"])  # newest first
        self.assertNotIn(TOKEN, json.dumps(entries))
        self.assertNotIn("secret", entries[0]["detail"])
        self.assertEqual(self.call("GET", "audit?limit=1")[1]["entries"][0]["detail"]["text"], "two")
        # reads write nothing
        self.call("GET", "overview")
        self.call("GET", "players?q=a")
        self.assertEqual(len(self.audit_lines()), 2)

    def test_it_rolls_over(self):
        with mock.patch.object(audit, "MAX_BYTES", 600):
            for i in range(10):
                self.post("broadcasts", {"text": "message %d" % i})
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, audit.FILE + ".1")))
        entries = self.call("GET", "audit")[1]["entries"]
        self.assertEqual(entries[0]["detail"]["text"], "message 9")

    def test_long_values_are_cut(self):
        entry = audit.record("x" * 100, "y" * 500, {"a": "z" * 1000, "deep": {"b": {"c": {"d": 1}}}},
                             200, "192.168.1.2")
        self.assertEqual((len(entry["action"]), len(entry["target"]), len(entry["detail"]["a"])),
                         (64, 128, audit.MAX_TEXT))
        self.assertEqual(entry["detail"]["deep"]["b"]["c"], "...")


class AdminBroadcastsTest(Base):
    """GET /admin/broadcasts and POST /admin/broadcasts/<id>/delete, for curl."""

    def test_routes(self):
        nid = notifications.broadcast(notifications.NEWS, {"Message": "hello"})
        self.assertEqual(json.loads(lsp.broadcasts_admin("GET", "/admin/broadcasts")[2])
                         ["broadcasts"][0]["id"], nid)
        self.assertEqual(lsp.broadcasts_admin("POST", "/admin/broadcasts")[0], 405)
        self.assertEqual(lsp.broadcasts_admin("GET", "/admin/broadcasts/%d/delete" % nid)[0], 405)
        self.assertEqual(lsp.broadcasts_admin("POST", "/admin/broadcasts/x/delete")[0], 404)
        self.assertEqual(lsp.broadcasts_admin("POST", "/admin/broadcasts/%d/delete" % nid)[0], 200)
        self.assertEqual(lsp.broadcasts_admin("POST", "/admin/broadcasts/%d/delete" % nid)[0], 404)
        self.assertEqual(notifications.broadcasts(), [])

    def test_through_the_handler(self):
        nid = notifications.broadcast(notifications.NEWS, {"Message": "hello"})

        async def request(method, path, headers=()):
            server = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                                "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(("%s %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n"
                              % (method, path, "".join(h + "\r\n" for h in headers))).encode())
                raw = await reader.read()
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
            return raw.split(b"\r\n", 1)[0].decode()
        proxied = ["X-Forwarded-For: 203.0.113.9"]
        self.assertIn("403", run(request("GET", "/admin/broadcasts", proxied)))
        self.assertIn("403", run(request("POST", "/admin/broadcasts/%d/delete" % nid,
                                         ["Origin: https://evil.example", "X-Admin-Token: " + TOKEN])))
        self.assertIn("200", run(request("GET", "/admin/broadcasts", ["X-Admin-Token: " + TOKEN])))
        self.assertIn("200", run(request("POST", "/admin/broadcasts/%d/delete" % nid,
                                         ["X-Admin-Token: " + TOKEN])))
        self.assertEqual(notifications.broadcasts(), [])


if __name__ == "__main__":
    unittest.main()
