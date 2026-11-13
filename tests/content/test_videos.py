"""Community videos (videos.py): the game's movie uploads told from pictures by their ASF header,
the header's checks, caps, the converter's results (checked, kept or dropped), the clips the
converter never answered, the public feed and its media with byte ranges, the operator's routes,
the owner's delete, bans and removal. Every clip and video here is made by this file (make_asf,
make_mp4): no game footage. ConverterTest also runs the real converter (video/video_worker.py)
on a clip ffmpeg makes from a test pattern; it is skipped when ffmpeg is not installed.

Run: python3 -m unittest discover -s tests -t .
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from forza_live.content import community
from forza_live.content import owners
from forza_live.content import photos
from forza_live.content import videos
from forza_live.competition import leaderboards
from forza_live.core import limits
from forza_live import server as srv
from tests.content import test_community as tc
from tests.support import ROOT

ALICE, BOB = tc.ALICE, tc.BOB
OWNER_TOKEN = "owner-test"


def guid(text):
    return uuid.UUID(text).bytes_le


def asf_object(kind, body):
    return kind + struct.pack("<Q", 24 + len(body)) + body


def make_asf(seconds=15.0, w=640, h=360, title="Subaru WRX '05 on Beaumont - Stage 3",
             fourcc=b"WVC1", audio=True, videos_n=1, broadcast=False, data=4096, extra=()):
    """An ASF clip's bytes as the game writes them: the Header Object with File Properties,
    Stream Properties (VC-1 video, WMA 2 audio) and Content Description, then a Data Object of
    `data` filler bytes."""
    props = (b"\x11" * 16 + struct.pack("<QQQ", 0, 0, 1) + struct.pack(
        "<QQQI", int((seconds + 3) * 1e7), int(seconds * 1e7), 3000, 1 if broadcast else 2)
        + struct.pack("<III", 3200, 3200, 4194304))
    objects = [asf_object(videos.ASF_FILE_PROPERTIES, props)]
    bitmap = struct.pack("<IiiHH4s", 40, w, h, 1, 24, fourcc) + bytes(20)
    video_spec = struct.pack("<IIBH", w, h, 2, len(bitmap)) + bitmap
    for n in range(videos_n):
        objects.append(asf_object(videos.ASF_STREAM_PROPERTIES, videos.ASF_VIDEO + bytes(16)
                                  + struct.pack("<QIIHI", 0, len(video_spec), 0, n + 2, 0)
                                  + video_spec))
    if audio:
        wave = struct.pack("<HHIIHHH", 0x0161, 2, 48000, 16000, 2731, 16, 0)
        objects.append(asf_object(videos.ASF_STREAM_PROPERTIES, videos.ASF_AUDIO + bytes(16)
                                  + struct.pack("<QIIHI", 0, len(wave), 0, 1, 0) + wave))
    t = (title + "\x00").encode("utf-16-le")
    author = "dyotson\x00".encode("utf-16-le")
    objects.append(asf_object(videos.ASF_CONTENT_DESCRIPTION,
                              struct.pack("<HHHHH", len(t), len(author), 0, 0, 0) + t + author))
    objects += list(extra)
    body = b"".join(objects)
    header = (videos.ASF_HEADER + struct.pack("<QI", 30 + len(body), len(objects)) + b"\x01\x02"
              + body)
    return header + guid("75B22636-668E-11CF-A6D9-00AA0062CE6C") + struct.pack(
        "<Q", 50 + data) + bytes(26) + bytes(data)


def box(kind, body=b""):
    return struct.pack(">I", 8 + len(body)) + kind + body


def make_mp4(payload=b"\x00" * 2048, brand=b"isom", extra=b""):
    """An MP4's top-level boxes as the converter writes them (ftyp, moov with an avc1 track, mdat)."""
    return (box(b"ftyp", brand + b"\x00\x00\x02\x00isomiso2avc1mp41")
            + box(b"moov", box(b"trak", box(b"mdia", box(b"stsd", b"avc1" + bytes(32)))))
            + extra + box(b"mdat", payload))


class Base(tc.Base):
    def setUp(self):
        super().setUp()
        self.owner_token = mock.patch.object(owners, "OWNER_TOKEN", OWNER_TOKEN)
        self.owner_token.start()

    def tearDown(self):
        self.owner_token.stop()
        super().tearDown()

    def records(self):
        return videos.load()["videos"]

    def only(self):
        (vid, v), = self.records().items()
        return vid, v

    def clip(self, xuid=ALICE, data=None, name="Forza14930605", **kw):
        """The game's movie upload: the clip in 256 KiB chunks, as the game sends it."""
        return self.upload(xuid, data if data is not None else make_asf(**kw), name=name,
                           overwrite=False, chunk=256 << 10)

    def converted(self, vid, ok=True, mp4=None, poster=None, frames=3, result=None, files=None):
        """What the converter writes for the clip: its result folder, in one rename."""
        part = videos.folder("converted", ".%s.part" % vid)
        os.makedirs(part)
        doc = result or ({"version": 1, "id": vid, "ok": True, "seconds": 15.27, "w": 640,
                          "h": 360, "frames": frames} if ok else
                         {"version": 1, "id": vid, "ok": False, "reason": "ffprobe failed (exit 1)"})
        made = {"result.json": json.dumps(doc).encode()}
        if ok:
            made["video.mp4"] = make_mp4() if mp4 is None else mp4
            made["poster.jpg"] = tc.make_jpeg(640, 360) if poster is None else poster
            for n in range(1, frames + 1):
                made["frame-%d.jpg" % n] = tc.make_jpeg(640, 360, comps=1 + 2 * (n % 2))
        made.update(files or {})
        for name, data in made.items():
            with open(os.path.join(part, name), "wb") as f:
                f.write(data)
        os.replace(part, videos.folder("converted", vid))

    def published(self, **kw):
        """A clip uploaded, converted and released: its id."""
        before = set(self.records())
        self.clip(**kw)
        vid, = set(self.records()) - before
        self.converted(vid)
        videos.collect()
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 200)
        return vid

    def feed(self):
        status, ctype, data, extra = self.http("GET", "/community/videos.json")
        self.assertEqual((status, ctype), (200, "application/json"))
        return json.loads(data)

    def vadmin(self, method, path, body=b"", **kw):
        headers = {"X-Admin-Token": tc.TOKEN}
        headers.update(kw.pop("headers", {}))
        status, _, data, _ = self.http(method, "/admin/videos" + path, headers, body, **kw)
        return status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


class HeaderTest(unittest.TestCase):
    def test_the_games_header_is_read(self):
        info = videos.read_asf(make_asf())
        self.assertEqual((info["w"], info["h"], info["video"], info["audio"], info["streams"]),
                         (640, 360, "WVC1", "0x0161", 2))
        self.assertAlmostEqual(info["seconds"], 15.0)
        self.assertEqual(info["title"], "Subaru WRX '05 on Beaumont - Stage 3")

    def test_clips_are_told_from_pictures(self):
        self.assertTrue(videos.looks_like_clip("Forza14930605", make_asf()[:16]))
        self.assertTrue(videos.looks_like_clip("replay.WMV", b"\xff\xd8\xff"))
        self.assertFalse(videos.looks_like_clip("ForzaBigshot.bmp", b"BM" + bytes(14)))
        self.assertFalse(videos.looks_like_clip("PHOTO1", tc.make_jpeg()[:16]))

    def test_what_is_refused(self):
        big = asf_object(guid("D2D0A440-E307-11D2-97F0-00A0C95EA850"), bytes(videos.MAX_HEADER))
        cases = {
            "not an ASF file": tc.make_jpeg(),
            "no video stream": make_asf(videos_n=0),
            "more than one video stream": make_asf(videos_n=2),
            "121.0 seconds long": make_asf(seconds=121),
            "video size 4096x2160": make_asf(w=4096, h=2160),
            "video size 32x32": make_asf(w=32, h=32),
            "a broadcast stream": make_asf(broadcast=True),
            "a damaged ASF header": make_asf(extra=[big]),
        }
        for why, data in cases.items():
            with self.assertRaises(videos.BadClip, msg=why) as cm:
                videos.read_asf(data)
            self.assertIn(why, str(cm.exception))
        damaged = bytearray(make_asf())
        struct.pack_into("<Q", damaged, 30 + 16, 10 ** 9)  # the first object's size
        with self.assertRaises(videos.BadClip):
            videos.read_asf(bytes(damaged))
        with self.assertRaises(videos.BadClip):
            videos.read_asf(make_asf()[:200])  # cut inside the header


class UploadTest(Base):
    def test_a_movie_upload_waits_for_the_converter_and_is_no_photo(self):
        before = time.time()
        with self.assertLogs("forza-live", "INFO") as logs:
            fid = self.clip()
        self.assertEqual(community.load()["photos"], {})
        self.assertFalse(any("JPEG" in line for line in logs.output))
        vid, v = self.only()
        self.assertRegex(vid, "^[A-Z2-7]{12}$")
        self.assertEqual((v["owner"], v["fileId"], v["status"], v["gamertag"], v["title"], v["w"],
                          v["h"]), (ALICE, fid, "converting", "Alice",
                                    "Subaru WRX '05 on Beaumont - Stage 3", 640, 360))
        self.assertGreaterEqual(v["created"], before)
        data = make_asf()
        with open(videos.folder("incoming", vid + ".wmv"), "rb") as f:
            self.assertEqual(f.read(), data)
        with open(videos.folder("incoming", vid + ".json")) as f:
            self.assertEqual(json.load(f), {"version": 1, "id": vid, "maxSeconds": videos.MAX_SECONDS,
                                            "maxWidth": videos.OUT_WIDTH, "frames": videos.FRAMES,
                                            "maxBytes": videos.MAX_MP4})
        self.assertEqual(v["clipSha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.download(ALICE, fid), data)  # the player's own file, as sent
        self.assertEqual(self.feed()["videos"], [])

    def test_pictures_still_become_photos(self):
        self.upload(ALICE, tc.make_jpeg(), name="Sunset.jpg")
        self.assertEqual(len(community.load()["photos"]), 1)
        self.assertEqual(self.records(), {})

    def test_a_wmv_that_is_not_asf_is_neither(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            self.clip(data=tc.make_jpeg(), name="replay.wmv")
        self.assertEqual((community.load()["photos"], self.records()), ({}, {}))
        self.assertTrue(any("not published: not an ASF file" in line for line in logs.output))

    def test_refused_clips_are_not_published_but_the_upload_succeeds(self):
        for kw in ({"seconds": 600}, {"w": 4000, "h": 2000}, {"videos_n": 0}):
            self.assertIsNotNone(self.clip(**kw), kw)  # the game's upload succeeds
        self.assertEqual(self.records(), {})

    def test_unsigned_banned_and_unknown_players(self):
        fid = self.upload(tc.LEGACY, make_asf(), name="Forza1", how="raw")
        self.assertIsNotNone(fid)
        self.assertEqual(self.records(), {})
        self.assertEqual(self.http("POST", "/admin/photos/ban/%016X" % BOB,
                                   {"X-Admin-Token": tc.TOKEN})[0], 200)
        self.clip(BOB)
        self.assertEqual(self.records(), {})
        with mock.patch.object(community, "credit", return_value=None):
            self.clip(ALICE)
        self.assertEqual(self.records(), {})

    def test_the_title_is_cleaned(self):
        self.clip(title="Café ‮racer\x07 " + "x" * 200)
        _, v = self.only()
        self.assertTrue(v["title"].startswith("Cafe racer"))
        self.assertLessEqual(len(v["title"]), videos.TITLE_MAX)

    def test_the_same_clip_again_moves_to_the_newer_file(self):
        self.clip()
        first = self.only()
        fid = self.clip()
        (vid, v), = self.records().items()
        self.assertEqual((vid, v["fileId"]), (first[0], fid))

    def test_caps(self):
        with mock.patch.object(videos, "PER_DAY", 2):
            for n in range(3):
                self.clip(title="clip %d" % n)
        self.assertEqual(len(self.records()), 2)
        videos.configure(self.dir.name)
        with mock.patch.object(videos, "MAX_WAITING", 3):
            for n in range(3, 6):
                self.clip(BOB, title="clip %d" % n)
        self.assertEqual(len(self.records()), 3)  # 2 of Alice's waiting, 1 of Bob's
        with mock.patch.object(videos, "PER_HOUR", 3):
            self.clip(tc.CAROL)
        self.assertEqual(len(self.records()), 3)

    def test_videos_count_in_the_disk_quota(self):
        self.assertIn("videos", limits.BLOB_DIRS)
        with mock.patch.object(limits, "DISK_CAP", 1):
            self.clip()
        self.assertEqual(self.records(), {})


class ConvertedTest(Base):
    def setUp(self):
        super().setUp()
        self.clip()
        self.vid = self.only()[0]

    def test_a_converted_clip_is_held_with_its_files_and_the_rest_is_cleared(self):
        self.converted(self.vid)
        self.assertTrue(videos.collect())
        v = self.records()[self.vid]
        self.assertEqual((v["status"], v["w"], v["h"], v["frames"], v["seconds"]),
                         ("held", 640, 360, 3, 15.27))
        with open(videos.media_path(self.vid, ".mp4"), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), v["sha256"])
        for n in (1, 2, 3):
            self.assertTrue(os.path.isfile(videos.frame_path(self.vid, n)))
        self.assertEqual(os.listdir(videos.folder("incoming")), [])
        self.assertEqual(os.listdir(videos.folder("converted")), [])
        self.assertEqual(self.feed()["videos"], [])  # held: not public until screened
        self.assertEqual(self.http("GET", "/community/video/%s.mp4" % self.vid)[0], 404)
        self.assertFalse(videos.collect())

    def test_new_videos_are_held_whatever_the_photos_setting(self):
        with mock.patch.object(community, "HOLD_NEW", False):
            self.converted(self.vid)
            videos.collect()
        self.assertEqual(self.records()[self.vid]["status"], "held")

    def test_the_poster_is_cleaned(self):
        self.converted(self.vid, poster=tc.make_jpeg(640, 360, extra=tc.METADATA, trailing=tc.SECRET))
        videos.collect()
        with open(videos.media_path(self.vid, ".jpg"), "rb") as f:
            self.assertNotIn(tc.SECRET, f.read())

    def check_dropped(self, **kw):
        self.converted(self.vid, **kw)
        with self.assertLogs("forza-live", "INFO") as logs:
            videos.collect()
        self.assertEqual(self.records(), {}, kw)
        self.assertEqual(os.listdir(videos.folder("converted")), [])
        self.assertEqual(os.listdir(videos.folder("incoming")), [])
        self.assertFalse(os.path.exists(videos.media_path(self.vid, ".mp4")))
        return " ".join(logs.output)

    def test_a_failed_conversion(self):
        self.assertIn("not published: ffprobe failed", self.check_dropped(ok=False))

    def test_a_result_for_another_clip(self):
        self.check_dropped(result={"version": 1, "id": "AAAAAAAAAAAA", "ok": True, "seconds": 3})

    def test_a_bad_mp4(self):
        self.check_dropped(mp4=b"\x00" * 100)

    def test_an_mp4_with_other_boxes(self):
        self.assertIn("unexpected box", self.check_dropped(mp4=make_mp4(extra=box(b"meta", b"x"))))

    def test_an_mp4_without_h264(self):
        self.check_dropped(mp4=box(b"ftyp", b"isom\x00\x00\x00\x00") + box(b"moov", b"mp4v") + box(b"mdat"))

    def test_a_bad_poster(self):
        self.check_dropped(poster=b"GIF89a" + bytes(100))

    def test_no_frames(self):
        self.check_dropped(frames=0)

    def test_a_bad_length(self):
        self.check_dropped(result={"version": 1, "id": self.vid, "ok": True, "seconds": 10 ** 6})

    def test_the_quota(self):
        with mock.patch.object(limits, "DISK_CAP", 1):
            self.check_dropped()

    def test_a_clip_the_converter_never_answered(self):
        videos.collect(now=time.time() + videos.CONVERT_WAIT - 60)
        self.assertIn(self.vid, self.records())
        with self.assertLogs("forza-live", "WARNING") as logs:
            videos.collect(now=time.time() + videos.CONVERT_WAIT + 60)
        self.assertEqual(self.records(), {})
        self.assertIn("the converter did not answer", logs.output[0])
        self.assertEqual(os.listdir(videos.folder("incoming")), [])

    def test_leftovers_of_gone_videos_and_the_converters_notes(self):
        os.makedirs(videos.folder("converted", "ZZZZZZZZZZZZ"))
        os.makedirs(videos.folder("converted", ".%s.part" % self.vid))
        with open(videos.folder("converted", ".%s.attempts" % self.vid), "w") as f:
            f.write("1")
        videos.collect()
        self.assertEqual(sorted(os.listdir(videos.folder("converted"))),
                         [".%s.attempts" % self.vid, ".%s.part" % self.vid])
        self.assertEqual(self.records()[self.vid]["status"], "converting")

    def test_the_servers_tick(self):
        self.converted(self.vid)
        videos.tick(now=time.time())
        self.assertEqual(self.records()[self.vid]["status"], "held")

    def test_videos_survive_a_restart(self):
        self.converted(self.vid)
        videos.collect()
        videos.configure(self.dir.name)
        self.assertEqual(self.records()[self.vid]["status"], "held")


class FeedTest(Base):
    def test_the_feed_and_its_media(self):
        vid = self.published()
        v = self.records()[vid]
        doc = self.feed()
        entry, = doc["videos"]
        self.assertEqual(sorted(entry), ["approvedAt", "gamertag", "h", "id", "owner", "poster",
                                         "posterSha256", "seconds", "sha256", "size", "takenAt",
                                         "title", "video", "w"])
        self.assertEqual((entry["id"], entry["gamertag"], entry["w"], entry["h"], entry["seconds"],
                          entry["video"], entry["poster"]),
                         (vid, "Alice", 640, 360, 15.27, "/community/video/%s.mp4" % vid,
                          "/community/video/%s.jpg" % vid))
        self.assertEqual(entry["owner"], leaderboards.player_id("%016X" % ALICE))
        text = json.dumps(doc)
        for xuid in (ALICE, BOB):
            self.assertNotIn("%016X" % xuid, text.upper())
            self.assertNotIn(str(xuid), text)
        status, ctype, data, extra = self.http("GET", entry["video"])
        self.assertEqual((status, ctype), (200, "video/mp4"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        self.assertEqual(len(data), entry["size"])
        self.assertEqual(extra["Accept-Ranges"], "bytes")
        self.assertEqual(extra["X-Content-Type-Options"], "nosniff")
        self.assertEqual(extra["Content-Security-Policy"], "default-src 'none'")
        status, ctype, poster, _ = self.http("GET", entry["poster"])
        self.assertEqual((status, ctype), (200, "image/jpeg"))
        self.assertEqual(hashlib.sha256(poster).hexdigest(), entry["posterSha256"])
        self.assertEqual(self.http("GET", entry["video"], {"If-None-Match": extra["ETag"]})[0], 304)
        self.assertEqual(self.http("GET", "/community/video/%s.png" % vid)[0], 404)
        self.assertEqual(self.http("POST", "/community/videos.json")[0], 405)
        self.assertEqual(v["status"], "live")

    def test_byte_ranges(self):
        vid = self.published()
        url = "/community/video/%s.mp4" % vid
        whole = self.http("GET", url)[2]
        n = len(whole)
        for header, (start, end) in (("bytes=0-1", (0, 1)), ("bytes=100-", (100, n - 1)),
                                     ("bytes=-10", (n - 10, n - 1)),
                                     ("bytes=5-999999", (5, n - 1))):
            status, _, data, extra = self.http("GET", url, {"Range": header})
            self.assertEqual(status, 206, header)
            self.assertEqual(data, whole[start:end + 1], header)
            self.assertEqual(extra["Content-Range"], "bytes %d-%d/%d" % (start, end, n))
        for header in ("bytes=%d-" % n, "bytes=9-3", "bytes=-0", "bytes=-"):
            status, _, _, extra = self.http("GET", url, {"Range": header})
            self.assertEqual((status, extra["Content-Range"]), (416, "bytes */%d" % n), header)
        for header in ("bytes=0-1,5-6", "items=0-1"):  # served whole
            self.assertEqual(self.http("GET", url, {"Range": header})[0], 200, header)
        etag = self.http("GET", url)[3]["ETag"]
        self.assertEqual(self.http("GET", url, {"Range": "bytes=0-1", "If-Range": etag})[0], 206)
        self.assertEqual(self.http("GET", url, {"Range": "bytes=0-1", "If-Range": '"old"'})[0], 200)

    def test_the_wire_names_206_and_416(self):
        self.assertTrue(srv.response(206, b"").startswith(b"HTTP/1.1 206 Partial Content\r\n"))
        self.assertTrue(srv.response(416, b"").startswith(b"HTTP/1.1 416 Range Not Satisfiable\r\n"))

    def test_media_requests_have_a_budget(self):
        vid = self.published()
        with mock.patch.object(videos, "MEDIA_PER_MINUTE", 2):
            codes = [self.http("GET", "/community/video/%s.jpg" % vid)[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_newest_first_capped_and_etags(self):
        first = self.published(title="one")
        second = self.published(title="two")
        self.assertEqual([e["id"] for e in self.feed()["videos"]], [second, first])
        with mock.patch.object(videos, "FEED_MAX", 1):
            videos._version[0] += 1
            self.assertEqual([e["id"] for e in self.feed()["videos"]], [second])
        status, _, _, extra = self.http("GET", "/community/videos.json")
        self.assertEqual(self.http("GET", "/community/videos.json",
                                   {"If-None-Match": extra["ETag"]})[0], 304)


class ModerationTest(Base):
    def test_routes_and_actions(self):
        self.clip()
        vid = self.only()[0]
        status, d = self.vadmin("GET", "?status=converting")
        self.assertEqual(([v["id"] for v in d["videos"]], d["videos"][0]["owner"]),
                         ([vid], "%016X" % ALICE))
        self.assertEqual(d["videos"][0]["frames"], [])
        self.assertEqual(self.vadmin("GET", "?status=bogus")[0], 400)
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 409)  # nothing to show yet
        self.assertEqual(self.vadmin("POST", "/%s/hide" % vid)[0], 409)
        self.converted(vid)
        status, d = self.vadmin("GET", "/queue")
        rec, = d["videos"]
        self.assertEqual(rec["frames"], ["/admin/videos/frame/%s-%d.jpg" % (vid, n) for n in (1, 2, 3)])
        for path in rec["frames"] + [rec["poster"], rec["video"]]:
            status, ctype, data, extra = self.http("GET", path, {"X-Admin-Token": tc.TOKEN})
            self.assertEqual(status, 200, path)
            self.assertEqual(extra["Cache-Control"], "no-store")
        self.assertEqual(self.vadmin("GET", "/frame/%s-4.jpg" % vid)[0], 404)
        status, d = self.vadmin("POST", "/%s/hide" % vid, json.dumps({"note": "screening: hide"}).encode())
        self.assertEqual((status, d["status"], d["note"]), (200, "hidden", "screening: hide"))
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 409)  # the worker cannot undo it
        self.assertEqual(self.vadmin("POST", "/%s/unhide" % vid)[0], 200)
        self.assertEqual([e["id"] for e in self.feed()["videos"]], [vid])
        self.assertEqual(self.vadmin("POST", "/%s/hold" % vid)[0], 200)
        self.assertEqual(self.feed()["videos"], [])
        self.assertEqual(self.vadmin("POST", "/%s/explode" % vid)[0], 404)
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid, b"not json")[0], 400)
        self.assertEqual(self.vadmin("POST", "/AAAAAAAAAAAA/release")[0], 404)

    def test_delete_and_the_same_clip_again(self):
        vid = self.published()
        self.assertEqual(self.vadmin("POST", "/%s/delete" % vid), (200, {"deleted": vid}))
        self.assertEqual(self.records(), {})
        self.assertFalse(os.path.exists(videos.media_path(vid, ".mp4")))
        self.clip()  # the same clip: not published again
        self.assertEqual(self.records(), {})

    def test_a_ban_hides_every_video(self):
        vid = self.published()
        self.assertEqual(self.vadmin("POST", "/%s/ban" % vid)[0], 200)
        self.assertEqual(self.feed()["videos"], [])
        self.assertEqual(self.http("GET", "/community/video/%s.mp4" % vid)[0], 404)
        self.assertEqual(self.http("POST", "/admin/photos/unban/%016X" % ALICE,
                                   {"X-Admin-Token": tc.TOKEN})[0], 200)
        self.assertEqual([e["id"] for e in self.feed()["videos"]], [vid])

    def test_the_operators_rules(self):
        self.assertEqual(self.vadmin("GET", "", headers={"X-Admin-Token": "wrong"})[0], 403)
        self.assertEqual(self.vadmin("GET", "", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.vadmin("GET", "", headers={"X-Forwarded-For": "203.0.113.9"})[0], 403)
        status, _, _, _ = self.http("GET", "/admin/videos?token=" + tc.TOKEN)
        self.assertEqual(status, 403)
        limits.configure_proxies(tc.LAN[0])  # from the tunnel agent's address
        self.assertEqual(self.vadmin("GET", "")[0], 403)


class OwnerAndRemovalTest(Base):
    def owner_delete(self, vid, xuid):
        status, _, data, _ = self.http("POST", "/owner/videos/%s/delete" % vid,
                                       {"X-Owner-Token": OWNER_TOKEN},
                                       json.dumps({"owner": "%016X" % xuid}).encode())
        return status, json.loads(data)

    def test_the_owner_deletes_their_video(self):
        vid = self.published()
        self.assertEqual(self.owner_delete(vid, BOB)[0], 404)  # not Bob's
        self.assertEqual(self.owner_delete(vid, ALICE), (200, {"deleted": vid}))
        self.assertEqual(self.records(), {})
        self.clip()  # theirs to upload again
        self.assertEqual(len(self.records()), 1)

    def test_overwrite_oldest_removes_the_video(self):
        with mock.patch.object(photos, "MAX_PER_PLAYER", 1):
            fid = self.clip()
            self.assertEqual(len(self.records()), 1)
            self.upload(ALICE, tc.make_jpeg(), name="PHOTO2", overwrite=True)
        self.assertEqual(self.records(), {})
        self.assertNotIn(str(fid), photos.load()["files"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg and ffprobe are not installed")
class ConverterTest(Base):
    """From the game's upload to the feed, with the real converter."""

    def test_end_to_end(self):
        clip = os.path.join(self.dir.name, "clip.wmv")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc2=s=640x360:r=20:d=3", "-f", "lavfi",
                        "-i", "sine=f=440:d=3:sample_rate=48000", "-c:v", "wmv2", "-b:v", "800k",
                        "-c:a", "wmav2", "-metadata", "title=Subaru WRX '05 on Beaumont",
                        "-metadata", "artist=SECRET-NAME", "-f", "asf", clip], check=True)
        with open(clip, "rb") as f:
            data = f.read()
        self.clip(data=data)
        vid, v = self.only()
        self.assertEqual(v["title"], "Subaru WRX '05 on Beaumont")
        worker = os.path.join(ROOT, "video", "video_worker.py")
        subprocess.run([sys.executable, "-I", worker, "--incoming", videos.folder("incoming"),
                        "--converted", videos.folder("converted"), "--once"], check=True,
                       capture_output=True)
        videos.collect()
        v = self.records()[vid]
        self.assertEqual((v["status"], v["w"], v["h"], v["frames"]), ("held", 640, 360, videos.FRAMES))
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 200)
        entry, = self.feed()["videos"]
        mp4 = self.http("GET", entry["video"])[2]
        self.assertEqual(mp4[4:8], b"ftyp")
        self.assertNotIn(b"SECRET", mp4)


if __name__ == "__main__":
    unittest.main()
