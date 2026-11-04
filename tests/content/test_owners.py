"""A player deletes their own photo or design from the website (owners.py): the owner-only routes
the website accounts service calls. Its own token only, never through the tunnel or a proxy,
never from a browser page, and only the owner's items; a deleted photo's picture can be
uploaded again (unlike an operator's delete); a design goes from the game and the site.

Run: python3 -m unittest discover -p 'test_*.py'
"""

import asyncio
import json
import os
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.content import community
from forza_live.content import designs
from forza_live.content import owners
from forza_live.core import limits
from tests.content import test_community as tc
from tests.content import test_design_feed as td

OWNER_TOKEN = "owner-route-test-token"


class OwnerRoutes:
    def setUp(self):
        super().setUp()
        self.owner_token = mock.patch.object(owners, "OWNER_TOKEN", OWNER_TOKEN)
        self.owner_token.start()

    def tearDown(self):
        self.owner_token.stop()
        super().tearDown()

    def owner_delete(self, kind, item, owner, token=OWNER_TOKEN, headers=None, method="POST",
                     body=None, peer=tc.LAN):
        h = {"X-Owner-Token": token} if token is not None else {}
        h.update(headers or {})
        raw = body if body is not None else json.dumps({"owner": "%016X" % owner}).encode()
        status, ctype, data, _ = self.http(method, "/owner/%s/%s/delete" % (kind, item), h, raw, peer)
        self.assertEqual(ctype, "application/json")
        return status, json.loads(data)


class PhotoTest(OwnerRoutes, tc.Base):
    def setUp(self):
        super().setUp()
        self.data = tc.make_jpeg()
        self.fid = self.upload(tc.ALICE, self.data)
        self.pid = self.only_photo()[0]

    def test_the_owner_deletes_it(self):
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE), (200, {"deleted": self.pid}))
        self.assertEqual((self.records(), self.ids()), ({}, []))
        self.assertFalse(os.path.exists(community.image_path(self.pid)))
        self.assertEqual(self.http("GET", "/community/img/%s.jpg" % self.pid)[0], 404)
        saved = tc.read_json(os.path.join(self.dir.name, "community.json"))
        self.assertEqual((saved["photos"], saved["deleted"]), ({}, []))
        self.assertEqual(self.download(tc.ALICE, self.fid), self.data)  # the player's own file stays
        # Unlike the operator's delete, the picture is the player's to share again.
        self.upload(tc.ALICE, self.data, name="AGAIN")
        self.assertEqual([p["title"] for p in self.records().values()], ["AGAIN"])
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE)[0], 404)

    def test_held_and_hidden_photos_too(self):
        self.admin("POST", "/%s/hide" % self.pid)
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE)[0], 200)
        self.assertEqual(self.records(), {})

    def test_another_players_photo_is_not_found(self):
        self.assertEqual(self.owner_delete("photos", self.pid, tc.BOB), (404, {"error": "no such photo"}))
        self.assertEqual(self.owner_delete("photos", "AAAAAAAAAAAA", tc.BOB), (404, {"error": "no such photo"}))
        self.assertIn(self.pid, self.records())

    def test_the_token(self):
        for token in (None, "", "wrong", OWNER_TOKEN + "x", tc.TOKEN):  # the admin token is not it
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, token=token)[0], 403, token)
        status, _, _, _ = self.http("POST", "/owner/photos/%s/delete?token=%s" % (self.pid, OWNER_TOKEN),
                                    {}, json.dumps({"owner": "%016X" % tc.ALICE}).encode())
        self.assertEqual(status, 403)
        with mock.patch.object(owners, "OWNER_TOKEN", ""):  # not set: the routes are off
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, token="")[0], 404)
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE)[0], 404)
        self.assertIn(self.pid, self.records())

    def test_never_through_the_tunnel_or_a_proxy(self):
        for proxy in ({"X-Forwarded-For": "203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
                      {"Forwarded": "for=203.0.113.9"}):
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, headers=proxy)[0], 403)
        limits.configure_proxies("192.168.1.20")  # the tunnel agent's address
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE)[0], 403)
        limits.configure_proxies()
        self.assertIn(self.pid, self.records())

    def test_browser_requests_are_refused(self):
        for origin in ("https://forza.dyotson.dev", "null", "http://192.168.1.20:21201"):
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, headers={"Origin": origin})[0], 403)
        self.assertIn(self.pid, self.records())

    def test_bad_requests(self):
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, method="GET")[0], 405)
        self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, method="DELETE")[0], 405)
        for body in (b"", b"[]", b"{}", b"not json", json.dumps({"owner": "%X" % tc.ALICE}).encode(),
                     json.dumps({"owner": tc.ALICE}).encode(), json.dumps({"owner": "g" * 16}).encode(),
                     json.dumps({"owner": "%016X" % tc.ALICE, "pad": "x" * 2000}).encode()):
            self.assertEqual(self.owner_delete("photos", self.pid, tc.ALICE, body=body)[0], 400, body[:40])
        for path in ("/owner/photos/%s" % self.pid, "/owner/photos/%s/hide" % self.pid,
                     "/owner/photos/%s/delete" % self.pid.lower(), "/owner/photos/../designs",
                     "/owner/", "/owner/cars/%s/delete" % self.pid):
            status = self.http("POST", path, {"X-Owner-Token": OWNER_TOKEN},
                               json.dumps({"owner": "%016X" % tc.ALICE}).encode())[0]
            self.assertEqual(status, 404, path)
        # the owner's id in lower case is the same XUID
        status, _ = self.owner_delete("photos", self.pid, tc.ALICE,
                                      body=json.dumps({"owner": "%016x" % tc.ALICE}).encode())
        self.assertEqual(status, 200)

    def test_the_token_reaches_nothing_else(self):
        token = {"X-Owner-Token": OWNER_TOKEN}
        self.assertEqual(self.http("GET", "/admin/photos", token)[0], 403)
        self.assertEqual(self.http("POST", "/admin/photos/%s/delete" % self.pid, token)[0], 403)
        self.assertEqual(self.http("POST", "/admin/photos/ban/%016X" % tc.BOB, token)[0], 403)
        self.assertEqual(self.http("GET", "/admin/designs", token)[0], 403)
        self.assertIn(self.pid, self.records())


class DesignTest(OwnerRoutes, td.Base):
    def setUp(self):
        super().setUp()
        self.fid = self.share(td.ALICE)
        self.release(self.fid)
        self.web_id = self.wid(self.fid)

    def test_the_owner_deletes_it(self):
        self.assertEqual(self.listed(), [self.web_id])
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE), (200, {"deleted": self.web_id}))
        self.assertNotIn(str(self.fid), designs.load()["files"])
        for suffix in (".bin", ".prev", ".png"):
            self.assertFalse(os.path.exists(designs.file_path(self.fid, suffix)))
        self.assertEqual(self.listed(), [])
        self.assertIsNone(self.purchase(td.EVE, self.fid))  # gone from the game too
        self.assertNotIn(str(self.fid), tc.read_json(os.path.join(self.dir.name, "designs.json"))["files"])
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE)[0], 404)

    def test_pending_and_hidden_designs_too(self):
        other = self.share(td.ALICE, title="SECOND")
        self.assertEqual(self.owner_delete("designs", self.wid(other), td.ALICE)[0], 200)
        self.admin("POST", "/%s/hide" % self.web_id)
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE)[0], 200)
        self.assertEqual(designs.load()["files"], {})

    def test_another_players_design_is_not_found(self):
        self.assertEqual(self.owner_delete("designs", self.web_id, td.BOB), (404, {"error": "no such design"}))
        self.assertEqual(self.listed(), [self.web_id])

    def test_refused_like_the_photos(self):
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE, token="wrong")[0], 403)
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE,
                                           headers={"X-Real-IP": "203.0.113.9"})[0], 403)
        self.assertEqual(self.owner_delete("designs", self.web_id, td.ALICE,
                                           headers={"Origin": "https://forza.dyotson.dev"})[0], 403)
        self.assertEqual(self.listed(), [self.web_id])


class HttpServerTest(OwnerRoutes, tc.Base, unittest.IsolatedAsyncioTestCase):
    """Through the server's HTTP handler: the route is lsp's, and a proxy header still refuses."""

    async def request(self, headers=()):
        server = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                            "127.0.0.1", 0)
        body = json.dumps({"owner": "%016X" % tc.ALICE}).encode()
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(("POST /owner/photos/%s/delete HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
                          "Content-Type: application/json\r\nContent-Length: %d\r\n%s\r\n"
                          % (self.pid, len(body), "".join(h + "\r\n" for h in headers))).encode() + body)
            raw = await reader.read()
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        return raw.split(b"\r\n\r\n", 1)[0].decode()

    async def test_on_the_wire(self):
        self.upload(tc.ALICE, tc.make_jpeg())
        self.pid = self.only_photo()[0]
        head = await self.request(["X-Owner-Token: " + OWNER_TOKEN, "X-Forwarded-For: 203.0.113.9"])
        self.assertIn("HTTP/1.1 403", head)
        head = await self.request(["X-Owner-Token: " + OWNER_TOKEN])
        self.assertIn("HTTP/1.1 200", head)
        self.assertEqual(self.records(), {})


if __name__ == "__main__":
    unittest.main()
