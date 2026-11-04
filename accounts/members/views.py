"""Three of allauth's headless views with the design's rules added; urls.py routes them in place
of allauth's own.

- Request a code: the address is replaced by its stand-in (members.identity) before allauth sees
  it, and an address with no account gets one, within the new-account limits, so signing in by
  email is signing up and every address gets the same answer. The code goes only to the address
  typed in this request.
- Confirm a code: wrong codes count against the visitor's address (20 an hour) besides allauth's 5
  tries per code.
- Start a Discord or GitHub sign-in: 20 an hour per address.
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
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect, JsonResponse

from members import adapters, identity, limits
from members.models import AuditEvent, User

log = logging.getLogger("members")


class StandInLoginCodeInput(RequestLoginCodeInput):
    def clean_email(self):
        address = self.cleaned_data["email"]
        if not address:
            return address
        request = context.request
        if not settings.EMAIL_SIGNIN_ENABLED:
            raise ValidationError("Email sign-in is not available.", code="unavailable")
        if not limits.take(request, "login_code_mail_all", key="all", dry_run=True):
            raise ValidationError("Email sign-in is paused for today. Use Discord or GitHub, or try tomorrow.",
                                  code="paused")
        stand_in = identity.stand_in(address)
        if not limits.take(request, "request_login_code", key=stand_in):
            raise get_adapter().validation_error("too_many_login_attempts")
        users = filter_users_by_email(stand_in, prefer_verified=True, for_login=True)
        user = next((u for u in users if u.is_active), None)
        # A suspended account (members.moderation) gets no code, and no new account in its place.
        if not users and adapters.new_account_allowed(request):
            user = User.objects.create()
            EmailAddress.objects.create(user=user, email=stand_in, primary=True, verified=False)
            AuditEvent.objects.create(user=user, event="created with email")
        if user is not None:
            limits.take(request, "login_code_mail_all", key="all")
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
        return super().dispatch(request, *args, **kwargs)

    def handle_invalid_input(self, input):
        limits.take(self.request, "code_failed")
        return super().handle_invalid_input(input)


class LimitedRedirectToProviderView(RedirectToProviderView):
    def post(self, request, *args, **kwargs):
        if not limits.take(request, "signin_start"):
            page = adapters.safe_return(request.POST.get("callback_url")) or "/account.html"
            return HttpResponseRedirect(settings.ORIGIN + page + "?error=busy")
        return super().post(request, *args, **kwargs)
