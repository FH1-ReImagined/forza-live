"""Forza Live: players delete their own community photos and designs from the website.

The website accounts service (accounts/, website #4) knows which game key a signed-in account is
linked to. When the player deletes one of their items on the account page, the service asks this
route to delete it, naming the item's owner:

  POST /owner/photos/<id>/delete       {"owner": "<the owner's XUID, 16 hex digits>"}
  POST /owner/designs/<webId>/delete   the same

Rules:
- The X-Owner-Token header must be FORZA_OWNER_TOKEN, compared in constant time. It is a token
  of its own, not FORZA_ADMIN_TOKEN. Without FORZA_OWNER_TOKEN set, the routes are off (404).
- Never through the tunnel or a reverse proxy (lsp.came_through_proxy: the token would cross
  it in plain text), and never from a browser page (any Origin header is refused). The accounts
  service reaches the game server directly, from its own network.
- The item must be the owner's: another player's item answers 404, as an id that does not
  exist does, so the route tells nothing about other players' items.
- A photo is dropped as an operator's delete drops it (community.drop), but its picture is not
  put on the list of pictures never published again: it is the player's own to upload again. A
  design goes as the storefront's own RemoveFile takes it (designs.remove).

The token can do nothing else: it cannot hide, release, ban, read held items or reach the
operator's other routes.

A banned owner (core/bans.py) deletes nothing: its items stay, hidden, for the operator to
review, and come back if the ban is lifted.

Answers: 200 {"deleted": "<id>"}; 400 a body that is not {"owner": <16 hex>}; 403 a browser,
a proxy, the wrong token or a banned owner; 404 off, unknown, or another player's; 405 not a
POST.
"""

import hmac
import json
import logging
import os
import re
import urllib.parse

from forza_live.content import community
from forza_live.content import design_feed
from forza_live.content import designs
from forza_live.core import bans

log = logging.getLogger("forza-live")

PREFIX = "/owner/"
OWNER_TOKEN = os.environ.get("FORZA_OWNER_TOKEN", "")
OWNER_RE = re.compile(r"[0-9A-Fa-f]{16}")
ROUTE_RE = re.compile(r"/owner/(photos|designs)/([A-Z2-7]{12})/delete")
MAX_BODY = 1024


def handles(path):
    return urllib.parse.urlsplit(path).path.startswith(PREFIX)


def reply(status, obj):
    return community.reply(status, obj)


def handle(method, path, headers, body, proxied):
    """(status, content type, body, extra headers) for /owner/...; proxied: the request came
    through the tunnel or a reverse proxy (lsp.came_through_proxy)."""
    if not OWNER_TOKEN:
        return reply(404, {"error": "not found"})
    if "origin" in headers:
        return reply(403, {"error": "refused"})
    if proxied:
        return reply(403, {"error": "refused"})
    given = headers.get("x-owner-token", "")
    if not hmac.compare_digest(given.encode(), OWNER_TOKEN.encode()):
        return reply(403, {"error": "refused"})
    if method != "POST":
        return reply(405, {"error": "POST only"})
    m = ROUTE_RE.fullmatch(urllib.parse.urlsplit(path).path)
    if m is None:
        return reply(404, {"error": "not found"})
    try:
        doc = json.loads(body[:MAX_BODY + 1]) if len(body) <= MAX_BODY else None
    except ValueError:
        doc = None
    owner = doc.get("owner") if isinstance(doc, dict) else None
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        return reply(400, {"error": "a JSON object, {\"owner\": \"<16 hex digits>\"}"})
    xuid = int(owner, 16)
    if bans.refused(xuid, "website delete"):  # a banned player's items stay for the operator
        return reply(403, {"error": "banned from this server"})
    kind, item = m.groups()
    if kind == "photos":
        return delete_photo(item, xuid)
    return delete_design(item, xuid)


def delete_photo(pid, xuid):
    p = community.load()["photos"].get(pid)
    if p is None or p["owner"] != xuid:
        return reply(404, {"error": "no such photo"})
    community.drop(pid, "deleted by its owner from the website")
    community.save()
    return reply(200, {"deleted": pid})


def delete_design(wid, xuid):
    found = design_feed.by_web_id(wid)
    if found is None or not found[1]["done"] or found[1]["owner"] != xuid:
        return reply(404, {"error": "no such design"})
    fid, f = found
    designs.remove(fid)
    designs.save()
    log.info("designs: %s %d (%s, %016X) deleted by its owner from the website", f["kind"], fid,
             wid, xuid)
    return reply(200, {"deleted": wid})
