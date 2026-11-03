"""Volunteer relays (FH1 #516): tickets, the relay's rules (session scope, private addresses,
the rate cap, slot expiry), its link to the main server (drop, reconnect, revocation), the main
server's offers and the signed /relays.json. In-process, with fake clocks and transports; the
end-to-end run with a real main server and a real relay is tests/network/test_relay_processes.py."""

import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout

from forza_live import server as srv
from forza_live.core import ed25519, relays, server_list
from forza_live.network import relay_broker as rb
from forza_live.network import relay_node as rn
from forza_live.title_services import lsp
from tests.support import FakeTransport

SECRET = bytes(range(32))
OTHER_SECRET = bytes([9] * 32)
MAIN = ("198.51.100.1", 21200)
RELAY_ADDR = ("192.0.2.50", 21300)
ALICE, BOB, CAROL = ("203.0.113.10", 40001), ("203.0.113.20", 40002), ("203.0.113.30", 40003)


class Clock:
    def __init__(self):
        self.mono, self.unix = 1000.0, 1_800_000_000.0

    def advance(self, seconds):
        self.mono += seconds
        self.unix += seconds


def datagrams(transport, addr, kind):
    return [d[5:] for a, d in transport.sent if a == addr and d[4] == kind]


class Tickets(unittest.TestCase):
    def test_valid_until_expiry(self):
        t = relays.make_ticket(SECRET, "eu-1", 0x1122, 0x0009000000000001, 7, 2000, 4096)
        self.assertEqual(len(t), relays.TICKET_SIZE)
        got = relays.check_ticket(SECRET, "eu-1", t, now=1999)
        self.assertEqual(got, relays.Ticket(0x1122, 0x0009000000000001, 7, 2000, 4096))
        with self.assertRaisesRegex(ValueError, "expired"):
            relays.check_ticket(SECRET, "eu-1", t, now=2001)

    def test_forged_or_for_another_relay(self):
        t = relays.make_ticket(SECRET, "eu-1", 1, 2, 3, 2000, 4096)
        with self.assertRaisesRegex(ValueError, "forged"):
            relays.check_ticket(OTHER_SECRET, "eu-1", t, now=1000)
        tampered = bytearray(t)
        tampered[10] ^= 1  # another session
        with self.assertRaisesRegex(ValueError, "forged"):
            relays.check_ticket(SECRET, "eu-1", bytes(tampered), now=1000)
        with self.assertRaisesRegex(ValueError, "another relay"):
            relays.check_ticket(SECRET, "us-1", t, now=1000)  # same secret, other relay
        with self.assertRaisesRegex(ValueError, "not a ticket"):
            relays.check_ticket(SECRET, "eu-1", t[:-1], now=1000)


class RefusedAddresses(unittest.TestCase):
    def test_private_and_special_addresses(self):
        for ip in ("10.1.2.3", "127.0.0.1", "192.168.1.5", "172.16.0.1", "172.31.255.255",
                   "169.254.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
                   "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "not an ip"):
            self.assertTrue(rn.refused_address(ip), ip)
        for ip in ("203.0.113.10", "198.51.100.7", "8.8.8.8", "172.32.0.1", "2001:db8::1"):
            self.assertFalse(rn.refused_address(ip), ip)


class NodeBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.node = rn.RelayNode("eu-1", SECRET, MAIN, clock=lambda: self.clock.mono,
                                 wall=lambda: self.clock.unix)
        self.transport = FakeTransport()
        self.node.connection_made(self.transport)

    def ticket(self, session, peer_id, life=600, rate=100000, secret=SECRET, relay="eu-1"):
        return relays.make_ticket(secret, relay, session, 0x0009000000000000 + peer_id, peer_id,
                                  self.clock.unix + life, rate)

    def bind(self, addr, ticket):
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_BIND]) + ticket, addr)

    def relay(self, src, dst_id, payload=b"x" * 100):
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY]) +
                                    struct.pack(">IHH", dst_id, 1000, 1000) + payload, src)

    def forwards(self, addr):
        return datagrams(self.transport, addr, rn.FORWARD)

    def errors(self, addr):
        return [(b[0], b[3:3 + struct.unpack_from(">H", b, 1)[0]].decode())
                for b in datagrams(self.transport, addr, rn.ERROR)]


class Node(NodeBase):
    def test_bind_and_forward_inside_a_session(self):
        self.bind(ALICE, self.ticket(0xAA, 1))
        self.bind(BOB, self.ticket(0xAA, 2))
        bound = datagrams(self.transport, ALICE, rn.RELAY_BOUND)
        self.assertEqual(struct.unpack(">QII", bound[0]), (0xAA, 1, 600))
        self.relay(ALICE, 2, b"hello")
        got = self.forwards(BOB)
        self.assertEqual(len(got), 1)
        # the main server's FORWARD: the sender's peer id, no addresses, ports, payload
        self.assertEqual(got[0], struct.pack(">IIHIH", 1, 0, 0, 0, 0) +
                         struct.pack(">HH", 1000, 1000) + b"hello")
        self.relay(BOB, 1, b"back")
        self.assertEqual(self.forwards(ALICE)[0][-4:], b"back")
        self.assertEqual(self.node.counters["forwarded_packets"], 2)

    def test_session_scope(self):
        self.bind(ALICE, self.ticket(0xAA, 1))
        self.bind(BOB, self.ticket(0xAA, 2))
        self.bind(CAROL, self.ticket(0xBB, 3))  # another session
        self.relay(CAROL, 1)
        self.relay(CAROL, 2)
        self.relay(ALICE, 3)
        self.assertEqual(self.forwards(ALICE) + self.forwards(BOB) + self.forwards(CAROL), [])
        self.assertEqual(self.node.counters["refused_session"], 3)

    def test_unbound_sender_is_told_to_bind(self):
        self.bind(BOB, self.ticket(0xAA, 2))
        self.relay(ALICE, 2)
        self.assertEqual(self.forwards(BOB), [])
        self.assertEqual(self.errors(ALICE), [(rn.ERROR_NOT_BOUND, "not bound")])

    def test_private_addresses_neither_bind_nor_get_traffic(self):
        lan = ("192.168.1.5", 40000)
        self.bind(lan, self.ticket(0xAA, 1))
        self.assertEqual(self.errors(lan), [(rn.ERROR_TICKET, "address refused")])
        self.assertEqual(self.node.slots, {})
        self.bind(("127.0.0.1", 22), self.ticket(0xAA, 4))
        self.assertEqual(self.node.slots, {})
        # and a slot that somehow points inside is never sent to
        self.bind(ALICE, self.ticket(0xAA, 2))
        self.node.slots[(0xAA, 9)] = rn.Slot(relays.Ticket(0xAA, 9, 9, self.clock.unix + 60, 1000),
                                             ("10.0.0.1", 53), self.clock.mono)
        self.relay(ALICE, 9)
        self.assertEqual(self.forwards(("10.0.0.1", 53)), [])
        self.assertEqual(self.node.counters["refused_private"], 3)

    def test_private_addresses_allowed_for_a_lan_test(self):
        self.node.allow_private = True
        self.bind(("127.0.0.1", 40001), self.ticket(0xAA, 1))
        self.bind(("127.0.0.1", 40002), self.ticket(0xAA, 2))
        self.relay(("127.0.0.1", 40001), 2)
        self.assertEqual(len(self.forwards(("127.0.0.1", 40002))), 1)

    def test_rate_cap_per_ticket(self):
        self.bind(ALICE, self.ticket(0xAA, 1, rate=1000))  # bursts of 4000 bytes
        self.bind(BOB, self.ticket(0xAA, 2))
        for _ in range(50):
            self.relay(ALICE, 2, b"x" * 192)  # 200 bytes each with the header
        self.assertEqual(len(self.forwards(BOB)), 20)
        self.assertEqual(self.node.counters["over_rate"], 30)
        self.clock.advance(1.0)  # 1000 bytes more
        for _ in range(10):
            self.relay(ALICE, 2, b"x" * 192)
        self.assertEqual(len(self.forwards(BOB)), 25)
        # Bob's own ticket is not spent by Alice's traffic
        for _ in range(10):
            self.relay(BOB, 1, b"x" * 192)
        self.assertEqual(len(self.forwards(ALICE)), 10)

    def test_unused_slots_expire_after_60_s(self):
        self.bind(ALICE, self.ticket(0xAA, 1))
        self.bind(BOB, self.ticket(0xAA, 2))
        self.clock.advance(40)
        self.relay(ALICE, 2)  # Alice's slot is used; Bob's is not
        self.clock.advance(25)
        self.node.tick()
        self.assertEqual(sorted(self.node.slots), [(0xAA, 1)])
        self.relay(ALICE, 2)
        self.assertEqual(len(self.forwards(BOB)), 1)  # Bob is gone
        self.clock.advance(61)
        self.node.tick()
        self.assertEqual(self.node.slots, {})
        self.assertEqual(self.node.by_addr, {})

    def test_expired_ticket_ends_the_slot(self):
        self.bind(ALICE, self.ticket(0xAA, 1, life=30))
        self.bind(BOB, self.ticket(0xAA, 2))
        self.clock.advance(20)
        self.relay(ALICE, 2)
        self.clock.advance(15)  # Alice's ticket ran out, though her slot is in use
        self.relay(ALICE, 2)
        self.assertEqual(len(self.forwards(BOB)), 1)
        self.assertEqual(self.errors(ALICE), [(rn.ERROR_NOT_BOUND, "not bound")])
        self.bind(ALICE, self.ticket(0xAA, 1, life=-1))
        self.assertEqual(self.errors(ALICE)[-1], (rn.ERROR_TICKET, "expired ticket"))

    def test_forged_tickets_are_refused(self):
        self.bind(ALICE, self.ticket(0xAA, 1, secret=OTHER_SECRET))
        self.bind(BOB, self.ticket(0xAA, 2, relay="us-1"))
        self.bind(CAROL, b"\x01" * relays.TICKET_SIZE)
        self.assertEqual(self.node.slots, {})
        self.assertEqual(self.errors(ALICE), [(rn.ERROR_TICKET, "forged ticket")])
        self.assertEqual(self.errors(BOB), [(rn.ERROR_TICKET, "ticket for another relay")])
        self.assertEqual(self.node.counters["refused_tickets"], 3)

    def test_one_slot_per_address_and_a_new_address_moves_it(self):
        self.bind(ALICE, self.ticket(0xAA, 1))
        self.bind(ALICE, self.ticket(0xBB, 1))  # moved to another session
        self.assertEqual(sorted(self.node.slots), [(0xBB, 1)])
        moved = ("203.0.113.10", 40999)  # NAT rebinding
        self.bind(moved, self.ticket(0xBB, 1))
        self.assertEqual(self.node.slots[(0xBB, 1)].addr, moved)
        self.assertNotIn(ALICE, self.node.by_addr)

    def test_ping_echoes_at_most_32_bytes(self):
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_PING]) + b"t" * 8, ALICE)
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_PING]) + b"t" * 33, BOB)
        self.assertEqual(datagrams(self.transport, ALICE, rn.RELAY_PONG), [b"t" * 8])
        self.assertEqual(datagrams(self.transport, BOB, rn.RELAY_PONG), [])


def peer(n, addr, features=rb.FEATURE_RELAYS):
    p = srv.Peer(n, addr, n, 0x0009000000000000 + n, "Driver%d" % n, "#TU4", (0, 0))
    p.features = features
    return p


class Linked(unittest.TestCase):
    """A main server (ForzaLive, in process) and a relay, their datagrams carried by hand."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        with redirect_stdout(io.StringIO()):
            relays.main(["--data-dir", self.dir, "add", "eu-1", "--address", "%s:%d" % RELAY_ADDR,
                         "--name", "Example relay", "--region", "Europe"])
        secret = relays.read_secret(relays.secret_path("eu-1", self.dir))
        self.clock = Clock()
        self.server = srv.ForzaLive("test", 64)
        self.addCleanup(self.server.events.db.close)
        self.main_tx = FakeTransport()
        self.server.transport = self.main_tx
        self.server.relays = rb.RelayBroker(self.server, rate=lambda: 50000,
                                            clock=lambda: self.clock.mono,
                                            wall=lambda: self.clock.unix)
        self.broker = self.server.relays
        self.broker.offer_after = 8.0
        self.broker.configure(self.dir)
        self.node = rn.RelayNode("eu-1", secret, MAIN, clock=lambda: self.clock.mono,
                                 wall=lambda: self.clock.unix)
        self.relay_tx = FakeTransport()
        self.node.connection_made(self.relay_tx)
        self.main_up = True

    def carry(self):
        """Delivers what the relay sent the main server, and the main server's answers."""
        for addr, data in self.relay_tx.sent:
            if addr == MAIN and self.main_up:
                self.server.datagram_received(data, RELAY_ADDR)
        self.relay_tx.sent = [(a, d) for a, d in self.relay_tx.sent if a != MAIN]
        for addr, data in self.main_tx.sent:
            if addr == RELAY_ADDR:
                self.node.datagram_received(data, MAIN)
        self.main_tx.sent = [(a, d) for a, d in self.main_tx.sent if a != RELAY_ADDR]

    def link(self):
        self.node.tick()
        self.carry()

    def players(self, *peers):
        for p in peers:
            self.server.peers[p.id] = p
            self.server.by_addr[p.addr] = p

    def relay_through_main(self, src, dst, seconds, step=0.5):
        body = struct.pack(">IHH", dst.id, 1000, 1000) + b"x" * 40
        for _ in range(int(seconds / step)):
            self.server.on_relay(src, body)
            self.clock.advance(step)

    def offers(self, addr):
        out = []
        for body in datagrams(self.main_tx, addr, rn.RELAY_INFO):
            if body[0] != rb.OP_OFFER:
                continue
            ip, port, n = struct.unpack_from(">IHB", body, 1)
            ticket = body[8:8 + n]
            count = body[8 + n]
            ids = list(struct.unpack_from(">%dI" % count, body, 9 + n))
            out.append((srv.u32_to_ip(ip), port, ticket, ids))
        return out


class Link(Linked):
    def test_link_up_and_status(self):
        self.link()
        self.assertTrue(self.node.linked)
        self.assertTrue(self.broker.up("eu-1"))
        status = self.server.status()["relays"]
        self.assertEqual([(r["id"], r["up"], r["region"]) for r in status], [("eu-1", True, "Europe")])
        self.assertNotIn("192.0.2.50", json.dumps(self.server.status()))  # no addresses

    def test_link_drop_and_reconnect(self):
        self.link()
        self.main_up = False  # the main server is unreachable
        for _ in range(31):
            self.clock.advance(1)
            self.link()
        self.assertFalse(self.node.linked)
        self.broker.tick()
        self.assertFalse(self.broker.up("eu-1"))
        self.main_up = True
        for _ in range(int(rn.LINK_RETRY) + 1):
            self.clock.advance(1)
            self.link()
        self.assertTrue(self.node.linked)
        self.assertTrue(self.broker.up("eu-1"))

    def test_main_server_restart_reconnects(self):
        self.link()
        fresh = rb.RelayBroker(self.server, clock=lambda: self.clock.mono,
                               wall=lambda: self.clock.unix)
        fresh.configure(self.dir)
        self.server.relays = fresh  # what a restarted server knows: the list, no links
        for _ in range(int(rn.LINK_INTERVAL) + 1):
            self.clock.advance(1)
            self.link()
        self.assertTrue(fresh.up("eu-1"))

    def test_replayed_or_forged_link(self):
        self.node.tick()
        (addr, data), = self.relay_tx.sent
        self.server.datagram_received(data, RELAY_ADDR)
        self.server.datagram_received(data, ("203.0.113.99", 5555))  # a replay elsewhere
        linked = [a for a, d in self.main_tx.sent if d[4] == rn.RELAY_LINKED]
        self.assertEqual(linked, [RELAY_ADDR])
        forged = rn.link_body("eu-1", OTHER_SECRET, self.clock.unix, b"n" * 16, 0, 0, 0)
        self.server.datagram_received(rn.MAGIC + bytes([rn.RELAY_LINK]) + forged, CAROL)
        self.assertEqual(datagrams(self.main_tx, CAROL, rn.RELAY_LINKED), [])
        refused = datagrams(self.main_tx, CAROL, rn.ERROR)
        self.assertEqual(refused[0][0], rn.ERROR_REFUSED)
        self.assertTrue(refused[0].endswith(b"n" * 16))

    def test_a_spoofed_refusal_is_ignored(self):
        self.link()
        text = b"relay refused: not listed"
        self.node.datagram_received(rn.MAGIC + bytes([rn.ERROR, rn.ERROR_REFUSED]) +
                                    struct.pack(">H", len(text)) + text + b"z" * 16, MAIN)
        self.assertFalse(self.node.revoked)

    def test_revoked_secret(self):
        self.link()
        old_ticket = relays.make_ticket(self.node.secret, "eu-1", 0xAA, 1, 1,
                                        self.clock.unix + 600, 50000)
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_BIND]) + old_ticket, ALICE)
        self.assertEqual(len(self.node.slots), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(relays.main(["--data-dir", self.dir, "remove", "eu-1"]), 0)
        self.broker.tick()
        self.assertFalse(self.broker.enabled)
        self.assertFalse(self.broker.up("eu-1"))
        # the relay's next link is refused: it drops its slots and takes no ticket
        self.clock.advance(rn.LINK_INTERVAL)
        self.link()
        self.assertTrue(self.node.revoked)
        self.assertFalse(self.node.linked)
        self.assertEqual(self.node.slots, {})
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_BIND]) + old_ticket, ALICE)
        self.assertEqual(self.node.slots, {})

    def test_a_new_secret_revokes_the_old_one(self):
        self.link()
        with redirect_stdout(io.StringIO()):
            relays.main(["--data-dir", self.dir, "add", "eu-1", "--address", "%s:%d" % RELAY_ADDR,
                         "--name", "Example relay"])  # a new secret for eu-1
        self.broker.tick()
        self.assertFalse(self.broker.up("eu-1"))
        self.clock.advance(rn.LINK_INTERVAL)
        self.link()  # still signed with the old secret
        self.assertTrue(self.node.revoked)
        self.assertFalse(self.broker.up("eu-1"))


class Offers(Linked):
    def setUp(self):
        super().setUp()
        self.link()
        self.a, self.b = peer(1, ALICE), peer(2, BOB)
        self.players(self.a, self.b)

    def test_probe_list_after_welcome(self):
        self.server.on_hello(struct.pack(">HQQIH", srv.VERSION, 77, 0x0009000000000077, 0, 0) +
                             b"\x04#TU4\x04Newb" + bytes([rb.FEATURE_RELAYS]), CAROL)
        probes = [b for b in datagrams(self.main_tx, CAROL, rn.RELAY_INFO) if b[0] == rb.OP_PROBE]
        self.assertEqual(len(probes), 1)
        version, count, index, ip, port = struct.unpack_from(">HBBIH", probes[0], 1)
        self.assertEqual((count, index, srv.u32_to_ip(ip), port), (1, 0, *RELAY_ADDR))
        # and an old game gets none
        self.server.on_hello(struct.pack(">HQQIH", srv.VERSION, 78, 0x0009000000000078, 0, 0) +
                             b"\x04#TU4\x03Old" + bytes([1]), ("203.0.113.40", 1))
        self.assertEqual(datagrams(self.main_tx, ("203.0.113.40", 1), rn.RELAY_INFO), [])

    def test_offered_only_after_hole_punching_had_its_time(self):
        self.relay_through_main(self.a, self.b, 7.5)
        self.assertEqual(self.offers(ALICE), [])
        self.relay_through_main(self.a, self.b, 1.0)
        (ip, port, ticket, ids), = self.offers(ALICE)
        self.assertEqual(((ip, port), ids), (RELAY_ADDR, [2]))
        (_, _, bob_ticket, bob_ids), = self.offers(BOB)
        self.assertEqual(bob_ids, [1])
        t = relays.check_ticket(self.node.secret, "eu-1", ticket, self.clock.unix)
        self.assertEqual((t.peer_id, t.xuid, t.rate), (1, self.a.xuid, 50000))
        self.assertEqual(t.session, relays.check_ticket(self.node.secret, "eu-1", bob_ticket,
                                                       self.clock.unix).session)

    def test_end_to_end_through_the_relay(self):
        self.relay_through_main(self.a, self.b, 9)
        ta, tb = self.offers(ALICE)[0][2], self.offers(BOB)[0][2]
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_BIND]) + ta, ALICE)
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY_BIND]) + tb, BOB)
        self.node.datagram_received(rn.MAGIC + bytes([rn.RELAY]) + struct.pack(">IHH", 2, 1, 1) +
                                    b"race", ALICE)
        (fwd,) = datagrams(self.relay_tx, BOB, rn.FORWARD)
        self.assertEqual(struct.unpack_from(">I", fwd)[0], 1)
        self.assertTrue(fwd.endswith(b"race"))

    def test_old_games_stay_on_the_main_relay(self):
        c = peer(3, CAROL, features=0)
        self.players(c)
        self.relay_through_main(self.a, c, 20)
        self.assertEqual(self.offers(ALICE) + self.offers(CAROL), [])

    def test_no_relay_up_no_offer(self):
        self.clock.advance(rb.LINK_TIMEOUT + 1)
        self.broker.tick()
        self.relay_through_main(self.a, self.b, 20)
        self.assertEqual(self.offers(ALICE), [])

    def test_a_third_player_joins_the_session(self):
        c = peer(3, CAROL)
        self.players(c)
        self.relay_through_main(self.a, self.b, 9)
        self.relay_through_main(c, self.a, 9)
        self.assertEqual(self.offers(CAROL)[-1][3], [1, 2])
        self.assertEqual(self.offers(ALICE)[-1][3], [2, 3])
        self.assertEqual(self.offers(BOB)[-1][3], [1, 3])

    def test_leaving_and_relay_down_withdraw(self):
        self.relay_through_main(self.a, self.b, 9)
        self.server.drop(self.b, "left")
        self.assertEqual(self.offers(ALICE)[-1], ("0.0.0.0", 0, b"", []))
        self.players(self.b)
        self.relay_through_main(self.a, self.b, 9)
        self.assertEqual(self.offers(ALICE)[-1][3], [2])
        self.clock.advance(rb.LINK_TIMEOUT + 1)  # the relay's link went quiet
        self.broker.tick()
        self.assertEqual(self.offers(ALICE)[-1], ("0.0.0.0", 0, b"", []))
        self.assertEqual(self.offers(BOB)[-1], ("0.0.0.0", 0, b"", []))

    def test_tickets_are_renewed(self):
        self.relay_through_main(self.a, self.b, 9)
        first = len(self.offers(ALICE))
        for _ in range(int(rb.TICKET_LIFE / 2 / 5) + 1):
            self.clock.advance(5)
            self.link()  # the relay stays linked
            self.broker.tick()
        self.assertEqual(len(self.offers(ALICE)), first + 1)

    def test_lowest_worst_pair_ping(self):
        with redirect_stdout(io.StringIO()):
            relays.main(["--data-dir", self.dir, "add", "us-1", "--address", "192.0.2.60:21300",
                         "--name", "Other relay"])
        self.broker.tick()
        us = rn.RelayNode("us-1", relays.read_secret(relays.secret_path("us-1", self.dir)), MAIN,
                          clock=lambda: self.clock.mono, wall=lambda: self.clock.unix)
        tx = FakeTransport()
        us.connection_made(tx)
        us.tick()
        self.link()
        for _, data in tx.sent:
            self.server.datagram_received(data, ("192.0.2.60", 21300))
        self.assertEqual(self.broker.up_relays(), ["eu-1", "us-1"])
        version = self.broker.version

        def pings(p, eu, us_ms):
            self.server.datagram_received(
                srv.MAGIC + bytes([rn.RELAY_INFO]) +
                struct.pack(">BHBBHBH", rb.OP_PINGS, version, 2, 0, eu, 1, us_ms), p.addr)

        pings(self.a, 20, 90)   # Alice is near eu-1
        pings(self.b, 200, 120)  # Bob is far from both, nearer us-1: worst 200 vs 120
        self.assertEqual(self.broker.choose(self.a, self.b), "us-1")
        pings(self.b, 200, rb.NO_ANSWER)  # Bob cannot reach us-1 at all
        self.assertEqual(self.broker.choose(self.a, self.b), "eu-1")


class SignedList(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        relays.configure(self.dir)
        self.addCleanup(relays.configure, "forza_live_data")
        with open(os.path.join(self.dir, server_list.KEY_FILE), "w") as f:
            f.write(bytes([7] * 32).hex() + "\n")

    def test_no_list_no_document(self):
        self.assertEqual(relays.handle("GET")[0], 404)

    def test_signed_with_the_server_list_key_and_no_secrets(self):
        path = relays.add_relay(self.dir, "eu-1", "Example relay", "your.host:21300", "Europe",
                                "operator@example.org")
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        status, kind, body, extra = lsp.handle(None, "GET", "/relays.json", {}, b"",
                                               ("192.0.2.50", 1))
        self.assertEqual((status, kind), (200, "application/json"))
        doc = json.loads(body)
        self.assertEqual(doc["key"], ed25519.public_key(bytes([7] * 32)).hex())
        listed = relays.check_document(doc, bytes.fromhex(doc["key"]))
        self.assertEqual(listed, [{"id": "eu-1", "name": "Example relay",
                                   "address": "your.host:21300", "region": "Europe"}])
        with open(path) as f:
            self.assertNotIn(f.read().strip(), body.decode())
        # a tampered list, or one signed for servers.json, is refused
        doc2 = dict(doc, list=doc["list"].replace("eu-1", "eu-2"))
        with self.assertRaisesRegex(ValueError, "bad signature"):
            relays.check_document(doc2, bytes.fromhex(doc["key"]))
        with self.assertRaisesRegex(ValueError, "expired"):
            relays.check_document(doc, bytes.fromhex(doc["key"]), now=2 ** 40)

    def test_revoked_relays_are_not_listed(self):
        relays.add_relay(self.dir, "eu-1", "Example relay", "your.host:21300")
        relays.add_relay(self.dir, "us-1", "Other relay", "192.0.2.60:21300")
        relays.remove_relay(self.dir, "eu-1")
        doc = json.loads(relays.handle("GET")[2])
        self.assertEqual([r["id"] for r in relays.check_document(doc, bytes.fromhex(doc["key"]))],
                         ["us-1"])

    def test_bad_entries_are_refused(self):
        for bad in (dict(relay_id="EU 1"), dict(address="no port:x"), dict(name="")):
            args = dict(relay_id="eu-1", name="Example", address="your.host:21300")
            args.update(bad)
            with self.assertRaises(ValueError):
                relays.add_relay(self.dir, **args)


if __name__ == "__main__":
    unittest.main()
