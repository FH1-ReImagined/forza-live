"""What several test files share: the repository's paths, a fake UDP transport, the server's
datagrams, a car club roster, a reference PNG decoder and the score tokens' clock."""

import os
import struct
import time
import zlib

from forza_live import server as srv
from forza_live.competition import score_token
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
