"""PROXY protocol v2 from the tunnel agent (proxy.py): a tunnelled player's public
address reaches the others for hole punching. The server never guesses whether a header is
there: without --proxy-protocol-udp / --proxy-protocol-tcp it is byte for byte the server it
was, and with them it takes a header only where the agent puts its own (first on a TCP
connection, first in a new UDP flow), never a player's own bytes mid-flow."""

import asyncio
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from forza_live.core import auth
from forza_live.core import ed25519
from forza_live.core import limits
from forza_live.title_services import lsp
from forza_live import server as srv
from forza_live.network import proxy
from forza_live.network import xtsw
from tests.support import FakeTransport

AGENT = "192.0.2.10"  # the tunnel agent's address, a trusted proxy
TUNNEL = ("192.0.2.10", 56677)  # the tunnel's public address (the header's destination)
REAL_A = ("81.2.69.142", 40001)  # players' public addresses (global unicast)
REAL_B = ("89.160.20.112", 52000)
FORGED = ("8.8.4.4", 3074)  # what a player claims in a header of its own


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


def xuid(token):
    return 0x0009000000000000 | token


def hello(token, name="P", room="r", lan=("10.0.0.5", 3074)):
    body = struct.pack(">HQQIH", srv.VERSION, token, xuid(token), srv.ip_to_u32(lan[0]), lan[1])
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


def aged(server, addr, seconds):
    """The flow of addr as if last heard `seconds` ago."""
    server.proxy_flows[addr].seen = time.monotonic() - seconds


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
        v6 = ("2a02:26f0::7", 40001)
        header = playit_v6(v6, ("2a02:26f0::1", 56677))
        self.assertEqual(len(header), 52)
        self.assertEqual(proxy.parse(header, proxy.DGRAM), (v6, b""))
        self.assertEqual(proxy.client_address(v6), v6)  # HTTP: logged and limited as is
        self.assertIsNone(proxy.client_address(v6, ipv4=True))  # UDP: the game has no IPv6
        mapped = playit_v6(("::ffff:81.2.69.142", 40001), ("::ffff:192.0.2.10", 56677))
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
            b"PROXY UDP4 81.2.69.142 192.0.2.10 40001 56677\r\n",  # version 1 text
            b"RLV1\x01",
        ]
        for data in bad:
            with self.subTest(data=data[:16].hex()), self.assertRaises(ValueError):
                proxy.parse(bytes(data), proxy.DGRAM)
        with self.assertRaises(ValueError):  # UDP's header on TCP
            proxy.parse(bytes(good), proxy.STREAM)

    def test_only_global_addresses(self):
        for ip in ("0.0.0.0", "127.0.0.1", "127.9.8.7", "10.0.0.1", "172.16.5.4", "192.168.1.1",
                   "100.64.0.1", "100.127.255.254", "169.254.3.4", "192.0.2.7", "198.51.100.7",
                   "203.0.113.7", "198.18.0.1", "224.0.0.1", "239.1.2.3", "240.0.0.1",
                   "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "2001:db8::7",
                   "ff02::1", "not an ip"):
            self.assertIsNone(proxy.client_address((ip, 1234)), ip)
        self.assertIsNone(proxy.client_address((REAL_A[0], 0)))
        self.assertIsNone(proxy.client_address(None))
        limits.configure_proxies("81.2.69.0/24")
        self.assertIsNone(proxy.client_address(REAL_A))  # a trusted proxy's own address
        limits.configure_proxies(AGENT)
        self.assertEqual(proxy.client_address(REAL_A, ipv4=True), REAL_A)
        self.assertEqual(proxy.client_address(("100.128.0.7", 5)), ("100.128.0.7", 5))


class ProxyServerTest(unittest.TestCase):
    """The UDP side; servers made with new_server(udp=...) are closed after each test."""

    def setUp(self):
        limits.configure_proxies(AGENT + ", 127.0.0.0/8")
        self.servers = []
        self.server, self.net = self.new_server()

    def tearDown(self):
        limits.configure_proxies("")
        for server in self.servers:
            server.events.db.close()

    def new_server(self, udp=True, started=True):
        """A server with --proxy-protocol-udp (udp), past its first PROXY_FLOW_IDLE s unless
        not started."""
        server, net = srv.ForzaLive("hi", 1024), FakeTransport()
        server.connection_made(net)
        server.proxy_udp = udp
        if started:
            server.proxy_unsure_until = 0
        self.servers.append(server)
        return server, net

    def send(self, data, addr, server=None):
        (server or self.server).datagram_received(data, addr)

    def join(self, addr, token, real=None, prefixed=False, server=None):
        """A player through the agent's socket addr; real: the address its header gives
        (agents from 0.16: a datagram of its own; prefixed: in front of every datagram)."""
        server = server or self.server
        header = proxy.header(real, TUNNEL) if real else b""
        if real and not prefixed:
            self.send(header, addr, server)
            header = b""
        self.send(header + hello(token, f"P{token}"), addr, server)
        return server.by_addr.get(addr)

    def signed(self, addr, token, slot=b"", who=None):
        """A HELLO of a game with signed requests (the friends feature; its auth block in the
        slot, or nothing yet) through the agent's socket addr, as the XUID of `who` (token)."""
        body = struct.pack(">HQQIH", srv.VERSION, token, xuid(who or token), 0, 0)
        body += bytes([1]) + b"r" + bytes([1]) + b"P" + bytes([srv.FEATURE_FRIENDS])
        self.send(srv.MAGIC + bytes([srv.HELLO]) + body + bytes([len(slot)]) + slot, addr)
        return body

    def sign_in(self, addr, token, real=None, who=None):
        """A game with signed requests signs in through addr: its first HELLO, then its answer
        to the challenge of the WELCOME that reached it. The registration, or None."""
        names = patch.object(auth, "SERVER_NAMES", set())  # signatures for any name
        names.start()
        self.addCleanup(names.stop)
        if real:
            self.send(proxy.header(real, TUNNEL), addr)
        self.net.sent.clear()
        body = self.signed(addr, token, who=who)
        welcome = [d for a, d in self.net.sent if a == addr and d[4] == srv.WELCOME]
        if not welcome:
            return None
        seed = bytes([token % 251] * 32)
        self.signed(addr, token, auth.hello_block(seed, "x", xuid(who or token), welcome[0][-16:], body), who)
        return self.server.by_addr.get(addr)


class OffTest(ProxyServerTest):
    """Without --proxy-protocol-udp: as before, whatever a datagram carries."""

    def test_headers_are_dropped_as_before(self):
        self.server.proxy_udp = False
        a, b = (AGENT, 40001), ("203.0.113.50", 5555)
        self.send(proxy.header(REAL_A, TUNNEL), a)
        self.send(proxy.header(REAL_A, TUNNEL) + hello(9, "P9"), a)  # not a game datagram
        self.assertEqual(self.net.sent, [])
        pa, pb = self.join(a, 1), self.join(b, 2)
        self.send(proxy.header(FORGED, TUNNEL), a)  # the review's injection: nothing
        self.assertIsNone(pa.real_addr)
        self.assertEqual(self.server.proxy_flows, {})
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))
        self.net.sent.clear()
        self.send(relay(pb.id), a)
        self.assertEqual(self.net.sent, [(b, srv.MAGIC + bytes([srv.FORWARD]) +
                                          struct.pack(">IIHIH", pa.id, 0, 0, 0, 0) +
                                          struct.pack(">HH", 1, 2) + b"hi")])
        status = self.server.status()
        for key in ("via_proxy_address", "proxy_protocol"):
            self.assertNotIn(key, status)
        self.assertNotIn("proxy_address", status["rooms"]["r"][0])
        self.assertNotIn("proxy_flows", self.server.snapshot())
        self.assertEqual(self.server.proxy_stats,
                         {"udp_headers": 0, "http_headers": 0, "rejected": 0})

    def test_welcome_bytes_as_before(self):
        self.server.proxy_udp = False
        self.server.http_port, self.server.proxy_http_port = 21201, 56678
        pa = self.join((AGENT, 40001), 1)
        welcome = [d for ad, d in self.net.sent if ad == pa.addr and d[4] == srv.WELCOME][0]
        # Everything up to the HTTP port is as before; the signing part (auth) follows it.
        before = (srv.MAGIC + bytes([srv.WELCOME]) +
                  struct.pack(">IHIHH", pa.id, self.server.epoch, 0, 0, 2) + b"hi" +
                  struct.pack(">H", 56678))
        self.assertEqual(welcome[:len(before)], before)


class UdpTest(ProxyServerTest):
    def test_header_datagram_then_hello(self):
        a = (AGENT, 40001)
        self.send(proxy.header(REAL_A, TUNNEL), a)
        self.assertEqual(self.net.sent, [])  # nothing answers the header itself
        self.assertEqual(self.server.proxy_flows[a].public, REAL_A)
        peer = self.join(a, 1)
        self.assertTrue(peer.proxied)
        self.assertEqual(peer.real_addr, REAL_A)
        self.assertEqual(self.server.proxy_flows[a].owner, (1, xuid(1)))
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
        self.assertEqual(self.server.proxy_flows[a].mode, srv.FLOW_PREFIXED)
        other = self.join(b, 2)
        self.send(proxy.header(REAL_A, TUNNEL) + relay(other.id), a)
        self.assertEqual(forwards(self.net, b),
                         [(peer.id, REAL_A[0], REAL_A[1], "10.0.0.5", 3074)])
        # the player's own header comes after the agent's: not a game datagram, dropped
        self.send(proxy.header(REAL_A, TUNNEL) + proxy.header(FORGED, TUNNEL) + relay(other.id), a)
        self.assertEqual(len(forwards(self.net, b)), 1)
        self.assertEqual(peer.real_addr, REAL_A)
        self.assertEqual(self.server.proxy_stats["udp_headers"], 3)

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
        self.assertNotIn(socket.inet_aton(AGENT), b"".join(d for _, d in self.net.sent))

    def test_injection_mid_flow_is_ignored(self):
        """The review's PoC (review/inject.py, poc_inject.py): a tunnelled player sends a
        header of its own, which a 0.16+ agent passes on unchanged from its flow's socket."""
        for genuine in (None, REAL_A):  # a flow without a header (setting off), and with one
            with self.subTest(genuine=genuine):
                server, net = self.new_server()
                s, d = (AGENT, 40001), ("203.0.113.50", 5555)
                attacker = self.join(s, 1, genuine, server=server)
                direct = self.join(d, 2, server=server)
                self.send(proxy.header(FORGED, TUNNEL), s, server)
                self.send(proxy.header(FORGED, TUNNEL) + hello(1, "P1"), s, server)
                self.send(relay(direct.id), s, server)
                self.assertEqual(attacker.real_addr, genuine)
                self.assertEqual(forwards(net, d)[-1][1:3], genuine or ("0.0.0.0", 0))
                self.send(hello(1, "P1"), s, server)
                self.assertEqual(welcome_public(net, s), genuine or ("0.0.0.0", 0))
                self.assertEqual(server.proxy_stats["rejected"], 2)
                self.assertNotIn(FORGED[0], json.dumps(server.snapshot()))

    def test_no_header_is_relayed_as_before(self):
        a, b = (AGENT, 40001), ("203.0.113.50", 5555)
        pa, pb = self.join(a, 1), self.join(b, 2)
        self.assertIsNone(pa.real_addr)
        self.assertEqual(self.server.proxy_flows[a].mode, srv.FLOW_PLAIN)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))
        self.assertEqual(self.server.forward_header(pa), struct.pack(">IIHIH", pa.id, 0, 0, 0, 0))
        self.send(relay(pb.id), a)
        self.assertEqual(forwards(self.net, b)[-1][1:], ("0.0.0.0", 0, "0.0.0.0", 0))

    def test_a_flow_from_before_gets_its_header_when_the_agent_opens_it_again(self):
        """A flow older than the setting stays relayed; once it was silent long enough for the
        agent to close it, the new flow's header is the first datagram again."""
        a = (AGENT, 40001)
        pa = self.join(a, 1)
        self.send(proxy.header(REAL_A, TUNNEL), a)  # 0 s later: mid-flow
        self.assertIsNone(pa.real_addr)
        aged(self.server, a, 70)  # the agent may still have it (up to 106 s)
        self.send(proxy.header(REAL_A, TUNNEL), a)
        self.assertNotIn(a, [x for x, f in self.server.proxy_flows.items() if f.public])
        aged(self.server, a, srv.PROXY_FLOW_IDLE + 1)
        self.server.drop(pa, "timed out")
        self.send(proxy.header(REAL_A, TUNNEL), a)  # a new flow
        back = self.join(a, 1)
        self.assertEqual(back.id, pa.id)
        self.assertEqual(back.real_addr, REAL_A)
        self.assertEqual(welcome_public(self.net, a), REAL_A)

    def test_headers_in_the_servers_first_minutes_are_not_taken(self):
        """A flow seen in the server's first PROXY_FLOW_IDLE s may be older than the server (its
        header went to the one before): its first datagram here may be the player's."""
        server, net = self.new_server(started=False)
        a = (AGENT, 40001)
        self.send(proxy.header(FORGED, TUNNEL), a, server)
        peer = self.join(a, 1, server=server)
        self.assertIsNone(peer.real_addr)
        self.assertEqual(server.proxy_flows[a].mode, srv.FLOW_PLAIN)
        self.assertEqual(server.proxy_stats["rejected"], 1)
        server.proxy_unsure_until = 0
        b = (AGENT, 40002)
        self.assertEqual(self.join(b, 2, REAL_B, server=server).real_addr, REAL_B)
        self.send(proxy.header(FORGED, TUNNEL), a, server)  # still mid-flow
        self.assertIsNone(peer.real_addr)

    def test_header_datagrams_count_against_the_flows_rate(self):
        a = (AGENT, 40001)
        header = proxy.header(REAL_A, TUNNEL)
        for _ in range(srv.PACKETS_PER_IP[1] + 100):
            self.send(header, a)
        self.assertGreater(self.server.dropped_packets, 0)
        self.assertIn(a, self.server.packet_buckets)
        before = self.server.dropped_packets
        b = self.join((AGENT, 40002), 2, REAL_B)  # another flow is not affected
        self.assertEqual(self.server.dropped_packets, before)
        self.assertEqual(b.real_addr, REAL_B)

    def test_malformed_header_from_the_agent(self):
        a = (AGENT, 40001)
        bad = bytearray(proxy.header(REAL_A, TUNNEL))
        bad[12] = 0x11  # version 1
        self.send(bytes(bad), a)
        self.send(bytes(bad) + hello(1, "P1"), a)  # mid-flow now: dropped
        self.assertEqual(self.net.sent, [])
        self.assertEqual(self.server.proxy_stats["rejected"], 2)
        self.join(a, 1)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))
        self.assertEqual(self.server.proxy_flows[a].mode, srv.FLOW_PLAIN)

    def test_untrusted_header_is_never_believed(self):
        x = ("203.0.113.99", 7000)
        self.send(proxy.header(REAL_A, TUNNEL), x)
        self.send(proxy.header(REAL_A, TUNNEL) + hello(1, "P1"), x)  # dropped, as before
        self.assertEqual(self.net.sent, [])
        self.assertEqual(self.server.proxy_flows, {})
        peer = self.join(x, 1)
        self.assertFalse(peer.proxied)
        self.assertEqual(welcome_public(self.net, x), x)  # its own address, as for any direct player

    def test_version_1_on_udp_is_ignored(self):
        a = (AGENT, 40001)
        v1 = b"PROXY UDP4 81.2.69.142 192.0.2.10 40001 56677\r\n"
        self.send(v1, a)
        self.send(v1 + hello(1, "P1"), a)
        self.assertEqual(self.net.sent, [])
        self.join(a, 1)
        self.assertEqual(welcome_public(self.net, a), ("0.0.0.0", 0))

    def test_header_without_a_usable_address(self):
        for header in (proxy.header(REAL_A, TUNNEL, command=proxy.LOCAL),
                       playit_v6(("2a02:26f0::7", 40001), ("2a02:26f0::1", 56677)),
                       proxy.header(("127.0.0.1", 40001), TUNNEL),
                       proxy.header(("10.1.2.3", 40001), TUNNEL),
                       proxy.header(("100.64.1.2", 40001), TUNNEL),
                       proxy.header((AGENT, 40001), TUNNEL)):
            server, net = self.new_server()
            a = (AGENT, 40001)
            self.send(header, a, server)
            peer = self.join(a, 1, server=server)
            self.assertIsNone(peer.real_addr)
            self.assertEqual(welcome_public(net, a), ("0.0.0.0", 0))
        mapped = playit_v6(("::ffff:81.2.69.142", 40001), ("::ffff:192.0.2.10", 56677))
        self.send(mapped, (AGENT, 40009))
        self.assertEqual(self.join((AGENT, 40009), 9).real_addr, REAL_A)

    def test_the_address_belongs_to_the_flows_first_game(self):
        """review/stale_flow.py: A leaves, B's game says HELLO on the same socket with no new
        header (setting turned off, or the header lost): B gets no address, not A's."""
        s = (AGENT, 40001)
        a = self.join(s, 1, REAL_A)
        self.send(srv.MAGIC + bytes([srv.BYE]), s)
        b = self.join(s, 2)
        self.assertIsNone(b.real_addr)
        self.assertEqual(welcome_public(self.net, s), ("0.0.0.0", 0))
        self.assertEqual(struct.unpack(">IIHIH", self.server.forward_header(b)), (b.id, 0, 0, 0, 0))
        self.assertIsNone(self.server.proxy_flows[s].public)  # dropped, for A too
        self.send(hello(1, "P1"), s)
        self.assertIsNone(self.server.by_addr[s].real_addr)
        self.assertNotEqual(a.id, b.id)

    def test_same_game_back_after_a_silence_keeps_its_address(self):
        """Silent longer than PEER_TIMEOUT, not long enough for the agent to close the flow:
        the same game (token) comes back on it without a new header."""
        a = (AGENT, 40001)
        peer = self.join(a, 1, REAL_A)
        self.server.drop(peer, "timed out")
        aged(self.server, a, 50)
        back = self.join(a, 1)
        self.assertEqual(back.id, peer.id)
        self.assertEqual(back.real_addr, REAL_A)

    def test_flows_expire(self):
        a, b = (AGENT, 40001), (AGENT, 40002)
        self.send(proxy.header(REAL_A, TUNNEL), a)  # a header, no player
        self.join(b, 2, REAL_B)
        aged(self.server, a, srv.PROXY_FLOW_IDLE + 1)
        self.send(hello(2, "P2"), b)  # each datagram keeps b's
        self.server.expire()
        self.assertNotIn(a, self.server.proxy_flows)
        self.assertIn(b, self.server.proxy_flows)

    def test_flow_map_is_capped(self):
        self.server.proxy_flows_max = 8
        players = [self.join((AGENT, 41000 + i), 1 + i, ("81.2.69.%d" % (10 + i), 40000))
                   for i in range(8)]
        self.assertEqual(len(self.server.proxy_flows), 8)
        late = (AGENT, 42000)
        self.send(proxy.header(REAL_B, TUNNEL), late)  # full: no address, nothing remembered
        self.assertEqual(len(self.server.proxy_flows), 8)
        self.assertEqual(self.server.proxy_stats["rejected"], 1)
        last = self.join(late, 200)
        self.assertIsNone(last.real_addr)
        self.send(proxy.header(REAL_B, TUNNEL), late)  # never believed while it is unknown
        self.assertIsNone(last.real_addr)
        self.assertTrue(all(p.real_addr for p in players))
        for p in players[:2]:  # room again
            aged(self.server, p.addr, srv.PROXY_FLOW_IDLE + 1)
        self.send(proxy.header(FORGED, TUNNEL), late)  # but late is mid-flow: not taken
        self.assertIsNone(last.real_addr)
        self.assertEqual(self.server.proxy_flows[late].mode, srv.FLOW_PLAIN)
        self.server.proxy_unsure_until = 0  # PROXY_FLOW_IDLE s later
        fresh = (AGENT, 42001)
        self.assertEqual(self.join(fresh, 300, REAL_B).real_addr, REAL_B)
        self.assertNotIn(players[0].addr, self.server.proxy_flows)

    # --- the address is bound to the accepted HELLO's token and XUID (from f8fde36) ---

    def flows_view(self):
        return {a: (f.mode, f.public, f.owner) for a, f in self.server.proxy_flows.items()}

    def test_reused_socket_without_header_forgets_previous_identity(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        peer = self.join(addr, 2)
        self.assertIsNone(peer.real_addr)
        self.assertEqual(welcome_public(self.net, addr), ("0.0.0.0", 0))
        self.assertEqual(self.flows_view()[addr], (srv.FLOW_HEADER, None, None))

    def test_legacy_flow_never_adopts_a_changed_public_address_mid_flow(self):
        addr = (AGENT, 40001)
        peer = self.join(addr, 1, REAL_A, prefixed=True)
        # A reused legacy socket may now carry a newer agent that passes client bytes raw.
        self.send(proxy.header(FORGED, TUNNEL) + hello(1), addr)
        self.assertIsNone(peer.real_addr)
        self.assertEqual(welcome_public(self.net, addr), ("0.0.0.0", 0))
        self.assertNotIn(FORGED[0], json.dumps(self.server.snapshot()))
        # Once invalidated, repeated prefixes cannot acquire another address until a new flow.
        self.send(proxy.header(REAL_A, TUNNEL) + hello(1), addr)
        self.assertIsNone(peer.real_addr)
        direct = ("203.0.113.50", 5555)
        other = self.join(direct, 2)
        self.send(proxy.header(REAL_A, TUNNEL) + relay(other.id, b"kept payload"), addr)
        self.assertEqual(forwards(self.net, direct)[-1][1:], ("0.0.0.0", 0, "0.0.0.0", 0))
        self.assertTrue(self.net.sent[-1][1].endswith(b"kept payload"))
        aged(self.server, addr, srv.PROXY_FLOW_IDLE + 1)
        self.send(proxy.header(REAL_B, TUNNEL) + hello(1), addr)
        self.assertEqual(peer.real_addr, REAL_B)

    def test_standalone_header_downgrades_legacy_flow_to_header_once(self):
        addr = (AGENT, 40001)
        peer = self.join(addr, 1, REAL_A, prefixed=True)
        self.server.state_dirty = False
        self.send(proxy.header(REAL_A, TUNNEL), addr)  # genuine newer agent header
        self.assertEqual(self.server.proxy_flows[addr].mode, srv.FLOW_HEADER)
        self.assertTrue(self.server.state_dirty)
        self.send(proxy.header(FORGED, TUNNEL) + hello(1), addr)
        self.assertEqual(peer.real_addr, REAL_A)

    def test_malformed_legacy_header_does_not_select_a_new_mode(self):
        addr = (AGENT, 40001)
        peer = self.join(addr, 1, REAL_A, prefixed=True)
        self.send(proxy.header(REAL_A, TUNNEL)[:20], addr)
        self.assertEqual(self.server.proxy_flows[addr].mode, srv.FLOW_PREFIXED)
        self.assertIsNone(peer.real_addr)

    def test_dropped_flow_does_not_give_another_identity_its_address(self):
        addr = (AGENT, 40001)
        old = self.join(addr, 1, REAL_A)
        self.server.drop(old, "timed out")
        self.assertIsNone(self.join(addr, 2).real_addr)

    def test_new_flow_same_public_address_accepts_new_identity(self):
        """A fresh header binds the next accepted HELLO, whoever it is; but only a header at
        the start of a flow (mid-flow it may be the player's own, and changes nothing)."""
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        self.send(proxy.header(REAL_A, TUNNEL), addr)  # mid-flow: ignored
        self.assertIsNone(self.join(addr, 2).real_addr)
        aged(self.server, addr, srv.PROXY_FLOW_IDLE + 1)  # the agent opened a new flow
        self.send(proxy.header(REAL_A, TUNNEL), addr)
        self.assertEqual(self.join(addr, 2).real_addr, REAL_A)
        self.assertEqual(welcome_public(self.net, addr), REAL_A)

    def test_zero_token_is_bound_not_pending(self):
        addr = (AGENT, 40001)
        self.join(addr, 0, REAL_A)
        self.assertEqual(self.server.proxy_flows[addr].owner, (0, xuid(0)))
        self.assertIsNone(self.join(addr, 2).real_addr)

    def test_address_is_bound_to_xuid_as_well_as_token(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        packet = bytearray(hello(1))
        struct.pack_into(">Q", packet, 15, xuid(2))
        self.send(bytes(packet), addr)
        self.assertIsNone(self.server.by_addr[addr].real_addr)
        self.assertEqual(welcome_public(self.net, addr), ("0.0.0.0", 0))

    def test_rejected_hello_cannot_claim_fresh_header(self):
        addr = (AGENT, 40001)
        self.send(proxy.header(REAL_A, TUNNEL), addr)
        bad = bytearray(hello(99))
        struct.pack_into(">H", bad, 5, srv.VERSION + 1)
        self.send(bytes(bad), addr)  # another protocol: refused
        self.assertIsNone(self.server.proxy_flows[addr].owner)
        self.server.max_peers = 0
        self.assertIsNone(self.join(addr, 98))  # server full: refused
        self.assertIsNone(self.server.proxy_flows[addr].owner)
        self.server.max_peers = 1024
        self.assertEqual(self.join(addr, 1).real_addr, REAL_A)

    def test_bad_or_untrusted_header_cannot_reset_pending_or_owned_flow(self):
        addr = (AGENT, 40001)
        header = proxy.header(REAL_A, TUNNEL)
        self.send(header, addr)
        for owned in (False, True):
            if owned:
                self.join(addr, 1)
            before = self.flows_view()
            self.send(header[:20], addr)  # trusted, but malformed
            self.send(proxy.header(REAL_B, TUNNEL), ("203.0.113.99", 40001))  # not trusted
            self.assertEqual(self.flows_view(), before)
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

    # --- the per-address limits count the player's own address, not the agent's socket ---

    def errors(self, addr):
        return [d[5] for a, d in self.net.sent if a == addr and d[4] == srv.ERROR]

    def test_players_per_address_through_the_tunnel(self):
        """Every game behind the agent used to be an address of its own (the agent's socket):
        one sender filled the server from as many source ports as it has players."""
        peers = [self.join((AGENT, 45000 + i), 1 + i, (REAL_A[0], 40000 + i)) for i in range(20)]
        self.assertEqual([p is not None for p in peers], [True] * srv.MAX_PEERS_PER_IP + [False] * 4)
        self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP})
        self.assertEqual(self.errors((AGENT, 45019)), [3])  # server full, for that address
        self.assertEqual(self.server.source_key(peers[0].addr), REAL_A[0])
        self.assertIsNotNone(self.join((AGENT, 46000), 100, REAL_B))  # another address has room
        self.server.drop(peers[0], "left")
        self.assertIsNotNone(self.join((AGENT, 45019), 20))  # and so has this one again
        self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP, REAL_B[0]: 1})

    def test_the_sign_in_rate_is_the_address_s(self):
        with patch.object(srv, "HELLOS_PER_IP", (0.001, 5)):
            joined = [self.join((AGENT, 45000 + i), 1 + i, (REAL_A[0], 40000 + i)) for i in range(8)]
            self.assertEqual(sum(p is not None for p in joined), 5)
            self.assertEqual(self.errors((AGENT, 45007)), [srv.ERR_SLOW_DOWN])
            self.assertIsNotNone(self.join((AGENT, 46000), 100, REAL_B))

    def test_games_without_an_address_share_a_pool(self):
        """Flows whose address is not known (older than the server, the table was full) cannot
        be told apart: together they get a share of the server, never all of it."""
        server, net = self.new_server()
        server.max_peers = 40
        self.assertEqual(server.pool_cap, 20)
        peers = [self.join((AGENT, 45000 + i), 1 + i, server=server) for i in range(30)]
        self.assertEqual(sum(p is not None for p in peers), 20)
        self.assertEqual(server.per_ip, {srv.POOL: 20})
        self.assertEqual([d[5] for a, d in net.sent if a == (AGENT, 45029) and d[4] == srv.ERROR], [3])
        # players with an address still get in, each under its own
        for i in range(10):
            self.assertIsNotNone(self.join((AGENT, 47000 + i), 200 + i, ("81.2.69.%d" % (10 + i), 40000),
                                           server=server))
        self.assertEqual(len(server.peers), 30)
        server.drop(peers[0], "left")
        self.assertIsNotNone(self.join((AGENT, 45029), 30, server=server))

    def test_a_tunnel_that_names_no_address_is_as_before(self):
        server, _ = self.new_server(udp=False)
        server.max_peers = 80
        self.assertIsNone(server.pool_cap)
        peers = [self.join((AGENT, 45000 + i), 1 + i, server=server) for i in range(30)]
        self.assertTrue(all(p is not None for p in peers))
        self.assertEqual(server.limit_key(peers[0].addr), peers[0].addr)

    def test_another_xuid_stays_under_its_flow_s_address(self):
        """A game that names another XUID loses the flow's address for hole punching, not its
        place under that address's limits."""
        addr = (AGENT, 40001)
        peer = self.join(addr, 1, REAL_A)
        packet = bytearray(hello(1))
        struct.pack_into(">Q", packet, 15, xuid(2))
        self.send(bytes(packet), addr)
        self.assertIsNone(peer.real_addr)
        self.assertEqual((peer.xuid, peer.source), (xuid(2), REAL_A[0]))
        self.assertEqual(self.server.per_ip, {REAL_A[0]: 1})

    def test_one_address_cannot_fill_the_flows_table(self):
        with patch.object(srv, "FLOWS_PER_ADDRESS", 4):
            for i in range(10):
                self.send(proxy.header((REAL_A[0], 40000 + i), TUNNEL), (AGENT, 45000 + i))
            self.assertEqual(len(self.server.proxy_flows), 4)
            self.assertEqual(self.server.flow_sources, {REAL_A[0]: 4})
            self.assertEqual(len(self.server.refused_flows), 6)  # the oldest: the newest four stay
            self.assertEqual(self.server.status()["proxy_protocol"]["refused_flows"], 6)
            # a refused flow's game does not register, with an address or without
            self.net.sent.clear()
            self.assertIsNone(self.join((AGENT, 45000), 10))
            self.assertEqual(self.net.sent, [])
            self.assertNotIn((AGENT, 45000), self.server.proxy_flows)
            self.assertIsNotNone(self.join((AGENT, 45009), 1))  # one of its four does
            self.assertIsNotNone(self.join((AGENT, 46000), 100, REAL_B))  # and another address's
            # flows the agent closed are forgotten, the refused ones too
            for i in range(6, 10):
                aged(self.server, (AGENT, 45000 + i), srv.PROXY_FLOW_IDLE + 1)
            for refused in self.server.refused_flows.values():
                refused[0] -= srv.PROXY_FLOW_IDLE + 1
            self.server.expire()
            self.assertEqual(self.server.flow_sources, {REAL_B[0]: 1})
            self.assertEqual(self.server.refused_flows, {})
            self.assertEqual(self.join((AGENT, 45009), 10, (REAL_A[0], 40009)).real_addr, (REAL_A[0], 40009))

    # --- an address is its sender's word, and often many players': what is done in its name
    # never keeps out a game that answers the challenge sent there ---

    def test_sign_ins_in_a_player_s_name_do_not_keep_its_game_out(self):
        """Registrations sent with a player's address as their source took that address's 16
        places, and the player's own game was told "server full" for as long as they were
        renewed."""
        with patch.object(srv, "HELLOS_PER_IP", (1000, 1000)):
            for i in range(srv.MAX_PEERS_PER_IP):  # they never see a WELCOME: no challenge answered
                self.assertIsNotNone(self.join((AGENT, 45000 + i), 100 + i, (REAL_A[0], 40000 + i)))
            self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP})
            mine = (AGENT, 46000)
            peer = self.sign_in(mine, 1, REAL_A)  # the player's own game
            self.assertEqual(self.errors(mine), [])
            self.assertIsNotNone(peer)
            self.assertTrue(peer.friends_ok)
            self.assertTrue(auth.answered(peer))
            self.assertEqual(peer.real_addr, REAL_A)
            # the one of the others that had waited longest gave way to it
            self.assertNotIn((AGENT, 45000), self.server.by_addr)
            self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP})
            # however many more come, with whatever they say they are: a game that answered stays
            for i in range(40):
                addr = (AGENT, 47000 + i)
                self.send(proxy.header((REAL_A[0], 42000 + i), TUNNEL), addr)
                self.signed(addr, 200 + i)
            self.assertIs(self.server.by_addr.get(mine), peer)
            self.assertTrue(peer.friends_ok)
            self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP})
            # a game before signed requests cannot answer: at a full address it is refused, as before
            self.assertIsNone(self.join((AGENT, 48000), 300, (REAL_A[0], 43000)))
            self.assertEqual(self.errors((AGENT, 48000)), [3])

    def test_games_that_answered_fill_an_address_as_before(self):
        with patch.object(srv, "HELLOS_PER_IP", (1000, 1000)), patch.object(auth, "FIRST_USE_SOURCE", (1000, 1000)):
            self.server.friends.store.first_use = auth.FirstUse()
            peers = [self.sign_in((AGENT, 45000 + i), 1 + i, (REAL_A[0], 40000 + i))
                     for i in range(srv.MAX_PEERS_PER_IP + 2)]
            self.assertEqual([p is not None and p.friends_ok for p in peers],
                             [True] * srv.MAX_PEERS_PER_IP + [False] * 2)
            self.assertEqual(self.errors((AGENT, 45001 + srv.MAX_PEERS_PER_IP)), [3])  # server full, for that address
            self.assertEqual(self.server.per_ip, {REAL_A[0]: srv.MAX_PEERS_PER_IP})

    def test_a_game_that_gave_way_waits_before_it_takes_a_place(self):
        """Two games that cannot answer (an XUID linked to another installation) do not take each
        other's place again and again."""
        with patch.object(srv, "HELLOS_PER_IP", (1000, 1000)), patch.object(srv, "MAX_PEERS_PER_IP", 2):
            a, b, c = (AGENT, 45001), (AGENT, 45002), (AGENT, 45003)
            for n, addr in enumerate((a, b)):
                self.send(proxy.header((REAL_A[0], 40001 + n), TUNNEL), addr)
                self.signed(addr, 1 + n)  # registered, and never answers
            first = self.server.by_addr[a]
            first.since -= 10  # the one registered longest ago
            self.send(proxy.header((REAL_A[0], 40003), TUNNEL), c)
            self.signed(c, 3)
            self.assertNotIn(a, self.server.by_addr)  # gave way to the newcomer
            self.assertIn(c, self.server.by_addr)
            self.net.sent.clear()
            self.signed(a, 1)  # it says HELLO again at once: not at another's cost, for now
            self.assertNotIn(a, self.server.by_addr)
            self.assertEqual(self.errors(a), [3])
            self.assertEqual(set(self.server.by_addr), {b, c})
            self.server.displaced[1] -= srv.DISPLACED_HOLD + 1
            self.signed(a, 1)
            self.assertIn(a, self.server.by_addr)

    def test_a_signed_in_game_changes_profile_at_its_own_rate(self):
        """Another profile on a registered game costs a sign-in. Counted against the address,
        that was spent by whoever signed in from there, or in its name."""
        mine = (AGENT, 46000)
        peer = self.sign_in(mine, 1, REAL_A)
        self.assertTrue(peer.friends_ok)
        with patch.object(srv, "HELLOS_PER_IP", (0.001, 3)):
            self.server.hello_buckets.clear()
            for i in range(6):
                self.join((AGENT, 45000 + i), 100 + i, (REAL_A[0], 40000 + i))
            self.assertEqual(self.errors((AGENT, 45005)), [srv.ERR_SLOW_DOWN])  # the address's are used up
            again = self.sign_in(mine, 1, who=2)  # the player's other profile
            self.assertEqual(self.errors(mine), [])
            self.assertIs(again, peer)
            self.assertEqual(peer.xuid, xuid(2))
            self.assertTrue(peer.friends_ok)

    def test_flows_in_an_address_s_name_do_not_keep_its_games_flows_out(self):
        """An address has so many flows: over that the newest was refused, and stayed refused
        for as long as its game kept sending. Flows opened in a player's name kept that
        player's own game out."""
        with patch.object(srv, "FLOWS_PER_ADDRESS", 4):
            for i in range(4):
                self.send(proxy.header((REAL_A[0], 40000 + i), TUNNEL), (AGENT, 45000 + i))
            self.assertEqual(self.server.flow_sources, {REAL_A[0]: 4})
            mine = (AGENT, 46000)
            peer = self.sign_in(mine, 1, REAL_A)  # the player's own game, on a new flow
            self.assertIsNotNone(peer)
            self.assertTrue(peer.friends_ok)
            self.assertEqual((peer.real_addr, peer.source), (REAL_A, REAL_A[0]))
            self.assertIn((AGENT, 45000), self.server.refused_flows)  # the oldest gave way to it
            # a flow whose game answered its challenge is not one of the address's four, nor refused
            for i in range(10):
                self.send(proxy.header((REAL_A[0], 41000 + i), TUNNEL), (AGENT, 47000 + i))
            self.assertIn(mine, self.server.proxy_flows)
            self.assertEqual(self.server.flow_sources, {REAL_A[0]: 5})
            self.assertEqual(len(self.server.refused_flows), 10)
            self.assertEqual(welcome_public(self.net, mine), REAL_A)

    def test_a_refused_flow_is_tried_again(self):
        with patch.object(srv, "FLOWS_PER_ADDRESS", 2):
            old = (AGENT, 45000)
            self.assertIsNotNone(self.join(old, 1, REAL_A))  # a game before signed requests
            for i in range(2):
                self.send(proxy.header((REAL_A[0], 41000 + i), TUNNEL), (AGENT, 47000 + i))
            self.assertIn(old, self.server.refused_flows)
            self.net.sent.clear()
            self.send(hello(1, "P1"), old)  # it keeps sending: dropped for now...
            self.send(relay(5), old)
            self.assertEqual(self.net.sent, [])
            # ...and tried again after a moment, as the address's newest flow with the address
            # its header gave: the one that is now the oldest gives way to it
            self.server.refused_flows[old][1] -= srv.REFUSED_RETRY + 1
            self.send(hello(1, "P1"), old)
            self.assertEqual(welcome_public(self.net, old), REAL_A)
            self.assertEqual(self.server.source_key(old), REAL_A[0])
            self.assertEqual(self.server.proxy_flows[old].mode, srv.FLOW_HEADER)
            self.assertEqual(set(self.server.refused_flows), {(AGENT, 47000)})
            self.assertEqual(self.server.flow_sources, {REAL_A[0]: 2})
            # a header in the middle of it is still a player's own bytes
            self.send(proxy.header(FORGED, TUNNEL), old)
            self.assertEqual(self.server.by_addr[old].real_addr, REAL_A)


class RestartTest(ProxyServerTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, srv.STATE_FILE)
        self.server.state_path = self.path

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def restarted(self, udp=True):
        server, net = self.new_server(udp=udp, started=False)  # its first minutes
        server.state_path = self.path
        server.load_state()
        return server, net

    def test_restart_keeps_the_address_of_the_flows_owner(self):
        a, c = (AGENT, 40001), (AGENT, 40003)
        self.join(a, 1, REAL_A)
        self.join(c, 3)
        self.assertTrue(self.server.save_state())
        with open(self.path) as f:
            state = json.load(f)
        self.assertEqual(state["proxy_flows"],
                         [[AGENT, 40001, srv.FLOW_HEADER, list(REAL_A), [1, xuid(1)]]])
        self.assertNotIn("real", state["peers"][0])
        again, net = self.restarted()
        self.assertEqual(again.by_addr[a].real_addr, REAL_A)
        self.send(hello(1, "P1"), a, again)  # no header: the flow went on
        self.send(hello(3, "P3"), c, again)
        self.assertEqual(welcome_public(net, a), REAL_A)
        self.assertEqual(welcome_public(net, c), ("0.0.0.0", 0))
        self.send(proxy.header(FORGED, TUNNEL), a, again)  # restored flows are mid-flow
        self.assertEqual(again.by_addr[a].real_addr, REAL_A)

    def test_restored_players_are_counted_under_their_address(self):
        a, b = (AGENT, 40001), (AGENT, 40002)
        self.join(a, 1, REAL_A)
        self.join(b, 2)  # no header: no address
        self.assertTrue(self.server.save_state())
        new, _ = self.restarted()
        self.assertEqual(new.per_ip, {REAL_A[0]: 1, srv.POOL: 1})
        self.assertEqual(new.flow_sources, {REAL_A[0]: 1})
        self.assertEqual(new.source_key(a), REAL_A[0])

    def test_restart_with_another_game_on_the_socket(self):
        """review/restart_reuse.py: the agent's socket of a restored player now carries another
        game (its header went to the stopped server): it gets no address, not the old one's."""
        s = (AGENT, 40001)
        self.join(s, 1, REAL_A)
        self.server.save_state()
        again, net = self.restarted()
        self.send(hello(2, "P2"), s, again)
        newcomer = again.by_addr[s]
        self.assertEqual(newcomer.token, 2)
        self.assertIsNone(newcomer.real_addr)
        self.assertEqual(welcome_public(net, s), ("0.0.0.0", 0))
        self.send(hello(1, "P1"), (AGENT, 40002), again)  # the first comes back elsewhere
        self.assertIsNone(again.by_addr[(AGENT, 40002)].real_addr)

    def test_restored_legacy_flow_accepts_newer_agent_format_without_injection(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A, prefixed=True)
        self.assertTrue(self.server.save_state())
        again, net = self.restarted()
        # Upgrade <=0.15.26 -> >=0.16.5, with source socket reuse during the restart.
        self.send(proxy.header(REAL_A, TUNNEL), addr, again)
        self.send(hello(1), addr, again)
        self.assertEqual(again.by_addr[addr].real_addr, REAL_A)
        self.send(proxy.header(FORGED, TUNNEL) + hello(1), addr, again)
        self.assertEqual(again.by_addr[addr].real_addr, REAL_A)
        self.assertEqual(again.proxy_flows[addr].mode, srv.FLOW_HEADER)
        self.assertEqual(welcome_public(net, addr), REAL_A)
        self.assertTrue(again.save_state())
        third, _ = self.restarted()
        self.assertEqual(third.proxy_flows[addr].mode, srv.FLOW_HEADER)
        self.send(proxy.header(FORGED, TUNNEL) + hello(1), addr, third)
        self.assertEqual(third.by_addr[addr].real_addr, REAL_A)

    def test_restored_legacy_flow_with_missed_new_header_cannot_learn_client_header(self):
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A, prefixed=True)
        self.assertTrue(self.server.save_state())
        again, net = self.restarted()
        # The newer agent's genuine standalone header went to the stopped server.
        self.send(proxy.header(FORGED, TUNNEL) + hello(1), addr, again)
        self.assertIsNone(again.by_addr[addr].real_addr)
        self.assertEqual(welcome_public(net, addr), ("0.0.0.0", 0))
        self.assertNotIn(FORGED[0], json.dumps(again.snapshot()))

    def test_a_restored_flow_owned_by_another_game(self):
        s = (AGENT, 40001)
        self.join(s, 1, REAL_A)
        self.server.save_state()
        with open(self.path) as f:
            state = json.load(f)
        state["proxy_flows"][0][4] = [99, xuid(99)]  # the address belongs to another game
        with open(self.path, "w") as f:
            json.dump(state, f)
        again, net = self.restarted()
        self.assertIsNone(again.by_addr[s].real_addr)
        self.send(hello(1, "P1"), s, again)
        self.assertEqual(welcome_public(net, s), ("0.0.0.0", 0))

    def test_restarted_server_does_not_give_new_identity_old_address(self):  # from f8fde36
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        self.assertTrue(self.server.save_state())
        again, _ = self.restarted()
        self.assertIsNone(self.join(addr, 2, server=again).real_addr)

    def test_restore_requires_current_proxy_trust(self):  # from f8fde36
        addr = (AGENT, 40001)
        self.join(addr, 1, REAL_A)
        self.assertTrue(self.server.save_state())
        limits.configure_proxies("")
        again, _ = self.restarted()
        self.assertEqual(len(again.peers), 1)
        self.assertIsNone(again.by_addr[addr].real_addr)
        self.assertEqual(again.proxy_flows, {})

    def test_restore_respects_proxy_flow_cap(self):  # from f8fde36
        self.join((AGENT, 40001), 1, REAL_A)
        self.join((AGENT, 40002), 2, REAL_B)
        self.assertTrue(self.server.save_state())
        again, _ = self.new_server(started=False)
        again.proxy_flows_max = 1
        again.state_path = self.path
        self.assertEqual(again.load_state(), 2)
        self.assertEqual(len(again.proxy_flows), 1)
        self.assertEqual(sum(p.real_addr is not None for p in again.peers.values()), 1)

    def test_a_pending_flow_is_not_restored(self):
        """A header with no accepted HELLO yet: after a restart any game could claim it."""
        self.send(proxy.header(REAL_A, TUNNEL), (AGENT, 40001))
        self.join((AGENT, 40002), 2)
        self.assertTrue(self.server.save_state())
        again, _ = self.restarted()
        self.assertEqual(again.proxy_flows, {})

    def test_flows_opened_while_the_server_was_down(self):
        """Their header went to the stopped server: the first datagram the new one hears
        may be the player's, so none is taken in its first minutes."""
        self.server.save_state()
        again, net = self.restarted()
        s = (AGENT, 40005)
        self.send(proxy.header(FORGED, TUNNEL), s, again)
        self.send(hello(5, "P5"), s, again)
        self.assertIsNone(again.by_addr[s].real_addr)

    def test_a_restored_player_moved_to_a_new_flow(self):
        s, moved = (AGENT, 40001), (AGENT, 40011)
        first = self.join(s, 1, REAL_A)
        self.server.save_state()
        again, net = self.restarted()
        again.proxy_unsure_until = 0  # later: a new flow, with its header
        self.send(proxy.header(REAL_B, TUNNEL), moved, again)
        self.send(hello(1, "P1"), moved, again)
        self.assertEqual(again.by_addr[moved].id, first.id)  # rebound (#93)
        self.assertEqual(again.by_addr[moved].real_addr, REAL_B)

    def test_without_the_option_the_state_is_as_before(self):
        a = (AGENT, 40001)
        self.join(a, 1, REAL_A)
        self.server.save_state()
        old, _ = self.restarted(udp=False)
        self.assertIsNone(old.by_addr[a].real_addr)
        self.assertEqual(old.proxy_flows, {})
        self.assertNotIn("proxy_flows", old.snapshot())
        # a state file of an older server (no proxy_flows) with the option on: relayed
        with open(self.path) as f:
            state = json.load(f)
        del state["proxy_flows"]
        with open(self.path, "w") as f:
            json.dump(state, f)
        again, _ = self.restarted()
        self.assertIsNone(again.by_addr[a].real_addr)

    def test_a_bad_proxy_flows_entry_keeps_the_registrations(self):
        self.join((AGENT, 40001), 1, REAL_A)
        self.server.save_state()
        with open(self.path) as f:
            state = json.load(f)
        state["proxy_flows"] = [[AGENT, 40001, "header"], "junk"]
        with open(self.path, "w") as f:
            json.dump(state, f)
        with self.assertLogs("forza-live", "WARNING"):
            again, _ = self.restarted()
        self.assertEqual(len(again.peers), 1)
        self.assertIsNone(again.by_addr[(AGENT, 40001)].real_addr)


class HttpTest(unittest.IsolatedAsyncioTestCase):
    """The title servers' port through the agent (the test connects from 127.0.0.1, the
    trusted proxy), with --proxy-protocol-tcp unless a test turns it off."""

    async def asyncSetUp(self):
        limits.configure_proxies("127.0.0.1")
        srv.http_conns.clear()
        self.writers = []
        self.server = srv.ForzaLive("test", 16)
        self.server.proxy_tcp = True
        self.http = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                               "127.0.0.1", 0)
        self.port = self.http.sockets[0].getsockname()[1]
        self.token = patch.object(lsp, "ADMIN_TOKEN", "")
        self.token.start()
        names = patch.object(auth, "SERVER_NAMES", set())  # signatures for any name, whatever ran before
        names.start()
        self.addCleanup(names.stop)
        auth._budget[:] = [float(auth.VERIFY_RATE[1]), time.monotonic()]
        srv.http_open.clear()

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

    def stream(self, source=REAL_A, **kw):
        return proxy.header(source, TUNNEL, proxy.STREAM, **kw)

    async def test_the_header_is_required(self):
        status, body = await self.call(self.stream())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["proxy_protocol"]["http_headers"], 1)
        self.assertEqual(await self.call(), (None, b""))  # no header from the agent: refused
        self.assertEqual(self.server.proxy_stats["rejected"], 1)
        status, _ = await self.call(self.stream(), pieces=7)
        self.assertEqual(status, 200)  # a header that arrives in pieces
        status, _ = await self.call(self.stream(command=proxy.LOCAL))
        self.assertEqual(status, 200)  # LOCAL: the agent's own, as itself

    async def test_off_is_as_before(self):
        self.server.proxy_tcp = False
        status, body = await self.call()
        self.assertEqual(status, 200)
        self.assertNotIn("proxy_protocol", json.loads(body))
        self.assertEqual(await self.call(self.stream()), (None, b""))  # not an HTTP request
        # review/inject.py: a client's own header is never counted under its forged address
        _, w = await self.keep_open()
        self.assertEqual(srv.http_conns, {"127.0.0.1": 1})
        w.close()

    async def test_log_names_the_player_and_admin_is_not_granted(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            status, _ = await self.call(self.stream(), "/gift?to=x&credits=1")
            self.assertEqual(status, 403)  # a tunnelled request is never "this machine"
            status, _ = await self.call(self.stream(("127.0.0.1", 40001)), "/gift?to=x&credits=1")
            self.assertEqual(status, 403)  # nor is a header that claims loopback
        lines = [line for line in logs.output if "http  " in line]
        self.assertIn("http  81.2.69.142 GET /gift", lines[0])
        self.assertIn("http  127.0.0.1 GET /gift", lines[1])

    async def test_connections_count_under_the_real_address(self):
        _, w1 = await self.keep_open(self.stream())
        _, w2 = await self.keep_open(self.stream(command=proxy.LOCAL))
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
            kept = [await self.keep_open(self.stream()) for _ in range(3)]
            self.assertEqual(await self.call(self.stream()), (None, b""))  # a 4th from that player
            self.assertEqual((await self.call(self.stream(REAL_B)))[0], 200)
            kept += [await self.keep_open(self.stream(command=proxy.LOCAL)) for _ in range(4)]
            for _, w in kept:
                w.close()
                await w.wait_closed()

    async def test_refused_headers(self):
        good = self.stream()
        v1 = b"PROXY TCP4 81.2.69.142 192.0.2.10 40001 56678\r\n"
        for prefix in (good[:12] + b"\x11" + good[13:],  # version 1 in a binary header
                       good[:13] + b"\x12" + good[14:],  # a UDP header on TCP
                       good[:6] + b"\x00" + good[7:],  # signature
                       v1, b"\r\n\r\n"):
            self.assertEqual(await self.call(prefix), (None, b""))
        self.assertEqual(self.server.proxy_stats["rejected"], 5)
        self.assertEqual((await self.call(good))[0], 200)

    # --- connections that cost their sender one request do not hold the server's ---

    async def signed_call(self, reader, writer, seed, xuid):
        """A title service read signed by the player's key on a kept-alive connection."""
        target = "/Services.Horizon/o.xtsw"
        headers = {"x-classname": "Forza.WebServices.Hopper, Forza.WebServices", "x-method": "GetList",
                   "x-userid": "0x%x" % xuid}
        body = bytes(xtsw.encode(xtsw.HopperGetList_Params, {"maxCategoryResults": 2, "maxHopperResults": 32}))
        headers[auth.HEADER] = auth.auth_header(seed, "x", xuid, "POST", target, body, headers)
        head = "POST %s HTTP/1.1\r\nHost: x\r\nConnection: Keep-Alive\r\nContent-Length: %d\r\n" % (
            target, len(body))
        head += "".join("%s: %s\r\n" % kv for kv in headers.items())
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        length = int([h for h in answer.split(b"\r\n") if h.lower().startswith(b"content-length")][0]
                     .split(b":")[1])
        await reader.readexactly(length)
        return int(answer.split()[1])

    async def a_game(self, n=1):
        """A kept-alive connection that made a call signed by its player's own key."""
        seed, xuid = bytes([n] * 32), 0x0009000000000100 + n
        self.server.friends.store.get(xuid, create=True)["pub"] = ed25519.public_key(seed).hex()
        reader, writer = await self.keep_open(self.stream((REAL_B[0], 41000 + n)))
        self.assertEqual(await self.signed_call(reader, writer, seed, xuid), 200)
        return reader, writer

    async def closed(self, reader, within=2.0):
        try:
            return await asyncio.wait_for(reader.read(), within) == b""
        except asyncio.TimeoutError:
            return False

    async def test_a_new_connection_takes_the_place_of_an_idle_unproven_one(self):
        """Every connection may idle for hours after one request: as many of them as the server
        takes held every slot, and each new connection was closed at once."""
        with patch.object(srv, "HTTP_TOTAL", 5), patch.object(srv, "HTTP_UNPROVEN", 3):
            game, game_writer = await self.a_game()
            idle = [await self.keep_open(self.stream((REAL_A[0], 40000 + i))) for i in range(3)]
            self.assertEqual(len(srv.http_open), 4)
            # the unproven ones are at their share: the next visitor is served all the same...
            self.assertEqual((await self.call(self.stream(REAL_B)))[0], 200)
            # ...in place of the one that waited longest
            self.assertTrue(await self.closed(idle[0][0]))
            self.assertFalse(await self.closed(idle[2][0], 0.2))
            self.assertFalse(await self.closed(game, 0.2))  # never a game's
            self.assertEqual(sum(1 for c in srv.http_open if not c.proved), 2)
            # and so on, however many come: the visitors that are left answer for each other
            for _ in range(6):
                self.assertEqual((await self.call(self.stream(REAL_B)))[0], 200)
            game_writer.write(self.request(keep=True))  # the game's connection is still its own
            await game_writer.drain()
            self.assertIn(b"200", await asyncio.wait_for(game.readuntil(b"\r\n\r\n"), 5))
        for _, w in idle + [(game, game_writer)]:
            w.close()

    async def test_a_connection_that_does_not_read_its_answer_gives_way_too(self):
        with patch.object(srv, "HTTP_TOTAL", 2), patch.object(srv, "HTTP_UNPROVEN", 2):
            stuck = []
            for i in range(2):  # each asks, and reads nothing
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                self.writers.append(writer)
                writer.write(self.stream((REAL_A[0], 40000 + i)) + self.request(keep=True))
                await writer.drain()
                stuck.append((reader, writer))
            await asyncio.sleep(0.1)
            self.assertEqual((await self.call(self.stream(REAL_B)))[0], 200)

    async def test_the_share_kept_for_games(self):
        with patch.object(srv, "HTTP_TOTAL", 3), patch.object(srv, "HTTP_UNPROVEN", 2):
            games = [await self.a_game(n) for n in (1, 2, 3)]  # each began unproven, in the others' share
            self.assertEqual(await self.call(self.stream()), (None, b""))  # full of games: refused
            self.assertTrue(all(c.proved for c in srv.http_open))
            self.assertFalse(await self.closed(games[0][0], 0.2))
        for _, w in games:
            w.close()

    async def test_one_key_does_not_hold_the_connections(self):
        """A connection with one call signed by a player's key was kept for good, and one key
        signed any number of them: when they were all the server takes, every new connection
        was closed unanswered."""
        with patch.object(srv, "HTTP_TOTAL", 8), patch.object(srv, "HTTP_UNPROVEN", 6):
            seed, player = bytes([9] * 32), 0x0009000000000109
            self.server.friends.store.get(player, create=True)["pub"] = ed25519.public_key(seed).hex()
            held = []
            for i in range(8):
                reader, writer = await self.keep_open(self.stream((REAL_B[0], 41000 + i)))
                self.assertEqual(await self.signed_call(reader, writer, seed, player), 200)
                held.append((reader, writer))
            self.assertTrue(all(c.proved for c in srv.http_open))
            # a visitor is served all the same...
            self.assertEqual((await self.call(self.stream()))[0], 200)
            # ...in place of the one of that key's connections that had waited longest: the key
            # keeps the few it used last
            self.assertTrue(await self.closed(held[0][0]))
            self.assertEqual(len(srv.http_kept(self.server)), srv.HTTP_PER_KEY)
            for _ in range(6):
                self.assertEqual((await self.call(self.stream()))[0], 200)
            for reader, writer in held[-srv.HTTP_PER_KEY:]:  # the player's working connections
                self.assertEqual(await self.signed_call(reader, writer, seed, player), 200)
        for _, w in held:
            w.close()

    async def test_a_game_s_connection_is_kept_while_its_player_is_here(self):
        """Kept for a player means for as long as the player's game runs, or for a while after
        its last signed call: not for the hours a connection may idle."""
        with patch.object(srv, "HTTP_TOTAL", 3), patch.object(srv, "HTTP_UNPROVEN", 2):
            games = [await self.a_game(n) for n in (1, 2, 3)]
            for c in srv.http_open:  # hours after their last signed calls
                c.used = getattr(c, "used", 0.0) - 6 * 3600
            # the first one's game is running (signed in on UDP with its key); the others' left
            running = srv.Peer(7, ("192.0.2.10", 40001), 1, 0x0009000000000101, "P", "r", (0, 0))
            running.friends_ok = True
            self.server.peers[running.id] = running
            self.assertEqual((await self.call(self.stream()))[0], 200)  # served: one of the others gave way
            self.assertTrue(await self.closed(games[1][0]))
            self.assertFalse(await self.closed(games[0][0], 0.2))
            self.assertEqual(await self.signed_call(*games[0], bytes([1] * 32), 0x0009000000000101), 200)
        for _, w in games:
            w.close()

    async def test_connections_kept_busy_give_way_too(self):
        """Only a connection that waited gave way. Connections kept being answered (a signed
        call that could not be checked was tried again for a second, and the next one was sent
        at once) held every place for as long as their sender liked."""
        with patch.object(srv, "HTTP_TOTAL", 3), patch.object(srv, "HTTP_UNPROVEN", 3):
            release = asyncio.Event()

            async def slow(*_):
                await release.wait()
                return 404, "text/plain", b"late"

            with patch.object(srv, "lsp_answer", slow):
                busy = []
                try:
                    for i in range(3):
                        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                        self.writers.append(writer)
                        writer.write(self.stream((REAL_A[0], 40000 + i)) + self.request("/storage/x", keep=True))
                        await writer.drain()
                        busy.append((reader, writer))
                        await asyncio.sleep(0.05)
                    self.assertEqual([entry[1] for entry in srv.http_open.values()], [None] * 3)
                    self.assertEqual((await self.call(self.stream(REAL_B)))[0], 200)
                    self.assertTrue(await self.closed(busy[0][0]))  # the one open longest
                finally:  # the handlers end before the server is closed
                    release.set()
                    await asyncio.sleep(0.05)

    async def test_ipv6_addresses_count_by_their_64(self):
        """A subscriber has a whole /64: each of its addresses was an address of its own for
        the connections of one address, its signature checks and its wrong tokens."""
        lsp._token_fails.clear()
        self.addCleanup(lsp._token_fails.clear)

        def v6(host, net=2):
            return playit_v6(("2a02:26f0:1:%x::%x" % (net, host), 40000), ("2a02:26f0::1", 56678), 0x21)

        with patch.object(srv, "HTTP_PER_IP", 3):
            kept = [await self.keep_open(v6(1 + i)) for i in range(3)]
            self.assertEqual(srv.http_conns, {"2a02:26f0:1:2::/64": 3})
            self.assertEqual({c.address for c in srv.http_open}, {"2a02:26f0:1:2::/64"})
            self.assertEqual(await self.call(v6(9)), (None, b""))  # a 4th from that /64
            self.assertEqual((await self.call(v6(1, net=3)))[0], 200)  # another's
            for _, w in kept:
                w.close()
        with patch.object(lsp, "ADMIN_TOKEN", "the-operator-s-own-token"), \
                self.assertLogs("forza-live", "WARNING") as logs:
            for i in range(lsp.TOKEN_FAILS):
                self.assertEqual((await self.call(v6(16 + i), "/gift?to=x&credits=1&token=guess-%d" % i))[0], 403)
            path = "/gift?to=nobody&credits=1&token=the-operator-s-own-token"
            self.assertEqual((await self.call(v6(99), path))[0], 403)  # nothing is taken from that /64 now
            self.assertEqual((await self.call(v6(1, net=3), path))[0], 404)  # taken from another: no such player
        self.assertIn("a wrong admin token from 2a02:26f0:1:2::/64", logs.output[0])

    async def test_a_handler_that_fails_does_not_log_the_token(self):
        token = "the-operator-s-own-token"
        with patch.object(lsp, "ADMIN_TOKEN", token), patch.object(lsp, "gift_request", side_effect=RuntimeError), \
                self.assertLogs("forza-live", "INFO") as logs:
            for query in ("to=nobody&credits=1&token=" + token, "%74oken=" + token + "&to=nobody"):
                status, _ = await self.call(self.stream(), "/gift?" + query)
                self.assertEqual(status, 500)  # the token was taken, and the handler failed
        text = "\n".join(logs.output)
        self.assertNotIn(token, text)
        self.assertIn("LSP GET /gift?to=nobody&credits=1&token=... failed", text)
        self.assertIn("LSP GET /gift?%74oken=...&to=nobody failed", text)

    async def test_the_operator_s_token_is_not_logged(self):
        with patch.object(lsp, "ADMIN_TOKEN", "the-operator-s-own-token"), \
                self.assertLogs("forza-live", "INFO") as logs:
            status, _ = await self.call(self.stream(), "/gift?to=nobody&credits=1&token=the-operator-s-own-token")
            self.assertEqual(status, 404)  # the token was taken: no such player
            status, _ = await self.call(self.stream(), "/notify?token=not-the-token&to=nobody")
            self.assertEqual(status, 403)
        text = "\n".join(logs.output)
        self.assertNotIn("the-operator-s-own-token", text)
        self.assertNotIn("not-the-token", text)
        self.assertIn("GET /gift?to=nobody&credits=1&token=...", text)
        self.assertIn("a wrong admin token from %s" % REAL_A[0], text)

    async def test_untrusted_connections_need_no_header(self):
        limits.configure_proxies("10.9.9.9")  # 127.0.0.1 is no proxy now
        self.assertEqual((await self.call())[0], 200)
        self.assertEqual(await self.call(self.stream()), (None, b""))  # as before
        self.assertEqual(self.server.proxy_stats["http_headers"], 0)


if __name__ == "__main__":
    unittest.main()
