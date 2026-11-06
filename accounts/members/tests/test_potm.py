"""Photo of the Month (members.potm), against the game server's data folder that test_likes makes.

Eligibility: a like is a vote only when it was given during the month, by a key whose likes count
in public (linked, active, pinned, not suspended, not banned, signed sign-ins old enough), whose
linked account was POTM_ACCOUNT_DAYS old when it liked, and not the owner's; the other likes stay
in the public totals. Entries: public photos uploaded during the month (UTC), not disqualified.
Month boundaries in UTC (the first second is in, the end is out, December to January, February
in a leap year). The tie-break: the earliest to reach the final count, then the earlier upload,
then the id. The operator's steps (confirm, withdraw, disqualify) with their checks, the admin's
review, the command, and the public answers (no XUID, no account, a public cache)."""

import json
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from forza_live.core import ed25519
from members import items, likes, potm
from members.models import Disqualified, GameLink, Like, MonthWinner, User
from members.tests.support import HOST
from members.tests.test_likes import LikesTest, item_id, photo
from members.tests.test_links import SEED_B, XUID, XUID_B, body

UTC = dt_timezone.utc
NOV = "2026-11"
NOV_START = datetime(2026, 11, 1, tzinfo=UTC)
DEC_START = datetime(2026, 12, 1, tzinfo=UTC)
AFTER = DEC_START + timedelta(days=2)       # "now" for a review of November
OLD = NOV_START - timedelta(days=60)        # an account that is old enough all month
P1, P2, P3, P4 = "POTMAAAAAAAA", "POTMBBBBBBBB", "POTMCCCCCCCC", "POTMDDDDDDDD"


def ts(dt):
    return dt.timestamp()


@override_settings(POTM_FROM="2026-01", POTM_ACCOUNT_DAYS=14)
class PotmTest(LikesTest):
    """Driver A (XUID) owns the entries; voters are made directly (an account, its link and the
    game server's record of its key) so their ages can be set."""

    def setUp(self):
        super().setUp()
        self.photos = {
            P1: photo(XUID, at=ts(NOV_START + timedelta(days=1))),
            P2: photo(XUID, at=ts(NOV_START + timedelta(days=2))),
            P3: photo(XUID_B, at=ts(NOV_START + timedelta(days=3))),
        }
        self.write_items()
        self.age(XUID, days=3000)
        self.age(XUID_B, days=3000)
        self.n = 0
        potm.forget()

    def voter(self, joined=OLD, signed_days=2, first_days=3000, link=True, active=True, xuid=None, seed=None):
        """A key bound on the game server, with a linked account that joined at `joined`."""
        self.n += 1
        xuid = xuid or 0x0009000000001000 + self.n
        seed = seed or bytes([self.n % 256, self.n // 256] + [7] * 30)
        if "%016X" % xuid not in self.friends:
            self.player(xuid, seed, "Voter%d" % self.n)
        self.age(xuid, days=first_days, signed_days=signed_days)
        user = User.objects.create(is_active=active)
        User.objects.filter(pk=user.pk).update(date_joined=joined)
        if link:
            GameLink.objects.create(xuid="%016X" % xuid, pub=ed25519.public_key(seed).hex(), user=user,
                                    gamertag="Voter%d" % self.n, linked_at=joined)
        return "%016X" % xuid

    def vote(self, xuid, item, at):
        Like.objects.create(xuid=xuid, kind="photo", item=item, at=at)

    def count(self, month=NOV, now=AFTER):
        return potm.count(month, now)

    def table(self, month=NOV, now=AFTER):
        return [(e.id, e.votes) for e in self.count(month, now).entries]


class Eligibility(PotmTest):
    def test_only_old_enough_linked_accounts_vote(self):
        at = NOV_START + timedelta(days=10)
        old = self.voter()
        self.vote(old, P1, at)
        young = self.voter(joined=at - timedelta(days=13, hours=23))  # 13 days 23 hours old then
        self.vote(young, P1, at)
        just = self.voter(joined=at - timedelta(days=14))                # exactly 14 days: counts
        self.vote(just, P1, at)
        s = self.count()
        e = {x.id: x for x in s.entries}[P1]
        self.assertEqual((e.votes, e.likes), (2, 3))
        self.assertEqual(e.not_counted, {potm.TOO_NEW: 1})

    def test_the_account_s_age_is_taken_when_it_liked_not_now(self):
        at = NOV_START + timedelta(days=5)
        xuid = self.voter(joined=at - timedelta(days=3))
        self.vote(xuid, P1, at)
        # Weeks later the account is old, but it was 3 days old when it liked.
        self.assertEqual(self.table(), [(P1, 0), (P2, 0), (P3, 0)])

    def test_keys_that_do_not_count_in_public_do_not_vote(self):
        at = NOV_START + timedelta(days=10)
        cases = {
            "unlinked": self.voter(link=False),
            "suspended": self.voter(active=False),
            "one signed day": self.voter(signed_days=1),
            # its first signed sign-in 12 hours before the review
            "new key": self.voter(first_days=(time.time() - ts(AFTER) + 43200) / 86400),
        }
        frozen = self.voter()
        GameLink.objects.filter(xuid=frozen).update(state=GameLink.FROZEN)
        cases["frozen"] = frozen
        repinned = self.voter()
        GameLink.objects.filter(xuid=repinned).update(pub="00" * 32)  # the XUID is bound to another key now
        cases["another key"] = repinned
        banned = self.voter()
        self.ban(int(banned, 16))
        cases["banned"] = banned
        for xuid in cases.values():
            self.vote(xuid, P1, at)
        e = {x.id: x for x in self.count().entries}[P1]
        self.assertEqual((e.votes, e.likes), (0, len(cases)))
        self.assertEqual(e.not_counted, {potm.NOT_LINKED: len(cases)})

    def test_the_owner_s_like_is_not_a_vote(self):
        owner = self.voter(xuid=XUID_B, seed=SEED_B)  # Driver B owns P3
        self.vote(owner, P3, NOV_START + timedelta(days=4))
        e = {x.id: x for x in self.count().entries}[P3]
        self.assertEqual((e.votes, e.not_counted), (0, {potm.OWN: 1}))

    def test_ineligible_likes_still_show_in_the_public_totals(self):
        at = timezone.now()
        month = potm.month_of(at)
        self.photos[P4] = photo(XUID, at=ts(at - timedelta(seconds=5)))
        self.write_items()
        young = self.voter(joined=at - timedelta(days=1))
        self.vote(young, P4, at)
        self.assertEqual(self.totals()[P4], 1)  # the gallery shows it
        with override_settings(POTM_FROM=month):
            self.assertEqual({e.id: e.votes for e in potm.count(month).entries}[P4], 0)

    def test_a_like_taken_back_is_no_vote(self):
        xuid = self.voter()
        self.vote(xuid, P1, NOV_START + timedelta(days=3))
        self.assertEqual(self.table()[0], (P1, 1))
        Like.objects.filter(xuid=xuid, item=P1).delete()
        self.assertEqual(self.table()[0][1], 0)

    def test_only_public_photos_uploaded_during_the_month_enter(self):
        self.photos[P4] = photo(XUID, at=ts(NOV_START - timedelta(seconds=1)))  # October
        self.photos["POTMHELDAAAA"] = photo(XUID, status="held", at=ts(NOV_START + timedelta(days=1)))
        self.photos["POTMHIDEAAAA"] = photo(XUID, status="hidden", at=ts(NOV_START + timedelta(days=1)))
        self.write_items()
        self.assertEqual(sorted(e.id for e in self.count().entries), [P1, P2, P3])
        self.ban(XUID_B)  # Driver B's photo leaves the gallery, and the contest
        items.forget()
        self.assertEqual(sorted(e.id for e in self.count().entries), [P1, P2])


class MonthBoundaries(PotmTest):
    def test_windows_are_utc_calendar_months(self):
        self.assertEqual(potm.window("2026-11"), (NOV_START, DEC_START))
        self.assertEqual(potm.window("2026-12"), (DEC_START, datetime(2027, 1, 1, tzinfo=UTC)))
        self.assertEqual(potm.window("2028-02")[1] - potm.window("2028-02")[0], timedelta(days=29))
        self.assertEqual(potm.window("2027-02")[1] - potm.window("2027-02")[0], timedelta(days=28))
        self.assertEqual(potm.previous("2027-01"), "2026-12")
        self.assertEqual(potm.previous("2026-11"), "2026-10")
        # 23:30 on 30 November in New York is already December in UTC.
        ny = dt_timezone(timedelta(hours=-5))
        self.assertEqual(potm.month_of(datetime(2026, 11, 30, 23, 30, tzinfo=ny)), "2026-12")
        self.assertEqual(potm.month_of(datetime(2026, 12, 1, 0, 30, tzinfo=dt_timezone(timedelta(hours=2)))), "2026-11")
        for bad in ("2026-13", "2026-00", "26-11", "2026-1", "2026-11-01", "", None, "2026-11\n", "\u0662\u0660\u0662\u0666-11"):
            with self.assertRaises(ValueError):
                potm.window(bad)

    def test_a_photo_uploaded_at_the_first_second_enters_and_at_the_end_does_not(self):
        self.photos = {P1: photo(XUID, at=ts(NOV_START)), P2: photo(XUID, at=ts(DEC_START) - 0.001),
                       P3: photo(XUID, at=ts(DEC_START))}
        self.write_items()
        self.assertEqual(sorted(e.id for e in self.count().entries), [P1, P2])
        self.assertEqual([e.id for e in self.count("2026-12", DEC_START + timedelta(days=40)).entries], [P3])

    def test_likes_count_only_during_the_month(self):
        a, b, c, d = self.voter(), self.voter(), self.voter(), self.voter()
        self.vote(a, P1, NOV_START)                                  # the first moment: in
        self.vote(b, P1, DEC_START - timedelta(microseconds=1))      # the last: in
        self.vote(c, P1, DEC_START)                                  # the month is over: out
        self.vote(d, P1, NOV_START - timedelta(microseconds=1))      # before the month: out
        e = {x.id: x for x in self.count().entries}[P1]
        self.assertEqual((e.votes, e.likes), (2, 2))
        self.assertEqual(e.reached, DEC_START - timedelta(microseconds=1))

    def test_december_into_january(self):
        self.photos = {P1: photo(XUID, at=ts(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC))),
                       P2: photo(XUID, at=ts(datetime(2027, 1, 1, tzinfo=UTC)))}
        self.write_items()
        a = self.voter()
        self.vote(a, P1, datetime(2026, 12, 31, 23, 59, 59, 500000, tzinfo=UTC))
        self.vote(a, P2, datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC))
        later = datetime(2027, 2, 2, tzinfo=UTC)
        self.assertEqual(self.table("2026-12", later), [(P1, 1)])
        self.assertEqual(self.table("2027-01", later), [(P2, 1)])

    def test_the_contest_months(self):
        now = datetime(2026, 11, 15, tzinfo=UTC)
        with override_settings(POTM_FROM="2026-10"):
            self.assertTrue(potm.contest_month("2026-10", now))
            self.assertTrue(potm.contest_month("2026-11", now))
            self.assertFalse(potm.contest_month("2026-09", now))
            self.assertFalse(potm.contest_month("2026-12", now))  # the future
            self.assertEqual(potm.current(now), "2026-11")
        with override_settings(POTM_FROM="2027-01"):
            self.assertEqual(potm.current(now), "2027-01")  # before the first: the first
            self.assertEqual(potm.state_of(potm.count("2027-01", now), None, now), "upcoming")
        with override_settings(POTM_FROM=""):
            self.assertFalse(potm.contest_month("2026-11", now))


class TieBreak(PotmTest):
    def test_the_most_votes_win(self):
        a, b, c = self.voter(), self.voter(), self.voter()
        for x in (a, b):
            self.vote(x, P2, NOV_START + timedelta(days=20))
        self.vote(c, P1, NOV_START + timedelta(days=5))
        self.assertEqual(self.table(), [(P2, 2), (P1, 1), (P3, 0)])

    def test_a_tie_goes_to_the_first_to_reach_the_final_count(self):
        a, b, c, d = self.voter(), self.voter(), self.voter(), self.voter()
        # P1 got its first vote early but its second late; P2 reached 2 votes before P1 did.
        self.vote(a, P1, NOV_START + timedelta(days=2))
        self.vote(b, P1, NOV_START + timedelta(days=20))
        self.vote(c, P2, NOV_START + timedelta(days=10))
        self.vote(d, P2, NOV_START + timedelta(days=11))
        s = self.count()
        self.assertEqual([(e.id, e.votes) for e in s.entries[:2]], [(P2, 2), (P1, 2)])
        self.assertEqual(s.entries[0].reached, NOV_START + timedelta(days=11))

    def test_a_like_taken_back_moves_when_the_count_was_reached(self):
        a, b, c = self.voter(), self.voter(), self.voter()
        self.vote(a, P1, NOV_START + timedelta(days=3))
        self.vote(b, P2, NOV_START + timedelta(days=4))
        self.vote(c, P1, NOV_START + timedelta(days=9))   # P1 leads 2 to 1
        self.vote(c, P2, NOV_START + timedelta(days=8))   # P2 reached 2 first
        self.assertEqual(self.table()[:2], [(P2, 2), (P1, 2)])
        Like.objects.filter(xuid=b, item=P2).delete()      # then P2 has 1
        self.assertEqual(self.table()[:2], [(P1, 2), (P2, 1)])

    def test_the_same_moment_goes_to_the_earlier_upload_then_the_id(self):
        a, b = self.voter(), self.voter()
        at = NOV_START + timedelta(days=12)
        self.vote(a, P2, at)
        self.vote(b, P1, at)
        self.assertEqual(self.table()[:2], [(P1, 1), (P2, 1)])  # P1 was uploaded a day earlier
        self.photos[P2]["created"] = self.photos[P1]["created"]
        self.write_items()
        self.assertEqual(self.table()[:2], [(P1, 1), (P2, 1)])  # the same upload time: the id
        self.assertEqual([e.id for e in sorted(reversed(self.count().entries), key=potm.order)][:2], [P1, P2])

    def test_disqualified_entries_drop_out(self):
        a, b = self.voter(), self.voter()
        self.vote(a, P2, NOV_START + timedelta(days=5))
        self.vote(b, P2, NOV_START + timedelta(days=6))
        self.vote(a, P1, NOV_START + timedelta(days=7))
        potm.disqualify(NOV, P2, "vote farm", "op", now=AFTER)
        self.assertEqual(self.table(), [(P1, 1), (P3, 0)])
        potm.requalify(NOV, P2, "op")
        self.assertEqual(self.table()[0], (P2, 2))


class Voted(PotmTest):
    """A and B like P1, A likes P2 too."""

    def setUp(self):
        super().setUp()
        self.a, self.b = self.voter(), self.voter()
        self.vote(self.a, P1, NOV_START + timedelta(days=5))
        self.vote(self.b, P1, NOV_START + timedelta(days=6))
        self.vote(self.a, P2, NOV_START + timedelta(days=7))


class OperatorSteps(Voted):
    def test_confirm_the_leader_once_the_month_is_over(self):
        with self.assertRaisesRegex(potm.PotmError, "not over"):
            potm.confirm(NOV, P1, "op", now=DEC_START - timedelta(seconds=1))
        with self.assertRaisesRegex(potm.PotmError, "does not lead"):
            potm.confirm(NOV, P2, "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "standings changed"):
            potm.confirm(NOV, P1, "op", expect_votes=3, now=AFTER)
        w = potm.confirm(NOV, P1, "op", expect_votes=2, note="checked\nby hand", now=AFTER)
        self.assertEqual((w.photo, w.votes, w.reached, w.gamertag, w.owner, w.note),
                         (P1, 2, NOV_START + timedelta(days=6), "DriverA", "%016X" % XUID, "checked by hand"))
        with self.assertRaisesRegex(potm.PotmError, "already has a winner"):
            potm.confirm(NOV, P1, "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "withdraw it first"):
            potm.disqualify(NOV, P1, "late find", "op", now=AFTER)
        # The winner stays, whatever happens to the photo and its likes.
        Like.objects.all().delete()
        del self.photos[P1]
        self.write_items()
        self.assertEqual(MonthWinner.objects.get(month=NOV).votes, 2)
        potm.withdraw(NOV, "op")
        self.assertFalse(MonthWinner.objects.exists())
        with self.assertRaisesRegex(potm.PotmError, "no confirmed winner"):
            potm.withdraw(NOV, "op")

    def test_no_winner_without_votes_and_only_contest_months(self):
        Like.objects.all().delete()
        with self.assertRaisesRegex(potm.PotmError, "no entry"):
            potm.confirm(NOV, P1, "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "not a contest month"):
            potm.confirm("2025-12", P1, "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "not a contest month"):
            potm.disqualify("2027-05", P1, "x", "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "say why"):
            potm.disqualify(NOV, P1, " \n", "op", now=AFTER)
        with self.assertRaisesRegex(potm.PotmError, "photo id"):
            potm.disqualify(NOV, "../../x", "why", "op", now=AFTER)

    def test_the_states(self):
        s = self.count(now=NOV_START + timedelta(days=9))
        self.assertEqual(potm.state_of(s, None, NOV_START + timedelta(days=9)), "open")
        s = self.count()
        self.assertEqual(potm.state_of(s, None, AFTER), "verifying")
        w = potm.confirm(NOV, P1, "op", now=AFTER)
        self.assertEqual(potm.state_of(s, w, AFTER), "confirmed")
        Like.objects.all().delete()
        self.assertEqual(potm.state_of(self.count(), None, AFTER), "no winner")

    def test_voters_for_the_review(self):
        rows = potm.voters(NOV, P1, AFTER)
        self.assertEqual([(r["key"], r["vote"]) for r in rows], [(self.a, True), (self.b, True)])
        self.assertEqual(rows[0]["likes_this_month"], 2)
        self.assertEqual(rows[0]["account_days"], 65)
        self.assertIsNotNone(rows[0]["first_signed"])


class TheCommand(Voted):
    def run_potm(self, *args):
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=AFTER):
            call_command("potm", *args, stdout=out)
        return out.getvalue()

    def test_standings_voters_and_confirm(self):
        out = self.run_potm("standings", NOV)
        self.assertIn("verifying", out)
        self.assertIn(" 1. %s    2 votes" % P1, out)
        doc = json.loads(self.run_potm("standings", NOV, "--json"))
        self.assertEqual(doc["top"][0]["id"], P1)
        self.assertIn("vote", self.run_potm("voters", NOV, P1))
        with self.assertRaisesRegex(CommandError, "standings changed"):
            self.run_potm("confirm", NOV, P1, "--votes", "5")
        self.assertIn("is the Photo of the Month", self.run_potm("confirm", NOV, P1, "--votes", "2"))
        self.assertIn("withdrawn", self.run_potm("withdraw", NOV))
        self.assertIn("out of", self.run_potm("disqualify", NOV, P1, "--note", "farm"))
        self.assertIn("back in", self.run_potm("requalify", NOV, P1))
        with self.assertRaisesRegex(CommandError, "not a contest month"):
            self.run_potm("standings", "2030-01")


class PublicAnswers(PotmTest):
    """The routes, for the month that is running now (the clock is real here)."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.month = potm.month_of(self.now)
        start = potm.window(self.month)[0]
        self.photos = {P1: photo(XUID, title="Gold <b>hour</b>", at=ts(start) + 1),
                       P2: photo(XUID, at=ts(start) + 2), P3: photo(XUID_B, at=ts(start) + 3)}
        self.write_items()
        self.a, self.b = self.voter(), self.voter(joined=self.now - timedelta(days=1))
        self.vote(self.a, P1, self.now - timedelta(seconds=30))
        self.vote(self.b, P1, self.now - timedelta(seconds=20))  # too new: a like, not a vote
        likes.forget_counts()
        self.override = override_settings(POTM_FROM=self.month)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def get(self, path, **query):
        return self.client.get(path, query, secure=True, HTTP_HOST=HOST)

    def test_the_standings(self):
        r = self.get("/api/potm/standings")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r["Cache-Control"], "public, max-age=60")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        doc = body(r)
        start, end = potm.window(self.month)
        self.assertEqual({k: doc[k] for k in ("version", "month", "start", "end", "state", "accountDays", "entries")},
                         {"version": 1, "month": self.month, "start": int(ts(start)), "end": int(ts(end)),
                          "state": "open", "accountDays": 14, "entries": 3})
        self.assertEqual(doc["top"], [{"rank": 1, "id": P1, "title": "Gold <b>hour</b>", "gamertag": "DriverA",
                                       "votes": 1, "likes": 2, "reached": int(ts(self.now - timedelta(seconds=30)))}])
        self.assertIsNone(doc["winner"])
        text = r.content.decode()
        for secret in ("%016X" % XUID, self.a, self.b, "xuid", "owner"):
            self.assertNotIn(secret, text)
        for user in User.objects.all():
            self.assertNotIn(user.username, text)
        self.assertEqual(body(self.get("/api/potm/standings", month=self.month))["month"], self.month)

    def test_months_that_are_not_contest_months(self):
        for month in (potm.previous(self.month), "2999-01", "nonsense", "2026-13"):
            r = self.get("/api/potm/standings", month=month)
            self.assertEqual((r.status_code, body(r)), (404, {"error": "no such month"}), month)
        with override_settings(POTM_FROM=""):
            for path in ("/api/potm/standings", "/api/potm/winners"):
                r = self.get(path)
                self.assertEqual((r.status_code, body(r)), (404, {"error": "not running"}))
                self.assertEqual(r["Cache-Control"], "no-store")
        with override_settings(GAME_DATA=None):
            self.assertEqual(self.get("/api/potm/standings").status_code, 503)

    def test_winners(self):
        doc = body(self.get("/api/potm/winners"))
        self.assertEqual((doc["version"], doc["from"], doc["winners"]), (1, self.month, []))
        MonthWinner.objects.create(month="2026-10", photo=P2, votes=7, reached=NOV_START - timedelta(days=3),
                                   title="T", gamertag="DriverA", owner="%016X" % XUID, confirmed_at=NOV_START,
                                   confirmed_by="op")
        r = self.get("/api/potm/winners")
        self.assertEqual(r["Cache-Control"], "public, max-age=60")
        self.assertEqual(body(r)["winners"], [{"month": "2026-10", "id": P2, "title": "T", "gamertag": "DriverA",
                                               "votes": 7, "reached": int(ts(NOV_START - timedelta(days=3))),
                                               "confirmed": int(ts(NOV_START))}])
        self.assertNotIn("%016X" % XUID, r.content.decode())
        self.assertNotIn("op", body(r)["winners"][0].values())

    def test_a_like_through_the_api_is_a_vote(self):
        b, user = self.linked("222", SEED_B, XUID_B)
        User.objects.filter(pk=user.pk).update(date_joined=self.now - timedelta(days=30))
        self.age(XUID_B, days=3000)
        self.assertEqual(self.like(b, P1).status_code, 200)
        potm.forget()
        likes.forget_counts()
        top = body(self.get("/api/potm/standings"))["top"]
        self.assertEqual((top[0]["id"], top[0]["votes"], top[0]["likes"]), (P1, 2, 3))

    def test_no_change_methods(self):
        r = self.client.post("/api/potm/standings", "{}", content_type="application/json", secure=True,
                             HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 405)

    def test_the_count_is_kept_a_minute(self):
        self.get("/api/potm/standings")
        with mock.patch.object(potm, "count", wraps=potm.count) as counted:
            self.get("/api/potm/standings")
            self.assertEqual(counted.call_count, 0)
            with override_settings(POTM_SECONDS=0):
                self.get("/api/potm/standings")
            self.assertEqual(counted.call_count, 1)


class TheAdminReview(Voted):
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create(is_staff=True, is_superuser=True, username="OPERATOR")
        self.admin = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1")
        self.admin.force_login(self.operator)
        self.clock = mock.patch("django.utils.timezone.now", return_value=AFTER)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        url = "/admin/members/monthwinner/review/%s/" % NOV
        self.url = url

    def test_review_and_confirm(self):
        r = self.admin.get("/admin/members/monthwinner/review/", secure=True)
        self.assertRedirects(r, self.url, fetch_redirect_response=False)
        page = self.admin.get(self.url, secure=True)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "verifying")
        self.assertContains(page, "Confirm %s as the Photo of the Month %s (2 votes)" % (P1, NOV))
        self.assertContains(page, self.a)  # the leader's likes, one by one
        r = self.admin.post(self.url, {"step": "confirm", "photo": P1, "votes": "1"}, secure=True)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(MonthWinner.objects.exists())  # the standings it showed were not those
        self.admin.post(self.url, {"step": "confirm", "photo": P1, "votes": "2"}, secure=True)
        w = MonthWinner.objects.get()
        self.assertEqual((w.photo, w.confirmed_by), (P1, "OPERATOR"))
        self.assertContains(self.admin.get(self.url, secure=True), "Confirmed winner")
        r = self.admin.post("/admin/members/monthwinner/%d/delete/" % w.pk, {"post": "yes"}, secure=True)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(MonthWinner.objects.exists())

    def test_disqualify_from_the_review(self):
        self.admin.post(self.url, {"step": "disqualify", "photo": P1, "note": "not their own"}, secure=True)
        self.assertEqual(Disqualified.objects.get().note, "not their own")
        self.assertContains(self.admin.get(self.url, secure=True), "Confirm %s" % P2)
        self.admin.post(self.url, {"step": "requalify", "photo": P1}, secure=True)
        self.assertFalse(Disqualified.objects.exists())

    def test_player_text_is_escaped(self):
        self.photos[P1]["title"] = "<script>alert(1)</script>"
        self.write_items()
        page = self.admin.get(self.url, secure=True)
        self.assertNotContains(page, "<script>alert(1)</script>")
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_only_staff_with_permissions_and_only_from_the_allow_list(self):
        outside = Client(HTTP_HOST=HOST, REMOTE_ADDR="198.51.100.9")
        outside.force_login(self.operator)
        self.assertEqual(outside.get(self.url, secure=True).status_code, 404)
        staff = User.objects.create(is_staff=True, username="HELPER")
        helper = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1")
        helper.force_login(staff)
        self.assertEqual(helper.get(self.url, secure=True).status_code, 403)
        r = helper.post(self.url, {"step": "confirm", "photo": P1, "votes": "2"}, secure=True)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(MonthWinner.objects.exists())
        player = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1")
        player.force_login(User.objects.create(username="PLAYER01"))
        self.assertEqual(player.get(self.url, secure=True).status_code, 302)  # to the admin's sign-in
        self.assertEqual(self.admin.get("/admin/members/monthwinner/review/2030-01/", secure=True).status_code, 404)

    def test_the_admin_form_needs_its_csrf_token(self):
        strict = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1", enforce_csrf_checks=True)
        strict.force_login(self.operator)
        r = strict.post(self.url, {"step": "confirm", "photo": P1, "votes": "2"}, secure=True)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(MonthWinner.objects.exists())


class Scale(PotmTest):
    def test_a_month_of_50000_likes_is_counted_in_under_two_seconds(self):
        keys, entries = 2500, 500
        now = int(time.time())
        self.photos = {item_id(i): photo(XUID, at=ts(NOV_START) + 60 * i) for i in range(entries)}
        ids = sorted(self.photos)
        self.write_items()
        users = User.objects.bulk_create([User(username="T%07d" % i, password="!", date_joined=OLD) for i in range(keys)])
        links = []
        for i, user in enumerate(users):
            xuid = 0x000A000000000000 + i
            pub = "%064x" % (i + 1)
            self.friends["%016X" % xuid] = {"name": "K%d" % i, "seen": now, "key": "", "pub": pub, "friends": {},
                                            "in": {}, "out": {}, "blocked": {}}
            (self.data / "players" / ("%016X.json" % xuid)).write_text(json.dumps(
                {"gamertag": "K%d" % i, "firstSigned": now - 3000 * 86400, "signedDays": 3}))
            links.append(GameLink(xuid="%016X" % xuid, pub=pub, user=user, gamertag="K%d" % i, linked_at=OLD))
        self.write_friends()
        GameLink.objects.bulk_create(links)
        Like.objects.bulk_create([Like(xuid="%016X" % (0x000A000000000000 + k), kind="photo",
                                       item=ids[(k * 7 + n) % entries], at=NOV_START + timedelta(minutes=k + n))
                                  for k in range(keys) for n in range(20)], batch_size=5000)
        started = time.perf_counter()
        s = potm.count(NOV, AFTER)
        took = time.perf_counter() - started
        self.assertLess(took, 2.0, "%.3f s" % took)
        self.assertEqual(sum(e.votes for e in s.entries), 50000)
        print("\na month of 50,000 likes counted in %.0f ms" % (took * 1000))
