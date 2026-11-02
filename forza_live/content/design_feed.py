"""Forza Live: the design storefront's paint jobs and vinyl groups on the website, once their
preview has passed the automatic screening (screening/: local models, no hosted service).

Designs are listed in the game as soon as they are uploaded (forza_designs.py); nothing here
changes that. The website shows a design only after its preview was screened, so each design
has a web state, kept with it in DATA_DIR/designs.json:

  pending   new, and every design there before this module: waiting for the screening worker.
            In the game as usual, not on the website.
  clear     released by the screening worker or the operator: on the website while the design
            is listed in the game (not hidden by reports, its owner not banned).
  held      left for the operator: the worker was not sure, or the preview cannot be converted.
            In the game as usual, not on the website.
  hidden    a confident screening hit, or the operator: hidden in the game too (listed to its
            owner only, as three reports do; forza_designs.public) and not on the website.

The website gets a design's public id (random, never the storefront's file id), type, share
code, title, description, the creator's gamertag (forza_community.credit: none on record, not
listed), the car's name for a paint job, counts and the preview as a PNG. Never a XUID, never
the payload (it holds the creator's XUID).

Previews: the design's cxds texture converted by forza_cxds.py (gamma 1/2.2, an RGB PNG with
IHDR, IDAT and IEND only), kept as DATA_DIR/designs/<file id>.png beside the design. Converted
when the design is first in the queue, the feed or an image route (a few milliseconds each, at
most QUEUE_MAX in one queue answer); a preview that does not convert, or whose PNG the website
would refuse (SITE_MIN_SIDE to forza_cxds.MAX_SIDE pixels a side, at most SITE_MAX_PNG bytes),
holds the design.

Car names: a paint job's ModelId is the car's id in the game's database (Data_Car). Its name,
"<year> <make> <model>", is the one the leaderboards show: the "cars" of the operator's
DATA_DIR/leaderboard_names.json (forza_leaderboards.names; written from the operator's own copy
of the game by the game repository's tools/extract_map_points.py --names, which reads the
--game-db's Data_Car and List_CarMake and the string tables they point to). The database alone
holds only references into the game's compressed string tables, so without that file the car
is null.

Public routes, on the title services port (no side effects, never a XUID):

  GET /community/designs.json          {"version":1,"generated":<unix>,"designs":[{"id","type",
                                       "code","title","description","gamertag","car","layers",
                                       "downloads","rating","createdAt","preview","w","h",
                                       "sha256"}]}: designs listed in the game and clear, newest
                                       first, at most FEED_MAX; ETag, Cache-Control
  GET /community/design-img/<id>.png   a design of the feed: nosniff, CSP default-src 'none',
                                       cached for IMAGE_SECONDS; at most IMAGES_PER_MINUTE a minute

Operator routes, with forza_community's rules (lsp.is_local_admin: X-Admin-Token in its header,
never through the tunnel or a reverse proxy; requests with an Origin header are refused):

  GET  /admin/designs[?state=pending|clear|held|hidden]  every design, with its owner's XUID
  GET  /admin/designs/queue                    pending designs, oldest first, at most QUEUE_MAX
                                               (the screening worker's; no XUID)
  GET  /admin/designs/img/<id>.png             a design's preview, whatever its state
  POST /admin/designs/<id>/release | hold | hide | unhide | delete | ban
       optional body {"note": "...", "expect": "<state>"}: expect, the action applies only to
       a design in that state (409 otherwise: the worker's actions expect pending)
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse

import forza_community as community
import forza_cxds as cxds
import forza_designs as designs
import forza_durable as durable
import forza_leaderboards as leaderboards
import forza_limits as limits

log = logging.getLogger("forza-live")

PENDING, CLEAR, HELD, HIDDEN = designs.PENDING, designs.CLEAR, designs.HELD, designs.HIDDEN
STATES = (PENDING, CLEAR, HELD, HIDDEN)
FEED_MAX = 500            # designs in the feed (the newest)
QUEUE_MAX = 50            # designs in one queue answer (the oldest)
FEED_SECONDS = 60         # the feed's Cache-Control max-age; it is built again at most this often
IMAGE_SECONDS = 86400     # an image's
IMAGES_PER_MINUTE = 600   # design images the server answers a minute (the website fetches each once)
SITE_MIN_SIDE = 32        # pixels a side of a preview the website takes (at most cxds.MAX_SIDE)
SITE_MAX_PNG = 1 << 20    # bytes of a preview the website takes
TYPES = {"livery": "livery", "layergroup": "vinylgroup"}
ADMIN_PREFIX = "/admin/designs"
FEED_PATH = "/community/designs.json"
IMG_PREFIX = "/community/design-img/"
ID_RE = re.compile("[A-Z2-7]{%d}" % designs.WEB_ID_LEN)
IMG_RE = re.compile(r"([A-Z2-7]{%d})\.png" % designs.WEB_ID_LEN)

# action: (the web states it applies to, the web state it gives); unhide also lists a design
# that three reports hid
ACTIONS = {"release": ((PENDING, HELD), CLEAR), "hold": ((PENDING, CLEAR), HELD),
           "hide": ((PENDING, CLEAR, HELD), HIDDEN), "unhide": ((HIDDEN,), CLEAR)}

_feed = {"key": None, "body": b"", "etag": ""}
_ids = {"version": None, "map": {}}
_images = []  # times of the design image requests of the last minute


def configure():
    """Forgets the caches (forza_designs.configure names the data folder) and reads the
    designs, so that designs from before this module get their web state at start."""
    _feed.update({"key": None, "body": b"", "etag": ""})
    _ids.update({"version": None, "map": {}})
    _images.clear()
    designs.load()


def png_path(fid):
    return designs.file_path(fid, ".png")


def by_web_id(wid):
    """(file id, record) of a design's public id, or None."""
    if _ids["version"] != designs._version[0]:
        _ids["map"] = {f.get("webId"): int(i) for i, f in designs.load()["files"].items()}
        _ids["version"] = designs._version[0]
    fid = _ids["map"].get(wid)
    f = designs.load()["files"].get(str(fid)) if fid is not None else None
    return (fid, f) if f is not None and f.get("webId") == wid else None


# ---- the preview as a PNG ----

def ensure_png(fid, f):
    """The preview's PNG ({"w", "h", "sha256", "size"}), converted and kept beside the design
    the first time; None when it cannot be (the design is then held, with the reason as its
    note, if it was pending or clear). Saves the index when it changed."""
    meta = f.get("png")
    if meta and os.path.isfile(png_path(fid)) and os.path.getsize(png_path(fid)) == meta["size"]:
        return meta
    try:
        with open(designs.file_path(fid, ".prev"), "rb") as src:
            data = src.read(designs.MAX_PREVIEW + 1)
        png, w, h = cxds.to_png(data)
        if min(w, h) < SITE_MIN_SIDE:
            raise cxds.BadPreview("size %dx%d (the website takes %d to %d pixels a side)"
                                  % (w, h, SITE_MIN_SIDE, cxds.MAX_SIDE))
        if len(png) > SITE_MAX_PNG:
            raise cxds.BadPreview("a PNG of %d bytes (the website takes %d at most)"
                                  % (len(png), SITE_MAX_PNG))
    except (OSError, cxds.BadPreview) as e:
        f["png"] = None
        if f.get("web") in (PENDING, CLEAR):
            set_state(fid, f, HELD, "preview: %s" % e)
        log.info("designs: %s %d (%s): preview not converted: %s", f["kind"], fid,
                 f.get("webId"), e)
        designs.save()
        return None
    tmp = png_path(fid) + ".tmp"
    with open(tmp, "wb") as out:
        out.write(png)
    durable.replace(tmp, png_path(fid))
    f["png"] = {"w": w, "h": h, "sha256": hashlib.sha256(png).hexdigest(), "size": len(png)}
    designs.save()
    return f["png"]


# ---- car names ----

def car_name(f):
    """The paint job's car, "<year> <make> <model>" as the leaderboards name it, or None (a
    vinyl group, or no name for that car in leaderboard_names.json)."""
    if f["kind"] != "livery":
        return None
    cars = leaderboards.names().get("cars")
    name = cars.get(str(f["meta"].get("modelId"))) if isinstance(cars, dict) else None
    if not isinstance(name, str):
        return None
    return leaderboards.clean_label(name, "") or None


# ---- the feed ----

def set_state(fid, f, state, note=None):
    f["web"] = state
    f["webAt"] = int(time.time())
    if note is not None:
        f["webNote"] = limits.clean_ascii_text(note, 200)
    log.info("designs: %s %d (%s, %016X) is %s on the website", f["kind"], fid, f["webId"],
             f["owner"], state)


def on_site(f, bans, credits):
    """Shown on the website: an upload that finished, listed in the game, clear, with a
    converted preview and a gamertag to credit. credits: owner -> credit, filled as needed."""
    if not (f["done"] and f.get("web") == CLEAR and f.get("png") and designs.public(f, bans)):
        return False
    if f["owner"] not in credits:
        credits[f["owner"]] = community.credit(f["owner"])
    return credits[f["owner"]] is not None


def feed_entry(f, gamertag):
    """A design as the feed lists it: exactly these fields (the website's contract)."""
    m, png = f["meta"], f["png"]
    name = car_name(f)
    return {"id": f["webId"], "type": TYPES[f["kind"]], "code": f["code"],
            "title": limits.clean_ascii_text(m["title"], 16),
            "description": limits.clean_ascii_text(m["description"], 256), "gamertag": gamertag,
            "car": {"name": name} if name else None,
            "layers": m.get("numLayers") if f["kind"] == "layergroup" else None,
            "downloads": len(f["downloaders"]),
            "rating": {"avg": round(designs.rating(f), 2), "count": len(f["ratings"])},
            "createdAt": int(f["created"]), "preview": IMG_PREFIX + f["webId"] + ".png",
            "w": png["w"], "h": png["h"], "sha256": png["sha256"]}


def feed():
    """(body, ETag) of the feed, built again when the designs or the bans changed, and at
    most every FEED_SECONDS otherwise (gamertags and car names change without either)."""
    bans = designs.banned_xuids()
    key = (designs._version[0], bans, int(time.time() // FEED_SECONDS))
    if _feed["key"] != key:
        credits = {}
        for fid, f in designs.files(everybody=True):
            if f.get("web") == CLEAR and not f.get("png"):
                ensure_png(fid, f)  # released before it was ever converted
        listed = [(fid, f) for fid, f in designs.files(everybody=True)
                  if on_site(f, bans, credits)][:FEED_MAX]
        doc = {"version": 1, "generated": int(time.time()),
               "designs": [feed_entry(f, credits[f["owner"]]) for _, f in listed]}
        body = json.dumps(doc, separators=(",", ":")).encode()
        etag = '"%s"' % hashlib.sha256(json.dumps(doc["designs"]).encode()).hexdigest()[:32]
        _feed.update({"key": (designs._version[0], bans, key[2]), "body": body, "etag": etag})
    return _feed["body"], _feed["etag"]


# ---- the screening queue and the operator's actions ----

def queue():
    """(pending designs, oldest first, at most QUEUE_MAX, each with its PNG; how many are
    pending in all). A preview that does not convert holds its design (ensure_png)."""
    pending = sorted(((int(i), f) for i, f in designs.load()["files"].items()
                      if f["done"] and f.get("web") == PENDING),
                     key=lambda e: (e[1]["created"], e[0]))
    out = [(fid, f) for fid, f in pending[:QUEUE_MAX] if ensure_png(fid, f) is not None]
    return out, sum(1 for _, f in pending if f.get("web") == PENDING)


def act(fid, f, action, note=None, expect=None):
    """An operator's or the screening worker's action on a design (ACTIONS). ValueError when
    the design's state does not allow it: a design the operator hid is not released, and with
    `expect` nothing applies to a design in another web state."""
    if expect is not None and f.get("web") != expect:
        raise ValueError("design %s is %s, not %s" % (f["webId"], f.get("web"), expect))
    before, state = ACTIONS[action]
    if action == "unhide" and f["status"] == designs.HIDDEN:  # three reports hid it
        f["status"] = designs.LIVE
        f["reporters"] = []
        log.info("designs: %s %d (%s, %016X) listed again after its reports", f["kind"], fid,
                 f["webId"], f["owner"])
        if f.get("web") != HIDDEN:
            if note is not None:
                f["webNote"] = limits.clean_ascii_text(note, 200)
            designs.save()
            return f
    elif f.get("web") not in before:
        raise ValueError("design %s is %s: %s does not apply" % (f["webId"], f.get("web"), action))
    set_state(fid, f, state, note)
    designs.save()
    return f


def admin_record(fid, f, bans, owner=False):
    """A design as the operator's routes list it; owner: with its owner's XUID (not in the
    queue)."""
    m, png = f["meta"], f.get("png") or {}
    r = {"id": f["webId"], "type": TYPES[f["kind"]], "code": f["code"],
         "title": limits.clean_ascii_text(m["title"], 16),
         "description": limits.clean_ascii_text(m["description"], 256),
         "gamertag": community.credit(f["owner"]) or "", "state": f.get("web"),
         "note": f.get("webNote", ""), "decidedAt": f.get("webAt"),
         "createdAt": int(f["created"]), "w": png.get("w"), "h": png.get("h"),
         "sha256": png.get("sha256", ""), "preview": ADMIN_PREFIX + "/img/%s.png" % f["webId"]}
    if owner:
        r.update(owner=designs.hx(f["owner"]), fileId=fid, status=f["status"],
                 listed=f["done"] and designs.public(f, bans), banned=f["owner"] in bans,
                 reports=len(f["reporters"]), downloads=len(f["downloaders"]),
                 car=car_name(f))
    return r


# ---- HTTP ----

def handles(path):
    p = urllib.parse.urlsplit(path).path
    return (p == FEED_PATH or p.startswith(IMG_PREFIX) or p == ADMIN_PREFIX
            or p.startswith(ADMIN_PREFIX + "/"))


def image_budget():
    now = time.monotonic()
    _images[:] = [t for t in _images if now - t < 60]
    if len(_images) >= IMAGES_PER_MINUTE:
        return False
    _images.append(now)
    return True


def image_reply(fid, f, headers, cache):
    if ensure_png(fid, f) is None:
        return community.reply(404, {"error": "no such design"})
    try:
        with open(png_path(fid), "rb") as src:
            data = src.read()
    except OSError:
        return community.reply(404, {"error": "no such design"})
    etag = '"%s"' % hashlib.sha256(data).hexdigest()
    extra = community.SAFE + ("ETag: " + etag, "Cache-Control: " + cache)
    if headers.get("if-none-match") == etag:
        return 304, "image/png", b"", extra
    return 200, "image/png", data, extra


def image_of(name):
    """(file id, record) of an image name, <id>.png; None."""
    m = IMG_RE.fullmatch(name)
    return by_web_id(m.group(1)) if m else None


def handle(method, path, headers, body, admin):
    """(status, content type, body, extra headers) for the routes above; None for another
    path. admin(): the request may use the operator's routes."""
    if not handles(path):
        return None
    url = urllib.parse.urlsplit(path)
    if url.path.startswith(ADMIN_PREFIX):
        return admin_handle(method, url, headers, body, admin)
    if method != "GET":
        return community.reply(405, {"error": "GET only"})
    if url.path == FEED_PATH:
        data, etag = feed()
        extra = community.SAFE + ("ETag: " + etag, "Cache-Control: public, max-age=%d" % FEED_SECONDS)
        if headers.get("if-none-match") == etag:
            return 304, "application/json", b"", extra
        return 200, "application/json", data, extra
    found = image_of(url.path[len(IMG_PREFIX):])
    if found is not None and found[1].get("web") == CLEAR and not found[1].get("png"):
        ensure_png(*found)  # released before it was ever converted
    if found is None or not on_site(found[1], designs.banned_xuids(), {}):
        return community.reply(404, {"error": "no such design"})
    if not image_budget():
        return community.reply(429, {"error": "too many requests, try again in a minute"})
    return image_reply(*found, headers, "public, max-age=%d" % IMAGE_SECONDS)


def admin_handle(method, url, headers, body, admin):
    if "origin" in headers:
        return community.reply(403, {"error": "use curl or a script, not a browser page"})
    if not admin():
        return community.reply(403, {"error": "forbidden: the X-Admin-Token header, from the LAN "
                                              "or an ssh tunnel (never through the tunnel or a "
                                              "proxy)"})
    rest = url.path[len(ADMIN_PREFIX):].strip("/")
    parts = rest.split("/") if rest else []
    bans = designs.banned_xuids()
    if method == "GET":
        if not parts:
            want = urllib.parse.parse_qs(url.query).get("state", [None])[0]
            if want is not None and want not in STATES:
                return community.reply(400, {"error": "state: " + ", ".join(STATES)})
            found = designs.files(done=True)
            return community.reply(200, {"designs": [admin_record(fid, f, bans, owner=True)
                                                     for fid, f in found
                                                     if want is None or f.get("web") == want]})
        if parts == ["queue"]:
            pending, total = queue()
            return community.reply(200, {"designs": [admin_record(fid, f, bans) for fid, f in pending],
                                         "pending": total})
        if len(parts) == 2 and parts[0] == "img":
            found = image_of(parts[1])
            if found is not None and found[1]["done"]:
                return image_reply(*found, headers, "no-store")
        return community.reply(404, {"error": "not found"})
    if method != "POST":
        return community.reply(405, {"error": "GET or POST"})
    if len(parts) != 2 or not ID_RE.fullmatch(parts[0]):
        return community.reply(404, {"error": "not found"})
    wid, action = parts
    found = by_web_id(wid)
    if found is None or not found[1]["done"]:
        return community.reply(404, {"error": "no such design"})
    fid, f = found
    if action == "delete":
        designs.remove(fid)
        designs.save()
        log.info("designs: %s %d (%s, %016X) deleted by the operator", f["kind"], fid, wid,
                 f["owner"])
        return community.reply(200, {"deleted": wid})
    if action == "ban":
        return community.ban(f["owner"], True)
    if action not in ACTIONS:
        return community.reply(404, {"error": "release, hold, hide, unhide, delete or ban"})
    note = expect = None
    if body:
        try:
            doc = json.loads(body)
            note, expect = doc.get("note"), doc.get("expect")
        except (ValueError, AttributeError):
            return community.reply(400, {"error": "a JSON object, {\"note\": ..., \"expect\": ...}"})
    try:
        act(fid, f, action, note if isinstance(note, str) else None,
            expect if isinstance(expect, str) else None)
    except ValueError as e:
        return community.reply(409, {"error": str(e)})
    return community.reply(200, admin_record(fid, f, designs.banned_xuids(), owner=True))
