"""The video converter (video_worker.py): jobs, refusals, limits, and real conversions of clips
made here with ffmpeg (a test pattern and a tone: no game footage). The conversions are skipped
when ffmpeg and ffprobe are not installed.

  python3 -m unittest discover -s video -p 'test_*.py' -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import video_worker as vw  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
VID = "ABCDEFGHIJKL"


def make_clip(path, seconds=3, size="640x360", source="testsrc2", audio=True, vcodec="wmv2",
              fmt="asf", extra=()):
    """A clip as the game's are made (ASF, Windows Media video and audio), from a test pattern."""
    args = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "%s%ss=%s:r=20:d=%s" % (source, ":" if "=" in source else "=", size, seconds)]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=f=440:d=%s:sample_rate=48000" % seconds,
                 "-c:a", "wmav2"]
    args += ["-c:v", vcodec, "-b:v", "800k", *extra, "-f", fmt, "-y", path]
    subprocess.run(args, check=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.incoming = os.path.join(self.dir.name, "incoming")
        self.converted = os.path.join(self.dir.name, "converted")
        os.makedirs(self.incoming)
        os.makedirs(self.converted)

    def tearDown(self):
        self.dir.cleanup()

    def job(self, vid=VID, **fields):
        doc = {"version": 1, "id": vid, "maxSeconds": 120, "maxWidth": 1280, "frames": 4,
               "maxBytes": 32 << 20}
        doc.update(fields)
        with open(os.path.join(self.incoming, vid + ".json"), "w") as f:
            json.dump(doc, f)

    def clip_path(self, vid=VID):
        return os.path.join(self.incoming, vid + ".wmv")

    def result(self, vid=VID):
        with open(os.path.join(self.converted, vid, "result.json")) as f:
            return json.load(f)

    def files(self, vid=VID):
        return sorted(os.listdir(os.path.join(self.converted, vid)))

    def run_once(self):
        with mock.patch("sys.stdout"):
            vw.main(["--incoming", self.incoming, "--converted", self.converted, "--once"])


class JobTest(Base):
    def test_only_whole_jobs_with_no_result_are_taken(self):
        with open(self.clip_path(), "wb") as f:
            f.write(b"x")
        self.assertEqual(vw.pending(self.incoming, self.converted), [])  # no job yet
        self.job()
        self.job("BADID")  # not an id: never taken
        with open(os.path.join(self.incoming, "ZZZZZZZZZZZZ.json.tmp"), "w") as f:
            f.write("{}")
        self.assertEqual(vw.pending(self.incoming, self.converted), [VID])
        os.makedirs(os.path.join(self.converted, VID))
        self.assertEqual(vw.pending(self.incoming, self.converted), [])

    def test_the_jobs_limits_only_lower_the_workers(self):
        self.job(maxSeconds=10 ** 6, maxWidth=4096, frames=99, maxBytes=1 << 40)
        got = vw.job_limits(os.path.join(self.incoming, VID + ".json"))
        self.assertEqual((got["maxSeconds"], got["maxWidth"], got["frames"], got["maxBytes"]),
                         (vw.MAX_SECONDS, vw.OUT_WIDTH, vw.MAX_FRAMES, vw.MAX_BYTES))
        self.job(maxSeconds=5, maxWidth=320, frames=2, maxBytes="lots")
        got = vw.job_limits(os.path.join(self.incoming, VID + ".json"))
        self.assertEqual((got["maxSeconds"], got["maxWidth"], got["frames"], got["maxBytes"]),
                         (5, 320, 2, vw.MAX_BYTES))

    def test_bad_jobs_get_a_failed_result(self):
        for body in (b"not json", b'{"version": 2}', b"[1]", b"{" + b" " * 5000 + b"}"):
            with open(os.path.join(self.incoming, VID + ".json"), "wb") as f:
                f.write(body)
            shutil.rmtree(os.path.join(self.converted, VID), ignore_errors=True)
            self.run_once()
            self.assertFalse(self.result()["ok"], body[:20])
        shutil.rmtree(os.path.join(self.converted, VID))
        self.job(vid=VID, id="ZZZZZZZZZZZZ")  # names another clip
        self.run_once()
        self.assertEqual(self.result()["reason"], "the job names another clip")
        shutil.rmtree(os.path.join(self.converted, VID))
        self.job()  # no clip beside it
        self.run_once()
        self.assertEqual((self.result()["ok"], self.result()["reason"]), (False, "no clip"))
        self.assertEqual(self.files(), ["result.json"])

    def test_a_clip_that_stopped_the_worker_twice_is_refused(self):
        self.job()
        with open(self.clip_path(), "wb") as f:
            f.write(b"\x00" * 64)
        for _ in range(vw.ATTEMPTS):  # two starts that never finished (the worker was killed)
            vw.attempts(self.converted, VID)
        self.run_once()
        self.assertIn("stopped the converter", self.result()["reason"])
        self.assertEqual(sorted(os.listdir(self.converted)), [VID])  # the note is gone too

    def test_limits_of_each_run(self):
        with mock.patch.object(vw.resource, "setrlimit") as setrlimit, \
                mock.patch.object(vw.os, "setsid"):
            vw.limited(1234)()
        got = {c.args[0]: c.args[1] for c in setrlimit.call_args_list}
        self.assertEqual(got[vw.resource.RLIMIT_AS], (vw.MEMORY, vw.MEMORY))
        self.assertEqual(got[vw.resource.RLIMIT_FSIZE], (1234, 1234))
        self.assertEqual(got[vw.resource.RLIMIT_CORE], (0, 0))
        self.assertIn(vw.resource.RLIMIT_CPU, got)
        self.assertIn(vw.resource.RLIMIT_NOFILE, got)

    def test_ffmpeg_is_told_to_read_asf_from_a_file_with_windows_media_decoders_only(self):
        self.assertEqual(vw.INPUT[:2], ["-f", "asf"])
        self.assertEqual(vw.INPUT[2:4], ["-protocol_whitelist", "file"])
        self.assertEqual(set(vw.INPUT[5].split(",")), set(vw.VIDEO_CODECS + vw.AUDIO_CODECS))

    def test_a_missing_ffmpeg_fails_the_clip_not_the_worker(self):
        self.job()
        with open(self.clip_path(), "wb") as f:
            f.write(b"\x00" * 64)
        with mock.patch.object(vw, "FFPROBE", "/nonexistent/ffprobe"):
            self.run_once()
        self.assertIn("could not start", self.result()["reason"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class ConversionTest(Base):
    def test_a_clip_becomes_an_mp4_with_a_poster_and_frames_and_no_metadata(self):
        make_clip(self.clip_path(), extra=["-metadata", "title=SECRET-TITLE",
                                           "-metadata", "artist=SECRET-NAME"])
        self.job()
        self.run_once()
        r = self.result()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["w"], r["h"], r["frames"]), (640, 360, 4))
        self.assertAlmostEqual(r["seconds"], 3, delta=0.2)
        self.assertEqual(self.files(), ["frame-1.jpg", "frame-2.jpg", "frame-3.jpg", "frame-4.jpg",
                                        "poster.jpg", "result.json", "video.mp4"])
        out = os.path.join(self.converted, VID)
        with open(os.path.join(out, "video.mp4"), "rb") as f:
            mp4 = f.read()
        self.assertEqual(mp4[4:8], b"ftyp")
        self.assertLess(mp4.find(b"moov"), mp4.find(b"mdat"))  # faststart: plays while loading
        self.assertNotIn(b"SECRET", mp4)
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,pix_fmt,profile",
             "-of", "json", os.path.join(out, "video.mp4")], capture_output=True, check=True).stdout)
        codecs = sorted(s["codec_name"] for s in probe["streams"])
        self.assertEqual(codecs, ["aac", "h264"])
        h264 = [s for s in probe["streams"] if s["codec_name"] == "h264"][0]
        self.assertEqual((h264["pix_fmt"], h264["profile"]), ("yuv420p", "Main"))
        for name in ("poster.jpg", "frame-1.jpg"):
            with open(os.path.join(out, name), "rb") as f:
                self.assertEqual(f.read(3), b"\xff\xd8\xff")
        self.assertEqual(sorted(os.listdir(self.converted)), [VID])  # no .part, no attempts note

    def test_a_clip_with_no_sound_and_a_wide_one(self):
        make_clip(self.clip_path(), seconds=2, size="1920x1080", audio=False)
        self.job(maxWidth=640)
        self.run_once()
        r = self.result()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["w"], r["h"]), (640, 360))

    def test_a_blank_picture_is_refused(self):
        make_clip(self.clip_path(), seconds=2, source="color=c=black")
        self.job()
        self.run_once()
        self.assertEqual(self.result()["reason"],
                         "the clip's picture is blank (every frame is one flat colour)")
        self.assertEqual(self.files(), ["result.json"])

    def test_a_long_clip_is_cut_or_refused(self):
        make_clip(self.clip_path(), seconds=5, audio=False)
        self.job(maxSeconds=3)
        self.run_once()
        self.assertIn("seconds long", self.result()["reason"])  # 5 s is over 3 + 1
        shutil.rmtree(os.path.join(self.converted, VID))
        make_clip(self.clip_path(), seconds=3.6, audio=False)
        self.run_once()
        self.assertAlmostEqual(self.result()["seconds"], 3.0, delta=0.15)  # cut at the limit

    def test_only_asf_with_windows_media_codecs(self):
        make_clip(self.clip_path(), seconds=1, vcodec="mpeg4", fmt="avi", audio=False)
        self.job()
        self.run_once()
        self.assertFalse(self.result()["ok"])  # an AVI named .wmv: the ASF demuxer refuses it
        shutil.rmtree(os.path.join(self.converted, VID))
        make_clip(self.clip_path(), seconds=1, vcodec="mpeg4", audio=False)  # ASF, MPEG-4 video
        self.run_once()
        self.assertFalse(self.result()["ok"])
        with open(self.clip_path(), "wb") as f:  # not a clip at all
            f.write(b"#EXTM3U\nfile:/etc/passwd\n")
        shutil.rmtree(os.path.join(self.converted, VID))
        self.run_once()
        self.assertFalse(self.result()["ok"])

    def test_an_oversized_output_and_a_slow_run_are_refused(self):
        make_clip(self.clip_path(), seconds=2)
        self.job(maxBytes=1 << 20)
        with mock.patch.object(vw, "MAX_BYTES", 30000):
            got = vw.job_limits(os.path.join(self.incoming, VID + ".json"))
            self.assertEqual(got["maxBytes"], 1 << 20)  # never under 1 MiB from a job
        with mock.patch.object(vw, "convert", side_effect=vw.Refused(
                "the converted video is larger than 1 bytes")):
            self.run_once()
        self.assertIn("larger than", self.result()["reason"])
        shutil.rmtree(os.path.join(self.converted, VID))
        with mock.patch.object(vw, "TIMEOUT", 0.001):
            self.run_once()
        self.assertIn("took longer", self.result()["reason"])


if __name__ == "__main__":
    unittest.main()
