"""allauth adapters: the design's rules on top of allauth's flows.

- Redirects (the `return` page after a Discord or GitHub sign-in) go only to a page of the site
  itself, /<name>.html: nothing else, so there is no open redirect.
- No password and no form signup: accounts are made by a first sign-in only, within the
  new-account limits.
- Nothing about the person is kept from Discord or GitHub but the numeric id (members.signals
  empties extra_data; no name, username, avatar, email address or token).
- The sign-in mail goes to the address typed in this same request (members.views), never to the
  stand-in allauth stores, and is fixed plain text.
"""

import logging
import re

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core import context
from allauth.headless.adapter import DefaultHeadlessAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.core.mail import EmailMessage
from django.utils.http import url_has_allowed_host_and_scheme

from members import identity, limits, net

log = logging.getLogger("members")

RETURN_PAGE = re.compile(r"/[a-z0-9-]+\.html")
MAIL_TO = "_fh_mail_to"  # request attribute: stand-in -> the address typed in this request


def safe_return(url):
    """A page of the site (absolute on the site's origin, or a path), as a path; else None."""
    if not isinstance(url, str):
        return None
    if url.startswith(settings.ORIGIN + "/"):
        url = url[len(settings.ORIGIN):]
    if not RETURN_PAGE.fullmatch(url) or not url_has_allowed_host_and_scheme(url, allowed_hosts=None):
        return None
    return url


def mail_text(code):
    site = settings.SITE_HOST
    shown = code[:4] + " " + code[4:] if len(code) == 8 else code
    return ("Your sign-in code for %s is %s. It works for 10 minutes in the browser where you asked "
            "for it. If you did not ask, ignore this mail.\n\n"
            "Or open this link in that same browser:\n%s/account.html#code=%s\n"
            % (site, shown, settings.ORIGIN, code))


class AccountAdapter(DefaultAccountAdapter):
    def get_client_ip(self, request):
        return net.client_ip(request)

    def is_safe_url(self, url):
        return safe_return(url) is not None

    def is_open_for_signup(self, request):
        return False  # no password or form signup; social sign-ups ask SocialAccountAdapter

    def add_message(self, *args, **kwargs):
        pass  # the static site shows its own messages; no messages cookie

    def populate_username(self, request, user):
        pass  # members.models.User gives every account its public id

    def send_mail(self, template_prefix, email, context_):
        request = context.request
        if template_prefix != "account/email/login_code" or not identity.is_stand_in(email):
            log.info("accounts: mail %s not sent (only sign-in codes are)", template_prefix)
            return
        to = getattr(request, MAIL_TO, {}).get(email)
        if not to:
            return  # not asked for in this request (a resend): nothing to send it to
        message = EmailMessage("Your sign-in code for " + settings.SITE_HOST, mail_text(context_["code"]),
                               settings.DEFAULT_FROM_EMAIL, [to], headers={"Auto-Submitted": "auto-generated"})
        try:
            message.send()
        except Exception as e:  # never the address: smtplib's errors carry it
            log.warning("accounts: a sign-in mail failed: %s %s", type(e).__name__, getattr(e, "smtp_code", ""))


def new_account_allowed(request):
    """The new-account limits: per IP, and for the whole service."""
    return limits.take(request, "new_account", key="all")


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        allowed = new_account_allowed(request)
        if not allowed:
            log.info("accounts: a %s sign-up was refused by the new-account limits", sociallogin.provider.id)
        return allowed

    def populate_user(self, request, sociallogin, data):
        return sociallogin.user  # no name, username or email address from the provider

    def pre_social_login(self, request, sociallogin):
        sociallogin.email_addresses = []
        sociallogin.account.extra_data = {}

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        log.warning("accounts: a %s sign-in failed: %s %s", getattr(provider, "id", provider), error,
                    type(exception).__name__ if exception else "")


class HeadlessAdapter(DefaultHeadlessAdapter):
    def serialize_user(self, user):
        """allauth's session answers name the account by its public id only: no database id and
        no email stand-in."""
        return {"id": user.public_id, "display": user.public_id, "has_usable_password": False}
