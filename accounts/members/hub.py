"""The mods and texture packs hub (website #2): players share mods and texture packs made for the
game; the website lists them, and a separate download host serves the files. Off unless
FORZA_HUB=1 (every route here answers 404 then).

Public (anyone, no session, a short public cache):
  GET  /api/hub/items[?kind=mod|pack]     the live items, newest first
  GET  /api/hub/items/<id>                one live item
Signed in, with the account linked to its game (members.likes.usable_link), like likes:
  POST /api/hub/items                     {"kind", "title", "description", "version", "own_work": true}
                                          -> a draft and where to send its file
  PUT  /api/hub/items/<id>/file           the .zip itself (Content-Type application/zip, a
                                          Content-Length; no JSON): streamed to the incoming folder
  GET  /api/hub/mine                      the key's items, in every state, with why
  POST /api/hub/items/<id>/delete         {}: takes it down (a sign-in in the past 10 minutes)
  POST /api/hub/reports                   {"item", "reason", "note"}: one report per key and item
Likes are members.likes' (kinds "mod" and "pack").

What happens to an upload (hub/README.md): this service only streams the bytes to
HUB_INCOMING/<id>.zip (checking the size, the zip's first bytes and the quota), then writes the
job <id>.json. It never opens the zip. The scanning worker, in a container with no network and a
read-only root (hub/hub_worker.py), checks it with hub/hub_check.py (an allow-list of contents,
zip slip, zip bombs, sizes and counts, the launcher's rules), scans it with ClamAV, and writes a
clean zip and its result to HUB_CHECKED. collect() reads the results: a refused upload says why
to its uploader, a clean one waits for a maintainer (state "review"), who approves it in the
admin; approve() copies the clean zip, checking its SHA-256, to HUB_PUBLIC/<sha256>/<folder>.zip,
which the download host (another host name, attachments only) serves. Hiding or taking an item
down deletes its public copy.

Reports: one per key and item. A "malware" report hides a live item at once; HUB_REPORTS_HIDE
reports from keys whose likes count hide it too. A maintainer checks it and shows it again or
takes it down. Every step writes a log line with the item id and the account id, never a XUID.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import List, Literal, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from ninja.security import django_auth
from pydantic import Field

from members import game, items, links
from members.api import EmptyIn, ErrorOut, Strict, api, recently_signed_in
from members.game import Refused
from members.models import AuditEvent, HubItem, HubReport, Like

log = logging.getLogger("members")

ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
ID_RE = re.compile(r"[A-Z2-7]{12}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
CHUNK = 1 << 20
RESULT_MAX = 256 << 10
CACHE_SECONDS = 60
OPEN = (HubItem.DRAFT, HubItem.UPLOADING, HubItem.QUEUED, HubItem.REVIEW)
ZIP_MAGIC = b"PK\x03\x04"
DRAFT_HOURS = 24          # a draft with no file is deleted after this
UPLOADING_HOURS = 1       # an upload that never finished goes back to draft after this

Kind = Literal["mod", "pack"]
Reason = Literal["malware", "not-own-work", "game-files", "broken", "offensive", "other"]

_lock = threading.Lock()
_list = {"at": None, "doc": None}
_collected = [0.0]


class HubError(Exception):
    """An operator's step that cannot be done."""


def on():
    if not settings.HUB:
        raise Refused(404, "not available")


def folders():
    for d in (settings.HUB_INCOMING, settings.HUB_CHECKED, settings.HUB_PUBLIC):
        d.mkdir(parents=True, exist_ok=True)


def new_id():
    while True:
        candidate = "".join(secrets.choice(ID_CHARS) for _ in range(12))
        if not HubItem.objects.filter(pk=candidate).exists():
            return candidate


def clean(text, limit, lines=False):
    """Player text: no control or format characters, spaces collapsed (lines kept if asked)."""
    import unicodedata
    text = "".join(c for c in (text or "") if unicodedata.category(c) not in ("Cf", "Cs", "Co"))
    text = "".join(c if c.isprintable() or (lines and c == "\n") else " " for c in text)
    rows = [" ".join(r.split()) for r in text.split("\n")] if lines else [" ".join(text.split())]
    return "\n".join(r for r in rows if r).strip()[:limit]


# ---- Schemas ----

class NewIn(Strict):
    kind: Kind
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=4000)
    version: str = Field(default="", max_length=60)
    own_work: bool


class NewOut(Strict):
    id: str
    state: str
    upload: str
    max_bytes: int


class UploadedOut(Strict):
    ok: bool
    state: str


class MineItem(Strict):
    id: str
    kind: Kind
    title: str
    version: str
    state: str
    reason: str
    created: int
    size: int
    sha256: str
    likes: int


class MineOut(Strict):
    items: List[MineItem]
    max_bytes: int


class ReportIn(Strict):
    item: str = Field(pattern=r"^[A-Z2-7]{12}$")
    reason: Reason
    note: str = Field(default="", max_length=2000)


class OkOut(Strict):
    ok: bool


# ---- What the public sees ----

def download_url(item):
    return "%s/%s/%s.zip" % (settings.HUB_DOWNLOADS, item.sha256, item.folder)


def public_doc(item, totals):
    return {"id": item.id, "kind": item.kind, "title": item.title, "description": item.description,
            "version": item.version, "author": (game.gamertag(int(item.owner, 16)) or "") if game.available() else "",
            "created": int(item.created.timestamp()),
            "published": int(item.published.timestamp()) if item.published else int(item.created.timestamp()),
            "size": item.file_size, "files": item.files, "sha256": item.sha256, "folder": item.folder,
            "download": download_url(item), "likes": totals.get(item.id, 0)}


def as_item(h):
    """A HubItem as members.items.Item, for likes."""
    return items.Item(h.kind, h.id, h.owner, h.title, h.state, h.state == HubItem.LIVE, int(h.created.timestamp()))


def find(kind, item_id):
    if not settings.HUB:
        return None
    h = HubItem.objects.filter(pk=item_id, kind=kind).first()
    return as_item(h) if h else None


def live_items(kind):
    """{id: items.Item} of the live items of a kind (members.likes counts their likes)."""
    if not settings.HUB:
        return {}
    return {h.id: as_item(h) for h in HubItem.objects.filter(kind=kind, state=HubItem.LIVE)}


def owned(xuid, kind):
    return list(HubItem.objects.filter(owner=xuid, kind=kind, state=HubItem.LIVE).values_list("id", flat=True))


def list_doc():
    from members import likes
    with _lock:
        if _list["at"] is not None and time.monotonic() - _list["at"] < settings.HUB_SECONDS:
            return _list["doc"]
    collect()
    totals = {kind: likes.snapshot(kind)["counts"] for kind in (HubItem.MOD, HubItem.PACK)}
    rows = [public_doc(h, totals[h.kind]) for h in HubItem.objects.filter(state=HubItem.LIVE).order_by("-published")]
    doc = {"version": 1, "generated": int(time.time()), "downloads": settings.HUB_DOWNLOADS, "items": rows}
    with _lock:
        _list.update(at=time.monotonic(), doc=doc)
    return doc


def forget():
    with _lock:
        _list.update(at=None, doc=None)


def public(doc):
    response = HttpResponse(json.dumps(doc, separators=(",", ":")), content_type="application/json")
    response["Cache-Control"] = "public, max-age=%d" % CACHE_SECONDS  # kept by ApiHeaders
    return response


@api.get("/hub/items", auth=None)
def hub_items(request, kind: Optional[Kind] = None):
    on()
    doc = list_doc()
    if kind:
        doc = dict(doc, items=[i for i in doc["items"] if i["kind"] == kind])
    return public(doc)


@api.get("/hub/items/{item_id}", auth=None)
def hub_item(request, item_id: str):
    on()
    found = next((i for i in list_doc()["items"] if i["id"] == item_id), None) if ID_RE.fullmatch(item_id) else None
    if found is None:
        raise Refused(404, "not found")
    return public(found)


# ---- Uploading ----

def stored_bytes():
    """What the hub keeps on disk, from its records: uploads waiting, clean zips, public copies."""
    q = HubItem.objects.exclude(state=HubItem.REMOVED)
    waiting = q.filter(state__in=(HubItem.UPLOADING, HubItem.QUEUED)).aggregate(n=Sum("upload_size"))["n"] or 0
    clean_zips = q.filter(state__in=(HubItem.REVIEW, HubItem.LIVE, HubItem.HIDDEN)).aggregate(n=Sum("file_size"))["n"] or 0
    public_copies = q.filter(state=HubItem.LIVE).aggregate(n=Sum("file_size"))["n"] or 0
    return waiting + clean_zips + public_copies


@api.post("/hub/items", response={200: NewOut, 400: ErrorOut, 403: ErrorOut, 409: ErrorOut, 429: ErrorOut,
                                  503: ErrorOut}, auth=django_auth)
def hub_new(request, body: NewIn):
    on()
    from members.likes import usable_link
    user = request.auth
    link = usable_link(user)
    if not body.own_work:
        return 400, {"error": "only your own work"}
    title = clean(body.title, 60)
    if not title:
        return 400, {"error": "give it a title"}
    if HubItem.objects.filter(owner=link.xuid, state__in=OPEN).count() >= settings.HUB_OPEN_MAX:
        return 409, {"error": "wait until your other uploads are checked"}
    if HubItem.objects.filter(state__in=(HubItem.UPLOADING, HubItem.QUEUED, HubItem.REVIEW)).count() >= settings.HUB_QUEUE_MAX:
        return 503, {"error": "the queue is full: try again later"}
    links.spend(request, "hub_upload", "a:%d" % user.pk)
    now = timezone.now()
    item = HubItem.objects.create(id=new_id(), kind=body.kind, owner=link.xuid, uploader=user, title=title,
                                  description=clean(body.description, 1000, lines=True),
                                  version=clean(body.version, 20), created=now, updated=now)
    AuditEvent.objects.create(user=user, event="started the %s %s" % (item.get_kind_display(), item.id))
    log.info("accounts: account %s started hub item %s (%s)", user.public_id, item.id, item.kind)
    return {"id": item.id, "state": item.state, "upload": "/api/hub/items/%s/file" % item.id,
            "max_bytes": settings.HUB_MAX_BYTES}


@api.put("/hub/items/{item_id}/file", response={200: UploadedOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut,
                                                409: ErrorOut, 411: ErrorOut, 413: ErrorOut, 415: ErrorOut,
                                                503: ErrorOut}, auth=django_auth)
def hub_upload(request, item_id: str):
    on()
    from members.likes import usable_link
    user = request.auth
    link = usable_link(user)
    if request.content_type not in ("application/zip", "application/x-zip-compressed", "application/octet-stream"):
        return 415, {"error": "send a .zip file"}
    try:
        length = int(request.META.get("CONTENT_LENGTH") or "")
    except ValueError:
        return 411, {"error": "the size is missing"}
    if length <= len(ZIP_MAGIC):
        return 400, {"error": "the file is empty"}
    if length > settings.HUB_MAX_BYTES:
        return 413, {"error": "bigger than %d MB" % (settings.HUB_MAX_BYTES >> 20)}
    if not ID_RE.fullmatch(item_id):
        return 404, {"error": "not found"}
    if stored_bytes() + length > settings.HUB_QUOTA_BYTES:
        log.warning("accounts: the hub is out of room (FORZA_HUB_QUOTA_GB)")
        return 503, {"error": "no room for uploads right now: try again later"}
    # Claim the draft: one upload at a time, and only the owner's.
    claimed = HubItem.objects.filter(pk=item_id, owner=link.xuid, state=HubItem.DRAFT).update(
        state=HubItem.UPLOADING, upload_size=length, updated=timezone.now())
    if not claimed:
        if not HubItem.objects.filter(pk=item_id, owner=link.xuid).exists():
            return 404, {"error": "not found"}
        return 409, {"error": "this one has its file already"}
    folders()
    part = settings.HUB_INCOMING / (item_id + ".part")
    try:
        digest, size = receive(request, part, length)
    except UploadRefused as e:
        part.unlink(missing_ok=True)
        HubItem.objects.filter(pk=item_id, state=HubItem.UPLOADING).update(state=HubItem.DRAFT, upload_size=0)
        return e.status, {"error": e.error}
    except OSError:
        part.unlink(missing_ok=True)
        HubItem.objects.filter(pk=item_id, state=HubItem.UPLOADING).update(state=HubItem.DRAFT, upload_size=0)
        log.exception("accounts: hub upload %s could not be written", item_id)
        return 503, {"error": "the file could not be kept: try again later"}
    item = HubItem.objects.get(pk=item_id)
    zip_path = settings.HUB_INCOMING / (item_id + ".zip")
    os.replace(part, zip_path)
    job = {"version": 1, "id": item_id, "kind": item.kind, "title": item.title, "size": size, "sha256": digest}
    write_atomic(settings.HUB_INCOMING / (item_id + ".json"), json.dumps(job).encode())
    item.state, item.upload_sha256, item.upload_size, item.updated = HubItem.QUEUED, digest, size, timezone.now()
    item.save(update_fields=["state", "upload_sha256", "upload_size", "updated"])
    log.info("accounts: account %s uploaded hub item %s (%d bytes)", user.public_id, item_id, size)
    return {"ok": True, "state": item.state}


class UploadRefused(Exception):
    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


def receive(request, path, length):
    """Streams exactly `length` bytes of the request's body to `path` (made here, never there
    before): (SHA-256, size). It is never held in memory whole, and never parsed."""
    h, n = hashlib.sha256(), 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as out:
        while n < length:
            chunk = request.read(min(CHUNK, length - n))
            if not chunk:
                raise UploadRefused(400, "the upload stopped before its end")
            if n == 0 and not chunk.startswith(ZIP_MAGIC[:len(chunk)]):
                raise UploadRefused(415, "that is not a .zip file")
            n += len(chunk)
            h.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return h.hexdigest(), n


def write_atomic(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---- The worker's results ----

def read_result(path, item_id):
    """The worker's result for an item, checked, or None."""
    try:
        with open_plain(path) as f:
            data = f.read(RESULT_MAX + 1)
        if len(data) > RESULT_MAX:
            raise ValueError("too big")
        doc = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, HubError) as e:
        log.warning("accounts: hub result %s unreadable: %s", item_id, e)
        return None
    if not (isinstance(doc, dict) and doc.get("version") == 1 and doc.get("id") == item_id
            and isinstance(doc.get("ok"), bool)):
        log.warning("accounts: hub result %s is not a result", item_id)
        return None
    if doc["ok"] and not (isinstance(doc.get("sha256"), str) and SHA_RE.fullmatch(doc["sha256"])
                          and isinstance(doc.get("size"), int) and doc["size"] > 0
                          and isinstance(doc.get("files"), int) and isinstance(doc.get("unpacked"), int)
                          and isinstance(doc.get("folder"), str) and FOLDER_RE.fullmatch(doc["folder"])):
        log.warning("accounts: hub result %s says ok without its facts", item_id)
        return None
    return doc


def collect(force=False):
    """Reads the worker's results for the queued items (at most every few seconds unless
    force): refused ones say why, clean ones wait for a maintainer. The number collected."""
    if not settings.HUB:
        return 0
    now = time.monotonic()
    if not force and now - _collected[0] < 5:
        return 0
    _collected[0] = now
    done = 0
    for item in HubItem.objects.filter(state=HubItem.QUEUED):
        path = settings.HUB_CHECKED / (item.id + ".json")
        if not path.exists():
            continue
        doc = read_result(path, item.id)
        if doc is None:
            continue
        if doc["ok"] and HubItem.objects.filter(sha256=doc["sha256"]).exclude(pk=item.pk).exclude(
                state__in=(HubItem.REMOVED, HubItem.REFUSED)).exists():
            doc = dict(doc, ok=False, error=False, reason="the same file is on the hub already")
        with transaction.atomic():
            if doc["ok"]:
                manifest = doc.get("manifest") if isinstance(doc.get("manifest"), dict) else {}
                names = [n for n in doc.get("names", []) if isinstance(n, str)][:500] \
                    if isinstance(doc.get("names"), list) else []
                item.state, item.sha256, item.file_size = HubItem.REVIEW, doc["sha256"], doc["size"]
                item.files, item.unpacked, item.folder = doc["files"], doc["unpacked"], doc["folder"]
                item.manifest = json.dumps({k: clean(str(v), 1000, lines=True) for k, v in manifest.items()
                                            if k in ("name", "version", "author", "description")})
                item.names = json.dumps([clean(n, 255) for n in names])
                item.reason = ""
            else:
                item.state = HubItem.REFUSED
                reason = clean(str(doc.get("reason") or "it did not pass the checks"), 280)
                item.reason = ("It could not be checked: %s." if doc.get("error") else "It was refused: %s.") % reason
            item.scanner = clean(str(doc.get("scanner") or ""), 80)
            item.updated = timezone.now()
            item.save()
        for name in (item.id + ".zip", item.id + ".json"):
            (settings.HUB_INCOMING / name).unlink(missing_ok=True)
        if not doc["ok"]:
            for name in (item.id + ".zip", item.id + ".json"):
                (settings.HUB_CHECKED / name).unlink(missing_ok=True)
        else:
            (settings.HUB_CHECKED / (item.id + ".json")).unlink(missing_ok=True)
        log.info("accounts: hub item %s %s", item.id, "is waiting for a maintainer" if doc["ok"] else
                 "was refused" + (" (malware: %s)" % clean(str(doc.get("infected")), 120) if doc.get("infected") else ""))
        done += 1
    return done


# ---- The uploader's own items ----

@api.get("/hub/mine", response={200: MineOut, 403: ErrorOut, 409: ErrorOut}, auth=django_auth)
def hub_mine(request):
    on()
    from members import likes
    link = likes.usable_link(request.auth)
    collect()
    totals = {kind: likes.snapshot(kind)["counts"] for kind in (HubItem.MOD, HubItem.PACK)}
    rows = HubItem.objects.filter(owner=link.xuid).exclude(state=HubItem.REMOVED)
    return {"items": [{"id": h.id, "kind": h.kind, "title": h.title, "version": h.version, "state": h.state,
                       "reason": h.reason, "created": int(h.created.timestamp()), "size": h.file_size or h.upload_size,
                       "sha256": h.sha256, "likes": totals[h.kind].get(h.id, 0) if h.state == HubItem.LIVE else 0}
                      for h in rows],
            "max_bytes": settings.HUB_MAX_BYTES}


@api.post("/hub/items/{item_id}/delete", response={200: OkOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
          auth=django_auth)
def hub_delete(request, item_id: str, body: EmptyIn):
    on()
    from members.likes import usable_link
    user = request.auth
    if not recently_signed_in(request):
        return 403, {"error": "sign in again"}
    link = usable_link(user)
    item = HubItem.objects.filter(pk=item_id, owner=link.xuid).exclude(state=HubItem.REMOVED).first() \
        if ID_RE.fullmatch(item_id) else None
    if item is None:
        return 404, {"error": "not found"}
    if item.state == HubItem.UPLOADING:
        return 409, {"error": "it is still uploading"}
    take_down(item, "its uploader")
    AuditEvent.objects.create(user=user, event="deleted the %s %s" % (item.get_kind_display(), item.id))
    log.info("accounts: account %s deleted hub item %s", user.public_id, item.id)
    return {"ok": True}


# ---- Reports ----

@api.post("/hub/reports", response={200: OkOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut, 429: ErrorOut},
          auth=django_auth)
def hub_report(request, body: ReportIn):
    on()
    from members import likes
    user = request.auth
    link = likes.usable_link(user)
    item = HubItem.objects.filter(pk=body.item, state=HubItem.LIVE).first()
    if item is None:
        return 404, {"error": "not found"}
    if item.owner == link.xuid:
        return 403, {"error": "your own"}
    links.spend(request, "hub_report", "a:%d" % user.pk)
    try:
        with transaction.atomic():
            HubReport.objects.create(item=item, xuid=link.xuid, reason=body.reason, note=clean(body.note, 500, lines=True),
                                     at=timezone.now())
    except IntegrityError:
        return 409, {"error": "you reported it already"}
    counted = HubReport.objects.filter(item=item).values_list("xuid", flat=True)
    pins = dict(likes.GameLink.objects.filter(xuid__in=list(counted), state=likes.GameLink.ACTIVE,
                                              user__is_active=True).values_list("xuid", "pub"))
    weight = sum(1 for x in counted if x in pins and game.counted(x, pins[x]))
    HubItem.objects.filter(pk=item.pk).update(reports=len(counted))
    # A malware report hides it at once, but only from a key whose likes count (not a new one).
    trusted = link.xuid in pins and game.counted(link.xuid, pins[link.xuid])
    if (body.reason == HubReport.MALWARE and trusted) or weight >= settings.HUB_REPORTS_HIDE:
        hide(item, "held after a report: a maintainer will check it")
        log.warning("accounts: hub item %s is hidden after a report (%s)", item.id, body.reason)
    log.info("accounts: account %s reported hub item %s (%s)", user.public_id, item.id, body.reason)
    return {"ok": True}


# ---- The maintainer's steps (members.admin, the hub command) ----

def copy_checked(src, dest, sha256):
    """Copies src to dest (made atomically), checking that its SHA-256 is `sha256`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    h = hashlib.sha256()
    with open_plain(src) as f, open(tmp, "wb") as out:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if h.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        try:
            dest.parent.rmdir()
        except OSError:
            pass
        raise HubError("the clean zip's SHA-256 is not the one the worker gave: it was not published")
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)


def public_path(item):
    return settings.HUB_PUBLIC / item.sha256 / (item.folder + ".zip")


def publish(item):
    copy_checked(settings.HUB_CHECKED / (item.id + ".zip"), public_path(item), item.sha256)


def unpublish(item):
    """Deletes the item's public copy, unless another live item is served from the same file."""
    if not (item.sha256 and SHA_RE.fullmatch(item.sha256)):
        return
    if HubItem.objects.filter(sha256=item.sha256, state=HubItem.LIVE).exclude(pk=item.pk).exists():
        return
    shutil.rmtree(settings.HUB_PUBLIC / item.sha256, ignore_errors=True)


def approve(item, by):
    if item.state != HubItem.REVIEW:
        raise HubError("%s is %s: only an item waiting for review can be approved" % (item.id, item.state))
    publish(item)
    item.state, item.reviewed_by, item.published, item.updated = HubItem.LIVE, by[:150], timezone.now(), timezone.now()
    item.save(update_fields=["state", "reviewed_by", "published", "updated"])
    forget()
    log.info("accounts: %s approved hub item %s", by, item.id)


def refuse(item, by, why="a maintainer did not approve it"):
    if item.state not in (HubItem.REVIEW, HubItem.HIDDEN):
        raise HubError("%s is %s" % (item.id, item.state))
    unpublish(item)
    for name in (item.id + ".zip", item.id + ".json"):
        (settings.HUB_CHECKED / name).unlink(missing_ok=True)
    item.state, item.reason, item.reviewed_by, item.updated = HubItem.REFUSED, "It was refused: %s." % clean(why, 250), \
        by[:150], timezone.now()
    item.save(update_fields=["state", "reason", "reviewed_by", "updated"])
    Like.objects.filter(kind=item.kind, item=item.id).delete()
    forget()
    log.info("accounts: %s refused hub item %s", by, item.id)


def hide(item, why="hidden by a maintainer"):
    if item.state != HubItem.LIVE:
        return False
    unpublish(item)
    HubItem.objects.filter(pk=item.pk).update(state=HubItem.HIDDEN, reason=clean(why, 280), updated=timezone.now())
    item.state = HubItem.HIDDEN
    forget()
    return True


def unhide(item, by):
    if item.state != HubItem.HIDDEN:
        raise HubError("%s is %s, not hidden" % (item.id, item.state))
    publish(item)
    HubItem.objects.filter(pk=item.pk).update(state=HubItem.LIVE, reason="", reviewed_by=by[:150], updated=timezone.now())
    item.state = HubItem.LIVE
    forget()
    log.info("accounts: %s showed hub item %s again", by, item.id)


def take_down(item, by):
    """Removes an item's files (its record stays, as REMOVED, for the takedown's record) and its
    likes."""
    unpublish(item)
    for folder in (settings.HUB_INCOMING, settings.HUB_CHECKED):
        for name in (item.id + ".zip", item.id + ".json", item.id + ".part"):
            (folder / name).unlink(missing_ok=True)
    HubItem.objects.filter(pk=item.pk).update(state=HubItem.REMOVED, updated=timezone.now(),
                                              reason="Taken down by %s." % by[:60])
    item.state = HubItem.REMOVED
    Like.objects.filter(kind=item.kind, item=item.id).delete()
    forget()
    log.info("accounts: hub item %s taken down by %s", item.id, by)


def take_down_all(xuid):
    """Every item of a key (its account was deleted)."""
    for item in HubItem.objects.filter(owner=xuid).exclude(state=HubItem.REMOVED):
        take_down(item, "its uploader")


def upkeep(now=None):
    """nightly: drafts with no file go after DRAFT_HOURS; uploads that never finished go back to
    draft after UPLOADING_HOURS; results are collected. The numbers."""
    if not settings.HUB:
        return {}
    now = now or timezone.now()
    stale = HubItem.objects.filter(state=HubItem.UPLOADING, updated__lt=now - timedelta(hours=UPLOADING_HOURS))
    for item in stale:
        (settings.HUB_INCOMING / (item.id + ".part")).unlink(missing_ok=True)
    reset = stale.update(state=HubItem.DRAFT, upload_size=0)
    drafts = HubItem.objects.filter(state=HubItem.DRAFT, updated__lt=now - timedelta(hours=DRAFT_HOURS)).delete()[0]
    orphans = remove_orphans()
    waiting = HubItem.objects.filter(state=HubItem.QUEUED, updated__lt=now - timedelta(days=1)).count()
    if waiting:
        log.warning("accounts: %d hub upload(s) waited over a day for the scanning worker: is it running?", waiting)
    return {"collected": collect(force=True), "drafts_deleted": drafts, "uploads_reset": reset, "waiting": waiting,
            "orphans_deleted": orphans}


def remove_orphans():
    """Files in incoming/ and checked/ whose item no longer needs them (deleted while the worker
    had it, refused, or gone), and public copies no live item is served from. The number."""
    keep_in = set(HubItem.objects.filter(state__in=(HubItem.UPLOADING, HubItem.QUEUED)).values_list("pk", flat=True))
    keep_checked = set(HubItem.objects.filter(state__in=(HubItem.QUEUED, HubItem.REVIEW, HubItem.LIVE, HubItem.HIDDEN))
                       .values_list("pk", flat=True))
    live = set(HubItem.objects.filter(state=HubItem.LIVE).values_list("sha256", flat=True))
    gone = 0
    for folder, keep in ((settings.HUB_INCOMING, keep_in), (settings.HUB_CHECKED, keep_checked)):
        for p in folder.iterdir() if folder.is_dir() else []:
            if p.name.split(".", 1)[0] not in keep and time.time() - p.lstat().st_mtime > 3600:
                p.unlink(missing_ok=True)
                gone += 1
    for p in settings.HUB_PUBLIC.iterdir() if settings.HUB_PUBLIC.is_dir() else []:
        if p.name not in live:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            gone += 1
    return gone


def open_plain(path):
    """A file the worker wrote, opened only when it is a plain file (never through a link)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HubError("%s is not a plain file" % Path(path).name)
    return os.fdopen(fd, "rb")


def checked_path(item):
    p = settings.HUB_CHECKED / (item.id + ".zip")
    try:
        return p if ID_RE.fullmatch(item.id) and stat.S_ISREG(os.lstat(p).st_mode) else None
    except OSError:
        return None
