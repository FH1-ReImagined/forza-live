"""The club chat channel (FH1 #145, forza_chat.py): every other member of the sender's car club
who is online and knows the channel, wherever they are; the same limits and moderation as the
other channels; the club tag the game is told. Drives the server's handlers directly (no
sockets)."""

import json
import os
import struct
import tempfile
import time
import unittest

import forza_chat as ch
import forza_clubs as clubs
import forza_live_lsp as lsp
import forza_live_server as srv

ALL = srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((addr, data))


def make_clubs(*members):
    """clubs.json in memory: club 1 TST and club 2 OTH, members = [(xuid, club id)]."""
    clubs.CLUBS.__init__()
    clubs.CLUBS.loaded = True
    now = time.time()
    clubs.CLUBS.data["clubs"] = {
        str(i): {"id": i, "tag": tag, "name": tag + " club", "description": "", "open": True,
                 "created": now, "requests": []} for i, tag in ((1, "TST"), (2, "OTH"))}
    clubs.CLUBS.data["members"] = {
        "%016X" % x: {"club": c, "role": clubs.MEMBER, "since": now} for x, c in members}


class ClubChatTest(unittest.TestCase):
    def setUp(self):
        self.server = srv.ForzaLive("test", 64)
        self.transport = FakeTransport()
        self.server.transport = self.transport
        self.peers = [self.join(n) for n in range(1, 7)]
        self.a, self.b, self.c, self.d, self.e, self.f = self.peers
        # A, B, C, E are in TST; D in OTH; F has no club
        make_clubs(*[(p.xuid, club) for p, club in
                     ((self.a, 1), (self.b, 1), (self.c, 1), (self.d, 2), (self.e, 1))])
        for p in self.peers:
            self.send(p, bytes([ch.CLUB_SYNC]))  # these games know the channel
        self.transport.sent.clear()

    def tearDown(self):
        clubs.CLUBS.__init__()

    def join(self, n, features=ALL, room="#TU4"):
        body = struct.pack(">HQQIH", srv.VERSION, n, 0x0009000000000000 + n, 0, 0)
        tag = b"Driver%d" % n
        r = room.encode()
        body += bytes([len(r)]) + r + bytes([len(tag)]) + tag + bytes([features])
        body += bytes([16]) + bytes(15) + bytes([n])
        addr = ("127.0.0.1", 20000 + n)
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, addr)
        return self.server.by_addr.get(addr)

    def send(self, peer, body):
        self.server.datagram_received(srv.MAGIC + bytes([srv.CHAT]) + body, peer.addr)

    def say(self, peer, channel, text, target=0):
        self.transport.sent.clear()
        self.send(peer, struct.pack(">BBQ", ch.SAY, channel, target) + ch.text16(text))

    def messages(self, peer):
        out = []
        for addr, d in self.transport.sent:
            if addr == peer.addr and d[4] == srv.CHAT and d[5] == ch.R_MESSAGE:
                channel, xuid, n = struct.unpack_from(">BQB", d, 6)
                name = d[16:16 + n].decode()
                (size,) = struct.unpack_from(">H", d, 16 + n)
                out.append((channel, name, d[18 + n:18 + n + size].decode()))
        return out

    def notices(self, peer):
        return [d[8:].decode() for addr, d in self.transport.sent
                if addr == peer.addr and d[4] == srv.CHAT and d[5] == ch.R_NOTICE]

    def tags(self, peer):
        return [d[7:7 + d[6]].decode() for addr, d in self.transport.sent
                if addr == peer.addr and d[4] == srv.CHAT and d[5] == ch.R_CLUBTAG]

    def test_goes_to_the_online_clubmates_only(self):
        self.say(self.a, ch.CLUB, "hello club")
        self.assertEqual(self.messages(self.b), [(ch.CLUB, "Driver1", "hello club")])
        self.assertEqual(self.messages(self.c), [(ch.CLUB, "Driver1", "hello club")])
        self.assertEqual(self.messages(self.e), [(ch.CLUB, "Driver1", "hello club")])
        for other in (self.a, self.d, self.f):  # the sender, another club, no club
            self.assertEqual(self.messages(other), [])
        self.assertEqual(self.notices(self.a), [])

    def test_wherever_they_are(self):
        # a clubmate in another room, one in a Horizon World, one hosting a session: all get it
        self.server.drop(self.c, "left")
        c = self.join(3, room="private")
        self.send(c, bytes([ch.CLUB_SYNC]))
        sid = bytes.fromhex("00000000000000AA")
        self.b.match = (lsp.WORLD_HOPPER, sid + bytes(100), 0)
        self.send(self.e, struct.pack(">BBQ", ch.SESSIONS, 1, 0x1234))
        self.say(self.a, ch.CLUB, "where are you")
        for p in (self.b, c, self.e):
            self.assertEqual(self.messages(p), [(ch.CLUB, "Driver1", "where are you")])

    def test_offline_clubmates_and_a_lone_member(self):
        for p in (self.b, self.c, self.e):
            self.server.drop(p, "left")
        self.say(self.a, ch.CLUB, "anyone?")
        self.assertEqual(self.notices(self.a), [ch.NO_CLUBMATE])

    def test_not_in_a_club(self):
        self.say(self.f, ch.CLUB, "hi")
        self.assertEqual(self.notices(self.f), [ch.NO_CLUB])
        self.assertEqual(sum(len(self.messages(p)) for p in self.peers), 0)

    def test_leaving_the_club_ends_the_messages(self):
        clubs.leave(self.b.xuid)
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(self.b), [])
        self.assertEqual(len(self.messages(self.c)), 1)
        self.say(self.b, ch.CLUB, "bye")
        self.assertEqual(self.notices(self.b), [ch.NO_CLUB])

    def test_a_game_without_club_chat_gets_nothing(self):
        old = self.join(9)  # a game that never asked for the club tag
        clubs.CLUBS.data["members"]["%016X" % old.xuid] = {"club": 1, "role": 3, "since": 0}
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(old), [])
        self.assertEqual(len(self.messages(self.b)), 1)
        self.say(old, ch.CLUB, "from an old game")  # it can still talk to those that listen
        self.assertEqual(len(self.messages(self.b)), 1)

    def test_a_game_without_chat_gets_nothing(self):
        old = self.join(9, features=srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS)
        clubs.CLUBS.data["members"]["%016X" % old.xuid] = {"club": 1, "role": 3, "since": 0}
        self.send(old, bytes([ch.CLUB_SYNC]))  # ignored: no chat feature
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(old), [])
        self.assertEqual(self.tags(old), [])

    def test_unproven_xuid_cannot_chat_to_a_club(self):
        self.a.friends_ok = False
        self.say(self.a, ch.CLUB, "hi")
        self.assertTrue(self.notices(self.a)[0].startswith("Chat is off"))
        self.assertEqual(self.messages(self.b), [])
        self.b.friends_ok = False  # and does not hear it either
        self.a.friends_ok = True
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(self.b), [])
        self.assertEqual(len(self.messages(self.c)), 1)

    def test_blocks_and_mutes_hide_club_messages(self):
        self.server.friends.store.block(self.b.xuid, self.a.xuid)
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual((self.messages(self.b), len(self.messages(self.c))), ([], 1))
        self.say(self.b, ch.CLUB, "hi")  # a blocker doesn't reach the blocked either
        self.assertEqual((self.messages(self.a), len(self.messages(self.c))), ([], 1))
        self.send(self.c, struct.pack(">BQB", ch.MUTE, self.a.xuid, 1))
        self.say(self.a, ch.CLUB, "again")
        self.assertEqual((self.messages(self.c), len(self.messages(self.e))), ([], 1))

    def test_admin_mute_rate_limit_and_text(self):
        self.server.chat.admin("mute", "Driver1", 10)
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(self.b), [])
        self.assertIn("muted you", self.notices(self.a)[0])
        self.server.chat.admin("mute", "Driver1", 0)
        got = 0
        for i in range(8):
            self.say(self.a, ch.CLUB, "spam %d" % i)
            got += len(self.messages(self.b))
        self.assertEqual(got, ch.RATE[1])
        self.assertTrue(self.notices(self.a)[0].startswith("Slow down"))
        self.server.chat.buckets.clear()
        self.say(self.a, ch.CLUB, "x‮" + "y" * 300)
        self.assertEqual(len(self.messages(self.b)[0][2]), ch.MAX_CHARS)

    def test_a_banned_player_is_gone_from_the_club_channel(self):
        self.server.chat.admin("kick", "Driver2", 5)
        self.say(self.a, ch.CLUB, "hi")
        self.assertEqual(self.messages(self.b), [])
        self.assertEqual(len(self.messages(self.c)), 1)

    def test_ring_keeps_the_club_messages_in_memory_only(self):
        self.say(self.a, ch.CLUB, "for the club")
        entry = self.server.chat.moderation()["messages"][-1]
        self.assertEqual((entry["channel"], entry["club"], entry["text"], entry["from"]),
                         ("club", "TST", "for the club", "Driver1"))
        self.assertEqual(sorted(entry["to"]), ["Driver2", "Driver3", "Driver5"])
        for i in range(ch.RING + 5):
            self.server.chat.buckets.clear()
            self.say(self.a, ch.CLUB, "m%d" % i)
        self.assertEqual(len(self.server.chat.moderation()["messages"]), ch.RING)
        with tempfile.TemporaryDirectory() as tmp:  # moderation.json holds mutes and bans only
            self.server.chat.path = os.path.join(tmp, ch.FILE)
            self.server.chat.admin("mute", "Driver4", 5)
            with open(self.server.chat.path) as f:
                saved = f.read()
            self.assertEqual(sorted(json.loads(saved)), ["bans", "mutes"])
            self.assertNotIn("for the club", saved)

    def test_club_tag_is_sent_when_it_changes(self):
        p = self.a
        self.transport.sent.clear()
        self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(p), [])  # told at the first sync (setUp), same now
        p.club_told = None  # a new connection
        self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(p), ["TST"])
        self.transport.sent.clear()
        self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(p), [])
        clubs.leave(p.xuid)
        self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(p), [""])  # no club
        clubs.join(p.xuid, clubs.CLUBS.data["clubs"]["2"])
        self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(p), ["", "OTH"])

    def test_a_new_connection_is_told_the_tag_of_its_player(self):
        self.transport.sent.clear()
        for p in self.peers:
            p.club_told = None
            self.send(p, bytes([ch.CLUB_SYNC]))
        self.assertEqual([self.tags(p) for p in self.peers],
                         [["TST"], ["TST"], ["TST"], ["OTH"], ["TST"], [""]])

    def test_unproven_player_is_told_no_club(self):
        self.a.friends_ok = False
        self.a.club_told = None
        self.transport.sent.clear()
        self.send(self.a, bytes([ch.CLUB_SYNC]))
        self.assertEqual(self.tags(self.a), [""])

    def test_other_channels_are_unchanged(self):
        self.send(self.a, struct.pack(">BB", ch.SESSIONS, 1) + struct.pack(">Q", 7))
        self.send(self.d, struct.pack(">BB", ch.SESSIONS, 1) + struct.pack(">Q", 7))
        self.say(self.a, ch.SESSION, "session hello")
        self.assertEqual(self.messages(self.d), [(ch.SESSION, "Driver1", "session hello")])
        self.assertEqual(self.messages(self.b), [])  # the club doesn't hear session chat


if __name__ == "__main__":
    unittest.main()
