"""Our own JSON endpoints, beside allauth's sign-in flows: forza.dyotson.dev/api/...

  GET  /api/health          {"ok": true}
  GET  /api/me              the signed-in account (401 when signed out)
  POST /api/logout-all      ends every session of the account, this one too
  POST /api/account/delete  {"confirm": "<account id>"}; needs a sign-in in the last 10 minutes

and, in members.links, the game link: the launcher's signed calls under /api/game/ and the
website's /api/link/peek, claim and remove.

Session auth with Django's CSRF check on every change (the page sends X-CSRFToken). Nothing here
returns an email address, a provider's id or an XUID. The OpenAPI page (/api/docs) exists only
with FORZA_API_DOCS=1, and answers only FORZA_ADMIN_ALLOW (members.middleware).
"""

import time
from datetime import date, datetime
from typing import List, Literal, Optional

from allauth.account.authentication import get_authentication_records
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import logout
from django.contrib.sessions.models import Session
from django.db import transaction
from ninja import NinjaAPI, Schema
from ninja.security import django_auth
from pydantic import ConfigDict, Field

from members.models import AuditEvent, SignIn

api = NinjaAPI(
    title="forza.dyotson.dev accounts",
    version="1",
    urls_namespace="api",
    docs_url="/docs" if settings.API_DOCS else None,
    openapi_url="/openapi.json" if settings.API_DOCS else None,
)

Method = Literal["discord", "github", "email"]
ACCOUNT_ID = r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$"


class Strict(Schema):
    model_config = ConfigDict(extra="forbid")


class HealthOut(Strict):
    ok: bool


class EventOut(Strict):
    at: datetime
    event: str


class LinkOut(Strict):
    """The linked game: active; frozen (the XUID is bound to another key now, so the operator
    checks it); waiting (the game server forgot the key after a long time away, and its next
    sign-in brings it back); or banned. Its gamertag, never its XUID."""
    state: Literal["active", "frozen", "waiting", "banned"]
    gamertag: str
    since: date


class MeOut(Strict):
    id: str = Field(pattern=ACCOUNT_ID)
    created: date
    methods: List[Method]
    sessions: int = Field(ge=0)
    recent: bool
    link: Optional[LinkOut] = None
    events: List[EventOut]


class EmptyIn(Strict):
    pass


class LogoutAllOut(Strict):
    ok: bool
    ended: int = Field(ge=0)


class DeleteIn(Strict):
    confirm: str = Field(pattern=ACCOUNT_ID)


class DeleteOut(Strict):
    ok: bool
    deleted: bool


class ErrorOut(Strict):
    error: str


def recently_signed_in(request):
    records = get_authentication_records(request, request.user)
    at = max((r.get("at", 0) for r in records), default=0)
    return time.time() - at <= settings.ACCOUNT_REAUTHENTICATION_TIMEOUT


def methods_of(user):
    found = set(SocialAccount.objects.filter(user=user).values_list("provider", flat=True))
    if EmailAddress.objects.filter(user=user).exists():
        found.add("email")
    return [m for m in ("discord", "github", "email") if m in found]


def end_sessions(user):
    """Every session of the account, then its password hash renewed: Django's own check
    (get_session_auth_hash) then refuses any session this missed."""
    keys = list(SignIn.objects.filter(user=user).values_list("session_key", flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    SignIn.objects.filter(user=user).delete()
    user.set_unusable_password()
    user.save(update_fields=["password"])
    return len(keys)


@api.get("/health", response=HealthOut, auth=None)
def health(request):
    return {"ok": True}


@api.get("/me", response={200: MeOut}, auth=django_auth)
def me(request):
    from members import links
    user = request.auth
    return {
        "id": user.public_id,
        "created": user.date_joined.date(),
        "methods": methods_of(user),
        "sessions": SignIn.objects.filter(user=user).count(),
        "recent": recently_signed_in(request),
        "link": links.summary(user),
        "events": [{"at": e.at, "event": e.event} for e in AuditEvent.objects.filter(user=user)[:20]],
    }


@api.post("/logout-all", response={200: LogoutAllOut}, auth=django_auth)
def logout_all(request, body: EmptyIn):
    user = request.auth
    with transaction.atomic():
        AuditEvent.objects.create(user=user, event="signed out everywhere")
        ended = end_sessions(user)
    logout(request)
    return {"ok": True, "ended": ended}


@api.post("/account/delete", response={200: DeleteOut, 400: ErrorOut, 403: ErrorOut}, auth=django_auth)
def delete_account(request, body: DeleteIn):
    user = request.auth
    if body.confirm != user.public_id:
        return 400, {"error": "the account id does not match"}
    if not recently_signed_in(request):
        return 403, {"error": "sign in again"}
    logout(request)
    with transaction.atomic():
        end_sessions(user)
        user.delete()  # identities, email stand-in, sessions and audit go with it (CASCADE)
    return {"ok": True, "deleted": True}
