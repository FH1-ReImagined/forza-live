"""Forza Live: the design storefront (Paint Shop > DESIGN STOREFRONT): paint jobs (liveries) and
vinyl groups that players share in the game, and other players browse, buy and apply.

What the game does (Title Update 4, a live game against this server, 2026-10-30):

- Upload. LOAD DESIGN > a paint job of the car the player is in > UPLOAD TO STOREFRONT sends
  Auction.UploadAuctionTextures (the livery as a 1024x1024 JPEG texture, forza_market keeps it),
  then StorefrontLivery.Create: fileHeader (Title, Description, Price, Quantity, SubmissionTime,
  Type 2, KeywordOne/Two), liveryDesc (MakeId, ModelId, TextureId, ForStorefrontUse), payloadSize
  and previewData; then Xls.Content.UploadContent with the payload. MY VINYL GROUPS > UPLOAD TO
  STOREFRONT sends StorefrontLayerGroup.Create (Type 1, layerGroupDesc NumLayers) the same way.
  A paint job's payload is the plaintext "crlv" design, a vinyl group's the plaintext "lvyg"
  group (neither encrypted nor compressed); both carry the creator's online XUID. The preview is
  a "cxds" texture (the paint job's 288x160, the vinyl group's 256x128 thumbnail), the bytes the
  game keeps as the design's thumb.xdc. Forza Horizon has no tune or replay sharing: tuning
  (page 0) and replays (page 4) would show as unnamed tabs, so the page list has 1 and 2 only.
- Browse. Entering the storefront asks for the featured, most downloaded and top rated designs
  (10 each, paint jobs and vinyl groups together). DESIGNS FOR MY CAR is SearchLiveryFiles with
  the car's make and model; SEARCH ALL DESIGNS has a gamertag field, SEARCH ALL VINYL GROUPS a
  free-text field (ManualSearchKeywords); a search is followed by GetFileHeavyWeightData for the
  ids found. A file's Ordinal is the car's ModelId for a paint job (0: CONTENT NOT AVAILABLE) and
  the number of layers for a vinyl group. An invalid SubmissionTime fails the whole answer
  ("Failed to retrieve featured files").
- Buy. The detail screen downloads the payload (Content.DownloadContent) to show the design on
  the player's car; BUY is Purchase(fileId, price, maxPayloadSize, maxPreviewSize), the sizes of
  the file's record. The game stores the design encrypted with the buyer's own profile keys,
  marked locked (it can be applied, not uploaded again) and with the creator's name.

Rules of this server:

- Uploads only from a game that signed the request with the player's own key (forza_auth,
  configure(proven=)); anything else gets a clean failure, which the game reports as an upload
  that did not succeed. Downloads, lists and searches are open.
- At most SLOTS files per player and kind (the storefront's SLOTS AVAILABLE), PER_DAY uploads a
  day per player, PER_HOUR uploads an hour on the whole server. A banned XUID
  (DATA_DIR/banned_xuids.json, a list of hex XUIDs the operator edits, read again when it
  changes; forza_community's /admin/photos/ban writes it too) uploads nothing, and its files are
  listed to nobody but itself. The payload must have the kind's magic and at most MAX_PAYLOAD
  bytes, the preview must be a cxds texture of at most MAX_PREVIEW bytes.
- Free: Price is stored and answered as 0, Quantity as -1 (no limit), whatever the player chose.
- A rating (1-5) and a download count once per player (a later rating replaces the earlier one),
  never the owner's. REPORT_HIDE distinct players reporting a file hide it at once. Only the
  owner removes a file or changes its description. Ratings, reports, downloads and views count
  only from callers proven by their key (forza_live_lsp.caller_trusted), whom no one else can
  impersonate.
- A file is "live" (listed), "held" (seen by its owner only: the hook for screening uploads
  before they are listed, hold_new(); nothing is held for now) or "hidden" (reported).
- A file also has a web state (forza_design_feed.py, the website's designs): "pending" (new,
  and every file there before the website's designs, until the screening worker sees its
  preview), "clear" (on the website), "held" (left for the operator: not on the website) or
  "hidden" (a confident screening hit or the operator: hidden in the game too, listed to its
  owner only, as three reports do). Its public id there (webId) is random, never the file id.
- Each file has a share code (CODE_LEN characters, no 0/O/1/I) for the website: typed in a
  search's gamertag or text field it finds that file.
- Nothing here is served outside the game's protocol: payloads and XUIDs never leave it (the
  website gets the preview, title, description, share code and counts: forza_design_feed.py).

Files: DATA_DIR/designs.json (the index, forza_durable), DATA_DIR/designs/<id>.bin (payload),
<id>.prev (preview) and <id>.png (the preview as the website shows it, forza_design_feed.py);
counted in the server's disk quota (forza_limits). Design ids start at
FIRST_ID so that they never meet photo ids (forza_photos): the storefront calls name a file by
its id only, and the calls shared with photos go to forza_photos for the ids below FIRST_ID.
"""

import json
import logging
import os
import secrets
import time

import forza_clubs as clubs
import forza_durable as durable
import forza_limits as limits
import forza_photos as photos
import forza_xtsw as x
import forza_xtsw_all as A

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
FIRST_ID = 100_000_000   # design ids; photo ids stay below (forza_photos)
SLOTS = 25               # files per player and kind (the storefront page's slots)
PER_DAY = 10             # uploads a day per player
PER_HOUR = 120           # uploads an hour on the whole server
MAX_PAYLOAD = 1 << 20    # a design (a 22-layer paint job is about 1.2 KB)
MAX_PREVIEW = 153_600    # the record's PreviewPayload field
MAX_LIST = 50            # files in one list answer (each carries its preview)
MAX_CHUNK = 1 << 20      # bytes handed back by one DownloadContent
STALE_UPLOAD = 3600      # an upload that never finished frees its slot after this
REPORT_HIDE = 3          # distinct reporters that hide a file
CODE_LEN = 6
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
LIVE, HELD, HIDDEN = "live", "held", "hidden"
PENDING, CLEAR = "pending", "clear"  # web states, with HELD and HIDDEN (forza_design_feed.py)
WEB_ID_LEN = 12
WEB_ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

# kind: (Create class, params/results type prefix, desc field, storefront page, header Type, magic)
KINDS = {
    "livery": ("StorefrontLivery", "ForzaStorefrontLiveryCreate", "liveryDesc", 1, 2, b"crlv"),
    "layergroup": ("StorefrontLayerGroup", "ForzaStorefrontLayerGroupCreate", "layerGroupDesc", 2,
                   1, b"lvyg"),
}
PAGES = {spec[3]: kind for kind, spec in KINDS.items()}

INDEX = {"nextId": FIRST_ID, "files": {}, "uploads": {}, "serverUploads": []}
_loaded = [False]
_version = [0]  # counts the saves: forza_design_feed builds its feed again after one
_bans = {"mtime": None, "xuids": frozenset()}
# Who is calling (forza_live_lsp): proven(), the request is signed by the caller's own key;
# trusted(), signed or carrying the caller's raw friends key (games before signed requests).
_caller = {"proven": lambda: False, "trusted": lambda: False}


def configure(data_dir, proven=None, trusted=None):
    global DATA_DIR
    DATA_DIR = data_dir
    INDEX.clear()
    INDEX.update({"nextId": FIRST_ID, "files": {}, "uploads": {}, "serverUploads": []})
    _loaded[0] = False
    _version[0] += 1
    _bans.update({"mtime": None, "xuids": frozenset()})
    _caller["proven"] = proven or (lambda: False)
    _caller["trusted"] = trusted or (lambda: False)


def index_path():
    return os.path.join(DATA_DIR, "designs.json")


def file_path(fid, suffix):
    return os.path.join(DATA_DIR, "designs", "%d%s" % (fid, suffix))


def load():
    if not _loaded[0]:
        _loaded[0] = True
        try:
            INDEX.update(durable.load(index_path()))
        except (OSError, ValueError):
            pass
        if add_web_state(INDEX["files"]):
            try:
                save()
            except OSError as e:
                log.warning("designs: web states not saved: %s", e)
    return INDEX


def save():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = index_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(INDEX, f, indent=1)
    durable.replace(tmp, index_path())
    _version[0] += 1


def new_web_id(files):
    used = {f.get("webId") for f in files.values()}
    while True:
        wid = "".join(secrets.choice(WEB_ID_CHARS) for _ in range(WEB_ID_LEN))
        if wid not in used:
            return wid


def web_fields(files):
    """A new file's web state: pending, with a public id of its own."""
    return {"webId": new_web_id(files), "web": PENDING, "webNote": "", "webAt": None,
            "png": None}


def add_web_state(files):
    """Files from before the website's designs get a web state: pending, like a new upload.
    True when any did."""
    added = False
    for f in files.values():
        if "web" not in f:
            f.update(web_fields(files))
            added = True
    return added


def hx(xuid):
    return "%016X" % xuid


def is_design(fid):
    return fid >= FIRST_ID


def banned_xuids():
    """The operator's ban list, DATA_DIR/banned_xuids.json (a JSON list of hex XUIDs), read again
    when it changes."""
    path = os.path.join(DATA_DIR, "banned_xuids.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime != _bans["mtime"]:
        xuids = set()
        if mtime is not None:
            try:
                with open(path) as f:
                    xuids = {int(str(v), 16) for v in json.load(f)}
            except (OSError, ValueError, TypeError):
                log.warning("designs: %s is not a JSON list of hex XUIDs: no bans", path)
        _bans.update({"mtime": mtime, "xuids": frozenset(xuids)})
    return _bans["xuids"]


def banned(xuid):
    return xuid in banned_xuids()


def set_ban(xuid, ban=True):
    """Puts the XUID on the ban list (or takes it off), keeping the rest of the file as the
    operator wrote it. True when the list changed; ValueError when the file is not a JSON list
    (it is left alone)."""
    path = os.path.join(DATA_DIR, "banned_xuids.json")
    try:
        with open(path) as f:
            entries = json.load(f)
    except FileNotFoundError:
        entries = []
    except (OSError, ValueError) as e:
        raise ValueError("%s cannot be read (%s): fix it by hand" % (path, e)) from None
    if not isinstance(entries, list):
        raise ValueError("%s is not a JSON list: fix it by hand" % path)

    def same(v):
        try:
            return int(str(v), 16) == xuid
        except ValueError:
            return False
    if any(same(v) for v in entries) == ban:
        return False
    kept = [v for v in entries if not same(v)] + ([hx(xuid)] if ban else [])
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(kept, f, indent=1)
    durable.replace(tmp, path)
    _bans["mtime"] = None  # read again at once, whatever the file system's clock resolution
    return True


def hold_new(xuid, kind, meta):
    """The state a new upload starts in: HELD keeps it from everyone but its owner until it is
    screened (a later step: an operator, a filter, or a player's first uploads). Nothing is held
    for now."""
    return LIVE


def new_code(files):
    used = {f.get("code") for f in files.values()}
    while True:
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN))
        if code not in used:
            return code


def remove(fid):
    load()["files"].pop(str(fid), None)
    for suffix in (".bin", ".prev", ".png"):
        try:
            os.remove(file_path(fid, suffix))
        except OSError:
            pass


def drop_stale():
    d = load()
    now = time.time()
    stale = [int(i) for i, f in d["files"].items()
             if not f["done"] and now - f["created"] > STALE_UPLOAD]
    for fid in stale:
        remove(fid)
    if stale:
        save()


def public(f, bans=None):
    """Listed for everybody: live, not hidden by the screening or the operator (web state
    hidden), and not a banned player's."""
    return (f["status"] == LIVE and f.get("web") != HIDDEN
            and f["owner"] not in (banned_xuids() if bans is None else bans))


def files(kind=None, owner=None, viewer=None, done=True, everybody=False):
    """(id, record) newest first: of one kind and owner when given; the ones a viewer may see
    (public, or the viewer's own) when viewer is given, the public ones with everybody;
    finished uploads only unless done=False."""
    out = []
    bans = banned_xuids()
    for i, f in load()["files"].items():
        if kind is not None and f["kind"] != kind:
            continue
        if owner is not None and f["owner"] != owner:
            continue
        if done and not f["done"]:
            continue
        if viewer is not None and f["owner"] != viewer and not public(f, bans):
            continue
        if everybody and not public(f, bans):
            continue
        out.append((int(i), f))
    return sorted(out, key=lambda e: (e[1]["created"], e[0]), reverse=True)


def visible(fid, viewer):
    f = load()["files"].get(str(fid))
    if f is None or not f["done"] or (f["owner"] != viewer and not public(f)):
        return None
    return f


def valid_preview(data):
    """A cxds texture (version 2, deflated, the length it says) of at most MAX_PREVIEW bytes."""
    if not 20 < len(data) <= MAX_PREVIEW or data[:4] != b"cxds":
        return False
    version, _, packed = int.from_bytes(data[4:8], "big"), data[8:12], int.from_bytes(data[12:16], "big")
    return version == 2 and packed == len(data) - 20


def recent(times, seconds, now):
    return [t for t in times if now - t < seconds]


# ---- uploads ----

def creator(kind):
    cls, base, descname, _, ftype, _ = KINDS[kind]
    params_t, results_t = getattr(A, base + "_Params"), getattr(A, base + "_Results")

    def create(xuid, body):
        p, _ = x.decode(params_t, body)
        if not _caller["proven"]():
            log.info("designs: %016X: %s upload refused: the request is not signed by the "
                     "player's key", xuid, kind)
            return x.failure(5, "uploads need a game that signs its requests")
        if banned(xuid):
            log.info("designs: %016X: %s upload refused: banned", xuid, kind)
            return x.failure(6, "uploads are not allowed for this player")
        h, desc = p["fileHeader"], p[descname]["base"]
        size = p["payloadSize"] or h["PayloadSize"]
        if not 4 <= size <= MAX_PAYLOAD:
            return x.failure(4, "design of %d bytes refused" % size)
        if not valid_preview(p["previewData"]):
            return x.failure(4, "preview refused")
        drop_stale()
        d = load()
        now = time.time()
        mine = recent(d["uploads"].get(hx(xuid), []), 86400, now)
        d["serverUploads"] = recent(d["serverUploads"], 3600, now)
        if len(files(kind, xuid, done=False)) >= SLOTS:
            return x.failure(3, "storefront full")
        if len(mine) >= PER_DAY:
            log.info("designs: %016X: daily upload limit (%d) reached", xuid, PER_DAY)
            return x.failure(7, "too many uploads today")
        if len(d["serverUploads"]) >= PER_HOUR:
            log.warning("designs: server-wide upload limit (%d an hour) reached", PER_HOUR)
            return x.failure(7, "the server takes no more uploads this hour")
        limits.accept(size + len(p["previewData"]))
        meta = {"title": limits.clean_ascii_text(h["Title"], 16, "DESIGN"),
                "description": limits.clean_ascii_text(h["Description"], 256),
                "keywordOne": h["KeywordOne"], "keywordTwo": h["KeywordTwo"]}
        if kind == "livery":
            meta.update(makeId=desc["MakeId"], modelId=desc["ModelId"], textureId=desc["TextureId"])
        else:
            meta.update(numLayers=desc["NumLayers"])
        fid = d["nextId"]
        d["nextId"] += 1
        os.makedirs(os.path.join(DATA_DIR, "designs"), exist_ok=True)
        with open(file_path(fid, ".bin"), "wb") as f:
            f.truncate(size)
        with open(file_path(fid, ".prev"), "wb") as f:
            f.write(p["previewData"])
        d["files"][str(fid)] = {
            "kind": kind, "type": ftype, "owner": xuid, "gamertag": clubs.gamertag(xuid),
            "size": size, "got": 0, "done": False, "created": now, "code": new_code(d["files"]),
            "status": hold_new(xuid, kind, meta), "meta": meta, "downloaders": [],
            "ratings": {}, "reporters": [], "views": 0, **web_fields(d["files"])}
        d["uploads"][hx(xuid)] = mine + [now]
        d["serverUploads"].append(now)
        save()
        log.info("designs: %016X: %s %d (%d bytes, preview %d bytes)", xuid, kind, fid, size,
                 len(p["previewData"]))
        return x.reply(results_t, {"contentFileId": fid})
    return create


def upload_content(xuid, body):
    p, _ = x.decode(A.ContentUploadContent_Params, body)
    if not is_design(p["contentId"]):
        return photos.upload_content(xuid, body)
    f = load()["files"].get(str(p["contentId"]))
    if f is None or f["owner"] != xuid or f["done"]:
        return x.failure(1, "no such file")
    data, offset = p["uploadData"], p["payloadOffset"]
    if offset > f["got"] or offset + len(data) > f["size"]:
        return x.failure(2, "bad range")
    if offset == 0 and data[:4] != KINDS[f["kind"]][5]:
        log.info("designs: %016X: file %d is not a %s: refused", xuid, p["contentId"], f["kind"])
        remove(p["contentId"])
        save()
        return x.failure(4, "not a design")
    if data:
        with open(file_path(p["contentId"], ".bin"), "r+b") as out:
            out.seek(offset)
            out.write(data)
    f["got"] = max(f["got"], offset + len(data))
    if f["got"] >= f["size"]:
        f["done"] = True
        log.info("designs: %016X: %s %d uploaded (%d bytes)", xuid, f["kind"], p["contentId"],
                 f["size"])
    save()
    return x.reply(A.ContentUploadContent_Results, {"payloadUploadedSize": f["got"]})


def download_content(xuid, body):
    p, _ = x.decode(A.ContentDownloadContent_Params, body)
    if not is_design(p["contentId"]):
        return photos.download_content(xuid, body)
    f = visible(p["contentId"], xuid)
    if f is None:
        return x.reply(A.ContentDownloadContent_Results, {})  # as for any unknown content id
    offset, want = p["payloadOffset"], min(p["requestedSize"], MAX_CHUNK)
    with open(file_path(p["contentId"], ".bin"), "rb") as src:
        src.seek(offset)
        data = src.read(want)
    return x.reply(A.ContentDownloadContent_Results, {
        "isLastBuffer": offset + len(data) >= f["size"], "downloadData": data,
        "totalSize": f["size"]})


# ---- records ----

def rating(f):
    stars = list(f["ratings"].values())
    return sum(stars) / len(stars) if stars else 0.0


def ordinal(f):
    m = f["meta"]
    return (m.get("modelId", 0) if f["kind"] == "livery" else m.get("numLayers", 0)) & 0xFFFF


def preview(fid):
    try:
        with open(file_path(fid, ".prev"), "rb") as src:
            return src.read(MAX_PREVIEW)
    except OSError:
        return b""


def record(fid, f, with_preview=True):
    """A design as the storefront lists show it (the descriptors' file record)."""
    m = f["meta"]
    return {"Id": fid, "Type": f["type"], "Title": limits.clean_ascii_text(m["title"], 16),
            "Description": limits.clean_ascii_text(m["description"], 256), "Price": 0,
            "Quantity": -1, "DownloadCount": len(f["downloaders"]), "Rating": rating(f),
            "SubmissionTime": x.iso_time(f["created"]), "KeywordOne": m["keywordOne"],
            "KeywordTwo": m["keywordTwo"], "Owner": clubs.user_data(f["owner"]),
            "PayloadSize": f["size"], "PreviewPayload": preview(fid) if with_preview else b"",
            "Ordinal": ordinal(f), "IsUnderReview": False}


def short(fid, f):
    r = record(fid, f, with_preview=False)
    return {"Id": fid, "Rating": r["Rating"], "Price": 0, "DownloadCount": r["DownloadCount"],
            "Ordinal": r["Ordinal"]}


# ---- the storefront: Forza.WebServices.Storefront ----

def design_page_list(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetDesignPageList_Params, body)
    drop_stale()
    pages = [{"Page": page, "TotalSlots": SLOTS,
              "UsedSlots": len(files(kind, p["ownerXuid"], done=False))}
             for page, kind in sorted(PAGES.items())]
    return x.reply(A.ForzaStorefrontGetDesignPageList_Results, {"Pages": pages[:p["maxPages"]]})


def file_list_for_page(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetFileListForPage_Params, body)
    kind = PAGES.get(p["page"])
    if kind is None:
        return photos.get_file_list_for_page(xuid, body)
    found = files(kind, p["ownerXuid"], viewer=xuid)
    start = p["startAt"]
    page = found[start:start + min(p["maxFiles"], MAX_LIST)]
    return x.reply(A.ForzaStorefrontGetFileListForPage_Results, {
        "Files": [record(i, f) for i, f in page], "totalFiles": len(found)})


def downloads_key(e):
    return (len(e[1]["downloaders"]), e[1]["created"])


def rating_key(e):
    return (rating(e[1]), len(e[1]["ratings"]), e[1]["created"])


def listing(params_t, results_t, field, key=None):
    def handler(xuid, body):
        p, _ = x.decode(params_t, body)
        found = files(everybody=True)
        if key:
            found.sort(key=key, reverse=True)
        found = found[:max(0, min(p["maxFiles"], MAX_LIST))]
        return x.reply(results_t, {field: [record(i, f) for i, f in found]})
    return handler


def code_of(text):
    t = text.strip().upper()
    return t if len(t) == CODE_LEN and all(c in CODE_CHARS for c in t) else None


def searcher(kind, params_t, results_t):
    """A search: a share code in the gamertag or text field finds that file alone; otherwise the
    make, model and keywords given (-1: any), the owner's gamertag containing the gamertag field
    and every word of the text field in the title or description."""
    def handler(xuid, body):
        p, _ = x.decode(params_t, body)
        q = p["searchParams"]
        limit = max(0, min(p["maxFiles"], 8192))
        codes = {c for c in (code_of(q["GamerTag"]), code_of(q["ManualSearchKeywords"])) if c}
        live = files(kind, everybody=True)
        if codes:
            by_code = [e for e in live if e[1]["code"] in codes]
            if by_code:
                return x.reply(results_t, {"searchResults": [short(i, f) for i, f in by_code]})
        tag = q["GamerTag"].strip().lower()
        words = q["ManualSearchKeywords"].lower().split()
        found = []
        for fid, f in sorted(live, key=rating_key, reverse=True):
            m = f["meta"]
            text = (m["title"] + " " + m["description"]).lower()
            if ((q["MakeId"] < 0 or q["MakeId"] == m.get("makeId", -2))
                    and (q["ModelId"] < 0 or q["ModelId"] == m.get("modelId", -2))
                    and (q["KeywordOneId"] < 0 or q["KeywordOneId"] == m["keywordOne"])
                    and (q["KeywordTwoId"] < 0 or q["KeywordTwoId"] == m["keywordTwo"])
                    and (not tag or tag in clubs.gamertag(f["owner"]).lower())
                    and all(w in text for w in words)):
                found.append(short(fid, f))
        return x.reply(results_t, {"searchResults": found[:limit]})
    return handler


def heavy_weight_data(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetFileHeavyWeightData_Params, body)
    out = []
    for fid in p["FileIds"][:MAX_LIST]:
        if is_design(fid):
            f = visible(fid, xuid)
            if f is not None:
                out.append(record(fid, f))
        else:
            f = photos.shared_file(fid)
            if f is not None:
                out.append(photos.file_record(fid, f))
    return x.reply(A.ForzaStorefrontGetFileHeavyWeightData_Results, {"fileData": out})


def purchase(xuid, body):
    """A design for anybody, free: the payload and preview within the sizes the game asks for."""
    p, _ = x.decode(A.ForzaStorefrontPurchase_Params, body)
    if not is_design(p["fileId"]):
        return photos.purchase(xuid, body)
    f = visible(p["fileId"], xuid)
    if f is None:
        return x.failure(1, "no such file")
    if f["size"] > p["maxPayloadSize"]:
        return x.failure(2, "file too large")
    with open(file_path(p["fileId"], ".bin"), "rb") as src:
        payload = src.read()
    prev = preview(p["fileId"])
    if len(prev) > p["maxPreviewSize"]:
        prev = b""
    if xuid != f["owner"] and _caller["trusted"]() and hx(xuid) not in f["downloaders"]:
        f["downloaders"].append(hx(xuid))
        save()
    r = record(p["fileId"], f, with_preview=False)
    return x.reply(A.ForzaStorefrontPurchase_Results, {
        "returnCredits": 0, "payload": payload, "previewPayload": prev,
        "purchasedFileInfo": {"Title": r["Title"], "Ordinal": r["Ordinal"], "Type": r["Type"],
                              "Owner": r["Owner"]}})


def by_file(params_t, design_fn, photo_fn=None):
    """A call naming a file: designs here, photos (ids below FIRST_ID) to forza_photos."""
    def handler(xuid, body):
        p, _ = x.decode(params_t, body)
        if not is_design(p["fileId"]):
            return photo_fn(xuid, body) if photo_fn else x.reply()
        f = load()["files"].get(str(p["fileId"]))
        if f is None or not f["done"]:
            return x.failure(1, "no such file")
        return design_fn(xuid, p, f)
    return handler


def _views(xuid, p, f):
    if _caller["trusted"]() and xuid != f["owner"]:
        f["views"] += 1
        save()
    return x.reply()


def _rate(xuid, p, f):
    if _caller["trusted"]() and xuid != f["owner"] and 1 <= p["rating"] <= 5 \
            and visible(p["fileId"], xuid) is not None:
        f["ratings"][hx(xuid)] = p["rating"]
        save()
    return x.reply()


def _report(xuid, p, f):
    if not _caller["trusted"]() or xuid == f["owner"] or hx(xuid) in f["reporters"]:
        return x.reply()
    f["reporters"].append(hx(xuid))
    if len(f["reporters"]) >= REPORT_HIDE and f["status"] == LIVE:
        f["status"] = HIDDEN
        log.warning("designs: %s %d (%016X) hidden after %d reports", f["kind"], p["fileId"],
                    f["owner"], len(f["reporters"]))
    save()
    return x.reply()


def _remove(xuid, p, f):
    if f["owner"] != xuid:
        return x.failure(1, "no such file")
    remove(p["fileId"])
    save()
    log.info("designs: %016X: %s %d removed", xuid, f["kind"], p["fileId"])
    return x.reply()


def _describe(xuid, p, f):
    if f["owner"] != xuid:
        return x.failure(1, "no such file")
    f["meta"]["description"] = limits.clean_ascii_text(p["newDescription"], 256)
    save()
    return x.reply()


def _price(xuid, p, f):
    """Accepted, but every design stays free and unlimited."""
    if f["owner"] != xuid:
        return x.failure(1, "no such file")
    return x.reply()


def change_photo_price(xuid, body):
    return x.reply()  # photos have neither price nor quantity here


def get_stats(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetStats_Params, body)
    stats = {}
    for kind, up, down in (("livery", "liveriesUploaded", "liveriesDownloaded"),
                           ("layergroup", "layerGroupsUploaded", "layerGroupsDownloaded")):
        mine = [f for _, f in files(kind, p["xuid"], everybody=True)]
        stats[up] = len(mine)
        stats[down] = sum(len(f["downloaders"]) for f in mine)
    shared = photos.shared_files(p["xuid"])
    stats["photosUploaded"] = len(shared)
    stats["photosDownloaded"] = sum(f["meta"]["downloads"] for _, f in shared)
    return x.reply(A.ForzaStorefrontGetStats_Results, {"stats": stats})


def services():
    s = {}
    for kind, (cls, base, *_rest) in KINDS.items():
        s[(cls, "Create")] = creator(kind)
        s[("Storefront", cls[len("Storefront"):] + "Create")] = s[(cls, "Create")]
    s.update({
        ("Storefront", "GetDesignPageList"): design_page_list,
        ("Storefront", "GetFileListForPage"): file_list_for_page,
        ("Storefront", "GetFeaturedDesigns"): listing(
            A.ForzaStorefrontGetFeaturedDesigns_Params, A.ForzaStorefrontGetFeaturedDesigns_Results,
            "FeaturedFiles"),
        ("Storefront", "GetMostDownloadedDesigns"): listing(
            A.ForzaStorefrontGetMostDownloadedDesigns_Params,
            A.ForzaStorefrontGetMostDownloadedDesigns_Results, "MostDownloadedDesigns",
            downloads_key),
        ("Storefront", "GetTopRatedDesigns"): listing(
            A.ForzaStorefrontGetTopRatedDesigns_Params, A.ForzaStorefrontGetTopRatedDesigns_Results,
            "TopRatedDesigns", rating_key),
        ("Storefront", "SearchLiveryFiles"): searcher(
            "livery", A.ForzaStorefrontSearchLiveryFiles_Params,
            A.ForzaStorefrontSearchLiveryFiles_Results),
        ("Storefront", "SearchLayergroupFiles"): searcher(
            "layergroup", A.ForzaStorefrontSearchLayergroupFiles_Params,
            A.ForzaStorefrontSearchLayergroupFiles_Results),
        ("Storefront", "GetFileHeavyWeightData"): heavy_weight_data,
        ("Storefront", "Purchase"): purchase,
        ("Storefront", "IncrementViews"): by_file(A.ForzaStorefrontIncrementViews_Params, _views),
        ("Storefront", "SubmitFileRating"): by_file(A.ForzaStorefrontSubmitFileRating_Params, _rate,
                                                    photos.submit_rating),
        ("Storefront", "ReportContent"): by_file(A.ForzaStorefrontReportContent_Params, _report),
        ("Storefront", "RemoveFile"): by_file(A.ForzaStorefrontRemoveFile_Params, _remove,
                                              photos.remove_file),
        ("Storefront", "ChangeFileDescription"): by_file(
            A.ForzaStorefrontChangeFileDescription_Params, _describe,
            photos.change_field(A.ForzaStorefrontChangeFileDescription_Params, "newDescription",
                                "description", 256)),
        ("Storefront", "ChangeFilePriceQuantity"): by_file(
            A.ForzaStorefrontChangeFilePriceQuantity_Params, _price, change_photo_price),
        ("Storefront", "GetStats"): get_stats,
        ("Xls.Content", "UploadContent"): upload_content,
        ("Xls.Content", "DownloadContent"): download_content,
    })
    return s
