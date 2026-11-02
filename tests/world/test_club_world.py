"""Car clubs in the Horizon World (FH1 #145, club_world.py): placement prefers the worlds
with the searcher's online clubmates, /club lists them and /club join names a world; nothing
changes for players without a club or without a clubmate in an open world. Drives the server's
matchmaking handlers directly (no sockets)."""

import struct
import time
import unittest

from forza_live.social import chat as ch
from forza_live.world import club_world as cw
from forza_live.social import clubs
from forza_live.title_services import lsp
from forza_live import server as srv
from tests.support import FakeTransport, make_clubs, record

KEY = lsp.WORLD_HOPPER
S = lambda n: bytes([n]) * 8  # a session id


class ClubWorldTest(unittest.TestCase):
    def setUp(self):
        self.server = srv.ForzaLive("test", 64)
        self.transport = FakeTransport()
        self.server.transport = self.transport
        self.n = 0
        self.club = {}  # name -> club id

    def tearDown(self):
        clubs.CLUBS.__init__()

    def player(self, name, club=0, room="#TU4", proven=True):
        self.n += 1
        peer = srv.Peer(self.n, ("127.0.0.1", 20000 + self.n), self.n,
                        0x0009000000000000 + self.n, name, room, (0, 0))
        peer.friends_ok = proven
        peer.features = srv.FEATURE_CHAT
        self.server.peers[peer.id] = peer
        self.server.by_addr[peer.addr] = peer
        self.club[name] = club
        make_clubs(*[(p.xuid, self.club[p.name]) for p in self.server.peers.values()
                     if self.club[p.name]])
        return peer

    def publish(self, peer, sid, filled, key=KEY):
        self.server.on_match(peer, srv.MATCH_PUBLISH,
                             bytes([1]) + struct.pack(">I", key) + record(sid, filled))

    def standby(self, peer, sid, key=KEY):
        self.server.on_match(peer, srv.MATCH_PUBLISH,
                             bytes([2]) + struct.pack(">I", key) + record(sid, 1))

    def search(self, peer, key=KEY):
        """The (session id, filled slots) of the answer, in order."""
        self.transport.sent.clear()
        self.server.on_match(peer, srv.MATCH_SEARCH, struct.pack(">II", key, 7))
        found = []
        for addr, data in self.transport.sent:
            if addr != peer.addr or data[4] != srv.MATCH_RESULTS:
                continue
            body = data[5:]
            count = struct.unpack_from(">H", body, 4)[0] & 0x7FFF
            pos = 6
            for _ in range(count):
                _, _, length = struct.unpack_from(">IQH", body, pos)
                rec = body[pos + 14:pos + 14 + length]
                found.append((rec[:8], rec[srv.SESSION_INFO_SIZE + 2]))
                pos += 14 + length
        return found

    def command(self, peer, text, channel=ch.SESSION):
        self.transport.sent.clear()
        self.assertTrue(self.server.chat_command(peer, channel, text))
        return [d[8:].decode() for addr, d in self.transport.sent
                if addr == peer.addr and d[4] == srv.CHAT and d[5] == ch.R_NOTICE]

    # --- placement ---

    def test_without_a_club_the_fullest_world_is_first_as_before(self):
        s = self.player("S")
        h1, h2 = self.player("H1", 1), self.player("H2", 1)
        self.publish(h1, S(1), 2)
        self.publish(h2, S(2), 5)
        self.assertEqual(self.search(s), [(S(2), 5), (S(1), 2)])

    def test_the_world_with_a_clubmate_comes_before_a_fuller_one(self):
        s, h1, h2 = self.player("S", 1), self.player("H1"), self.player("H2", 1)
        self.publish(h1, S(1), 5)
        self.publish(h2, S(2), 2)
        self.assertEqual(self.search(s), [(S(2), 2), (S(1), 5)])

    def test_a_clubmate_who_joined_a_world_counts_too(self):
        s, h1, h2, j = (self.player("S", 1), self.player("H1"), self.player("H2"),
                        self.player("J", 1))
        self.publish(h1, S(1), 6)
        self.publish(h2, S(2), 2)
        self.standby(j, S(2))
        self.assertEqual(self.search(s), [(S(2), 2), (S(1), 6)])

    def test_the_world_with_most_clubmates_first_then_the_fullest(self):
        s = self.player("S", 1)
        a, b, c = self.player("A", 1), self.player("B", 1), self.player("C", 1)
        h, i = self.player("H"), self.player("I")
        self.publish(a, S(1), 2)      # one clubmate
        self.publish(b, S(2), 3)      # two: B hosts, C joined
        self.standby(c, S(2))
        self.publish(h, S(3), 7)      # none, the fullest
        self.publish(i, S(4), 4)      # none
        self.assertEqual(self.search(s), [(S(2), 3), (S(1), 2), (S(3), 7), (S(4), 4)])

    def test_a_full_world_of_clubmates_is_not_offered(self):
        s, a, h = self.player("S", 1), self.player("A", 1), self.player("H")
        self.publish(a, S(1), 8)
        self.publish(h, S(2), 3)
        self.assertEqual(self.search(s), [(S(2), 3)])

    def test_blocked_unproven_and_other_club_players_are_no_clubmates(self):
        s = self.player("S", 1)
        blocked, unproven, other, plain = (self.player("Bl", 1), self.player("Un", 1, proven=False),
                                           self.player("Ot", 2), self.player("Pl"))
        self.publish(plain, S(4), 4)
        for peer, n in ((blocked, 1), (unproven, 2), (other, 3)):
            self.publish(peer, S(n), 2)
        self.server.friends.store.block(s.xuid, blocked.xuid)
        self.assertEqual(self.search(s)[0], (S(4), 4))
        self.server.friends.store.unblock(s.xuid, blocked.xuid)
        self.assertEqual(self.search(s)[0], (S(1), 2))  # now a clubmate
        self.server.friends.store.block(blocked.xuid, s.xuid)  # blocked either way
        self.assertEqual(self.search(s)[0], (S(4), 4))

    def test_an_unproven_searcher_gets_the_old_order(self):
        s, a, h = self.player("S", 1, proven=False), self.player("A", 1), self.player("H")
        self.publish(a, S(1), 2)
        self.publish(h, S(2), 5)
        self.assertEqual(self.search(s), [(S(2), 5), (S(1), 2)])

    def test_other_playlists_are_not_reordered(self):
        s = self.player("S", 1)
        a, h = self.player("A", 1), self.player("H")
        c, d = self.player("C", 1), self.player("D")
        self.publish(a, S(1), 2, 4)
        self.publish(h, S(2), 5, 4)
        self.publish(c, S(3), 2, lsp.COOP_HOPPER)
        self.publish(d, S(4), 5, lsp.COOP_HOPPER)
        self.assertEqual(self.search(s, 4), [(S(2), 5), (S(1), 2)])  # a custom game type
        self.assertEqual(self.search(s, lsp.COOP_HOPPER), [])  # friends only, as before

    def test_a_clubmate_in_another_room_changes_nothing(self):
        s, a, h = self.player("S", 1), self.player("A", 1, room="other"), self.player("H")
        self.publish(a, S(1), 2)
        self.publish(h, S(2), 5)
        self.assertEqual(self.search(s), [(S(2), 5)])

    # --- /club ---

    def test_club_lists_the_clubmates_and_where_they_are(self):
        s = self.player("S", 1)
        a, b, c, d, e = (self.player("Alice", 1), self.player("Bob", 1), self.player("Cara", 1),
                         self.player("Dan", 1, room="other"), self.player("Eve", 2))
        self.publish(a, S(1), 3)
        self.publish(b, S(2), 8)
        self.assertEqual(self.command(s, "/club"), [
            "[TST] 4 of 5 members online:", "  Alice: in a Horizon World (3/8)",
            "  Bob: in a Horizon World (8/8, full)", "  Cara: online",
            "  Dan: online in another room"])
        self.assertEqual(self.command(s, "/club who", ch.WORLD)[0], "[TST] 4 of 5 members online:")

    def test_club_lists_at_most_a_few(self):
        s = self.player("S", 1)
        for i in range(cw.MAX_LISTED + 3):
            self.player("M%02d" % i, 1)
        lines = self.command(s, "/club")
        self.assertEqual((len(lines), lines[-1]), (cw.MAX_LISTED + 2, "  and 3 more"))

    def test_club_alone_and_without_a_club(self):
        s, f = self.player("S", 1), self.player("F")
        self.assertEqual(self.command(s, "/club"), ["[TST] No other club member is online (1 members)"])
        self.assertEqual(self.command(f, "/club"), [ch.NO_CLUB])
        self.assertEqual(self.command(f, "/club join"), [ch.NO_CLUB])
        self.assertEqual(self.command(s, "/club nonsense"), [cw.USAGE])

    def test_club_join_names_the_world_for_the_next_search(self):
        s, a, b, h = self.player("S", 1), self.player("Alice", 1), self.player("Bob", 1), self.player("H")
        self.publish(a, S(1), 2)
        self.publish(b, S(2), 5)
        self.publish(h, S(3), 7)
        self.assertEqual(self.search(s), [(S(2), 5), (S(1), 2), (S(3), 7)])
        notice = self.command(s, "/club join alice")
        self.assertEqual(notice, ["Open Find Game > Horizon World to join Alice (2/8)"])
        self.assertEqual(self.search(s), [(S(1), 2), (S(2), 5), (S(3), 7)])
        self.assertEqual(self.command(s, "/club join Bob"),
                         ["Open Find Game > Horizon World to join Bob (5/8)"])
        self.assertEqual(self.search(s)[0], (S(2), 5))

    def test_club_join_without_a_name_picks_the_world_with_most_clubmates(self):
        s, a, b, c = self.player("S", 1), self.player("A", 1), self.player("B", 1), self.player("C", 1)
        self.publish(a, S(1), 2)
        self.publish(b, S(2), 3)
        self.standby(c, S(2))
        self.assertEqual(self.command(s, "/club join"),
                         ["Open Find Game > Horizon World to join B, C (3/8)"])
        self.assertEqual(self.search(s)[0], (S(2), 3))

    def test_club_join_expires(self):
        s, a, b = self.player("S", 1), self.player("Alice", 1), self.player("Bob", 1)
        self.publish(a, S(1), 2)
        self.publish(b, S(2), 5)
        self.command(s, "/club join Alice")
        sid, until = s.club_join
        self.assertAlmostEqual(until - time.monotonic(), cw.JOIN_SECONDS, delta=5)
        self.assertEqual(self.search(s)[0], (S(1), 2))
        s.club_join = (sid, time.monotonic() - 1)
        self.assertEqual(self.search(s)[0], (S(2), 5))  # back to the fullest clubmate world

    def test_club_join_refusals(self):
        s, a, b, f = (self.player("S", 1), self.player("Alice", 1), self.player("Bob", 1),
                      self.player("Fay", 2))
        self.assertEqual(self.command(s, "/club join"),
                         ["None of your clubmates is in a Horizon World with room "
                          "(/club shows where they are)"])
        self.assertEqual(self.command(s, "/club join Fay"),
                         ["Fay is not a club member who is online in your room (/club lists them)"])
        self.assertEqual(self.command(s, "/club join Nobody")[0][:6], "Nobody")
        self.publish(a, S(1), 8)  # full
        self.assertEqual(self.command(s, "/club join Alice"),
                         ["Alice is not in a Horizon World with room (/club shows where they are)"])
        self.assertIsNone(getattr(s, "club_join", None))
        self.publish(b, S(2), 2)
        self.standby(s, S(2))
        self.assertEqual(self.command(s, "/club join Bob"), ["You are already in Bob's world"])

    def test_club_join_from_a_world_of_your_own_says_to_leave_it(self):
        s, a = self.player("S", 1), self.player("Alice", 1)
        self.publish(s, S(9), 1)  # S hosts a world alone
        self.publish(a, S(1), 3)
        self.assertEqual(self.command(s, "/club join Alice"), [
            "Open Find Game > Horizon World to join Alice (3/8); leave your current world first"])


if __name__ == "__main__":
    unittest.main()
