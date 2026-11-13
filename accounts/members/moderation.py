"""The operator's tools (community accounts design, section 5, "Admin tools") and the purge of
likes whose item is gone. The Django admin (members.admin) and the management commands
(void_likes, purge_likes, stats, backup, restore) call these; there is no HTTP route for them.

Every change to an account writes an audit event, which its owner sees on the account page, and
a log line with the account id only (never an XUID).

- void_likes: deletes likes, for abuse (a key farm, a script). The likes belong to the key, so
  voiding an account's likes is voiding its linked key's. The key can like again: to stop it,
  suspend the account or ban the XUID on the game server.
- suspend / unsuspend: a suspended account (is_active off) is signed out everywhere, cannot sign
  in, cannot be linked, and its key's likes are left out of the public totals.
- sign_out: every session of the account ends (as "sign out everywhere").
- unlink: the account's game link goes; the key keeps its likes, which no longer count.
- purge_missing_likes: the nightly purge. A liked item that is no longer in the game server's
  community.json, videos.json or designs.json at all, in any state (the operator deleted it, the game removed
  it, the screening took it down), is noted as missing; once it has been missing for LIKES_PURGE_GRACE_DAYS, its
  likes are deleted. An item that comes back is forgotten. A file that is missing, damaged or
  empty is not judged at all, so a bad mount or a restore in progress deletes nothing.
"""

import logging
import re
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from members import api, game, items
from members.models import AuditEvent, GameLink, Like, MissingItem, User

log = logging.getLogger("members")

CHUNK = 500  # ids per IN (...) query
ACCOUNT_ID = re.compile(r"^([2-9A-HJ-NP-Z]{4})-?([2-9A-HJ-NP-Z]{4})$")


def account_key(text):
    """K7Q2-XM4P or k7q2xm4p -> K7Q2XM4P (the account's username), else None."""
    m = ACCOUNT_ID.match((text or "").strip().upper())
    return m.group(1) + m.group(2) if m else None


def _forget_counts():
    from members import likes  # its import registers the API routes
    likes.forget_counts()


def accounts_of(xuids):
    """{XUID hex: the account linked to it now} for the XUIDs that are linked."""
    return {link.xuid: link.user for link in GameLink.objects.filter(xuid__in=list(xuids)).select_related("user")}


def void_likes(likes, by="the operator"):
    """Deletes these likes (a Like queryset); the accounts linked to their keys get an audit
    event each. The number deleted."""
    with transaction.atomic():
        per_key = Counter(likes.values_list("xuid", flat=True))
        if not per_key:
            return 0
        owners = accounts_of(per_key)
        deleted = likes.delete()[0]
        for xuid, n in per_key.items():
            user = owners.get(xuid)
            if user is not None:
                AuditEvent.objects.create(user=user, event="%s removed %d of this game's likes" % (by, n))
    _forget_counts()
    for xuid, n in per_key.items():
        user = owners.get(xuid)
        log.info("accounts: %s voided %d like(s) of %s", by, n,
                 "the game of account %s" % user.public_id if user else "a key that is not linked")
    return deleted


def void_key(xuid_hex, by="the operator"):
    return void_likes(Like.objects.filter(xuid=xuid_hex), by)


def void_account(user, by="the operator"):
    """The likes of the key linked to the account (likes belong to the key). None when the
    account is not linked."""
    link = GameLink.objects.filter(user=user).first()
    if link is None:
        return None
    return void_key(link.xuid, by)


def operable(user):
    """Staff accounts are left alone: signing one out would also void its admin password."""
    return not (user.is_staff or user.is_superuser)


def suspend(user):
    if not operable(user) or not user.is_active:
        return False
    with transaction.atomic():
        user.is_active = False
        user.save(update_fields=["is_active"])
        api.end_sessions(user)
        AuditEvent.objects.create(user=user, event="the operator suspended this account")
    _forget_counts()
    log.info("accounts: the operator suspended account %s", user.public_id)
    return True


def unsuspend(user):
    if user.is_active:
        return False
    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=["is_active"])
        AuditEvent.objects.create(user=user, event="the operator lifted this account's suspension")
    _forget_counts()
    log.info("accounts: the operator lifted the suspension of account %s", user.public_id)
    return True


def sign_out(user):
    """The number of sessions ended, or None for a staff account."""
    if not operable(user):
        return None
    with transaction.atomic():
        ended = api.end_sessions(user)
        AuditEvent.objects.create(user=user, event="the operator signed this account out everywhere")
    log.info("accounts: the operator signed account %s out everywhere (%d session(s))", user.public_id, ended)
    return ended


def unlink(link):
    with transaction.atomic():
        AuditEvent.objects.create(user=link.user, event="the operator unlinked the game of %s" % link.gamertag)
        link.delete()
    _forget_counts()
    log.info("accounts: the operator unlinked the game of account %s", link.user.public_id)


def _chunks(values):
    values = list(values)
    for i in range(0, len(values), CHUNK):
        yield values[i:i + CHUNK]


def purge_missing_likes(now=None, grace_days=None, dry_run=False):
    """The nightly purge of likes whose item is gone (see the module's docstring).
    {kind: {"missing": items missing now, "purged": items whose likes went, "likes": likes
    deleted, "skipped": True when the kind's file could not be judged}}."""
    now = now or timezone.now()
    grace = timedelta(days=settings.LIKES_PURGE_GRACE_DAYS if grace_days is None else grace_days)
    if not game.available():
        return {}
    in_files = items.present()
    report = {}
    for kind in items.KINDS:
        present = in_files[kind]
        liked = set(Like.objects.filter(kind=kind).values_list("item", flat=True).distinct())
        if not present:  # None (missing or damaged) or empty
            report[kind] = {"missing": 0, "purged": 0, "likes": 0, "skipped": True}
            if liked:
                log.warning("accounts: the game server's %s file is missing, damaged or empty: no %s likes purged",
                            kind, kind)
            continue
        missing = liked - present
        noted = dict(MissingItem.objects.filter(kind=kind).values_list("item", "since"))
        back = [i for i in noted if i not in missing]
        new = [i for i in missing if i not in noted]
        due = sorted(i for i, since in noted.items() if i in missing and since <= now - grace)
        deleted = 0
        if not dry_run:
            with transaction.atomic():
                for chunk in _chunks(back):
                    MissingItem.objects.filter(kind=kind, item__in=chunk).delete()
                MissingItem.objects.bulk_create([MissingItem(kind=kind, item=i, since=now) for i in new])
                for chunk in _chunks(due):
                    deleted += Like.objects.filter(kind=kind, item__in=chunk).delete()[0]
                    MissingItem.objects.filter(kind=kind, item__in=chunk).delete()
        else:
            deleted = sum(Like.objects.filter(kind=kind, item__in=chunk).count() for chunk in _chunks(due))
        report[kind] = {"missing": len(missing), "purged": len(due), "likes": deleted, "skipped": False}
        if due and not dry_run:
            log.info("accounts: deleted %d like(s) of %d %s(s) gone from the game server", deleted, len(due), kind)
    if not dry_run:
        _forget_counts()
    return report


def stats(now=None):
    """The service's numbers, for the operator."""
    now = now or timezone.now()
    day = now - timedelta(days=1)
    links = GameLink.objects
    likes_by_kind = Counter(Like.objects.values_list("kind", flat=True))
    return {
        "accounts": User.objects.filter(is_staff=False, is_superuser=False).count(),
        "accounts_new_today": User.objects.filter(is_staff=False, is_superuser=False, date_joined__gte=day).count(),
        "accounts_suspended": User.objects.filter(is_active=False).count(),
        "links": links.count(),
        "links_frozen": links.filter(state=GameLink.FROZEN).count(),
        "links_new_today": links.filter(linked_at__gte=day).count(),
        "links_daily_cap": settings.LINKS_DAILY,
        "likes_photo": likes_by_kind.get(Like.PHOTO, 0),
        "likes_design": likes_by_kind.get(Like.DESIGN, 0),
        "likes_video": likes_by_kind.get(Like.VIDEO, 0),
        "likes_new_today": Like.objects.filter(at__gte=day).count(),
        "liked_items_missing": MissingItem.objects.count(),
    }
