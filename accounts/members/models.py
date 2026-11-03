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
