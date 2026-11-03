"""What happens around a sign-in: the audit events, the session records, the provider's data."""

import time

from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY, get_authentication_records
from allauth.account.signals import user_signed_up
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.contrib.sessions.models import Session
from django.db.models.signals import pre_save
from django.dispatch import receiver

from members.models import AuditEvent, SignIn


@receiver(pre_save, sender=SocialAccount)
def keep_only_the_id(sender, instance, **kwargs):
    """Discord's and GitHub's user data (name, avatar, email...) is never stored: only the uid."""
    instance.extra_data = {}


def method_of(request, user):
    records = get_authentication_records(request, user) if request is not None else []
    last = records[-1] if records else {}
    if last.get("method") == "socialaccount":
        return last.get("provider") or "a provider"
    if last.get("method") == "code":
        return "email"
    return last.get("method") or "unknown"


@receiver(user_signed_up)
def signed_up(sender, request, user, sociallogin=None, **kwargs):
    if sociallogin is not None:
        AuditEvent.objects.create(user=user, event="created with " + sociallogin.provider.id)


@receiver(user_logged_in)
def signed_in(sender, request, user, **kwargs):
    if request is None or not hasattr(request, "session"):
        return
    request.session["fh_t0"] = int(time.time())
    if not request.session.session_key:
        request.session.save()
    key = request.session.session_key
    SignIn.objects.update_or_create(session_key=key, defaults={"user": user})
    AuditEvent.objects.create(user=user, event="signed in with " + method_of(request, user))
    # allauth's record of how this session signed in keeps the method, provider and time only:
    # not the provider's user id or the email stand-in, which its session answers would show.
    records = request.session.get(AUTHENTICATION_METHODS_SESSION_KEY)
    if isinstance(records, list):
        request.session[AUTHENTICATION_METHODS_SESSION_KEY] = [
            {k: v for k, v in r.items() if k in ("method", "at", "provider")} for r in records if isinstance(r, dict)]
    # At most MAX_SESSIONS: the oldest sign-ins end.
    old = list(SignIn.objects.filter(user=user).order_by("-created", "-id")
               .values_list("session_key", flat=True)[settings.MAX_SESSIONS:])
    if old:
        Session.objects.filter(session_key__in=old).delete()
        SignIn.objects.filter(session_key__in=old).delete()


@receiver(user_logged_out)
def signed_out(sender, request, user, **kwargs):
    if request is None or user is None:
        return
    key = request.session.session_key
    if key:
        SignIn.objects.filter(session_key=key).delete()
    AuditEvent.objects.create(user=user, event="signed out")
