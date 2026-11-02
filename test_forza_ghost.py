"""The checks of an uploaded Rivals ghost (forza_ghost.py), on ghosts laid out as the game writes
them (a Rivals lap of GoPro Millennia Masters, Clear Springs Circuit, in the Aventador).

Run: python3 -m unittest discover -p 'test_*.py'
"""

import struct
import unittest
import zlib

import forza_ghost as g

LAP, AVENTADOR, CLEAR_SPRINGS = 138.513, 1398, 156


def deflate(data, chunk=g.CHUNK_MAX):
    """A block as the game writes it: raw deflate, 4096 bytes at a time."""
    out = b""
    for at in range(0, len(data), chunk):
        co = zlib.compressobj(6, zlib.DEFLATED, -15)
        z = co.compress(data[at:at + chunk]) + co.flush()
        out += struct.pack(">II", len(z), len(data[at:at + chunk])) + z
    return out


def item(data):
    return struct.pack(">I", len(data)) + data


def replay_init(lap=LAP, car=AVENTADOR, track=CLEAR_SPRINGS, name="DriverA", cars=1,
                version=g.VERSION, extra=b""):
    settings = bytearray(g.SETTINGS)
    settings[4:8] = struct.pack(">I", track)
    settings[328:332] = struct.pack(">I", 199)
    entry = bytearray(g.CAR_SIZE)
    entry[34:47] = b"LAM_LP7004_12"
    entry[568:572] = b"\x3f\x80\x00\x00"  # as in the game's ghosts, after a short name
    entry[g.CAR_GAMERTAG:g.CAR_GAMERTAG + 2 * len(name)] = name.encode("utf-16-be")
    entry[g.CAR_ID:g.CAR_ID + 4] = struct.pack(">I", car)
    livery = b"crlv" + bytes(458)
    return (struct.pack(">I", version) + bytes(settings) + struct.pack(">dBI", lap, 1, 3) +
            struct.pack(">3I", 0, 9467, 22352) + bytes([cars]) + bytes(entry) * cars + b"\x01" +
            item(b"base:%d,1>ingame_lodn" % car) + item(livery) + extra)


def record(t, kind, body):
    return struct.pack(">fB", t, kind) + body + struct.pack(">I", len(body))


def replay_data(lap=LAP, every=0.2, start=0.0024, skip=()):
    """A keyframe, the car's state every `every` s (but in `skip`), events, the lap's end."""
    out = record(start, 0x20, bytes(1433))
    t, n = start + 0.117, 0
    while t < lap + start:
        if not any(a <= t < b for a, b in skip):
            out += record(t, g.SAMPLE, struct.pack(">H3f", 34, -176.9, 104.9, -3678.1) + bytes(9))
        if n % 10 == 5:
            out += record(t, 0x44, bytes(6))
        t, n = t + every, n + 1
    end = lap + start + 0.004
    out += record(end, 0x45, bytes(31)) + record(end, 0x45, bytes(31))
    return out + record(end, g.SAMPLE, bytes(25))


def ghost(init=None, data=None):
    bi, bd = deflate(init if init is not None else replay_init()), \
        deflate(data if data is not None else replay_data())
    return struct.pack(">II", len(bi), len(bd)) + bi + bd


def problem(upload, score=LAP, car=AVENTADOR, tracks=frozenset({CLEAR_SPRINGS})):
    return g.check(upload, score, car, tracks)[0]


def blocks(upload):
    a, b = struct.unpack_from(">II", upload)
    return upload[8:8 + a], upload[8 + a:]


class GhostTest(unittest.TestCase):
    def test_a_lap_as_the_game_uploads_it(self):
        upload = ghost()
        found, kept = g.check(upload, 138.51297, AVENTADOR, {CLEAR_SPRINGS})  # a float32 score
        self.assertIsNone(found)
        # the driver's gamertag is cleared in the kept copy, and nothing else
        init = b"".join(g.inflate(blocks(kept)[0], g.INIT_MAX))
        self.assertEqual(init, replay_init(name=""))
        self.assertEqual(blocks(kept)[1], blocks(upload)[1])
        self.assertNotIn("DriverA".encode("utf-16-be"), init)
        self.assertEqual(g.check(kept, LAP, AVENTADOR, {CLEAR_SPRINGS}), (None, kept))
        # a name of 15 characters (the longest gamertag) is cleared to its end and no further
        init = b"".join(g.inflate(blocks(g.check(ghost(replay_init(name="P" * 15)), LAP,
                                                 AVENTADOR)[1])[0], g.INIT_MAX))
        self.assertEqual(init, replay_init(name="\0" * 15))

    def test_without_the_board_s_tracks_any_track_passes(self):
        self.assertIsNone(problem(ghost(replay_init(track=107)), tracks=None))
        self.assertIsNone(problem(ghost(replay_init(track=107)), tracks={107, 155}))

    def test_sizes_and_header(self):
        upload = ghost()
        self.assertEqual(problem(b"\0" * 10), "10 bytes")
        self.assertEqual(problem(upload + b"\0" * (g.GHOST_MAX - len(upload) + 1)),
                         "%d bytes" % (g.GHOST_MAX + 1))
        self.assertRegex(problem(upload[:-1]), r"^header sizes")
        self.assertRegex(problem(upload + b"\0"), r"^header sizes")
        a, b = struct.unpack_from(">II", upload)
        self.assertRegex(problem(struct.pack(">II", 0, a + b) + upload[8:]), r"^header sizes")

    def test_chunks(self):
        bi, bd = blocks(ghost())
        head = lambda block: struct.pack(">II", len(block), len(bd)) + block + bd  # noqa: E731
        big = deflate(replay_data(), 8192)
        self.assertEqual(problem(struct.pack(">II", len(bi), len(big)) + bi + big),
                         "chunk sizes %d/8192" % struct.unpack_from(">I", big)[0])
        csize, size = struct.unpack_from(">II", bi)
        broken = bi[:8] + bytes([bi[8] ^ 0xFF]) + bi[9:]
        self.assertRegex(problem(head(broken)), r"^a chunk (does not inflate|inflates to)")
        self.assertRegex(problem(head(struct.pack(">II", csize, size - 1) + bi[8:])),
                         r"^a chunk inflates to")
        padded = struct.pack(">II", csize + 3, size) + bi[8:] + b"pad"
        self.assertRegex(problem(head(padded)), r"^a chunk inflates to")
        self.assertRegex(problem(head(bi + b"\0\0\0")), r"^a chunk header is cut off")
        # a decompression bomb stops at the limit
        zeros = deflate(bytes(g.CHUNK_MAX)) * (g.DATA_MAX // g.CHUNK_MAX + 1)
        self.assertRegex(problem(struct.pack(">II", len(bi), len(zeros)) + bi + zeros),
                         r"^(more than \d+ bytes decompressed|\d+ bytes)$")

    def test_replay_init(self):
        self.assertEqual(problem(ghost(replay_init(version=0x178B))), "version 178B")
        self.assertEqual(problem(ghost(replay_init(cars=2))), "2 cars")
        self.assertEqual(problem(ghost(replay_init(extra=b"x"))),
                         "1 bytes after ReplayInit's last field")
        self.assertEqual(problem(ghost(replay_init()[:900])), "ReplayInit is cut off")

    def test_the_row_s_car_track_and_time(self):
        self.assertEqual(problem(ghost(replay_init(car=432))), "car 432, the score's is 1398")
        self.assertEqual(problem(ghost(), car=432), "car 1398, the score's is 432")
        self.assertEqual(problem(ghost(replay_init(track=107))), "track 107, the board's is 156")
        self.assertEqual(problem(ghost(), tracks={107, 155}), "track 156, the board's is 107/155")
        # a ghost of a slower lap given to a faster score, and the other way round
        self.assertEqual(problem(ghost(), score=120.0),
                         "lap time 138.513 s, the score is 120.000 s")
        self.assertEqual(problem(ghost(replay_init(lap=float("nan")))),
                         "lap time nan s, the score is 138.513 s")

    def test_records(self):
        # ReplayInit claims the score, the records are of a shorter or longer lap
        self.assertEqual(problem(ghost(data=replay_data(lap=120.0))),
                         "the records span 120.004 s, the score is 138.513 s")
        self.assertEqual(problem(ghost(data=replay_data(lap=145.0))),
                         "the records span 145.004 s, the score is 138.513 s")
        self.assertIsNone(problem(ghost(data=replay_data(lap=LAP + 0.6))))  # within 0.5 s + 0.5%
        # a hole in the car's samples (a cut), too few of them, too many
        self.assertEqual(problem(ghost(data=replay_data(skip=[(40.0, 43.0)]))),
                         "car samples 3.20 s apart")
        self.assertRegex(problem(ghost(data=replay_data(every=1.5))), r"^\d+ car samples for")
        self.assertRegex(problem(ghost(data=replay_data(every=0.01))), r"^\d+ car samples for")
        # bytes in front of the first record, records out of time order, a bad time
        self.assertEqual(problem(ghost(data=b"junk" + replay_data())),
                         "ReplayData does not start with a record")
        late = record(500.0, g.SAMPLE, bytes(25))
        self.assertEqual(problem(ghost(data=late + replay_data())), "records out of time order")
        self.assertRegex(problem(ghost(data=record(float("inf"), 0x40, bytes(25)) + replay_data())),
                         r"^a record at")
        huge = struct.pack(">I", g.RECORD_MAX + 1)
        self.assertEqual(problem(ghost(data=replay_data() + huge)),
                         "a record of %d bytes" % (g.RECORD_MAX + 1))
        many = b"".join(record(i * 0.001, 0x44, b"") for i in range(g.RECORDS_MAX + 1))
        self.assertEqual(problem(ghost(data=many)), "more than %d records" % g.RECORDS_MAX)


if __name__ == "__main__":
    unittest.main()
