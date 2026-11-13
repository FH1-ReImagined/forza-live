"""Forza Live: community videos, the clips players upload from the game, published for the
website's Videos tab (FH1-ReImagined/website tools/build_videos.py reads the feed).

The game's movie upload goes through the same large-file upload as photo mode's Big Shot
(photos.py): LargeFileUpload.Create(payloadSize, overwriteOldest, name), with a name such as
"Forza14930605" (no extension), then the clip in 256 KiB Content.UploadContent chunks. The clip
is an ASF (WMV) file: VC-1 video and WMA 2 audio (640x360 and 48 kHz stereo from TU4, November
2026), titled by the game after the car and the route ("Subaru WRX '05 on Beaumont - Stage 3")
and tagged with the driver's name. When an upload finishes, photos hands it to upload_finished:
a file that starts with the ASF Header Object (or is named .wmv or .asf) comes here, anything
else goes on to community.photo_uploaded as before. A clip is never read as a picture.

A clip becomes a community video if:
- the upload was signed with the player's own key, the player is not banned and has a gamertag
  on record (community.photo_uploaded's rules);
- its ASF header reads (read_asf): one video stream of MIN_VIDEO to MAX_VIDEO pixels, at most
  MAX_STREAMS streams, at most MAX_SECONDS long, at most MAX_CLIP bytes;
- the player's caps (PER_DAY a day, PER_PLAYER in all) and the server's (PER_HOUR an hour, and
  MAX_WAITING clips waiting for the converter) allow it, and the disk quota has room;
- the converter turns it into an MP4 (H.264 and AAC) with a poster and frames for screening, and
  what it hands back passes the checks here (check_mp4, community.clean_jpeg). A clip whose
  picture is blank (every frame one flat colour) is not published.

Anything else is simply not published: the game's upload succeeds all the same.

The conversion does not happen in this process. The clip is copied to
DATA_DIR/videos/incoming/<id>.wmv with a job beside it (<id>.json, written last), and the video
converter (video/ in this repository: ffmpeg in a container with no network, a read-only root,
memory, CPU, process and file size limits) writes its result to DATA_DIR/videos/converted/<id>/
(result.json, video.mp4, poster.jpg, frame-<n>.jpg; the folder appears in one rename). collect()
(the server's tick, and every request to these routes) checks what it finds there, keeps the
cleaned files and deletes the rest. A clip the converter has not answered within CONVERT_WAIT is
dropped: a server without the converter publishes no videos and keeps no clips.

A video is "converting" (waiting for the converter), "held" (converted, waiting for screening),
"live" (in the feed and served) or "hidden" (by an operator or the screening). Every video is
held when it has been converted, whatever FORZA_COMMUNITY_HOLD says: a clip is never published
unscreened. The screening worker (screening/) scores the frames and releases, hides or leaves
it held; without it the operator releases it. A banned player's videos are served to nobody. The
same clip (SHA-256 of the upload) from the same player again moves its video to the newer file;
a clip an operator deleted is not published again. When the player's file goes (photos.remove),
its video goes with it. The player deletes one from the website's account page (owners.py).

Public routes, on the title services port (no side effects, never a XUID):

  GET /community/videos.json         {"version":1,"generated":<unix>,"videos":[{"id","gamertag",
                                     "owner","title","takenAt","approvedAt","seconds","w","h",
                                     "size","sha256","video","poster","posterSha256"}]}, live
                                     videos newest first, at most FEED_MAX; ETag, Cache-Control
  GET /community/video/<id>.mp4      a live video (byte ranges: 206 Partial Content, which a
                                     browser's player needs), nosniff, CSP default-src 'none'
  GET /community/video/<id>.jpg      its poster; both at most MEDIA_PER_MINUTE a minute

Operator routes (lsp.is_local_admin, as the photos': X-Admin-Token in its header, never
through the tunnel or a reverse proxy, never with an Origin header):

  GET  /admin/videos[?status=converting|held|live|hidden]   every video with its owner's XUID
  GET  /admin/videos/queue                  the held videos, oldest first, with their frames
  GET  /admin/videos/media/<id>.mp4 | .jpg  a video or its poster, whatever its status
  GET  /admin/videos/frame/<id>-<n>.jpg     a frame for screening
  POST /admin/videos/<id>/hide | unhide | hold | release | delete | ban

Files: DATA_DIR/videos.json (the index, durable) and DATA_DIR/videos/ (public/<id>.mp4 and
.jpg, frames/<id>-<n>.jpg, incoming/, converted/), in the server's disk quota. A video's id is
random (community.ID_LEN base32 characters), never the file's id.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import time
import urllib.parse
import uuid

from forza_live.content import community
from forza_live.content import designs
from forza_live.competition import leaderboards
from forza_live.core import durable
from forza_live.core import limits

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
PER_DAY = 5             # videos a day per player
PER_PLAYER = 50         # videos per player in all (converting, held, live and hidden)
PER_HOUR = 20           # videos an hour on the whole server
MAX_WAITING = 20        # clips waiting for the converter on the whole server
FEED_MAX = 200          # videos in the feed (the newest)
TITLE_MAX = 80
MAX_CLIP = 32 << 20     # bytes of an uploaded clip (photos.MAX_BIG: the large-file upload's own cap)
MAX_HEADER = 1 << 20    # bytes of its ASF header
MAX_SECONDS = 120       # a clip's length, as its header says (the converter cuts there too)
MIN_VIDEO = (96, 64)
MAX_VIDEO = (1920, 1088)
MAX_STREAMS = 4
MAX_MP4 = 32 << 20      # bytes of a converted video
MAX_POSTER = 2 << 20    # bytes of a poster or a frame
MAX_FRAMES = 16         # frames for screening
OUT_WIDTH = 1280        # the converter scales a wider clip down to this
FRAMES = 8              # frames the converter samples for screening
CONVERT_WAIT = 86400    # a clip the converter has not answered by then is dropped
COLLECT_SECONDS = 5     # how often the server's tick looks for converted clips
DELETED_MAX = 10000
FEED_SECONDS = 60
MEDIA_SECONDS = 86400
MEDIA_PER_MINUTE = 600
CONVERTING, HELD, LIVE, HIDDEN = "converting", "held", "live", "hidden"
STATUSES = (CONVERTING, HELD, LIVE, HIDDEN)
ADMIN_PREFIX = "/admin/videos"
ID_RE = community.ID_RE

INDEX = {"videos": {}, "uploads": {}, "serverUploads": [], "deleted": []}
_loaded = [False]
_feed = {"key": None, "body": b"", "etag": ""}
_version = [0]
_media = []        # times of the media requests of the last minute
_collected = [0.0]


def configure(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir
    INDEX.clear()
    INDEX.update({"videos": {}, "uploads": {}, "serverUploads": [], "deleted": []})
    _loaded[0] = False
    _feed.update({"key": None, "body": b"", "etag": ""})
    _version[0] += 1
    _media.clear()
    _collected[0] = 0.0


def index_path():
    return os.path.join(DATA_DIR, "videos.json")


def folder(*parts):
    return os.path.join(DATA_DIR, "videos", *parts)


def media_path(vid, ext):
    return folder("public", vid + ext)


def frame_path(vid, n):
    return folder("frames", "%s-%d.jpg" % (vid, n))


def load():
    if not _loaded[0]:
        _loaded[0] = True
        try:
            INDEX.update(durable.load(index_path()))
        except (OSError, ValueError):
            pass
    return INDEX


def save():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = index_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(INDEX, f, indent=1)
    durable.replace(tmp, index_path())
    _version[0] += 1


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(data)
    durable.replace(tmp, path)


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- recognising a clip: the ASF header (standard library only) ----

def guid(text):
    return uuid.UUID(text).bytes_le


ASF_HEADER = guid("75B22630-668E-11CF-A6D9-00AA0062CE6C")
ASF_FILE_PROPERTIES = guid("8CABDCA1-A947-11CF-8EE4-00C00C205365")
ASF_STREAM_PROPERTIES = guid("B7DC0791-A9B7-11CF-8EE6-00C00C205365")
ASF_CONTENT_DESCRIPTION = guid("75B22633-668E-11CF-A6D9-00AA0062CE6C")
ASF_VIDEO = guid("BC19EFC0-5B4D-11CF-A8FD-00805F5C442B")
ASF_AUDIO = guid("F8699E40-5B4D-11CF-A8FD-00805F5C442B")
CLIP_NAMES = (".wmv", ".asf")


class BadClip(ValueError):
    """A clip this server does not publish."""


def looks_like_clip(name, head):
    """The upload is a movie, not a picture: its first bytes are the ASF Header Object's GUID, or
    its file name says .wmv or .asf (such a file that is not ASF is refused as a clip, never read
    as a picture)."""
    return head[:16] == ASF_HEADER or (name or "").lower().endswith(CLIP_NAMES)


def utf16(data):
    return data.decode("utf-16-le", "replace").split("\x00")[0]


def read_asf(data):
    """{"seconds", "w", "h", "video", "audio", "title", "streams"} from a clip's ASF header (the
    Header Object at the start of the file and the objects in it). BadClip for anything that is
    not an ASF file, a damaged header, or a clip this server does not publish."""
    if len(data) < 30 or data[:16] != ASF_HEADER:
        raise BadClip("not an ASF file")
    size, count = struct.unpack_from("<QI", data, 16)
    if not 30 <= size <= min(len(data), MAX_HEADER) or count > 256:
        raise BadClip("a damaged ASF header")
    info = {"seconds": None, "title": "", "streams": 0, "video": None, "audio": None,
            "w": 0, "h": 0}
    i = 30
    for _ in range(count):
        if i + 24 > size:
            raise BadClip("a damaged ASF header")
        kind = data[i:i + 16]
        length = struct.unpack_from("<Q", data, i + 16)[0]
        if length < 24 or i + length > size:
            raise BadClip("a damaged ASF header")
        body = data[i + 24:i + length]
        if kind == ASF_FILE_PROPERTIES and len(body) >= 80:
            duration, _, preroll, flags = struct.unpack_from("<QQQI", body, 40)
            if flags & 1:  # broadcast: no duration
                raise BadClip("a broadcast stream, not a clip")
            info["seconds"] = max(0.0, duration / 1e7 - preroll / 1000.0)
        elif kind == ASF_STREAM_PROPERTIES and len(body) >= 54:
            info["streams"] += 1
            stype = body[:16]
            extra = struct.unpack_from("<I", body, 40)[0]
            spec = body[54:54 + extra]
            if stype == ASF_VIDEO:
                if info["video"] is not None:
                    raise BadClip("more than one video stream")
                if len(spec) < 11 + 20:
                    raise BadClip("a damaged video stream header")
                w, h = struct.unpack_from("<II", spec, 0)
                fourcc = spec[11 + 16:11 + 20]
                info.update(w=w, h=h, video=re.sub(r"[^A-Za-z0-9]", "?",
                                                   fourcc.decode("latin-1")))
            elif stype == ASF_AUDIO and info["audio"] is None and len(spec) >= 2:
                info["audio"] = "0x%04x" % struct.unpack_from("<H", spec, 0)[0]
        elif kind == ASF_CONTENT_DESCRIPTION and len(body) >= 10:
            title_len = struct.unpack_from("<H", body, 0)[0]
            info["title"] = utf16(body[10:10 + title_len])
        i += length
    if info["video"] is None:
        raise BadClip("no video stream")
    if info["streams"] > MAX_STREAMS:
        raise BadClip("%d streams" % info["streams"])
    if not (MIN_VIDEO[0] <= info["w"] <= MAX_VIDEO[0] and MIN_VIDEO[1] <= info["h"] <= MAX_VIDEO[1]):
        raise BadClip("video size %dx%d" % (info["w"], info["h"]))
    if info["seconds"] is None:
        raise BadClip("no file properties (no length)")
    if info["seconds"] > MAX_SECONDS:
        raise BadClip("%.1f seconds long (at most %d)" % (info["seconds"], MAX_SECONDS))
    return info


def clean_title(title):
    """The clip's title (the game's own: the car and the route) as plain text, or ""."""
    return limits.clean_ascii_text(title or "", TITLE_MAX).strip()


# ---- the upload ----

def upload_finished(fid, rec, path, signed):
    """photos' finished hook: a clip comes here (clip_uploaded), anything else goes to
    community.photo_uploaded as before."""
    try:
        with open(path, "rb") as src:
            head = src.read(16)
    except OSError:
        head = b""
    if looks_like_clip(rec.get("name", ""), head):
        return clip_uploaded(fid, rec, path, signed)
    return community.photo_uploaded(fid, rec, path, signed)


def file_removed(fid):
    """photos' removed hook: the player's file is gone, and so is its photo or video."""
    community.photo_removed(fid)
    gone = [vid for vid, v in load()["videos"].items() if v["fileId"] == fid]
    for vid in gone:
        drop(vid, "the player's file %d was removed" % fid)
    if gone:
        save()


def waiting(d):
    return sum(1 for v in d["videos"].values() if v["status"] == CONVERTING)


def clip_uploaded(fid, rec, path, signed):
    """The player's movie upload `fid` (its record and file) has arrived. The video's id, or
    None when it is not published."""
    xuid = rec["owner"]

    def refuse(why):
        log.info("videos: %016X: file %d (a movie) not published: %s", xuid, fid, why)
        return None

    if not signed:
        return refuse("the upload was not signed by the player's key")
    if designs.banned(xuid):
        return refuse("banned")
    gamertag = community.credit(xuid)
    if gamertag is None:
        return refuse("no gamertag on record")
    d = load()
    now = time.time()
    mine = community.recent(d["uploads"].get(community.hx(xuid), []), 86400, now)
    d["serverUploads"] = community.recent(d["serverUploads"], 3600, now)
    owned = sum(1 for v in d["videos"].values() if v["owner"] == xuid)
    why = ("the player's %d videos a day" % PER_DAY if len(mine) >= PER_DAY else
           "the player's %d videos" % PER_PLAYER if owned >= PER_PLAYER else
           "the server's %d videos an hour" % PER_HOUR if len(d["serverUploads"]) >= PER_HOUR else
           "%d clips are waiting for the converter" % MAX_WAITING if waiting(d) >= MAX_WAITING
           else None)
    if why:
        return refuse(why)
    try:
        if os.path.getsize(path) > MAX_CLIP:
            raise BadClip("larger than %d bytes" % MAX_CLIP)
        with open(path, "rb") as src:
            data = src.read(MAX_CLIP + 1)
        info = read_asf(data)
    except (OSError, BadClip) as e:
        return refuse(e)
    sha = hashlib.sha256(data).hexdigest()
    if sha in d["deleted"]:
        return refuse("an operator deleted this clip")
    for vid, v in d["videos"].items():
        if v["owner"] == xuid and v["clipSha256"] == sha:  # the same clip again: the newer file
            v["fileId"] = fid
            save()
            log.info("videos: %016X: file %d is video %s again", xuid, fid, vid)
            return vid
    try:
        limits.accept(len(data), MAX_CLIP)
    except ValueError as e:
        return refuse(e)
    vid = community.new_id(d["videos"])
    write(folder("incoming", vid + ".wmv"), data)
    job = {"version": 1, "id": vid, "maxSeconds": MAX_SECONDS, "maxWidth": OUT_WIDTH,
           "frames": FRAMES, "maxBytes": MAX_MP4}
    write(folder("incoming", vid + ".json"), json.dumps(job).encode())  # last: the job is whole
    d["videos"][vid] = {
        "owner": xuid, "fileId": fid, "gamertag": gamertag, "title": clean_title(info["title"]),
        "status": CONVERTING, "created": now, "takenAt": int(now), "approvedAt": None,
        "decidedAt": None, "note": "", "clipSha256": sha, "clipSize": len(data),
        "clip": {"video": info["video"], "audio": info["audio"], "w": info["w"], "h": info["h"],
                 "seconds": round(info["seconds"], 2)},
        "seconds": round(info["seconds"], 2), "w": info["w"], "h": info["h"],
        "sha256": "", "size": 0, "posterSha256": "", "frames": 0}
    d["uploads"][community.hx(xuid)] = mine + [now]
    d["serverUploads"].append(now)
    save()
    log.info("videos: %016X: file %d is video %s (%s %dx%d, %s audio, %.1f s, %d bytes), "
             "waiting for the converter", xuid, fid, vid, info["video"], info["w"], info["h"],
             info["audio"] or "no", info["seconds"], len(data))
    return vid


# ---- what the converter hands back ----

MP4_TOP = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"}
MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1"}


def check_mp4(data):
    """An MP4 as the converter writes it: top-level boxes ftyp (first, with an ISO brand), one
    moov holding an H.264 track, mdat, and padding boxes only, filling the file exactly.
    BadClip for anything else."""
    if len(data) > MAX_MP4:
        raise BadClip("a converted video of %d bytes" % len(data))
    i, seen = 0, []
    while i < len(data):
        if i + 8 > len(data):
            raise BadClip("the video ends inside a box")
        size, kind = struct.unpack_from(">I4s", data, i)
        head = 8
        if size == 1:
            if i + 16 > len(data):
                raise BadClip("the video ends inside a box")
            size, head = struct.unpack_from(">Q", data, i + 8)[0], 16
        elif size == 0:
            size = len(data) - i
        if size < head or i + size > len(data):
            raise BadClip("a box of the video has a bad size")
        if kind not in MP4_TOP:
            raise BadClip("an unexpected box %r in the video" % kind.decode("latin-1"))
        if kind == b"ftyp":
            if seen or size < 16 or data[i + 8:i + 12] not in MP4_BRANDS:
                raise BadClip("the video's ftyp box")
        elif kind == b"moov":
            if b"moov" in seen:
                raise BadClip("two moov boxes")
            if b"avc1" not in data[i:i + size]:
                raise BadClip("the video has no H.264 track")
        seen.append(kind)
        i += size
    if not seen or seen[0] != b"ftyp" or b"moov" not in seen or b"mdat" not in seen:
        raise BadClip("not an MP4 file")


def picture(data, what):
    """A poster or frame from the converter, as community.clean_jpeg cleans it: (file, w, h)."""
    if len(data) > MAX_POSTER:
        raise BadClip("a %s of %d bytes" % (what, len(data)))
    try:
        return community.clean_jpeg(data, MIN_VIDEO, MAX_VIDEO)
    except community.BadImage as e:
        raise BadClip("the %s: %s" % (what, e)) from None


def read_small(path, limit):
    with open(path, "rb") as src:
        data = src.read(limit + 1)
    if len(data) > limit:
        raise BadClip("%s is larger than %d bytes" % (os.path.basename(path), limit))
    return data


def finish(vid, v, out):
    """The converter's result folder `out` for the converting video: the video held with its
    files, or BadClip."""
    try:
        result = json.loads(read_small(os.path.join(out, "result.json"), 65536))
    except (OSError, ValueError) as e:
        raise BadClip("the converter's result: %s" % e) from None
    if not isinstance(result, dict) or result.get("id") != vid:
        raise BadClip("the converter's result is not this clip's")
    if not result.get("ok"):
        raise BadClip(limits.clean_ascii_text(str(result.get("reason") or "the converter failed"), 200))
    seconds = result.get("seconds")
    if not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_SECONDS + 1:
        raise BadClip("the converted video's length")
    try:
        video = read_small(os.path.join(out, "video.mp4"), MAX_MP4)
        poster = read_small(os.path.join(out, "poster.jpg"), MAX_POSTER)
        names = sorted((n for n in os.listdir(out) if re.fullmatch(r"frame-\d{1,2}\.jpg", n)),
                       key=lambda n: int(n[6:-4]))
        frames = [read_small(os.path.join(out, n), MAX_POSTER) for n in names[:MAX_FRAMES + 1]]
    except OSError as e:
        raise BadClip("the converter's files: %s" % e) from None
    check_mp4(video)
    poster, w, h = picture(poster, "poster")
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise BadClip("%d frames for screening" % len(frames))
    frames = [picture(f, "frame")[0] for f in frames]
    total = len(video) + len(poster) + sum(map(len, frames))
    limits.accept(total, MAX_MP4 + (MAX_FRAMES + 1) * MAX_POSTER)  # ValueError: the quota
    write(media_path(vid, ".mp4"), video)
    write(media_path(vid, ".jpg"), poster)
    for n, f in enumerate(frames, 1):
        write(frame_path(vid, n), f)
    v.update(status=HELD, seconds=round(float(seconds), 2), w=w, h=h, size=len(video),
             sha256=hashlib.sha256(video).hexdigest(), posterSha256=hashlib.sha256(poster).hexdigest(),
             frames=len(frames), convertedAt=int(time.time()))


def collect(now=None):
    """The converter's results: each converting video is held with its files or dropped; clips
    the converter has not answered within CONVERT_WAIT are dropped. Leftovers of videos that are
    gone are deleted. True when anything changed."""
    now = time.time() if now is None else now
    d = load()
    changed = False
    done = folder("converted")
    try:
        names = os.listdir(done)
    except OSError:
        names = []
    for name in names:
        if not ID_RE.fullmatch(name):
            continue  # the converter's own (a folder being written, its attempt notes)
        out = os.path.join(done, name)
        v = d["videos"].get(name)
        if v is not None and v["status"] == CONVERTING:
            try:
                finish(name, v, out)
                log.info("videos: %016X: video %s converted (%dx%d, %.1f s, %d bytes), held for "
                         "screening", v["owner"], name, v["w"], v["h"], v["seconds"], v["size"])
            except (BadClip, ValueError) as e:
                d["videos"].pop(name)
                remove_media(name)
                log.info("videos: %016X: file %d (a movie) not published: %s", v["owner"],
                         v["fileId"], e)
            changed = True
        shutil.rmtree(out, ignore_errors=True)
        for ext in (".wmv", ".json"):
            remove_file(folder("incoming", name + ext))
    for vid, v in list(d["videos"].items()):
        if v["status"] == CONVERTING and now - v["created"] > CONVERT_WAIT:
            d["videos"].pop(vid)
            remove_media(vid)
            log.warning("videos: %016X: file %d (a movie) not published: the converter did not "
                        "answer in %d hours (is it running?)", v["owner"], v["fileId"],
                        CONVERT_WAIT // 3600)
            changed = True
    if changed:
        save()
    return changed


def tick(now=None):
    """The server's tick (server.main): collect() every COLLECT_SECONDS."""
    now = time.time() if now is None else now
    if now - _collected[0] >= COLLECT_SECONDS:
        _collected[0] = now
        try:
            collect(now)
        except Exception:  # never take the server's loop down
            log.exception("videos: collecting the converter's results failed")


# ---- the videos ----

def remove_media(vid):
    for ext in (".mp4", ".jpg"):
        remove_file(media_path(vid, ext))
    for n in range(1, MAX_FRAMES + 1):
        remove_file(frame_path(vid, n))
    for ext in (".wmv", ".json"):
        remove_file(folder("incoming", vid + ext))


def drop(vid, why):
    v = load()["videos"].pop(vid, None)
    if v is None:
        return None
    remove_media(vid)
    log.info("videos: video %s (%016X) removed: %s", vid, v["owner"], why)
    return v


def public(v, bans):
    return v["status"] == LIVE and v["owner"] not in bans


def feed_entry(vid, v):
    """A video as the feed lists it: exactly these fields (the website's contract)."""
    return {"id": vid, "gamertag": v["gamertag"],
            "owner": leaderboards.player_id(community.hx(v["owner"])), "title": v["title"],
            "takenAt": v["takenAt"], "approvedAt": v["approvedAt"], "seconds": v["seconds"],
            "w": v["w"], "h": v["h"], "size": v["size"], "sha256": v["sha256"],
            "video": "/community/video/%s.mp4" % vid, "poster": "/community/video/%s.jpg" % vid,
            "posterSha256": v["posterSha256"]}


def feed():
    """(body, ETag) of the feed, built again only when the videos or the bans changed."""
    bans = designs.banned_xuids()
    key = (_version[0], bans)
    if _feed["key"] != key:
        live = [(vid, v) for vid, v in load()["videos"].items() if public(v, bans)]
        live.sort(key=lambda e: (e[1]["approvedAt"] or 0, e[1]["created"], e[0]), reverse=True)
        doc = {"version": 1, "generated": int(time.time()),
               "videos": [feed_entry(vid, v) for vid, v in live[:FEED_MAX]]}
        body = json.dumps(doc, separators=(",", ":")).encode()
        _feed.update({"key": key, "body": body,
                      "etag": '"%s"' % hashlib.sha256(json.dumps(doc["videos"]).encode()).hexdigest()[:32]})
    return _feed["body"], _feed["etag"]


def queue():
    """The held videos, oldest first: what the screening worker looks at."""
    held = [(vid, v) for vid, v in load()["videos"].items() if v["status"] == HELD]
    return sorted(held, key=lambda e: (e[1]["created"], e[0]))


# action: (the statuses it applies to, the status it gives); a converting video is not
# released or hidden: it has nothing to show yet (delete works on it)
ACTIONS = {"hide": ((LIVE, HELD), HIDDEN), "unhide": ((HIDDEN,), LIVE),
           "hold": ((LIVE,), HELD), "release": ((HELD,), LIVE)}


def act(vid, action, note=None):
    """An operator's or the screening worker's action on a video (ACTIONS): its record, None for
    an unknown id, ValueError when its status does not allow it."""
    v = load()["videos"].get(vid)
    if v is None:
        return None
    before, status = ACTIONS[action]
    if v["status"] not in before:
        raise ValueError("video %s is %s: %s does not apply" % (vid, v["status"], action))
    v["status"] = status
    v["decidedAt"] = int(time.time())
    if status == LIVE and v["approvedAt"] is None:
        v["approvedAt"] = v["decidedAt"]
    if note is not None:
        v["note"] = limits.clean_ascii_text(note, 200)
    save()
    log.info("videos: video %s (%016X) is %s", vid, v["owner"], status)
    return v


def delete(vid):
    """Removes the video and its files; the same clip is not published again."""
    d = load()
    v = drop(vid, "deleted by the operator")
    if v is None:
        return None
    d["deleted"] = (d["deleted"] + [v["clipSha256"]])[-DELETED_MAX:]
    save()
    return v


def admin_record(vid, v, bans):
    rec = {"id": vid, "owner": community.hx(v["owner"]), "gamertag": v["gamertag"],
           "title": v["title"], "status": v["status"], "banned": v["owner"] in bans,
           "public": public(v, bans), "takenAt": v["takenAt"], "approvedAt": v["approvedAt"],
           "decidedAt": v["decidedAt"], "note": v.get("note", ""), "seconds": v["seconds"],
           "w": v["w"], "h": v["h"], "size": v["size"], "sha256": v["sha256"],
           "clipSha256": v["clipSha256"], "clipSize": v["clipSize"], "clip": v.get("clip", {}),
           "fileId": v["fileId"], "frames": [], "video": None, "poster": None}
    if v["status"] != CONVERTING:
        rec.update(video=ADMIN_PREFIX + "/media/%s.mp4" % vid,
                   poster=ADMIN_PREFIX + "/media/%s.jpg" % vid,
                   frames=[ADMIN_PREFIX + "/frame/%s-%d.jpg" % (vid, n)
                           for n in range(1, v["frames"] + 1)])
    return rec


# ---- HTTP ----

def handles(path):
    p = urllib.parse.urlsplit(path).path
    return (p == "/community/videos.json" or p.startswith("/community/video/")
            or p == ADMIN_PREFIX or p.startswith(ADMIN_PREFIX + "/"))


reply = community.reply
TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}
MEDIA_RE = re.compile(r"/([A-Z2-7]{%d})(\.mp4|\.jpg)" % community.ID_LEN)
FRAME_RE = re.compile(r"/frame/([A-Z2-7]{%d})-(\d{1,2})\.jpg" % community.ID_LEN)
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def byte_range(header, size):
    """(start, end) of a single "bytes=" range within size bytes (end inclusive), None for no
    usable Range header (the whole file), or "bad" for one that cannot be served."""
    if not header:
        return None
    m = RANGE_RE.fullmatch(header.strip())
    if m is None:
        return None  # several ranges or another unit: the whole file, as RFC 9110 allows
    first, last = m.groups()
    if not first and not last:
        return "bad"
    if not first:  # the last N bytes
        n = int(last)
        if n == 0:
            return "bad"
        return max(0, size - n), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return "bad"
    return start, end


def media_reply(path, ext, headers, cache, etag):
    """A video (with byte ranges), a poster or a frame; only the bytes asked for are read."""
    extra = community.SAFE + ("ETag: " + etag, "Cache-Control: " + cache, "Accept-Ranges: bytes")
    try:
        with open(path, "rb") as src:
            size = os.fstat(src.fileno()).st_size
            if headers.get("if-none-match") == etag:
                return 304, TYPES[ext], b"", extra
            r = byte_range(headers.get("range"), size) if ext == ".mp4" else None
            if r == "bad":
                return 416, "text/plain", b"", extra + ("Content-Range: bytes */%d" % size,)
            if r is not None and headers.get("if-range", etag) == etag:
                start, end = r
                src.seek(start)
                return (206, TYPES[ext], src.read(end + 1 - start),
                        extra + ("Content-Range: bytes %d-%d/%d" % (start, end, size),))
            return 200, TYPES[ext], src.read(), extra
    except OSError:
        return reply(404, {"error": "no such video"})


def media_budget():
    now = time.monotonic()
    _media[:] = [t for t in _media if now - t < 60]
    if len(_media) >= MEDIA_PER_MINUTE:
        return False
    _media.append(now)
    return True


def media_of(path):
    """(id, record, extension, ETag) of the media file a path names (/<id>.mp4 or .jpg), or
    None."""
    m = MEDIA_RE.fullmatch(path)
    v = load()["videos"].get(m.group(1)) if m else None
    if v is None or v["status"] == CONVERTING:
        return None
    sha = v["sha256"] if m.group(2) == ".mp4" else v["posterSha256"]
    return m.group(1), v, m.group(2), '"%s"' % sha


def handle(method, path, headers, body, admin):
    """(status, content type, body, extra headers) for the videos' routes; None for another
    path. admin(): the request may use the operator's routes."""
    if not handles(path):
        return None
    url = urllib.parse.urlsplit(path)
    if url.path.startswith(ADMIN_PREFIX):
        return admin_handle(method, url, headers, body, admin)
    tick()
    if method != "GET":
        return reply(405, {"error": "GET only"})
    if url.path == "/community/videos.json":
        data, etag = feed()
        extra = community.SAFE + ("ETag: " + etag, "Cache-Control: public, max-age=%d" % FEED_SECONDS)
        if headers.get("if-none-match") == etag:
            return 304, "application/json", b"", extra
        return 200, "application/json", data, extra
    found = media_of(url.path[len("/community/video"):])
    if found is None or not public(found[1], designs.banned_xuids()):
        return reply(404, {"error": "no such video"})
    if not media_budget():
        return reply(429, {"error": "too many requests, try again in a minute"})
    vid, _, ext, etag = found
    return media_reply(media_path(vid, ext), ext, headers, "public, max-age=%d" % MEDIA_SECONDS, etag)


def admin_handle(method, url, headers, body, admin):
    if "origin" in headers:
        return reply(403, {"error": "use curl or a script, not a browser page"})
    if not admin():
        return reply(403, {"error": "forbidden: the X-Admin-Token header, from the LAN or an ssh "
                                    "tunnel (never through the tunnel or a proxy)"})
    collect()  # what the converter finished since the server's last tick
    rest = url.path[len(ADMIN_PREFIX):].strip("/")
    parts = rest.split("/") if rest else []
    bans = designs.banned_xuids()
    if method == "GET":
        if not parts:
            want = urllib.parse.parse_qs(url.query).get("status", [None])[0]
            if want is not None and want not in STATUSES:
                return reply(400, {"error": "status: " + ", ".join(STATUSES)})
            videos = sorted(load()["videos"].items(), key=lambda e: (e[1]["created"], e[0]),
                            reverse=True)
            return reply(200, {"videos": [admin_record(vid, v, bans) for vid, v in videos
                                          if want is None or v["status"] == want],
                               "banned": sorted(community.hx(b) for b in bans)})
        if parts == ["queue"]:
            return reply(200, {"videos": [admin_record(vid, v, bans) for vid, v in queue()]})
        if len(parts) == 2 and parts[0] == "media":
            found = media_of("/" + parts[1])
            if found is not None:
                vid, _, ext, etag = found
                return media_reply(media_path(vid, ext), ext, headers, "no-store", etag)
        m = FRAME_RE.fullmatch("/" + rest)
        if m is not None:
            v = load()["videos"].get(m.group(1))
            n = int(m.group(2))
            if v is not None and v["status"] != CONVERTING and 1 <= n <= v["frames"]:
                return media_reply(frame_path(m.group(1), n), ".jpg", headers, "no-store",
                                   '"%s-%d"' % (v["sha256"][:32], n))
        return reply(404, {"error": "not found"})
    if method != "POST":
        return reply(405, {"error": "GET or POST"})
    if len(parts) != 2 or not ID_RE.fullmatch(parts[0]):
        return reply(404, {"error": "not found"})
    vid, action = parts
    v = load()["videos"].get(vid)
    if v is None:
        return reply(404, {"error": "no such video"})
    if action == "delete":
        delete(vid)
        return reply(200, {"deleted": vid})
    if action == "ban":
        return community.ban(v["owner"], True)
    if action not in ACTIONS:
        return reply(404, {"error": "hide, unhide, hold, release, delete or ban"})
    note = None
    if body:
        try:
            note = json.loads(body).get("note")
        except (ValueError, AttributeError):
            return reply(400, {"error": "a JSON object, {\"note\": ...}"})
    try:
        act(vid, action, note if isinstance(note, str) else None)
    except ValueError as e:
        return reply(409, {"error": str(e)})
    return reply(200, admin_record(vid, v, designs.banned_xuids()))
