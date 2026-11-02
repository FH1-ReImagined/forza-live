"""Forza Live: photo uploads (Forza.WebServices.LargeFileUpload / StorefrontPhoto.Create and
Xls.WebServices.Content) and the photo storefront (Forza.WebServices.Storefront reads).

Photo mode's SAVE and "choose large photo" end at "uploading to forzamotorsport.net". The game
first asks for a content file (LargeFileUpload.Create(payloadSize, overwriteOldest, name), or
StorefrontPhoto.Create with the photo's header and preview, both answer a contentFileId), then
sends the picture with Content.UploadContent(contentId, payloadOffset, payloadTotalSize,
uploadData) and carries on when the answer's payloadUploadedSize reaches the total. A server
that answers neither leaves the game at the upload screen (empty answers read as id 0 and
0 bytes uploaded).

A player's files are kept as sent, under DATA_DIR/photos/ (counted in the server's disk quota),
at most MAX_PER_PLAYER of them, each up to limits.MAX_BLOB bytes; with overwriteOldest the oldest
of the player's files makes room, without it a full player gets a failure. DATA_DIR/photos.json
is the index. Content.DownloadContent gives a player's own file back, and anybody's shared photo
(anything else gets the empty answer the game has always had for it).

Photos that came in with StorefrontPhoto.Create are the player's shared photos (the storefront,
MY SHARED PHOTOS): at most STOREFRONT_SLOTS per player, one storefront page (PHOTO_PAGE) that
GetPhotoPageList reports with its total and used slots; GetFileListForPage lists a player's
shared photos (newest first, with their previews), Purchase hands one out (to anybody),
RemoveFile / ChangeFileTitle / ChangeFileDescription act on the owner's. The featured, most
downloaded and top rated photo lists are over everybody's. An upload that never finished
frees its slot after STALE_UPLOAD seconds.

Photo mode's large photo (TAKE PHOTO, RB: BIG SHOT 8.3 MEGAPIXEL, then "create a large photo and
upload it to ForzaMotorsport.net") is LargeFileUpload.Create(24883254, overwriteOldest false,
"ForzaBigshot.bmp"): a 3840x2160 24-bit BMP. Files over limits.MAX_BLOB ("big shots", up to
MAX_BIG) are taken only from a request signed with the player's own key, at most BIG_PER_HOUR
are started an hour on the whole server, and a player has one unfinished at a time. A big
shot is not kept as a file: once it has arrived it goes to forza_community (which publishes a
smaller copy, or nothing) and is forgotten.

A finished photo mode upload (kind "large") is handed to configure(finished=) with whether it was
signed by the player's own key (its Create and the call that completed it; configure(proven=),
forza_live_lsp.caller_signed): forza_community publishes the signed ones. Every file removed is
told to configure(removed=).
"""

import json
import logging
import os
import time

import forza_clubs as clubs
import forza_limits as limits
import forza_xtsw as x
import forza_xtsw_all as A
import forza_durable as durable

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
MAX_PER_PLAYER = 100
MAX_PREVIEW = 256 << 10  # a photo's preview (thumbnail) in PhotoCreate
MAX_CHUNK = 1 << 20      # bytes handed back by one DownloadContent
STOREFRONT_SLOTS = 25    # shared photos per player
PHOTO_PAGE = 3           # the storefront page of the shared photos (tuning, liveries, layer groups, photos, replays)
STALE_UPLOAD = 3600      # an unfinished shared photo gives its slot back after this
MAX_LIST = 50            # files in one list answer (each carries its preview)
MAX_BIG = 32 << 20       # a big shot (photo mode's 3840x2160 BMP is 24,883,254 bytes)
BIG_PER_HOUR = 60        # big shots started an hour on the whole server
INDEX = {"nextId": 1, "files": {}}
_big = []                # when this hour's big shots were started
_loaded = [False]
_hooks = {"proven": lambda: False, "finished": None, "removed": None}


def configure(data_dir, proven=None, finished=None, removed=None):
    """proven(): the call being handled is signed by its player's own key; finished(fid, record,
    path, signed): a photo mode upload has arrived; removed(fid): a file is gone."""
    global DATA_DIR
    DATA_DIR = data_dir
    INDEX.update({"nextId": 1, "files": {}})
    _loaded[0] = False
    _big.clear()
    _hooks.update({"proven": proven or (lambda: False), "finished": finished, "removed": removed})


def tell(hook, *args):
    """A hook's call; its failure is logged and never fails the player's upload."""
    if _hooks[hook] is not None:
        try:
            _hooks[hook](*args)
        except Exception:
            log.exception("photos: the %s hook failed", hook)


def index_path():
    return os.path.join(DATA_DIR, "photos.json")


def file_path(fid, suffix=".bin"):
    return os.path.join(DATA_DIR, "photos", "%d%s" % (fid, suffix))


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


def forget(fid):
    load()["files"].pop(str(fid), None)
    for suffix in (".bin", ".prev"):
        try:
            os.remove(file_path(fid, suffix))
        except OSError:
            pass


def remove(fid):
    forget(fid)
    tell("removed", fid)


def create(xuid, size, overwrite, name="", kind="photo", preview=b"", meta=None):
    """A new content file of `size` bytes for the player; its id. ValueError: refused."""
    d = load()
    big = kind == "large" and size > limits.MAX_BLOB
    if not 0 < size <= (MAX_BIG if big else limits.MAX_BLOB):
        raise ValueError("file of %d bytes refused" % size)
    if len(preview) > MAX_PREVIEW:
        raise ValueError("preview of %d bytes refused" % len(preview))
    if big:
        if not _hooks["proven"]():
            raise ValueError("a big shot of %d bytes needs a request signed by the player's key"
                             % size)
        now = time.time()
        _big[:] = [t for t in _big if now - t < 3600]
        if len(_big) >= BIG_PER_HOUR:
            raise ValueError("the server takes no more big shots this hour")
        for i, f in list(d["files"].items()):  # the player's unfinished one, abandoned ones
            if f.get("big") and not f["done"] and (f["owner"] == xuid
                                                   or now - f["created"] > STALE_UPLOAD):
                forget(int(i))
    mine = sorted((f["created"], int(i)) for i, f in d["files"].items() if f["owner"] == xuid)
    while len(mine) >= MAX_PER_PLAYER:
        if not overwrite:
            raise ValueError("storage full")
        remove(mine.pop(0)[1])
    limits.accept(size + len(preview), MAX_BIG if big else limits.MAX_BLOB)
    fid = d["nextId"]
    d["nextId"] += 1
    os.makedirs(os.path.dirname(file_path(fid)), exist_ok=True)
    with open(file_path(fid), "wb") as f:
        f.truncate(size)  # sparse: the quota counts the whole file from the start
    if preview:
        with open(file_path(fid, ".prev"), "wb") as f:
            f.write(preview)
    d["files"][str(fid)] = {"owner": xuid, "size": size, "got": 0, "kind": kind,
                            "name": name[:64], "created": time.time(), "done": False}
    if kind == "large":
        d["files"][str(fid)]["signed"] = bool(_hooks["proven"]())
    if big:
        d["files"][str(fid)]["big"] = True
        _big.append(time.time())
    if meta is not None:
        d["files"][str(fid)]["meta"] = meta
    save()
    log.info("photos: %016X: file %d (%s, %d bytes)", xuid, fid, kind, size)
    return fid


def large_file_create(xuid, body):
    p, _ = x.decode(A.ForzaLargeFileUploadCreate_Params, body)
    fid = create(xuid, p["payloadSize"], p["overwriteOldest"], p["userFileName"], "large")
    return x.reply(A.ForzaLargeFileUploadCreate_Results, {"contentFileId": fid})


def shared_files(owner=None):
    """(id, record) of the shared photos, finished or still arriving, newest first; the
    unfinished ones that were abandoned are dropped."""
    d = load()
    now = time.time()
    stale = [int(i) for i, f in d["files"].items()
             if "meta" in f and not f["done"] and now - f["created"] > STALE_UPLOAD]
    for fid in stale:
        remove(fid)
    if stale:
        save()
    mine = [(int(i), f) for i, f in d["files"].items()
            if "meta" in f and (owner is None or f["owner"] == owner)]
    return sorted(mine, key=lambda e: (e[1]["created"], e[0]), reverse=True)


def photo_create(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontPhotoCreate_Params, body)
    h, desc = p["fileHeader"], p["photoDesc"]["base"]
    if len(shared_files(xuid)) >= STOREFRONT_SLOTS:
        log.info("photos: %016X: storefront full (%d slots)", xuid, STOREFRONT_SLOTS)
        return x.failure(3, "storefront full")
    for f, n in (("Title", 16), ("Description", 256)):
        h[f] = limits.clean_ascii_text(h[f], n)
    meta = {"title": h["Title"], "description": h["Description"], "price": h["Price"],
            "quantity": h["Quantity"], "type": h["Type"], "keywordOne": h["KeywordOne"],
            "keywordTwo": h["KeywordTwo"], "envId": desc["EnvironmentId"],
            "trackId": desc["TrackId"], "makeId": desc["MakeId"], "modelId": desc["ModelId"],
            "forStorefront": bool(desc["ForStorefrontUse"]), "downloads": 0, "ratings": 0,
            "ratingSum": 0}
    size = p["payloadSize"] or h["PayloadSize"]
    fid = create(xuid, size, True, h["Title"], "photo", p["previewData"], meta)
    return x.reply(A.ForzaStorefrontPhotoCreate_Results, {"contentFileId": fid})


# ---- the storefront: Forza.WebServices.Storefront ----

def file_record(fid, f, preview=True):
    """A shared photo as the storefront lists show it (the descriptors' file record)."""
    m = f["meta"]
    prev = b""
    if preview:
        try:
            with open(file_path(fid, ".prev"), "rb") as src:
                prev = src.read()
        except OSError:
            pass
    rating = m["ratingSum"] / m["ratings"] if m["ratings"] else 0.0
    return {"Id": fid, "Type": m["type"], "Title": limits.clean_ascii_text(m["title"], 16),
            "Description": limits.clean_ascii_text(m["description"], 256),
            "Price": m["price"], "Quantity": m["quantity"], "DownloadCount": m["downloads"],
            "Rating": rating, "SubmissionTime": x.iso_time(f["created"]),
            "KeywordOne": m["keywordOne"], "KeywordTwo": m["keywordTwo"],
            "Owner": clubs.user_data(f["owner"]), "PayloadSize": f["size"],
            "PreviewPayload": prev, "Ordinal": m["envId"]}


def listing(result_type, field, files):
    return x.reply(result_type, {field: [file_record(i, f) for i, f in files[:MAX_LIST]]})


def get_photo_page_list(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetPhotoPageList_Params, body)
    pages = [{"Page": PHOTO_PAGE, "TotalSlots": STOREFRONT_SLOTS,
              "UsedSlots": len(shared_files(p["ownerXuid"]))}]
    return x.reply(A.ForzaStorefrontGetPhotoPageList_Results, {"Pages": pages[:p["maxPages"]]})


def get_file_list_for_page(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetFileListForPage_Params, body)
    if p["page"] != PHOTO_PAGE:
        return x.reply(A.ForzaStorefrontGetFileListForPage_Results, {})
    files = [e for e in shared_files(p["ownerXuid"]) if e[1]["done"]]
    start = p["startAt"]
    page = files[start:start + min(p["maxFiles"], MAX_LIST)]
    return x.reply(A.ForzaStorefrontGetFileListForPage_Results, {
        "Files": [file_record(i, f) for i, f in page], "totalFiles": len(files)})


def get_stats(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetStats_Params, body)
    mine = shared_files(p["xuid"])
    return x.reply(A.ForzaStorefrontGetStats_Results, {"stats": {
        "photosUploaded": len(mine),
        "photosDownloaded": sum(f["meta"]["downloads"] for _, f in mine)}})


def finished(key=None):
    """Everybody's finished shared photos, best first by `key` (newest first by default)."""
    files = [e for e in shared_files() if e[1]["done"] and e[1]["meta"]["forStorefront"]]
    return sorted(files, key=key, reverse=True) if key else files


def downloads_key(e):
    return (e[1]["meta"]["downloads"], e[1]["created"])


def rating_key(e):
    m = e[1]["meta"]
    return (m["ratingSum"] / m["ratings"] if m["ratings"] else 0.0, m["ratings"], e[1]["created"])


def featured_photos(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetFeaturedPhotos_Params, body)
    return listing(A.ForzaStorefrontGetFeaturedPhotos_Results, "FeaturedFiles",
                   finished(None)[:max(p["maxFiles"], 0)])


def most_downloaded_photos(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetMostDownloadedPhotos_Params, body)
    return listing(A.ForzaStorefrontGetMostDownloadedPhotos_Results, "MostDownloadedPhotos",
                   finished(downloads_key)[:max(p["maxFiles"], 0)])


def top_rated_photos(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetTopRatedPhotos_Params, body)
    return listing(A.ForzaStorefrontGetTopRatedPhotos_Results, "TopRatedPhotos",
                   finished(rating_key)[:max(p["maxFiles"], 0)])


def search_photos(xuid, body):
    """SearchPhotoFiles: the shared photos that match the fields the player filled in (a field
    of -1 or an empty name matches everything)."""
    p, _ = x.decode(A.ForzaStorefrontSearchPhotoFiles_Params, body)
    q, tag = p["searchParams"], p["searchParams"]["GamerTag"].lower()
    found = []
    for fid, f in finished(rating_key):
        m = f["meta"]
        if ((q["MakeId"] < 0 or q["MakeId"] == m["makeId"])
                and (q["ModelId"] < 0 or q["ModelId"] == m["modelId"])
                and (q["KeywordOneId"] < 0 or q["KeywordOneId"] == m["keywordOne"])
                and (q["EnvironmentId"] < 0 or q["EnvironmentId"] == m["envId"])
                and (q["TrackId"] < 0 or q["TrackId"] == m["trackId"])
                and (q["KeywordTwoId"] < 0 or q["KeywordTwoId"] == m["keywordTwo"])
                and (not tag or tag in clubs.gamertag(f["owner"]).lower())):
            r = file_record(fid, f, preview=False)
            found.append({"Id": fid, "Rating": r["Rating"], "Price": r["Price"],
                          "DownloadCount": r["DownloadCount"], "Ordinal": r["Ordinal"]})
    return x.reply(A.ForzaStorefrontSearchPhotoFiles_Results,
                   {"searchResults": found[:max(p["maxFiles"], 0)]})


def shared_file(fid):
    f = load()["files"].get(str(fid))
    return f if f is not None and "meta" in f and f["done"] else None


def heavy_weight_data(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontGetFileHeavyWeightData_Params, body)
    files = [(i, shared_file(i)) for i in p["FileIds"][:MAX_LIST]]
    return x.reply(A.ForzaStorefrontGetFileHeavyWeightData_Results, {
        "fileData": [file_record(i, f) for i, f in files if f]})


def purchase(xuid, body):
    """A shared photo's picture and preview: free, to anybody."""
    p, _ = x.decode(A.ForzaStorefrontPurchase_Params, body)
    f = shared_file(p["fileId"])
    if f is None:
        return x.failure(1, "no such file")
    if f["size"] > p["maxPayloadSize"]:
        return x.failure(2, "file too large")
    rec = file_record(p["fileId"], f)
    with open(file_path(p["fileId"]), "rb") as src:
        payload = src.read()
    f["meta"]["downloads"] += 1
    save()
    return x.reply(A.ForzaStorefrontPurchase_Results, {
        "returnCredits": 0, "payload": payload, "previewPayload": rec["PreviewPayload"],
        "purchasedFileInfo": {"Title": rec["Title"], "Ordinal": rec["Ordinal"],
                              "Type": rec["Type"], "Owner": rec["Owner"]}})


def own_shared_file(xuid, fid):
    f = shared_file(fid)
    return f if f is not None and f["owner"] == xuid else None


def remove_file(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontRemoveFile_Params, body)
    if own_shared_file(xuid, p["fileId"]) is None:
        return x.failure(1, "no such file")
    remove(p["fileId"])
    save()
    log.info("photos: %016X: shared photo %d removed", xuid, p["fileId"])
    return x.reply()


def change_field(ptype, param, field, limit):
    def handler(xuid, body):
        p, _ = x.decode(ptype, body)
        f = own_shared_file(xuid, p["fileId"])
        if f is None:
            return x.failure(1, "no such file")
        f["meta"][field] = limits.clean_ascii_text(p[param], limit)
        save()
        return x.reply()
    return handler


def submit_rating(xuid, body):
    p, _ = x.decode(A.ForzaStorefrontSubmitFileRating_Params, body)
    f = shared_file(p["fileId"])
    if f is not None and f["owner"] != xuid:
        f["meta"]["ratings"] += 1
        f["meta"]["ratingSum"] += min(p["rating"], 5)
        save()
    return x.reply()


def upload_content(xuid, body):
    p, _ = x.decode(A.ContentUploadContent_Params, body)
    f = load()["files"].get(str(p["contentId"]))
    if f is None or f["owner"] != xuid:
        return x.failure(1, "no such file")
    data, offset = p["uploadData"], p["payloadOffset"]
    if offset > f["got"] or offset + len(data) > f["size"]:
        return x.failure(2, "bad range")  # a gap, or more than the size it announced
    if data:
        with open(file_path(p["contentId"]), "r+b") as out:
            out.seek(offset)
            out.write(data)
    f["got"] = max(f["got"], offset + len(data))
    if f["got"] >= f["size"] and not f["done"]:
        f["done"] = True
        save()
        log.info("photos: %016X: file %d uploaded (%d bytes)", xuid, p["contentId"], f["size"])
        if f["kind"] == "large":  # photo mode's upload: a community photo when it was signed
            tell("finished", p["contentId"], f, file_path(p["contentId"]),
                 bool(f.get("signed") and _hooks["proven"]()))
        if f.get("big"):  # not kept: the community has its copy (or nothing)
            forget(p["contentId"])
            save()
    return x.reply(A.ContentUploadContent_Results, {"payloadUploadedSize": f["got"]})


def download_content(xuid, body):
    p, _ = x.decode(A.ContentDownloadContent_Params, body)
    f = load()["files"].get(str(p["contentId"]))
    shared = f is not None and f.get("meta", {}).get("forStorefront")
    if f is None or not f["done"] or (f["owner"] != xuid and not shared):
        # What every other content id got before: the game asks for id 0 after sign-in and
        # a failure there would be retried; nothing (isLastBuffer false, 0 bytes) is final.
        return x.reply(A.ContentDownloadContent_Results, {})
    offset, want = p["payloadOffset"], min(p["requestedSize"], MAX_CHUNK)
    with open(file_path(p["contentId"]), "rb") as src:
        src.seek(offset)
        data = src.read(want)
    return x.reply(A.ContentDownloadContent_Results, {
        "isLastBuffer": offset + len(data) >= f["size"], "downloadData": data,
        "totalSize": f["size"]})


def services():
    return {
        ("LargeFileUpload", "Create"): large_file_create,
        ("StorefrontPhoto", "Create"): photo_create,  # the TU4 game's class and method
        ("Storefront", "PhotoCreate"): photo_create,
        ("Storefront", "GetPhotoPageList"): get_photo_page_list,
        ("Storefront", "GetFileListForPage"): get_file_list_for_page,
        ("Storefront", "GetStats"): get_stats,
        ("Storefront", "GetFeaturedPhotos"): featured_photos,
        ("Storefront", "GetMostDownloadedPhotos"): most_downloaded_photos,
        ("Storefront", "GetTopRatedPhotos"): top_rated_photos,
        ("Storefront", "SearchPhotoFiles"): search_photos,
        ("Storefront", "GetFileHeavyWeightData"): heavy_weight_data,
        ("Storefront", "Purchase"): purchase,
        ("Storefront", "RemoveFile"): remove_file,
        ("Storefront", "ChangeFileTitle"): change_field(
            A.ForzaStorefrontChangeFileTitle_Params, "newTitle", "title", 16),
        ("Storefront", "ChangeFileDescription"): change_field(
            A.ForzaStorefrontChangeFileDescription_Params, "newDescription", "description", 256),
        ("Storefront", "SubmitFileRating"): submit_rating,
        ("Xls.Content", "UploadContent"): upload_content,
        ("Xls.Content", "DownloadContent"): download_content,
    }
