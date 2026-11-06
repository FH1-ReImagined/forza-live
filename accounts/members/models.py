"""An account is a Django user whose username is its public id (8 letters of the share-code
alphabet, shown as K7Q2-XM4P). It keeps no name, no email address and no password:
- Discord and GitHub sign-ins are allauth SocialAccounts with the provider's numeric id only
  (members.signals empties extra_data);
- an email sign-in is an allauth EmailAddress whose address is HMAC-SHA256(pepper, address)
  @email.invalid (members.identity), so the real address is never stored.

SignIn tracks each account's sessions, to end the oldest past MAX_SESSIONS and every one of them
for "sign out everywhere"; AuditEvent is what the account page shows (90 days, no addresses).

GameLink binds an account to one game key (members.links): one XUID per account and one account
per XUID, with the key's public key pinned. LinkRequest is a link in progress (10 minutes): it
keeps only hashes of its token and code. SignedNonce is the launcher's request nonces, shared by
every worker, so a signed request works once.

Like is a thumbs-up for a community photo or design (members.likes). It belongs to the game key
(the XUID), not to the account: a second account, or a link that moved, cannot like twice. Who
liked what is never shown; only the totals are public. MissingItem is a liked item that the
nightly purge (members.moderation) no longer found in the game server's files, and since when:
its likes go once it has been missing for LIKES_PURGE_GRACE_DAYS.

MonthWinner is a Photo of the Month the operator confirmed (members.potm), with what the
standings said when they did: it stays when the photo or its likes go later. Disqualified is a
photo the operator took out of one month's contest (it stays in the gallery).

HubItem is a mod or texture pack on the mods and texture packs hub (members.hub), in every state
from its upload to its takedown; it belongs to the game key that uploaded it, like a like.
HubReport is a player's report of one (one per key and item).
"""

import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # forza_live.content.designs.CODE_CHARS


def new_public_id():
    return "".join(secrets.choice(CODE_CHARS) for _ in range(8))


class User(AbstractUser):
    def save(self, *args, **kwargs):
        if not self.username:
            while True:
                candidate = new_public_id()
                if not User.objects.filter(username=candidate).exists():
                    self.username = candidate
                    break
        if not self.password:
            self.set_unusable_password()
        super().save(*args, **kwargs)

    @property
    def public_id(self):
        return self.username[:4] + "-" + self.username[4:]


class SignIn(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sign_ins")
    session_key = models.CharField(max_length=40, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="audit_events")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    event = models.CharField(max_length=64)

    class Meta:
        ordering = ["-at", "-id"]


class GameLink(models.Model):
    ACTIVE, FROZEN = "active", "frozen"

    xuid = models.CharField(max_length=16, primary_key=True)  # 16 upper-case hex digits
    pub = models.CharField(max_length=64)  # the key's public key when it was linked (hex)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="game_link")
    gamertag = models.CharField(max_length=15, blank=True)  # as last seen, for the account page
    state = models.CharField(max_length=8, choices=[(ACTIVE, "active"), (FROZEN, "frozen")], default=ACTIVE)
    linked_at = models.DateTimeField()
    moved_at = models.DateTimeField(null=True, blank=True)  # the key moved here from another account
    frozen_at = models.DateTimeField(null=True, blank=True)


class LinkRequest(models.Model):
    PENDING, CLAIMED, DECLINED, LINKED, CANCELLED = "pending", "claimed", "declined", "linked", "cancelled"
    STATES = [(s, s) for s in (PENDING, CLAIMED, DECLINED, LINKED, CANCELLED)]

    id = models.CharField(max_length=32, primary_key=True)  # 128 random bits (hex)
    xuid = models.CharField(max_length=16, db_index=True)
    pub = models.CharField(max_length=64)
    gamertag = models.CharField(max_length=15)
    token_hash = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField()
    expires = models.DateTimeField(db_index=True)
    state = models.CharField(max_length=10, choices=STATES, default=PENDING)
    claimed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                                   related_name="+")
    claimed_session = models.CharField(max_length=40, blank=True)


class SignedNonce(models.Model):
    nonce = models.CharField(max_length=32, primary_key=True)
    until = models.DateTimeField(db_index=True)


class Like(models.Model):
    PHOTO, DESIGN, MOD, PACK = "photo", "design", "mod", "pack"
    KINDS = [(PHOTO, "photo"), (DESIGN, "design"), (MOD, "mod"), (PACK, "texture pack")]

    xuid = models.CharField(max_length=16)  # the game key's XUID (16 upper-case hex digits)
    kind = models.CharField(max_length=6, choices=KINDS)
    item = models.CharField(max_length=12)  # the photo's or design's public id
    at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["xuid", "kind", "item"], name="one_like_per_key")]
        indexes = [models.Index(fields=["kind", "item"], name="likes_of_an_item"),
                   models.Index(fields=["kind", "at"], name="likes_by_time")]  # a month's (members.potm)


class MissingItem(models.Model):
    kind = models.CharField(max_length=6, choices=Like.KINDS)
    item = models.CharField(max_length=12)
    since = models.DateTimeField()  # the first purge that did not find it

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "item"], name="one_missing_row_per_item")]


class MonthWinner(models.Model):
    """A confirmed Photo of the Month. Its month is "YYYY-MM" (UTC)."""
    month = models.CharField(max_length=7, unique=True)
    photo = models.CharField(max_length=12)
    votes = models.PositiveIntegerField()  # the likes that counted for the contest
    reached = models.DateTimeField()       # when the photo reached that count (the tie-break)
    title = models.CharField(max_length=40, blank=True)
    gamertag = models.CharField(max_length=15)
    owner = models.CharField(max_length=16)  # the owner's XUID, for the operator only
    confirmed_at = models.DateTimeField()
    confirmed_by = models.CharField(max_length=150)  # the operator's username
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-month"]
        verbose_name = "Photo of the Month"
        verbose_name_plural = "Photos of the Month"

    def __str__(self):
        return "%s: %s" % (self.month, self.photo)


class Disqualified(models.Model):
    """A photo out of a month's contest (vote manipulation, not the player's own, the rules)."""
    month = models.CharField(max_length=7)
    photo = models.CharField(max_length=12)
    note = models.CharField(max_length=200)
    at = models.DateTimeField()
    by = models.CharField(max_length=150)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "photo"], name="one_disqualification_per_entry")]
        ordering = ["-month", "-at"]
        verbose_name = "disqualified entry"
        verbose_name_plural = "disqualified entries"


class HubItem(models.Model):
    """A mod or texture pack (members.hub). Its id is 12 characters of the photos' alphabet."""
    MOD, PACK = "mod", "pack"
    KINDS = [(MOD, "mod"), (PACK, "texture pack")]
    DRAFT, UPLOADING, QUEUED, REFUSED, REVIEW, LIVE, HIDDEN, REMOVED = (
        "draft", "uploading", "queued", "refused", "review", "live", "hidden", "removed")
    STATES = [(s, s) for s in (DRAFT, UPLOADING, QUEUED, REFUSED, REVIEW, LIVE, HIDDEN, REMOVED)]

    id = models.CharField(max_length=12, primary_key=True)
    kind = models.CharField(max_length=4, choices=KINDS)
    state = models.CharField(max_length=9, choices=STATES, default=DRAFT, db_index=True)
    owner = models.CharField(max_length=16, db_index=True)  # the uploading key's XUID (never public)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="hub_items")
    title = models.CharField(max_length=60)
    description = models.TextField(max_length=1000, blank=True)
    version = models.CharField(max_length=20, blank=True)
    created = models.DateTimeField()
    updated = models.DateTimeField()
    upload_size = models.BigIntegerField(default=0)       # the upload as it came
    upload_sha256 = models.CharField(max_length=64, blank=True)
    file_size = models.BigIntegerField(default=0)         # the clean zip the worker wrote (what is served)
    sha256 = models.CharField(max_length=64, blank=True)  # its SHA-256, shown on the page
    files = models.PositiveIntegerField(default=0)
    unpacked = models.BigIntegerField(default=0)
    folder = models.CharField(max_length=40, blank=True)  # the download's name, and the folder in it
    names = models.TextField(blank=True)                  # its first paths, for the moderator (JSON)
    manifest = models.TextField(blank=True)               # mod.toml or pack.toml, cleaned (JSON)
    reason = models.CharField(max_length=300, blank=True)  # why it was refused or hidden (the uploader sees it)
    scanner = models.CharField(max_length=80, blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    published = models.DateTimeField(null=True, blank=True)
    reports = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created"]
        verbose_name = "hub item"

    def __str__(self):
        return "%s %s (%s)" % (self.kind, self.id, self.state)


class HubReport(models.Model):
    MALWARE, NOT_OWN, GAME_FILES, BROKEN, OFFENSIVE, OTHER = (
        "malware", "not-own-work", "game-files", "broken", "offensive", "other")
    REASONS = [(MALWARE, "malware or a harmful file"), (NOT_OWN, "not the uploader's own work"),
               (GAME_FILES, "the game's original files"), (BROKEN, "does not work"),
               (OFFENSIVE, "offensive"), (OTHER, "something else")]

    item = models.ForeignKey(HubItem, on_delete=models.CASCADE, related_name="report_set")
    xuid = models.CharField(max_length=16)  # the reporting key
    reason = models.CharField(max_length=12, choices=REASONS)
    note = models.CharField(max_length=500, blank=True)
    at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["item", "xuid"], name="one_report_per_key")]
        ordering = ["-at"]
