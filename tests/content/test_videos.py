"""Community videos (videos.py): the game's movie uploads told from pictures by their ASF header,
the header's checks, caps, the converter's results (checked, kept or dropped), the clips the
converter never answered, the public feed and its media with byte ranges, the operator's routes,
the owner's delete, bans and removal. Every clip and video here is made by this file (make_asf,
make_mp4): no game footage. ConverterTest also runs the real converter (video/video_worker.py)
on a clip ffmpeg makes from a test pattern; it is skipped when ffmpeg is not installed.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
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
from tests.support import ROOT, fill, fresh_count, no_walks_here

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

    def converted(self, vid, ok=True, mp4=None, frames=None, result=None, files=None, seconds=15.27,
                  poster_frame=2, frame=None, **fields):
        """What the converter writes for the clip: its result folder, in one rename. frames:
        how many it writes (frames_for(seconds) unless given); frame(n): frame n's bytes."""
        part = videos.folder("converted", ".%s.part" % vid)
        os.makedirs(part)
        want = videos.frames_for(seconds)
        frames = want if frames is None else frames
        doc = result or ({"version": 1, "id": vid, "ok": True, "seconds": seconds, "w": 640,
                          "h": 360, "frames": want, "posterFrame": poster_frame} if ok else
                         {"version": 1, "id": vid, "ok": False, "reason": "ffprobe failed (exit 1)"})
        doc.update(fields)
        made = {"result.json": json.dumps(doc).encode()}
        if ok:
            made["video.mp4"] = make_mp4() if mp4 is None else mp4
            for n in range(1, frames + 1):
                made["frame-%d.jpg" % n] = (frame(n) if frame else
                                            tc.make_jpeg(640, 360, comps=1 + 2 * (n % 2)))
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
            "0.50 seconds long": make_asf(seconds=0.5),
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
            job = json.load(f)
        self.assertRegex(job.pop("seed"), "^[0-9a-f]{32}$")  # where the screened frames are taken
        self.assertEqual(job, {"version": 1, "id": vid, "maxSeconds": videos.MAX_SECONDS,
                               "maxWidth": videos.OUT_WIDTH, "maxBytes": videos.MAX_MP4})
        self.assertEqual(v["clipSha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.feed()["videos"], [])

    def test_the_clip_is_never_kept_as_the_players_file(self):
        small = self.clip(title="small")
        self.assertNotIn(str(small), photos.load()["files"])
        self.assertEqual(self.download(ALICE, small), b"")  # nothing to give back
        self.assertFalse(os.path.exists(photos.file_path(small)))
        refused = self.clip(seconds=600)  # refused clips too
        self.assertNotIn(str(refused), photos.load()["files"])
        self.assertEqual(len(self.records()), 1)
        photo = self.upload(ALICE, tc.make_jpeg(), name="Sunset.jpg")  # a picture is kept as before
        self.assertIn(str(photo), photos.load()["files"])

    def test_the_seed_is_never_published(self):
        self.clip()
        vid, _ = self.only()
        with open(videos.folder("incoming", vid + ".json")) as f:
            seed = json.load(f)["seed"]
        self.converted(vid)
        status, d = self.vadmin("GET", "")
        self.assertNotIn(seed, json.dumps(d))
        self.assertNotIn(seed, json.dumps(videos.load()))
        self.vadmin("POST", "/%s/release" % vid)
        self.assertNotIn(seed, json.dumps(self.feed()))

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

    def test_videos_have_their_own_budget(self):
        self.assertNotIn("videos", limits.BLOB_DIRS)  # they never take the other uploads' room
        with mock.patch.object(videos, "VIDEOS_CAP", 1000):
            with self.assertLogs("forza-live", "INFO") as logs:
                self.clip()
        self.assertEqual(self.records(), {})
        self.assertIn("the videos' disk budget", " ".join(logs.output))
        self.published()
        limits.count()
        before = limits.usage()
        self.published(title="another")  # videos do not grow the shared quota
        self.assertEqual(limits.usage(), before)
        limits.count()
        self.assertEqual(limits.usage(), before)

    @unittest.skipIf(os.name == "nt", "the converter runs on Linux only")
    def test_the_converter_and_the_screening_count_frames_as_the_server_does(self):
        import importlib.util
        mods = []
        for rel, name in (("video/video_worker.py", "video_worker"), ("screening/screen_client.py", "screen_client")):
            spec = importlib.util.spec_from_file_location("check_" + name, os.path.join(ROOT, rel))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
        for hundredths in range(100, 13000, 7):
            s = hundredths / 100
            self.assertEqual({m.frames_for(s) for m in mods} | {videos.frames_for(s)}, {videos.frames_for(s)}, s)
        self.assertEqual((mods[0].MAX_FRAMES, mods[1].MAX_FRAMES), (videos.MAX_FRAMES, videos.MAX_FRAMES))

    def test_frames_for(self):
        self.assertEqual([videos.frames_for(s) for s in (1, 15.27, 16, 16.01, 30, 119.99, 120, 121)],
                         [8, 8, 8, 9, 15, 60, 60, 60])


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
                         ("held", 640, 360, 8, 15.27))
        with open(videos.media_path(self.vid, ".mp4"), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), v["sha256"])
        for n in range(1, 9):
            self.assertTrue(os.path.isfile(videos.frame_path(self.vid, n)))
        with open(videos.media_path(self.vid, ".jpg"), "rb") as f, \
                open(videos.frame_path(self.vid, 2), "rb") as g:
            self.assertEqual(f.read(), g.read())  # the poster is the screened frame 2
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

    def test_the_poster_and_frames_are_cleaned_and_a_poster_of_its_own_is_ignored(self):
        dirty = tc.make_jpeg(640, 360, extra=tc.METADATA, trailing=tc.SECRET)
        self.converted(self.vid, frame=lambda n: dirty,
                       files={"poster.jpg": tc.make_jpeg(800, 450)})  # never screened: not used
        videos.collect()
        v = self.records()[self.vid]
        with open(videos.media_path(self.vid, ".jpg"), "rb") as f:
            poster = f.read()
        self.assertNotIn(tc.SECRET, poster)
        self.assertEqual(community.clean_jpeg(poster, videos.MIN_VIDEO)[1:], (640, 360))
        self.assertEqual((v["w"], v["h"]), (640, 360))

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

    def test_a_bad_frame(self):
        self.check_dropped(frame=lambda n: b"GIF89a" + bytes(100) if n == 3 else tc.make_jpeg(640, 360))

    def test_frames_that_are_not_what_was_asked(self):
        cases = [{"frames": 0}, {"frames": 7}, {"frames": 9}, {"frames": 8, "seconds": 30},
                 {"posterFrame": 0}, {"posterFrame": 9}, {"posterFrame": True}, {"posterFrame": "2"},
                 {"frame": lambda n: tc.make_jpeg(640 if n < 5 else 1280, 360 if n < 5 else 720)}]
        self.enterContext(mock.patch.multiple(videos, PER_DAY=100, PER_HOUR=100, MAX_WAITING=100))
        for i, kw in enumerate(cases):
            with self.subTest(case=i):
                before = set(self.records())
                self.clip(title="clip %d" % i)
                vid, = set(self.records()) - before
                frames = kw.pop("frames", None)
                seconds = kw.pop("seconds", 15.27)
                self.converted(vid, frames=frames, seconds=seconds, **kw)
                with self.assertLogs("forza-live", "INFO") as logs:
                    videos.collect()
                self.assertNotIn(vid, self.records())
                self.assertIn("not published", " ".join(logs.output))
                self.assertEqual(os.listdir(videos.folder("converted")), [])

    def test_a_link_in_the_result_is_not_followed(self):
        secret = os.path.join(self.dir.name, "secret.mp4")
        with open(secret, "wb") as f:
            f.write(make_mp4())  # a file the converter should not be able to hand over
        self.converted(self.vid)
        out = videos.folder("converted", self.vid)
        os.remove(os.path.join(out, "video.mp4"))
        try:
            os.symlink(secret, os.path.join(out, "video.mp4"))
        except (OSError, NotImplementedError):
            self.skipTest("no symbolic links here")
        with self.assertLogs("forza-live", "INFO"):
            videos.collect()
        self.assertEqual(self.records(), {})
        self.assertTrue(os.path.isfile(secret))

    def test_a_result_folder_that_is_a_link(self):
        elsewhere = os.path.join(self.dir.name, "elsewhere")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "keep"), "w") as f:
            f.write("x")
        os.makedirs(videos.folder("converted"), exist_ok=True)
        try:
            os.symlink(elsewhere, videos.folder("converted", self.vid))
        except (OSError, NotImplementedError):
            self.skipTest("no symbolic links here")
        with self.assertLogs("forza-live", "INFO"):
            videos.collect()
        self.assertEqual(self.records(), {})
        self.assertEqual(os.listdir(videos.folder("converted")), [])
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "keep")))  # the link went, not its target

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs here")
    def test_a_fifo_in_the_result_does_not_block_the_server(self):
        self.converted(self.vid)
        out = videos.folder("converted", self.vid)
        os.remove(os.path.join(out, "frame-1.jpg"))
        os.mkfifo(os.path.join(out, "frame-1.jpg"))
        done = []
        t = threading.Thread(target=lambda: done.append(videos.collect()), daemon=True)
        with self.assertLogs("forza-live", "INFO") as logs:
            t.start()
            t.join(5)
        self.assertEqual(done, [True], "collect() blocked on a FIFO")
        self.assertIn("not a plain file", " ".join(logs.output))
        self.assertEqual(self.records(), {})

    def test_too_many_files_or_too_large_ones(self):
        self.converted(self.vid, files={"junk-%d" % i: b"x" for i in range(videos.MAX_RESULT_ENTRIES)})
        with self.assertLogs("forza-live", "INFO") as logs:
            videos.collect()
        self.assertIn("files in the converter's result", " ".join(logs.output))
        self.assertEqual(self.records(), {})

    def test_a_result_that_breaks_the_server_is_looked_at_once(self):
        self.converted(self.vid)
        with mock.patch.object(videos, "_finish", side_effect=RuntimeError("boom")) as f:
            with self.assertLogs("forza-live", "ERROR") as logs:
                self.assertTrue(videos.collect())
            videos.collect()
        self.assertEqual(f.call_count, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.records(), {})
        self.assertEqual(os.listdir(videos.folder("converted")), [])
        videos.configure(self.dir.name)
        self.assertEqual(self.records(), {})  # saved

    def test_a_bad_length(self):
        self.check_dropped(result={"version": 1, "id": self.vid, "ok": True, "seconds": 10 ** 6})

    def test_the_videos_budget(self):
        with mock.patch.object(videos, "VIDEOS_CAP", 3000):
            self.assertIn("budget", self.check_dropped())

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


class BudgetTest(Base):
    """The videos' own budget is kept in memory as the disk quota is (limits.Stored, #64)."""

    def old_videos(self):
        """Several thousand small files where the videos and their frames are kept: their bytes."""
        return (fill(videos.folder("public"), 1500) + fill(videos.folder("frames"), 3000, size=9))

    def test_the_budget_follows_the_files(self):
        old = self.old_videos()
        self.assertEqual(videos.STORED.count(), (4500, old, None))
        self.assertEqual(videos.usage(), old)
        vid = self.published()
        v = self.records()[vid]
        files = [videos.media_path(vid, ".mp4"), videos.media_path(vid, ".jpg")] + [
            videos.frame_path(vid, n) for n in range(1, v["frames"] + 1)]
        # the video, its poster and frames; the clip and its job went when it was converted
        self.assertEqual(videos.usage(), old + sum(map(os.path.getsize, files)))
        self.assertEqual(videos.usage(), fresh_count(videos.STORED))
        videos.delete(vid)
        self.assertEqual(videos.usage(), old)
        videos.configure(self.dir.name)  # a restart: nothing known until the count
        self.assertEqual(videos.usage(), 0)
        videos.STORED.count()
        self.assertEqual(videos.usage(), old)

    def test_the_budget_refuses_when_full_and_a_delete_gives_the_room_back(self):
        vid = self.published()
        self.old_videos()
        videos.STORED.count()
        clip = make_asf(title="one more")
        with mock.patch.object(videos, "VIDEOS_CAP", videos.usage() + len(clip) - 1):
            with self.assertLogs("forza-live", "INFO") as logs:
                self.clip(BOB, data=clip)
            self.assertIn("the videos' disk budget", " ".join(logs.output))
            self.assertEqual(list(self.records()), [vid])
            used = videos.usage()
            videos.delete(vid)
            self.assertLess(videos.usage(), used)
            self.clip(BOB, data=clip)  # the room the deleted video gave back
            self.assertEqual([v["status"] for v in self.records().values()], ["converting"])

    def test_before_the_first_count_clips_are_bounded_by_the_allowance(self):
        self.old_videos()  # how much is there is not known yet
        clip = make_asf()
        with mock.patch.object(limits, "START_ALLOWANCE", 2 * len(clip) + 500):
            self.clip(data=clip)
            self.clip(BOB, data=make_asf(title="a second"))
            with self.assertLogs("forza-live", "INFO") as logs:
                self.clip(tc.CAROL, data=make_asf(title="a third"))
            self.assertIn("still being counted", " ".join(logs.output))
            self.assertEqual(len(self.records()), 2)
            videos.STORED.count()
            self.clip(tc.CAROL, data=make_asf(title="a third"))  # the budget alone from here on
            self.assertEqual(len(self.records()), 3)

    def test_a_converted_clip_is_not_dropped_for_the_start_s_allowance(self):
        clip = make_asf()
        with mock.patch.object(limits, "START_ALLOWANCE", len(clip) + 500):
            self.clip(data=clip)
            vid, _ = self.only()
            self.converted(vid, mp4=make_mp4(payload=bytes(3 * len(clip))))  # larger than the allowance
            videos.collect()
            self.assertEqual(self.records()[vid]["status"], "held")
        small = make_asf(title="another", data=10)
        # the budget itself still holds: room for the clip and its job, not for its video
        with mock.patch.object(videos, "VIDEOS_CAP", videos.usage() + len(small) + 600):
            self.clip(BOB, data=small)
            (other,) = set(self.records()) - {vid}
            self.converted(other)
            with self.assertLogs("forza-live", "INFO") as logs:
                videos.collect()
            self.assertIn("the videos' disk budget", " ".join(logs.output))
            self.assertEqual(list(self.records()), [vid])

    def test_a_count_corrects_a_wrong_total_and_sees_the_converter_s_files(self):
        self.clip()
        vid, _ = self.only()
        videos.STORED.count()
        right = videos.usage()
        videos.STORED.add(5000)  # counted, never written
        self.converted(vid)      # the converter's result: not written by the server
        result = sum(os.path.getsize(os.path.join(videos.folder("converted", vid), name))
                     for name in os.listdir(videos.folder("converted", vid)))
        self.assertEqual(videos.usage(), right + 5000)
        files, found, drift = videos.STORED.count()
        self.assertEqual((found, drift), (right + result, 5000 - result))
        self.assertEqual(videos.usage(), right + result)


class BudgetLoopTest(Base, unittest.IsolatedAsyncioTestCase):
    async def counted(self):
        for _ in range(4000):
            if videos.STORED.counted:
                return
            await asyncio.sleep(0.005)
        self.fail("the videos were not counted")

    async def test_a_clip_walks_no_folder_on_the_event_loop(self):
        old = fill(videos.folder("public"), 1500) + fill(videos.folder("frames"), 3000, size=9)
        first, second = make_asf(), make_asf(title="another")
        with no_walks_here() as walks:
            task = asyncio.create_task(srv.keep_counted((("videos", videos.STORED),), every=3600))
            self.clip(data=first)  # before the first count
            await self.counted()
            self.clip(BOB, data=second)
            with mock.patch.object(videos, "VIDEOS_CAP", videos.usage() + 100):
                with self.assertLogs("forza-live", "INFO") as logs:
                    self.clip(tc.CAROL, title="a third")  # full: still refused
            task.cancel()
        self.assertEqual(walks.here, [])
        self.assertTrue(walks.elsewhere)  # the count did walk, in its thread
        self.assertIn("the videos' disk budget", " ".join(logs.output))
        self.assertEqual(len(self.records()), 2)
        jobs = sum(os.path.getsize(videos.folder("incoming", vid + ".json")) for vid in self.records())
        self.assertEqual(videos.usage(), old + len(first) + len(second) + jobs)

    async def test_the_recount_corrects_a_wrong_total(self):
        old = fill(videos.folder("public"), 300)
        task = asyncio.create_task(srv.keep_counted((("videos", videos.STORED),), every=0.02))
        await self.counted()
        self.assertEqual(videos.usage(), old)
        videos.STORED.add(7000)  # counted, never written
        for _ in range(4000):
            if videos.usage() == old:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        self.assertEqual(videos.usage(), old)


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
        for header in ("bytes=0-1,5-6", "items=0-1", "bytes=" + "9" * 5000 + "-",
                       "bytes=0-" + "9" * 5000):  # served whole
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
        self.assertEqual(rec["frames"], ["/admin/videos/frame/%s-%d.jpg" % (vid, n) for n in range(1, 9)])
        for path in rec["frames"] + [rec["poster"], rec["video"]]:
            status, ctype, data, extra = self.http("GET", path, {"X-Admin-Token": tc.TOKEN})
            self.assertEqual(status, 200, path)
            self.assertEqual(extra["Cache-Control"], "no-store")
        self.assertEqual(self.vadmin("GET", "/frame/%s-9.jpg" % vid)[0], 404)
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

    def test_a_video_takes_no_slot_of_the_players_files(self):
        with mock.patch.object(photos, "MAX_PER_PLAYER", 1):
            self.clip()
            self.upload(ALICE, tc.make_jpeg(), name="PHOTO2", overwrite=False)  # room: the clip is not kept
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(community.load()["photos"]), 1)


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
        self.assertEqual((v["status"], v["w"], v["h"], v["frames"]), ("held", 640, 360, videos.frames_for(3)))
        self.assertEqual(self.vadmin("POST", "/%s/release" % vid)[0], 200)
        entry, = self.feed()["videos"]
        mp4 = self.http("GET", entry["video"])[2]
        self.assertEqual(mp4[4:8], b"ftyp")
        self.assertNotIn(b"SECRET", mp4)


if __name__ == "__main__":
    unittest.main()
