"""Likes, and the player's own photos and designs (community accounts design, sections 4.1-4.3).

  POST /api/likes                {"kind": "photo"|"design", "item": "<id>", "on": true|false}
  GET  /api/likes/mine?kind=     the items this key liked, and its own public ones
  GET  /api/likes/counts?kind=   anyone: the public totals, {"counts": {"<id>": n}}
  GET  /api/me/items             the key's photos and designs, public or not, with their state
  POST /api/me/items/delete      {"kind", "id"}: needs a sign-in in the past 10 minutes

Everything but the totals needs a signed-in account linked to its game, with the link usable:
active, the key the XUID is bound to still the pinned one (else the link freezes), and the XUID
not banned. A like belongs to the key (the XUID), one per item, so a second account or a link
that moved gains nothing. Only items in the public feeds can be liked (members.items), and never
the key's own.

A like counts in the public totals when the key that gave it counts (members.game.counted):
its link active and pinned, its account not suspended, not banned, and its first signed sign-in on the game server
LIKES_AFTER_HOURS old with signed sign-ins on LIKES_SIGNED_DAYS days. Who liked what is never
shown. The totals are counted again at most every LIKE_COUNTS_SECONDS in each worker; the
website's builder copies them into the pages, and signed-in visitors' pages ask for them live.

Deleting an item asks the game server's owner-only route (forza_live/content/owners.py,
FORZA_GAME_URL and FORZA_OWNER_TOKEN), which checks the owner again; the item's likes go with it.
"""

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import List, Literal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from ninja.security import django_auth
from pydantic import Field

from members import game, items, limits, links
from members.api import ErrorOut, Strict, api, recently_signed_in
from members.game import Refused
from members.models import AuditEvent, GameLink, Like

log = logging.getLogger("members")

Kind = Literal["photo", "design"]
State = Literal["live", "pending", "held", "hidden"]
COUNTS_MAX_AGE = 30  # Cache-Control of the public totals

_counts_lock = threading.Lock()
_counts = {}  # kind -> {"at", "counts", "body", "etag"}


# ---- Schemas ----

class LikeIn(Strict):
    kind: Kind
    item: str = Field(pattern=items.ITEM_ID)
    on: bool


class LikeOut(Strict):
    ok: bool
    on: bool
    counted: bool  # this key's likes are in the public totals (else they will be, later)


class MineOut(Strict):
    kind: Kind
    liked: List[str]
    own: List[str]  # the key's own public items: they cannot be liked
    counted: bool


class ItemOut(Strict):
    kind: Kind
    id: str
    title: str
    state: State
    public: bool
    likes: int = Field(ge=0)
    created: int


class ItemsOut(Strict):
    items: List[ItemOut]
    counted: bool


class ItemIn(Strict):
    kind: Kind
    id: str = Field(pattern=items.ITEM_ID)


class DeletedOut(Strict):
    ok: bool
    deleted: bool


# ---- The link, and the totals ----

def usable_link(user):
    """The account's link when its key may like and manage its items, else Refused."""
    if not game.available():
        raise Refused(503, "not available")
    link = GameLink.objects.filter(user=user).first()
    if link is None:
        raise Refused(403, "link your game first")
    xuid = int(link.xuid, 16)
    pub = game.bound_pub(xuid)
    if link.state == GameLink.FROZEN or (pub is not None and not links.pinned(link, pub)):
        raise Refused(409, "this link needs the operator")
    if pub is None:
        raise Refused(409, "play online first")  # the game server forgot the key: it waits
    if game.banned(xuid):
        raise Refused(403, "not available")
    return link


def counts_of(kind, now=None):
    """{item id: likes that count} of the public items of a kind."""
    index = items.index()
    # A suspended account's key (members.moderation) is left out.
    pins = dict(GameLink.objects.filter(state=GameLink.ACTIVE, user__is_active=True).values_list("xuid", "pub"))
    keys, totals = {}, Counter()
    for xuid, item in Like.objects.filter(kind=kind).values_list("xuid", "item").iterator(chunk_size=10000):
        found = index.items.get((kind, item))
        if found is None or not found.public or found.owner == xuid:
            continue
        ok = keys.get(xuid)
        if ok is None:
            pin = pins.get(xuid)
            ok = keys[xuid] = pin is not None and game.counted(xuid, pin, now)
        if ok:
            totals[item] += 1
    return dict(totals)


def snapshot(kind):
    """The public totals of a kind, counted again when LIKE_COUNTS_SECONDS passed."""
    with _counts_lock:
        snap = _counts.get(kind)
        if snap is None or time.monotonic() - snap["at"] >= settings.LIKE_COUNTS_SECONDS:
            counts = dict(sorted(counts_of(kind).items()))
            body = json.dumps({"version": 1, "kind": kind, "generated": int(time.time()), "counts": counts},
                              separators=(",", ":")).encode()
            etag = '"%s"' % hashlib.sha256(json.dumps(counts).encode()).hexdigest()[:32]
            snap = _counts[kind] = {"at": time.monotonic(), "counts": counts, "body": body, "etag": etag}
        return snap


def forget_counts():
    with _counts_lock:
        _counts.clear()


def counted(link):
    return link.state == GameLink.ACTIVE and game.counted(link.xuid, link.pub)


# ---- Likes ----

@api.get("/likes/counts", auth=None)
def like_counts(request, kind: Kind):
    if not game.available():
        raise Refused(503, "not available")
    snap = snapshot(kind)
    if request.headers.get("If-None-Match") == snap["etag"]:
        response = HttpResponse(status=304)
    else:
        response = HttpResponse(snap["body"], content_type="application/json")
    response["ETag"] = snap["etag"]
    response["Cache-Control"] = "public, max-age=%d" % COUNTS_MAX_AGE  # kept by ApiHeaders
    return response


@api.post("/likes", response={200: LikeOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut, 429: ErrorOut},
          auth=django_auth)
def like(request, body: LikeIn):
    user = request.auth
    link = usable_link(user)
    found = items.find(body.kind, body.item)
    if body.on:
        if found is None or not found.public:
            raise Refused(404, "not found")
        if found.owner == link.xuid:
            raise Refused(403, "your own")
    links.spend(request, "like", "a:%d" % user.pk)
    if body.on:
        try:
            with transaction.atomic():
                Like.objects.get_or_create(xuid=link.xuid, kind=body.kind, item=body.item,
                                           defaults={"at": timezone.now()})
        except IntegrityError:
            pass  # the same like from another request at the same moment
    else:
        Like.objects.filter(xuid=link.xuid, kind=body.kind, item=body.item).delete()
    return {"ok": True, "on": body.on, "counted": counted(link)}


@api.get("/likes/mine", response={200: MineOut, 403: ErrorOut, 409: ErrorOut}, auth=django_auth)
def likes_mine(request, kind: Kind):
    link = usable_link(request.auth)
    liked = sorted(Like.objects.filter(xuid=link.xuid, kind=kind).values_list("item", flat=True))
    own = [i.id for i in items.index().by_owner.get(link.xuid, []) if i.kind == kind and i.public]
    return {"kind": kind, "liked": liked, "own": own, "counted": counted(link)}


# ---- My items ----

@api.get("/me/items", response={200: ItemsOut, 403: ErrorOut, 409: ErrorOut}, auth=django_auth)
def my_items(request):
    link = usable_link(request.auth)
    totals = {kind: snapshot(kind)["counts"] for kind in items.KINDS}
    mine = items.index().by_owner.get(link.xuid, [])
    return {"items": [{"kind": i.kind, "id": i.id, "title": i.title, "state": i.state, "public": i.public,
                       "likes": totals[i.kind].get(i.id, 0) if i.public else 0, "created": i.created}
                      for i in mine],
            "counted": counted(link)}


def owner_delete(kind, item_id, xuid_hex):
    """The game server's owner-only delete route: its HTTP status, or None when it could not be
    reached. Straight to FORZA_GAME_URL: no proxy from the environment, no redirect."""
    path = "/owner/%s/%s/delete" % ("photos" if kind == items.PHOTO else "designs", item_id)
    req = urllib.request.Request(settings.GAME_URL + path, method="POST",
                                 data=json.dumps({"owner": xuid_hex}).encode(),
                                 headers={"Content-Type": "application/json", "X-Owner-Token": settings.OWNER_TOKEN})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(req, timeout=settings.GAME_TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@api.post("/me/items/delete", response={200: DeletedOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut,
                                        429: ErrorOut, 502: ErrorOut, 503: ErrorOut}, auth=django_auth)
def delete_item(request, body: ItemIn):
    user = request.auth
    if not recently_signed_in(request):
        return 403, {"error": "sign in again"}
    link = usable_link(user)
    found = items.find(body.kind, body.id)
    if found is None or found.owner != link.xuid:
        raise Refused(404, "not found")
    if not (settings.GAME_URL and settings.OWNER_TOKEN):
        raise Refused(503, "deleting is not available")
    links.spend(request, "item_delete", "a:%d" % user.pk)
    status = owner_delete(body.kind, body.id, link.xuid)
    if status == 404:
        items.forget()
        raise Refused(404, "not found")
    if status != 200:
        log.warning("accounts: the game server did not delete %s %s for account %s (%s)", body.kind, body.id,
                    user.public_id, status if status is not None else "unreachable")
        raise Refused(502, "try again later")
    with transaction.atomic():
        Like.objects.filter(kind=body.kind, item=body.id).delete()
        AuditEvent.objects.create(user=user, event="deleted the %s %s" % (
            body.kind, "“%s”" % found.title if found.title else body.id))
    items.forget()
    forget_counts()
    log.info("accounts: account %s deleted its %s %s", user.public_id, body.kind, body.id)
    return {"ok": True, "deleted": True}
