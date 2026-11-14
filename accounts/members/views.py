"""Three of allauth's headless views with the design's rules added; urls.py routes them in place
of allauth's own.

- Request a code: the address is replaced by its stand-in (members.identity) before allauth sees
  it, and an address with no account gets one, within the new-account limits, so signing in by
  email is signing up and every address gets the same answer. The code goes only to the address
  typed in this request. The per-address limit counts the mailbox (identity.mailbox: every
  spelling of it together), and a browser that signed in with the address before has a budget of
  its own (KNOWN_COOKIE), so other people's requests for an address cannot keep its owner out.
  Addresses that never finished a sign-in share half of the day's mails; the rest is the members'.
- Confirm a code: wrong codes count against the visitor's address (20 an hour) besides allauth's 5
  tries per code.
- Start a Discord or GitHub sign-in: 20 an hour per address, and only to sign in (allauth's
  "connect", which adds the provider's account to the signed-in account, is refused).
"""

import logging

from allauth.account.adapter import get_adapter
from allauth.account.internal.userkit import filter_users_by_email
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.headless.account.inputs import RequestLoginCodeInput
from allauth.headless.account.views import ConfirmLoginCodeView, RequestLoginCodeView
from allauth.headless.socialaccount.views import RedirectToProviderView
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect, JsonResponse

from members import adapters, identity, limits
from members.models import AuditEvent, User

log = logging.getLogger("members")

# Set when a code signs a browser in, and for a browser that holds a session of an account with
# an email address (members.middleware.KnownBrowser: the members from before this cookie get it
# at their next visit, not at their next code): the stand-in of the address, signed. A code
# request that carries it for the same address counts against request_login_code_known, not the
# mailbox's public budget. It names no address and opens nothing.
KNOWN_COOKIE = "__Host-fh_known"
KNOWN_SALT = "members.views.known"
KNOWN_DAYS = 400
MAILS_OUT_WARNED = "fh:mails-out-warned:"  # + "all" or "new": one warning a day each


def known_stand_in(request):
    """The stand-in this browser signed in with before (KNOWN_COOKIE), or None."""
    value = request.COOKIES.get(KNOWN_COOKIE)
    if not value:
        return None
    try:
        return signing.loads(value, salt=KNOWN_SALT, max_age=KNOWN_DAYS * 86400)
    except signing.BadSignature:
        return None


def stand_in_of(user):
    """The stand-in of the account's email address, or None for an account without one."""
    stand_in = EmailAddress.objects.filter(user=user, verified=True).values_list("email", flat=True).first()
    return stand_in if stand_in and identity.is_stand_in(stand_in) else None


def set_known(response, stand_in):
    response.set_cookie(KNOWN_COOKIE, signing.dumps(stand_in, salt=KNOWN_SALT), max_age=KNOWN_DAYS * 86400,
                        path="/", secure=True, httponly=True, samesite="Strict")


def warn_mails_out(which, message):
    if cache.add(MAILS_OUT_WARNED + which, True, 86400):
        log.warning("accounts: %s", message)


class StandInLoginCodeInput(RequestLoginCodeInput):
    def clean_email(self):
        address = self.cleaned_data["email"]
        if not address:
            return address
        request = context.request
        if not settings.EMAIL_SIGNIN_ENABLED:
            raise ValidationError("Email sign-in is not available.", code="unavailable")
        box = identity.mailbox(address)
        if box is None:  # another spelling of an address (identity.mailbox): no account, no mail
            raise ValidationError("Enter a valid email address.", code="invalid")
        if not limits.take(request, "login_code_mail_all", key="all", dry_run=True):
            warn_mails_out("all", "the day's %d code mails are used up: email sign-in is paused"
                           % settings.EMAIL_DAILY)
            raise ValidationError("Email sign-in is paused for today. Use Discord or GitHub, or try tomorrow.",
                                  code="paused")
        stand_in = identity.stand_in(address)
        if known_stand_in(request) == stand_in:
            allowed = limits.take(request, "request_login_code_known", key=stand_in)
        else:
            allowed = limits.take(request, "request_login_code", key=identity.mailbox_key(box))
        if not allowed:
            raise get_adapter().validation_error("too_many_login_attempts")
        users = filter_users_by_email(stand_in, prefer_verified=True, for_login=True)
        user = next((u for u in users if u.is_active), None)
        # An address that never finished a sign-in (a new one too) draws on the strangers' share
        # of the day's mails. Without room it gets the answer any address gets, and no mail.
        member = user is not None and EmailAddress.objects.filter(user=user, verified=True).exists()
        room = member or limits.take(request, "login_code_mail_new", key="all", dry_run=True)
        if not room:
            warn_mails_out("new", "the day's code mails for addresses that never signed in are used up: "
                           "they get none until tomorrow (accounts that have signed in still do)")
            user = None
        # A suspended account (members.moderation) gets no code, and no new account in its place.
        if not users and room and adapters.new_account_allowed(request):
            user = User.objects.create()
            EmailAddress.objects.create(user=user, email=stand_in, primary=True, verified=False)
            AuditEvent.objects.create(user=user, event="created with email")
        if user is not None:
            limits.take(request, "login_code_mail_all", key="all")
            if not member:
                limits.take(request, "login_code_mail_new", key="all")
            mail_to = getattr(request, adapters.MAIL_TO, {})
            mail_to[stand_in] = identity.normalize(address)
            setattr(request, adapters.MAIL_TO, mail_to)
        # No account and no room for one: allauth answers as for any address and sends nothing.
        self._user = user
        return stand_in


class StandInRequestLoginCodeView(RequestLoginCodeView):
    input_class = StandInLoginCodeInput


class CountedConfirmLoginCodeView(ConfirmLoginCodeView):
    def dispatch(self, request, *args, **kwargs):
        if not limits.take(request, "code_failed", dry_run=True):
            return JsonResponse({"status": 429, "errors": [{"code": "too_many_attempts",
                                 "message": "Too many wrong codes. Try again later."}]}, status=429)
        response = super().dispatch(request, *args, **kwargs)
        if response.status_code == 200 and request.method == "POST" and request.user.is_authenticated:
            stand_in = stand_in_of(request.user)
            if stand_in:
                set_known(response, stand_in)
        return response

    def handle_invalid_input(self, input):
        limits.take(self.request, "code_failed")
        return super().handle_invalid_input(input)


class LimitedRedirectToProviderView(RedirectToProviderView):
    def post(self, request, *args, **kwargs):
        # The site only signs in. allauth's other process, "connect", would add the provider's
        # account to the account of this session as one more way in, for good and with no trace:
        # whoever holds a session for a moment could keep the account.
        if request.POST.get("process") != "login":
            return JsonResponse({"status": 400, "errors": [{"code": "invalid", "param": "process",
                                 "message": "Only sign-in is available."}]}, status=400)
        if not limits.take(request, "signin_start"):
            page = adapters.safe_return(request.POST.get("callback_url")) or "/account.html"
            return HttpResponseRedirect(settings.ORIGIN + page + "?error=busy")
        return super().post(request, *args, **kwargs)
