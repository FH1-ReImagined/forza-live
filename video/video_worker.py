#!/usr/bin/env python3
"""Forza Live community videos: the video converter (README.md in this folder).

The game server (forza_live/content/videos.py) copies each movie a player uploads to
INCOMING/<id>.wmv and writes the job INCOMING/<id>.json last. This worker turns the clip into
what a browser plays and the screening worker can look at, with ffmpeg, and writes the result to
CONVERTED/<id>/ in one rename (it builds CONVERTED/.<id>.part/ first):

  result.json    {"version": 1, "id", "ok": true, "seconds", "w", "h", "frames"}, or
                 {"version": 1, "id", "ok": false, "reason"}
  video.mp4      H.264 (Main, yuv420p, 30 fps) and AAC (stereo, 48 kHz), at most OUT_WIDTH wide,
                 at most MAX_SECONDS long, at most MAX_BYTES, "faststart", no metadata
  poster.jpg     the frame with the most detail, from the converted video
  frame-<n>.jpg  FRAMES frames spread over the video, for screening

Every clip is hostile input. The worker:
- runs in a container with no network, a read-only root, no capabilities and memory, CPU and
  process limits (compose.yaml, service video-converter), and reads the clips read-only;
- forces ffmpeg's ASF demuxer (-f asf: no format probing, so no playlist, concat or image
  sequence tricks), allows the file protocol only and the Windows Media decoders only
  (VIDEO_CODECS, AUDIO_CODECS), and reads the converted MP4 back only to take pictures from it;
- gives each ffmpeg run its own limits (address space, CPU seconds, file size, open files, no
  core dumps) and a wall clock timeout, and a clean environment;
- refuses a clip whose picture is blank (every sampled frame one flat colour: the game recorded
  no picture), longer than MAX_SECONDS + 1 (its header said less), or larger than MAX_INPUT;
- gives up on a clip that stopped it twice (killed for its memory, say): the third start writes a
  failed result, so no clip can stop the queue.

The job's own limits (maxSeconds, maxWidth, frames, maxBytes) can only lower the worker's.

  video_worker.py --incoming DIR --converted DIR [--once] [--poll SECONDS]
"""

import argparse
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

ID_RE = re.compile(r"[A-Z2-7]{12}")
MAX_INPUT = 32 << 20                # bytes of a clip (the game server's own cap)
MAX_SECONDS = int(os.environ.get("FORZA_VIDEO_MAX_SECONDS", "120"))
OUT_WIDTH = int(os.environ.get("FORZA_VIDEO_MAX_WIDTH", "1280"))
MAX_BYTES = 32 << 20                # bytes of a converted video
FRAMES = 8                          # frames for screening (the game server takes 1 to 16)
MAX_FRAMES = 16
VIDEO_CODECS = ("vc1", "wmv3", "wmv2", "wmv1")      # what the game's movies use, and older WMV
AUDIO_CODECS = ("wmav2", "wmav1", "wmapro")
DECODERS = ",".join(VIDEO_CODECS + AUDIO_CODECS)
MAX_DIM = (1920, 1088)
MIN_DIM = (96, 64)
BLANK_RANGE = 12                    # a frame whose brightness spans less than this is flat
SAMPLE_FPS = 2                      # for the blank check and the poster's choice
TIMEOUT = int(os.environ.get("FORZA_VIDEO_TIMEOUT", "300"))  # seconds of one ffmpeg run
MEMORY = int(os.environ.get("FORZA_VIDEO_MEMORY_MB", "1024")) << 20  # address space of one run
ATTEMPTS = 2
FFMPEG = os.environ.get("FORZA_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FORZA_FFPROBE", "ffprobe")


class Refused(Exception):
    """The clip is not converted: the reason goes into its result."""


def log(event, **fields):
    print(json.dumps(dict(event=event, **fields), separators=(",", ":"), default=str), flush=True)


def limited(file_size):
    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))
        cpu = max(1, int(TIMEOUT))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def run(args, file_size=MAX_BYTES + (1 << 20), timeout=None):
    """ffmpeg or ffprobe with its limits: (stdout bytes, stderr text). Refused when it fails,
    is killed or runs out of time."""
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout or TIMEOUT, preexec_fn=limited(file_size),
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        raise Refused("the conversion took longer than %d seconds" % (timeout or TIMEOUT)) from None
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused("the converter could not start %s (%s)" % (os.path.basename(args[0]), e)) from None
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        why = "killed by signal %d" % -p.returncode if p.returncode < 0 else "exit %d" % p.returncode
        last = [line for line in err.strip().splitlines() if line.strip()][-1:]
        raise Refused("%s failed (%s)%s" % (os.path.basename(args[0]), why,
                                            ": " + last[0][:160] if last else ""))
    return p.stdout, err


INPUT = ["-f", "asf", "-protocol_whitelist", "file", "-codec_whitelist", DECODERS]
READ_BACK = ["-f", "mp4", "-protocol_whitelist", "file", "-codec_whitelist", "h264,aac"]


def probe(path, input_args):
    out, _ = run([FFPROBE, "-v", "error", *input_args, "-show_entries",
                  "format=duration:stream=index,codec_type,codec_name,width,height",
                  "-of", "json", "file:" + path], file_size=1 << 20, timeout=60)
    try:
        doc = json.loads(out)
    except ValueError:
        raise Refused("the clip could not be read") from None
    streams = doc.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    try:
        seconds = float((doc.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        raise Refused("the clip has no length") from None
    return video, audio, seconds


def check_clip(path, max_seconds):
    """(seconds to convert, has audio) of a clip; Refused for one this worker does not take."""
    if os.path.getsize(path) > MAX_INPUT:
        raise Refused("the clip is larger than %d bytes" % MAX_INPUT)
    video, audio, seconds = probe(path, INPUT)
    if len(video) != 1:
        raise Refused("%d video streams" % len(video))
    v = video[0]
    if v.get("codec_name") not in VIDEO_CODECS:
        raise Refused("video codec %s" % str(v.get("codec_name"))[:20])
    w, h = v.get("width") or 0, v.get("height") or 0
    if not (MIN_DIM[0] <= w <= MAX_DIM[0] and MIN_DIM[1] <= h <= MAX_DIM[1]):
        raise Refused("video size %dx%d" % (w, h))
    if audio and audio[0].get("codec_name") not in AUDIO_CODECS:
        raise Refused("audio codec %s" % str(audio[0].get("codec_name"))[:20])
    if not 0 < seconds <= max_seconds + 1:
        raise Refused("%.1f seconds long (at most %d)" % (seconds, max_seconds))
    return min(seconds, max_seconds), bool(audio)


def convert(src, out, seconds, audio, width, max_bytes):
    scale = "scale=w='trunc(min(%d,iw)/2)*2':h=-2:flags=bicubic,format=yuv420p" % width
    args = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-threads", "1",
            *INPUT, "-i", "file:" + src, "-map", "0:v:0"]
    if audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
    args += ["-t", "%.3f" % seconds, "-vf", scale, "-fps_mode", "cfr", "-r", "30",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "2500k",
             "-bufsize", "5000k", "-profile:v", "main", "-g", "60", "-threads", "1",
             "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
             "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-movflags", "+faststart",
             "-fs", str(max_bytes), "-f", "mp4", "file:" + out]
    run(args, file_size=max_bytes + (1 << 20))
    size = os.path.getsize(out)
    if size >= max_bytes:
        raise Refused("the converted video is larger than %d bytes" % max_bytes)
    video, _, length = probe(out, READ_BACK)
    if len(video) != 1 or video[0].get("codec_name") != "h264":
        raise Refused("the converted video could not be read back")
    return length, video[0]["width"], video[0]["height"]


def brightness(src, seconds):
    """[(lowest, highest, mean spread)] of the converted video's frames, SAMPLE_FPS a second,
    each as a 32x18 greyscale picture."""
    n = max(1, min(int(seconds * SAMPLE_FPS) + 1, 600))
    raw, _ = run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-threads", "1", *READ_BACK,
                  "-i", "file:" + src, "-vf", "fps=%d,scale=32:18:flags=area,format=gray" % SAMPLE_FPS,
                  "-frames:v", str(n), "-f", "rawvideo", "pipe:1"], file_size=1 << 20)
    frames = [raw[i:i + 576] for i in range(0, len(raw) - 575, 576)]
    out = []
    for f in frames:
        mean = sum(f) / len(f)
        out.append((min(f), max(f), sum(abs(b - mean) for b in f) / len(f)))
    return out


def picture(src, at, dest):
    run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-threads", "1", "-ss", "%.3f" % at,
         *READ_BACK, "-i", "file:" + src, "-frames:v", "1", "-q:v", "3", "-map_metadata", "-1",
         "-fflags", "+bitexact", "-f", "image2", "-c:v", "mjpeg", "file:" + dest], file_size=4 << 20)
    if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        raise Refused("no picture at %.1f s" % at)


def process(src, part, limits):
    """Converts one clip into the folder part: the result's fields. Refused on any refusal."""
    seconds, audio = check_clip(src, limits["maxSeconds"])
    video = os.path.join(part, "video.mp4")
    length, w, h = convert(src, video, seconds, audio, limits["maxWidth"], limits["maxBytes"])
    stats = brightness(video, length)
    if not stats:
        raise Refused("the converted video has no frames")
    if all(hi - lo < BLANK_RANGE for lo, hi, _ in stats):
        raise Refused("the clip's picture is blank (every frame is one flat colour)")
    best = max(range(len(stats)), key=lambda i: (stats[i][2], -i))
    picture(video, min(best / SAMPLE_FPS, max(0.0, length - 0.1)), os.path.join(part, "poster.jpg"))
    n = limits["frames"]
    for k in range(n):
        picture(video, min(length * (k + 0.5) / n, max(0.0, length - 0.1)),
                os.path.join(part, "frame-%d.jpg" % (k + 1)))
    return {"seconds": round(length, 3), "w": w, "h": h, "frames": n}


def job_limits(job_path):
    """The job's limits, never above the worker's own."""
    with open(job_path, "rb") as f:
        raw = f.read(4097)
    if len(raw) > 4096:
        raise Refused("the job is too large")
    try:
        job = json.loads(raw)
    except ValueError:
        raise Refused("the job is not JSON") from None
    if not isinstance(job, dict) or job.get("version") != 1:
        raise Refused("not a version 1 job")

    def number(key, default, low, high):
        v = job.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool):
            v = default
        return max(low, min(v, high))

    return {"id": job.get("id"), "maxSeconds": number("maxSeconds", MAX_SECONDS, 1, MAX_SECONDS),
            "maxWidth": number("maxWidth", OUT_WIDTH, MIN_DIM[0], OUT_WIDTH),
            "frames": number("frames", FRAMES, 1, MAX_FRAMES),
            "maxBytes": number("maxBytes", MAX_BYTES, 1 << 20, MAX_BYTES)}


def attempts(converted, vid):
    """How many times this clip was started before (a note kept beside the results)."""
    path = os.path.join(converted, ".%s.attempts" % vid)
    try:
        with open(path) as f:
            n = int(f.read(16) or 0)
    except (OSError, ValueError):
        n = 0
    with open(path, "w") as f:
        f.write(str(n + 1))
    return n


def handle(incoming, converted, vid):
    """One job: its result folder written (ok or not)."""
    part = os.path.join(converted, ".%s.part" % vid)
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(part)
    t0 = time.monotonic()
    result = {"version": 1, "id": vid}
    try:
        if attempts(converted, vid) >= ATTEMPTS:
            raise Refused("the clip stopped the converter %d times" % ATTEMPTS)
        limits = job_limits(os.path.join(incoming, vid + ".json"))
        if limits["id"] != vid:
            raise Refused("the job names another clip")
        src = os.path.join(incoming, vid + ".wmv")
        if not os.path.isfile(src) or os.path.islink(src):
            raise Refused("no clip")
        result.update(ok=True, **process(src, part, limits))
    except Refused as e:
        for name in os.listdir(part):  # a refused clip leaves nothing but its result
            os.remove(os.path.join(part, name))
        result.update(ok=False, reason=str(e)[:200])
    with open(os.path.join(part, "result.json"), "w") as f:
        json.dump(result, f)
    os.replace(part, os.path.join(converted, vid))
    try:
        os.remove(os.path.join(converted, ".%s.attempts" % vid))
    except OSError:
        pass
    log("clip", id=vid, ok=result["ok"], reason=result.get("reason"),
        seconds=result.get("seconds"), size="%sx%s" % (result.get("w"), result.get("h")) if result["ok"] else None,
        ms=round((time.monotonic() - t0) * 1000))
    return result


def pending(incoming, converted):
    """The jobs waiting, oldest first: a whole job (its .json) with no result yet."""
    try:
        names = os.listdir(incoming)
    except OSError:
        return []
    jobs = []
    for name in names:
        vid = name[:-5]
        if name.endswith(".json") and ID_RE.fullmatch(vid) and not os.path.exists(
                os.path.join(converted, vid)):
            try:
                jobs.append((os.path.getmtime(os.path.join(incoming, name)), vid))
            except OSError:
                pass
    return [vid for _, vid in sorted(jobs)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--incoming", required=True, help="the game server's clips and jobs (read-only)")
    ap.add_argument("--converted", required=True, help="where the results go")
    ap.add_argument("--once", action="store_true", help="convert what is waiting, then exit")
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    os.makedirs(a.converted, exist_ok=True)
    log("start", ffmpeg=FFMPEG, max_seconds=MAX_SECONDS, max_width=OUT_WIDTH, timeout=TIMEOUT,
        memory_mb=MEMORY >> 20)
    while not stop:
        for vid in pending(a.incoming, a.converted):
            if stop:
                break
            handle(a.incoming, a.converted, vid)
        if a.once:
            break
        time.sleep(a.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
