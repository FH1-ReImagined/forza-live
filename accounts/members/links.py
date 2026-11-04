"""Linking a website account to the player's game key (community accounts design, section 2).

Both sides approve every link: the website account claims the link request, and the game key
confirms the account it was shown. A token, code or address that leaks is useless alone.

  launcher (signed with the player's key, members.game.signed_caller; no cookie, no Origin)
    POST /api/game/link/start            {"v": 1} -> {id, url, code, expires}
    GET  /api/game/link/<id>             the request: pending, claimed (by which account), ...
    POST /api/game/link/<id>/confirm     {"account": "K7Q2-XM4P"}: the account the player saw
    POST /api/game/link/<id>/cancel      {} ("Not me")
    GET  /api/game/account               the key's link: none, active or frozen
    POST /api/game/unlink                {}
    POST /api/game/signout               {}: every session of the linked account ends
  website (session, CSRF)
    POST /api/link/peek                  {"token"} or {"code"}: the gamertag and the state
    POST /api/link/claim                 {"token"} or {"code"}: this account claims it
    POST /api/link/remove                {}: needs a sign-in in the past 10 minutes

1. The launcher starts a request. The key must be bound on the game server, not banned, and
   have a gamertag and a signed sign-in there. It gets a token (256 bits, for the browser's
   address, never shown) and a code (8 characters, for another device). A new request ends the
   XUID's previous one.
2. In the browser the signed-in account peeks (sees the gamertag) and claims it.
3. The launcher polls the request, shows the claiming account's id and confirms it by that id.
   One transaction then drops any older link of the key and of the account and writes the new
   one, with the key's public key pinned, and every other session of the account ends.

Rules: one key per account and one account per key; a key or an account links at most once a
day; the key holder can always unlink and sign the account out everywhere. When the key the XUID
is bound to is no longer the pinned one, the link freezes until the operator checks it. Nothing
here returns an XUID.
"""

import hashlib
import hmac
import logging
import re
import secrets
from datetime import date, timedelta
from importlib import import_module
from typing import Literal, Optional

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja.security import django_auth
from pydantic import Field, model_validator

from members import game, limits
from members.api import ACCOUNT_ID, EmptyIn, ErrorOut, Method, Strict, api, end_sessions, methods_of, \
    recently_signed_in
from members.game import Refused
from members.models import CODE_CHARS, AuditEvent, GameLink, LinkRequest, SignIn

log = logging.getLogger("members")

CODES_OFF = "fh:link-codes-off"
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
State = Literal["pending", "claimed", "declined", "linked", "cancelled", "expired"]


@api.exception_handler(Refused)
def refused(request, exc):
    return api.create_response(request, exc.body, status=exc.status)


# ---- Schemas ----

class StartIn(Strict):
    v: Literal[1]


class StartOut(Strict):
    id: str
    url: str
    code: str = Field(pattern=r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
    expires: int  # unix time


class AccountOut(Strict):
    id: str = Field(pattern=ACCOUNT_ID)
    method: Method
    created: date


class PollOut(Strict):
    state: State
    account: Optional[AccountOut] = None  # the account that claimed it
    replaces: Optional[str] = Field(None, pattern=ACCOUNT_ID)  # the account the key is linked to now


class ConfirmIn(Strict):
    account: str = Field(pattern=ACCOUNT_ID)


class StateOut(Strict):
    ok: bool
    state: State


class GameAccountOut(Strict):
    linked: bool
    state: Optional[Literal["active", "frozen"]] = None
    account: Optional[AccountOut] = None


class UnlinkOut(Strict):
    ok: bool
    unlinked: bool


class SignoutOut(Strict):
    ok: bool
    ended: int = Field(ge=0)


class SecretIn(Strict):
    token: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{43}$")
    code: Optional[str] = Field(None, max_length=12)

    @model_validator(mode="after")
    def one_of(self):
        if (self.token is None) == (self.code is None):
            raise ValueError("send a token or a code")
        return self


class PeekOut(Strict):
    state: State
    gamertag: str
    expires: int
    replaces: Optional[str] = None  # the gamertag this account is linked to now, when another


class ClaimOut(Strict):
    ok: bool
    state: State
    account: str = Field(pattern=ACCOUNT_ID)


# ---- Helpers ----

def now():
    return timezone.now()


def unix(t):
    return int(t.timestamp())


def secret_hash(kind, value):
    return hmac.new(settings.SECRET_KEY.encode(), ("%s|%s" % (kind, value)).encode(), hashlib.sha256).hexdigest()


def normal_code(text):
    """K7M2-QXPA, k7m2 qxpa... -> K7M2QXPA; None when it cannot be a code."""
    code = re.sub(r"[\s-]", "", text or "").upper()
    return code if len(code) == 8 and all(c in CODE_CHARS for c in code) else None


def state_of(req):
    if req.state in (LinkRequest.PENDING, LinkRequest.CLAIMED) and req.expires <= now():
        return "expired"
    return req.state


def account_of(user):
    found = methods_of(user)
    return {"id": user.public_id, "method": found[0] if found else "email", "created": user.date_joined.date()}


def change_keys(xuid_hex, user=None):
    keys = ["x:" + xuid_hex]
    if user is not None:
        keys.append("a:%d" % user.pk)
    return keys


def may_change(request, keys):
    """A key or an account links at most once a day."""
    return all(limits.take(request, "link_change", key=k, dry_run=True) for k in keys)


def spend(request, action, key, error="too many requests"):
    if not limits.take(request, action, key=key):
        raise Refused(429, error)


def caller(request):
    """The signed launcher call's XUID (hex) and public key, within its rate."""
    xuid, pub = game.signed_caller(request)
    hx = game.hex_xuid(xuid)
    spend(request, "game_call", "x:" + hx)
    return xuid, hx, pub


def own_request(rid, hx):
    req = LinkRequest.objects.filter(pk=rid, xuid=hx).select_related("claimed_by").first() \
        if REQUEST_ID.fullmatch(rid) else None
    if req is None:
        raise Refused(404, "not found")
    return req


def freeze(link):
    if link.state != GameLink.FROZEN:
        link.state, link.frozen_at = GameLink.FROZEN, now()
        link.save(update_fields=["state", "frozen_at"])
        AuditEvent.objects.create(user=link.user, event="the game link is frozen: its key changed")
        log.warning("accounts: the link of account %s is frozen (the key changed)", link.user.public_id)


def pinned(link, pub):
    """The link is the caller's key's: else it freezes."""
    if not hmac.compare_digest(link.pub, pub):
        freeze(link)
        return False
    return True


def end_other_sessions(user, keep):
    """Every session of the account but `keep` (the browser that claimed the link)."""
    keys = list(SignIn.objects.filter(user=user).exclude(session_key=keep).values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    SignIn.objects.filter(session_key__in=keys).delete()
    user.set_unusable_password()  # refuses any session SignIn missed (get_session_auth_hash)
    user.save(update_fields=["password"])
    store = import_module(settings.SESSION_ENGINE).SessionStore
    if keep and store().exists(keep):
        session = store(session_key=keep)
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
    return len(keys)


def summary(user):
    """The account's link for GET /api/me, or None."""
    link = GameLink.objects.filter(user=user).first()
    if link is None:
        return None
    state = link.state
    if game.available():
        xuid = int(link.xuid, 16)
        tag = game.gamertag(xuid)
        if tag and tag != link.gamertag:
            link.gamertag = tag
            link.save(update_fields=["gamertag"])
        pub = game.bound_pub(xuid)
        if pub is not None and not pinned(link, pub):
            state = GameLink.FROZEN
        elif pub is None and state == GameLink.ACTIVE:
            state = "waiting"
        if game.banned(xuid):
            state = "banned"
    counted = state == GameLink.ACTIVE and game.available() and game.counted(link.xuid, link.pub)
    return {"state": state, "gamertag": link.gamertag, "since": link.linked_at.date(), "counted": counted}


# ---- The launcher's calls ----

@api.post("/game/link/start", response={200: StartOut}, auth=None)
def link_start(request, body: StartIn):
    xuid, hx, pub = caller(request)
    if game.banned(xuid):
        raise Refused(403, "not available")
    tag = game.gamertag(xuid)
    if not tag or not isinstance(game.player(xuid).get("firstSigned"), int):
        raise Refused(409, "play online first")
    if not may_change(request, change_keys(hx)):
        raise Refused(429, "changed in the past day")
    spend(request, "link_start", "x:" + hx)
    t = now()
    LinkRequest.objects.filter(expires__lt=t - timedelta(hours=1)).delete()
    for _ in range(5):
        token = secrets.token_urlsafe(32)
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(8))
        try:
            with transaction.atomic():
                LinkRequest.objects.filter(xuid=hx, state__in=(LinkRequest.PENDING, LinkRequest.CLAIMED)) \
                    .update(state=LinkRequest.CANCELLED)
                req = LinkRequest.objects.create(
                    id=secrets.token_hex(16), xuid=hx, pub=pub, gamertag=tag, created=t,
                    expires=t + timedelta(minutes=settings.LINK_MINUTES),
                    token_hash=secret_hash("token", token), code_hash=secret_hash("code", code))
            break
        except IntegrityError:
            continue  # a code another request has: a new one
    else:
        raise Refused(503, "try again")
    return {"id": req.id, "url": settings.ORIGIN + "/account.html#link=" + token,
            "code": code[:4] + "-" + code[4:], "expires": unix(req.expires)}


@api.get("/game/link/{rid}", response={200: PollOut}, auth=None)
def link_poll(request, rid: str):
    _, hx, _ = caller(request)
    req = own_request(rid, hx)
    out = {"state": state_of(req)}
    if out["state"] == LinkRequest.CLAIMED:
        out["account"] = account_of(req.claimed_by)
        now_linked = GameLink.objects.filter(xuid=hx).exclude(user=req.claimed_by).select_related("user").first()
        out["replaces"] = now_linked.user.public_id if now_linked else None
    return out


@api.post("/game/link/{rid}/confirm", response={200: StateOut}, auth=None)
def link_confirm(request, rid: str, body: ConfirmIn):
    xuid, hx, pub = caller(request)
    req = own_request(rid, hx)
    if state_of(req) != LinkRequest.CLAIMED or not req.claimed_by.is_active:
        raise Refused(409, "not claimed")
    user = req.claimed_by
    if body.account != user.public_id:
        raise Refused(409, "the account does not match")
    if not hmac.compare_digest(req.pub, pub):
        raise Refused(409, "the key changed")
    if game.banned(xuid):
        raise Refused(403, "not available")
    keys = change_keys(hx, user)
    if not may_change(request, keys):
        raise Refused(429, "changed in the past day")
    spend(request, "new_link", "all", "too many links today")
    t = now()
    with transaction.atomic():
        if not LinkRequest.objects.filter(pk=req.pk, state=LinkRequest.CLAIMED, claimed_by=user) \
                .update(state=LinkRequest.LINKED):
            raise Refused(409, "not claimed")
        of_key = GameLink.objects.filter(xuid=hx).select_related("user").first()
        of_account = GameLink.objects.filter(user=user).exclude(xuid=hx).first()
        moved = of_key is not None and of_key.user_id != user.pk
        if moved:
            AuditEvent.objects.create(user=of_key.user, event="the game moved its link to another account")
        if of_account is not None:
            AuditEvent.objects.create(user=user, event="unlinked the game of %s" % of_account.gamertag)
        GameLink.objects.filter(xuid=hx).delete()
        GameLink.objects.filter(user=user).delete()
        GameLink.objects.create(xuid=hx, pub=pub, user=user, gamertag=req.gamertag, linked_at=t,
                                moved_at=t if moved else None)
        AuditEvent.objects.create(user=user, event="linked the game of %s" % req.gamertag)
        ended = end_other_sessions(user, req.claimed_session)
    for k in keys:
        limits.take(request, "link_change", key=k)
    log.info("accounts: account %s linked a game%s; %d other session(s) ended", user.public_id,
             " (moved from another account)" if moved else "", ended)
    return {"ok": True, "state": "linked"}


@api.post("/game/link/{rid}/cancel", response={200: StateOut}, auth=None)
def link_cancel(request, rid: str, body: EmptyIn):
    _, hx, _ = caller(request)
    req = own_request(rid, hx)
    if state_of(req) in (LinkRequest.PENDING, LinkRequest.CLAIMED):
        req.state = LinkRequest.DECLINED
        req.save(update_fields=["state"])
    return {"ok": True, "state": state_of(req)}


@api.get("/game/account", response={200: GameAccountOut}, auth=None)
def game_account(request):
    _, hx, pub = caller(request)
    link = GameLink.objects.filter(xuid=hx).select_related("user").first()
    if link is None:
        return {"linked": False}
    if not pinned(link, pub):
        return {"linked": True, "state": GameLink.FROZEN}  # not the key that linked: no account
    return {"linked": True, "state": link.state, "account": account_of(link.user)}


def pinned_link(hx, pub):
    link = GameLink.objects.filter(xuid=hx).select_related("user").first()
    if link is not None and not pinned(link, pub):
        raise Refused(409, "this link needs the operator")
    return link


@api.post("/game/unlink", response={200: UnlinkOut}, auth=None)
def game_unlink(request, body: EmptyIn):
    _, hx, pub = caller(request)
    link = pinned_link(hx, pub)
    if link is None:
        return {"ok": True, "unlinked": False}
    with transaction.atomic():
        AuditEvent.objects.create(user=link.user, event="the game unlinked this account")
        link.delete()
    return {"ok": True, "unlinked": True}


@api.post("/game/signout", response={200: SignoutOut}, auth=None)
def game_signout(request, body: EmptyIn):
    _, hx, pub = caller(request)
    link = pinned_link(hx, pub)
    if link is None:
        raise Refused(404, "not linked")
    with transaction.atomic():
        AuditEvent.objects.create(user=link.user, event="the game signed this account out everywhere")
        ended = end_sessions(link.user)
    return {"ok": True, "ended": ended}


# ---- The website's calls ----

def codes_off():
    return bool(cache.get(CODES_OFF))


def find(request, user, body):
    """The request a token or code names, for this account; else Refused (a code that finds
    nothing counts towards the breaker: after too many, codes stop for a while)."""
    account = "a:%d" % user.pk
    if not limits.take(request, "link_try", key=account, dry_run=True):
        raise Refused(429, "too many tries")
    if body.token is not None:
        req = LinkRequest.objects.filter(token_hash=secret_hash("token", body.token)).first()
    else:
        if codes_off():
            raise Refused(429, "codes are paused")
        code = normal_code(body.code)
        if code is None:
            raise Refused(400, "not a code")
        req = LinkRequest.objects.filter(code_hash=secret_hash("code", code)).first()
    if req is not None and req.claimed_by_id not in (None, user.pk):
        raise Refused(409, "another account claimed this link")
    if req is None or (req.claimed_by_id is None and state_of(req) != LinkRequest.PENDING):
        limits.take(request, "link_try", key=account)
        if body.code is not None and not limits.take(request, "link_code_failed", key="all"):
            cache.set(CODES_OFF, True, settings.LINK_CODES_OFF_MINUTES * 60)
            log.warning("accounts: too many link codes that found nothing: codes are off for %d minutes",
                        settings.LINK_CODES_OFF_MINUTES)
        raise Refused(404, "not found")
    return req


@api.post("/link/peek", response={200: PeekOut}, auth=django_auth)
def link_peek(request, body: SecretIn):
    user = request.auth
    req = find(request, user, body)
    mine = GameLink.objects.filter(user=user).exclude(xuid=req.xuid).first()
    return {"state": state_of(req), "gamertag": req.gamertag, "expires": unix(req.expires),
            "replaces": mine.gamertag if mine else None}


@api.post("/link/claim", response={200: ClaimOut}, auth=django_auth)
def link_claim(request, body: SecretIn):
    user = request.auth
    req = find(request, user, body)
    spend(request, "link_try", "a:%d" % user.pk, "too many tries")
    state = state_of(req)
    if state == LinkRequest.CLAIMED:
        return {"ok": True, "state": state, "account": user.public_id}  # this account's already
    if state != LinkRequest.PENDING:
        raise Refused(409, "this link is over")
    if not may_change(request, ["a:%d" % user.pk]):
        raise Refused(429, "changed in the past day")
    if not request.session.session_key:
        request.session.save()
    if not LinkRequest.objects.filter(pk=req.pk, state=LinkRequest.PENDING, claimed_by=None) \
            .update(state=LinkRequest.CLAIMED, claimed_by=user, claimed_session=request.session.session_key):
        raise Refused(409, "another account claimed this link")
    AuditEvent.objects.create(user=user, event="asked to link the game of %s" % req.gamertag)
    return {"ok": True, "state": LinkRequest.CLAIMED, "account": user.public_id}


@api.post("/link/remove", response={200: UnlinkOut, 403: ErrorOut}, auth=django_auth)
def link_remove(request, body: EmptyIn):
    user = request.auth
    if not recently_signed_in(request):
        return 403, {"error": "sign in again"}
    link = GameLink.objects.filter(user=user).first()
    if link is None:
        return {"ok": True, "unlinked": False}
    with transaction.atomic():
        AuditEvent.objects.create(user=user, event="unlinked the game of %s" % link.gamertag)
        link.delete()
    return {"ok": True, "unlinked": True}
