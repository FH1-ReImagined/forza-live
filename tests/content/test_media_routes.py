"""The limits of the public media routes (#63; core/media.py and server.send): a photo or a
video sent in pieces, the reader that takes none dropped after server.DRAIN_SECONDS, and each
address's own budget of requests and bytes in front of the server's. Who has no budget of its
own: the home network, this machine, --website-address and a tunnel that names no address.
Every photo and video here is made by the tests.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import json
import math
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from forza_live.content import community
from forza_live.content import videos
from forza_live.core import auth
from forza_live.core import limits
from forza_live.core import media
from forza_live.network import proxy
from forza_live import server as srv
from forza_live.title_services import lsp
from tests.content import test_community as tc
from tests.content import test_videos as tv

LAN = tc.LAN
A, B = ("81.2.69.142", 40001), ("89.160.20.112", 52000)  # public addresses (global unicast)
V6 = "2a02:26f0:1:2::%x"  # addresses of one public /64
TUNNEL = ("192.0.2.10", 56677)  # the tunnel's public address (a PROXY header's destination)


class Clock:
    """media's clock, moved by the test."""
    now = 1000.0

    def monotonic(self):
        return self.now


class Writer:
    """The part of a StreamWriter server.send uses. drain() takes `delay` seconds (None: it
    never returns, a reader that takes nothing); the transport is closing once `closing`
    writes were made."""

    def __init__(self, delay=0.0, closing=None):
        self.delay, self.closing, self.written, self.aborted = delay, closing, [], False
        self.transport = self

    def write(self, data):
        self.written.append(bytes(data))

    async def drain(self):
        if self.delay is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)

    def is_closing(self):
        return self.closing is not None and len(self.written) >= self.closing

    def abort(self):
        self.aborted = True


class BudgetTest(unittest.TestCase):
    """media.Budget alone, at the times the test gives it."""

    def setUp(self):
        self.budget = media.Budget()

    def test_requests_at_once_then_a_rate(self):
        b, now = self.budget, 500.0
        self.assertEqual(media.REQUESTS, (2.0, 300))
        self.assertEqual([b.admit("a", now) for _ in range(300)], [0] * 300)
        self.assertEqual(b.admit("a", now), 1)  # the seconds until one is taken: Retry-After
        self.assertEqual(b.admit("b", now), 0)  # another address has its own
        self.assertEqual(b.admit("a", now + 0.5), 0)  # it refills: 2 a second
        self.assertEqual(b.admit("a", now + 0.5), 1)
        self.assertEqual(sum(b.admit("a", now + 60.5) == 0 for _ in range(1000)), 120)
        # however long it stayed away, never more at once
        self.assertEqual(sum(b.admit("a", now + 86400) == 0 for _ in range(1000)), 300)

    def test_bytes_at_once_then_a_rate(self):
        b, now = self.budget, 500.0
        rate, burst = media.BYTES
        self.assertEqual((rate, burst), (512 << 10, 2 << 30))
        b.charge("a", burst - 1, now)
        self.assertEqual(b.admit("a", now), 0)  # anything left: one more answer, whatever its size
        b.charge("a", 32 << 20, now)
        self.assertEqual(b.admit("a", now), 64)  # 32 MiB below nothing: back at 512 KiB a second
        self.assertEqual(b.admit("b", now), 0)
        self.assertEqual(b.admit("a", now + 63), 1)
        self.assertEqual(b.admit("a", now + 64), 0)
        self.assertEqual(b.admit("a", now + 10 ** 6), 0)
        self.assertEqual(b.buckets["a"][1], burst)  # never more than at once

    def test_what_was_not_sent_comes_back(self):
        b, now = self.budget, 500.0
        burst = media.BYTES[1]
        b.charge("a", burst + 100, now)
        self.assertGreater(b.admit("a", now), 0)
        b.refund("a", 101, now)
        self.assertEqual(b.admit("a", now), 0)
        b.refund("a", 10 * burst, now)
        self.assertEqual(b.buckets["a"][1], burst)

    def test_the_table_is_bounded(self):
        b, now = self.budget, 500.0
        with mock.patch.object(media, "MAX_ADDRESSES", 3), \
                mock.patch.object(media, "REQUESTS", (1.0, 2)):
            self.assertEqual([b.admit(key, now) for key in "abc"], [0, 0, 0])
            # full: the addresses it cannot remember share one budget, so new ones buy nothing
            self.assertEqual([b.admit(key, now) for key in "def"], [0, 0, 1])
            self.assertEqual(sorted(b.buckets), ["a", "b", "c", media.OTHERS])
            self.assertEqual(b.admit("a", now), 0)  # the remembered ones keep theirs
            # a bucket that is full again has nothing to remember: forgotten when room is needed
            self.assertEqual(b.admit("g", now + 60), 0)
            self.assertEqual(sorted(b.buckets), ["g"])


class SourceTest(unittest.TestCase):
    """media.source: what a request is counted under, and who has no budget of its own."""

    def tearDown(self):
        media.configure()
        limits.configure_proxies()

    def test_a_public_address_as_the_server_counts_it(self):
        for ip in (A[0], B[0], V6 % 1, "::ffff:" + A[0]):
            self.assertEqual(media.source((ip, 40000)), limits.address_key(ip), ip)
        self.assertEqual(media.source((V6 % 1, 40000)), "2a02:26f0:1:2::/64")
        self.assertEqual(media.source(("2a02:26f0:1:2:ffff::99", 40000)), "2a02:26f0:1:2::/64")
        self.assertEqual(media.source(("2a02:26f0:1:3::1", 40000)), "2a02:26f0:1:3::/64")
        self.assertEqual(media.source(("::ffff:" + A[0], 40000)), A[0])

    def test_the_home_network_and_this_machine_have_none(self):
        for ip in ("127.0.0.1", "::1", "192.168.1.20", "10.1.2.3", "172.16.5.4", "169.254.1.1",
                   "fd12:3456::8", "fe80::1%eth0", "::ffff:192.168.1.20"):
            self.assertIsNone(media.source((ip, 40000)), ip)
        self.assertIsNone(media.source(None))

    def test_the_website_s_addresses_have_none(self):
        media.configure("%s, 89.160.20.0/24" % A[0], V6 % 5)
        for ip in (A[0], B[0], "89.160.20.1", V6 % 5, V6 % 0x99):  # an IPv6 one: its /64
            self.assertIsNone(media.source((ip, 40000)), ip)
        for ip in ("81.2.69.143", "89.160.21.1", "2a02:26f0:1:3::5"):
            self.assertEqual(media.source((ip, 40000)), limits.address_key(ip), ip)
        media.configure("2a02:26f0::/32")  # a network; the list is replaced
        self.assertIsNone(media.source(("2a02:26f0:1:3::5", 40000)))
        self.assertEqual(media.source((A[0], 40000)), A[0])
        with self.assertRaises(ValueError):
            media.configure("not an address")

    def test_behind_the_tunnel(self):
        limits.configure_proxies("192.168.1.9")
        agent = ("192.168.1.9", 5000)
        self.assertIsNone(media.source(agent))  # it names no address: none to count
        who = auth.Client(None, connection=True)
        entered = auth.enter(who)  # as server.http_handler does for its connection
        try:
            self.assertIsNone(media.source(agent))
            who.address = limits.address_key(A[0])  # its PROXY header's
            self.assertEqual(media.source(agent), A[0])
            who.address = limits.address_key(V6 % 7)
            self.assertEqual(media.source(agent), "2a02:26f0:1:2::/64")
            media.configure(A[0])
            who.address = limits.address_key(A[0])
            self.assertIsNone(media.source(agent))  # the website's, through the tunnel
        finally:
            auth.leave(entered)


class RoutesTest(tv.Base):
    """The routes as lsp.handle answers them, by who asks."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        patcher = mock.patch.object(media, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(media.configure)
        self.vid = self.published()
        self.upload(tc.ALICE, tc.make_jpeg())
        self.video = "/community/video/%s.mp4" % self.vid
        self.poster = "/community/video/%s.jpg" % self.vid
        (pid,) = community.load()["photos"]
        self.photo = "/community/img/%s.jpg" % pid

    def codes(self, path, peer, times):
        return [self.http("GET", path, peer=peer)[0] for _ in range(times)]

    def test_one_address_cannot_use_up_the_routes_for_another(self):
        codes = self.codes(self.poster, A, 1000)
        self.assertEqual(codes, [200] * 300 + [429] * 700)
        # what its own budget refused never reached the server's: 300 of the route's 600 a minute
        self.assertEqual(len(videos._media), 300)
        status, _, data, extra = self.http("GET", self.poster, peer=A)
        self.assertEqual((status, extra["Retry-After"]), (429, "1"))
        self.assertIn("from this address", json.loads(data)["error"])
        # the two routes together: its photos are refused as well, and count nowhere
        self.assertEqual(self.http("GET", self.photo, peer=A)[0], 429)
        self.assertEqual(community._images, [])
        # another address is answered, and so is the website's own on the home network
        for peer in (B, (V6 % 1, 40000), LAN):
            self.assertEqual(self.http("GET", self.poster, peer=peer)[0], 200, peer)
            self.assertEqual(self.http("GET", self.photo, peer=peer)[0], 200, peer)
        # a minute of asking ten times a second: 2 a second are answered, so 420 of the
        # route's 600 at the most, and the others still have theirs
        answered = 0
        for _ in range(60):
            self.clock.now += 1
            answered += self.codes(self.poster, A, 10).count(200)
        self.assertEqual(answered, 120)
        self.assertLess(len(videos._media), videos.MEDIA_PER_MINUTE)
        self.assertEqual(self.http("GET", self.poster, peer=B)[0], 200)
        self.assertEqual(self.http("GET", self.poster, peer=LAN)[0], 200)

    def test_requests_refill(self):
        with mock.patch.object(media, "REQUESTS", (0.5, 3)):
            self.assertEqual(self.codes(self.poster, A, 4), [200, 200, 200, 429])
            self.assertEqual(self.http("GET", self.photo, peer=A)[3]["Retry-After"], "2")
            self.clock.now += 1
            self.assertEqual(self.http("GET", self.photo, peer=A)[3]["Retry-After"], "1")
            self.clock.now += 1
            self.assertEqual(self.codes(self.photo, A, 2), [200, 429])  # one in 2 s
            self.clock.now += 3600
            self.assertEqual(self.codes(self.poster, A, 5), [200, 200, 200, 429, 429])

    def test_bytes_are_counted_and_refill(self):
        size = os.path.getsize(videos.media_path(self.vid, ".mp4"))
        with mock.patch.object(media, "BYTES", (size / 10, 1.5 * size)):
            status, _, _, extra = self.http("GET", self.video, peer=A)
            self.assertEqual(status, 200)
            # what sends no file costs no bytes
            self.assertEqual(self.http("GET", self.video, {"If-None-Match": extra["ETag"]},
                                       peer=A)[0], 304)
            self.assertEqual(self.http("GET", self.video, {"Range": "bytes=%d-" % size},
                                       peer=A)[0], 416)
            self.assertEqual(self.http("GET", "/community/video/AAAAAAAAAAAA.mp4", peer=A)[0], 404)
            self.assertEqual(media.BUDGET.buckets[A[0]][1], 0.5 * size)
            # a range costs the bytes it names
            self.assertEqual(self.http("GET", self.video, {"Range": "bytes=0-99"}, peer=A)[0], 206)
            self.assertEqual(media.BUDGET.buckets[A[0]][1], 0.5 * size - 100)
            # anything left: one more answer; then nothing until enough came back
            self.assertEqual(self.http("GET", self.video, peer=A)[0], 200)
            status, _, _, extra = self.http("GET", self.video, peer=A)
            wait = math.ceil((1 + 0.5 * size + 100) / (size / 10))
            self.assertEqual((status, extra["Retry-After"]), (429, str(wait)))
            self.assertEqual(self.http("GET", self.photo, peer=A)[0], 429)  # the two routes together
            self.assertEqual(self.http("GET", self.video, peer=B)[0], 200)  # another's are its own
            self.clock.now += wait - 1
            self.assertEqual(self.http("GET", self.video, peer=A)[0], 429)
            self.clock.now += 1
            self.assertEqual(self.http("GET", self.video, peer=A)[0], 200)

    def test_the_website_and_the_home_network_are_not_limited(self):
        media.configure(B[0])  # --website-address: a builder or web server at a public address
        peers = (LAN, ("127.0.0.1", 40000), ("::1", 40000), ("fd12:3456::8", 40000), B)
        with mock.patch.object(media, "REQUESTS", (0.0, 2)), \
                mock.patch.object(media, "BYTES", (0.0, 1)):
            for peer in peers:
                for path in (self.video, self.poster, self.photo):
                    self.assertEqual(self.codes(path, peer, 5), [200] * 5, (peer, path))
            self.assertEqual(media.BUDGET.buckets, {})  # nothing is counted for them
            # an address that is none of those, under the same numbers
            self.assertEqual(self.codes(self.video, A, 3), [200, 429, 429])
            self.assertEqual(self.http("GET", self.video, peer=A)[3]["Retry-After"], "3600")
            self.assertEqual(self.codes(self.poster, LAN, 3), [200] * 3)
        # the server's own budgets count them as before
        self.assertEqual(len(videos._media), len(peers) * 10 + 1 + 3)
        self.assertEqual(len(community._images), len(peers) * 5)
        with mock.patch.object(videos, "MEDIA_PER_MINUTE", 10):
            self.assertEqual(self.http("GET", self.poster, peer=LAN)[0], 429)

    def test_ipv6_is_counted_by_its_64(self):
        with mock.patch.object(media, "REQUESTS", (0.0, 4)):
            codes = [self.http("GET", self.poster, peer=(V6 % n, 40000))[0] for n in range(1, 7)]
            self.assertEqual(codes, [200] * 4 + [429] * 2)  # six addresses of one /64: one budget
            self.assertEqual(list(media.BUDGET.buckets), [limits.address_key(V6 % 1)])
            self.assertEqual(limits.address_key(V6 % 1), "2a02:26f0:1:2::/64")
            # the next /64 has its own, and an IPv4-mapped address is the IPv4 one
            self.assertEqual(self.http("GET", self.poster, peer=("2a02:26f0:1:3::1", 40000))[0], 200)
            self.assertEqual(self.codes(self.poster, ("::ffff:" + A[0], 40000), 2), [200, 200])
            self.assertEqual(self.codes(self.poster, A, 3), [200, 200, 429])
            self.assertEqual(sorted(media.BUDGET.buckets),
                             ["2a02:26f0:1:2::/64", "2a02:26f0:1:3::/64", A[0]])


class SendTest(unittest.IsolatedAsyncioTestCase):
    """server.send with a writer the test controls."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "video.mp4")
        self.whole = os.urandom(2 * media.PIECE + 5000)
        with open(self.path, "wb") as f:
            f.write(self.whole)
        patcher = mock.patch.object(media, "time", Clock())  # nothing comes back meanwhile
        patcher.start()
        self.addCleanup(patcher.stop)
        media.BUDGET.reset()
        self.addCleanup(media.BUDGET.reset)

    def body(self, start=0, length=None):
        return media.FileBody(self.path, start, len(self.whole) - start if length is None else length)

    async def test_a_file_goes_out_in_pieces(self):
        for start, length in ((0, None), (1000, media.PIECE), (media.PIECE - 1, 2),
                              (media.PIECE - 3, media.PIECE + 6), (5, 2 * media.PIECE)):
            body, w = self.body(start, length), Writer()
            head = srv.response(206, body, "video/mp4")
            self.assertTrue(head.endswith(b"\r\n\r\n"))  # the head alone: the file is not read for it
            self.assertIn(b"Content-Length: %d\r\n" % len(body), head)
            await srv.send(w, head, body)
            self.assertEqual(w.written[0], head)
            self.assertLessEqual(max(map(len, w.written[1:])), media.PIECE)
            self.assertEqual(len(w.written) - 1, -(-len(body) // media.PIECE))
            self.assertTrue(b"".join(w.written[1:]) == self.whole[start:start + len(body)],
                            (start, length))
            self.assertFalse(w.aborted)

    async def test_a_reader_that_takes_nothing_is_dropped(self):
        body, w = self.body(), Writer(delay=None)
        media.charge(A[0], (200, "video/mp4", body))
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.05):
            with self.assertRaises(asyncio.TimeoutError):
                await srv.send(w, srv.response(200, body, "video/mp4"), body)
        self.assertTrue(w.aborted)  # aborted, not closed: nothing stays buffered for it
        self.assertEqual(len(w.written), 1)
        self.assertEqual(media.BUDGET.buckets[A[0]][1], media.BYTES[1])  # all of it given back

    async def test_only_a_file_s_answer_has_the_time_limit(self):
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.05):
            w = Writer(delay=0.3)  # six times slower than a file's reader may be
            reply = srv.response(200, bytes(1 << 20), "bin/xtsw")  # a title call's answer
            await srv.send(w, reply)
            self.assertEqual((w.written, w.aborted), ([reply], False))
            body, w = self.body(), Writer(delay=0.3)
            with self.assertRaises(asyncio.TimeoutError):
                await srv.send(w, srv.response(200, body, "video/mp4"), body)
            self.assertTrue(w.aborted)

    async def test_the_time_is_a_piece_s_not_the_answer_s(self):
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.2), mock.patch.object(media, "PIECE", 20000):
            body, w = self.body(), Writer(delay=0.01)
            started = time.monotonic()
            await srv.send(w, srv.response(200, body, "video/mp4"), body)
            self.assertGreater(time.monotonic() - started, 0.2)  # 27 pieces, 10 ms and more each
            self.assertTrue(b"".join(w.written[1:]) == self.whole)
            self.assertFalse(w.aborted)

    async def test_a_connection_closed_meanwhile_gets_no_more(self):
        body, w = self.body(), Writer(closing=2)  # closed (its place given away) after a piece
        media.charge(A[0], (200, "video/mp4", body))
        with self.assertRaises(ConnectionError):
            await srv.send(w, srv.response(200, body, "video/mp4"), body)
        self.assertEqual(len(w.written), 2)  # the head and one piece
        self.assertTrue(w.aborted)
        self.assertEqual(media.BUDGET.buckets[A[0]][1], media.BYTES[1] - media.PIECE)

    async def test_a_file_removed_meanwhile_ends_the_answer(self):
        body, w = self.body(), Writer()
        media.charge(A[0], (200, "video/mp4", body))
        write = w.write

        def write_then_remove(data):
            write(data)
            if len(w.written) == 2:
                os.remove(self.path)

        w.write = write_then_remove
        with self.assertRaises(OSError):  # http_handler ends the connection: a short answer
            await srv.send(w, srv.response(200, body, "video/mp4"), body)
        self.assertEqual(len(w.written), 2)
        self.assertEqual(media.BUDGET.buckets[A[0]][1], media.BYTES[1] - media.PIECE)
        with open(self.path, "wb") as f:  # a file shorter than its answer, too
            f.write(self.whole[:100])
        with self.assertRaises(OSError):
            list(self.body().pieces())


class WireTest(tv.Base, unittest.IsolatedAsyncioTestCase):
    """The routes through the server's HTTP handler, on a socket."""

    async def asyncSetUp(self):
        srv.http_conns.clear()
        srv.http_open.clear()
        self.sndbuf = None  # the server's send buffer for the test's connections, when set
        self.socks = []
        self.listener = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        self.port = self.listener.sockets[0].getsockname()[1]
        self.addCleanup(media.configure)

    async def asyncTearDown(self):
        for sock in self.socks:
            sock.close()
        self.listener.close()
        await self.listener.wait_closed()
        await self.ended()

    def accept(self, reader, writer):
        if self.sndbuf:
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                                       self.sndbuf)
        return srv.http_handler(self.server, reader, writer)

    async def ended(self, within=5.0):
        """Every connection's handler has ended: how long that took."""
        started = time.monotonic()
        while srv.http_open and time.monotonic() - started < within:
            await asyncio.sleep(0.01)
        self.assertEqual(srv.http_open, {})
        self.assertEqual(srv.http_conns, {})  # and its address has its connection back
        return time.monotonic() - started

    async def connect(self, path, headers=(), prefix=b"", rcvbuf=None):
        """A connection that has sent its request for `path` (after `prefix`, a PROXY header)."""
        sock = socket.socket()
        self.socks.append(sock)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, ("127.0.0.1", self.port))
        await loop.sock_sendall(sock, prefix + ("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n" % (
            path, "".join(h + "\r\n" for h in headers))).encode())
        return sock

    async def read(self, sock, chunk=1 << 16, pause=0.0):
        """What the server sends until it ends the connection, `chunk` bytes at a time."""
        loop, got = asyncio.get_running_loop(), []
        try:
            while True:
                data = await asyncio.wait_for(loop.sock_recv(sock, chunk), 10)
                if not data:
                    break
                got.append(data)
                if pause:
                    await asyncio.sleep(pause)
        except ConnectionError:  # aborted by the server
            pass
        return b"".join(got)

    async def get(self, path, headers=(), prefix=b""):
        raw = await self.read(await self.connect(path, headers, prefix))
        head, body = raw.split(b"\r\n\r\n", 1)
        return head.decode(), body

    def video_of(self, nbytes):
        """A live video whose MP4 holds nbytes of random bytes: (its URL, the file's bytes)."""
        self.clip()
        vid = self.only()[0]
        self.converted(vid, mp4=tv.make_mp4(payload=os.urandom(nbytes)))
        videos.collect()
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 200)
        with open(videos.media_path(vid, ".mp4"), "rb") as f:
            return "/community/video/%s.mp4" % vid, f.read()

    def tunnel(self):
        """The test's connections come from the tunnel's agent, with --proxy-protocol-tcp."""
        limits.configure_proxies("127.0.0.1")  # tc.Base.tearDown forgets it
        self.server.proxy_tcp = True
        self.server.proxy_stats = {"http_headers": 0}
        self.server.tell_once = lambda *a: None

    async def test_a_range_is_right_across_pieces(self):
        url, whole = self.video_of(600 << 10)
        n, piece = len(whole), media.PIECE
        self.assertGreater(n, 2 * piece)
        head, body = await self.get(url)
        self.assertIn("HTTP/1.1 200", head)
        self.assertIn("Content-Length: %d" % n, head)
        self.assertTrue(body == whole)
        for header, (start, end) in (("bytes=0-", (0, n - 1)),
                                     ("bytes=%d-%d" % (piece - 1, piece), (piece - 1, piece)),
                                     ("bytes=1000-%d" % (piece + 999), (1000, piece + 999)),
                                     ("bytes=100000-", (100000, n - 1)),
                                     ("bytes=-%d" % (2 * piece + 7), (n - 2 * piece - 7, n - 1)),
                                     ("bytes=%d-%d" % (2 * piece, 10 * n), (2 * piece, n - 1))):
            head, body = await self.get(url, ["Range: " + header])
            self.assertIn("HTTP/1.1 206", head, header)
            self.assertIn("Content-Range: bytes %d-%d/%d" % (start, end, n), head)
            self.assertIn("Content-Length: %d" % (end + 1 - start), head)
            self.assertTrue(body == whole[start:end + 1], header)
        await self.ended()

    async def test_a_reader_that_takes_nothing_is_dropped(self):
        url, whole = self.video_of(4 << 20)
        self.sndbuf = 8192
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.3):
            sock = await self.connect(url, rcvbuf=4096)
            await asyncio.sleep(0.15)
            self.assertEqual(len(srv.http_open), 1)  # waited for, so far
            took = 0.15 + await self.ended()
            self.assertGreater(took, 0.25)  # not before its time was up (a coarse clock's worth off)
            self.assertLess(took, 3.0)
            got = await self.read(sock)  # what was on its way, and no more
        self.assertLess(len(got), len(whole) // 2)

    async def test_a_slow_reader_that_keeps_taking_is_not_dropped(self):
        url, whole = self.video_of(2 << 20)
        self.sndbuf = 8192
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.75), \
                mock.patch.object(media, "PIECE", 64 << 10):
            started = time.monotonic()
            sock = await self.connect(url, rcvbuf=256 << 10)
            raw = await self.read(sock, 32768, pause=0.02)  # 1.6 MiB a second at the most
            took = time.monotonic() - started
        self.assertTrue(raw.split(b"\r\n\r\n", 1)[1] == whole)
        self.assertGreater(took, 0.75)  # longer than the limit in all: the limit is a piece's
        await self.ended()

    async def test_a_title_call_s_slow_reader_is_not_dropped(self):
        answer = os.urandom(1 << 20)  # more than the buffers hold for the reader
        self.sndbuf = 8192
        with mock.patch.object(srv, "DRAIN_SECONDS", 0.2), \
                mock.patch.object(lsp, "handle", return_value=(200, "bin/xtsw", answer)):
            sock = await self.connect("/title/o.xtsw", rcvbuf=128 << 10)
            await asyncio.sleep(0.7)  # it takes nothing for longer than a file's reader may
            raw = await self.read(sock)
        self.assertTrue(raw.split(b"\r\n\r\n", 1)[1] == answer)
        await self.ended()

    async def test_a_tunnelled_address_is_counted_as_itself(self):
        self.tunnel()
        poster = "/community/video/%s.jpg" % self.published()
        from_a = proxy.header(A, TUNNEL, proxy.STREAM)
        with mock.patch.object(media, "REQUESTS", (0.0, 2)):
            heads = [(await self.get(poster, prefix=from_a))[0] for _ in range(3)]
            self.assertEqual([h.split()[1] for h in heads], ["200", "200", "429"])
            self.assertIn("Retry-After: 3600", heads[2])
            head, _ = await self.get(poster, prefix=proxy.header(B, TUNNEL, proxy.STREAM))
            self.assertIn("HTTP/1.1 200", head)  # another player's address
            self.assertEqual(sorted(media.BUDGET.buckets), sorted([A[0], B[0]]))
            # the agent's own connections, and a tunnel that names no address: none to count
            local = proxy.header(A, TUNNEL, proxy.STREAM, command=proxy.LOCAL)
            for _ in range(4):
                self.assertIn("HTTP/1.1 200", (await self.get(poster, prefix=local))[0])
            self.server.proxy_tcp = False
            for _ in range(4):
                self.assertIn("HTTP/1.1 200", (await self.get(poster))[0])
            self.assertEqual(sorted(media.BUDGET.buckets), sorted([A[0], B[0]]))
        await self.ended()

    async def test_what_a_reader_did_not_take_is_given_back(self):
        self.tunnel()
        url, whole = self.video_of(4 << 20)
        self.sndbuf = 8192
        with mock.patch.object(media, "BYTES", (0.0, 1 << 30)):
            sock = await self.connect(url, prefix=proxy.header(A, TUNNEL, proxy.STREAM),
                                      rcvbuf=4096)
            loop = asyncio.get_running_loop()
            self.assertIn(b"HTTP/1.1 200", await asyncio.wait_for(loop.sock_recv(sock, 1024), 5))
            self.assertEqual(media.BUDGET.buckets[A[0]][1], (1 << 30) - len(whole))  # charged whole
            sock.close()  # it leaves with most of the video unsent
            await self.ended()
            spent = (1 << 30) - media.BUDGET.buckets[A[0]][1]
        self.assertLess(spent, len(whole) // 2)
        self.assertEqual(spent % media.PIECE, 0)  # the pieces it was sent


if __name__ == "__main__":
    unittest.main()
