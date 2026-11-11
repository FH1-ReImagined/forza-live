"""Likes and the player's own items (members.likes, members.items, members.game.counted), against
a game server's data folder made here (community.json, designs.json beside friends.json, players/
and banned_xuids.json), and the owner-only delete route of the game server itself
(forza_live.content.owners) served on 127.0.0.1.

The like rules: a linked, usable account only; public items only; never the key's own; one like
per key. The public totals: only keys whose link is active and pinned, not banned, and that have a
result on the boards at any time (firstScore, a signed sign-in or not), at once by default, or
after the operator's wait (LIKES_AFTER_HOURS, LIKES_SIGNED_DAYS, on firstSigned) when set; a
frozen, banned, unlinked or unscored key does not count. 50,000 likes are counted in under a second. My items: the key's
items in every state, and nobody else's. Deleting: a recent sign-in, the owner's item, the game
server's own checks, and the likes go with it."""

import json
import threading
import time
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from forza_live.content import owners
from forza_live.core import ed25519
from members import items, likes
from members.models import AuditEvent, GameLink, Like, User
from members.tests.support import Browser
from members.tests.test_links import SEED, SEED_B, SEED_C, XUID, XUID_B, LinkTest, body

XUID_C = 0x0009000000000003
PHOTOS = ["PHOTOAAAAAAA", "PHOTOBBBBBBB", "PHOTOCCCCCCC"]
DESIGN, DESIGN_B = "DESIGNAAAAAA", "DESIGNBBBBBB"
DAY = 86400
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def item_id(i):
    return "PH" + "".join(B32[(i >> (5 * k)) & 31] for k in range(10))


def photo(owner, status="live", title="Sunset", at=1_800_000_000):
    return {"owner": owner, "fileId": 1, "gamertag": "x", "title": title, "type": "jpeg", "takenAt": at,
            "created": at, "status": status, "approvedAt": at if status == "live" else None,
            "decidedAt": None, "note": "", "sha256": "0" * 64, "w": 1280, "h": 720, "size": 1}


def design(owner, wid, web="clear", status="live", title="FLAMES", at=1_800_000_000, done=True):
    return {"kind": "livery", "owner": owner, "done": done, "created": at, "status": status, "code": "ABC123",
            "meta": {"title": title, "description": "", "modelId": 1}, "ratings": {}, "downloaders": [],
            "reporters": [], "webId": wid, "web": web, "webNote": "", "webAt": None,
            "png": {"w": 288, "h": 160, "sha256": "1" * 64, "size": 10}}


class LikesTest(LinkTest):
    """Driver A (XUID) shares the items; Driver B (XUID_B) and C (XUID_C) like them."""

    def setUp(self):
        super().setUp()
        self.player(XUID_B, SEED_B, "DriverB")
        self.player(XUID_C, SEED_C, "DriverC")
        self.photos = {pid: photo(XUID) for pid in PHOTOS}
        self.designs = {"100000000": design(XUID, DESIGN), "100000001": design(XUID_B, DESIGN_B, at=1_800_000_100)}
        self.write_items()
        for x in (XUID, XUID_B, XUID_C):
            self.age(x)
        items.forget()
        likes.forget_counts()

    def write_items(self):
        (self.data / "community.json").write_text(json.dumps(
            {"photos": self.photos, "uploads": {}, "serverUploads": [], "deleted": []}))
        (self.data / "designs.json").write_text(json.dumps(
            {"nextId": 100000002, "files": self.designs, "uploads": {}, "serverUploads": []}))
        items.forget()

    def age(self, xuid, days=4, signed_days=2, tag=None):
        """The key's first signed sign-in `days` ago, with signed sign-ins on `signed_days` days."""
        path = self.data / "players" / ("%016X.json" % xuid)
        record = json.loads(path.read_text())
        record.update(firstSigned=int(time.time()) - int(days * DAY), signedDays=signed_days,
                      firstScore=int(time.time()) - int(days * DAY))
        if tag:
            record["gamertag"] = tag
        path.write_text(json.dumps(record))

    def unscored(self, xuid):
        """The key's record without a result on the boards (no firstScore): a key that signed in
        but never uploaded a speed trap or any other score."""
        path = self.data / "players" / ("%016X.json" % xuid)
        record = json.loads(path.read_text())
        record.pop("firstScore", None)
        path.write_text(json.dumps(record))

    def linked(self, uid, seed, xuid):
        b, user = self.signed_in(uid=uid, ip="203.0.113.%d" % (int(uid) % 200))
        self.link(b, user, seed=seed, xuid=xuid)
        return b, user

    def like(self, b, item=PHOTOS[0], on=True, kind="photo"):
        return b.post_json("/api/likes", {"kind": kind, "item": item, "on": on})

    def counts(self, kind="photo", fresh=True, **headers):
        if fresh:
            likes.forget_counts()
        return self.client.get("/api/likes/counts", {"kind": kind}, secure=True, HTTP_HOST=settings.SITE_HOST,
                               **headers)

    def totals(self, kind="photo"):
        r = self.counts(kind)
        self.assertEqual(r.status_code, 200, r.content)
        return body(r)["counts"]


class Liking(LikesTest):
    def test_a_like_counts_once_per_key(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        r = self.like(b)
        self.assertEqual((r.status_code, body(r)), (200, {"ok": True, "on": True, "counted": True}))
        self.assertEqual(self.like(b).status_code, 200)  # again: still one
        self.assertEqual(Like.objects.count(), 1)
        self.assertEqual((Like.objects.get().xuid, Like.objects.get().item), ("%016X" % XUID_B, PHOTOS[0]))
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        c, _ = self.linked("333", SEED_C, XUID_C)
        self.like(c)
        self.like(c, PHOTOS[1])
        self.assertEqual(self.totals(), {PHOTOS[0]: 2, PHOTOS[1]: 1})
        self.assertEqual(body(self.like(b, on=False)), {"ok": True, "on": False, "counted": True})
        self.assertEqual(self.totals(), {PHOTOS[0]: 1, PHOTOS[1]: 1})

    def test_designs_too(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        self.assertEqual(self.like(b, DESIGN, kind="design").status_code, 200)
        self.assertEqual(self.like(b, DESIGN, kind="photo").status_code, 404)  # not a photo's id
        self.assertEqual(self.totals("design"), {DESIGN: 1})
        self.assertEqual(self.totals("photo"), {})

    def test_self_likes_are_refused(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        r = self.like(b, DESIGN_B, kind="design")
        self.assertEqual((r.status_code, body(r)), (403, {"error": "your own"}))
        a, _ = self.linked("111", SEED, XUID)
        for pid in PHOTOS:
            self.assertEqual(self.like(a, pid).status_code, 403)
        # a self-like put in the database by hand is not counted either
        Like.objects.create(xuid="%016X" % XUID, kind="photo", item=PHOTOS[0], at=timezone.now())
        self.assertEqual(self.totals(), {})
        self.assertFalse(Like.objects.filter(xuid="%016X" % XUID_B).exists())

    def test_only_public_items(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        self.photos[PHOTOS[1]]["status"] = "held"
        self.photos[PHOTOS[2]]["status"] = "hidden"
        self.designs["100000000"]["web"] = "pending"
        self.write_items()
        for kind, item in (("photo", PHOTOS[1]), ("photo", PHOTOS[2]), ("photo", "AAAAAAAAAAAA"),
                           ("design", DESIGN)):
            r = self.like(b, item, kind=kind)
            self.assertEqual((r.status_code, body(r)), (404, {"error": "not found"}), item)
        for bad in ("photoaaaaaaa", "PHOTO", "PHOTOAAAAAAAA", "../PHOTOAAAA", 7):
            self.assertEqual(self.like(b, bad).status_code, 422, bad)
        self.assertEqual(b.post_json("/api/likes", {"kind": "car", "item": PHOTOS[0], "on": True}).status_code, 422)
        self.assertEqual(b.post_json("/api/likes", {"kind": "photo", "item": PHOTOS[0], "on": True,
                                                    "xuid": "0009000000000001"}).status_code, 422)
        self.assertFalse(Like.objects.exists())
        # a like that was given while the photo was public can be taken back once it is not
        self.like(b)
        self.photos[PHOTOS[0]]["status"] = "hidden"
        self.write_items()
        self.assertEqual(self.totals(), {})
        self.assertEqual(self.like(b, on=False).status_code, 200)
        self.assertFalse(Like.objects.exists())

    def test_a_banned_owner_s_items_cannot_be_liked(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        self.like(b)
        self.ban(XUID)
        self.assertEqual(self.like(b, PHOTOS[1]).status_code, 404)
        self.assertEqual(self.totals(), {})

    def test_the_feed_s_cap(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        with mock.patch("forza_live.content.community.FEED_MAX", 2):
            self.photos[PHOTOS[0]].update(approvedAt=1_700_000_000)  # the oldest: not in the feed
            self.write_items()
            self.assertEqual(self.like(b, PHOTOS[0]).status_code, 404)
            self.assertEqual(self.like(b, PHOTOS[1]).status_code, 200)

    def test_only_linked_accounts(self):
        anonymous = Browser()
        r = anonymous.post_json("/api/likes", {"kind": "photo", "item": PHOTOS[0], "on": True})
        self.assertEqual(r.status_code, 401)
        b, _ = self.signed_in(uid="444")
        r = self.like(b)
        self.assertEqual((r.status_code, body(r)), (403, {"error": "link your game first"}))
        self.assertEqual(b.get_json("/api/likes/mine?kind=photo").status_code, 403)
        self.assertEqual(b.get_json("/api/me/items").status_code, 403)
        self.assertFalse(Like.objects.exists())

    def test_csrf_and_origin(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        data = {"kind": "photo", "item": PHOTOS[0], "on": True}
        self.assertEqual(b.post_json("/api/likes", data, origin="https://evil.example").status_code, 403)
        self.assertEqual(b.post_json("/api/likes", data, fetch_site="cross-site").status_code, 403)
        self.assertEqual(b.post_json("/api/likes", data, token=False).status_code, 403)
        self.assertEqual(b.send("POST", "/api/likes", "kind=photo", content_type="application/x-www-form-urlencoded")
                         .status_code, 415)
        self.assertFalse(Like.objects.exists())

    def test_the_rate_limit(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        with override_settings(ACCOUNT_RATE_LIMITS=dict(settings.ACCOUNT_RATE_LIMITS, like="3/h/key")):
            got = [self.like(b, PHOTOS[i % 3], on=i % 2 == 0).status_code for i in range(4)]
        self.assertEqual(got, [200, 200, 200, 429])

    def test_mine(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        self.like(b, PHOTOS[2])
        self.like(b, PHOTOS[0])
        r = b.get_json("/api/likes/mine?kind=photo")
        self.assertEqual(body(r), {"kind": "photo", "liked": [PHOTOS[0], PHOTOS[2]], "own": [], "counted": True})
        r = b.get_json("/api/likes/mine?kind=design")
        self.assertEqual(body(r), {"kind": "design", "liked": [], "own": [DESIGN_B], "counted": True})
        self.assertEqual(b.get_json("/api/likes/mine?kind=car").status_code, 422)
        self.assertNotIn("%016X" % XUID_B, r.content.decode())


class Counting(LikesTest):
    def setUp(self):
        super().setUp()
        self.b, self.user_b = self.linked("222", SEED_B, XUID_B)
        self.c, self.user_c = self.linked("333", SEED_C, XUID_C)
        for browser in (self.b, self.c):
            self.like(browser)

    def test_keys_count_as_soon_as_they_have_a_result_on_the_boards(self):
        # No wait by default: a key that signed in a minute ago with one signed day counts.
        self.assertEqual((settings.LIKES_AFTER_HOURS, settings.LIKES_SIGNED_DAYS), (0, 0))
        self.age(XUID_C, days=60 / DAY, signed_days=1)
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})
        self.assertEqual(body(self.c.get_json("/api/me"))["link"]["counted"], True)
        self.unscored(XUID_C)  # signed in, but no speed trap (or other score) yet
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.assertEqual(body(self.like(self.c, PHOTOS[1]))["counted"], False)  # stored, not counted yet
        self.assertEqual(body(self.c.get_json("/api/me"))["link"]["counted"], False)
        self.age(XUID_C, days=60 / DAY, signed_days=1)  # its first speed trap: counted at once
        self.assertEqual(self.totals(), {PHOTOS[0]: 2, PHOTOS[1]: 1})
        record = self.data / "players" / ("%016X.json" % XUID_C)
        # A speed trap from before the game signed its calls (the backfill), no signed sign-in: counted.
        record.write_text(json.dumps({"gamertag": "DriverC", "logons": 9, "firstScore": 1}))
        self.assertEqual(self.totals(), {PHOTOS[0]: 2, PHOTOS[1]: 1})
        self.assertEqual(body(self.c.get_json("/api/me"))["link"]["counted"], True)

    @override_settings(LIKES_AFTER_HOURS=72, LIKES_SIGNED_DAYS=2)
    def test_the_wait_still_works_when_the_operator_sets_it(self):
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})
        self.age(XUID_C, days=2.9, signed_days=5)
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.age(XUID_C, days=30, signed_days=1)
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.age(XUID_C, days=3.01, signed_days=2)
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})
        record = self.data / "players" / ("%016X.json" % XUID_C)
        # A speed trap but no signed sign-in: the wait has nothing to start from.
        record.write_text(json.dumps({"gamertag": "DriverC", "logons": 9, "firstScore": 1}))
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.assertEqual(body(self.like(self.c, PHOTOS[1]))["counted"], False)  # stored, not counted yet
        self.assertEqual(body(self.c.get_json("/api/me"))["link"]["counted"], False)
        self.assertEqual(body(self.b.get_json("/api/me"))["link"]["counted"], True)
        self.assertEqual(Like.objects.filter(xuid="%016X" % XUID_C).count(), 2)

    def test_a_banned_key_neither_likes_nor_counts(self):
        self.ban(XUID_C)
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        r = self.like(self.c, PHOTOS[1])
        self.assertEqual((r.status_code, body(r)), (403, {"error": "not available"}))
        self.ban()
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})

    def test_a_frozen_key_neither_likes_nor_counts(self):
        self.friends["%016X" % XUID_C]["pub"] = ed25519.public_key(bytes([0xD0] * 32)).hex()  # re-keyed
        self.write_friends()
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        r = self.like(self.c, PHOTOS[1])
        self.assertEqual((r.status_code, body(r)), (409, {"error": "this link needs the operator"}))
        self.assertEqual(GameLink.objects.get(user=self.user_c).state, GameLink.FROZEN)
        # the old key comes back: the link stays frozen until the operator checks it
        self.friends["%016X" % XUID_C]["pub"] = ed25519.public_key(SEED_C).hex()
        self.write_friends()
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.assertEqual(self.like(self.c, PHOTOS[1]).status_code, 409)

    def test_a_forgotten_key_waits(self):
        del self.friends["%016X" % XUID_C]
        self.write_friends()
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.assertEqual(body(self.like(self.c, PHOTOS[1])), {"error": "play online first",
                                                              "message": likes.KEY_FORGOTTEN})
        self.player(XUID_C, SEED_C, "DriverC")  # the same identity signs in again
        self.age(XUID_C)
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})

    def test_unlinked_keys_keep_their_likes_but_do_not_count(self):
        r = self.signed("POST", "/api/game/unlink", {}, seed=SEED_C, xuid=XUID_C)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})
        self.assertEqual(Like.objects.filter(xuid="%016X" % XUID_C).count(), 1)

    def test_a_moved_link_does_not_like_twice(self):
        # Driver C's key moves to a new account the next day: the key's like is still one
        d, user_d = self.signed_in(uid="555", ip="203.0.113.55")
        cache.clear()  # a day later: the once-a-day limit
        self.link(d, user_d, seed=SEED_C, xuid=XUID_C)
        self.assertEqual(GameLink.objects.get(xuid="%016X" % XUID_C).user, user_d)
        self.assertEqual(self.like(d).status_code, 200)
        self.assertEqual(self.totals(), {PHOTOS[0]: 2})
        self.assertEqual(Like.objects.filter(item=PHOTOS[0]).count(), 2)

    def test_deleting_the_account_deletes_its_key_s_likes(self):
        with mock.patch("time.time", return_value=time.time()):
            r = self.c.post_json("/api/account/delete", {"confirm": self.user_c.public_id})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(Like.objects.filter(xuid="%016X" % XUID_C).exists())
        self.assertEqual(self.totals(), {PHOTOS[0]: 1})

    def test_the_public_totals(self):
        r = self.counts()
        self.assertEqual(r.status_code, 200)
        doc = body(r)
        self.assertEqual(set(doc), {"version", "kind", "generated", "counts"})
        self.assertEqual((doc["version"], doc["kind"], doc["counts"]), (1, "photo", {PHOTOS[0]: 2}))
        self.assertEqual(r["Cache-Control"], "public, max-age=30")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", r)
        self.assertNotIn("Set-Cookie", r)
        text = r.content.decode()
        for secret in ("%016X" % XUID_B, "%016X" % XUID_C, self.user_b.public_id, self.user_c.public_id,
                       self.user_b.username, "DriverB"):
            self.assertNotIn(secret, text)
        again = self.counts(fresh=False, HTTP_IF_NONE_MATCH=r["ETag"])
        self.assertEqual((again.status_code, again.content, again["Cache-Control"]), (304, b"", "public, max-age=30"))
        self.like(self.b, PHOTOS[1])
        self.assertEqual(self.counts(fresh=False).content, r.content)  # the snapshot, for a minute
        changed = self.counts(HTTP_IF_NONE_MATCH=r["ETag"])
        self.assertEqual((changed.status_code, body(changed)["counts"]), (200, {PHOTOS[0]: 2, PHOTOS[1]: 1}))
        self.assertEqual(self.counts("car").status_code, 422)
        self.assertEqual(self.client.generic("POST", "/api/likes/counts?kind=photo", "{}", content_type="application/json",
                                             secure=True, HTTP_HOST=settings.SITE_HOST).status_code, 405)
        with override_settings(GAME_DATA=None):
            self.assertEqual(self.counts().status_code, 503)
        # every other answer of the API stays out of caches
        self.assertEqual(self.b.get_json("/api/likes/mine?kind=photo")["Cache-Control"], "no-store")

    def test_50000_likes_are_counted_in_under_a_second(self):
        keys, items_n = 2500, 500
        now = int(time.time())
        self.photos = {item_id(i): photo(XUID, at=1_800_000_000 + i) for i in range(items_n)}
        ids = sorted(self.photos)
        self.assertEqual(len(ids), items_n)
        self.write_items()
        users = User.objects.bulk_create([User(username="T%07d" % i, password="!") for i in range(keys)])
        links = []
        for i, user in enumerate(users):
            xuid = 0x000A000000000000 + i
            pub = "%064x" % (i + 1)
            self.friends["%016X" % xuid] = {"name": "K%d" % i, "seen": now, "key": "", "pub": pub, "friends": {},
                                            "in": {}, "out": {}, "blocked": {}}
            (self.data / "players" / ("%016X.json" % xuid)).write_text(json.dumps(
                dict({"gamertag": "K%d" % i}, **({"firstScore": now - 5 * DAY} if i % 10 else {}))))
            links.append(GameLink(xuid="%016X" % xuid, pub=pub, user=user, gamertag="K%d" % i,
                                  linked_at=timezone.now()))
        self.write_friends()
        GameLink.objects.bulk_create(links)
        at = timezone.now()
        Like.objects.bulk_create([Like(xuid="%016X" % (0x000A000000000000 + k), kind="photo",
                                       item=ids[(k * 7 + n) % items_n], at=at)
                                  for k in range(keys) for n in range(20)], batch_size=5000)
        self.assertEqual(Like.objects.count(), 50000 + 2)  # and Driver B's and C's
        likes.forget_counts()
        started = time.perf_counter()
        totals = likes.snapshot("photo")["counts"]
        took = time.perf_counter() - started
        self.assertLess(took, 1.0, "%.3f s" % took)
        self.assertEqual(sum(totals.values()), 2250 * 20)  # every tenth key has no result on the boards
        print("\n50,000 likes counted in %.0f ms" % (took * 1000))


class MyItems(LikesTest):
    def setUp(self):
        super().setUp()
        self.a, self.user_a = self.linked("111", SEED, XUID)
        self.b, _ = self.linked("222", SEED_B, XUID_B)
        self.photos[PHOTOS[1]].update(status="held", title="Waiting", created=1_800_000_500)
        self.photos[PHOTOS[2]].update(status="hidden", title="")
        self.designs["100000002"] = design(XUID, "DESIGNCCCCCC", web="pending", title="NEW", at=1_800_000_900)
        self.designs["100000003"] = design(XUID, "DESIGNDDDDDD", web="clear", status="hidden")  # 3 reports
        self.designs["100000004"] = design(XUID, "DESIGNEEEEEE", done=False)  # still uploading
        self.write_items()

    def test_the_key_s_items_in_every_state(self):
        self.like(self.b)
        self.like(self.b, DESIGN, kind="design")
        r = self.a.get_json("/api/me/items")
        self.assertEqual(r.status_code, 200, r.content)
        doc = likes.ItemsOut.model_validate(body(r))
        got = {(i.kind, i.id): (i.state, i.public, i.likes, i.title) for i in doc.items}
        self.assertEqual(got, {
            ("photo", PHOTOS[0]): ("live", True, 1, "Sunset"),
            ("photo", PHOTOS[1]): ("held", False, 0, "Waiting"),
            ("photo", PHOTOS[2]): ("hidden", False, 0, ""),
            ("design", DESIGN): ("live", True, 1, "FLAMES"),
            ("design", "DESIGNCCCCCC"): ("pending", False, 0, "NEW"),
            ("design", "DESIGNDDDDDD"): ("hidden", False, 0, "FLAMES"),
        })
        self.assertEqual([i.id for i in doc.items][:2], ["DESIGNCCCCCC", PHOTOS[1]])  # newest first
        self.assertNotIn(DESIGN_B, r.content.decode())  # Driver B's
        for x in (XUID, XUID_B):
            self.assertNotIn("%016X" % x, r.content.decode())
        self.assertEqual([i["id"] for i in body(self.b.get_json("/api/me/items"))["items"]], [DESIGN_B])


class OwnerRoute(BaseHTTPRequestHandler):
    """The game server's HTTP port, as far as the owner routes go: forza_live.content.owners itself,
    on the same data folder (configured by members.game)."""
    seen = []
    answer = None  # a status to give instead of the route's

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        OwnerRoute.seen.append((self.path, headers, raw))
        if OwnerRoute.answer is not None:
            status, ctype, data = OwnerRoute.answer, "application/json", b"{}"
        else:
            status, ctype, data, _ = owners.handle("POST", self.path, headers, raw, proxied=False)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Deleting(LikesTest):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), OwnerRoute)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        OwnerRoute.seen, OwnerRoute.answer = [], None
        token = mock.patch.object(owners, "OWNER_TOKEN", "the-owner-token")
        token.start()
        self.addCleanup(token.stop)
        game = override_settings(GAME_URL="http://127.0.0.1:%d" % self.httpd.server_address[1],
                                 OWNER_TOKEN="the-owner-token")
        game.enable()
        self.addCleanup(game.disable)
        (self.data / "community").mkdir()
        (self.data / "community" / (PHOTOS[0] + ".jpg")).write_bytes(b"jpeg")
        self.a, self.user_a = self.linked("111", SEED, XUID)
        self.b, _ = self.linked("222", SEED_B, XUID_B)

    def delete(self, browser, kind, item_id):
        return browser.post_json("/api/me/items/delete", {"kind": kind, "id": item_id})

    def test_the_owner_deletes_a_photo_and_its_likes_go(self):
        self.like(self.b)
        self.like(self.b, PHOTOS[1])
        r = self.delete(self.a, "photo", PHOTOS[0])
        self.assertEqual((r.status_code, body(r)), (200, {"ok": True, "deleted": True}))
        path, headers, raw = OwnerRoute.seen[-1]
        self.assertEqual((path, json.loads(raw)), ("/owner/photos/%s/delete" % PHOTOS[0], {"owner": "%016X" % XUID}))
        self.assertEqual(headers["x-owner-token"], "the-owner-token")
        for header in ("origin", "x-forwarded-for", "x-real-ip", "forwarded", "cookie"):
            self.assertNotIn(header, headers)
        saved = json.loads((self.data / "community.json").read_text())
        self.assertNotIn(PHOTOS[0], saved["photos"])  # the game server's own index
        self.assertFalse((self.data / "community" / (PHOTOS[0] + ".jpg")).exists())
        self.assertEqual(list(Like.objects.values_list("item", flat=True)), [PHOTOS[1]])
        self.assertEqual(self.totals(), {PHOTOS[1]: 1})
        self.assertNotIn(PHOTOS[0], [i["id"] for i in body(self.a.get_json("/api/me/items"))["items"]])
        self.assertIn("deleted the photo “Sunset”", [e.event for e in AuditEvent.objects.filter(user=self.user_a)])
        self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 404)

    def test_the_owner_deletes_a_design(self):
        (self.data / "designs").mkdir()
        r = self.delete(self.b, "design", DESIGN_B)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("100000001", json.loads((self.data / "designs.json").read_text())["files"])

    def test_only_the_owner(self):
        r = self.delete(self.b, "photo", PHOTOS[0])
        self.assertEqual((r.status_code, body(r)), (404, {"error": "not found"}))
        self.assertEqual(self.delete(self.b, "photo", "AAAAAAAAAAAA").status_code, 404)
        self.assertEqual(self.delete(self.b, "design", PHOTOS[0]).status_code, 404)
        self.assertEqual(OwnerRoute.seen, [])
        anonymous = Browser()
        self.assertEqual(anonymous.post_json("/api/me/items/delete", {"kind": "photo", "id": PHOTOS[0]}).status_code, 401)
        self.assertIn(PHOTOS[0], json.loads((self.data / "community.json").read_text())["photos"])

    def test_a_recent_sign_in(self):
        with mock.patch("time.time", return_value=time.time() + 601):
            r = self.delete(self.a, "photo", PHOTOS[0])
        self.assertEqual((r.status_code, body(r)), (403, {"error": "sign in again"}))
        self.assertEqual(OwnerRoute.seen, [])

    def test_the_game_server_checks_again(self):
        # the game server's index says another owner (it changed since this service read it)
        self.photos[PHOTOS[0]]["owner"] = XUID_B
        (self.data / "community.json").write_text(json.dumps({"photos": self.photos}))
        with mock.patch.object(items, "find", return_value=items.Item("photo", PHOTOS[0], "%016X" % XUID,
                                                                      "Sunset", "live", True, 0)):
            r = self.delete(self.a, "photo", PHOTOS[0])
        self.assertEqual((r.status_code, body(r)), (404, {"error": "not found"}))
        with mock.patch.object(owners, "OWNER_TOKEN", "another-token"), self.assertLogs("members", "WARNING"):
            r = self.delete(self.b, "photo", PHOTOS[0])  # a token that is not the game server's
        self.assertEqual(r.status_code, 502)
        self.assertIn(PHOTOS[0], json.loads((self.data / "community.json").read_text())["photos"])

    def test_the_game_server_unreachable_or_failing(self):
        OwnerRoute.answer = 500
        with self.assertLogs("members", "WARNING"):
            self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 502)
        OwnerRoute.answer = 302
        with self.assertLogs("members", "WARNING"):
            self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 502)
        with override_settings(GAME_URL="http://127.0.0.1:9"), self.assertLogs("members", "WARNING"):
            self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 502)
        with override_settings(OWNER_TOKEN=""):
            r = self.delete(self.a, "photo", PHOTOS[0])
            self.assertEqual((r.status_code, body(r)), (503, {"error": "deleting is not available"}))
        self.assertEqual(Like.objects.count(), 0)
        self.assertIn(PHOTOS[0], json.loads((self.data / "community.json").read_text())["photos"])

    def test_a_banned_or_frozen_key_cannot_delete(self):
        self.ban(XUID)
        self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 403)
        self.ban()
        self.friends["%016X" % XUID]["pub"] = ed25519.public_key(bytes([0xD0] * 32)).hex()
        self.write_friends()
        self.assertEqual(self.delete(self.a, "photo", PHOTOS[0]).status_code, 409)
        self.assertEqual(OwnerRoute.seen, [])

    def test_the_rate_limit(self):
        with override_settings(ACCOUNT_RATE_LIMITS=dict(settings.ACCOUNT_RATE_LIMITS, item_delete="2/d/key")):
            got = [self.delete(self.a, "photo", pid).status_code for pid in PHOTOS]
        self.assertEqual(got, [200, 200, 429])
        self.assertEqual(len(OwnerRoute.seen), 2)


class Unconfigured(LikesTest):
    def test_without_the_game_data_folder(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        with override_settings(GAME_DATA=None):
            self.assertEqual(self.like(b).status_code, 503)
            self.assertEqual(b.get_json("/api/me/items").status_code, 503)
            self.assertFalse(body(b.get_json("/api/me"))["link"]["counted"])

    def test_damaged_index_files_list_nothing(self):
        b, _ = self.linked("222", SEED_B, XUID_B)
        (self.data / "community.json").write_text("{not json")
        (self.data / "designs.json").write_text(json.dumps({"files": {"1": {"owner": "x"}, "2": []}}))
        items.forget()
        self.assertEqual(self.like(b).status_code, 404)
        self.assertEqual(body(b.get_json("/api/me/items"))["items"], [])
        self.assertEqual(self.totals(), {})


del LinkTest  # imported for its helpers: its tests run in test_links
