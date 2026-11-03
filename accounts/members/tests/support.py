"""What the tests share: a browser-like client (HTTPS, the site's Host and Origin, the CSRF
token from its cookie), a fake OAuth provider in place of Discord and GitHub, and the allauth
headless paths."""

import base64
import hashlib
import json
import secrets
from contextlib import contextmanager
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

ORIGIN = settings.ORIGIN
HOST = settings.SITE_HOST
H = "/api/auth/browser/v1/"
CONFIG, SESSION = H + "config", H + "auth/session"
CODE_REQUEST, CODE_CONFIRM = H + "auth/code/request", H + "auth/code/confirm"
PROVIDER_REDIRECT = H + "auth/provider/redirect"

PROVIDERS = {
    "discord": {"APPS": [{"client_id": "discord-test-id", "secret": "discord-test-secret"}],
                "OAUTH_PKCE_ENABLED": True, "SCOPE": ["identify"], "AUTH_PARAMS": {"prompt": "none"}},
    "github": {"APPS": [{"client_id": "github-test-id", "secret": "github-test-secret"}],
               "OAUTH_PKCE_ENABLED": True, "SCOPE": []},
}


class Browser(Client):
    """The site's page in a browser: HTTPS, Host forza.dyotson.dev, Origin and Sec-Fetch-Site
    same-origin on changes, and X-CSRFToken from the CSRF cookie (as account.js sends it)."""

    def __init__(self, ip="203.0.113.10", **kwargs):
        super().__init__(enforce_csrf_checks=True, HTTP_HOST=HOST, REMOTE_ADDR=ip, **kwargs)

    def csrf(self):
        cookie = self.cookies.get(settings.CSRF_COOKIE_NAME)
        if cookie is None:
            self.get(CONFIG, secure=True)
            cookie = self.cookies.get(settings.CSRF_COOKIE_NAME)
        return cookie.value

    def send(self, method, path, data=None, *, origin=ORIGIN, fetch_site="same-origin", token=True,
             content_type="application/json", **extra):
        headers = dict(extra)
        if origin is not None:
            headers["HTTP_ORIGIN"] = origin
        if fetch_site is not None:
            headers["HTTP_SEC_FETCH_SITE"] = fetch_site
        if token:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf()
        body = json.dumps(data) if content_type == "application/json" and data is not None else data
        return self.generic(method, path, body or "", content_type=content_type, secure=True, **headers)

    def post_json(self, path, data=None, **kw):
        return self.send("POST", path, data if data is not None else {}, **kw)

    def get_json(self, path, **kw):
        return self.get(path, secure=True, **kw)

    def form(self, path, data, *, origin=ORIGIN, fetch_site="same-origin"):
        """A plain HTML form submission (the sign-in buttons), with the CSRF token as a field."""
        data = dict(data, csrfmiddlewaretoken=self.csrf())
        headers = {"HTTP_ORIGIN": origin} if origin else {}
        if fetch_site:
            headers["HTTP_SEC_FETCH_SITE"] = fetch_site
        return self.post(path, data, secure=True, **headers)


def challenge_of(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class FakeResponse:
    def __init__(self, status, doc):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(doc)
        self.content = self.text.encode()
        self._doc = doc

    def json(self):
        return self._doc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class FakeProvider:
    """Discord's and GitHub's token and user endpoints, as allauth calls them through the
    adapter's requests session: a code is issued for an authorize URL, works once, and only with
    the client's secret, the exact redirect_uri and the PKCE verifier of the URL's challenge."""

    def __init__(self):
        self.codes = {}    # code -> (client id, redirect_uri, challenge, uid)
        self.tokens = {}   # access token -> uid
        self.token_calls = 0

    def authorize(self, url, uid):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = secrets.token_urlsafe(12)
        self.codes[code] = (q["client_id"], q["redirect_uri"], q.get("code_challenge"), str(uid))
        return code, q

    # requests.Session's interface, as far as allauth uses it
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, params=None, data=None, headers=None, auth=None, **kw):
        self.token_calls += 1
        data = data or {}
        entry = self.codes.pop(data.get("code"), None)
        name = "discord" if "discord.com" in url else "github"
        app = PROVIDERS[name]["APPS"][0]
        if (entry is None or data.get("client_id") != app["client_id"] or data.get("client_secret") != app["secret"]
                or data.get("redirect_uri") != entry[1] or data.get("grant_type") != "authorization_code"
                or not entry[2] or challenge_of(data.get("code_verifier", "")) != entry[2]):
            return FakeResponse(400, {"error": "invalid_grant"})
        token = secrets.token_urlsafe(16)
        self.tokens[token] = entry[3]
        return FakeResponse(200, {"access_token": token, "token_type": "Bearer", "scope": "identify"})

    def get(self, url, headers=None, **kw):
        token = (headers or {}).get("Authorization", "").split(" ")[-1]
        uid = self.tokens.get(token)
        if uid is None:
            return FakeResponse(401, {"message": "401: Unauthorized"})
        # What the providers really send: names, avatars, maybe an email. None of it may be kept.
        return FakeResponse(200, {"id": uid, "username": "player" + uid, "login": "player" + uid,
                                  "global_name": "Player", "avatar": "abc", "email": "p%s@example.com" % uid,
                                  "name": "Real Name"})


@contextmanager
def fake_provider():
    fake = FakeProvider()
    with mock.patch("members.adapters.SocialAccountAdapter.get_requests_session", return_value=fake):
        yield fake


@override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class AccountsTest(TestCase):
    def setUp(self):
        cache.clear()  # allauth's rate limits

    def oauth_start(self, browser, provider="discord", callback="/account.html"):
        return browser.form(PROVIDER_REDIRECT, {"provider": provider, "callback_url": callback, "process": "login"})

    def oauth_sign_in(self, browser, fake, uid="111222333444555666", provider="discord", callback="/account.html"):
        start = self.oauth_start(browser, provider, callback)
        self.assertEqual(start.status_code, 302, start.content)
        code, q = fake.authorize(start["Location"], uid)
        done = browser.get(urlsplit(q["redirect_uri"]).path, {"code": code, "state": q["state"]}, secure=True)
        return start, q, done

    def email_sign_in(self, browser, address="player@example.com"):
        from django.core import mail
        before = len(mail.outbox)
        r = browser.post_json(CODE_REQUEST, {"email": address})
        self.assertEqual(r.status_code, 401, r.content)
        self.assertEqual(len(mail.outbox), before + 1)
        code = mail.outbox[-1].body.split("#code=")[1].strip()
        r = browser.post_json(CODE_CONFIRM, {"code": code})
        self.assertEqual(r.status_code, 200, r.content)
        return r
