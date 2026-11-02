"""Content manifests (#567, forza_chat.py op 5): a game says which DLC offers and car mods it has,
the server tells the players of one session what they do not share (a car one game lacks is a
plain placeholder in the other). Drives the server's handlers directly (no sockets)."""

import struct
import unittest

import forza_chat as ch
import forza_live_server as srv

ALL = srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((addr, data))


def manifest(dlc=(), mods=()):
    return "dlc=%s;mods=%s" % (",".join(str(d) for d in dlc), ",".join(mods))


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(ch.parse_content("dlc=1100,729;mods=B,A"),
                         (frozenset({729, 1100}), frozenset({"A", "B"})))
        self.assertEqual(ch.parse_content("dlc=;mods="), (frozenset(), frozenset()))

    def test_malformed(self):
        for text in ("", "dlc=1", "mods=a", "dlc=x;mods=", "dlc=1;mods=;dlc=2", "dlc=1;mods=;x=1",
                     "dlc=" + ",".join(["1"] * 200) + ";mods=", "dlc=1234567890123;mods="):
            self.assertIsNone(ch.parse_content(text), text)

    def test_mod_names_are_cleaned(self):
        self.assertEqual(ch.parse_content("dlc=;mods=A\x1b[31m b")[1], frozenset({"A [31m b"}))

    def test_names(self):
        self.assertEqual(ch.content_names({3060, "UDLC"}), "DLC 3060, UDLC")
        self.assertEqual(ch.content_names({1, 2, 3, 4, 5}), "DLC 1, DLC 2, DLC 3 and 2 more")


class ContentTest(unittest.TestCase):
    def setUp(self):
        self.server = srv.ForzaLive("test", 64)
        self.transport = FakeTransport()
        self.server.transport = self.transport
        self.peers = [self.join(n) for n in range(1, 5)]
        self.transport.sent.clear()

    def join(self, n, features=ALL):
        body = struct.pack(">HQQIH", srv.VERSION, n, 0x0009000000000000 + n, 0, 0)
        tag = b"Driver%d" % n
        body += bytes([4]) + b"#TU4" + bytes([len(tag)]) + tag + bytes([features])
        body += bytes([16]) + bytes(15) + bytes([n])
        addr = ("127.0.0.1", 20000 + n)
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, addr)
        return self.server.by_addr.get(addr)

    def send(self, peer, body):
        self.server.datagram_received(srv.MAGIC + bytes([srv.CHAT]) + body, peer.addr)

    def sessions(self, peer, *ids):
        self.send(peer, struct.pack(">BB", ch.SESSIONS, len(ids)) +
                  b"".join(struct.pack(">Q", i) for i in ids))

    def content(self, peer, text):
        raw = text.encode()
        self.send(peer, bytes([ch.CONTENT]) + struct.pack(">H", len(raw)) + raw)

    def notices(self, peer):
        return [d[8:].decode() for addr, d in self.transport.sent
                if addr == peer.addr and d[4] == srv.CHAT and d[5] == ch.R_NOTICE]

    def test_missing_content_is_told_to_both_sides(self):
        a, b, c, _ = self.peers
        for p in (a, b, c):
            self.sessions(p, 7)
        self.content(a, manifest([1100], ["Traffic unlocker"]))
        self.content(b, manifest([1100]))
        self.assertEqual(self.notices(a), ["Driver2 does not have Traffic unlocker: your car may "
                                           "look like a plain placeholder to them"])
        self.assertEqual(self.notices(b), ["Driver1 has Traffic unlocker that you do not: their "
                                           "car may look like a plain placeholder to you"])
        self.assertEqual(self.notices(c), [])  # c sent no content: nothing to compare

    def test_same_content_says_nothing(self):
        a, b = self.peers[:2]
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.content(a, manifest([729, 1100], ["Car pack"]))
        self.content(b, manifest([1100, 729], ["Car pack"]))
        self.assertEqual(self.notices(a) + self.notices(b), [])

    def test_told_once(self):
        a, b = self.peers[:2]
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.content(a, manifest([3060]))
        self.content(b, manifest())
        first = len(self.transport.sent)
        self.assertEqual(first, 2)
        self.content(a, manifest([3060]))   # the game repeats it every 30 s
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.assertEqual(len(self.transport.sent), first)

    def test_other_session_is_not_compared(self):
        a, b = self.peers[:2]
        self.sessions(a, 7)
        self.sessions(b, 8)
        self.content(a, manifest([3060]))
        self.content(b, manifest())
        self.assertEqual(self.transport.sent, [])

    def test_joining_a_session_compares(self):
        a, b = self.peers[:2]
        self.content(a, manifest([3060]))
        self.content(b, manifest())
        self.assertEqual(self.transport.sent, [])   # in no session yet
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.assertEqual(len(self.notices(a)) + len(self.notices(b)), 2)

    def test_game_without_chat_gets_no_notice(self):
        a = self.peers[0]
        b = self.join(9, features=srv.FEATURE_INVITE_PICK)
        self.transport.sent.clear()
        self.sessions(a, 7)
        self.content(a, manifest([3060]))
        self.assertEqual(self.transport.sent, [])

    def test_changes_are_rate_limited(self):
        a, b = self.peers[:2]
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.content(b, manifest())
        self.content(a, manifest([3060]))
        self.transport.sent.clear()
        self.content(a, manifest([3060, 3061]))   # a second change at once is ignored
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(a.content[0], frozenset({3060}))

    def test_bad_content_is_ignored(self):
        a, b = self.peers[:2]
        self.sessions(a, 7)
        self.sessions(b, 7)
        self.content(b, manifest())
        self.content(a, "nonsense")
        self.send(a, bytes([ch.CONTENT]) + struct.pack(">H", 5000) + b"x" * 10)
        self.send(a, bytes([ch.CONTENT]))
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(hasattr(a, "content") and a.content)


if __name__ == "__main__":
    unittest.main()
