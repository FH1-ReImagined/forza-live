"""The community photos and designs, as the game server keeps them, read-only.

The game server writes DATA_DIR/community.json (forza_live.content.community) and
DATA_DIR/designs.json (forza_live.content.designs) with forza_live.core.durable.replace, so a
reader always sees a whole file. This service reads them again when they change (and at least
once a minute, as gamertags change without them), with the game server's own rules:
- a photo is public (in the gallery feed the website is built from) when community.public says
  so: live, its owner not banned; the newest community.FEED_MAX of them, in the feed's order;
- a design when design_feed.on_site says so: listed in the game, clear, with a converted preview
  and a gamertag to credit; the newest design_feed.FEED_MAX.

Only public items can be liked, and only they are counted. Every item, public or not, is listed
to its owner (members.likes, "My items"), with its state: live, pending (waiting for the
screening), held (left for the operator) or hidden. The game server's own modules are never
asked to load these files (designs.load would write web states into them): they are read here
and only the game server's pure rules are applied to them.
"""

import os
import threading
import time
from typing import NamedTuple

from django.conf import settings

from forza_live.content import community, design_feed, designs
from forza_live.core import durable, limits

from members import game

PHOTO, DESIGN = "photo", "design"
KINDS = (PHOTO, DESIGN)
ITEM_ID = r"^[A-Z2-7]{12}$"  # community.ID_RE, designs.WEB_ID_CHARS and WEB_ID_LEN
REFRESH_SECONDS = 60

_lock = threading.Lock()
_cache = {"key": None, "index": None}


class Item(NamedTuple):
    kind: str
    id: str
    owner: str        # the owner's XUID, 16 upper-case hex digits (never sent to a browser)
    title: str
    state: str        # live, pending, held or hidden
    public: bool      # in the public feed: can be liked, and its likes count
    created: int      # when it was uploaded (unix time)


class Index(NamedTuple):
    items: dict       # (kind, id) -> Item
    by_owner: dict    # XUID hex -> [Item], newest first


def _mtime(name):
    try:
        return os.stat(os.path.join(str(settings.GAME_DATA), name)).st_mtime_ns
    except OSError:
        return None


def _read(name, default):
    try:
        doc = durable.load(os.path.join(str(settings.GAME_DATA), name))
    except (OSError, ValueError):
        return default
    return doc if isinstance(doc, dict) else default


def _photos(bans):
    photos = _read("community.json", {}).get("photos")
    out, live = [], []
    for pid, p in (photos if isinstance(photos, dict) else {}).items():
        try:
            owner, status = int(p["owner"]), p["status"]
            created = int(p.get("created") or p.get("takenAt") or 0)  # when it was uploaded
        except (KeyError, TypeError, ValueError):
            continue
        if status not in (community.LIVE, community.HELD, community.HIDDEN):
            continue
        if community.public(p, bans):
            live.append((p.get("approvedAt") or 0, p.get("created") or 0, pid))
        out.append((pid, owner, limits.clean_ascii_text(p.get("title") or "", community.TITLE_MAX), status,
                    created))
    live.sort(reverse=True)  # community.feed's order
    in_feed = {pid for _, _, pid in live[:community.FEED_MAX]}
    return [Item(PHOTO, pid, designs.hx(owner), title, status, pid in in_feed, created)
            for pid, owner, title, status, created in out]


def _design_state(f):
    if f.get("web") == designs.HIDDEN or f.get("status") == designs.HIDDEN:
        return "hidden"
    return {designs.CLEAR: "live", designs.HELD: "held"}.get(f.get("web"), "pending")


def _designs(bans):
    files = _read("designs.json", {}).get("files")
    rows, credits = [], {}
    for fid, f in (files if isinstance(files, dict) else {}).items():
        try:
            if not f["done"] or not isinstance(f.get("webId"), str):
                continue
            owner, created = int(f["owner"]), int(f["created"])
            title = limits.clean_ascii_text(f["meta"]["title"], 16)
            shown = bool(design_feed.on_site(f, bans, credits))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((created, int(fid), f["webId"], owner, title, _design_state(f), shown))
    rows.sort(reverse=True)  # designs.files' order: newest first
    in_feed, out = 0, []
    for created, _, wid, owner, title, state, shown in rows:
        public = shown and in_feed < design_feed.FEED_MAX
        in_feed += shown
        out.append(Item(DESIGN, wid, designs.hx(owner), title, state, public, created))
    return out


def index():
    """Every photo and design (Index), read again when a file or the bans changed, or a minute
    passed."""
    game.configure()
    bans = designs.banned_xuids()
    key = (_mtime("community.json"), _mtime("designs.json"), bans, int(time.time() // REFRESH_SECONDS))
    with _lock:
        if _cache["key"] != key:
            items, by_owner = {}, {}
            for item in _photos(bans) + _designs(bans):
                items[(item.kind, item.id)] = item
                by_owner.setdefault(item.owner, []).append(item)
            for mine in by_owner.values():
                mine.sort(key=lambda i: i.created, reverse=True)
            _cache.update({"key": key, "index": Index(items, by_owner)})
        return _cache["index"]


def find(kind, item_id):
    return index().items.get((kind, item_id))


def present():
    """{kind: the ids of every photo or design in the game server's file, whatever its state, or
    None when the file is missing or damaged (and so is its .bak)}: for members.moderation's
    purge of the likes of items that are gone."""
    photos = (_read("community.json", None) or {}).get("photos")
    files = (_read("designs.json", None) or {}).get("files")
    return {
        PHOTO: set(photos) if isinstance(photos, dict) else None,
        DESIGN: {f["webId"] for f in files.values() if isinstance(f, dict) and isinstance(f.get("webId"), str)}
        if isinstance(files, dict) else None,
    }


def forget():
    """The next index() reads the files again (after this service deleted an item)."""
    with _lock:
        _cache.update({"key": None, "index": None})
