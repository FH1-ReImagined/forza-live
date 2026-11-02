"""Community photos (forza_community.py): photo mode uploads (LargeFileUpload.Create and
Content.UploadContent through the server's dispatch, signed, unsigned and with a raw friends key),
big shots (BMP to PNG), the JPEG check and cleaning, the public feed and images, the operator's
routes, caps, bans and removal. Every picture here is made by this file (make_jpeg, make_bmp): no
game images.

Run: python3 -m unittest discover -p 'test_*.py'
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import struct
import tempfile
import time
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

import forza_auth as auth
import forza_community as community
import forza_ed25519 as ed25519
import forza_friends as friends
import forza_limits as limits
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_photos as photos
import forza_xtsw as x
import forza_xtsw_all as A

try:
    from PIL import Image
except ImportError:  # the CI has the standard library only
    Image = None

ALICE, BOB, CAROL = 0x0009000000000A01, 0x0009000000000B02, 0x0009000000000C03
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol Racer"}
SEEDS = {ALICE: bytes([1]) * 32, BOB: bytes([2]) * 32}
LEGACY, LEGACY_KEY = 0x0009000000000D04, bytes(range(16))  # a game before signed requests
OLD = 0x0009000000000E05  # no key on record at all: its calls are unverified
TITLE = "/Services.Horizon/o.xtsw"
TOKEN = "photo-admin-test"
LAN = ("192.168.1.20", 50000)
SECRET = b"SECRET"


# ---- synthetic JPEGs ----

def segment(marker, payload=b""):
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def huffman(table_class, table_id, symbol):
    """A Huffman table with a single code ("0", one bit) for one symbol."""
    return segment(0xC4, bytes([table_class << 4 | table_id, 1] + [0] * 15 + [symbol]))


def packed(nbits):
    """nbits zero bits, padded with ones to a whole byte."""
    data = bytearray(nbits // 8)
    if nbits % 8:
        data.append((1 << (8 - nbits % 8)) - 1)
    return bytes(data)


def jfif(thumbnail=b""):
    w = 2 if thumbnail else 0
    return segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01" + bytes([w, 1 if w else 0])
                   + thumbnail)


def make_jpeg(w=1280, h=720, comps=3, progressive=False, extra=(), trailing=b"", bits=8,
              sof=None, app0=True):
    """A flat grey JPEG, w x h, made by hand: every block has DC difference 0 and no AC (one bit
    each with single-code Huffman tables). extra: segments put after APP0 (metadata to drop)."""
    blocks = -(-w // 8) * -(-h // 8)
    ids = range(1, comps + 1)
    frame = struct.pack(">BHHB", bits, h, w, comps) + b"".join(bytes([c, 0x11, 0]) for c in ids)
    out = [b"\xff\xd8", jfif() if app0 else b"", *extra, segment(0xDB, b"\x00" + bytes([1]) * 64),
           segment(sof if sof is not None else 0xC2 if progressive else 0xC0, frame),
           huffman(0, 0, 0), huffman(1, 0, 0)]
    if not progressive:
        out += [segment(0xDA, bytes([comps]) + b"".join(bytes([c, 0]) for c in ids) + b"\x00\x3f\x00"),
                packed(blocks * comps * 2)]
    else:  # a DC scan of every component, then one AC scan each (an end-of-band per block)
        out += [segment(0xDA, bytes([comps]) + b"".join(bytes([c, 0]) for c in ids) + b"\x00\x00\x00"),
                packed(blocks * comps)]
        for c in ids:
            out += [segment(0xDA, bytes([1, c, 0]) + b"\x01\x3f\x00"), packed(blocks)]
    out.append(b"\xff\xd9" + trailing)
    return b"".join(out)


def read_json(path):
    with open(path) as f:
        return json.load(f)


def markers(data):
    """The markers of a JPEG's segments, in order (scan data skipped)."""
    found, i = [], 2
    while i < len(data):
        marker = data[i + 1]
        found.append(marker)
        if marker == 0xD9:
            break
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xDA:
            i = community._scan_end(data, i)
    return found


def pattern(n, seed=0):
    return bytes((i * 37 + seed * 101 + i // 7) % 251 for i in range(n))


def make_bmp(w, h, top_down=True, rows=None, header=40, bits=24, compression=0):
    """A 24-bit BMP as photo mode writes its big shot (top-down, 40-byte header), of `rows` (RGB,
    top to bottom) or of a pattern that differs from row to row and tile to tile."""
    if rows is None:
        base = pattern(w * 3 + 3 * h)
        rows = [base[3 * y:3 * y + w * 3] for y in range(h)]
    stride = (w * 3 + 3) & ~3
    pixels = []
    for row in (rows if top_down else rows[::-1]):
        bgr = bytearray(row)
        bgr[0::3], bgr[2::3] = row[2::3], row[0::3]
        pixels.append(bytes(bgr) + bytes(stride - w * 3))
    data = b"".join(pixels)
    info = struct.pack("<IiiHHIIiiII", header, w, -h if top_down else h, 1, bits, compression,
                       len(data), 0, 0, 0, 0) + bytes(header - 40)
    return b"BM" + struct.pack("<IHHI", 14 + len(info) + len(data), 0, 0, 14 + len(info)) + info + data


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


METADATA = (
    segment(0xE1, b"Exif\x00\x00MM\x00\x2a" + SECRET + b"-GPS-41.2N"),
    segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>" + SECRET + b"</x:xmpmeta>"),
    segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + SECRET),
    segment(0xED, b"Photoshop 3.0\x00" + SECRET),
    segment(0xEF, SECRET),
    segment(0xFE, b"comment " + SECRET),
    segment(0xE0, b"JFXX\x00\x10" + SECRET),  # a JFIF extension: an embedded thumbnail
    segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01" + SECRET),
)


class JpegTest(unittest.TestCase):
    def test_metadata_and_trailing_bytes_are_dropped(self):
        original = make_jpeg(extra=METADATA, trailing=b"after the end " + SECRET)
        original = original.replace(jfif(), jfif(b"\x01\x02\x03" + SECRET[:3]), 1)
        clean, w, h = community.clean_jpeg(original)
        self.assertEqual((w, h), (1280, 720))
        self.assertNotIn(SECRET, clean)
        self.assertNotIn(SECRET[:3], clean)
        self.assertEqual(markers(clean), [0xE0, 0xEE, 0xDB, 0xC0, 0xC4, 0xC4, 0xDA, 0xD9])
        self.assertTrue(clean.endswith(b"\xff\xd9"))
        app0 = clean[2:2 + 18]
        self.assertEqual(app0[:4], b"\xff\xe0\x00\x10")
        self.assertEqual(app0[-2:], b"\x00\x00")  # no thumbnail
        self.assertEqual(clean[20:20 + 16], b"\xff\xee\x00\x0e" + b"Adobe\x00\x64\x00\x00\x00\x00\x01")
        self.assertEqual(community.clean_jpeg(clean)[0], clean)  # nothing more to drop

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_the_cleaned_picture_decodes_to_the_same_pixels(self):
        for progressive in (False, True):
            for comps in (1, 3):
                original = make_jpeg(800, 448, comps, progressive, extra=METADATA[1:2] + METADATA[5:6])
                clean = community.clean_jpeg(original)[0]
                a = Image.open(io.BytesIO(original))
                b = Image.open(io.BytesIO(clean))
                self.assertEqual((b.size, b.mode), ((800, 448), "L" if comps == 1 else "RGB"))
                self.assertEqual(a.tobytes(), b.tobytes())
                self.assertNotIn("exif", b.info)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_a_real_encoders_jpeg(self):
        im = Image.effect_noise((1280, 720), 40).convert("RGB")
        exif = Image.Exif()
        exif[0x010F] = "SECRET camera"
        for progressive in (False, True):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85, progressive=progressive, exif=exif.tobytes(),
                    comment=b"SECRET comment", icc_profile=b"SECRET" * 20)
            clean = community.clean_jpeg(buf.getvalue() + SECRET)[0]
            self.assertNotIn(SECRET, clean)
            self.assertEqual(Image.open(io.BytesIO(clean)).tobytes(),
                             Image.open(io.BytesIO(buf.getvalue())).tobytes())

    def test_progressive_and_greyscale_are_taken(self):
        for progressive in (False, True):
            for comps in (1, 3):
                self.assertEqual(community.clean_jpeg(make_jpeg(640, 360, comps, progressive))[1:],
                                 (640, 360))
        self.assertEqual(community.clean_jpeg(make_jpeg(3840, 2160))[1:], (3840, 2160))
        self.assertEqual(community.clean_jpeg(make_jpeg(7680, 4320))[1:], (7680, 4320))

    def test_scan_data_with_stuffed_bytes_restarts_and_fill_bytes(self):
        scan = b"\x12\xff\x00\x34\xff\xd0\x56\xff\xd1\x78"
        original = make_jpeg()
        start = original.index(b"\xff\xda")
        start += 2 + struct.unpack(">H", original[start + 2:start + 4])[0]
        end = original.index(b"\xff\xd9")
        original = (original[:start] + scan + b"\xff\xff\xff" + original[end:])
        original = original.replace(b"\xff\xdb", segment(0xDD, b"\x00\x10") + b"\xff\xdb", 1)
        clean = community.clean_jpeg(original)[0]
        self.assertIn(segment(0xDD, b"\x00\x10"), clean)
        self.assertTrue(clean.endswith(scan + b"\xff\xd9"))

    def test_anything_else_is_refused(self):
        good = make_jpeg()
        bad = {
            "empty": b"",
            "png": b"\x89PNG\r\n\x1a\n" + bytes(64),
            "text": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            "extended sequential": make_jpeg(sof=0xC1),
            "lossless": make_jpeg(sof=0xC3),
            "arithmetic": make_jpeg(sof=0xC9),
            "hierarchical": make_jpeg(sof=0xC5),
            "12-bit": make_jpeg(bits=12),
            "cmyk": make_jpeg(comps=4),
            "two components": make_jpeg(comps=2),
            "too small": make_jpeg(320, 180),
            "too narrow": make_jpeg(639, 360),
            "too large": make_jpeg(8192, 4320),
            "too tall": make_jpeg(7680, 4352),
            "a strip": make_jpeg(4000, 1000),
            "two frames": good.replace(b"\xff\xc4", segment(0xC0, good[good.index(b"\xff\xc0") + 4:
                                                                       good.index(b"\xff\xc4")]) + b"\xff\xc4", 1),
            "no end": good[:-2],
            "cut in the scan": good[:good.index(b"\xff\xda") + 30],
            "no scan": good[:good.index(b"\xff\xda")] + b"\xff\xd9",
            "no frame": good.replace(good[good.index(b"\xff\xc0"):good.index(b"\xff\xc4")], b""),
            "no huffman tables": good.replace(huffman(0, 0, 0) + huffman(1, 0, 0), b""),
            "garbage between segments": good.replace(b"\xff\xc4", b"junk\xff\xc4", 1),
            "a bad length": good.replace(b"\xff\xdb\x00\x43", b"\xff\xdb\xff\xf0", 1),
            "a second start": good.replace(b"\xff\xdb", b"\xff\xd8\xff\xdb", 1),
            "DNL": good.replace(b"\xff\xd9", segment(0xDC, b"\x02\xd0") + b"\xff\xd9"),
            "a reserved marker": good.replace(b"\xff\xdb", segment(0x02, b"x") + b"\xff\xdb", 1),
            "bad restart interval": good.replace(b"\xff\xdb", segment(0xDD, b"\x00") + b"\xff\xdb", 1),
        }
        for name, data in bad.items():
            with self.subTest(name):
                self.assertRaises(community.BadImage, community.clean_jpeg, data)

    def test_too_many_segments(self):
        many = make_jpeg(extra=[segment(0xFE, b"x")] * (community.MAX_SEGMENTS + 1))
        self.assertRaises(community.BadImage, community.clean_jpeg, many)


class BigShotTest(unittest.TestCase):
    def test_png_is_lossless(self):
        rows = [pattern(30, y) for y in range(7)]
        w, h, back = decode_png(community.png(rows, 10, 7))
        self.assertEqual((w, h, back), (10, 7, rows))

    def test_bmp_rows_top_down_bottom_up_and_padding(self):
        base = pattern(642 * 3 + 3 * 360)
        rows = [base[3 * y:3 * y + 642 * 3] for y in range(360)]
        for top_down in (True, False):
            for header in (40, 124):
                got, w, h = community.read_bmp(make_bmp(642, 360, top_down, rows, header))
                self.assertEqual((w, h, got), (642, 360, rows))

    def assert_halved(self, rows, got, ys):
        """Each pixel of the rows got (at the ys) is the mean of its 2 x 2 in rows, within 1."""
        for y, row in zip(ys, got):
            for x in range(0, len(row) // 3, 37):
                for c in range(3):
                    quad = [rows[2 * y + dy][(2 * x + dx) * 3 + c] for dy in (0, 1) for dx in (0, 1)]
                    self.assertLessEqual(abs(row[x * 3 + c] - sum(quad) / 4), 1)

    def test_halving_averages_each_2_by_2(self):
        bmp = make_bmp(1280, 720)
        with mock.patch.object(community, "COPY_MAX_WIDTH", 640):
            png, w, h = community.bmp_to_png(bmp)
        self.assertEqual((w, h), (640, 360))
        _, _, got = decode_png(png)
        self.assert_halved(community.read_bmp(bmp)[0], got, range(360))
        with mock.patch.object(community, "COPY_MAX_WIDTH", 4000):  # wide enough: as it is
            self.assertEqual(decode_png(community.bmp_to_png(bmp)[0])[2], community.read_bmp(bmp)[0])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_a_big_shot_becomes_a_1920_png(self):
        bmp = make_bmp(3840, 2160)
        png, w, h = community.bmp_to_png(bmp)
        self.assertEqual((w, h), (1920, 1080))
        self.assertLess(len(png), len(bmp) // 4)
        im = Image.open(io.BytesIO(png))
        self.assertEqual((im.size, im.mode, im.info), ((1920, 1080), "RGB", {}))
        data, ys = im.tobytes(), range(0, 1080, 97)
        self.assert_halved(community.read_bmp(bmp)[0], [data[y * 5760:(y + 1) * 5760] for y in ys], ys)

    def test_a_failed_capture_is_refused(self):
        tile = [pattern(640 * 3, y) for y in range(360)]  # 640x360 tiles, 3 x 3
        rows = [r * 3 for r in tile] * 3
        with self.assertRaisesRegex(community.BadImage, "same picture"):
            community.bmp_to_png(make_bmp(1920, 1080, rows=rows))
        rows[500] = bytes(len(rows[500]))  # one tile differs: a picture
        self.assertEqual(community.bmp_to_png(make_bmp(1920, 1080, rows=rows))[1:], (1920, 1080))

    def test_other_bmps_are_refused(self):
        for name, data in {"32-bit": make_bmp(640, 360)[:28] + b"\x20\x00" + make_bmp(640, 360)[30:],
                           "compressed": make_bmp(640, 360, compression=1),
                           "core header": make_bmp(640, 360, header=40)[:14] + b"\x0c" + make_bmp(640, 360)[15:],
                           "too small": make_bmp(320, 180), "a strip": make_bmp(4000, 1000),
                           "cut short": make_bmp(640, 360)[:-100], "not a bmp": b"BA" + bytes(100)}.items():
            with self.subTest(name):
                self.assertRaises(community.BadImage, community.picture, data)


# ---- the server ----

class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir.name,  # the real wiring
                                         "--server-name", "forza.dyotson.dev"]))
        auth._nonces.clear()
        for xuid, name in list(NAMES.items()) + [(LEGACY, "Legacy"), (OLD, "Old Timer")]:
            lsp.save_player(xuid, {"gamertag": name})
        store = friends.Store()
        for xuid, seed in SEEDS.items():
            store.get(xuid, create=True)["pub"] = ed25519.public_key(seed).hex()
        store.get(CAROL, create=True)["pub"] = ed25519.public_key(bytes([3]) * 32).hex()
        SEEDS[CAROL] = bytes([3]) * 32
        store.get(LEGACY, create=True)["key"] = friends.key_hash(LEGACY_KEY)
        self.server = SimpleNamespace(friends=SimpleNamespace(store=store))
        self.token = mock.patch.object(lsp, "ADMIN_TOKEN", TOKEN)
        self.token.start()

    def tearDown(self):
        self.token.stop()
        limits.configure_proxies()
        lsp.DATA_DIR = self.old_dir
        self.dir.cleanup()

    # the game's calls

    def call(self, xuid, cls, method, ptype, params, rtype, how="signed"):
        headers = {"x-classname": "%s, Forza.WebServices" % cls, "x-method": method,
                   "x-userid": "0x%x" % xuid}
        body = bytes(x.encode(ptype, params))
        if how == "signed":
            headers[auth.HEADER] = auth.auth_header(SEEDS[xuid], "forza.dyotson.dev", xuid, "POST",
                                                    TITLE, body, headers)
        elif how == "raw":
            headers["x-friends-key"] = LEGACY_KEY.hex()
        reply = lsp.service_call(headers, body, self.server, "POST", TITLE)
        if reply[:4] != bytes(4):
            return None
        return x.decode(rtype, reply, 12)[0]

    def upload(self, xuid, data, name="PHOTO1", how="signed", overwrite=True, chunk=65536,
               last=None):
        """Photo mode's upload: LargeFileUpload.Create, then the picture in chunks (the last one
        sent `last` way when given). The file id, or None when the game would see a failure."""
        r = self.call(xuid, "Forza.WebServices.LargeFileUpload", "Create",
                      A.ForzaLargeFileUploadCreate_Params,
                      {"payloadSize": len(data), "overwriteOldest": overwrite, "userFileName": name},
                      A.ForzaLargeFileUploadCreate_Results, how)
        if r is None:
            return None
        fid = r["contentFileId"]
        for offset in range(0, len(data), chunk):
            final = offset + chunk >= len(data)
            got = self.call(xuid, "Xls.WebServices.Content", "UploadContent",
                            A.ContentUploadContent_Params,
                            {"contentId": fid, "payloadOffset": offset, "payloadTotalSize": len(data),
                             "uploadData": data[offset:offset + chunk]},
                            A.ContentUploadContent_Results, last if final and last else how)
            if got is None:
                return None
        f = photos.load()["files"].get(str(fid))
        self.assertTrue(f is None or f["done"])  # a big shot is forgotten once it is in
        return fid

    def download(self, xuid, fid, how="signed"):
        r = self.call(xuid, "Xls.WebServices.Content", "DownloadContent",
                      A.ContentDownloadContent_Params,
                      {"contentId": fid, "payloadOffset": 0, "requestedSize": 1 << 20,
                       "userInitiated": True}, A.ContentDownloadContent_Results, how)
        return r["downloadData"]

    # HTTP

    def http(self, method, path, headers=None, body=b"", peer=LAN):
        answer = lsp.handle(self.server, method, path, {k.lower(): v for k, v in (headers or {}).items()},
                            body, peer)
        status, ctype, data = answer[:3]
        extra = dict(line.split(": ", 1) for line in (answer[3] if len(answer) > 3 else ()))
        return status, ctype, data, extra

    def gallery(self):
        status, ctype, data, extra = self.http("GET", "/community/gallery.json")
        self.assertEqual((status, ctype), (200, "application/json"))
        return json.loads(data)

    def ids(self):
        return [p["id"] for p in self.gallery()["photos"]]

    def admin(self, method, path, body=b"", **kw):
        headers = {"X-Admin-Token": TOKEN}
        headers.update(kw.pop("headers", {}))
        status, _, data, _ = self.http(method, "/admin/photos" + path, headers, body, **kw)
        return status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    def records(self):
        return community.load()["photos"]

    def only_photo(self):
        (pid, p), = self.records().items()
        return pid, p


class UploadTest(Base):
    def test_a_signed_upload_is_published(self):
        original = make_jpeg(extra=METADATA, trailing=SECRET)
        before = time.time()
        fid = self.upload(ALICE, original, name="Sunset at the lake.jpg")
        self.assertEqual(self.download(ALICE, fid), original)  # the player's own file, as sent
        pid, p = self.only_photo()
        self.assertRegex(pid, "^[A-Z2-7]{12}$")
        self.assertNotEqual(pid, str(fid))
        self.assertEqual((p["owner"], p["fileId"], p["status"], p["gamertag"], p["title"]),
                         (ALICE, fid, "live", "Alice", "Sunset at the lake"))
        doc = self.gallery()
        self.assertEqual(doc["version"], 1)
        self.assertGreaterEqual(doc["generated"], int(before))
        entry, = doc["photos"]
        self.assertEqual(sorted(entry), ["approvedAt", "gamertag", "h", "id", "image", "sha256",
                                         "takenAt", "title", "w"])
        self.assertEqual((entry["id"], entry["gamertag"], entry["title"], entry["w"], entry["h"]),
                         (pid, "Alice", "Sunset at the lake", 1280, 720))
        self.assertEqual(entry["image"], "/community/img/%s.jpg" % pid)
        self.assertGreaterEqual(entry["takenAt"], int(before))
        self.assertEqual(entry["approvedAt"], entry["takenAt"])
        status, ctype, data, extra = self.http("GET", entry["image"])
        self.assertEqual((status, ctype), (200, "image/jpeg"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        self.assertEqual(data, community.clean_jpeg(original)[0])
        self.assertNotIn(SECRET, data)
        self.assertEqual(extra["X-Content-Type-Options"], "nosniff")
        self.assertEqual(extra["Content-Security-Policy"], "default-src 'none'")
        self.assertIn("max-age=", extra["Cache-Control"])
        self.assertEqual(p["size"], len(data))

    def test_unsigned_and_raw_key_uploads_stay_private(self):
        data = make_jpeg()
        fid_old = self.upload(OLD, data, how="none")  # an older game, no key on record
        fid_legacy = self.upload(LEGACY, data, how="raw")  # a raw friends key
        self.assertEqual(self.download(OLD, fid_old, "none"), data)
        self.assertEqual(self.download(LEGACY, fid_legacy, "raw"), data)
        self.assertEqual(self.records(), {})
        self.assertEqual(self.gallery()["photos"], [])
        # a signing player's unsigned call is someone else's: refused outright
        self.assertIsNone(self.upload(ALICE, data, how="none"))
        self.assertIsNone(self.upload(ALICE, data, how="raw"))
        self.assertEqual(self.records(), {})

    def test_an_upload_created_unsigned_stays_private(self):
        """A file created before its player's key was on record (unverified) and finished with a
        signed call is not published: both ends of the upload must be signed."""
        dave, seed = 0x0009000000000F06, bytes([4]) * 32
        lsp.save_player(dave, {"gamertag": "Dave"})
        SEEDS[dave] = seed
        data = make_jpeg()
        r = self.call(dave, "Forza.WebServices.LargeFileUpload", "Create",
                      A.ForzaLargeFileUploadCreate_Params,
                      {"payloadSize": len(data), "overwriteOldest": True, "userFileName": "P"},
                      A.ForzaLargeFileUploadCreate_Results, "none")
        self.server.friends.store.get(dave, create=True)["pub"] = ed25519.public_key(seed).hex()
        self.assertEqual(self.call(dave, "Xls.WebServices.Content", "UploadContent",
                                   A.ContentUploadContent_Params,
                                   {"contentId": r["contentFileId"], "payloadOffset": 0,
                                    "payloadTotalSize": len(data), "uploadData": data},
                                   A.ContentUploadContent_Results)["payloadUploadedSize"], len(data))
        self.assertEqual(self.records(), {})
        self.upload(dave, data)  # signed from the start
        self.assertEqual(len(self.records()), 1)

    def test_not_a_jpeg_stays_private(self):
        for data in (b"\x89PNG\r\n\x1a\n" + os.urandom(4000), make_jpeg(320, 180),
                     make_jpeg(sof=0xC1)):
            fid = self.upload(ALICE, data)
            self.assertEqual(self.download(ALICE, fid), data)  # the game's upload succeeded
        self.assertEqual(self.records(), {})

    def test_no_gamertag_no_photo(self):
        lsp.save_player(BOB, {"gamertag": "  "})
        self.upload(BOB, make_jpeg())
        self.assertEqual(self.records(), {})

    def test_gamertag_and_title_are_cleaned(self):
        self.upload(CAROL, make_jpeg(), name="  My\tphoto\x07 été " + "x" * 80)
        pid, p = self.only_photo()
        self.assertEqual(p["gamertag"], "Carol Racer")
        self.assertEqual(p["title"], ("My photo ete " + "x" * 80)[:community.TITLE_MAX])

    def test_the_same_picture_again_moves_to_the_newer_file(self):
        data = make_jpeg()
        first = self.upload(ALICE, data)
        pid, _ = self.only_photo()
        second = self.upload(ALICE, data, name="again")
        self.assertEqual(self.only_photo(), (pid, self.records()[pid]))
        self.assertEqual(self.records()[pid]["fileId"], second)
        self.assertNotEqual(first, second)
        self.upload(BOB, data)  # another player's copy is another photo
        self.assertEqual(len(self.records()), 2)

    def test_photos_survive_a_restart(self):
        self.upload(ALICE, make_jpeg())
        pid, p = self.only_photo()
        community.configure(self.dir.name)
        self.assertEqual(self.records(), {pid: p})
        self.assertEqual(self.ids(), [pid])

    def test_feed_is_newest_first_and_capped(self):
        with mock.patch.object(community, "FEED_MAX", 3):
            made = []
            for n in range(5):
                with mock.patch.object(community.time, "time", return_value=1_800_000_000 + n):
                    self.upload(ALICE, make_jpeg(640 + 8 * n, 360), name="P%d" % n)
                made.append(next(pid for pid, p in self.records().items() if p["title"] == "P%d" % n))
            self.assertEqual(self.ids(), made[::-1][:3])

    def test_gallery_has_no_owner_or_xuid_anywhere(self):
        for xuid in (ALICE, BOB, CAROL):
            self.upload(xuid, make_jpeg())
        status, _, data, _ = self.http("GET", "/community/gallery.json")

        def keys(v):
            if isinstance(v, dict):
                for k, item in v.items():
                    yield k
                    yield from keys(item)
            elif isinstance(v, list):
                for item in v:
                    yield from keys(item)
        found = set(keys(json.loads(data)))
        self.assertFalse({"owner", "xuid", "fileId", "note", "created"} & {k.lower() for k in found})
        self.assertFalse(any("xuid" in k.lower() or "owner" in k.lower() for k in found))
        for xuid in (ALICE, BOB, CAROL):
            for text in ("%016X" % xuid, "%016x" % xuid, "%x" % xuid, str(xuid)):
                self.assertNotIn(text.encode(), data)
        self.assertEqual(len(json.loads(data)["photos"]), 3)

    def test_feed_reads_have_no_side_effects_and_answer_etags(self):
        self.upload(ALICE, make_jpeg())
        index = os.path.join(self.dir.name, "community.json")
        before = (os.stat(index).st_mtime_ns, sorted(os.listdir(self.dir.name)))
        status, _, data, extra = self.http("GET", "/community/gallery.json")
        self.assertEqual(status, 200)
        self.assertIn("max-age=", extra["Cache-Control"])
        again = self.http("GET", "/community/gallery.json", {"If-None-Match": extra["ETag"]})
        self.assertEqual((again[0], again[2]), (304, b""))
        pid = self.ids()[0]
        img = self.http("GET", "/community/img/%s.jpg" % pid)
        self.assertEqual(self.http("GET", "/community/img/%s.jpg" % pid,
                                   {"If-None-Match": img[3]["ETag"]})[0], 304)
        self.assertEqual((os.stat(index).st_mtime_ns, sorted(os.listdir(self.dir.name))), before)
        self.upload(BOB, make_jpeg())  # a new photo: a new ETag
        self.assertEqual(self.http("GET", "/community/gallery.json",
                                   {"If-None-Match": extra["ETag"]})[0], 200)

    def test_public_routes(self):
        self.upload(ALICE, make_jpeg())
        pid = self.ids()[0]
        for method, path in (("POST", "/community/gallery.json"), ("PUT", "/community/img/%s.jpg" % pid),
                             ("HEAD", "/community/gallery.json")):
            self.assertEqual(self.http(method, path)[0], 405)
        for path in ("/community/img/%s.png" % pid, "/community/img/%s.jpg" % pid.lower(),
                     "/community/img/../community.json", "/community/img/AAAAAAAAAAAA.jpg",
                     "/community/", "/community/photos"):
            self.assertEqual(self.http("GET", path)[0], 404, path)

    def test_image_requests_have_a_budget(self):
        self.upload(ALICE, make_jpeg())
        path = "/community/img/%s.jpg" % self.ids()[0]
        with mock.patch.object(community, "IMAGES_PER_MINUTE", 3):
            self.assertEqual([self.http("GET", path)[0] for _ in range(4)], [200, 200, 200, 429])

    def test_the_disk_quota_counts_community_photos(self):
        self.assertIn("community", limits.BLOB_DIRS)
        data = make_jpeg()
        self.upload(ALICE, data)
        size = self.only_photo()[1]["size"]
        self.assertEqual(os.path.getsize(community.image_path(self.only_photo()[0])), size)
        limits.configure(self.dir.name)
        self.assertGreaterEqual(limits.usage(), len(data) + size)
        with mock.patch.object(limits, "DISK_CAP", limits.usage() + len(data) + size // 2):
            self.upload(BOB, data)  # the private file fits, its community copy does not
        self.assertEqual(len(self.records()), 1)


class BigShotUploadTest(Base):
    """Photo mode's large photo: LargeFileUpload.Create(size, overwriteOldest false,
    "ForzaBigshot.bmp") and the BMP in 256 KiB chunks."""

    def big_shot(self, xuid, data, how="signed"):
        return self.upload(xuid, data, name="ForzaBigshot.bmp", how=how, overwrite=False,
                           chunk=262144)

    def test_a_signed_big_shot_is_published_as_a_png(self):
        bmp = make_bmp(1280, 720)
        self.assertGreater(len(bmp), limits.MAX_BLOB)
        fid = self.big_shot(ALICE, bmp)
        pid, p = self.only_photo()
        self.assertEqual((p["type"], p["title"], p["w"], p["h"]), ("png", "", 1280, 720))
        entry, = self.gallery()["photos"]
        self.assertEqual((entry["image"], entry["title"]), ("/community/img/%s.png" % pid, ""))
        status, ctype, data, extra = self.http("GET", entry["image"])
        self.assertEqual((status, ctype), (200, "image/png"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        self.assertEqual(decode_png(data)[2], community.read_bmp(bmp)[0])
        self.assertEqual(self.http("GET", "/community/img/%s.jpg" % pid)[0], 404)
        # not kept as a private file: the community copy is all there is
        self.assertNotIn(str(fid), photos.load()["files"])
        self.assertEqual(os.listdir(os.path.join(self.dir.name, "photos")), [])
        self.assertEqual(self.download(ALICE, fid), b"")

    def test_older_games_big_shots_are_refused_as_before(self):
        bmp = make_bmp(1280, 720)
        self.assertIsNone(self.big_shot(OLD, bmp, how="none"))
        self.assertIsNone(self.big_shot(LEGACY, bmp, how="raw"))
        self.assertEqual((self.records(), photos.load()["files"]), ({}, {}))
        small = make_jpeg()  # what fits the old limit is still taken, privately
        self.assertEqual(self.download(OLD, self.upload(OLD, small, how="none"), "none"), small)

    def test_a_failed_capture_is_not_published(self):
        tile = [pattern(640 * 3, y) for y in range(360)]
        fid = self.big_shot(ALICE, make_bmp(1920, 1080, rows=[r * 3 for r in tile] * 3))
        self.assertIsNotNone(fid)  # the game is told it worked
        self.assertEqual((self.records(), photos.load()["files"]), ({}, {}))

    def test_big_shots_an_hour_and_one_unfinished_per_player(self):
        size = len(make_bmp(1280, 720))
        create = lambda xuid: self.call(  # noqa: E731
            xuid, "Forza.WebServices.LargeFileUpload", "Create", A.ForzaLargeFileUploadCreate_Params,
            {"payloadSize": size, "overwriteOldest": False, "userFileName": "ForzaBigshot.bmp"},
            A.ForzaLargeFileUploadCreate_Results)
        first = create(ALICE)["contentFileId"]
        second = create(ALICE)["contentFileId"]
        self.assertEqual(list(photos.load()["files"]), [str(second)])  # the first was dropped
        self.assertNotEqual(first, second)
        with mock.patch.object(photos, "BIG_PER_HOUR", 3):
            self.assertIsNotNone(create(BOB))
            self.assertIsNone(create(CAROL))
        self.assertIsNone(self.call(
            ALICE, "Forza.WebServices.LargeFileUpload", "Create", A.ForzaLargeFileUploadCreate_Params,
            {"payloadSize": photos.MAX_BIG + 1, "overwriteOldest": False, "userFileName": "x"},
            A.ForzaLargeFileUploadCreate_Results))


class CapsTest(Base):
    def test_per_player_day(self):
        with mock.patch.object(community, "PER_DAY", 3):
            for n in range(4):
                self.upload(ALICE, make_jpeg(640 + 8 * n, 360))
            self.assertEqual(len(self.records()), 3)
            self.upload(BOB, make_jpeg())
            self.assertEqual(len(self.records()), 4)
            later = time.time() + 86401
            with mock.patch.object(community.time, "time", return_value=later):
                self.upload(ALICE, make_jpeg(800, 360))
            self.assertEqual(len(self.records()), 5)

    def test_per_player_total(self):
        with mock.patch.object(community, "PER_PLAYER", 2):
            for n in range(3):
                self.upload(ALICE, make_jpeg(640 + 8 * n, 360))
            self.assertEqual(len(self.records()), 2)
            pid = next(iter(self.records()))
            self.assertEqual(self.admin("POST", "/%s/delete" % pid)[0], 200)
            self.upload(ALICE, make_jpeg(800, 360))
            self.assertEqual(len(self.records()), 2)

    def test_server_hour(self):
        with mock.patch.object(community, "PER_HOUR", 2):
            for xuid in (ALICE, BOB, CAROL):
                self.upload(xuid, make_jpeg())
            self.assertEqual(len(self.records()), 2)
            with mock.patch.object(community.time, "time", return_value=time.time() + 3601):
                self.upload(CAROL, make_jpeg())
            self.assertEqual(len(self.records()), 3)


class ModerationTest(Base):
    def test_hidden_and_held_photos_are_not_served(self):
        self.upload(ALICE, make_jpeg())
        pid, p = self.only_photo()
        image = "/community/img/%s.jpg" % pid
        self.assertEqual(self.admin("POST", "/%s/hide" % pid)[0], 200)
        self.assertEqual((self.ids(), self.http("GET", image)[0]), ([], 404))
        status, body = self.admin("GET", "/img/%s.jpg" % pid)  # the operator still sees it
        self.assertEqual((status, hashlib.sha256(body).hexdigest()), (200, p["sha256"]))
        self.assertEqual(self.admin("POST", "/%s/unhide" % pid)[0], 200)
        self.assertEqual((self.ids(), self.http("GET", image)[0]), ([pid], 200))
        self.assertEqual(self.admin("POST", "/%s/hold" % pid, b'{"note": "check the plate"}')[0], 200)
        self.assertEqual((self.ids(), self.http("GET", image)[0]), ([], 404))
        status, queue = self.admin("GET", "/queue")
        self.assertEqual([(q["id"], q["note"]) for q in queue["photos"]], [(pid, "check the plate")])
        self.assertEqual(self.admin("POST", "/%s/release" % pid)[0], 200)
        self.assertEqual((self.ids(), self.http("GET", image)[0]), ([pid], 200))
        self.assertEqual(self.admin("GET", "/queue")[1]["photos"], [])

    def test_a_worker_cannot_release_what_the_operator_hid(self):
        self.upload(ALICE, make_jpeg())
        pid, _ = self.only_photo()
        self.admin("POST", "/%s/hold" % pid)
        self.admin("POST", "/%s/hide" % pid)
        self.assertEqual(self.admin("POST", "/%s/release" % pid)[0], 409)
        self.assertEqual(self.admin("POST", "/%s/unhide" % pid)[0], 200)
        self.assertEqual(self.admin("POST", "/%s/unhide" % pid)[0], 409)
        self.assertEqual(self.ids(), [pid])

    def test_new_photos_held_for_screening(self):
        with mock.patch.object(community, "HOLD_NEW", True):
            self.upload(ALICE, make_jpeg())
        pid, p = self.only_photo()
        self.assertEqual((p["status"], p["approvedAt"], self.ids()), ("held", None, []))
        with mock.patch.object(community.time, "time", return_value=time.time() + 600):
            self.admin("POST", "/%s/release" % pid)
        entry, = self.gallery()["photos"]
        self.assertGreaterEqual(entry["approvedAt"], entry["takenAt"] + 600)

    def test_delete(self):
        data = make_jpeg()
        fid = self.upload(ALICE, data)
        pid, _ = self.only_photo()
        self.assertEqual(self.admin("POST", "/%s/delete" % pid), (200, {"deleted": pid}))
        self.assertEqual((self.records(), self.ids()), ({}, []))
        self.assertFalse(os.path.exists(community.image_path(pid)))
        self.assertEqual(self.download(ALICE, fid), data)  # the player's own file stays
        self.upload(ALICE, data)  # the same picture is not published again
        self.assertEqual(self.records(), {})
        self.upload(ALICE, make_jpeg(800, 360))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.admin("POST", "/%s/delete" % pid)[0], 404)

    def test_ban_blocks_and_hides(self):
        self.upload(ALICE, make_jpeg())
        self.upload(BOB, make_jpeg())
        alice = next(pid for pid, p in self.records().items() if p["owner"] == ALICE)
        status, body = self.admin("POST", "/ban/%016X" % ALICE)
        self.assertEqual((status, body["banned"], body["changed"]), (200, True, True))
        self.assertEqual(read_json(os.path.join(self.dir.name, "banned_xuids.json")),
                         ["%016X" % ALICE])
        self.assertEqual(len(self.ids()), 1)
        self.assertNotIn(alice, self.ids())
        self.assertEqual(self.http("GET", "/community/img/%s.jpg" % alice)[0], 404)
        self.upload(ALICE, make_jpeg(800, 360))  # blocked
        self.assertEqual(len(self.records()), 2)
        listing = self.admin("GET", "")[1]
        self.assertEqual(listing["banned"], ["%016X" % ALICE])
        self.assertEqual({p["id"]: p["banned"] for p in listing["photos"]}[alice], True)
        self.assertEqual(self.admin("POST", "/ban/%016X" % ALICE)[1]["changed"], False)
        self.assertEqual(self.admin("POST", "/unban/%016X" % ALICE)[0], 200)
        self.assertIn(alice, self.ids())
        # banning the owner of a photo, by the photo's id
        bob = next(pid for pid, p in self.records().items() if p["owner"] == BOB)
        self.assertEqual(self.admin("POST", "/%s/ban" % bob)[1]["xuid"], "%016X" % BOB)
        self.assertEqual(self.ids(), [alice])

    def test_a_ban_list_written_by_hand(self):
        path = os.path.join(self.dir.name, "banned_xuids.json")
        with open(path, "w") as f:
            json.dump(["9000000000c03", "%016x" % BOB], f)
        self.upload(BOB, make_jpeg())
        self.assertEqual(self.records(), {})
        self.assertEqual(self.admin("POST", "/unban/%016X" % BOB)[0], 200)
        self.assertEqual(read_json(path), ["9000000000c03"])  # the rest as it was written
        with open(path, "w") as f:
            f.write("not json")
        self.assertEqual(self.admin("POST", "/ban/%016X" % BOB)[0], 409)
        with open(path) as f:
            self.assertEqual(f.read(), "not json")

    def test_listing_and_unknown_actions(self):
        self.upload(ALICE, make_jpeg())
        pid, p = self.only_photo()
        status, listing = self.admin("GET", "")
        entry, = listing["photos"]
        self.assertEqual((entry["id"], entry["owner"], entry["status"], entry["public"]),
                         (pid, "%016X" % ALICE, "live", True))
        self.assertEqual(self.admin("GET", "?status=hidden")[1]["photos"], [])
        self.assertEqual(self.admin("POST", "/%s/approve" % pid)[0], 404)
        self.assertEqual(self.admin("POST", "/AAAAAAAAAAAA/hide")[0], 404)
        self.assertEqual(self.admin("POST", "/ban/not-hex")[0], 400)
        self.assertEqual(self.admin("POST", "/%s/hide" % pid, b"[1]")[0], 400)
        self.assertEqual(self.admin("DELETE", "/%s" % pid)[0], 405)


class AdminAuthTest(Base):
    def setUp(self):
        super().setUp()
        self.upload(ALICE, make_jpeg())
        self.pid = self.only_photo()[0]

    def status(self, headers, peer=LAN, path="", method="GET"):
        return self.http(method, "/admin/photos" + path, headers, b"", peer)[0]

    def test_the_token_in_its_header_only(self):
        self.assertEqual(self.status({}), 403)
        self.assertEqual(self.status({"X-Admin-Token": "wrong"}), 403)
        self.assertEqual(self.status({}, path="?token=" + TOKEN), 403)
        self.assertEqual(self.status({"X-Admin-Token": TOKEN}), 200)
        self.assertEqual(self.status({}, path="/%s/hide?token=%s" % (self.pid, TOKEN),
                                     method="POST"), 403)
        self.assertEqual(self.records()[self.pid]["status"], "live")

    def test_this_machine_without_a_token(self):
        self.assertEqual(self.status({}, peer=("127.0.0.1", 40000)), 200)

    def test_never_through_the_tunnel_or_a_proxy(self):
        token = {"X-Admin-Token": TOKEN}
        for proxy in ({"X-Forwarded-For": "203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
                      {"Forwarded": "for=203.0.113.9"}):
            self.assertEqual(self.status(dict(token, **proxy)), 403)
            self.assertEqual(self.status(dict(proxy), peer=("127.0.0.1", 40000)), 403)
        limits.configure_proxies("127.0.0.1")  # the tunnel agent on this machine
        self.assertEqual(self.status(token, peer=("127.0.0.1", 40000)), 403)
        self.assertEqual(self.status({}, peer=("127.0.0.1", 40000)), 403)
        self.assertEqual(self.status(token), 200)  # the LAN still works
        # the public routes do not care
        self.assertEqual(self.http("GET", "/community/gallery.json", {}, b"", ("127.0.0.1", 1))[0], 200)

    def test_browser_requests_are_refused(self):
        self.assertEqual(self.status({"X-Admin-Token": TOKEN, "Origin": "http://192.168.1.20:21201"}), 403)
        self.assertEqual(self.status({"Origin": "https://evil.example"}, peer=("127.0.0.1", 1),
                                     path="/%s/delete" % self.pid, method="POST"), 403)
        self.assertIn(self.pid, self.records())


class RemovalTest(Base):
    def test_overwrite_oldest_removes_the_community_photo(self):
        with mock.patch.object(photos, "MAX_PER_PLAYER", 2):
            first = self.upload(ALICE, make_jpeg(640, 360))
            self.upload(ALICE, make_jpeg(648, 360))
            old = next(pid for pid, p in self.records().items() if p["fileId"] == first)
            self.assertEqual(len(self.ids()), 2)
            self.upload(ALICE, make_jpeg(656, 360))  # the oldest file makes room
        self.assertNotIn(str(first), photos.load()["files"])
        self.assertNotIn(old, self.records())
        self.assertNotIn(old, self.ids())
        self.assertEqual(len(self.ids()), 2)
        self.assertFalse(os.path.exists(community.image_path(old)))
        self.assertEqual(self.http("GET", "/community/img/%s.jpg" % old)[0], 404)

    def test_a_private_file_going_leaves_other_photos(self):
        with mock.patch.object(photos, "MAX_PER_PLAYER", 1):
            self.upload(ALICE, make_jpeg(), how="signed")
            self.upload(BOB, make_jpeg(640, 360))
            self.upload(ALICE, b"not a picture")  # replaces Alice's file
        self.assertEqual([p["owner"] for p in self.records().values()], [BOB])

    def test_a_hook_failure_never_fails_the_game(self):
        with mock.patch.object(community, "clean_jpeg", side_effect=RuntimeError("boom")):
            with self.assertLogs("forza-live", "ERROR"):
                fid = self.upload(ALICE, make_jpeg())
        self.assertIsNotNone(fid)
        self.assertEqual(self.records(), {})


class HttpServerTest(Base, unittest.IsolatedAsyncioTestCase):
    """The routes through the server's HTTP handler: the extra headers reach the wire, and
    /admin/photos is not taken by the other /admin/ endpoints (whose rules differ)."""

    async def request(self, path, headers=()):
        server = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                            "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n"
                          % (path, "".join(h + "\r\n" for h in headers))).encode())
            raw = await reader.read()
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        head, body = raw.split(b"\r\n\r\n", 1)
        return head.decode(), body

    async def test_headers_on_the_wire(self):
        self.upload(ALICE, make_jpeg())
        pid = self.only_photo()[0]
        head, body = await self.request("/community/img/%s.jpg" % pid)
        self.assertIn("HTTP/1.1 200", head)
        self.assertIn("Content-Type: image/jpeg", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertIn("Content-Security-Policy: default-src 'none'", head)
        self.assertIn("Cache-Control: public, max-age=", head)
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.only_photo()[1]["sha256"])
        head, body = await self.request("/community/gallery.json")
        self.assertIn("ETag: ", head)
        self.assertEqual(json.loads(body)["photos"][0]["id"], pid)
        # /admin/'s other endpoints take ?token= even through a proxy; photos never do
        head, _ = await self.request("/admin/photos?token=" + TOKEN, ["X-Forwarded-For: 203.0.113.9"])
        self.assertIn("HTTP/1.1 403", head)
        head, _ = await self.request("/admin/photos", ["X-Forwarded-For: 203.0.113.9",
                                                       "X-Admin-Token: " + TOKEN])
        self.assertIn("HTTP/1.1 403", head)
        head, _ = await self.request("/admin/photos")  # this machine, no proxy: allowed
        self.assertIn("HTTP/1.1 200", head)


if __name__ == "__main__":
    unittest.main()
