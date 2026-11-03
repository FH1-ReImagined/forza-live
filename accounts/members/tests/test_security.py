"""Cookies, CSRF, the Origin and Sec-Fetch-Site matrix, the answer headers, the routes allauth
must not expose, and the admin and API docs allow list."""

import ipaddress
import json

from django.core.cache import cache
from django.test import override_settings

from members.tests.support import CODE_REQUEST, CONFIG, SESSION, AccountsTest, Browser, fake_provider


def cookie_attrs(response, name):
    morsel = response.cookies[name]
    return {"secure": bool(morsel["secure"]), "httponly": bool(morsel["httponly"]),
            "samesite": morsel["samesite"], "path": morsel["path"], "domain": morsel["domain"]}


class Cookies(AccountsTest):
    def test_session_cookie(self):
        with fake_provider() as fake:
            _, _, done = self.oauth_sign_in(Browser(), fake)
        self.assertEqual(cookie_attrs(done, "__Host-fh_session"),
                         {"secure": True, "httponly": True, "samesite": "Lax", "path": "/", "domain": ""})
        self.assertEqual(int(done.cookies["__Host-fh_session"]["max-age"]), 30 * 86400)

    def test_csrf_cookie(self):
        r = Browser().get(CONFIG, secure=True)
        self.assertEqual(cookie_attrs(r, "__Host-fh_csrf"),
                         {"secure": True, "httponly": False, "samesite": "Strict", "path": "/", "domain": ""})

    def test_no_other_cookie(self):
        with fake_provider() as fake:
            _, _, done = self.oauth_sign_in(Browser(), fake)
        self.assertEqual(set(done.cookies), {"__Host-fh_session", "__Host-fh_csrf"})


class Changes(AccountsTest):
    """Every change needs the CSRF token, the site's Origin, and Sec-Fetch-Site same-origin when
    the browser sends one."""

    def signed_in_browser(self):
        cache.clear()
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake)
        return b

    def test_origin_and_fetch_site_matrix(self):
        cases = [
            # (Origin, Sec-Fetch-Site, accepted)
            ("https://forza.dyotson.dev", "same-origin", True),
            ("https://forza.dyotson.dev", None, True),
            ("https://forza.dyotson.dev", "same-site", False),
            ("https://forza.dyotson.dev", "cross-site", False),
            ("https://forza.dyotson.dev", "none", False),
            ("https://evil.example", "cross-site", False),
            ("https://evil.example", None, False),
            ("http://forza.dyotson.dev", None, False),
            ("https://forza.dyotson.dev.evil.example", None, False),
            ("https://sub.forza.dyotson.dev", "same-site", False),
            ("null", None, False),
        ]
        for origin, site, ok in cases:
            for path, body in (("/api/logout-all", {}), (CODE_REQUEST, {"email": "m@example.com"})):
                b = self.signed_in_browser()
                r = b.post_json(path, body, origin=origin, fetch_site=site)
                self.assertEqual(r.status_code != 403, ok, (path, origin, site, r.status_code, r.content[:80]))

    def test_no_csrf_token_is_refused(self):
        b = self.signed_in_browser()
        for path in ("/api/logout-all", "/api/account/delete", CODE_REQUEST):
            self.assertEqual(b.post_json(path, {}, token=False).status_code, 403, path)
        self.assertEqual(b.send("DELETE", SESSION, token=False).status_code, 403)
        self.assertEqual(b.get_json("/api/me").status_code, 200)  # still signed in

    def test_a_wrong_csrf_token_is_refused(self):
        b = self.signed_in_browser()
        r = b.post_json("/api/logout-all", {}, token=False, HTTP_X_CSRFTOKEN="x" * 32)
        self.assertEqual(r.status_code, 403)

    def test_a_form_post_to_our_api_is_refused(self):
        b = self.signed_in_browser()
        r = b.post("/api/logout-all", {"csrfmiddlewaretoken": b.csrf()}, secure=True,
                   HTTP_ORIGIN="https://forza.dyotson.dev")
        self.assertEqual(r.status_code, 415)
        self.assertEqual(b.get_json("/api/me").status_code, 200)

    def test_get_changes_nothing(self):
        b = self.signed_in_browser()
        for path in ("/api/logout-all", "/api/account/delete"):
            self.assertEqual(b.get_json(path).status_code, 405)
        self.assertEqual(b.get_json("/api/me").status_code, 200)


class Headers(AccountsTest):
    def test_api_answers(self):
        b = Browser()
        for path in ("/api/health", "/api/me", CONFIG, SESSION):
            r = b.get(path, secure=True, HTTP_ORIGIN="https://evil.example")
            self.assertEqual(r["Cache-Control"], "no-store", path)
            self.assertEqual(r["X-Content-Type-Options"], "nosniff", path)
            self.assertTrue(r["Content-Type"].startswith("application/json"), path)
            self.assertFalse([h for h in r.headers if h.lower().startswith("access-control")], path)

    def test_options_is_405(self):
        for path in ("/api/me", "/api/logout-all", CODE_REQUEST):
            r = Browser().options(path, secure=True, HTTP_ORIGIN="https://evil.example",
                                  HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
            self.assertEqual(r.status_code, 405, path)
            self.assertFalse([h for h in r.headers if h.lower().startswith("access-control")])

    def test_framing_is_denied(self):
        self.assertEqual(Browser().get("/api/health", secure=True)["X-Frame-Options"], "DENY")

    def test_unknown_host_is_refused(self):
        r = Browser().get("/api/health", secure=True, HTTP_HOST="evil.example")
        self.assertEqual(r.status_code, 400)


class Routes(AccountsTest):
    """Only the routes the sign-ins use: no passwords, no form signup, no address management."""

    def test_allauth_routes_not_used_are_gone(self):
        h = "/api/auth/browser/v1/"
        for path in ("auth/login", "auth/signup", "auth/password/request", "auth/password/reset",
                     "auth/reauthenticate", "auth/email/verify", "auth/code/resend", "auth/provider/signup",
                     "auth/provider/token", "account/email", "account/password/change", "account/providers",
                     "account/phone", "auth/sessions"):
            r = Browser().post_json(h + path, {"email": "x@example.com", "password": "hunter2hunter2"})
            self.assertEqual(r.status_code, 404, path)
        for path in ("/api/auth/login/", "/api/auth/signup/", "/api/auth/discord/login/", "/api/auth/password/reset/",
                     "/api/auth/email/", "/api/auth/3rdparty/", "/api/auth/browser/v1/openapi.json"):
            self.assertEqual(Browser().get(path, secure=True).status_code, 404, path)

    def test_session_routes_allow_their_methods_only(self):
        self.assertEqual(Browser().post_json(SESSION, {}).status_code, 405)
        self.assertEqual(Browser().get(CODE_REQUEST, secure=True).status_code, 405)


class Admin(AccountsTest):
    def test_admin_only_from_allowed_addresses(self):
        self.assertEqual(Browser(ip="203.0.113.5").get("/admin/", secure=True).status_code, 404)
        self.assertEqual(Browser(ip="127.0.0.1").get("/admin/", secure=True).status_code, 302)  # to its login
        with override_settings(ADMIN_ALLOW=[ipaddress.ip_network("192.0.2.0/24")]):
            self.assertEqual(Browser(ip="192.0.2.9").get("/admin/", secure=True).status_code, 302)
            self.assertEqual(Browser(ip="127.0.0.1").get("/admin/", secure=True).status_code, 404)

    def test_x_real_ip_from_an_untrusted_peer_does_not_open_the_admin(self):
        r = Browser(ip="203.0.113.5").get("/admin/", secure=True, HTTP_X_REAL_IP="127.0.0.1")
        self.assertEqual(r.status_code, 404)

    def test_the_api_docs_are_off(self):
        for path in ("/api/docs", "/api/openapi.json"):
            self.assertEqual(Browser(ip="127.0.0.1").get(path, secure=True).status_code, 404, path)


class SessionAnswers(AccountsTest):
    """allauth's own answers show no provider id, email stand-in or database id either."""

    def test_after_discord(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid="987654321987")
        r = b.get_json(SESSION)
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        self.assertNotIn("987654321987", text)
        data = json.loads(text)["data"]
        self.assertEqual(set(data["user"]), {"id", "display", "has_usable_password"})
        self.assertRegex(data["user"]["id"], r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")

    def test_after_email(self):
        b = Browser()
        confirm = self.email_sign_in(b, "who@example.com")
        for r in (confirm, b.get_json(SESSION)):
            self.assertNotIn("email.invalid", r.content.decode())
            self.assertNotIn("example.com", r.content.decode())
