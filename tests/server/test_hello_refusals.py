"""Why a game cannot sign in, for its player and for the operator: a refused HELLO (codes 2-6, a
malformed one) is logged at info once per address and reason in a while; a HELLO over the
per-address rate gets error 6 instead of silence; a kicked or banned game's other packets get
error 4 with the reason, not "not registered". Drives the datagram handler directly (no
sockets)."""

import struct
import tempfile
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.core import bans
from tests.support import FakeTransport, hello, welcome_id

A = ("203.0.113.1", 3074)
B = ("203.0.113.2", 3074)
XUID = 0x0009000000000001


def list_packet():
    return srv.MAGIC + bytes([srv.LIST])


class Base(unittest.TestCase):
    def setUp(self):
        self.server = srv.ForzaLive("test", 64)
        self.transport = FakeTransport()
        self.server.transport = self.transport
        self.addCleanup(self.server.events.db.close)

    def errors(self, addr):
        """(code, text) of every ERROR sent to addr."""
        out = []
        for a, d in self.transport.sent:
            if a == addr and d[4] == srv.ERROR:
                code, n = struct.unpack_from(">BH", d, 5)
                out.append((code, d[8:8 + n].decode()))
        return out

    def send(self, data, addr=A):
        self.server.datagram_received(data, addr)

    def refusal_lines(self, logs):
        return [line for line in logs.output if "refused HELLO" in line]


class LoggedTest(Base):
    def test_a_refusal_is_logged_once_per_address_and_reason(self):
        old = hello(1, XUID, "Old")
        old = old[:5] + struct.pack(">H", srv.VERSION + 1) + old[7:]
        with self.assertLogs("forza-live", "INFO") as logs:
            for _ in range(5):
                self.send(old)
            self.send(old, B)
        lines = self.refusal_lines(logs)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("INFO:"))
        self.assertIn("203.0.113.1:3074 xuid 0009000000000001 'Old'", lines[0])
        self.assertIn("error 2", lines[0])
        self.assertEqual(self.errors(A)[0][0], 2)

    def test_logged_again_after_a_while(self):
        old = hello(1, XUID, "Old")
        old = old[:5] + struct.pack(">H", 0) + old[7:]
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(old)
            for key in self.server.refusals_logged:
                self.server.refusals_logged[key] -= srv.REFUSAL_LOG_SECONDS + 1
            self.send(old)
        self.assertEqual(len(self.refusal_lines(logs)), 2)

    def test_malformed_hello(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(srv.MAGIC + bytes([srv.HELLO]) + b"\x00\x01short")
            self.send(srv.MAGIC + bytes([srv.HELLO]) + b"\x00\x01short")
        lines = self.refusal_lines(logs)
        self.assertEqual(len(lines), 1)
        self.assertIn("malformed (7 bytes)", lines[0])
        self.assertEqual(self.server.peers, {})

    def test_event_unlocks_required(self):
        self.server.events.required = 1
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(hello(1, XUID, "NoEvents"))
        self.assertIn("error 5", self.refusal_lines(logs)[0])
        self.assertEqual(self.errors(A)[0][0], 5)

    def test_server_full(self):
        self.server.max_peers = 1
        self.send(hello(1, XUID, "First"), B)
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(hello(2, XUID + 1, "Second"))
        self.assertIn("server full (error 3)", self.refusal_lines(logs)[0])
        self.assertEqual(self.errors(A), [(3, "server full")])

    def test_a_welcome_logs_no_refusal(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(hello(1, XUID, "Fine"))
        self.assertEqual(self.refusal_lines(logs), [])
        self.assertIsNotNone(welcome_id(self.transport, A))


class RateLimitTest(Base):
    def test_over_the_hello_rate_is_told(self):
        burst = 5  # under MAX_PEERS_PER_IP: the rate, not the cap, refuses the rest
        with mock.patch.object(srv, "HELLOS_PER_IP", (0.001, burst)), \
                self.assertLogs("forza-live", "INFO") as logs:
            for i in range(burst + 3):
                self.send(hello(100 + i, XUID + i, "P%d" % i), ("203.0.113.1", 4000 + i))
        self.assertEqual(len(self.server.peers), burst)
        told = [self.errors(("203.0.113.1", 4000 + i)) for i in range(burst, burst + 3)]
        self.assertEqual([t[0][0] for t in told], [srv.ERR_SLOW_DOWN] * 3)
        self.assertNotEqual(srv.ERR_SLOW_DOWN, 1)  # 1 makes the game say HELLO again at once
        lines = self.refusal_lines(logs)
        self.assertEqual(len(lines), 1, lines)  # one address (the IP): logged once
        self.assertIn("too many sign-ins", lines[0])
        self.assertIn("error 6", lines[0])

    def test_the_reply_is_itself_rate_limited(self):
        with mock.patch.object(srv, "HELLOS_PER_IP", (0.001, 1)):
            for i in range(40):
                self.send(hello(100 + i, XUID + i, "P%d" % i), ("203.0.113.1", 4000 + i))
        sent = sum(len(self.errors(("203.0.113.1", 4000 + i))) for i in range(40))
        self.assertEqual(sent, srv.ERRORS_PER_IP[1])


class RemovedTest(Base):
    def join(self, addr=A, xuid=XUID, token=1):
        self.send(hello(token, xuid, "Player"), addr)
        return self.server.by_addr.get(addr)

    def test_a_kicked_game_s_packets_get_error_4(self):
        self.assertIsNotNone(self.join())
        self.server.chat.admin("kick", "%016X" % XUID, 10)
        self.assertEqual(self.server.peers, {})
        self.transport.sent.clear()
        self.send(list_packet())
        self.assertEqual(self.errors(A), [(4, "removed by the server admin")])
        self.send(list_packet(), B)  # someone else: not registered, as before
        self.assertEqual(self.errors(B), [(1, "not registered")])

    def test_a_kicked_game_s_hello(self):
        self.server.chat.admin("kick", "%016X" % XUID, 10)  # kicked while offline
        with self.assertLogs("forza-live", "INFO") as logs:
            self.send(hello(1, XUID, "Player"))
        self.assertIn("removed by the server admin (error 4)", self.refusal_lines(logs)[0])
        self.send(list_packet())
        self.assertEqual([c for c, _ in self.errors(A)], [4, 4])

    def test_a_lifted_kick_signs_in_and_is_forgotten(self):
        self.join()
        self.server.chat.admin("kick", "%016X" % XUID, 10)
        self.server.chat.admin("kick", "%016X" % XUID, 0)
        self.assertIsNotNone(self.join())
        self.assertNotIn(A, self.server.removed)
        self.server.drop(self.server.by_addr[A], "timed out")
        self.transport.sent.clear()
        self.send(list_packet())
        self.assertEqual(self.errors(A), [(1, "not registered")])

    def test_a_banned_game_s_packets_get_error_4(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = bans.DATA_DIR
        bans.configure(tmp.name)
        self.addCleanup(bans.configure, old)
        self.join()
        self.assertTrue(bans.set_ban(XUID))  # the server drops the game at once (on_ban)
        self.assertEqual(self.server.peers, {})
        self.transport.sent.clear()
        for _ in range(3):
            self.send(list_packet())
        self.assertEqual(self.errors(A), [(4, "banned from this server")] * 3)

    def test_banned_while_online_the_next_packet_is_told(self):
        self.join()
        with mock.patch.object(bans, "cached", return_value=frozenset({XUID})):
            self.send(list_packet())
        self.assertEqual(self.server.peers, {})
        self.assertEqual(self.errors(A), [(4, "banned from this server")])

    def test_forgotten_after_a_while(self):
        self.server.remember_removed(A, "banned from this server")
        message, until = self.server.removed[A]
        self.server.removed[A] = (message, until - srv.REMOVED_KEEP - 1)
        self.send(list_packet())
        self.assertEqual(self.errors(A), [(1, "not registered")])
        self.assertEqual(self.server.removed, {})

    def test_the_list_is_bounded(self):
        with mock.patch.object(srv, "REMOVED_MAX", 3):
            for port in range(10):
                self.server.remember_removed(("203.0.113.9", port), "banned from this server")
        self.assertEqual(list(self.server.removed), [("203.0.113.9", p) for p in (7, 8, 9)])


if __name__ == "__main__":
    unittest.main()
