"""PROXY protocol v2 from the tunnel agent (forza_proxy.py): a tunnelled player's public
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

import forza_limits as limits
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_proxy as proxy

AGENT = "192.168.100.105"  # the tunnel agent's address, a trusted proxy
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


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((addr, data))


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
        self.assertEqual(welcome, srv.MAGIC + bytes([srv.WELCOME]) +
                         struct.pack(">IHIHH", pa.id, self.server.epoch, 0, 0, 2) + b"hi" +
                         struct.pack(">H", 56678))


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

    def test_limits_stay_those_of_the_agents_socket(self):
        # 20 players behind one public address (a CGNAT) through the agent: no per-address cap
        # by their real address, as before (MAX_PEERS_PER_IP is 16)
        peers = [self.join((AGENT, 45000 + i), 1 + i, (REAL_A[0], 40000 + i)) for i in range(20)]
        self.assertTrue(all(p is not None and p.real_addr for p in peers))
        self.assertEqual(self.server.limit_key(peers[0].addr), peers[0].addr)
        self.assertEqual(self.server.per_ip, {AGENT: 20})


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

    async def test_untrusted_connections_need_no_header(self):
        limits.configure_proxies("10.9.9.9")  # 127.0.0.1 is no proxy now
        self.assertEqual((await self.call())[0], 200)
        self.assertEqual(await self.call(self.stream()), (None, b""))  # as before
        self.assertEqual(self.server.proxy_stats["http_headers"], 0)


if __name__ == "__main__":
    unittest.main()
