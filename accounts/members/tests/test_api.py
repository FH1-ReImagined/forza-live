"""Our endpoints: me, logout-all, account delete; their schemas; the session cap and lifetime;
the daily upkeep and the integrity check."""

import datetime
import json
import sqlite3
import time
from io import StringIO
from unittest import mock

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from pydantic import ValidationError

from members import api
from members.management.commands import nightly
from members.models import AuditEvent, SignIn, User
from members.tests.support import SESSION, AccountsTest, Browser, fake_provider


def body(response):
    return json.loads(response.content)


class Me(AccountsTest):
    def test_signed_out(self):
        r = Browser().get_json("/api/me")
        self.assertEqual(r.status_code, 401)

    def test_signed_in(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid="123")
        doc = body(b.get_json("/api/me"))
        api.MeOut.model_validate(doc)
        self.assertEqual(set(doc), {"id", "created", "methods", "sessions", "recent", "link", "events"})
        self.assertEqual(doc["id"], User.objects.get().public_id)
        self.assertEqual(doc["methods"], ["discord"])
        self.assertEqual((doc["sessions"], doc["recent"], doc["link"]), (1, True, None))
        self.assertEqual([e["event"] for e in doc["events"]], ["signed in with discord", "created with discord"])
        self.assertNotIn("123", json.dumps(doc))  # never the provider's id

    def test_email_account_shows_no_address(self):
        b = Browser()
        self.email_sign_in(b, "who@example.com")
        doc = body(b.get_json("/api/me"))
        self.assertEqual(doc["methods"], ["email"])
        self.assertNotIn("@", json.dumps(doc))

    def test_recent_runs_out_after_ten_minutes(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake)
        with mock.patch("time.time", return_value=time.time() + 601):
            self.assertFalse(body(b.get_json("/api/me"))["recent"])


class LogoutAll(AccountsTest):
    def test_ends_every_session_of_the_account_only(self):
        mine = [Browser(ip="203.0.113.%d" % i) for i in range(3)]
        other = Browser()
        with fake_provider() as fake:
            for b in mine:
                self.oauth_sign_in(b, fake, uid="1")
            self.oauth_sign_in(other, fake, uid="2")
        self.assertEqual(body(mine[0].get_json("/api/me"))["sessions"], 3)
        r = mine[0].post_json("/api/logout-all", {})
        self.assertEqual(r.status_code, 200)
        api.LogoutAllOut.model_validate(body(r))
        self.assertEqual(body(r)["ended"], 3)
        for b in mine:
            self.assertEqual(b.get_json("/api/me").status_code, 401)
        self.assertEqual(other.get_json("/api/me").status_code, 200)
        # Signing in again works, and the account page shows what happened.
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid="1")
        self.assertIn("signed out everywhere", [e["event"] for e in body(b.get_json("/api/me"))["events"]])

    def test_this_browser_signs_out(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake)
        b.send("DELETE", SESSION)
        self.assertEqual(b.get_json("/api/me").status_code, 401)
        self.assertEqual(SignIn.objects.count(), 0)

    def test_rejects_extra_fields(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake)
        self.assertEqual(b.post_json("/api/logout-all", {"everyone": True}).status_code, 422)


class Delete(AccountsTest):
    def signed_in(self, uid="5"):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid=uid)
        return b, body(b.get_json("/api/me"))["id"]

    def test_deletes_everything_at_once(self):
        b, account = self.signed_in()
        second = Browser(ip="203.0.113.77")
        with fake_provider() as fake:
            self.oauth_sign_in(second, fake, uid="5")
        r = b.post_json("/api/account/delete", {"confirm": account})
        self.assertEqual((r.status_code, body(r)), (200, {"ok": True, "deleted": True}))
        for model in (User, SocialAccount, EmailAddress, SignIn, AuditEvent, Session):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(second.get_json("/api/me").status_code, 401)
        # The same Discord account makes a new account, with a new id.
        b, again = self.signed_in()
        self.assertNotEqual(again, account)

    def test_needs_a_recent_sign_in(self):
        b, account = self.signed_in()
        with mock.patch("time.time", return_value=time.time() + 601):
            r = b.post_json("/api/account/delete", {"confirm": account})
        self.assertEqual((r.status_code, body(r)), (403, {"error": "sign in again"}))
        self.assertEqual(User.objects.count(), 1)

    def test_needs_the_account_id(self):
        b, account = self.signed_in()
        other = "2222-2222" if account != "2222-2222" else "3333-3333"
        self.assertEqual(b.post_json("/api/account/delete", {"confirm": other}).status_code, 400)
        for bad in ({}, {"confirm": "nope"}, {"confirm": account, "also": 1}, {"confirm": 12345678}, []):
            self.assertEqual(b.post_json("/api/account/delete", bad).status_code, 422, bad)
        self.assertEqual(User.objects.count(), 1)

    def test_signed_out_is_401(self):
        self.assertEqual(Browser().post_json("/api/account/delete", {"confirm": "2222-2222"}).status_code, 401)


class Schemas(AccountsTest):
    def test_request_schemas(self):
        api.DeleteIn.model_validate({"confirm": "K7Q2-XM4P"})
        for bad in ({"confirm": "k7q2-xm4p"}, {"confirm": "K7Q2XM4P"}, {"confirm": "K7Q2-XM4I"},  # I is not a code letter
                    {"confirm": "K7Q2-XM4P", "x": 1}, {}):
            with self.assertRaises(ValidationError, msg=bad):
                api.DeleteIn.model_validate(bad)
        api.EmptyIn.model_validate({})
        with self.assertRaises(ValidationError):
            api.EmptyIn.model_validate({"x": 1})

    def test_response_schemas(self):
        good = {"id": "K7Q2-XM4P", "created": "2026-11-03", "methods": ["github"], "sessions": 1, "recent": False,
                "link": None, "events": []}
        api.MeOut.model_validate(good)
        for change in ({"methods": ["password"]}, {"sessions": -1}, {"id": "K7Q2"}, {"email": "a@b.c"}):
            with self.assertRaises(ValidationError, msg=change):
                api.MeOut.model_validate(dict(good, **change))

    def test_health(self):
        r = Browser().get_json("/api/health")
        self.assertEqual((r.status_code, body(r)), (200, {"ok": True}))


class Sessions(AccountsTest):
    def test_at_most_twenty_per_account(self):
        browsers = []
        with fake_provider() as fake:
            for i in range(21):
                b = Browser(ip="198.51.100.%d" % i)
                self.oauth_sign_in(b, fake, uid="7")
                browsers.append(b)
        self.assertEqual(SignIn.objects.count(), 20)
        self.assertEqual(browsers[0].get_json("/api/me").status_code, 401)  # the oldest ended
        self.assertEqual(browsers[-1].get_json("/api/me").status_code, 200)

    def test_thirty_days_idle_and_ninety_in_all(self):
        b = Browser()
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake)
        session = Session.objects.get()
        self.assertAlmostEqual((session.expire_date - timezone.now()).days, 29, delta=1)
        # Used every few weeks it stays, until 90 days after the sign-in.
        now = time.time()
        for day in (25, 50, 75):
            with mock.patch("time.time", return_value=now + day * 86400):
                self.assertEqual(b.get_json("/api/me").status_code, 200, day)
        with mock.patch("time.time", return_value=now + 91 * 86400):
            self.assertEqual(b.get_json("/api/me").status_code, 401)


class Upkeep(AccountsTest):
    def test_purge(self):
        with fake_provider() as fake:
            self.oauth_sign_in(Browser(), fake, uid="1")
            self.oauth_sign_in(Browser(), fake, uid="2")
        old, recent = User.objects.order_by("id")
        User.objects.filter(pk=old.pk).update(last_login=timezone.now() - datetime.timedelta(days=31))
        AuditEvent.objects.filter(user=recent).update(at=timezone.now() - datetime.timedelta(days=91))
        # An email account whose code was never used, after a day.
        Browser().post_json("/api/auth/browser/v1/auth/code/request", {"email": "never@example.com"})
        User.objects.filter(last_login__isnull=True).update(date_joined=timezone.now() - datetime.timedelta(days=2))
        staff = User.objects.create(is_staff=True, last_login=timezone.now() - datetime.timedelta(days=400))
        gone = nightly.purge()
        self.assertEqual(gone, 2)
        self.assertEqual(set(User.objects.values_list("pk", flat=True)), {recent.pk, staff.pk})
        self.assertEqual(AuditEvent.objects.filter(user=recent).count(), 0)

    def test_backup_keeps_seven(self):
        from django.conf import settings
        from members import identity
        identity.pepper()  # made at the first email sign-in
        source = sqlite3.connect(settings.DATA_DIR / "source.sqlite3")
        source.execute("CREATE TABLE t(x)")
        source.execute("INSERT INTO t VALUES (42)")
        source.commit()
        with override_settings(BACKUP_DIR=settings.DATA_DIR / "backups-test"):
            start = timezone.now()
            for day in range(9):
                nightly.backup(start + datetime.timedelta(days=day), source=source)
            names = sorted(p.name for p in settings.BACKUP_DIR.glob("accounts-*.sqlite3"))
            self.assertEqual(len(names), 7)
            self.assertEqual(names[-1], "accounts-%s.sqlite3" % (start + datetime.timedelta(days=8)).strftime("%Y-%m-%d"))
            copy = sqlite3.connect(settings.BACKUP_DIR / names[-1])
            self.assertEqual(copy.execute("SELECT x FROM t").fetchall(), [(42,)])
            copy.close()
            self.assertTrue((settings.BACKUP_DIR / settings.EMAIL_PEPPER_FILE.name).exists())
        source.close()

    def test_checkdb(self):
        out = StringIO()
        call_command("checkdb", stdout=out)
        self.assertEqual(out.getvalue().strip(), "ok")
