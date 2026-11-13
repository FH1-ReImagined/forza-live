"""Email sign-in by code: identical answers, the mail, no stored address, tries, expiry, the
limits (per visitor, per mailbox whatever its spelling, the owner's own budget, the strangers'
share of the day's mails, the counters' table), and the SMTP relay (a fake smtplib.SMTP)."""

import json
import time
from unittest import mock

from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import override_settings

from members import identity, views
from members.models import User
from members.tests.support import CODE_CONFIRM, CODE_REQUEST, AccountsTest, Browser


def code_of(message):
    return message.body.split("#code=")[1].strip()


def answer(response):
    return response.status_code, json.loads(response.content)


class Answers(AccountsTest):
    def test_known_and_unknown_addresses_get_the_same_answer(self):
        self.email_sign_in(Browser(), "known@example.com")
        mail.outbox.clear()
        known = Browser(ip="203.0.113.21").post_json(CODE_REQUEST, {"email": "known@example.com"})
        unknown = Browser(ip="203.0.113.22").post_json(CODE_REQUEST, {"email": "unknown@example.com"})
        self.assertEqual(answer(known), answer(unknown))
        self.assertEqual(known.status_code, 401)  # allauth: a login by code is now pending
        self.assertEqual([m.to for m in mail.outbox], [["known@example.com"], ["unknown@example.com"]])

    def test_an_address_over_the_new_account_limit_gets_the_same_answer_and_no_mail(self):
        with override_settings(ACCOUNT_RATE_LIMITS=dict(_rates(), new_account="0/d/ip")):
            r = Browser(ip="203.0.113.23").post_json(CODE_REQUEST, {"email": "nobody@example.com"})
        normal = Browser(ip="203.0.113.24").post_json(CODE_REQUEST, {"email": "somebody@example.com"})
        self.assertEqual(answer(r), answer(normal))
        self.assertEqual([m.to for m in mail.outbox], [["somebody@example.com"]])
        self.assertFalse(EmailAddress.objects.filter(email=identity.stand_in("nobody@example.com")).exists())

    def test_a_bad_address_is_refused(self):
        r = Browser().post_json(CODE_REQUEST, {"email": "not an address"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(mail.outbox, [])


def _rates():
    from django.conf import settings
    return dict(settings.ACCOUNT_RATE_LIMITS)


class TheMail(AccountsTest):
    def test_fixed_plain_text_to_the_typed_address(self):
        Browser().post_json(CODE_REQUEST, {"email": "  Player@Example.COM "})
        (m,) = mail.outbox
        self.assertEqual(m.to, ["player@example.com"])
        self.assertEqual(m.from_email, "signin@forza.dyotson.dev")
        self.assertEqual(m.subject, "Your sign-in code for forza.dyotson.dev")
        self.assertEqual(getattr(m, "alternatives", []), [])  # no HTML part
        code = code_of(m)
        self.assertRegex(code, r"^[0-9]{8}$")
        self.assertIn("Your sign-in code for forza.dyotson.dev is %s %s." % (code[:4], code[4:]), m.body)
        self.assertIn("https://forza.dyotson.dev/account.html#code=" + code, m.body)
        self.assertNotIn("Player", m.body)

    def test_the_address_is_never_stored(self):
        self.email_sign_in(Browser(), "Secret.Person@Example.com")
        self.assertTrue(identity.is_stand_in(EmailAddress.objects.get().email))
        with connection.cursor() as c:
            for table in connection.introspection.table_names():
                c.execute('SELECT * FROM "%s"' % table)
                text = repr(c.fetchall()).lower()
                self.assertNotIn("secret.person", text, table)
                self.assertNotIn("example.com", text, table)

    def test_the_same_address_signs_in_to_the_same_account(self):
        self.email_sign_in(Browser(), "same@example.com")
        self.email_sign_in(Browser(), " SAME@example.com")
        self.assertEqual(User.objects.count(), 1)


class Codes(AccountsTest):
    def request(self, browser, address="player@example.com"):
        browser.post_json(CODE_REQUEST, {"email": address})
        return code_of(mail.outbox[-1])

    def test_five_tries_then_the_code_is_gone(self):
        b = Browser()
        code = self.request(b)
        wrong = "%08d" % ((int(code) + 1) % 10 ** 8)
        for _ in range(4):
            self.assertEqual(b.post_json(CODE_CONFIRM, {"code": wrong}).status_code, 400)
        last = b.post_json(CODE_CONFIRM, {"code": wrong})
        self.assertIn(last.status_code, (400, 409))
        self.assertEqual(b.post_json(CODE_CONFIRM, {"code": code}).status_code, 409)
        self.assertEqual(b.get_json("/api/me").status_code, 401)

    def test_a_code_expires_after_ten_minutes(self):
        b = Browser()
        code = self.request(b)
        later = time.time() + 601
        with mock.patch("time.time", return_value=later):
            r = b.post_json(CODE_CONFIRM, {"code": code})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(b.get_json("/api/me").status_code, 401)

    def test_a_code_works_until_ten_minutes(self):
        b = Browser()
        code = self.request(b)
        with mock.patch("time.time", return_value=time.time() + 590):
            self.assertEqual(b.post_json(CODE_CONFIRM, {"code": code}).status_code, 200)

    def test_a_code_works_only_in_the_browser_that_asked(self):
        code = self.request(Browser())
        other = Browser()
        self.assertEqual(other.post_json(CODE_CONFIRM, {"code": code}).status_code, 409)
        self.assertEqual(other.get_json("/api/me").status_code, 401)

    def test_a_new_code_replaces_the_old_one(self):
        b = Browser()
        first = self.request(b)
        second = self.request(b)
        if first != second:
            self.assertEqual(b.post_json(CODE_CONFIRM, {"code": first}).status_code, 400)
        self.assertEqual(b.post_json(CODE_CONFIRM, {"code": second}).status_code, 200)


class Limits(AccountsTest):
    def test_five_code_mails_an_hour_per_ip(self):
        b = Browser(ip="198.51.100.30")
        for i in range(5):
            self.assertEqual(b.post_json(CODE_REQUEST, {"email": "p%d@example.com" % i}).status_code, 401)
        r = b.post_json(CODE_REQUEST, {"email": "p5@example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.content)["errors"][0]["code"], "too_many_login_attempts")
        self.assertEqual(len(mail.outbox), 5)

    def test_three_codes_an_hour_and_five_a_day_per_address(self):
        now = time.time()
        sent = 0
        for hour, expect in ((0, 3), (1.1, 2), (2.2, 0)):
            with mock.patch("time.time", return_value=now + hour * 3600):
                for i in range(4):
                    Browser(ip="198.51.100.%d" % (40 + 10 * int(hour) + i)).post_json(CODE_REQUEST, {"email": "target@example.com"})
            self.assertEqual(len(mail.outbox) - sent, expect, hour)
            sent = len(mail.outbox)
        self.assertEqual(sent, 5)

    def test_email_sign_in_pauses_at_the_daily_cap(self):
        with override_settings(ACCOUNT_RATE_LIMITS=dict(_rates(), login_code_mail_all="2/d/key")):
            for i in range(2):
                self.assertEqual(Browser(ip="198.51.100.%d" % (60 + i)).post_json(
                    CODE_REQUEST, {"email": "q%d@example.com" % i}).status_code, 401)
            r = Browser(ip="198.51.100.70").post_json(CODE_REQUEST, {"email": "q9@example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.content)["errors"][0]["code"], "paused")
        self.assertEqual(len(mail.outbox), 2)

    def test_five_new_accounts_a_day_per_ip(self):
        for i in range(5):
            self.email_sign_in(Browser(ip="198.51.100.80"), "n%d@example.com" % i)
        mail.outbox.clear()
        with mock.patch("time.time", return_value=time.time() + 3700):  # past the 5 mails an hour
            r = Browser(ip="198.51.100.80").post_json(CODE_REQUEST, {"email": "n5@example.com"})
        self.assertEqual(mail.outbox, [])
        self.assertEqual(User.objects.count(), 5)
        self.assertEqual(r.status_code, 401)  # the same answer as for any address

    def test_twenty_wrong_codes_an_hour_per_ip(self):
        ip = "198.51.100.90"
        for n in range(4):
            b = Browser(ip=ip)
            b.post_json(CODE_REQUEST, {"email": "w%d@example.com" % n})
            code = code_of(mail.outbox[-1])
            for _ in range(5):
                b.post_json(CODE_CONFIRM, {"code": "%08d" % ((int(code) + 1) % 10 ** 8)})
        with mock.patch("time.time", return_value=time.time() + 3700):  # a new hour for the mails
            b = Browser(ip=ip)
            b.post_json(CODE_REQUEST, {"email": "w9@example.com"})
        code = code_of(mail.outbox[-1])
        r = b.post_json(CODE_CONFIRM, {"code": code})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(b.get_json("/api/me").status_code, 401)


    def test_twenty_code_requests_a_day_per_ip(self):
        now = time.time()
        b = Browser(ip="198.51.100.95")
        for hour in range(4):
            with mock.patch("time.time", return_value=now + hour * 3700):
                for i in range(5):
                    r = b.post_json(CODE_REQUEST, {"email": "d%d-%d@example.com" % (hour, i)})
                    self.assertEqual(r.status_code, 401, (hour, i))
        with mock.patch("time.time", return_value=now + 4 * 3700):
            r = b.post_json(CODE_REQUEST, {"email": "d4-0@example.com"})
            self.assertEqual(json.loads(r.content)["errors"][0]["code"], "too_many_login_attempts")
            self.assertEqual(Browser(ip="198.51.100.96").post_json(
                CODE_REQUEST, {"email": "d4-0@example.com"}).status_code, 401)  # another visitor


class Mailboxes(AccountsTest):
    """The per-address limit counts the mailbox, whatever the spelling, and the address's owner
    keeps a budget nobody else can use up."""

    SPELLINGS = ("victim@gmail.com", "victim+1@gmail.com", "v.ictim@gmail.com", "Vic.Tim+x@GoogleMail.com",
                 '"victim"@gmail.com')

    def test_what_a_mailbox_is(self):
        for address in self.SPELLINGS:
            self.assertEqual(identity.mailbox(address), "victim@gmail.com", address)
        self.assertEqual(identity.mailbox("First.Last+tag@Example.com"), "first.last@example.com")
        self.assertEqual(identity.mailbox("p@b\u00fccher.example"), "p@xn--bcher-kva.example")
        self.assertEqual(identity.mailbox("p@xn--bcher-kva.example"), "p@xn--bcher-kva.example")
        # another spelling of a domain: mail servers read it as the plain one
        for address in ("victim@\uff47mail.com", "victim@gm\u00adail.com", "victim@gmail\u3002com",
                        "victim@exa\u200dmple.com", "no-at-sign", "@example.com", "a@"):
            self.assertIsNone(identity.mailbox(address), ascii(address))

    def test_every_spelling_shares_the_mailbox_s_three_an_hour(self):
        for i, address in enumerate(self.SPELLINGS):
            r = Browser(ip="198.51.100.%d" % (100 + i)).post_json(CODE_REQUEST, {"email": address})
            self.assertEqual(r.status_code, 401 if i < 3 else 400, address)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(json.loads(r.content)["errors"][0]["code"], "too_many_login_attempts")

    def test_another_spelling_of_a_domain_is_refused(self):
        for address in ("victim@\uff47mail.com", "victim@gm\u00adail.com"):
            r = Browser(ip="198.51.100.110").post_json(CODE_REQUEST, {"email": address})
            self.assertEqual(r.status_code, 400, ascii(address))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(User.objects.count(), 0)

    def spend(self, address, start, ip=120):
        """Other visitors ask for the address's codes until its five a day are gone."""
        for n, hour in enumerate((0, 0, 0, 1.1, 1.1)):
            with mock.patch("time.time", return_value=start + hour * 3600):
                r = Browser(ip="198.51.100.%d" % (ip + n)).post_json(CODE_REQUEST, {"email": address})
                self.assertEqual(r.status_code, 401, n)
        return start + 1.2 * 3600

    def test_others_cannot_use_up_the_owner_s_codes(self):
        owner = Browser(ip="198.51.100.111")
        done = self.email_sign_in(owner, "member@example.com")
        self.assertIn(views.KNOWN_COOKIE, done.cookies)
        owner.post_json("/api/logout-all", {})
        later = self.spend("member@example.com", time.time() + 86400 * 2)
        mail.outbox.clear()
        with mock.patch("time.time", return_value=later):
            stranger = Browser(ip="198.51.100.130").post_json(CODE_REQUEST, {"email": "member@example.com"})
            self.assertEqual(stranger.status_code, 400)  # the mailbox's public budget is gone
            self.assertEqual(mail.outbox, [])
            again = owner.post_json(CODE_REQUEST, {"email": "Member@Example.com"})  # the owner's browser
            self.assertEqual(again.status_code, 401)
            self.assertEqual([m.to for m in mail.outbox], [["member@example.com"]])
            self.assertEqual(owner.post_json(CODE_CONFIRM, {"code": code_of(mail.outbox[-1])}).status_code, 200)

    def test_the_owner_s_budget_is_for_that_address_only(self):
        owner = Browser(ip="198.51.100.112")
        self.email_sign_in(owner, "member@example.com")
        later = self.spend("other@example.com", time.time() + 86400 * 2, ip=140)
        with mock.patch("time.time", return_value=later):
            self.assertEqual(owner.post_json(CODE_REQUEST, {"email": "other@example.com"}).status_code, 400)

    def test_a_forged_cookie_gets_no_budget(self):
        later = self.spend("member@example.com", time.time())
        forged = Browser(ip="198.51.100.113")
        forged.cookies[views.KNOWN_COOKIE] = identity.stand_in("member@example.com")
        with mock.patch("time.time", return_value=later):
            self.assertEqual(forged.post_json(CODE_REQUEST, {"email": "member@example.com"}).status_code, 400)

    def test_the_cookie(self):
        done = self.email_sign_in(Browser(), "member@example.com")
        morsel = done.cookies[views.KNOWN_COOKIE]
        self.assertEqual((bool(morsel["secure"]), bool(morsel["httponly"]), morsel["samesite"], morsel["path"],
                          morsel["domain"]), (True, True, "Strict", "/", ""))
        self.assertEqual(int(morsel["max-age"]), 400 * 86400)
        self.assertNotIn("member", morsel.value)
        self.assertNotIn("example", morsel.value)


class Shares(AccountsTest):
    """Half of the day's code mails are for addresses that never finished a sign-in; the rest is
    left for accounts that have."""

    def rates(self, total=4, new=2):
        return override_settings(ACCOUNT_RATE_LIMITS=dict(
            _rates(), login_code_mail_all="%d/d/key" % total, login_code_mail_new="%d/d/key" % new))

    def test_half_is_the_default(self):
        from django.conf import settings
        self.assertEqual(settings.ACCOUNT_RATE_LIMITS["login_code_mail_new"], "%d/d/key" % (settings.EMAIL_DAILY // 2))

    def test_strangers_cannot_use_up_the_members_mails(self):
        with self.rates(), self.assertLogs("members", "WARNING") as logs:
            self.email_sign_in(Browser(ip="198.51.100.150"), "member@example.com")  # a new address: 1 of 2
            normal = Browser(ip="198.51.100.151").post_json(CODE_REQUEST, {"email": "s1@example.com"})
            self.assertEqual(len(mail.outbox), 2)
            for i in range(2, 5):  # the strangers' share is used up: the same answer, no mail, no account
                r = Browser(ip="198.51.100.%d" % (150 + i)).post_json(CODE_REQUEST, {"email": "s%d@example.com" % i})
                self.assertEqual(answer(r), answer(normal))
            self.assertEqual(len(mail.outbox), 2)
            self.assertEqual(User.objects.count(), 2)
            # nor a second mail for an address that asked before and never signed in
            Browser(ip="198.51.100.156").post_json(CODE_REQUEST, {"email": "s1@example.com"})
            self.assertEqual(len(mail.outbox), 2)
            # the member still signs in
            self.email_sign_in(Browser(ip="198.51.100.157"), "member@example.com")
            self.email_sign_in(Browser(ip="198.51.100.158"), "member@example.com")
            # until the whole day's mails are gone
            r = Browser(ip="198.51.100.159").post_json(CODE_REQUEST, {"email": "member@example.com"})
            self.assertEqual(json.loads(r.content)["errors"][0]["code"], "paused")
        warned = [line for line in logs.output if "used up" in line]
        self.assertEqual(len(warned), 2, logs.output)  # once for the strangers' share, once for the day's
        self.assertNotIn("example.com", "\n".join(logs.output))


class Counters(AccountsTest):
    """The limits are rows of the cache table. Django's default cap of 300 rows dropped a third of
    them, lowest keys first, at every write above it: the counters started again."""

    def fill(self, rows=400):
        for i in range(rows):
            cache.set("zz-another-counter-%d" % i, [time.time()], 3600)

    def test_the_table_holds_them(self):
        from django.conf import settings
        self.assertGreaterEqual(settings.CACHES["default"]["OPTIONS"]["MAX_ENTRIES"], 100000)

    def test_a_limit_outlives_many_other_counters(self):
        b = Browser(ip="198.51.100.160")
        for i in range(5):
            self.assertEqual(b.post_json(CODE_REQUEST, {"email": "c%d@example.com" % i}).status_code, 401)
        self.assertEqual(b.post_json(CODE_REQUEST, {"email": "c5@example.com"}).status_code, 400)
        self.fill()
        r = Browser(ip="198.51.100.160").post_json(CODE_REQUEST, {"email": "c6@example.com"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.content)["errors"][0]["code"], "too_many_login_attempts")

    def test_the_daily_mail_cap_outlives_many_visitors(self):
        with override_settings(ACCOUNT_RATE_LIMITS=dict(_rates(), login_code_mail_all="3/d/key",
                                                        login_code_mail_new="3/d/key")):
            for i in range(3):
                Browser(ip="198.51.100.%d" % (170 + i)).post_json(CODE_REQUEST, {"email": "m%d@example.com" % i})
            self.fill()
            r = Browser(ip="198.51.100.180").post_json(CODE_REQUEST, {"email": "m9@example.com"})
        self.assertEqual(json.loads(r.content)["errors"][0]["code"], "paused")
        self.assertEqual(len(mail.outbox), 3)

    def test_expired_counters_go_nightly(self):
        from django.utils import timezone
        from members.management.commands import nightly
        cache.set("an-hour", 1, 3600)
        cache.set("a-day", 1, 86400)
        self.assertEqual(nightly.purge_cache(), 0)
        later = timezone.now() + timezone.timedelta(hours=2)
        self.assertEqual(nightly.purge_cache(later), 1)
        with connection.cursor() as c:
            c.execute("SELECT cache_key FROM cache")
            keys = [row[0] for row in c.fetchall()]
        self.assertEqual(len(keys), 1)
        self.assertIn("a-day", keys[0])
        self.assertEqual(cache.get("a-day"), 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="smtp.relay.example",
                   EMAIL_HOST_USER="relay-user", EMAIL_HOST_PASSWORD="relay-password")
class Relay(AccountsTest):
    def test_starttls_on_587(self):
        with override_settings(EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_USE_SSL=False), \
                mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            Browser().post_json(CODE_REQUEST, {"email": "relay@example.com"})
        smtp_ssl.assert_not_called()
        smtp.assert_called_once()
        self.assertEqual(smtp.call_args.args[:2], ("smtp.relay.example", 587))
        conn = smtp.return_value
        conn.starttls.assert_called_once()
        conn.login.assert_called_once_with("relay-user", "relay-password")
        (from_, to, body), _ = conn.sendmail.call_args
        self.assertEqual((from_, to), ("signin@forza.dyotson.dev", ["relay@example.com"]))
        self.assertIn(b"Content-Type: text/plain", body)
        self.assertIn(b"#code=", body)

    def test_tls_on_465(self):
        with override_settings(EMAIL_PORT=465, EMAIL_USE_TLS=False, EMAIL_USE_SSL=True), \
                mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            Browser().post_json(CODE_REQUEST, {"email": "relay@example.com"})
        smtp.assert_not_called()
        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.relay.example", 465))
        smtp_ssl.return_value.login.assert_called_once_with("relay-user", "relay-password")

    def test_a_relay_failure_changes_nothing_in_the_answer_and_logs_no_address(self):
        import smtplib
        with override_settings(EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_USE_SSL=False), \
                mock.patch("smtplib.SMTP") as smtp, self.assertLogs("members", "WARNING") as logs:
            smtp.return_value.sendmail.side_effect = smtplib.SMTPRecipientsRefused({"fail@example.com": (550, b"no")})
            failed = Browser(ip="203.0.113.32").post_json(CODE_REQUEST, {"email": "fail@example.com"})
            smtp.return_value.sendmail.side_effect = None
            fine = Browser(ip="203.0.113.33").post_json(CODE_REQUEST, {"email": "fine@example.com"})
        self.assertEqual(answer(failed), answer(fine))
        self.assertNotIn("example.com", "\n".join(logs.output))
