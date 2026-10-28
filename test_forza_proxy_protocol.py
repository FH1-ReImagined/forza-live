"""PROXY protocol v2 from the tunnel agent (forza_proxy.py): a tunnelled player's public
address reaches the others for hole punching, and a flow without a header (the setting off,
or a flow that began before it was turned on or before a restart) is relayed exactly as
before, so the server can be deployed before the tunnel's setting is turned on."""

import asyncio
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import forza_limits as limits
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_proxy as proxy

AGENT = "192.168.100.105"  # the tunnel agent's address, a trusted proxy
TUNNEL = ("192.0.2.10", 56677)  # the tunnel's public address (the header's destination)
REAL_A = ("203.0.113.7", 40001)  # players' public addresses
REAL_B = ("198.51.100.9", 52000)


def playit_v4(src, dst, kind=0x12):
    """The bytes playit's agent writes (packages/agent_core/src/network/proxy_protocol.rs,
    write_v2_udp; 0x11 for TCP, write_v2_tcp)."""
    return (bytes([0x0D, 0x0A, 0x0D, 0x0A, 0x00, 0x0D, 0x0A, 0x51, 0x55, 0x49, 0x54, 0x0A,
                   0x21, kind]) + (12).to_bytes(2, "big") + socket.inet_aton(src[0]) +
            socket.inet_aton(dst[0]) + src[1].to_bytes(2, "big") + dst[1].to_bytes(2, "big"))


def playit_v6(src, dst, kind=0x22):
    return (proxy.SIGNATURE + bytes([0x21, kind]) + (36).to_bytes(2, "big") +
            socket.inet_pton(socket.AF_INET6, src[0]) + socket.inet_pton(socket.AF_INET6, dst[0]) +
            src[1].to_bytes(2, "big") + dst[1].to_bytes(2, "big"))


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((addr, data))


def hello(token, name="P", room="r", lan=("10.0.0.5", 3074)):
    body = struct.pack(">HQQIH", srv.VERSION, token, 0x0009000000000000 | token,
                       srv.ip_to_u32(lan[0]), lan[1])
    body += bytes([len(room)]) + room.encode() + bytes([len(name)]) + name.encode()
    return srv.MAGIC + bytes([srv.HELLO]) + body


def relay(dst_id, payload=b"hi"):
    return srv.MAGIC + bytes([srv.RELAY]) + struct.pack(">IHH", dst_id, 1, 2) + payload


def welcome_public(net, addr):
    """(ip, port) of the last WELCOME sent to addr."""
    data = [d for a, d in net.sent if a == addr and d[4] == srv.WELCOME][-1]
    _id, _epoch, ip, port, _ = struct.unpack_from(">IHIHH", data, 5)
    return srv.u32_to_ip(ip), port


def forwards(net, addr):
    """(id, public ip, port, lan ip, lan port) of every FORWARD sent to addr."""
    return [(lambda f: (f[0], srv.u32_to_ip(f[1]), f[2], srv.u32_to_ip(f[3]), f[4]))(
        struct.unpack_from(">IIHIH", d, 5)) for a, d in net.sent if a == addr and d[4] == srv.FORWARD]


class ParseTest(unittest.TestCase):
    def setUp(self):
        limits.configure_proxies(AGENT)

    def tearDown(self):
        limits.configure_proxies("")

    def test_playit_udp_and_tcp_headers(self):
        self.assertEqual(proxy.parse(playit_v4(REAL_A, TUNNEL), proxy.DGRAM), (REAL_A, b""))
        self.assertEqual(proxy.parse(playit_v4(REAL_A, TUNNEL, 0x11), proxy.STREAM), (REAL_A, b""))
        self.assertEqual(proxy.header(REAL_A, TUNNEL), playit_v4(REAL_A, TUNNEL))
        self.assertEqual(proxy.header(REAL_A, TUNNEL, proxy.STREAM), playit_v4(REAL_A, TUNNEL, 0x11))
        self.assertEqual(proxy.header_length(playit_v4(REAL_A, TUNNEL)[:16]), 28)

    def test_what_follows_the_header_is_returned(self):  # agents up to 0.15.26
        self.assertEqual(proxy.parse(playit_v4(REAL_A, TUNNEL) + b"RLV1\x01x", proxy.DGRAM),
                         (REAL_A, b"RLV1\x01x"))

    def test_ipv6(self):
        v6 = ("2001:db8::7", 40001)
        header = playit_v6(v6, ("2001:db8::1", 56677))
        self.assertEqual(len(header), 52)
        self.assertEqual(proxy.parse(header, proxy.DGRAM), (v6, b""))
        self.assertEqual(proxy.client_address(v6), v6)  # HTTP: logged and limited as is
        self.assertIsNone(proxy.client_address(v6, ipv4=True))  # UDP: the game has no IPv6
        mapped = playit_v6(("::ffff:203.0.113.7", 40001), ("::ffff:192.0.2.10", 56677))
        self.assertEqual(proxy.parse(mapped, proxy.DGRAM), (REAL_A, b""))

    def test_tlvs_are_skipped(self):
        tlvs = bytes([0x04]) + struct.pack(">H", 2) + b"ab" + bytes([0x30]) + struct.pack(">H", 0)
        self.assertEqual(proxy.parse(proxy.header(REAL_A, TUNNEL, tlvs=tlvs) + b"x", proxy.DGRAM),
                         (REAL_A, b"x"))
        for bad in (b"\x04", b"\x04\x00", b"\x04\x00\x05ab"):
            with self.assertRaises(ValueError):
                proxy.parse(proxy.header(REAL_A, TUNNEL, tlvs=bad), proxy.DGRAM)

    def test_local_and_unspecified_name_no_address(self):
        local = proxy.header(REAL_A, TUNNEL, command=proxy.LOCAL)
        self.assertEqual(proxy.parse(local + b"x", proxy.DGRAM), (None, b"x"))
        unspec = proxy.SIGNATURE + b"\x21\x00\x00\x00"
        self.assertEqual(proxy.parse(unspec, proxy.STREAM), (None, b""))
        unix = proxy.SIGNATURE + b"\x21\x31" + struct.pack(">H", 216) + bytes(216)
        self.assertEqual(proxy.parse(unix, proxy.STREAM), (None, b""))

    def test_malformed(self):
        good = bytearray(playit_v4(REAL_A, TUNNEL))

        def changed(pos, value):
            data = bytearray(good)
            data[pos] = value
            return bytes(data)

        bad = [
            bytes(good[:15]),  # shorter than the fixed part
            bytes(good[:27]),  # addresses cut short
            changed(6, 0x0B),  # signature
            changed(12, 0x11),  # version 1
            changed(12, 0x31),  # version 3
            changed(12, 0x22),  # command 2
            changed(12, 0x2F),  # command 15
            changed(13, 0x42),  # family 4
            changed(13, 0x13),  # transport 3
            changed(13, 0x10),  # INET without a transport
            changed(13, 0x02),  # UNSPEC with a transport
            changed(13, 0x11),  # TCP's header on UDP
            changed(13, 0x22),  # IPv6 with 12 bytes of addresses
            changed(15, 11),  # 11 bytes: too few for IPv4
            changed(14, 0x10),  # 4108 bytes: over MAX_LENGTH
            b"PROXY UDP4 203.0.113.7 192.0.2.10 40001 56677\r\n",  # version 1 text
            b"RLV1\x01",
        ]
        for data in bad:
            with self.subTest(data=data[:16].hex()), self.assertRaises(ValueError):
                proxy.parse(bytes(data), proxy.DGRAM)
        with self.assertRaises(ValueError):  # UDP's header on TCP
            proxy.parse(bytes(good), proxy.STREAM)

    def test_unusable_source_addresses(self):
        for ip in ("0.0.0.0", "127.0.0.1", "127.9.8.7", "224.0.0.1", "169.254.3.4",
                   "240.0.0.1", "255.255.255.255", AGENT, "::1", "::", "fe80::1", "not an ip"):
            self.assertIsNone(proxy.client_address((ip, 1234)), ip)
        self.assertIsNone(proxy.client_address(("203.0.113.7", 0)))
        self.assertIsNone(proxy.client_address(None))
        self.assertEqual(proxy.client_address(REAL_A, ipv4=True), REAL_A)
        self.assertEqual(proxy.client_address(("10.1.2.3", 5)), ("10.1.2.3", 5))


class UdpTest(unittest.TestCase):
    def setUp(self):
        limits.configure_proxies(AGENT + ", 127.0.0.0/8")
        self.server = self.new_server()
        self.net = FakeTransport()
        self.server.connection_made(self.net)

    def tearDown(self):
        limits.configure_proxies("")
        for server in self.servers:
            server.events.db.close()

    def new_server(self):
        if not hasattr(self, "servers"):
            self.servers = []
        self.servers.append(srv.ForzaLive("hi", 1024))
        return self.servers[-1]

    def send(self, data, addr):
        self.server.datagram_received(data, addr)

    def join(self, addr, token, real=None, prefixed=False):
        """A player through the agent's socket addr; real: the address its header gives
        (agents from 0.16: a datagram of its own; prefixed: in front of every datagram)."""
        header = proxy.header(real, TUNNEL) if real else b""
        if real and not prefixed:
            self.send(header, addr)
            header = b""
        self.send(header + hello(token, f"P{token}"), addr)
        return self.server.by_addr.get(addr)

    def test_header_datagram_then_hello(self):
        a = (AGENT, 40001)
        self.send(proxy.header(REAL_A, TUNNEL), a)
        self.assertEqual(self.net.sent, [])  # nothing answers the header itself
        self.assertEqual(self.server.proxy_flows[a][0], REAL_A)
        peer = self.join(a, 1)
        self.assertTrue(peer.proxied)
        self.assertEqual(peer.real_addr, REAL_A)
        self.assertEqual(welcome_public(self.net, a), REAL_A)
        self.assertEqual(struct.unpack(">IIHIH", self.server.forward_header(peer)),
                         (peer.id, srv.ip_to_u32(REAL_A[0]), REAL_A[1],
                          srv.ip_to_u32("10.0.0.5"), 3074))
        status = self.server.status()
        self.assertEqual((status["via_proxy"], status["via_proxy_address"]), (1, 1))
        self.assertTrue(status["rooms"]["r"][0]["proxy_address"])
        self.assertEqual(status["proxy_protocol"],
                         {"udp_headers": 1, "http_headers": 0, "rejected": 0, "flows": 1})
        self.assertNotIn(REAL_A[0], json.dumps(status))  # the status page shows no address

    def test_header_in_front_of_every_datagram(self):  # playit agents up to 0.15.26
        a, b = (AGENT, 40001), ("203.0.113.50", 5555)
        peer = self.join(a, 1, REAL_A, prefixed=True)
        self.assertEqual(welcome_public(self.net, a), REAL_A)
        other = self.join(b, 2)
        self.send(proxy.header(REAL_A, TUNNEL) + relay(other.id), a)
        self.assertEqual(forwards(self.net, b),
                         [(peer.id, REAL_A[0], REAL_A[1], "10.0.0.5", 3074)])
        self.assertEqual(len(self.server.proxy_flows), 1)
        self.assertEqual(self.server.proxy_stats["udp_headers"], 2)

    def test_others_punch_to_the_real_address_and_the_relay_stays(self):
        a, c = (AGENT, 40001), (AGENT, 40003)
        b = ("203.0.113.50", 5555)  # connects directly
        pa, pb, pc = self.join(a, 1, REAL_A), self.join(b, 2), self.join(c, 3)  # c: no header
        self.net.sent.clear()
        self.send(relay(pb.id, b"from a"), a)
        self.send(relay(pb.id, b"from c"), c)
        self.assertEqual(forwards(self.net, b),
                         [(pa.id, REAL_A[0], REAL_A[1], "10.0.0.5", 3074),
                          (pc.id, "0.0.0.0", 0, "0.0.0.0", 0)])
        self.send(relay(pa.id, b"to a"), b)  # the relay to a player with an address still works
        self.assertEqual(forwards(self.net, a), [(pb.id, b[0], b[1], "10.0.0.5", 3074)])
        self.assertEqual(self.server.relayed_packets, 3)
        self.net.sent.clear()
        self.send(srv.MAGIC + bytes([srv.BROADCAST]) + b"\0\1\0\2hey", a)
        self.assertEqual(forwards(self.net, b)[0][1:3], REAL_A)
        self.assertEqual(forwards(self.net, c)[0][1:3], REAL_A)
        # nobody is told the agent's socket as a punch target
        self.assertNotIn(socket.inet_aton(AGENT), b"".join(d for _, d in self.net.sent))

    def test_no_header_is_exactly_as_before(self):
        """The new server before the tunnel's setting is on: byte for byte what it sent."""
        self.server.http_port, self.server.proxy_http_port = 21201, 56678
        a, b = (AGENT, 40001), ("203.0.113.50", 5555)
        pa, pb = self.join(a, 1), self.join(b, 2)
        welcome = [d for ad, d in self.net.sent if ad == a and d[4] == srv.WELCOME][0]
        self.assertEqual(welcome, srv.MAGIC + bytes([srv.WELCOME]) +
                         struct.pack(">IHIHH", pa.id, self.server.epoch, 0, 0, 2) + b"hi" +
                         struct.pack(">H", 56678))
        self.assertIsNone(pa.real_addr)
        self.assertEqual(self.server.forward_header(pa), struct.pack(">IIHIH", pa.id, 0, 0, 0, 0))
        self.net.sent.clear()
        self.send(relay(pb.id), a)
        self.assertEqual(self.net.sent, [(b, srv.MAGIC + bytes([srv.FORWARD]) +
                                          struct.pack(">IIHIH", pa.id, 0, 0, 0, 0) +
                                          struct.pack(">HH", 1, 2) + b"hi")])
        status = self.server.status()
        self.assertEqual((status["via_proxy"], status["via_proxy_address"]), (1, 0))
        self.assertEqual(status["proxy_protocol"],
                         {"udp_headers": 0, "http_headers": 0, "rejected": 0, "flows": 0})

    def test_pre_existing_flow_gets_a_header_later(self):
        """A flow from before the setting (or a restart) stays relayed; when the agent opens
        it again (after 60 to 90 s idle, or an agent restart) its header arrives first."""
        a, b = (AGENT, 40001), ("203.0.113.50", 5555)
        pa, pb = self.join(a, 1), self.join(b, 2)
        for _ in range(3):
            self.send(hello(1, "P1"), a)
            self.send(relay(pb.id), a)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))
        self.assertEqual(forwards(self.net, b)[-1][1:], ("0.0.0.0", 0, "0.0.0.0", 0))
        self.server.state_dirty = False
        self.send(proxy.header(REAL_A, TUNNEL), a)
        self.assertIs(self.server.by_addr[a], pa)
        self.assertEqual(pa.real_addr, REAL_A)
        self.assertTrue(self.server.state_dirty)
        self.send(relay(pb.id), a)
        self.assertEqual(forwards(self.net, b)[-1][1:3], REAL_A)
        self.send(hello(1, "P1"), a)
        self.assertEqual(welcome_public(self.net, a), REAL_A)

    def test_players_on_both_sides_of_the_flip(self):
        old, new = (AGENT, 40001), (AGENT, 40002)
        p_old, p_new = self.join(old, 1), self.join(new, 2, REAL_B)
        self.assertEqual(welcome_public(self.net, old), ("0.0.0.0", 0))
        self.assertEqual(welcome_public(self.net, new), REAL_B)
        self.assertEqual(self.server.status()["via_proxy_address"], 1)
        self.assertIsNone(p_old.real_addr)
        self.assertEqual(p_new.real_addr, REAL_B)

    def test_untrusted_header_is_never_believed(self):
        x = ("203.0.113.99", 7000)
        self.send(proxy.header(REAL_A, TUNNEL), x)
        self.send(proxy.header(REAL_A, TUNNEL) + hello(1, "P1"), x)  # dropped, as before
        self.assertEqual(self.net.sent, [])
        self.assertEqual(self.server.proxy_flows, {})
        self.assertEqual(self.server.proxy_stats["rejected"], 2)
        peer = self.join(x, 1)
        self.assertFalse(peer.proxied)
        self.assertEqual(welcome_public(self.net, x), x)  # its own address, as for any direct player

    def test_malformed_header_from_the_agent_is_ignored(self):
        a = (AGENT, 40001)
        bad = bytearray(proxy.header(REAL_A, TUNNEL))
        bad[12] = 0x11  # version 1
        self.send(bytes(bad), a)
        self.send(bytes(bad) + hello(1, "P1"), a)  # where the datagram starts is unknown: dropped
        self.send(proxy.header(REAL_A, TUNNEL)[:20], a)  # cut short
        self.assertEqual(self.net.sent, [])
        self.assertEqual(self.server.proxy_flows, {})
        self.assertEqual(self.server.proxy_stats["rejected"], 3)
        self.join(a, 1)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))

    def test_version_1_on_udp_is_ignored(self):
        a = (AGENT, 40001)
        v1 = b"PROXY UDP4 203.0.113.7 192.0.2.10 40001 56677\r\n"
        self.send(v1, a)
        self.send(v1 + hello(1, "P1"), a)
        self.assertEqual((self.net.sent, self.server.proxy_flows), ([], {}))
        self.join(a, 1)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))

    def test_header_without_a_usable_address(self):
        a = (AGENT, 40001)
        peer = self.join(a, 1, REAL_A)
        self.assertEqual(peer.real_addr, REAL_A)
        for header in (proxy.header(REAL_A, TUNNEL, command=proxy.LOCAL),
                       playit_v6(("2001:db8::7", 40001), ("2001:db8::1", 56677)),
                       proxy.header(("127.0.0.1", 40001), TUNNEL),
                       proxy.header((AGENT, 40001), TUNNEL),
                       proxy.header(("0.0.0.0", 40001), TUNNEL)):
            self.send(proxy.header(REAL_A, TUNNEL), a)
            self.assertEqual(peer.real_addr, REAL_A)
            self.send(header, a)  # a new flow on that socket, with no address: relayed
            self.assertIsNone(peer.real_addr)
            self.assertNotIn(a, self.server.proxy_flows)
            self.assertEqual(self.server.forward_header(peer), struct.pack(">IIHIH", peer.id, 0, 0, 0, 0))
        mapped = playit_v6(("::ffff:203.0.113.7", 40001), ("::ffff:192.0.2.10", 56677))
        self.send(mapped, a)
        self.assertEqual(peer.real_addr, REAL_A)

    def test_a_new_header_replaces_the_old_one(self):
        a = (AGENT, 40001)
        first = self.join(a, 1, REAL_A)
        self.send(proxy.header(REAL_B, TUNNEL), a)  # the socket now carries another player
        self.send(hello(2, "P2"), a)  # its game: another token, the old registration goes
        second = self.server.by_addr[a]
        self.assertIsNot(second, first)
        self.assertEqual(second.real_addr, REAL_B)
        self.assertEqual(welcome_public(self.net, a), REAL_B)
        self.assertEqual(len(self.server.proxy_flows), 1)

    def test_flows_expire(self):
        a, b = (AGENT, 40001), (AGENT, 40002)
        self.send(proxy.header(REAL_A, TUNNEL), a)  # a header, no player yet
        peer = self.join(b, 2, REAL_B)
        old = time.monotonic() - srv.PROXY_FLOW_TTL - 1
        for addr in (a, b):
            self.server.proxy_flows[addr] = (self.server.proxy_flows[addr][0], old)
        self.server.expire()
        self.assertNotIn(a, self.server.proxy_flows)
        self.assertIn(b, self.server.proxy_flows)  # a registered player keeps its flow
        self.send(hello(2, "P2"), b)  # each HELLO refreshes it
        self.assertGreater(self.server.proxy_flows[b][1], old)
        self.server.drop(peer, "timed out")
        self.server.expire()
        self.assertIn(b, self.server.proxy_flows)  # for a while: the agent keeps its flow 60-90 s
        self.server.proxy_flows[b] = (REAL_B, old)
        self.server.expire()
        self.assertEqual(self.server.proxy_flows, {})
        self.assertEqual(self.server.proxy_flow_owners, {})

    def test_back_after_a_silence_on_the_same_flow(self):
        """A game silent for longer than PEER_TIMEOUT but not long enough for the agent to
        forget its flow comes back without a new header: it keeps its address."""
        a = (AGENT, 40001)
        peer = self.join(a, 1, REAL_A)
        self.server.drop(peer, "timed out")
        back = self.join(a, 1)
        self.assertEqual(back.id, peer.id)
        self.assertEqual(back.real_addr, REAL_A)
        self.assertEqual(welcome_public(self.net, a), REAL_A)

    def test_flow_map_is_capped(self):
        self.server.proxy_flows_max = 8
        players = [self.join((AGENT, 41000 + i), 1 + i, ("203.0.113.%d" % (10 + i), 40000))
                   for i in range(3)]
        for i in range(40):  # headers of flows that never register
            self.send(proxy.header(("198.51.100.%d" % (i + 1), 40000), TUNNEL), (AGENT, 42000 + i))
            self.assertLessEqual(len(self.server.proxy_flows), 8)
            self.assertEqual(set(self.server.proxy_flow_owners), set(self.server.proxy_flows))
        for p in players:
            self.assertIn(p.addr, self.server.proxy_flows)
        self.assertIn((AGENT, 42039), self.server.proxy_flows)  # the newest kept, the oldest gone
        self.assertNotIn((AGENT, 42000), self.server.proxy_flows)
        more = [self.join((AGENT, 43000 + i), 100 + i, ("203.0.113.%d" % (100 + i), 40000))
                for i in range(5)]
        self.assertEqual(len(self.server.proxy_flows), 8)  # all of them registered players
        last = self.join((AGENT, 44000), 200, ("203.0.113.200", 40000))
        self.assertIsNone(last.real_addr)  # no room: relayed, as before
        self.assertEqual(welcome_public(self.net, last.addr), ("0.0.0.0", 0))
        self.assertTrue(all(p.real_addr for p in players + more))

    def test_limits_stay_those_of_the_agents_socket(self):
        # 20 players behind one public address (a CGNAT) through the agent: no per-address cap
        # by their real address, as before (MAX_PEERS_PER_IP is 16)
        peers = [self.join((AGENT, 45000 + i), 1 + i, ("203.0.113.7", 40000 + i)) for i in range(20)]
        self.assertTrue(all(p is not None and p.real_addr for p in peers))
        self.assertEqual(self.server.limit_key(peers[0].addr), peers[0].addr)
        self.assertEqual(self.server.per_ip, {AGENT: 20})

    def test_restart_keeps_the_address(self):
        """The agent's flow goes on through a restart without a new header: the address
        is kept with the registration (#93)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, srv.STATE_FILE)
            a, c = (AGENT, 40001), (AGENT, 40003)
            self.server.state_path = path
            self.join(a, 1, REAL_A)
            self.join(c, 3)
            self.assertTrue(self.server.save_state())
            with open(path) as f:
                saved = {d["addr"][1]: d["real"] for d in json.load(f)["peers"]}
            self.assertEqual(saved, {40001: list(REAL_A), 40003: None})
            again, net = self.new_server(), FakeTransport()
            again.connection_made(net)
            again.state_path = path
            self.assertEqual(again.load_state(), 2)
            self.assertEqual(again.by_addr[a].real_addr, REAL_A)
            again.datagram_received(hello(1, "P1"), a)  # no header: the flow went on
            again.datagram_received(hello(3, "P3"), c)
            self.assertEqual(welcome_public(net, a), REAL_A)
            self.assertEqual(welcome_public(net, c), ("0.0.0.0", 0))
            # a state file written before this feature has no "real": relayed, as before
            with open(path) as f:
                state = json.load(f)
            for d in state["peers"]:
                del d["real"]
            with open(path, "w") as f:
                json.dump(state, f)
            # the agent opened a new flow during the restart: another socket, with a header
            # (or without one, the setting off)
            again = self.new_server()
            again.connection_made(net)
            again.state_path = path
            again.load_state()
            moved, plain = (AGENT, 40011), (AGENT, 40013)
            again.datagram_received(proxy.header(REAL_B, TUNNEL), moved)
            again.datagram_received(hello(1, "P1"), moved)
            again.datagram_received(hello(3, "P3"), plain)
            self.assertEqual(again.by_addr[moved].real_addr, REAL_B)
            self.assertEqual(welcome_public(net, moved), REAL_B)
            again.datagram_received(hello(1, "P1"), a)  # its old socket no longer has it
            self.assertNotEqual(again.by_addr[a].id, again.by_addr[moved].id)
            self.assertIsNone(again.by_addr[plain].real_addr)
            self.assertEqual(again.by_addr[plain].id, self.server.by_addr[c].id)  # rebound
            old = self.new_server()
            old.state_path = path
            self.assertEqual(old.load_state(), 2)
            self.assertIsNone(old.by_addr[a].real_addr)
            self.assertEqual(old.proxy_flows, {})


    def test_reused_socket_without_header_forgets_previous_identity(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        peer = self.join(addr, 2)
        self.assertIsNone(peer.real_addr)
        self.assertEqual(welcome_public(self.net, addr), ("0.0.0.0", 0))
        self.assertNotIn(addr, self.server.proxy_flows)
        self.assertNotIn(addr, self.server.proxy_flow_owners)

    def test_dropped_flow_does_not_give_another_identity_its_address(self):
        addr = (AGENT, 40001)
        old = self.join(addr, 1, REAL_A)
        self.server.drop(old, "timed out")
        self.assertIsNone(self.join(addr, 2).real_addr)

    def test_restarted_server_does_not_give_new_identity_old_address(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live_state.json")
            self.server.state_path = path
            self.assertTrue(self.server.save_state())
            self.server = self.new_server()
            self.server.connection_made(self.net)
            self.server.state_path = path
            self.assertEqual(self.server.load_state(), 1)
            self.assertIsNone(self.join(addr, 2).real_addr)

    def test_restore_requires_current_proxy_trust(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live_state.json")
            self.server.state_path = path
            self.assertTrue(self.server.save_state())
            limits.configure_proxies("")
            again = self.new_server()
            again.connection_made(self.net)
            again.state_path = path
            self.assertEqual(again.load_state(), 1)
            self.assertIsNone(again.by_addr[addr].real_addr)
            self.assertEqual(again.proxy_flows, {})
            self.assertEqual(again.proxy_flow_owners, {})

    def test_fresh_header_same_public_address_accepts_new_identity(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        self.send(proxy.header(REAL_A, TUNNEL), addr)
        self.assertEqual(self.join(addr, 2).real_addr, REAL_A)
        self.assertEqual(welcome_public(self.net, addr), REAL_A)

    def test_zero_token_is_bound_not_pending(self):
        addr = (AGENT, 40001)
        self.join(addr, 0, REAL_A)
        self.assertIsNone(self.join(addr, 2).real_addr)

    def test_address_is_bound_to_xuid_as_well_as_token(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        packet = bytearray(hello(1))
        struct.pack_into(">Q", packet, 15, 0x0009000000000002)
        self.send(bytes(packet), addr)
        self.assertIsNone(self.server.by_addr[addr].real_addr)
        self.assertEqual(welcome_public(self.net, addr), ("0.0.0.0", 0))

    def test_restore_respects_proxy_flow_cap(self):
        self.join((AGENT, 40001), 1, REAL_A)
        self.join((AGENT, 40002), 2, REAL_B)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "live_state.json")
            self.server.state_path = path
            self.assertTrue(self.server.save_state())
            again = self.new_server()
            again.connection_made(self.net)
            again.proxy_flows_max = 1
            again.state_path = path
            self.assertEqual(again.load_state(), 2)
            self.assertEqual(len(again.proxy_flows), 1)
            self.assertEqual(set(again.proxy_flow_owners), set(again.proxy_flows))
            self.assertEqual(sum(p.real_addr is not None for p in again.peers.values()), 1)

    def test_rejected_hello_cannot_claim_fresh_header(self):
        addr = (AGENT, 40001)
        self.send(proxy.header(REAL_A, TUNNEL), addr)
        bad = bytearray(hello(99))
        struct.pack_into(">H", bad, 5, srv.VERSION + 1)
        self.send(bytes(bad), addr)
        self.assertIsNone(self.server.proxy_flow_owners[addr])
        self.server.max_peers = 0
        self.assertIsNone(self.join(addr, 98))
        self.assertIsNone(self.server.proxy_flow_owners[addr])
        self.server.max_peers = 1024
        self.assertEqual(self.join(addr, 1).real_addr, REAL_A)

    def test_bad_or_untrusted_header_cannot_reset_pending_or_owned_flow(self):
        addr = (AGENT, 40001)
        header = proxy.header(REAL_A, TUNNEL)
        self.send(header, addr)
        for owned in (False, True):
            if owned:
                self.join(addr, 1)
            before = (dict(self.server.proxy_flows), dict(self.server.proxy_flow_owners))
            self.send(header[:20], addr)  # trusted, but malformed
            self.send(proxy.header(REAL_B, TUNNEL), ("203.0.113.99", 40001))
            self.assertEqual((self.server.proxy_flows, self.server.proxy_flow_owners), before)
        self.assertEqual(self.join(addr, 1).real_addr, REAL_A)

    def test_revoked_proxy_address_is_not_advertised(self):
        addr = (AGENT, 40001)
        peer = self.join(addr, 1, REAL_A)
        limits.configure_proxies("")
        self.assertIsNone(self.server.public_of(peer))
        self.assertEqual(self.server.forward_header(peer),
                         struct.pack(">IIHIH", peer.id, 0, 0, 0, 0))
        self.join(addr, 1)
        self.assertIsNone(peer.real_addr)
        self.assertEqual(self.server.proxy_flows, {})
        self.assertEqual(self.server.proxy_flow_owners, {})


class HttpTest(unittest.IsolatedAsyncioTestCase):
    """The title servers' port through the agent (the test connects from 127.0.0.1, the
    trusted proxy)."""

    async def asyncSetUp(self):
        limits.configure_proxies("127.0.0.1")
        srv.http_conns.clear()
        self.writers = []
        self.server = srv.ForzaLive("test", 16)
        self.http = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                               "127.0.0.1", 0)
        self.port = self.http.sockets[0].getsockname()[1]
        self.token = patch.object(lsp, "ADMIN_TOKEN", "")
        self.token.start()

    async def asyncTearDown(self):
        for writer in self.writers:  # a failed test's kept-alive connections too
            writer.close()
        self.http.close()
        await self.http.wait_closed()
        self.token.stop()
        limits.configure_proxies("")
        self.server.events.db.close()

    @staticmethod
    def request(path="/status", keep=False):
        return (f"GET {path} HTTP/1.1\r\nHost: x\r\n"
                f"Connection: {'Keep-Alive' if keep else 'close'}\r\n\r\n").encode()

    async def call(self, prefix=b"", path="/status", pieces=1):
        """(status or None when the connection closed without an answer, body)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        data = prefix + self.request(path)
        step = -(-len(data) // pieces)
        for i in range(0, len(data), step):
            writer.write(data[i:i + step])
            await writer.drain()
            await asyncio.sleep(0.01)
        try:
            raw = await asyncio.wait_for(reader.read(), 5)
        except ConnectionResetError:  # closed with the request unread
            raw = b""
        writer.close()
        if not raw:
            return None, b""
        head, body = raw.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), body

    async def keep_open(self, prefix=b""):
        """A kept-alive connection that had one answer."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        writer.write(prefix + self.request(keep=True))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        length = int([h for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")][0]
                     .split(b":")[1])
        await reader.readexactly(length)
        return reader, writer

    async def test_with_and_without_a_header(self):
        status, body = await self.call(proxy.header(REAL_A, TUNNEL, proxy.STREAM))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["proxy_protocol"]["http_headers"], 1)
        status, body = await self.call()  # the setting off: as before
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["proxy_protocol"],
                         {"udp_headers": 0, "http_headers": 1, "rejected": 0, "flows": 0})
        status, _ = await self.call(proxy.header(REAL_A, TUNNEL, proxy.STREAM), pieces=7)
        self.assertEqual(status, 200)  # a header that arrives in pieces

    async def test_log_names_the_player_and_admin_is_not_granted(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            status, _ = await self.call(proxy.header(REAL_A, TUNNEL, proxy.STREAM),
                                        "/gift?to=x&credits=1")
            self.assertEqual(status, 403)  # a tunnelled request is never "this machine"
            status, _ = await self.call(proxy.header(("127.0.0.1", 40001), TUNNEL, proxy.STREAM),
                                        "/gift?to=x&credits=1")
            self.assertEqual(status, 403)  # nor is a header that claims loopback
            status, _ = await self.call(b"", "/gift?to=x&credits=1")
            self.assertEqual(status, 403)
        lines = [line for line in logs.output if "http  " in line]
        self.assertIn("http  203.0.113.7 GET /gift", lines[0])
        self.assertIn("http  127.0.0.1 GET /gift", lines[1])
        self.assertIn("http  127.0.0.1 GET /gift", lines[2])

    async def test_connections_count_under_the_real_address(self):
        _, w1 = await self.keep_open(proxy.header(REAL_A, TUNNEL, proxy.STREAM))
        _, w2 = await self.keep_open()
        self.assertEqual(srv.http_conns, {REAL_A[0]: 1, "127.0.0.1": 1})
        for w in (w1, w2):
            w.close()
            await w.wait_closed()
        for _ in range(100):
            if not srv.http_conns:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(srv.http_conns, {})

    async def test_per_address_cap_applies_to_the_real_address(self):
        with patch.object(srv, "HTTP_PER_IP", 3):
            header_a = proxy.header(REAL_A, TUNNEL, proxy.STREAM)
            kept = [await self.keep_open(header_a) for _ in range(3)]
            self.assertEqual(await self.call(header_a), (None, b""))  # a 4th from the same player
            self.assertEqual((await self.call(proxy.header(REAL_B, TUNNEL, proxy.STREAM)))[0], 200)
            kept += [await self.keep_open() for _ in range(4)]  # no header: the agent is exempt
            self.assertEqual((await self.call())[0], 200)
            for _, w in kept:
                w.close()
                await w.wait_closed()

    async def test_refused_headers(self):
        good = proxy.header(REAL_A, TUNNEL, proxy.STREAM)
        v1 = b"PROXY TCP4 203.0.113.7 192.0.2.10 40001 56678\r\n"
        for prefix in (good[:12] + b"\x11" + good[13:],  # version 1 in a binary header
                       good[:13] + b"\x12" + good[14:],  # a UDP header on TCP
                       good[:6] + b"\x00" + good[7:],  # signature
                       v1):
            self.assertEqual(await self.call(prefix), (None, b""))
        self.assertEqual(self.server.proxy_stats["rejected"], 4)
        self.assertEqual((await self.call())[0], 200)

    async def test_untrusted_header_is_refused_as_before(self):
        limits.configure_proxies("10.9.9.9")  # 127.0.0.1 is no proxy now
        self.assertEqual(await self.call(proxy.header(REAL_A, TUNNEL, proxy.STREAM)), (None, b""))
        self.assertEqual(self.server.proxy_stats["http_headers"], 0)
        self.assertEqual((await self.call())[0], 200)


if __name__ == "__main__":
    unittest.main()
