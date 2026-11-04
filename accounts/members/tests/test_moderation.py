"""The operator's tools and the purge of likes whose item is gone (members.moderation, the admin,
and the void_likes, purge_likes, stats, backup and restore commands), against the game server's
data folder that test_likes makes.

The purge: an item missing from community.json or designs.json keeps its likes for the grace
period, then they go; an item that comes back is forgotten; a missing, damaged or empty file is
not judged. Voiding: a key's or an account's likes, with an audit event. Suspending: signed out
everywhere, no sign-in, no new account in its place, its likes not counted. The admin: the like
list and its filters, the actions, an operator's unlink and delete."""

import json
import sqlite3
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.utils import timezone

from members import moderation
from members.management.commands import nightly, restore
from members.models import AuditEvent, GameLink, Like, MissingItem, User
from members.tests.support import HOST, AccountsTest, Browser, fake_provider
from members.tests.test_likes import DESIGN, PHOTOS, XUID_C, LikesTest
from members.tests.test_links import SEED_B, SEED_C, XUID_B, body

KEY_B, KEY_C = "%016X" % XUID_B, "%016X" % XUID_C
GRACE = timedelta(days=7)


def events(user):
    return list(AuditEvent.objects.filter(user=user).values_list("event", flat=True))


class ModerationTest(LikesTest):
    """Drivers B and C like the first two photos and Driver A's design."""

    def setUp(self):
        super().setUp()
        self.b, self.user_b = self.linked("222", SEED_B, XUID_B)
        self.c, self.user_c = self.linked("333", SEED_C, XUID_C)
        for browser in (self.b, self.c):
            self.assertEqual(self.like(browser, PHOTOS[0]).status_code, 200)
            self.assertEqual(self.like(browser, PHOTOS[1]).status_code, 200)
        self.assertEqual(self.like(self.c, DESIGN, kind="design").status_code, 200)

    def remove_photo(self, pid):
        del self.photos[pid]
        self.write_items()

    def purge(self, days=0, **kw):
        return moderation.purge_missing_likes(timezone.now() + timedelta(days=days), **kw)


class Purge(ModerationTest):
    def test_a_photo_s_likes_go_after_the_grace_period(self):
        self.remove_photo(PHOTOS[0])
        report = self.purge()
        self.assertEqual(report["photo"], {"missing": 1, "purged": 0, "likes": 0, "skipped": False})
        self.assertEqual(Like.objects.filter(item=PHOTOS[0]).count(), 2)  # kept for now
        self.assertEqual(list(MissingItem.objects.values_list("kind", "item")), [("photo", PHOTOS[0])])
        self.assertEqual(self.purge(days=6.9)["photo"]["likes"], 0)
        report = self.purge(days=7)
        self.assertEqual(report["photo"], {"missing": 1, "purged": 1, "likes": 2, "skipped": False})
        self.assertFalse(Like.objects.filter(item=PHOTOS[0]).exists())
        self.assertEqual(Like.objects.filter(item=PHOTOS[1]).count(), 2)  # the others stay
        self.assertEqual(Like.objects.filter(item=DESIGN).count(), 1)
        self.assertFalse(MissingItem.objects.exists())

    def test_an_item_that_comes_back_keeps_its_likes(self):
        saved = self.photos[PHOTOS[0]]
        self.remove_photo(PHOTOS[0])
        self.purge()
        self.photos[PHOTOS[0]] = saved
        self.write_items()
        self.assertEqual(self.purge(days=3)["photo"]["missing"], 0)
        self.assertFalse(MissingItem.objects.exists())
        self.remove_photo(PHOTOS[0])  # gone again: the grace period starts again
        self.purge(days=4)
        self.assertEqual(self.purge(days=10)["photo"]["likes"], 0)
        self.assertEqual(self.purge(days=11)["photo"]["likes"], 2)

    def test_a_hidden_or_held_item_is_not_missing(self):
        self.photos[PHOTOS[0]]["status"] = "hidden"
        self.photos[PHOTOS[1]]["status"] = "held"
        self.designs["100000000"]["web"] = "hidden"
        self.write_items()
        self.purge()
        report = self.purge(days=30)
        self.assertEqual((report["photo"]["missing"], report["design"]["missing"]), (0, 0))
        self.assertEqual(Like.objects.count(), 5)

    def test_a_removed_design(self):
        del self.designs["100000000"]
        self.write_items()
        self.purge()
        report = self.purge(days=7)
        self.assertEqual(report["design"], {"missing": 1, "purged": 1, "likes": 1, "skipped": False})
        self.assertEqual(report["photo"]["likes"], 0)
        self.assertEqual(Like.objects.filter(kind="photo").count(), 4)

    def test_a_missing_damaged_or_empty_file_is_not_judged(self):
        (self.data / "community.json").unlink()
        self.purge()
        report = self.purge(days=30)
        self.assertTrue(report["photo"]["skipped"])
        self.assertFalse(report["design"]["skipped"])
        (self.data / "community.json").write_text("{not json")  # and no .bak
        self.assertTrue(self.purge(days=60)["photo"]["skipped"])
        (self.data / "community.json").write_text(json.dumps({"photos": {}}))
        self.assertTrue(self.purge(days=90)["photo"]["skipped"])
        self.assertEqual(Like.objects.filter(kind="photo").count(), 4)
        self.assertFalse(MissingItem.objects.exists())

    def test_a_damaged_file_with_its_previous_copy_is_judged(self):
        good = (self.data / "community.json").read_text()
        (self.data / "community.json.bak").write_text(good)
        (self.data / "community.json").write_text("{not json")
        self.assertFalse(self.purge()["photo"]["skipped"])
        self.assertFalse(MissingItem.objects.exists())

    def test_a_dry_run_changes_nothing(self):
        self.remove_photo(PHOTOS[0])
        self.purge()
        report = self.purge(days=8, dry_run=True)
        self.assertEqual(report["photo"]["likes"], 2)
        self.assertEqual(Like.objects.filter(item=PHOTOS[0]).count(), 2)
        self.assertEqual(MissingItem.objects.count(), 1)

    def test_without_the_game_data(self):
        with override_settings(GAME_DATA=None):
            self.assertEqual(moderation.purge_missing_likes(), {})
            with self.assertRaises(CommandError):
                call_command("purge_likes", stdout=StringIO())

    def test_the_command_and_nightly(self):
        self.remove_photo(PHOTOS[0])
        out = StringIO()
        call_command("purge_likes", stdout=out)
        self.assertIn("photos: 1 missing, 0 like(s) of 0 item(s) deleted", out.getvalue())
        MissingItem.objects.update(since=timezone.now() - GRACE)
        out = StringIO()
        call_command("purge_likes", "--dry-run", stdout=out)
        self.assertIn("2 like(s) of 1 item(s) would go", out.getvalue())
        self.remove_photo(PHOTOS[1])
        with mock.patch("members.management.commands.nightly.backup", return_value="the backup"):
            out = StringIO()
            call_command("nightly", stdout=out)
        self.assertIn("photos: 2 missing, 2 like(s) of 1 item(s) deleted", out.getvalue())
        self.assertFalse(Like.objects.filter(item=PHOTOS[0]).exists())
        self.assertEqual(Like.objects.filter(item=PHOTOS[1]).count(), 2)  # its grace starts now
        out = StringIO()
        call_command("purge_likes", "--grace-days", "0", stdout=out)
        self.assertFalse(Like.objects.filter(kind="photo").exists())

    def test_the_totals_drop_at_once(self):
        self.assertEqual(self.totals(), {PHOTOS[0]: 2, PHOTOS[1]: 2})
        self.remove_photo(PHOTOS[0])
        self.assertEqual(self.totals(), {PHOTOS[1]: 2})  # never counted while it is gone


class Voiding(ModerationTest):
    def test_a_key_s_likes(self):
        self.assertEqual(moderation.void_key(KEY_C), 3)
        self.assertFalse(Like.objects.filter(xuid=KEY_C).exists())
        self.assertEqual(Like.objects.filter(xuid=KEY_B).count(), 2)
        self.assertIn("the operator removed 3 of this game's likes", events(self.user_c))
        self.assertEqual(body(self.c.get_json("/api/me"))["events"][0]["event"],
                         "the operator removed 3 of this game's likes")  # the player sees it
        self.assertEqual(self.totals(), {PHOTOS[0]: 1, PHOTOS[1]: 1})
        self.assertEqual(self.like(self.c).status_code, 200)  # it can like again

    def test_an_unlinked_key_s_likes(self):
        self.signed("POST", "/api/game/unlink", {}, seed=SEED_C, xuid=XUID_C)
        before = AuditEvent.objects.count()
        with self.assertLogs("members", "INFO") as logs:
            self.assertEqual(moderation.void_key(KEY_C), 3)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertIn("a key that is not linked", logs.output[0])
        self.assertNotIn(KEY_C, "".join(logs.output))  # never an XUID in the log

    def test_the_command(self):
        out = StringIO()
        call_command("void_likes", "--account", self.user_c.public_id.lower(), "--dry-run", stdout=out)
        self.assertEqual(out.getvalue().strip(), "3 like(s) would be voided")
        self.assertEqual(Like.objects.filter(xuid=KEY_C).count(), 3)
        call_command("void_likes", "--account", self.user_c.username, stdout=out)
        self.assertFalse(Like.objects.filter(xuid=KEY_C).exists())
        call_command("void_likes", "--key", KEY_B.lower(), stdout=out)
        self.assertFalse(Like.objects.exists())
        self.assertIn("the operator removed 2 of this game's likes", events(self.user_b))
        for args in (["--account", "ZZZZ-ZZZZ"], ["--key", "nothex"], []):
            with self.assertRaises(CommandError, msg=args):
                call_command("void_likes", *args, stdout=out)
        unlinked = User.objects.create()
        with self.assertRaisesRegex(CommandError, "not linked"):
            call_command("void_likes", "--account", unlinked.public_id, stdout=out)

    def test_deleting_an_account_any_way_deletes_its_key_s_likes(self):
        self.user_c.delete()  # as the admin's delete does
        self.assertFalse(Like.objects.filter(xuid=KEY_C).exists())
        self.assertEqual(Like.objects.filter(xuid=KEY_B).count(), 2)


class Suspending(ModerationTest):
    def test_suspend_and_lift(self):
        self.assertTrue(moderation.suspend(self.user_c))
        self.assertEqual(self.c.get_json("/api/me").status_code, 401)  # signed out everywhere
        self.assertEqual(self.totals(), {PHOTOS[0]: 1, PHOTOS[1]: 1})  # its likes are not counted
        self.assertEqual(Like.objects.filter(xuid=KEY_C).count(), 3)  # but kept
        self.assertIn("the operator suspended this account", events(self.user_c))
        # it cannot sign in again
        again = Browser(ip="203.0.113.77")
        with fake_provider() as fake:
            self.oauth_sign_in(again, fake, uid="333")
        self.assertEqual(again.get_json("/api/me").status_code, 401)
        self.assertEqual(User.objects.filter(socialaccount__uid="333").count(), 1)
        self.assertTrue(moderation.unsuspend(User.objects.get(pk=self.user_c.pk)))
        self.assertEqual(self.totals(), {PHOTOS[0]: 2, PHOTOS[1]: 2})
        self.assertIn("the operator lifted this account's suspension", events(self.user_c))
        with fake_provider() as fake:
            self.oauth_sign_in(again, fake, uid="333")
        self.assertEqual(again.get_json("/api/me").status_code, 200)

    def test_a_suspended_email_account_gets_no_code_and_no_new_account(self):
        from django.core import mail
        self.email_sign_in(Browser(ip="203.0.113.80"), "abuser@example.com")
        user = User.objects.get(emailaddress__isnull=False)
        moderation.suspend(user)
        before, accounts = len(mail.outbox), User.objects.count()
        r = Browser(ip="203.0.113.81").post_json("/api/auth/browser/v1/auth/code/request",
                                                 {"email": "abuser@example.com"})
        self.assertEqual(r.status_code, 401)  # the same answer as for any address
        self.assertEqual((len(mail.outbox), User.objects.count()), (before, accounts))

    def test_a_suspended_account_is_not_purged_as_unused(self):
        idle = User.objects.create()
        moderation.suspend(idle)
        User.objects.filter(pk=idle.pk).update(date_joined=timezone.now() - timedelta(days=400))
        nightly.purge(timezone.now() + timedelta(days=60))
        self.assertTrue(User.objects.filter(pk=idle.pk).exists())

    def test_staff_accounts_are_left_alone(self):
        staff = User.objects.create(is_staff=True)
        self.assertFalse(moderation.suspend(staff))
        self.assertIsNone(moderation.sign_out(staff))
        self.assertTrue(User.objects.get(pk=staff.pk).is_active)

    def test_sign_out_and_unlink(self):
        self.assertEqual(moderation.sign_out(self.user_b), 1)
        self.assertEqual(self.b.get_json("/api/me").status_code, 401)
        moderation.unlink(GameLink.objects.get(user=self.user_c))
        self.assertIn("the operator unlinked the game of DriverC", events(self.user_c))
        self.assertEqual(Like.objects.filter(xuid=KEY_C).count(), 3)  # the key keeps them
        self.assertEqual(self.totals(), {PHOTOS[0]: 1, PHOTOS[1]: 1})


class TheAdmin(ModerationTest):
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create(is_staff=True, is_superuser=True)
        self.admin = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1")
        self.admin.force_login(self.operator)

    def get(self, path, **query):
        r = self.admin.get(path, query, secure=True)
        self.assertEqual(r.status_code, 200, path)
        return r

    def act(self, path, action, pks):
        r = self.admin.post(path, {"action": action, "_selected_action": [str(p) for p in pks]}, secure=True)
        self.assertEqual(r.status_code, 302, r.content[:500])

    def test_the_like_list_and_its_filters(self):
        likes = "/admin/members/like/"
        page = self.get(likes)
        self.assertEqual(page.context["cl"].result_count, 5)
        self.assertContains(page, self.user_c.public_id)
        self.assertNotContains(page, "Add like")
        self.assertEqual(self.get(likes, item=PHOTOS[0]).context["cl"].result_count, 2)
        self.assertEqual(self.get(likes, key=KEY_C).context["cl"].result_count, 3)
        self.assertEqual(self.get(likes, account=self.user_b.public_id).context["cl"].result_count, 2)
        self.assertEqual(self.get(likes, account="ZZZZ-ZZZZ").context["cl"].result_count, 0)
        self.assertEqual(self.get(likes, kind="design").context["cl"].result_count, 1)
        self.assertEqual(self.get(likes, q=PHOTOS[1]).context["cl"].result_count, 2)
        self.remove_photo(PHOTOS[0])
        self.assertContains(self.get(likes, item=PHOTOS[0]), "gone")
        actions = [a for a, _ in page.context["action_form"].fields["action"].choices if a]
        self.assertEqual(sorted(actions), ["void_keys", "void_selected"])  # no plain delete
        self.assertEqual(self.admin.get(likes + "%d/delete/" % Like.objects.first().pk, secure=True).status_code, 403)

    def test_void_from_the_like_list(self):
        likes = "/admin/members/like/"
        one = Like.objects.get(xuid=KEY_B, item=PHOTOS[0])
        self.act(likes, "void_selected", [one.pk])
        self.assertEqual(Like.objects.filter(xuid=KEY_B).count(), 1)
        self.assertIn("the operator removed 1 of this game's likes", events(self.user_b))
        self.act(likes, "void_keys", [Like.objects.filter(xuid=KEY_C).first().pk])
        self.assertFalse(Like.objects.filter(xuid=KEY_C).exists())

    def test_the_accounts(self):
        users = "/admin/members/user/"
        for term in (self.user_c.public_id, self.user_c.username.lower(), "DriverC", KEY_C):
            found = self.get(users, q=term).context["cl"].result_list
            self.assertEqual([u.pk for u in found], [self.user_c.pk], term)
        self.assertContains(self.get(users + "%d/change/" % self.user_c.pk), "linked the game of DriverC")
        self.act(users, "suspend", [self.user_c.pk])
        self.assertFalse(User.objects.get(pk=self.user_c.pk).is_active)
        self.assertEqual(self.c.get_json("/api/me").status_code, 401)
        self.act(users, "unsuspend", [self.user_c.pk])
        self.assertTrue(User.objects.get(pk=self.user_c.pk).is_active)
        self.act(users, "sign_out", [self.user_b.pk])
        self.assertEqual(self.b.get_json("/api/me").status_code, 401)
        self.act(users, "void_likes", [self.user_b.pk])
        self.assertFalse(Like.objects.filter(xuid=KEY_B).exists())
        self.assertEqual(self.get(users, linked="no").context["cl"].result_count, 1)  # the operator

    def test_deleting_an_account_in_the_admin(self):
        r = self.admin.post("/admin/members/user/%d/delete/" % self.user_c.pk, {"post": "yes"}, secure=True)
        self.assertEqual(r.status_code, 302, r.content[:500])
        self.assertFalse(User.objects.filter(pk=self.user_c.pk).exists())
        self.assertFalse(Like.objects.filter(xuid=KEY_C).exists())

    def test_the_game_links(self):
        links = "/admin/members/gamelink/"
        page = self.get(links)
        actions = [a for a, _ in page.context["action_form"].fields["action"].choices if a]
        self.assertNotIn("delete_selected", actions)  # unlink instead, with its audit event
        self.act(links, "void_likes", [KEY_B])
        self.assertFalse(Like.objects.filter(xuid=KEY_B).exists())
        self.act(links, "unlink", [KEY_C])
        self.assertFalse(GameLink.objects.filter(xuid=KEY_C).exists())
        self.assertIn("the operator unlinked the game of DriverC", events(self.user_c))
        r = self.admin.post(links + "%s/delete/" % KEY_B, {"post": "yes"}, secure=True)
        self.assertEqual(r.status_code, 302)
        self.assertIn("the operator unlinked the game of DriverB", events(self.user_b))

    def test_only_the_allowed_addresses(self):
        outside = Client(HTTP_HOST=HOST, REMOTE_ADDR="203.0.113.5")
        outside.force_login(self.operator)
        self.assertEqual(outside.get("/admin/members/like/", secure=True).status_code, 404)


class Operations(ModerationTest):
    def test_stats(self):
        self.remove_photo(PHOTOS[0])
        self.purge()
        out = StringIO()
        call_command("stats", "--json", stdout=out)
        numbers = json.loads(out.getvalue())
        self.assertEqual((numbers["accounts"], numbers["links"], numbers["likes_photo"], numbers["likes_design"],
                          numbers["liked_items_missing"]), (2, 2, 4, 1, 1))
        call_command("stats", stdout=StringIO())

    def test_half_the_daily_links_is_logged_once(self):
        seed_d, xuid_d, seed_e, xuid_e = bytes([0xD0] * 32), 0x0009000000000004, bytes([0xE0] * 32), 0x0009000000000005
        self.player(xuid_d, seed_d, "DriverD")
        self.player(xuid_e, seed_e, "DriverE")
        d, user_d = self.signed_in(uid="444", ip="203.0.113.44")
        e, user_e = self.signed_in(uid="555", ip="203.0.113.55")
        with override_settings(LINKS_DAILY=6), self.assertLogs("members", "WARNING") as logs:
            self.link(d, user_d, seed=seed_d, xuid=xuid_d)
        self.assertIn("3 new game links in the past day", "".join(logs.output))
        with override_settings(LINKS_DAILY=6), mock.patch("members.links.log") as log:
            self.link(e, user_e, seed=seed_e, xuid=xuid_e)
        log.warning.assert_not_called()  # once a day


def service_like_database(path, *users):
    """A small database with this service's tables' names (a test's own database cannot be
    backed up from inside its transaction)."""
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE django_migrations(id); CREATE TABLE members_user(username);")
    db.executemany("INSERT INTO members_user VALUES (?)", [(u,) for u in users])
    db.commit()
    return db


class Restore(AccountsTest):
    def test_a_restore_drill(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BACKUP_DIR=Path(folder) / "backups"):
            live = service_like_database(Path(folder) / "accounts.sqlite3", "BEFORE")
            dest = nightly.backup(source=live)
            live.execute("INSERT INTO members_user VALUES ('AFTER')")
            live.commit()
            keep = restore.restore(dest, target=live)
            self.assertEqual(live.execute("SELECT username FROM members_user").fetchall(), [("BEFORE",)])
            live.close()
            before = sqlite3.connect(keep)  # the database as it was, kept first
            self.assertEqual(before.execute("SELECT username FROM members_user").fetchall(), [("BEFORE",), ("AFTER",)])
            before.close()
            self.assertEqual(len(list((Path(folder) / "backups").glob("accounts-*.sqlite3"))), 1)  # not one of the 7

    def test_the_pepper_comes_back_when_it_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with override_settings(BACKUP_DIR=folder / "backups", EMAIL_PEPPER_FILE=folder / "email_pepper.bin"):
                (folder / "email_pepper.bin").write_bytes(b"p" * 32)
                live = service_like_database(folder / "accounts.sqlite3")
                dest = nightly.backup(source=live)
                (folder / "email_pepper.bin").unlink()
                restore.restore(dest, target=live)
                live.close()
                self.assertEqual((folder / "email_pepper.bin").read_bytes(), b"p" * 32)

    def test_the_command_checks_the_backup_first(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BACKUP_DIR=Path(folder)):
            live = service_like_database(Path(folder) / "live.sqlite3")
            dest = nightly.backup(source=live)
            live.close()
            with self.assertRaisesRegex(CommandError, "--yes"):
                call_command("restore", dest.stem[len("accounts-"):], stdout=StringIO())
            with self.assertRaisesRegex(CommandError, "no backup"):
                call_command("restore", "1999-01-01", "--yes", stdout=StringIO())
            damaged = Path(folder) / "damaged.sqlite3"
            damaged.write_bytes(b"not a database" * 100)
            with self.assertRaisesRegex(CommandError, "not a database"):
                call_command("restore", str(damaged), "--yes", stdout=StringIO())
            other = sqlite3.connect(Path(folder) / "other.sqlite3")
            other.execute("CREATE TABLE t(x)")
            other.commit()
            other.close()
            with self.assertRaisesRegex(CommandError, "not a backup of this service"):
                call_command("restore", str(Path(folder) / "other.sqlite3"), "--yes", stdout=StringIO())


del LikesTest  # imported for its helpers: its tests run in test_likes
