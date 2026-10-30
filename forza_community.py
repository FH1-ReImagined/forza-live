"""Forza Live: community photos, the pictures players upload from the game's photo mode, published
for the website's gallery (FH1-ReImagined/website tools/build_gallery.py reads the feed).

Photo mode's upload is the BIG SHOT: TAKE PHOTO, RB (BIG SHOT : 8.3 MEGAPIXEL), A, then "You are
about to create a large photo and upload it to ForzaMotorsport.net ... Are you sure you want to
continue?" YES sends LargeFileUpload.Create(24883254, overwriteOldest false, "ForzaBigshot.bmp")
and the picture, a 3840x2160 24-bit BMP (top-down), in 256 KiB Content.UploadContent chunks; the
game then says "Your Big Shot photo was successfully uploaded to the ForzaMotorsport.net server.
You can retrieve it from there later." (TU4, October 2026). When such an upload finishes,
forza_photos hands it here (photo_uploaded), and it becomes a community photo if:

- the upload was signed with the player's own key (forza_auth, X-Forza-Auth): an older game, or
  one carrying a raw friends key, has its big shots refused as before (forza_photos), and its
  smaller uploads stay private files and nothing more;
- the player is not on the ban list (DATA_DIR/banned_xuids.json, shared with forza_designs) and
  has a gamertag on record;
- the picture is one this module reads, MIN_SIZE to MAX_SIZE pixels: a 24-bit BMP (the big
  shot), published as a PNG halved down to COPY_MAX_WIDTH (bmp_to_png; not when every tile of it
  is the same picture: one_tile_repeated), or a JPEG (one baseline or progressive frame, 8-bit,
  greyscale or colour) rewritten with only what a decoder needs (clean_jpeg). Either way the
  file served is made here: no EXIF, XMP, GPS, comment, thumbnail or trailing data survives;
- the player's caps (PER_DAY a day, PER_PLAYER in all) and the server's (PER_HOUR an hour) allow
  it, and the disk quota (forza_limits) has room.

Anything else is simply not published: the game's upload succeeds all the same. The same picture
(SHA-256 of the published file) from the same player again moves its photo to the newer file
rather than adding a second one; a picture an operator deleted is not published again.

A photo is "live" (in the feed and served), "held" (waiting for screening: hold_new() and the
queue below; nothing is held unless FORZA_COMMUNITY_HOLD=1) or "hidden" (by an operator). A
banned player's photos are served to nobody, whatever their status, until the ban is lifted.
When the player's file goes (an upload with overwriteOldest makes room by dropping the player's
oldest file, forza_photos.remove), its community photo goes with it. A big shot is not kept as a
file (forza_photos forgets it once it is here), and the game sends overwriteOldest false: a player
cannot withdraw one from the game; the operator removes it.

Public routes, on the title services port (no side effects, never a XUID):

  GET /community/gallery.json   {"version":1,"generated":<unix>,"photos":[{"id","gamertag",
                                "title","takenAt","approvedAt","w","h","sha256","image"}]}, live
                                photos newest first, at most FEED_MAX; ETag, Cache-Control
  GET /community/img/<id>.png   a live photo (.jpg for a JPEG): nosniff, CSP default-src 'none',
                                cached for IMAGE_SECONDS; at most IMAGES_PER_MINUTE a minute

Operator routes (lsp.is_local_admin: X-Admin-Token in its header, never ?token=, and never
through the tunnel or a reverse proxy; requests with an Origin header are refused):

  GET  /admin/photos[?status=live|held|hidden]   every photo with its owner's XUID
  GET  /admin/photos/queue                       the held photos, oldest first (screening)
  GET  /admin/photos/img/<id>.png (or .jpg)      a photo, whatever its status
  POST /admin/photos/<id>/hide | unhide | hold | release | delete | ban
  POST /admin/photos/ban/<XUID> | unban/<XUID>

Files: DATA_DIR/community.json (the index, forza_durable) and DATA_DIR/community/<id>.png or .jpg,
in the server's disk quota. A photo's id is random (ID_LEN base32 characters), never the file's
id.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import struct
import time
import urllib.parse
import zlib

import forza_designs as designs
import forza_durable as durable
import forza_limits as limits

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
PER_DAY = 10            # photos a day per player
PER_PLAYER = 200        # photos per player in all (live, held and hidden)
PER_HOUR = 60           # photos an hour on the whole server
FEED_MAX = 500          # photos in the feed (the newest)
TITLE_MAX = 40
MIN_SIZE = (640, 360)
MAX_SIZE = (7680, 4320)
MAX_ASPECT = 3.0        # the website refuses wider or taller pictures
# a big shot's copy is halved down to this width (the website shows 1920 at most)
COPY_MAX_WIDTH = int(os.environ.get("FORZA_COMMUNITY_MAX_WIDTH", "1920"))
MAX_COPY = 16 << 20     # bytes of a published picture (the website takes up to 16 MiB)
MAX_SEGMENTS = 512      # JPEG segments (markers) in one file
MAX_SCANS = 64          # a progressive JPEG's scans
DELETED_MAX = 10000     # pictures (SHA-256) an operator deleted, kept so they are not published again
FEED_SECONDS = 60       # the feed's Cache-Control max-age
IMAGE_SECONDS = 86400   # an image's
IMAGES_PER_MINUTE = 600  # image requests the server answers a minute (the website fetches each once)
ID_LEN = 12
ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
ID_RE = re.compile("[A-Z2-7]{%d}" % ID_LEN)
LIVE, HELD, HIDDEN = "live", "held", "hidden"
HOLD_NEW = os.environ.get("FORZA_COMMUNITY_HOLD", "0") == "1"
ADMIN_PREFIX = "/admin/photos"

INDEX = {"photos": {}, "uploads": {}, "serverUploads": [], "deleted": []}
_loaded = [False]
_feed = {"key": None, "body": b"", "etag": ""}
_version = [0]
_images = []  # times of the image requests of the last minute
_gamertag = [lambda xuid: ""]


def configure(data_dir, gamertag=None):
    """gamertag(xuid): the name the player signed in with ("" when unknown)."""
    global DATA_DIR
    DATA_DIR = data_dir
    INDEX.clear()
    INDEX.update({"photos": {}, "uploads": {}, "serverUploads": [], "deleted": []})
    _loaded[0] = False
    _feed.update({"key": None, "body": b"", "etag": ""})
    _version[0] += 1
    _images.clear()
    _gamertag[0] = gamertag or (lambda xuid: "")


def index_path():
    return os.path.join(DATA_DIR, "community.json")


TYPES = {"jpeg": (".jpg", "image/jpeg"), "png": (".png", "image/png")}


def image_path(pid, kind="jpeg"):
    return os.path.join(DATA_DIR, "community", pid + TYPES[kind][0])


def image_url(pid, p):
    return "/community/img/%s%s" % (pid, TYPES[p.get("type", "jpeg")][0])


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


def hx(xuid):
    return "%016X" % xuid


# ---- JPEG: validation and cleaning (standard library only) ----

SOI, EOI, SOS = 0xD8, 0xD9, 0xDA
SOF_KEPT = (0xC0, 0xC2)  # baseline, progressive (Huffman)
# Segments a decoder needs besides the frame and its scans: quantization and Huffman tables,
# the restart interval. APP0 (JFIF) and APP14 (Adobe: the colour transform) are kept shortened
# to their fixed fields. Every other APPn (EXIF, XMP, ICC, Photoshop...), COM and anything else
# is dropped.
DQT, DHT, DRI, APP0, APP14 = 0xDB, 0xC4, 0xDD, 0xE0, 0xEE
DROPPED = set(range(0xE1, 0xEE)) | {0xEF, 0xFE}


class BadImage(ValueError):
    """A picture this server does not publish."""


def _scan_end(data, i):
    """The offset of the marker that ends the entropy-coded data starting at i (the 0xFF of the
    marker itself, after any fill bytes)."""
    n = len(data)
    while True:
        i = data.find(b"\xff", i)
        if i < 0 or i + 1 >= n:
            raise BadImage("the scan data has no end")
        nxt = data[i + 1]
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:  # a stuffed 0xFF, a restart marker
            i += 2
            continue
        if nxt == 0xFF:  # fill bytes before a marker (dropped with the marker's own 0xFF)
            j = i + 1
            while j < n and data[j] == 0xFF:
                j += 1
            if j >= n:
                raise BadImage("the scan data has no end")
            if data[j] == 0x00 or 0xD0 <= data[j] <= 0xD7:
                i = j + 1
                continue
        return i


def clean_jpeg(data):
    """(cleaned JPEG, width, height) of a JPEG file; BadImage when it is anything else or not one
    this server publishes. The cleaned file holds SOI, a shortened JFIF APP0 and Adobe APP14 when
    the file had them, its DQT, DHT and DRI segments, the frame (SOF0 or SOF2) and its scans
    with their data, and EOI: nothing that is not needed to decode the picture."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4 or data[:2] != b"\xff\xd8":
        raise BadImage("not a JPEG file")
    data = bytes(data)
    out = [b"\xff\xd8"]
    i, n = 2, len(data)
    frame = None
    scans = segments = 0
    tables = set()
    while True:
        if i >= n or data[i] != 0xFF:
            raise BadImage("no marker at offset %d" % i)
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            raise BadImage("the file ends inside a marker")
        marker = data[i]
        i += 1
        segments += 1
        if segments > MAX_SEGMENTS:
            raise BadImage("too many segments")
        if marker == EOI:
            break
        if marker == SOI or marker == 0x00 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            raise BadImage("marker %02X out of place" % marker)
        if i + 2 > n:
            raise BadImage("the file ends inside a segment")
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2 or i + length > n:
            raise BadImage("segment %02X has a bad length" % marker)
        body = data[i + 2:i + length]
        segment = data[i - 2:i + length]
        i += length
        if marker in SOF_KEPT:
            if frame is not None:
                raise BadImage("more than one frame")
            if len(body) < 6:
                raise BadImage("short frame header")
            bits, h, w, comps = body[0], int.from_bytes(body[1:3], "big"), \
                int.from_bytes(body[3:5], "big"), body[5]
            if bits != 8:
                raise BadImage("%d-bit samples" % bits)
            if comps not in (1, 3) or len(body) != 6 + 3 * comps:
                raise BadImage("%d components" % comps)
            if not (MIN_SIZE[0] <= w <= MAX_SIZE[0] and MIN_SIZE[1] <= h <= MAX_SIZE[1]):
                raise BadImage("size %dx%d" % (w, h))
            if max(w / h, h / w) > MAX_ASPECT:
                raise BadImage("aspect ratio %dx%d" % (w, h))
            for c in range(comps):
                sampling = body[7 + 3 * c]
                if not (1 <= sampling >> 4 <= 4 and 1 <= sampling & 15 <= 4):
                    raise BadImage("bad sampling factors")
            frame = (w, h, marker)
            out.append(segment)
        elif 0xC0 <= marker <= 0xCF and marker != DHT:  # other frame types, JPG, DAC
            raise BadImage("frame type %02X (only baseline and progressive JPEG)" % marker)
        elif marker == SOS:
            if frame is None:
                raise BadImage("a scan before the frame")
            if DQT not in tables or DHT not in tables:
                raise BadImage("a scan without quantization or Huffman tables")
            ns = body[0] if body else 0
            if not 1 <= ns <= 4 or len(body) != 4 + 2 * ns:
                raise BadImage("bad scan header")
            scans += 1
            if scans > MAX_SCANS:
                raise BadImage("too many scans")
            end = _scan_end(data, i)
            out.append(segment)
            out.append(data[i:end])
            i = end
        elif marker in (DQT, DHT, DRI):
            if marker == DRI and length != 4:
                raise BadImage("bad restart interval")
            tables.add(marker)
            out.append(segment)
        elif marker == APP0:
            if body[:5] == b"JFIF\x00" and len(body) >= 14:
                # version, units, densities; no thumbnail (its width and height 0)
                out.append(b"\xff\xe0\x00\x10" + body[:12] + b"\x00\x00")
        elif marker == APP14:
            if body[:5] == b"Adobe" and len(body) >= 12:
                out.append(b"\xff\xee\x00\x0e" + body[:12])
        elif marker in DROPPED:
            pass
        else:  # DNL, DHP, EXP, JPGn, reserved: not in a picture this server takes
            raise BadImage("marker %02X" % marker)
    if frame is None or scans == 0:
        raise BadImage("no picture")
    out.append(b"\xff\xd9")  # whatever followed EOI is dropped
    return b"".join(out), frame[0], frame[1]


# ---- BMP (photo mode's big shot) to PNG, standard library only ----

def read_bmp(data):
    """(rows, width, height) of a 24-bit uncompressed BMP: its rows top to bottom, each the RGB
    bytes of its pixels. BadImage (the same refusal) for anything else, or a size this server does
    not publish."""
    if len(data) < 54 or data[:2] != b"BM":
        raise BadImage("not a BMP file")
    offset = int.from_bytes(data[10:14], "little")
    header, w, h = (int.from_bytes(data[14:18], "little"), int.from_bytes(data[18:22], "little", signed=True),
                    int.from_bytes(data[22:26], "little", signed=True))
    planes, bits, compression = (int.from_bytes(data[26:28], "little"), int.from_bytes(data[28:30], "little"),
                                 int.from_bytes(data[30:34], "little"))
    if header not in (40, 108, 124) or planes != 1 or bits != 24 or compression != 0:
        raise BadImage("a BMP other than 24-bit uncompressed")
    top_down, h = h < 0, abs(h)
    if not (MIN_SIZE[0] <= w <= MAX_SIZE[0] and MIN_SIZE[1] <= h <= MAX_SIZE[1]):
        raise BadImage("size %dx%d" % (w, h))
    if max(w / h, h / w) > MAX_ASPECT:
        raise BadImage("aspect ratio %dx%d" % (w, h))
    stride = (w * 3 + 3) & ~3
    if offset < 14 + header or offset + stride * h > len(data):
        raise BadImage("the BMP's pixels are not all there")
    rows = []
    for y in range(h):
        start = offset + stride * (y if top_down else h - 1 - y)
        bgr = data[start:start + w * 3]
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = bgr[2::3], bgr[1::3], bgr[0::3]
        rows.append(bytes(rgb))
    return rows, w, h


def one_tile_repeated(rows, w, h, n=3):
    """The picture is one tile repeated n x n times. A big shot is rendered in 3 x 3 tiles of the
    screen; when the game's capture of them fails, every tile holds the same picture (the
    loading screen, in October 2026): nothing to publish."""
    if w % n or h % n:
        return False
    t, th = w // n * 3, h // n
    return (all(r[:t] == r[i * t:(i + 1) * t] for r in rows for i in range(1, n))
            and all(rows[y] == rows[y + i * th] for y in range(th) for i in range(1, n)))


def _masks(nbytes):
    return (int.from_bytes(b"\x80" * nbytes, "big"), int.from_bytes(b"\x7f" * nbytes, "big"),
            (1 << 8 * nbytes) - 1)


def halve(rows, w, h):
    """The picture at half its size, each pixel the average of 2 x 2 (whole bytes at a time:
    big integers do the arithmetic of every byte of a row at once)."""
    half_w, n = w // 2, (w // 2) * 6
    lo, lo_half = _masks(n)[1], _masks(n // 2)[1]
    out = []
    for y in range(0, h - 1, 2):
        a, b = int.from_bytes(rows[y][:n], "big"), int.from_bytes(rows[y + 1][:n], "big")
        v = ((a & b) + (((a ^ b) >> 1) & lo)).to_bytes(n, "big")  # the rows' average, rounded down
        even, odd = bytearray(n // 2), bytearray(n // 2)
        even[0::3], even[1::3], even[2::3] = v[0::6], v[1::6], v[2::6]
        odd[0::3], odd[1::3], odd[2::3] = v[3::6], v[4::6], v[5::6]
        e, o = int.from_bytes(even, "big"), int.from_bytes(odd, "big")
        out.append(((e | o) - (((e ^ o) >> 1) & lo_half)).to_bytes(n // 2, "big"))  # rounded up
    return out, half_w, h // 2


def png(rows, w, h):
    """An RGB PNG of the rows: IHDR, IDAT and IEND only. Each row is filtered with PNG's Average
    filter (the byte minus the mean of the byte to its left and the one above), worked out a
    whole row at a time with big integers."""
    n = w * 3
    hi, lo, mask = _masks(n)
    out, prev = [], 0
    for row in rows:
        x = int.from_bytes(row, "big")
        left = x >> 24  # the pixel to the left (none for the first)
        mean = (left & prev) + (((left ^ prev) >> 1) & lo)
        diff = (((x | hi) - (mean & lo)) ^ ((x ^ (~mean & mask)) & hi)) & mask
        out.append(b"\x03" + diff.to_bytes(n, "big"))
        prev = x

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(out), 6)) + chunk(b"IEND", b""))


def bmp_to_png(data):
    """(PNG, width, height) of photo mode's big shot (a BMP), halved until it is at most
    COPY_MAX_WIDTH wide. BadImage for anything else, or a failed capture (one_tile_repeated)."""
    rows, w, h = read_bmp(data)
    if one_tile_repeated(rows, w, h):
        raise BadImage("every tile of the big shot is the same picture (the game's capture failed)")
    while w > COPY_MAX_WIDTH and w // 2 >= MIN_SIZE[0] and h // 2 >= MIN_SIZE[1]:
        rows, w, h = halve(rows, w, h)
    return png(rows, w, h), w, h


# ---- the photos ----

def new_id(photos):
    while True:
        pid = "".join(secrets.choice(ID_CHARS) for _ in range(ID_LEN))
        if pid not in photos:
            return pid


def recent(times, seconds, now):
    return [t for t in times if now - t < seconds]


def clean_title(name):
    """The upload's file name as a title: no extension, and nothing for the name photo mode gives
    every big shot."""
    title = limits.clean_ascii_text(name, 64)
    for ext in (".jpeg", ".jpg", ".bmp", ".png"):
        if title.lower().endswith(ext):
            title = title[:-len(ext)].rstrip()
    return "" if title.lower() == "forzabigshot" else title[:TITLE_MAX].strip()


def picture(data):
    """(file, type, width, height) of what is published for an upload: a JPEG cleaned
    (clean_jpeg), a BMP as a PNG (bmp_to_png). BadImage for anything else."""
    if data[:2] == b"BM":
        out, w, h = bmp_to_png(data)
        return out, "png", w, h
    clean, w, h = clean_jpeg(data)
    return clean, "jpeg", w, h


def credit(xuid):
    """The player's gamertag as the website credits it, or None when none is on record (never a
    name made from the XUID)."""
    raw = _gamertag[0](xuid) or ""
    if not any(c.isalnum() for c in limits.ascii_fold(raw)):
        return None
    return limits.clean_gamertag(raw, xuid)


def hold_new(xuid, title):
    """The state a new photo starts in. HELD keeps it out of the feed until a screening worker
    (or an operator) releases it; nothing is held unless FORZA_COMMUNITY_HOLD=1."""
    return HELD if HOLD_NEW else LIVE


def photo_uploaded(fid, rec, path, signed):
    """forza_photos: the player's photo mode upload `fid` (its record and file) has arrived. The
    community photo's id, or None when it is not published."""
    xuid = rec["owner"]
    if not signed:
        log.info("community: %016X: file %d not published: the upload was not signed by the "
                 "player's key", xuid, fid)
        return None
    if designs.banned(xuid):
        log.info("community: %016X: file %d not published: banned", xuid, fid)
        return None
    gamertag = credit(xuid)
    if gamertag is None:
        log.info("community: %016X: file %d not published: no gamertag on record", xuid, fid)
        return None
    d = load()
    now = time.time()  # the caps first: a picture takes a moment to convert
    mine = recent(d["uploads"].get(hx(xuid), []), 86400, now)
    d["serverUploads"] = recent(d["serverUploads"], 3600, now)
    owned = sum(1 for p in d["photos"].values() if p["owner"] == xuid)
    why = ("the player's %d photos a day" % PER_DAY if len(mine) >= PER_DAY else
           "the player's %d photos" % PER_PLAYER if owned >= PER_PLAYER else
           "the server's %d photos an hour" % PER_HOUR if len(d["serverUploads"]) >= PER_HOUR
           else None)
    if why:
        log.info("community: %016X: file %d not published: %s", xuid, fid, why)
        return None
    try:
        with open(path, "rb") as src:
            clean, kind, w, h = picture(src.read())
    except (OSError, BadImage) as e:
        log.info("community: %016X: file %d not published: %s", xuid, fid, e)
        return None
    sha = hashlib.sha256(clean).hexdigest()
    if sha in d["deleted"]:
        log.info("community: %016X: file %d not published: an operator deleted this picture",
                 xuid, fid)
        return None
    for pid, p in d["photos"].items():
        if p["owner"] == xuid and p["sha256"] == sha:  # the same picture again: the newer file
            p["fileId"] = fid
            save()
            log.info("community: %016X: file %d is photo %s again", xuid, fid, pid)
            return pid
    try:
        limits.accept(len(clean), MAX_COPY)
    except ValueError as e:
        log.warning("community: %016X: file %d not published: %s", xuid, fid, e)
        return None
    pid = new_id(d["photos"])
    os.makedirs(os.path.join(DATA_DIR, "community"), exist_ok=True)
    tmp = image_path(pid, kind) + ".tmp"
    with open(tmp, "wb") as out:
        out.write(clean)
    durable.replace(tmp, image_path(pid, kind))
    status = hold_new(xuid, rec.get("name", ""))
    d["photos"][pid] = {
        "owner": xuid, "fileId": fid, "gamertag": gamertag, "title": clean_title(rec.get("name", "")),
        "type": kind, "takenAt": int(now), "created": now, "status": status,
        "approvedAt": int(now) if status == LIVE else None, "decidedAt": None, "note": "",
        "sha256": sha, "w": w, "h": h, "size": len(clean)}
    d["uploads"][hx(xuid)] = mine + [now]
    d["serverUploads"].append(now)
    save()
    log.info("community: %016X: file %d is photo %s (%dx%d, %d bytes, %s)", xuid, fid, pid, w, h,
             len(clean), status)
    return pid


def drop(pid, why):
    d = load()
    p = d["photos"].pop(pid, None)
    if p is None:
        return None
    try:
        os.remove(image_path(pid, p.get("type", "jpeg")))
    except OSError:
        pass
    log.info("community: photo %s (%016X) removed: %s", pid, p["owner"], why)
    return p


def photo_removed(fid):
    """forza_photos: the player's file `fid` is gone (overwritten by a newer upload): so is its
    community photo."""
    d = load()
    gone = [pid for pid, p in d["photos"].items() if p["fileId"] == fid]
    for pid in gone:
        drop(pid, "the player's file %d was removed" % fid)
    if gone:
        save()


def public(p, bans):
    return p["status"] == LIVE and p["owner"] not in bans


def feed_entry(pid, p):
    """A photo as the feed lists it: exactly these fields (the website's contract)."""
    return {"id": pid, "gamertag": p["gamertag"], "title": p["title"], "takenAt": p["takenAt"],
            "approvedAt": p["approvedAt"], "w": p["w"], "h": p["h"], "sha256": p["sha256"],
            "image": image_url(pid, p)}


def feed():
    """(body, ETag) of the feed, built again only when the photos or the bans changed."""
    bans = designs.banned_xuids()
    key = (_version[0], bans)
    if _feed["key"] != key:
        live = [(pid, p) for pid, p in load()["photos"].items() if public(p, bans)]
        live.sort(key=lambda e: (e[1]["approvedAt"] or 0, e[1]["created"], e[0]), reverse=True)
        doc = {"version": 1, "generated": int(time.time()),
               "photos": [feed_entry(pid, p) for pid, p in live[:FEED_MAX]]}
        body = json.dumps(doc, separators=(",", ":")).encode()
        _feed.update({"key": key, "body": body,
                      "etag": '"%s"' % hashlib.sha256(json.dumps(doc["photos"]).encode()).hexdigest()[:32]})
    return _feed["body"], _feed["etag"]


# ---- the screening queue and the operator's actions ----

def queue():
    """The held photos, oldest first: what a screening worker looks at."""
    held = [(pid, p) for pid, p in load()["photos"].items() if p["status"] == HELD]
    return sorted(held, key=lambda e: (e[1]["created"], e[0]))


# action: (the statuses it applies to, the status it gives)
ACTIONS = {"hide": ((LIVE, HELD), HIDDEN), "unhide": ((HIDDEN,), LIVE),
           "hold": ((LIVE,), HELD), "release": ((HELD,), LIVE)}


def act(pid, action, note=None):
    """An operator's or the screening worker's action on a photo (ACTIONS): its record, None for
    an unknown id, ValueError when the photo's status does not allow it (a screening worker
    cannot release a photo an operator hid in the meantime)."""
    p = load()["photos"].get(pid)
    if p is None:
        return None
    before, status = ACTIONS[action]
    if p["status"] not in before:
        raise ValueError("photo %s is %s: %s does not apply" % (pid, p["status"], action))
    p["status"] = status
    p["decidedAt"] = int(time.time())
    if status == LIVE and p["approvedAt"] is None:
        p["approvedAt"] = p["decidedAt"]  # its first time in the feed
    if note is not None:
        p["note"] = limits.clean_ascii_text(note, 200)
    save()
    log.info("community: photo %s (%016X) is %s", pid, p["owner"], status)
    return p


def delete(pid):
    """Removes the photo and its file; its picture is not published again."""
    d = load()
    p = drop(pid, "deleted by the operator")
    if p is None:
        return None
    d["deleted"] = (d["deleted"] + [p["sha256"]])[-DELETED_MAX:]
    save()
    return p


def admin_record(pid, p, bans):
    return {"id": pid, "owner": hx(p["owner"]), "gamertag": p["gamertag"], "title": p["title"],
            "status": p["status"], "banned": p["owner"] in bans, "public": public(p, bans),
            "takenAt": p["takenAt"], "approvedAt": p["approvedAt"], "decidedAt": p["decidedAt"],
            "note": p.get("note", ""), "w": p["w"], "h": p["h"], "size": p["size"],
            "sha256": p["sha256"], "fileId": p["fileId"],
            "image": ADMIN_PREFIX + image_url(pid, p)[len("/community"):]}


# ---- HTTP ----

def handles(path):
    p = urllib.parse.urlsplit(path).path
    return p.startswith("/community/") or p == ADMIN_PREFIX or p.startswith(ADMIN_PREFIX + "/")


def reply(status, obj, extra=()):
    return status, "application/json", json.dumps(obj, indent=1).encode(), tuple(extra)


SAFE = ("X-Content-Type-Options: nosniff", "Content-Security-Policy: default-src 'none'")


def image_reply(pid, p, headers, cache):
    kind = p.get("type", "jpeg")
    try:
        with open(image_path(pid, kind), "rb") as src:
            data = src.read()
    except OSError:
        return reply(404, {"error": "no such photo"})
    etag = '"%s"' % hashlib.sha256(data).hexdigest()
    extra = SAFE + ("ETag: " + etag, "Cache-Control: " + cache)
    if headers.get("if-none-match") == etag:
        return 304, TYPES[kind][1], b"", extra
    return 200, TYPES[kind][1], data, extra


def image_budget():
    now = time.monotonic()
    _images[:] = [t for t in _images if now - t < 60]
    if len(_images) >= IMAGES_PER_MINUTE:
        return False
    _images.append(now)
    return True


IMG_RE = re.compile(r"/img/([A-Z2-7]{%d})(\.jpg|\.png)" % ID_LEN)


def image_of(path):
    """The photo (id, record) an image path names (/img/<id>.jpg or .png, its own type), or
    None."""
    m = IMG_RE.fullmatch(path)
    p = load()["photos"].get(m.group(1)) if m else None
    if p is None or TYPES[p.get("type", "jpeg")][0] != m.group(2):
        return None
    return m.group(1), p


def handle(method, path, headers, body, admin):
    """(status, content type, body, extra headers) for /community/... and /admin/photos...;
    None for another path. admin(): the request may use the operator's routes."""
    if not handles(path):
        return None
    url = urllib.parse.urlsplit(path)
    if url.path.startswith(ADMIN_PREFIX):
        return admin_handle(method, url, headers, body, admin)
    if method != "GET":
        return reply(405, {"error": "GET only"})
    if url.path == "/community/gallery.json":
        data, etag = feed()
        extra = SAFE + ("ETag: " + etag, "Cache-Control: public, max-age=%d" % FEED_SECONDS)
        if headers.get("if-none-match") == etag:
            return 304, "application/json", b"", extra
        return 200, "application/json", data, extra
    if url.path.startswith("/community/img/"):
        found = image_of(url.path[len("/community"):])
        if found is None or not public(found[1], designs.banned_xuids()):
            return reply(404, {"error": "no such photo"})
        if not image_budget():
            return reply(429, {"error": "too many requests, try again in a minute"})
        return image_reply(*found, headers, "public, max-age=%d" % IMAGE_SECONDS)
    return reply(404, {"error": "not found"})


def admin_handle(method, url, headers, body, admin):
    if "origin" in headers:
        return reply(403, {"error": "use curl or a script, not a browser page"})
    if not admin():
        return reply(403, {"error": "forbidden: the X-Admin-Token header, from the LAN or an ssh "
                                    "tunnel (never through the tunnel or a proxy)"})
    rest = url.path[len(ADMIN_PREFIX):].strip("/")
    parts = rest.split("/") if rest else []
    bans = designs.banned_xuids()
    if method == "GET":
        if not parts:
            want = urllib.parse.parse_qs(url.query).get("status", [None])[0]
            photos = sorted(load()["photos"].items(), key=lambda e: (e[1]["created"], e[0]),
                            reverse=True)
            return reply(200, {"photos": [admin_record(pid, p, bans) for pid, p in photos
                                          if want is None or p["status"] == want],
                               "banned": sorted(hx(b) for b in bans)})
        if parts == ["queue"]:
            return reply(200, {"photos": [admin_record(pid, p, bans) for pid, p in queue()]})
        found = image_of("/" + rest)
        if found is not None:
            return image_reply(*found, headers, "no-store")
        return reply(404, {"error": "not found"})
    if method != "POST":
        return reply(405, {"error": "GET or POST"})
    if len(parts) == 2 and parts[0] in ("ban", "unban"):
        try:
            xuid = int(parts[1], 16)
        except ValueError:
            return reply(400, {"error": "a hex XUID"})
        return ban(xuid, parts[0] == "ban")
    if len(parts) != 2 or not ID_RE.fullmatch(parts[0]):
        return reply(404, {"error": "not found"})
    pid, action = parts
    p = load()["photos"].get(pid)
    if p is None:
        return reply(404, {"error": "no such photo"})
    if action == "delete":
        delete(pid)
        return reply(200, {"deleted": pid})
    if action == "ban":
        return ban(p["owner"], True)
    if action not in ACTIONS:
        return reply(404, {"error": "hide, unhide, hold, release, delete or ban"})
    note = None
    if body:
        try:
            note = json.loads(body).get("note")
        except (ValueError, AttributeError):
            return reply(400, {"error": "a JSON object, {\"note\": ...}"})
    try:
        act(pid, action, note if isinstance(note, str) else None)
    except ValueError as e:
        return reply(409, {"error": str(e)})
    return reply(200, admin_record(pid, p, designs.banned_xuids()))


def ban(xuid, on):
    try:
        changed = designs.set_ban(xuid, on)
    except ValueError as e:
        return reply(409, {"error": str(e)})
    _version[0] += 1
    log.warning("community: %016X %s by the operator", xuid, "banned" if on else "unbanned")
    return reply(200, {"xuid": hx(xuid), "banned": on, "changed": changed})
