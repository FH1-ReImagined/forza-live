"""An account is a Django user whose username is its public id (8 letters of the share-code
alphabet, shown as K7Q2-XM4P). It keeps no name, no email address and no password:
- Discord and GitHub sign-ins are allauth SocialAccounts with the provider's numeric id only
  (members.signals empties extra_data);
- an email sign-in is an allauth EmailAddress whose address is HMAC-SHA256(pepper, address)
  @email.invalid (members.identity), so the real address is never stored.

SignIn tracks each account's sessions, to end the oldest past MAX_SESSIONS and every one of them
for "sign out everywhere"; AuditEvent is what the account page shows (90 days, no addresses).
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
