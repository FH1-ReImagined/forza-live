"""Discord and GitHub sign-in against a fake provider: the authorize request, state and PKCE,
a replayed callback, the return-page allowlist, what is kept, and the limits."""

import json
from urllib.parse import parse_qs, urlsplit

from allauth.socialaccount.models import SocialAccount
from django.core.cache import cache
from django.db import connection

from members.models import User
from members.tests.support import AccountsTest, Browser, challenge_of, fake_provider


def signed_in(browser):
    return browser.get_json("/api/me").status_code == 200


class AuthorizeRequest(AccountsTest):
    def test_discord_asks_identify_with_state_pkce_and_prompt_none(self):
        start = self.oauth_start(Browser(), "discord")
        self.assertEqual(start.status_code, 302)
        url = urlsplit(start["Location"])
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(url.netloc, "discord.com")
        self.assertEqual(q["scope"], "identify")
        self.assertEqual(q["prompt"], "none")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertGreaterEqual(len(q["code_challenge"]), 43)
        self.assertGreaterEqual(len(q["state"]), 12)
        self.assertEqual(q["redirect_uri"], "https://forza.dyotson.dev/api/auth/discord/login/callback/")

    def test_github_asks_no_scope(self):
        start = self.oauth_start(Browser(), "github")
        q = {k: v[0] for k, v in parse_qs(urlsplit(start["Location"]).query).items()}
        self.assertEqual(urlsplit(start["Location"]).netloc, "github.com")
        self.assertNotIn("scope", q)
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], "https://forza.dyotson.dev/api/auth/github/login/callback/")

    def test_the_start_is_a_post_with_csrf(self):
        b = Browser()
        self.assertEqual(b.get(PROVIDER_REDIRECT_PATH, {"provider": "discord", "callback_url": "/account.html",
                                                        "process": "login"}, secure=True).status_code, 405)
        r = b.post(PROVIDER_REDIRECT_PATH, {"provider": "discord", "callback_url": "/account.html", "process": "login"},
                   secure=True, HTTP_ORIGIN="https://forza.dyotson.dev")
        self.assertEqual(r.status_code, 403)  # no CSRF token


PROVIDER_REDIRECT_PATH = "/api/auth/browser/v1/auth/provider/redirect"


class Callback(AccountsTest):
    def test_sign_in_makes_an_account_with_only_the_numeric_id(self):
        b = Browser()
        with fake_provider() as fake:
            _, _, done = self.oauth_sign_in(b, fake, uid="80351110224678912")
        self.assertEqual(done.status_code, 302)
        self.assertEqual(done["Location"], "/account.html")
        self.assertTrue(signed_in(b))
        account = SocialAccount.objects.get()
        self.assertEqual((account.provider, account.uid), ("discord", "80351110224678912"))
        self.assertEqual(account.extra_data, {})
        user = account.user
        self.assertEqual((user.email, user.first_name, user.last_name), ("", "", ""))
        self.assertRegex(user.username, r"^[2-9A-HJ-NP-Z]{8}$")
        self.assertFalse(user.has_usable_password())
        # Nothing the provider said about the person is anywhere in the database.
        with connection.cursor() as c:
            for table in connection.introspection.table_names():
                c.execute('SELECT * FROM "%s"' % table)
                text = repr(c.fetchall())
                for leak in ("player80351110224678912", "Real Name", "example.com", "abc"):
                    self.assertNotIn(leak, text, table)

    def test_the_same_id_signs_in_to_the_same_account(self):
        with fake_provider() as fake:
            self.oauth_sign_in(Browser(), fake, uid="42")
            self.oauth_sign_in(Browser(ip="203.0.113.11"), fake, uid="42")
            self.oauth_sign_in(Browser(), fake, uid="42", provider="github")
        self.assertEqual(User.objects.count(), 2)  # Discord 42 and GitHub 42 are different people
        self.assertEqual(SocialAccount.objects.count(), 2)

    def test_state_mismatch_signs_nobody_in(self):
        b = Browser()
        with fake_provider() as fake:
            start = self.oauth_start(b)
            code, q = fake.authorize(start["Location"], "7")
            done = b.get("/api/auth/discord/login/callback/", {"code": code, "state": "not-" + q["state"]}, secure=True)
            self.assertEqual(fake.token_calls, 0)  # refused before the code is even exchanged
        self.assertEqual(done.status_code, 302)
        self.assertIn("error=", done["Location"])
        self.assertFalse(signed_in(b))
        self.assertEqual(User.objects.count(), 0)

    def test_a_callback_in_another_browser_signs_nobody_in(self):
        """Login CSRF: the state belongs to the browser that started the sign-in."""
        with fake_provider() as fake:
            start = self.oauth_start(Browser())
            code, q = fake.authorize(start["Location"], "7")
            other = Browser()
            done = other.get("/api/auth/discord/login/callback/", {"code": code, "state": q["state"]}, secure=True)
        self.assertIn("error=", done["Location"])
        self.assertFalse(signed_in(other))

    def test_a_replayed_callback_signs_nobody_in(self):
        b = Browser()
        with fake_provider() as fake:
            start, q, done = self.oauth_sign_in(b, fake, uid="9")
            self.assertTrue(signed_in(b))
            b.send("DELETE", "/api/auth/browser/v1/auth/session")
            self.assertFalse(signed_in(b))
            # The same code and state again (a copied URL, or the browser's history).
            code, q2 = fake.authorize(start["Location"], "9")
            again = b.get("/api/auth/discord/login/callback/", {"code": code, "state": q["state"]}, secure=True)
        self.assertIn("error=", again["Location"])
        self.assertFalse(signed_in(b))

    def test_pkce_mismatch_signs_nobody_in(self):
        b = Browser()
        with fake_provider() as fake:
            start = self.oauth_start(b)
            code, q = fake.authorize(start["Location"], "8")
            code_, challenge = code, fake.codes[code]
            fake.codes[code] = challenge[:2] + (challenge_of("another verifier"),) + challenge[3:]
            done = b.get("/api/auth/discord/login/callback/", {"code": code_, "state": q["state"]}, secure=True)
        self.assertIn("error=", done["Location"])
        self.assertFalse(signed_in(b))
        self.assertEqual(User.objects.count(), 0)

    def test_the_provider_refusing_signs_nobody_in(self):
        b = Browser()
        with fake_provider() as fake:
            start = self.oauth_start(b)
            q = {k: v[0] for k, v in parse_qs(urlsplit(start["Location"]).query).items()}
            done = b.get("/api/auth/discord/login/callback/", {"error": "access_denied", "state": q["state"]}, secure=True)
        self.assertTrue(done["Location"].startswith("/account.html?"))
        self.assertIn("error=", done["Location"])
        self.assertFalse(signed_in(b))


class ReturnPage(AccountsTest):
    def test_only_pages_of_the_site(self):
        for good in ("/account.html", "/link.html", "https://forza.dyotson.dev/link.html"):
            b = Browser()
            with fake_provider() as fake:
                _, _, done = self.oauth_sign_in(b, fake, uid="1", callback=good)
            self.assertIn(done["Location"], (good, "https://forza.dyotson.dev" + good), good)
        for bad in ("https://evil.example/account.html", "//evil.example/account.html", "/\\evil.example/a.html",
                    "javascript:alert(1)", "/account.html?x=1", "/account.html#x", "/../account.html",
                    "/Account.html", "/a/account.html", "http://forza.dyotson.dev/account.html",
                    "https://forza.dyotson.dev.evil.example/account.html", "account.html", "", "/"):
            cache.clear()
            start = self.oauth_start(Browser(), callback=bad)
            target = start.get("Location", "")
            self.assertFalse(target.startswith("https://discord.com"), bad)
            self.assertNotIn("evil", target, bad)


class SignInOnly(AccountsTest):
    """allauth's redirect also takes process=connect from a signed-in session, which adds the
    provider's account to that account as another way in. The site never asks for it."""

    def test_connect_is_refused(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid="111")
            self.assertTrue(signed_in(b))
            for process in ("connect", "", "redirect"):
                r = b.form(PROVIDER_REDIRECT_PATH, {"provider": "github", "callback_url": "/account.html",
                                                    "process": process})
                self.assertEqual(r.status_code, 400, process)
                self.assertNotIn("Location", r)
            r = b.form(PROVIDER_REDIRECT_PATH, {"provider": "github", "callback_url": "/account.html"})
            self.assertEqual(r.status_code, 400)
        self.assertEqual(list(SocialAccount.objects.values_list("provider", "uid")), [("discord", "111")])
        self.assertEqual(json.loads(b.get_json("/api/me").content)["methods"], ["discord"])

    def test_signing_in_still_starts(self):
        self.assertTrue(self.oauth_start(Browser())["Location"].startswith("https://discord.com"))


class Limits(AccountsTest):
    def test_twenty_sign_in_starts_an_hour_per_address(self):
        b = Browser(ip="198.51.100.7")
        for _ in range(20):
            self.assertTrue(self.oauth_start(b)["Location"].startswith("https://discord.com"))
        busy = self.oauth_start(b)
        self.assertEqual(busy["Location"], "https://forza.dyotson.dev/account.html?error=busy")
        # Another address is not affected.
        self.assertTrue(self.oauth_start(Browser(ip="198.51.100.8"))["Location"].startswith("https://discord.com"))

    def test_five_new_accounts_a_day_per_address(self):
        with fake_provider() as fake:
            for uid in range(5):
                b = Browser(ip="198.51.100.9")
                self.oauth_sign_in(b, fake, uid=str(uid))
                self.assertTrue(signed_in(b))
            b = Browser(ip="198.51.100.9")
            _, _, done = self.oauth_sign_in(b, fake, uid="5")
            self.assertFalse(signed_in(b))
            self.assertIn("error=", done["Location"])
            # An existing account still signs in from there.
            b = Browser(ip="198.51.100.9")
            self.oauth_sign_in(b, fake, uid="0")
            self.assertTrue(signed_in(b))
        self.assertEqual(User.objects.count(), 5)

    def test_x_real_ip_counts_only_from_the_trusted_proxy(self):
        import ipaddress
        from django.test import override_settings
        with override_settings(TRUSTED_PROXIES=[ipaddress.ip_network("10.0.0.5/32")]):
            nginx = Browser(ip="10.0.0.5")
            for i in range(21):
                r = self.oauth_start(nginx)
            self.assertTrue(r["Location"].endswith("error=busy"))  # no X-Real-IP: all count as nginx
            cache.clear()
            # With X-Real-IP from nginx, each visitor has a budget of its own.
            for i in range(21):
                r = nginx.post(PROVIDER_REDIRECT_PATH, {"provider": "discord", "callback_url": "/account.html",
                               "process": "login", "csrfmiddlewaretoken": nginx.csrf()}, secure=True,
                               HTTP_ORIGIN="https://forza.dyotson.dev", HTTP_X_REAL_IP="192.0.2.%d" % i)
                self.assertTrue(r["Location"].startswith("https://discord.com"))
            # Anyone else sending X-Real-IP is taken at its own address.
            other = Browser(ip="203.0.113.50")
            for i in range(21):
                r = other.post(PROVIDER_REDIRECT_PATH, {"provider": "discord", "callback_url": "/account.html",
                               "process": "login", "csrfmiddlewaretoken": other.csrf()}, secure=True,
                               HTTP_ORIGIN="https://forza.dyotson.dev", HTTP_X_REAL_IP="192.0.2.%d" % i)
            self.assertTrue(r["Location"].endswith("error=busy"))
