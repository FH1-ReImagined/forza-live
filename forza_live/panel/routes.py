"""Forza Live: the operator panel, one web page for the operator's tools (docs/operating.md,
"Operator panel"). server.http_handler hands it every path under /operator.

    GET  /operator/                  the page (index.html, panel.js, panel.css from static/)
    GET  /operator/api/<read>        what the page shows
    POST /operator/api/<action>      a change, with a JSON body; each one is written to the audit
                                     log (audit.py)

Every action goes through the server's own operator code: lsp.broadcast_request, gift_request
and notify_request, scores.admin, community.admin_handle and design_feed.admin_handle, the chat's
admin, server.events.handle, server.world_request, relays.add_relay and remove_relay, and the
server list's checks (server_list.clean_entry). The panel adds what had no route: listing and
deleting broadcasts, a player's page, the server list's and the relays' editing, the audit log.

Who can reach it (all of it, the page too; anything else gets 404, so the panel looks absent):

- FORZA_ADMIN_TOKEN is set (without it the panel is off);
- the request did not come through the tunnel or a reverse proxy (lsp.came_through_proxy: a
  --trusted-proxy address, X-Forwarded-For, X-Real-IP or Forwarded), the rule of the photo and
  design moderation;
- it comes from a private or loopback address (LOCAL_NETWORKS): a server whose port is forwarded
  to the internet without a tunnel still keeps the panel to the home network.

Then every /operator/api/ request needs the admin token in the X-Admin-Token header (never in the
URL). The page keeps it in sessionStorage and sends it as that header: no cookies, so another
site's page cannot make the browser act with it (no CSRF). A request whose Origin is not the
panel's own, or whose Sec-Fetch-Site says another site, is refused, and a change must be
application/json. FAILS_PER_IP wrong tokens from one address in FAIL_WINDOW seconds (FAILS_TOTAL
from everyone) answer 429 to everything until the window has passed. Every answer has a strict
Content-Security-Policy (scripts and styles from the panel only), no-store and no framing; the
page builds everything it shows as text (names, titles and server names are players' and
owners' text)."""

import asyncio
import collections
import hmac
import ipaddress
import json
import logging
import os
import re
import time
import urllib.parse

from forza_live.content import community
from forza_live.content import design_feed
from forza_live.content import designs
from forza_live.content import market
from forza_live.competition import objectives
from forza_live.competition import score_token
from forza_live.competition import scores
from forza_live.core import durable
from forza_live.core import relays
from forza_live.core import server_list
from forza_live.panel import audit
from forza_live.social import clubs
from forza_live.social import discord_status
from forza_live.social import notifications
from forza_live.title_services import lsp

log = logging.getLogger("forza-live")

PREFIX = "/operator"
API = PREFIX + "/api/"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {PREFIX: "index.html", PREFIX + "/": "index.html", PREFIX + "/panel.js": "panel.js",
          PREFIX + "/panel.css": "panel.css"}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
HEADERS = ("Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; "
           "img-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'",
           "X-Content-Type-Options: nosniff", "X-Frame-Options: DENY",
           "Referrer-Policy: no-referrer", "Cache-Control: no-store",
           "Cross-Origin-Opener-Policy: same-origin", "Cross-Origin-Resource-Policy: same-origin")
LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10"))
FAIL_WINDOW = 600.0
FAILS_PER_IP = 10
FAILS_TOTAL = 100
MAX_BODY = 65536
# The most credits one gift from the panel may carry (FORZA_PANEL_GIFT_MAX).
GIFT_MAX = int(os.environ.get("FORZA_PANEL_GIFT_MAX", "1000000") or 1000000)
REASON_MAX = 200
TEXT_MAX = 512
# The website accounts service's Django admin, linked from the panel when set (never fetched).
ACCOUNTS_ADMIN = os.environ.get("FORZA_ACCOUNTS_ADMIN_URL", "").strip()
SEARCH_MAX = 50
CHECK_MAX = 50          # servers checked at once
PLAYER_SCAN_MAX = 20000  # player records a search reads

_fails = {"total": collections.deque(), "ip": {}}


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handles(path):
    p = urllib.parse.urlsplit(path).path
    return p == PREFIX or p.startswith(PREFIX + "/")


# ---- who may use it ----

def local_address(ip):
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return any(a in n for n in LOCAL_NETWORKS)


def reachable(peer, headers):
    """Whether the panel exists for this request (404 otherwise)."""
    return (bool(lsp.ADMIN_TOKEN) and bool(peer) and not lsp.came_through_proxy(peer, headers)
            and local_address(peer[0]))


def same_origin(headers):
    origin = headers.get("origin")
    site = headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    if origin is None:
        return True
    host = headers.get("host", "")
    return bool(host) and origin in ("http://" + host, "https://" + host)


def _prune(times, now):
    while times and now - times[0] > FAIL_WINDOW:
        times.popleft()


def locked(ip, now=None):
    now = time.monotonic() if now is None else now
    _prune(_fails["total"], now)
    mine = _fails["ip"].get(ip)
    if mine is not None:
        _prune(mine, now)
        if not mine:
            del _fails["ip"][ip]
            mine = None
    return len(_fails["total"]) >= FAILS_TOTAL or (mine is not None and len(mine) >= FAILS_PER_IP)


def failed(ip, now=None):
    now = time.monotonic() if now is None else now
    _fails["total"].append(now)
    if len(_fails["ip"]) > 10000:
        _fails["ip"].clear()
    _fails["ip"].setdefault(ip, collections.deque()).append(now)


def token_ok(headers):
    given = headers.get("x-admin-token", "")
    return bool(lsp.ADMIN_TOKEN) and hmac.compare_digest(given.encode(), lsp.ADMIN_TOKEN.encode())


# ---- answers ----

def reply(status, obj):
    return status, "application/json", json.dumps(obj, indent=1, default=str).encode(), HEADERS


def not_found():
    return 404, "text/plain", b"not found", ()


def static(path):
    name = STATIC.get(path)
    if name is None:
        return not_found()
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            data = f.read()
    except OSError:
        return not_found()
    return 200, CONTENT_TYPES[os.path.splitext(name)[1]], data, HEADERS


def decoded(answer):
    """(status, object) of an operator handler's (status, content type, body[, extra])."""
    status, ctype, data = answer[:3]
    if ctype == "application/json":
        try:
            return status, json.loads(data)
        except ValueError:
            pass
    text = data.decode("utf-8", "replace")
    return status, ({"error": text} if status >= 400 else {"result": text})


async def handle(server, method, path, headers, body, peer):
    """(status, content type, body, extra headers) for a path under /operator."""
    if not reachable(peer, headers):
        return not_found()
    url = urllib.parse.urlsplit(path)
    if not url.path.startswith(API):
        if method != "GET":
            return not_found()
        return static(url.path)
    ip = peer[0]
    if not same_origin(headers):
        return reply(403, {"error": "a request from another site's page"})
    if locked(ip):
        return reply(429, {"error": "too many wrong tokens: try again in a few minutes"})
    if not token_ok(headers):
        if headers.get("x-admin-token"):
            failed(ip)
            log.warning("operator panel: a wrong admin token from %s", ip)
        return reply(401, {"error": "the admin token (X-Admin-Token) is missing or wrong"})
    name = url.path[len(API):]
    query = urllib.parse.parse_qs(url.query)
    audit.configure(lsp.DATA_DIR)
    if method == "GET":
        route = match(READS, name)
        if route is None:
            return reply(404, {"error": "no such panel route"})
        fn, groups = route
        try:
            answer = fn(server, query, *groups)
        except Refused as e:
            return reply(e.status, {"error": str(e)})
        except Exception:  # a damaged data file must not end the connection without an answer
            log.exception("operator panel: %s failed", name)
            return reply(500, {"error": "the panel could not read that: see the server's log"})
        return answer if len(answer) == 4 else reply(*answer)
    if method != "POST":
        return reply(405, {"error": "GET or POST"})
    if headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return reply(415, {"error": "a change needs Content-Type: application/json"})
    if len(body) > MAX_BODY:
        return reply(413, {"error": "the request is too large"})
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return reply(400, {"error": "the body is not JSON"})
    if not isinstance(data, dict):
        return reply(400, {"error": "the body must be a JSON object"})
    route = match(ACTIONS, name)
    if route is None:
        return reply(404, {"error": "no such panel action"})
    (fn, action, target), groups = route
    try:
        result = fn(server, data, *groups)
        if asyncio.iscoroutine(result):
            result = await result
        status, obj = result
    except Refused as e:
        status, obj = e.status, {"error": str(e)}
    except (ValueError, TypeError, OverflowError) as e:
        status, obj = 400, {"error": str(e)}
    except Exception:  # an operator's mistake must not end the connection without an answer
        log.exception("operator panel: %s failed", name)
        status, obj = 500, {"error": "the action failed: see the server's log"}
    try:
        what = action(data, *groups) if callable(action) else action
    except Exception:  # a malformed body: the audit line is still written
        what = name
    try:
        aim = target(data, *groups)
    except Exception:
        aim = ""
    audit.record(what, aim, data, status, ip)
    return reply(status, obj)


def match(table, name):
    for pattern, value in table:
        m = re.fullmatch(pattern, name)
        if m:
            return value, m.groups()
    return None


# ---- helpers ----

def q1(query, name, default=""):
    return query.get(name, [default])[0]


def text(data, name, limit, required=False):
    value = data.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Refused(400, "%s: text" % name)
    value = value.strip()
    if required and not value:
        raise Refused(400, "%s is required" % name)
    if len(value) > limit:
        raise Refused(400, "%s: at most %d characters" % (name, limit))
    if any(ord(c) < 32 and c not in "\n" for c in value):
        raise Refused(400, "%s: control characters" % name)
    return value


def number(data, name, low, high, default=None):
    value = data.get(name, default)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise Refused(400, "%s: a whole number" % name)
    value = int(value)
    if not low <= value <= high:
        raise Refused(400, "%s: %s to %s" % (name, format(low, ","), format(high, ",")))
    return value


def xuid_of(value):
    """An XUID from hex text (16 digits, 0x optional), or None."""
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    v = v[2:] if v.startswith("0X") else v
    if re.fullmatch("[0-9A-F]{16}", v):
        return int(v, 16)
    return None


def hx(xuid):
    return "%016X" % xuid


def who_text(data, name="to"):
    who = text(data, name, 32, required=True)
    return who


def store_of(server):
    return server.friends.store


def gamertag(server, xuid):
    p = store_of(server).get(xuid)
    if p and p.get("name"):
        return p["name"]
    return lsp.load_player(xuid).get("gamertag", "")


# ---- status ----

def overview(server, query):
    status = server.status()
    return 200, {"status": status, "discord": discord_preview(status),
                 "panel": {"gift_max": GIFT_MAX, "accounts_admin": accounts_admin(),
                           "server_id": scores.server_id(),
                           "score_tokens": score_token.MODE,
                           "challenges": objectives.ENABLED,
                           "challenge_gifts": objectives.GIFTS,
                           "discord": discord_status.enabled()}}


def accounts_admin():
    url = ACCOUNTS_ADMIN
    if not url.startswith(("http://", "https://")) or any(c in url for c in "\"'<> \\"):
        return ""
    return url if url.endswith("/") else url + "/"


def discord_preview(status):
    """The Discord status message as the server would post it now (the community servers'
    states are the last ones the status loop fetched)."""
    try:
        players, worlds, found = discord_status.summary(status)
        return {"enabled": discord_status.enabled(), "text": discord_status.render(
            players, worlds, found=found, relays=discord_status.relay_states(status),
            community=discord_status._state.get("community") or (),
            challenges=discord_status.challenge_states())}
    except Exception as e:  # the preview never breaks the page
        return {"enabled": discord_status.enabled(), "text": "", "error": str(e)}


# ---- broadcasts and messages ----

def type_info():
    out = []
    for ntype, (name, record) in sorted(notifications.TYPES.items()):
        out.append({"type": "0x%04X" % ntype, "name": name,
                    "fields": [field for field, _ in record.sub]})
    return out


def text_types():
    return {ntype for ntype, (_, record) in notifications.TYPES.items()
            if [f for f, _ in record.sub] == ["Message"]}


def broadcast_list(server, query):
    out = []
    for b in reversed(notifications.broadcasts()):
        out.append({"id": b["id"], "type": "0x%04X" % b["type"],
                    "name": notifications.TYPES.get(b["type"], ("?",))[0],
                    "text": b["params"].get("Message", ""), "params": b["params"],
                    "sent": int(b["sent"]), "expires": int(b["expires"])})
    return 200, {"broadcasts": out, "types": type_info(),
                 "text_types": ["0x%04X" % t for t in sorted(text_types())],
                 "max_days": notifications.KEEP_DAYS}


def notification_type(data):
    raw = data.get("type", "0x0100")
    try:
        ntype = int(raw, 0) if isinstance(raw, str) else int(raw)
    except ValueError:
        raise Refused(400, "type: a notification type such as 0x0100") from None
    if ntype not in notifications.TYPES:
        raise Refused(400, "type: unknown notification type")
    return ntype


def notification_query(data, ntype):
    """The query fields of a notification (its text, or each field of its record)."""
    fields = data.get("fields")
    query = {}
    if ntype in text_types() or fields is None:
        query["Message"] = [text(data, "text", TEXT_MAX, required=True)]
    if isinstance(fields, dict):
        names = {f for f, _ in notifications.TYPES[ntype][1].sub}
        for k, v in fields.items():
            if k not in names:
                raise Refused(400, "fields: %s is not a field of this type" % k)
            if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                raise Refused(400, "fields: %s must be text or a number" % k)
            query[k] = [str(v)[:TEXT_MAX]]
    return query


def broadcast_send(server, data):
    ntype = notification_type(data)
    days = number(data, "days", 1, notifications.KEEP_DAYS, notifications.KEEP_DAYS)
    query = {"type": [str(ntype)], "days": [str(days)], **notification_query(data, ntype)}
    return decoded(lsp.broadcast_request(query))


def broadcast_delete(server, data):
    nid = number(data, "id", 1, 2 ** 31)
    if not notifications.delete_broadcast(nid):
        raise Refused(404, "no broadcast %d" % nid)
    return 200, {"deleted": nid}


def notify_send(server, data):
    if data.get("type") == "sample":  # one of each kind, to see how the game shows them
        return decoded(lsp.notify_request({"to": [who_text(data)], "type": ["sample"]}))
    ntype = notification_type(data)
    query = {"to": [who_text(data)], "type": [str(ntype)], **notification_query(data, ntype)}
    return decoded(lsp.notify_request(query))


# ---- gifts ----

def gift_record(g):
    out = {"id": g["id"], "type": g.get("type"), "to": g.get("recipient"),
           "sender": g.get("senderTag", ""), "sent": int(g.get("sent", 0)),
           "retrieved": bool(g.get("retrieved"))}
    if g.get("type") == "credits":
        out["credits"] = g.get("credits", 0)
    elif isinstance(g.get("car"), dict):
        out["car"] = g["car"].get("carId")
    return out


def gift_list(server, query):
    market.MARKET.load()
    gifts = sorted(market.MARKET.gifts.values(), key=lambda g: g.get("sent", 0), reverse=True)
    out = []
    for g in gifts[:200]:
        r = gift_record(g)
        try:
            r["gamertag"] = gamertag(server, int(r["to"], 16))
        except (TypeError, ValueError):
            r["gamertag"] = ""
        out.append(r)
    return 200, {"gifts": out, "gift_max": GIFT_MAX}


def gift_send(server, data):
    who = who_text(data)
    reason = text(data, "reason", REASON_MAX, required=True)
    query = {"to": [who], "message": [reason]}
    if data.get("car") not in (None, ""):
        query["car"] = [str(number(data, "car", 1, 2 ** 31 - 1))]
    else:
        query["credits"] = [str(number(data, "credits", 1, GIFT_MAX))]
    return decoded(lsp.gift_request(query))


# ---- players ----

def search(server, query):
    q = q1(query, "q").strip()
    if not q or len(q) > 32:
        raise Refused(400, "q: a gamertag or XUID, 1 to 32 characters")
    want, upper = q.lower(), q.upper().removeprefix("0X")
    found = {}
    for k, p in store_of(server).players.items():
        if want in (p.get("name") or "").lower() or k.startswith(upper):
            found[k] = {"xuid": k, "gamertag": p.get("name", ""), "seen": int(p.get("seen", 0))}
    folder = os.path.join(lsp.DATA_DIR, "players")
    names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    for name in names[:PLAYER_SCAN_MAX]:
        k = name[:-5].upper() if name.endswith(".json") else ""
        if xuid_of(k) is None or k in found or len(found) >= 4 * SEARCH_MAX:
            continue
        rec = lsp.load_player(int(k, 16))
        if not k.startswith(upper) and want not in (rec.get("gamertag") or "").lower():
            continue
        found[k] = {"xuid": k, "gamertag": rec.get("gamertag", ""),
                    "seen": int(rec.get("lastLogon", 0))}
    online = {hx(p.xuid) for p in server.peers.values()}
    out = sorted(found.values(), key=lambda r: (-(r["xuid"] in online), -r["seen"]))[:SEARCH_MAX]
    for r in out:
        r["online"] = r["xuid"] in online
    return 200, {"players": out}


def key_state(p):
    if not p:
        return {"state": "unknown", "text": "not in friends.json (never signed in with friends)"}
    if p.get("pub"):
        return {"state": "signed", "text": "bound to a signed game's public key",
                "public_key": p["pub"][:16] + "..."}
    if p.get("key"):
        return {"state": "friends key", "text": "bound to a friends key (a game before signed "
                                                "requests)"}
    return {"state": "unbound", "text": "no key on record yet"}


def player(server, query):
    x = xuid_of(q1(query, "xuid"))
    if x is None:
        raise Refused(400, "xuid: 16 hex digits")
    k = hx(x)
    p = store_of(server).get(x)
    rec = lsp.load_player(x)
    if not p and not rec:
        raise Refused(404, "no player %s on this server" % k)
    online = [{"gamertag": peer.name, "room": peer.room, "online_for": int(time.time() - peer.since),
               "via_proxy": peer.proxied, "status": peer.status}
              for peer in server.peers.values() if peer.xuid == x]
    club, role = clubs.membership(x)
    rows = json.loads(scores.admin("GET", "/admin/scores", {"player": [k]})[2])
    builds = sorted({r.get("build") or "none" for r in rows["rows"]})
    latest = max(rows["rows"], key=lambda r: r.get("submitted") or 0, default=None)
    bans = designs.banned_xuids()
    photos = [community.admin_record(pid, ph, bans) for pid, ph in
              sorted(community.load()["photos"].items(), key=lambda e: e[1]["created"], reverse=True)
              if ph["owner"] == x]
    found_designs = []
    for fid, f in designs.files(done=True, owner=x):
        try:
            found_designs.append(design_feed.admin_record(fid, f, bans, owner=True))
        except (KeyError, TypeError):
            continue
    market.MARKET.load()
    gifts = sorted((gift_record(g) for g in market.MARKET.gifts.values() if g.get("recipient") == k),
                   key=lambda g: g["sent"], reverse=True)[:50]
    challenges = None
    if objectives.ENABLED:
        doc = objectives.document(x)
        challenges = {"sets": doc["sets"], "player": doc.get("player", {})}
    out = {"xuid": k, "gamertag": (p or {}).get("name") or rec.get("gamertag", ""),
           "key": key_state(p),
           "signed_logons": {"first": rec.get("firstSigned"), "days": rec.get("signedDays", 0)},
           "last_seen": int((p or {}).get("seen", 0)) or None, "last_logon": rec.get("lastLogon"),
           "logons": rec.get("logons", 0), "career_level": rec.get("careerLevel"),
           "online": online,
           "build": (latest or {}).get("build"), "builds": builds,
           "friends": {n: len((p or {}).get(f, {})) for n, f in
                       (("friends", "friends"), ("requests_in", "in"), ("requests_out", "out"),
                        ("blocked", "blocked"), ("muted", "muted"))},
           "club": {"tag": club["tag"], "name": club["name"], "id": club["id"], "role": role}
           if club else None,
           "scores": {"ranked": sum(r["state"] == "ranked" for r in rows["rows"]),
                      "unranked": sum(r["state"] == "unranked" for r in rows["rows"]),
                      "matched": rows["matched"], "rows": rows["rows"][:100]},
           "photos": photos, "designs": found_designs, "challenges": challenges, "gifts": gifts,
           "bans": {"content": x in bans, "content_reason": audit.last_reason("ban", k),
                    "signin_until": int(server.chat.banned(x) or 0) or None,
                    "muted_until": int(server.chat.admin_muted(x) or 0) or None}}
    if accounts_admin():
        out["accounts_admin"] = accounts_admin() + "members/user/?q=" + k
    return 200, out


# ---- bans and chat ----

def ban_list(server, query):
    store = store_of(server)
    content = [{"xuid": hx(x), "gamertag": store.name(x) if store.get(x) else
                lsp.load_player(x).get("gamertag", ""), "reason": audit.last_reason("ban", hx(x))}
               for x in sorted(designs.banned_xuids())]
    now = time.time()
    timed = [{"kind": kind, "xuid": hx(x), "gamertag": store.name(x), "until": int(until),
              "left": int(until - now)}
             for kind, table in (("sign-in", server.chat.bans), ("mute", server.chat.mutes))
             for x, until in sorted(table.items()) if until > now]
    return 200, {"content": content, "timed": timed}


def ban_set(server, data):
    x = xuid_of(data.get("xuid"))
    if x is None:
        raise Refused(400, "xuid: 16 hex digits")
    on = data.get("ban", True)
    if not isinstance(on, bool):
        raise Refused(400, "ban: true or false")
    text(data, "reason", REASON_MAX, required=on)
    path = "/admin/photos/%s/%s" % ("ban" if on else "unban", hx(x))
    return decoded(community.admin_handle("POST", urllib.parse.urlsplit(path), {}, b"",
                                          lambda: True))


def chat_view(server, query):
    return 200, server.chat.moderation()


def chat_action(server, data, action):
    who = who_text(data, "who")
    minutes = number(data, "minutes", 0, 525600, 10)
    text(data, "reason", REASON_MAX, required=minutes > 0)
    result = server.chat.admin(action, who, minutes)
    if result is None:
        raise Refused(404, "no such player")
    return 200, result


# ---- scores ----

def score_query(filters):
    if not isinstance(filters, dict):
        raise Refused(400, "filters: an object")
    query = {}
    for k, v in filters.items():
        if k not in scores.ADMIN_FILTERS + ("state",):
            raise Refused(400, "unknown filter: %s" % k)
        if v in (None, ""):
            continue
        if not isinstance(v, (str, int)) or isinstance(v, bool) or len(str(v)) > 100:
            raise Refused(400, "%s: text" % k)
        query[k] = [str(v)]
    return query


def score_list(server, query):
    filters = {k: v[0] for k, v in query.items() if k in scores.ADMIN_FILTERS + ("state",)}
    status, obj = decoded(scores.admin("GET", "/admin/scores", score_query(filters)))
    if status == 200:
        obj["score_tokens"] = score_token.status()["score_tokens"]
        obj["filters"] = list(scores.ADMIN_FILTERS)
    return status, obj


def score_change(server, data, action):
    query = score_query(data.get("filters", {}))
    return decoded(scores.admin("POST", "/admin/scores/" + action, query))


# ---- photos and designs ----

PHOTO_STATES = ("", "live", "held", "hidden")


def photo_list(server, query):
    status = q1(query, "status")
    if status not in PHOTO_STATES:
        raise Refused(400, "status: live, held or hidden")
    path = "/admin/photos" + ("?status=" + status if status else "")
    return decoded(community.admin_handle("GET", urllib.parse.urlsplit(path), {}, b"",
                                          lambda: True))


def photo_queue(server, query):
    return decoded(community.admin_handle("GET", urllib.parse.urlsplit("/admin/photos/queue"),
                                          {}, b"", lambda: True))


def photo_image(server, query, name):
    answer = community.admin_handle("GET", urllib.parse.urlsplit("/admin/photos/img/" + name),
                                    {}, b"", lambda: True)
    if answer[0] != 200:
        return reply(*decoded(answer))
    return answer[0], answer[1], answer[2], HEADERS


def note_body(data, extra=()):
    doc = {}
    note = text(data, "note", REASON_MAX)
    if note:
        doc["note"] = note
    for k in extra:
        if data.get(k):
            doc[k] = text(data, k, 16)
    return json.dumps(doc).encode() if doc else b""


def photo_action(server, data, pid, action):
    if action not in ("hide", "unhide", "hold", "release", "delete", "ban"):
        raise Refused(404, "hide, unhide, hold, release, delete or ban")
    if action == "ban":
        text(data, "reason", REASON_MAX, required=True)
    path = "/admin/photos/%s/%s" % (pid, action)
    return decoded(community.admin_handle("POST", urllib.parse.urlsplit(path), {},
                                          note_body(data), lambda: True))


DESIGN_STATES = ("", "pending", "clear", "held", "hidden")


def design_list(server, query):
    state = q1(query, "state")
    if state not in DESIGN_STATES:
        raise Refused(400, "state: pending, clear, held or hidden")
    path = "/admin/designs" + ("?state=" + state if state else "")
    return decoded(design_feed.admin_handle("GET", urllib.parse.urlsplit(path), {}, b"",
                                            lambda: True))


def design_queue(server, query):
    return decoded(design_feed.admin_handle("GET", urllib.parse.urlsplit("/admin/designs/queue"),
                                            {}, b"", lambda: True))


def design_image(server, query, name):
    answer = design_feed.admin_handle("GET", urllib.parse.urlsplit("/admin/designs/img/" + name),
                                      {}, b"", lambda: True)
    if answer[0] != 200:
        return reply(*decoded(answer))
    return answer[0], answer[1], answer[2], HEADERS


def design_action(server, data, wid, action):
    if action not in ("release", "hold", "hide", "unhide", "delete", "ban"):
        raise Refused(404, "release, hold, hide, unhide, delete or ban")
    if action == "ban":
        text(data, "reason", REASON_MAX, required=True)
    path = "/admin/designs/%s/%s" % (wid, action)
    return decoded(design_feed.admin_handle("POST", urllib.parse.urlsplit(path), {},
                                            note_body(data, ("expect",)), lambda: True))


# ---- the server list ----

ENTRY_KEYS = ("name", "address", "region", "contact", "verified", "id")


def list_path():
    return os.path.join(lsp.DATA_DIR, server_list.LIST_FILE)


def read_list():
    """The operator's server_list.json as it is ({} when there is none); Refused when it cannot
    be edited here."""
    try:
        data = durable.load(list_path())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise Refused(409, "%s cannot be read (%s): fix it by hand" % (server_list.LIST_FILE, e))
    if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
        raise Refused(409, "%s is not an object with a servers list: fix it by hand"
                      % server_list.LIST_FILE)
    return data


def write_list(data):
    os.makedirs(lsp.DATA_DIR, exist_ok=True)
    tmp = list_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    durable.replace(tmp, list_path())


def servers_view(server, query):
    exists = os.path.exists(list_path())
    try:
        raw = read_list()
        problem = None
    except Refused as e:
        raw, problem = {}, str(e)
    try:
        entries, problems = server_list.load_entries(list_path()) if exists and not problem \
            else ([], [])
    except (OSError, ValueError) as e:
        entries, problems = [], [str(e)]
    seed = server_list.load_key(lsp.DATA_DIR, create=False)
    from forza_live.core import ed25519
    return 200, {"file": exists, "self": raw.get("self"), "servers": raw.get("servers", []),
                 "listed": entries, "problems": ([problem] if problem else []) + problems,
                 "public_key": ed25519.public_key(seed).hex() if seed else None,
                 "server_id": scores.server_id()}


def entry_of(data, own):
    entry = data.get("entry")
    if not isinstance(entry, dict):
        raise Refused(400, "entry: an object")
    unknown = set(entry) - set(ENTRY_KEYS)
    if unknown:
        raise Refused(400, "entry: unknown field %s" % ", ".join(sorted(unknown)))
    kept = {k: entry[k] for k in ENTRY_KEYS if k in entry and entry[k] not in (None, "")}
    if own:
        kept.pop("verified", None)
        kept.pop("id", None)
    try:
        server_list.clean_entry(kept, own=own)
    except ValueError as e:
        raise Refused(400, str(e)) from None
    return kept


def slot_of(data, servers):
    slot = data.get("slot")
    if slot in ("self", "new"):
        return slot
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < len(servers):
        raise Refused(400, "slot: self, new or the entry's index")
    return slot


def check_before(data, current):
    """The edit was made on the entry as the page showed it (another edit did not move it)."""
    before = data.get("before")
    now = (current or {}).get("address") if isinstance(current, dict) else None
    if before != now:
        raise Refused(409, "the list changed meanwhile: reload it")


def servers_save(server, data):
    raw = read_list()
    servers = raw.setdefault("servers", [])
    slot = slot_of(data, servers)
    entry = entry_of(data, slot == "self")
    current = raw.get("self") if slot == "self" else None if slot == "new" else servers[slot]
    check_before(data, current)
    address = entry["address"].lower()
    for i, other in enumerate(servers):
        if i != slot and isinstance(other, dict) and str(other.get("address", "")).lower() == address:
            raise Refused(409, "%s is already listed" % address)
    if slot != "self" and isinstance(raw.get("self"), dict) and \
            str(raw["self"].get("address", "")).lower() == address:
        raise Refused(409, "%s is this server's own address" % address)
    if slot == "new" and len(servers) >= server_list.MAX_SERVERS:
        raise Refused(409, "the list has %d servers already" % server_list.MAX_SERVERS)
    if slot == "self":
        raw["self"] = entry
    elif slot == "new":
        servers.append(entry)
    else:
        servers[slot] = entry
    write_list(raw)
    return 200, {"saved": slot, "entry": entry}


def servers_remove(server, data):
    raw = read_list()
    servers = raw.setdefault("servers", [])
    slot = slot_of(data, servers)
    if slot in ("self", "new"):
        raise Refused(400, "slot: the index of a community server")
    check_before(data, servers[slot])
    gone = servers.pop(slot)
    write_list(raw)
    return 200, {"removed": gone}


def check_one(address):
    started = time.monotonic()
    status = discord_status.fetch_status(address)
    ms = int((time.monotonic() - started) * 1000)
    if status is None:
        return {"address": address, "up": False, "ms": ms}
    try:
        players, worlds, _ = discord_status.summary(status)
    except (AttributeError, KeyError, TypeError, ValueError):
        players = worlds = None
    return {"address": address, "up": True, "ms": ms, "players": players, "worlds": worlds,
            "server": str(status.get("server", ""))[:32], "protocol": status.get("protocol")}


async def servers_check(server, data):
    if data.get("address"):
        addresses = [text(data, "address", server_list.LIMITS["address"])]
    else:
        raw = read_list()
        addresses = [e.get("address") for e in ([raw.get("self")] + raw.get("servers", []))
                     if isinstance(e, dict) and isinstance(e.get("address"), str)]
    addresses = [a.lower() for a in addresses if server_list.valid_address(a.lower())][:CHECK_MAX]
    if not addresses:
        raise Refused(400, "address: host[:port], as players type it")
    results = await asyncio.gather(*(asyncio.to_thread(check_one, a) for a in addresses))
    return 200, {"checks": list(results)}


# ---- relays ----

def relays_view(server, query):
    try:
        entries, problems = relays.load_entries(lsp.DATA_DIR)
    except FileNotFoundError:
        entries, problems = [], []
    except (OSError, ValueError) as e:
        entries, problems = [], [str(e)]
    live = server.relays.status() if getattr(server.relays, "entries", None) else \
        {"relays": [], "relay_offers": 0}
    return 200, {"entries": [{k: e.get(k, "") for k in ("id", "name", "address", "region",
                                                         "contact")} for e in entries],
                 "problems": problems, "status": live}


def relay_add(server, data):
    rid = text(data, "id", 32, required=True)
    try:
        path = relays.add_relay(lsp.DATA_DIR, rid, text(data, "name", 48, required=True),
                                text(data, "address", relays.LIMITS["address"], required=True),
                                text(data, "region", 32), text(data, "contact", 120))
    except ValueError as e:
        raise Refused(400, str(e)) from None
    rel = os.path.relpath(path, lsp.DATA_DIR)
    return 200, {"added": rid, "secret_file": rel,
                 "note": "the relay's secret is in %s in the data folder: send that file to the "
                         "relay's operator privately (the panel never shows it)" % rel}


def relay_remove(server, data):
    rid = text(data, "id", 32, required=True)
    if not relays.remove_relay(lsp.DATA_DIR, rid):
        raise Refused(404, "%s is not listed" % rid)
    return 200, {"revoked": rid}


# ---- challenges ----

def challenges_view(server, query):
    out = {"enabled": objectives.ENABLED, "gifts": objectives.GIFTS,
           "gift_credits": objectives.GIFT_CREDITS, "gift_after": objectives.GIFT_AFTER}
    if not objectives.ENABLED:
        return 200, out
    doc = objectives.document()
    week = objectives.period("weekly", time.time())[0]
    with objectives.STORE.lock:
        players = dict(objectives.STORE.data.get("gifts", {}).get(week, {}))
    gifts = [{"xuid": k, "gamertag": gamertag(server, int(k, 16)), "finished": r.get("n", 0),
              "sent": r.get("sent"), "gift": r.get("gift")}
             for k, r in sorted(players.items(), key=lambda e: -(e[1].get("sent") or 0))
             if xuid_of(k) is not None]
    out.update(sets=doc["sets"], week=week, week_players=len(gifts),
               gifts_sent=[g for g in gifts if g["sent"]],
               almost=[g for g in gifts if not g["sent"]][:50])
    return 200, out


# ---- community events (events.py, tools/event_admin.py) ----

def events_call(server, method, path, data):
    import sqlite3
    try:
        return 200, server.events.handle(method, path, data)
    except (ValueError, TypeError, sqlite3.IntegrityError) as e:
        return 400, {"error": str(e)}
    except (OSError, sqlite3.Error):
        log.exception("operator panel: community event storage unavailable")
        return 500, {"error": "event storage unavailable"}


EVENT_ID = r"([A-Za-z0-9._-]{1,64})"


def events_list(server, query):
    return events_call(server, "GET", "/admin/events", {})


def events_attendance(server, query, eid):
    return events_call(server, "GET", "/admin/events/%s/attendance" % eid, {})


def events_create(server, data):
    event = data.get("event")
    if not isinstance(event, dict):
        raise Refused(400, "event: an object (the event's JSON)")
    return events_call(server, "POST", "/admin/events", event)


def events_enroll(server, data):
    if xuid_of(data.get("xuid")) is None:
        raise Refused(400, "xuid: 16 hex digits")
    return events_call(server, "POST", "/admin/events/enroll", {"xuid": data["xuid"].strip()})


def events_action(server, data, eid, action):
    body = {}
    if action in ("award", "revoke"):
        if xuid_of(data.get("xuid")) is None:
            raise Refused(400, "xuid: 16 hex digits")
        body["xuid"] = data["xuid"].strip()
    if action == "award":
        owned = data.get("owned_models")
        if not isinstance(owned, list) or not all(isinstance(m, int) and not isinstance(m, bool)
                                                  for m in owned):
            raise Refused(400, "owned_models: a list of car ids ([] when none is owned)")
        body["owned_models"] = owned
    return events_call(server, "POST", "/admin/events/%s/%s" % (eid, action), body)


# ---- Horizon World events ----

def world_event(server, data):
    session = text(data, "session", 16) or "all"
    if session != "all" and not re.fullmatch("[0-9a-fA-F]{16}", session):
        raise Refused(400, "session: all or a world's 16 hex digit id")
    event = number(data, "event", 0, 2 ** 31 - 1, 0)
    return 200, server.world_request(session, event)


# ---- the audit log ----

def audit_view(server, query):
    try:
        limit = int(q1(query, "limit", "200"))
    except ValueError:
        limit = 200
    return 200, {"entries": audit.recent(max(1, min(limit, audit.SHOWN)))}


# (pattern, function) of the reads; (pattern, (function, audit action, target(data, *groups)))
# of the changes
READS = (
    (r"overview", overview),
    (r"broadcasts", broadcast_list),
    (r"gifts", gift_list),
    (r"players", search),
    (r"player", player),
    (r"bans", ban_list),
    (r"chat", chat_view),
    (r"scores", score_list),
    (r"photos", photo_list),
    (r"photos/queue", photo_queue),
    (r"photos/img/([A-Z2-7]{%d}\.(?:jpg|png))" % community.ID_LEN, photo_image),
    (r"designs", design_list),
    (r"designs/queue", design_queue),
    (r"designs/img/([A-Z2-7]{%d}\.png)" % designs.WEB_ID_LEN, design_image),
    (r"servers", servers_view),
    (r"relays", relays_view),
    (r"challenges", challenges_view),
    (r"events", events_list),
    (r"events/%s/attendance" % EVENT_ID, events_attendance),
    (r"audit", audit_view),
)

ACTIONS = (
    (r"broadcasts", (broadcast_send, "broadcast", lambda d: "everyone")),
    (r"broadcasts/delete", (broadcast_delete, "broadcast delete", lambda d: d.get("id"))),
    (r"notify", (notify_send, "notify", lambda d: d.get("to"))),
    (r"gifts", (gift_send, "gift", lambda d: d.get("to"))),
    (r"bans", (ban_set, lambda d: "ban" if d.get("ban", True) is True else "unban",
               lambda d: str(d.get("xuid", "")).strip().upper())),
    (r"chat/(mute|kick)", (chat_action, lambda d, a: "chat " + a, lambda d, a: d.get("who"))),
    (r"scores/(unrank|rank|delete)", (score_change, lambda d, a: "scores " + a,
                                      lambda d, a: json.dumps(d.get("filters", {}),
                                                              sort_keys=True)[:128])),
    (r"photos/([A-Z2-7]{%d})/([a-z]+)" % community.ID_LEN,
     (photo_action, lambda d, pid, a: "photo " + a, lambda d, pid, a: pid)),
    (r"designs/([A-Z2-7]{%d})/([a-z]+)" % designs.WEB_ID_LEN,
     (design_action, lambda d, wid, a: "design " + a, lambda d, wid, a: wid)),
    (r"servers/save", (servers_save, "server list save",
                       lambda d: (d.get("entry") or {}).get("address", ""))),
    (r"servers/remove", (servers_remove, "server list remove", lambda d: d.get("before"))),
    (r"servers/check", (servers_check, "server check", lambda d: d.get("address") or "all")),
    (r"relays/add", (relay_add, "relay add", lambda d: d.get("id"))),
    (r"relays/remove", (relay_remove, "relay remove", lambda d: d.get("id"))),
    (r"events", (events_create, "event create", lambda d: (d.get("event") or {}).get("id"))),
    (r"events/enroll", (events_enroll, "event enroll", lambda d: d.get("xuid"))),
    (r"events/%s/(start|close|award|revoke)" % EVENT_ID,
     (events_action, lambda d, eid, a: "event " + a, lambda d, eid, a: eid)),
    (r"world-event", (world_event, "world event", lambda d: d.get("session") or "all")),
)
