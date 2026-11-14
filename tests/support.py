"""What several test files share: the repository's paths, a fake UDP transport, the server's
datagrams, a car club roster, a reference PNG decoder, the score tokens' clock and the check
that the stored files' running totals are right."""

import contextlib
import os
import struct
import threading
import time
import zlib
from unittest import mock

from forza_live import server as srv
from forza_live.competition import score_token
from forza_live.content import videos
from forza_live.core import limits
from forza_live.social import clubs

# The repository's root (forza_live_server.py, the entry point, is there).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The game repository's tools/, which some tests compare against: beside this repository when it
# is that repository's server/ submodule, or where FORZA_REPO_TOOLS says (the CI checks the files
# out).
GAME_TOOLS = os.environ.get("FORZA_REPO_TOOLS") or os.path.join(ROOT, "..", "tools")


class RaceClock:
    """score_token's clock for a test (made before scores.configure, the server's start):
    race() lets an event's time pass after its start."""

    def __init__(self, test, now=10000.0):
        self.now = now
        test.addCleanup(setattr, score_token, "clock", score_token.clock)
        score_token.clock = lambda: self.now

    def race(self, seconds=600.0):
        self.now += seconds


class FakeTransport:
    """The server's UDP transport: what it sends is kept in `sent` as (address, datagram)."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((addr, data))

    def kinds(self, addr):
        return [d[4] for a, d in self.sent if a == addr]


def hello(token, xuid, name, room="#TU4"):
    room, name = room.encode(), name.encode()
    body = struct.pack(">HQQIH", srv.VERSION, token, xuid, 0, 0)
    body += bytes([len(room)]) + room + bytes([len(name)]) + name + bytes([1])
    return srv.MAGIC + bytes([srv.HELLO]) + body


def welcome_id(transport, addr):
    for a, d in reversed(transport.sent):
        if a == addr and d[4] == srv.WELCOME:
            return struct.unpack_from(">IH", d, 5)
    return None


def record(session_id, filled=1, slots=8):
    """A session record: the XSESSION_INFO (the session id first), then the public slots."""
    info = bytearray(srv.SESSION_INFO_SIZE)
    info[:8] = session_id
    return bytes(info) + bytes([slots, 0, filled, 0]) + bytes(32)


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


def decode_png(data):
    """(width, height, rows) of an 8-bit RGB PNG with filter types 0-3 (a plain reference decoder),
    checking that it holds IHDR, IDAT and IEND only."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    i, chunks = 8, []
    while i < len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        assert struct.unpack(">I", data[i + 8 + n:i + 12 + n])[0] == zlib.crc32(kind + body)
        chunks.append((kind, body))
        i += 12 + n
    assert [k for k, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"], [k for k, _ in chunks]
    w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, color, interlace) == (8, 2, 0)
    raw, n, rows, prev = zlib.decompress(chunks[1][1]), w * 3, [], bytes(w * 3)
    for y in range(h):
        kind, f = raw[y * (n + 1)], raw[y * (n + 1) + 1:(y + 1) * (n + 1)]
        out = bytearray(n)
        for j in range(n):
            left = out[j - 3] if j >= 3 else 0
            out[j] = (f[j] + (0, left, prev[j], (left + prev[j]) // 2)[kind]) & 255
        rows.append(bytes(out))
        prev = out
    return w, h, rows


def fresh_count(store, folders=None):
    """The bytes a count of the store's folders (or of `folders`) finds now; the store's own
    running total is left as it is."""
    return limits.Stored(folders or store._folders, store._stat).count()[1]


class DiskTotals:
    """The stored files' running totals against the files (#64). Made in a test's setUp, once
    the data folder is configured; check(test) at its end, while the folder is still there:
    the uploads' total (limits.STORED), and with with_videos the videos' (videos.STORED), must
    be what a fresh count finds, so every file the test's calls wrote or deleted was counted.
    The video converter's folder is left out: its files are not the server's, and only a count
    sees them. A store configured again during the test (a restart) is left out too, as its
    total then starts from the count the server makes; so is everything after skip(), for a
    test that changes the files behind the server's back."""

    def __init__(self, with_videos=False):
        stores = (limits.STORED, videos.STORED) if with_videos else (limits.STORED,)
        self.rounds = {store: store._round for store in stores}

    def skip(self):
        self.rounds = {}

    def check(self, test):
        if self.rounds.get(limits.STORED) == limits.STORED._round:
            test.assertEqual(limits.usage(), fresh_count(limits.STORED),
                             "the disk quota's running total is not what the files add up to")
        if self.rounds.get(videos.STORED) == videos.STORED._round and not videos.STORED.counted:
            own = fresh_count(videos.STORED, lambda: [videos.folder(sub) for sub in
                                                      ("incoming", "public", "frames")])
            test.assertEqual(videos.usage(), own,
                             "the videos' running total is not what their files add up to")


class Walks:
    """What no_walks_here() saw: `here`, the folders the guarded thread tried to walk (none, in
    a test that passes); `elsewhere`, those other threads walked."""

    def __init__(self):
        self.here, self.elsewhere = [], []


@contextlib.contextmanager
def no_walks_here():
    """Inside it os.walk and os.scandir, what a count of the stored files is made of, fail on
    the calling thread (an async test's event loop) and work on any other. The Walks it yields
    says who walked what: a failure raised deep inside the server may be caught and logged
    there, so tests look at `here` as well."""
    thread, seen = threading.get_ident(), Walks()

    def guarded(real):
        def call(top=".", *args, **kw):
            if threading.get_ident() == thread:
                seen.here.append(top)
                raise AssertionError("a folder walked on the event loop's thread: %r" % (top,))
            seen.elsewhere.append(top)
            return real(top, *args, **kw)
        return call

    with mock.patch.object(os, "walk", guarded(os.walk)), \
            mock.patch.object(os, "scandir", guarded(os.scandir)):
        yield seen


def fill(folder, files, size=7):
    """`files` small files in `folder` (made if need be), old-<n>.bin: names no upload gets.
    The bytes they hold together."""
    os.makedirs(folder, exist_ok=True)
    for n in range(files):
        with open(os.path.join(folder, "old-%d.bin" % n), "wb") as f:
            f.write(bytes(size))
    return files * size
