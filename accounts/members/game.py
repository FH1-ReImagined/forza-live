"""The game server's data, read-only, and the launcher's signed requests.

The game server (forza_live/) keeps its data in FORZA_GAME_DATA, which this service mounts
read-only. It reads, with the game server's own code:
- friends.json (forza_live.social.friends.Store): the public key each XUID is bound to, read
  again whenever the file changes;
- players/<XUID>.json (forza_live.core.durable): the gamertag, the first result on the boards
  (firstScore: lsp.first_score, and lsp.backfill_first_scores for results from before it) and,
  for the optional likes wait only, the signed sign-ins (firstSigned, signedDays:
  forza_live.title_services.lsp.signed_logon);
- banned_xuids.json (forza_live.content.designs.banned_xuids);
- community.json, videos.json and designs.json, the photos, videos and designs (members.items).

A signed request is checked by forza_live.core.auth.check_request: the server name (this site's
host only), the 120 s clock window, the signature by the key the XUID is bound to, and its nonce,
in this process and (SignedNonce) in every worker. Refusals all look alike."""

import hmac
import logging
import os
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from forza_live.content import community, designs, videos
from forza_live.core import auth, durable
from forza_live.social import friends

from members.models import SignedNonce

log = logging.getLogger("members")

_lock = threading.Lock()
_store = {"mtime": None, "store": None}
_configured = {"dir": None}


class Refused(Exception):
    """A signed request that is not the player's (401), or one this service will not do."""

    def __init__(self, status=401, error="refused", **extra):
        super().__init__(error)
        self.status, self.body = status, {"error": error, **extra}


def available():
    return settings.GAME_DATA is not None


def configure():
    """The game server's modules pointed at the read-only data folder (once per folder)."""
    folder = str(settings.GAME_DATA)
    if _configured["dir"] != folder:
        designs.configure(folder)
        community.configure(folder, gamertag=lambda xuid: player(xuid).get("gamertag", ""))
        videos.configure(folder)
        auth.SERVER_NAMES.clear()
        auth.SERVER_NAMES.add(auth.server_name(settings.SITE_HOST))
        _store.update({"mtime": None, "store": None})
        _configured["dir"] = folder


def store():
    """The game server's friends.Store, as friends.json has it now."""
    configure()
    path = os.path.join(str(settings.GAME_DATA), friends.FILE)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    with _lock:
        if mtime != _store["mtime"] or _store["store"] is None:
            s = friends.Store(path)
            s.load()  # forgets what the game server forgets at its start (FORGET_DAYS)
            _store.update({"mtime": mtime, "store": s})
        return _store["store"]


def player(xuid):
    """The game server's record of the player (players/<XUID>.json), or {}."""
    try:
        record = durable.load(os.path.join(str(settings.GAME_DATA), "players", "%016X.json" % xuid))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def gamertag(xuid):
    """The gamertag the website may show (forza_live.content.community.credit), or None."""
    configure()
    return community.credit(xuid)


def banned(xuid):
    configure()
    return designs.banned(xuid)


def bound_pub(xuid):
    """The public key (hex) the XUID is bound to now, or None (no record, or not a public key)."""
    pub = auth.bound_pub(store(), xuid)
    return pub.hex() if pub else None


def stamp(record, field):
    value = record.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def played(record):
    """The player's record qualifies its key for a link, and its link's likes to count: a result
    on the boards (firstScore: a speed trap, speed zone, event time or 1000 Club medal, from
    before the game server noted them too, lsp.backfill_first_scores). Only the game server
    writes it. The key itself is proven by the signed request and its binding: the game server
    binds an XUID to a public key only on a HELLO that key signed (forza_live.core.auth.hello),
    and every call here is signed by the key the XUID is bound to now."""
    return stamp(record, "firstScore") is not None


def counted(xuid_hex, pinned_pub, now=None):
    """The key's likes count in public: it is still bound to the public key its link pinned, it
    is not banned, and its record is played() (the caller leaves out suspended accounts). With
    LIKES_AFTER_HOURS or LIKES_SIGNED_DAYS set (both 0 by default: no wait), its first sign-in
    signed by its key (firstSigned) must also be that many hours old, with signed sign-ins on
    that many days (signedDays). Only the game server writes those, and only for a sign-in its
    key signed, so nobody can age a key before binding it."""
    xuid = int(xuid_hex, 16)
    pub = bound_pub(xuid)
    if pub is None or not hmac.compare_digest(pub, pinned_pub) or banned(xuid):
        return False
    record = player(xuid)
    if not played(record):
        return False
    if not settings.LIKES_AFTER_HOURS and not settings.LIKES_SIGNED_DAYS:
        return True
    first = stamp(record, "firstSigned")
    if first is None:
        return False
    now = time.time() if now is None else now
    days = stamp(record, "signedDays") or 0
    return now - first >= settings.LIKES_AFTER_HOURS * 3600 and days >= settings.LIKES_SIGNED_DAYS


def hex_xuid(xuid):
    return "%016X" % xuid


def signed_caller(request):
    """(XUID, its public key in hex) of a request the launcher signed with the player's key, or
    Refused. Browsers are refused: no Origin or Sec-Fetch-Site, and no cookie is read."""
    if "Origin" in request.headers or "Sec-Fetch-Site" in request.headers:
        raise Refused(403)
    if not available():
        raise Refused(503, "linking is not available")
    headers = {k.lower(): v for k, v in request.headers.items()}
    # The request line's target as it came (gunicorn's RAW_URI): what the launcher signed.
    target = request.META.get("RAW_URI") or request.get_full_path()
    checked = auth.check_request(store(), request.method, target, headers, request.body)
    if checked is None or not checked.ok:
        raise Refused()
    nonce = auth.parse_header(headers[auth.HEADER])["nonce"]
    now = timezone.now()
    try:
        with transaction.atomic():
            SignedNonce.objects.create(nonce=nonce, until=now + timedelta(seconds=2 * auth.WINDOW + 1))
    except IntegrityError:
        raise Refused() from None  # a replay that reached another worker
    pub = bound_pub(checked.xuid)
    if pub is None:
        raise Refused()
    return checked.xuid, pub


def purge_nonces(now=None):
    SignedNonce.objects.filter(until__lt=now or timezone.now()).delete()
