"""The website's players-online feed (online.py, GET /community/online.json): its counts with
no, one and several players, proxied players, probe registrations, races and matches, the
fields it never has, its headers and methods. Drives the server's matchmaking handlers
directly (no sockets).

Run: python3 -m unittest discover -p 'test_*.py'
"""

import builtins
import json
import struct
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.content import online
from forza_live.title_services import lsp
from forza_live.world import world
from tests.support import FakeTransport

RACES, PLAYGROUND, POINT_TO_POINT, FREE_ROAM = 1, 2, 3, 8
PEER = ("203.0.113.9", 40000)  # a visitor's browser (through the website's pass-through)


def record(session_id, filled=1, game_type=None):
    """A session record: XSESSION_INFO (the session id first), the public slots, the custom
    game type (property 0x10000030) when given, no contexts, no QoS data."""
    info = bytearray(srv.SESSION_INFO_SIZE)
    info[:8] = session_id
    props = bytes([0]) if game_type is None else (
        bytes([1]) + struct.pack(">IB", 0x10000030, 1) + struct.pack(">I", game_type) + bytes(4))
    return bytes(info) + bytes([8, 0, filled, 0]) + props + bytes([0]) + struct.pack(">H", 0)


class OnlineFeedTest(unittest.TestCase):
    def setUp(self):
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = FakeTransport()
        self.n = 0
        online._memo.update(server=None, at=0.0, body=b"")
        self.addCleanup(online._memo.update, server=None, at=0.0, body=b"")
        racing = set(world.WORLDS.racing)
        self.addCleanup(lambda: (world.WORLDS.racing.clear(), world.WORLDS.racing.update(racing)))
        world.WORLDS.racing.clear()

    def player(self, name, room="#TU4", proxied=False):
        self.n += 1
        peer = srv.Peer(self.n, ("198.51.100.%d" % self.n, 20000 + self.n), self.n,
                        0x0009000000000000 + self.n, name, room, (0, 0))
        peer.proxied = proxied
        self.server.peers[peer.id] = peer
        self.server.by_addr[peer.addr] = peer
        return peer

    def host(self, peer, sid, key, filled=1, game_type=None):
        body = bytes([1]) + struct.pack(">I", key) + record(sid, filled, game_type)
        self.server.on_match(peer, srv.MATCH_PUBLISH, body)

    def join(self, peer, sid, key, game_type=None):
        """The player's standby record of a session someone else hosts."""
        body = bytes([2]) + struct.pack(">I", key) + record(sid, 1, game_type)
        self.server.on_match(peer, srv.MATCH_PUBLISH, body)

    def http(self, method, path=online.PATH):
        answer = lsp.handle(self.server, method, path, {}, b"", PEER)
        status, ctype, data = answer[:3]
        extra = dict(line.split(": ", 1) for line in (answer[3] if len(answer) > 3 else ()))
        return status, ctype, data, extra

    def feed(self):
        online._memo.update(server=None)  # counted now
        status, ctype, data, _ = self.http("GET")
        self.assertEqual((status, ctype), (200, "application/json"))
        body = json.loads(data)
        self.assertEqual(set(body), {"version", "generated", "players", "worlds", "racing"})
        self.assertEqual(body["version"], 1)
        self.assertIsInstance(body["generated"], int)
        return body["players"], body["worlds"], body["racing"]

    def test_nobody_online(self):
        self.assertEqual(self.feed(), (0, 0, 0))

    def test_one_player(self):
        self.player("Alice")  # single player or the menus: online, in no session
        self.assertEqual(self.feed(), (1, 0, 0))
        self.host(self.server.peers[1], b"world001", lsp.WORLD_HOPPER)
        self.assertEqual(self.feed(), (1, 1, 0))

    def test_several_players_and_worlds(self):
        a, b, c, d, e = (self.player(n) for n in "ABCDE")
        self.host(a, b"world001", lsp.WORLD_HOPPER, filled=3)
        self.join(b, b"world001", lsp.WORLD_HOPPER)
        self.join(c, b"world001", lsp.WORLD_HOPPER)
        self.host(d, b"world002", lsp.WORLD_HOPPER)
        self.assertEqual(self.feed(), (5, 2, 0))  # e: single player
        del self.server.peers[a.id]  # world001's host left: its members keep it
        self.assertEqual(self.feed(), (4, 2, 0))
        self.host(e, b"coop0001", lsp.COOP_HOPPER)  # a Co-op Festival world: friends only
        self.assertEqual(self.feed(), (4, 2, 0))

    def test_proxied_players_count(self):
        a = self.player("Tunnelled", proxied=True)
        b = self.player("AlsoTunnelled", proxied=True)
        self.player("Direct")
        self.host(a, b"world001", lsp.WORLD_HOPPER, filled=2)
        self.join(b, b"world001", lsp.WORLD_HOPPER)
        self.assertEqual(self.feed(), (3, 1, 0))

    def test_probe_registrations_are_not_players(self):
        probe = self.player("Probe", room="probe")  # tools/server_probe.py
        self.host(probe, b"world001", lsp.WORLD_HOPPER)  # a world with only the probe in it
        self.assertEqual(self.feed(), (0, 0, 0))
        self.host(self.player("Alice"), b"world002", lsp.WORLD_HOPPER)
        self.assertEqual(self.feed(), (1, 1, 0))

    def test_players_in_races_and_matches(self):
        a, b, c, d, e, f, g, h = (self.player(n) for n in "ABCDEFGH")
        self.host(a, b"races001", RACES, filled=2)                      # Races lobby
        self.join(b, b"races001", RACES)
        self.host(c, b"play0001", PLAYGROUND)                           # Playground Games
        self.host(d, b"p2p00001", POINT_TO_POINT, game_type=POINT_TO_POINT)  # custom race (key 3)
        self.host(e, b"roam0001", FREE_ROAM, game_type=FREE_ROAM)       # custom Free Roam
        self.host(f, b"world001", lsp.WORLD_HOPPER, filled=2)           # a world
        self.join(g, b"world001", lsp.WORLD_HOPPER)
        self.host(h, b"world002", lsp.WORLD_HOPPER)
        self.assertEqual(self.feed(), (8, 2, 4))  # the custom race is not a world
        world.WORLDS.racing.add(b"world001")  # its event round started: f and g race
        self.assertEqual(self.feed(), (8, 2, 6))
        world.WORLDS.racing.discard(b"world001")  # back in the free roam
        self.assertEqual(self.feed(), (8, 2, 4))

    def test_racing_is_never_more_than_the_players(self):
        a = self.player("A")
        self.host(a, b"races001", RACES, filled=8)  # its record says 8, one game is here
        self.assertEqual(self.feed(), (1, 0, 1))

    def test_no_player_data_in_the_feed(self):
        a = self.player("SecretTag", proxied=True)
        self.host(a, b"world001", lsp.WORLD_HOPPER)
        self.host(self.player("OtherTag"), b"races001", RACES)
        self.feed()
        data = self.http("GET")[2].decode()
        for secret in ("SecretTag", "OtherTag", "%016X" % a.xuid, "%x" % a.xuid,
                       b"world001".hex(), b"races001".hex(), "198.51.100", "gamertag", "xuid",
                       "session", "id\""):
            self.assertNotIn(secret, data)

    def test_headers_and_methods(self):
        status, _, _, extra = self.http("GET")
        self.assertEqual(status, 200)
        self.assertEqual(extra["Cache-Control"], "public, max-age=15")
        self.assertEqual(extra["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.http("GET", online.PATH + "?t=1")[0], 200)
        for method in ("HEAD", "POST", "PUT", "DELETE"):
            self.assertEqual(self.http(method)[0], 405, method)
        # the other community feeds answer HEAD the same way
        self.assertEqual(self.http("HEAD", "/community/gallery.json")[0], 405)

    def test_through_the_http_response(self):
        status, ctype, data, extra = self.http("GET")
        raw = srv.response(status, data, ctype, tuple("%s: %s" % kv for kv in extra.items()))
        head = raw.split(b"\r\n\r\n", 1)[0].decode()
        self.assertIn("HTTP/1.1 200 OK", head)
        self.assertIn("Cache-Control: public, max-age=15", head)
        self.assertIn("Content-Type: application/json", head)

    def test_counted_from_memory_at_most_every_few_seconds(self):
        self.player("Alice")
        with mock.patch.object(builtins, "open", side_effect=AssertionError("disk I/O")):
            first = online.feed(self.server, lsp.WORLD_HOPPERS, now=100.0)
            self.player("Bob")
            self.assertEqual(online.feed(self.server, lsp.WORLD_HOPPERS, now=103.0), first)
            later = online.feed(self.server, lsp.WORLD_HOPPERS, now=100.0 + online.MEMO_SECONDS)
        self.assertEqual((json.loads(first)["players"], json.loads(later)["players"]), (1, 2))
        other = srv.ForzaLive("test", 64)  # another server (tests): its own counts
        self.assertEqual(json.loads(online.feed(other, lsp.WORLD_HOPPERS, now=101.0))["players"], 0)

    def test_without_a_server(self):
        self.assertEqual(json.loads(online.feed(None, lsp.WORLD_HOPPERS))["players"], 0)


if __name__ == "__main__":
    unittest.main()
