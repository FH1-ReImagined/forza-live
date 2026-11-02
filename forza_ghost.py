"""Forza Live: the checks a Rivals ghost (Scoreboard.UploadFile) passes before other players get it.

The format, from the game (the writer sub_82C82A58 and reader sub_82C83AC0, the chunk codec
sub_82C77E00 / sub_82C7B4A8, the ReplayInit serializer sub_8258A678; base game addresses) and
ghosts uploaded by a local game (GoPro Millennia Masters, Clear Springs Circuit, track 156: two
Rivals laps, an Aventador, car 1398, in 138.513 s and a Saleen S7, car 432, in 138.412 s, and
the race's best lap, lap 2 of 4 in the Aventador, 117.704 s):

    u32 init size, u32 data size, then the two blocks (8 + init + data = the upload)
    block: chunks of u32 compressed size, u32 size (at most 4096), raw deflate of the chunk
           (each chunk its own stream; the game's inflater takes zlib's)

ReplayInit (2747-2748 bytes in those ghosts), big-endian:

    0     u32  0x178C, the version
    4     720  the race's settings: +4 the track id (u32)
    724   f64  the lap time, s (the score, which the game sends as a float)
    732   u8
    733   u32  n, n u32s
          u8   cars (1), 1504 bytes each: +544 the driver's gamertag (UTF-16, at most 16
               characters, nul-terminated), +1060 the car id (u32)
          u8   items, each: u32 length + a name ("base:1398,1>ingame_lodn"), u32 length + data
               (the livery)

ReplayData, records written one after another: f32 time (s from the lap's start), u8 type,
the body, u32 the body's size (so it reads from the end back to the start). Type 0x40 is the
car's state (its position, orientation and speed, 25 bytes), about 5 a second (at most 0.22 s
apart); 0x20 a full keyframe (1433 bytes) at the start and every 40-55 s; 0x44, 0x45 (the
lap's end, with the lap time as f64) and 0x47 events. 853, 839 and 733 records in those laps,
the first at 0.002-0.015 s, the last within 0.012 s of the score (in the race too: the lap is
recorded from its own start).

The checks: the sizes and the chunks; the version; one car, the row's; the board's track (when
the server knows the event's tracks: the game's database, or a time trial's); ReplayInit's lap
time and the records' span both the score; the records reaching back to the start exactly, in
time order, no more than a lap needs, the car's samples no further apart than MAX_GAP. The game
leaves its driver's gamertag in the ghost; the kept copy has it cleared (other players see the
row's gamertag over the ghost car, not the ghost's).
"""

import math
import struct
import zlib

GHOST_MAX = 256000          # the game's download buffer (DownloadFile maxSize)
CHUNK_MAX = 4096            # the writer compresses 4096 bytes at a time
INIT_MAX = 64 << 10         # ReplayInit: 2.7 KB for one car
DATA_MAX = 4 << 20          # ReplayData: 31 KB for a lap of 138 s
VERSION = 0x178C
SETTINGS = 720
CAR_SIZE = 1504
CAR_GAMERTAG, GAMERTAG_CHARS = 544, 16
CAR_ID = 1060
SAMPLE = 0x40               # a record of the car's state
RECORD_MAX = 1 << 16        # one record's body (a keyframe is 1433 bytes)
RECORDS_MAX = 20000         # records in a ghost (about 6 a second)
MAX_GAP = 2.0               # s between two car samples (0.22 s in the game's ghosts)
MAX_RATE = 60.0             # car samples a second at most (the game writes about 5)
MIN_RATE = 1.0              # and at least
TOLERANCE = 0.5             # s, and SLACK of the score: the records' span against the score
SLACK = 0.005
LAP_TOLERANCE = 0.01        # s: ReplayInit's lap time against the score (a float32)


class Bad(Exception):
    pass


def inflate(block, limit):
    """The chunks of a block, decompressed: [bytes]."""
    out, pos, total = [], 0, 0
    while pos < len(block):
        if pos + 8 > len(block):
            raise Bad("a chunk header is cut off")
        csize, size = struct.unpack_from(">II", block, pos)
        pos += 8
        if not 0 < size <= CHUNK_MAX or csize == 0 or pos + csize > len(block):
            raise Bad("chunk sizes %d/%d" % (csize, size))
        total += size
        if total > limit:
            raise Bad("more than %d bytes decompressed" % limit)
        d = zlib.decompressobj(-15)
        try:
            raw = d.decompress(block[pos:pos + csize], size + 1)
        except zlib.error as e:
            raise Bad("a chunk does not inflate (%s)" % e) from None
        if len(raw) != size or not d.eof or d.unused_data or d.unconsumed_tail:
            raise Bad("a chunk inflates to %d bytes, not %d" % (len(raw), size))
        out.append(raw)
        pos += csize
    if not out:
        raise Bad("an empty block")
    return out


def deflate(data):
    """data as a block of chunks, as the game writes it."""
    out = []
    for at in range(0, len(data), CHUNK_MAX):
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        z = co.compress(data[at:at + CHUNK_MAX]) + co.flush()
        out.append(struct.pack(">II", len(z), min(CHUNK_MAX, len(data) - at)) + z)
    return b"".join(out)


def take(data, pos, fmt):
    size = struct.calcsize(fmt)
    if pos + size > len(data):
        raise Bad("ReplayInit is cut off")
    return struct.unpack_from(fmt, data, pos), pos + size


def replay_init(init):
    """(track id, lap time, car id, offset of the car's gamertag) of a ReplayInit."""
    (version,), _ = take(init, 0, ">I")
    if version != VERSION:
        raise Bad("version %X" % version)
    (track,), _ = take(init, 8, ">I")
    (lap, _flag, n), pos = take(init, 4 + SETTINGS, ">dBI")
    if n > 64:
        raise Bad("%d splits" % n)
    pos += 4 * n
    (cars,), pos = take(init, pos, ">B")
    if cars != 1:
        raise Bad("%d cars" % cars)
    car = pos
    (car_id,), _ = take(init, car + CAR_ID, ">I")
    pos += CAR_SIZE
    (items,), pos = take(init, pos, ">B")
    if items > 8:
        raise Bad("%d items" % items)
    for _ in range(items):
        for _ in range(2):
            (size,), pos = take(init, pos, ">I")
            if size > INIT_MAX or pos + size > len(init):
                raise Bad("an item of %d bytes" % size)
            pos += size
    if pos != len(init):
        raise Bad("%d bytes after ReplayInit's last field" % (len(init) - pos))
    return track, lap, car_id, car + CAR_GAMERTAG


def records(data):
    """[(time, type)] of ReplayData, first to last."""
    out, end = [], len(data)
    while end > 0:
        if end < 9:
            raise Bad("ReplayData does not start with a record")
        (size,) = struct.unpack_from(">I", data, end - 4)
        start = end - 9 - size
        if size > RECORD_MAX or start < 0:
            raise Bad("a record of %d bytes" % size)
        (t,) = struct.unpack_from(">f", data, start)
        if not math.isfinite(t) or t < -1.0:
            raise Bad("a record at %r s" % t)
        if out and t > out[-1][0] + 1e-3:
            raise Bad("records out of time order")
        if len(out) >= RECORDS_MAX:
            raise Bad("more than %d records" % RECORDS_MAX)
        out.append((t, data[start + 4]))
        end = start
    out.reverse()
    return out


def check(upload, score, car_id, tracks=None):
    """(problem or None, the ghost to keep): a problem (text) when the ghost is not one of a lap
    of `score` seconds in car `car_id` (on one of `tracks`, when given); else None and the
    upload with the driver's gamertag cleared."""
    try:
        if not 24 <= len(upload) <= GHOST_MAX:
            raise Bad("%d bytes" % len(upload))
        a, b = struct.unpack_from(">II", upload, 0)
        if a == 0 or b == 0 or 8 + a + b != len(upload):
            raise Bad("header sizes %d + %d for %d bytes" % (a, b, len(upload)))
        init = b"".join(inflate(upload[8:8 + a], INIT_MAX))
        data = b"".join(inflate(upload[8 + a:], DATA_MAX))
        track, lap, ghost_car, name = replay_init(init)
        if ghost_car != car_id:
            raise Bad("car %d, the score's is %d" % (ghost_car, car_id))
        if tracks and track not in tracks:
            raise Bad("track %d, the board's is %s" % (
                track, "/".join(str(t) for t in sorted(tracks))))
        if not math.isfinite(lap) or abs(lap - score) > LAP_TOLERANCE + 1e-5 * score:
            raise Bad("lap time %.3f s, the score is %.3f s" % (lap, score))
        recs = records(data)
        span = recs[-1][0] - recs[0][0]
        if abs(span - score) > TOLERANCE + SLACK * score:
            raise Bad("the records span %.3f s, the score is %.3f s" % (span, score))
        samples = [t for t, kind in recs if kind == SAMPLE]
        if not MIN_RATE * score <= len(samples) <= MAX_RATE * score + 100:
            raise Bad("%d car samples for %.3f s" % (len(samples), score))
        gap = max((y - x for x, y in zip(samples, samples[1:])), default=score)
        if gap > MAX_GAP:
            raise Bad("car samples %.2f s apart" % gap)
    except (Bad, struct.error) as e:
        return str(e), None
    n = 0
    while n < GAMERTAG_CHARS and init[name + 2 * n:name + 2 * n + 2] != b"\0\0":
        n += 1
    if not n:
        return None, upload
    cleared = deflate(init[:name] + bytes(2 * n) + init[name + 2 * n:])
    return None, struct.pack(">II", len(cleared), b) + cleared + upload[8 + a:]
