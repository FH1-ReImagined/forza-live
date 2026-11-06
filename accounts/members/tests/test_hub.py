"""The mods and texture packs hub (members.hub) end to end: an upload through the API, the
scanning worker (hub/hub_worker.py, with a stand-in for clamscan), the results, a maintainer's
review in the admin, the public list and the download host's folder, likes, reports and takedowns.

The rules: off unless FORZA_HUB=1; only a signed-in account linked to its game uploads, reports or
likes; the upload is streamed (never parsed here, never held whole: larger than Django's 16 KB
body limit), checked for its size, its first bytes, the quota, the open-items and queue caps and
the rate limits, and needs its CSRF token, Origin and an upload type; what the worker refuses says
why to its uploader; nothing is public before a maintainer approves it, and then only from the
download host's folder, as the worker's clean zip with the SHA-256 shown; a malware report hides
it at once; the owner or a maintainer takes it down, files and likes and all."""

import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, override_settings

from members import hub, likes
from members.models import AuditEvent, HubItem, HubReport, Like, User
from members.tests.support import HOST
from members.tests.test_likes import XUID_C, LikesTest
from members.tests.test_links import SEED_B, SEED_C, XUID_B, body

REPO = Path(settings.BASE_DIR).parent
sys.path.insert(0, str(REPO / "hub"))
import hub_worker  # noqa: E402
from test_hub import EICAR, FAKE_CLAMSCAN, MOD, PACK, make_zip  # noqa: E402


class HubTest(LikesTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dirs = Path(tmp.name)
        for d in ("incoming", "checked", "public", "work", "db"):
            (self.dirs / d).mkdir()
        (self.dirs / "db" / "test.ndb").write_text("x\n")  # the stand-in reads no signature
        override = override_settings(HUB=True, HUB_INCOMING=self.dirs / "incoming", HUB_CHECKED=self.dirs / "checked",
                                     HUB_PUBLIC=self.dirs / "public", HUB_DOWNLOADS="https://downloads.example")
        override.enable()
        self.addCleanup(override.disable)
        fake = self.dirs / "clamscan"
        fake.write_text(FAKE_CLAMSCAN.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        fake.chmod(0o755)
        self.worker = type("A", (), dict(incoming=str(self.dirs / "incoming"), checked=str(self.dirs / "checked"),
                                         work=str(self.dirs / "work"), clamav_db=str(self.dirs / "db"),
                                         clamscan=str(fake)))
        self.b, self.user_b = self.linked("222", SEED_B, XUID_B)
        hub.forget()
        hub._collected[0] = 0.0

    def new(self, browser=None, **fields):
        doc = dict(kind="mod", title="Better title", description="Line one\nLine two", version="1.2", own_work=True)
        doc.update(fields)
        return (browser or self.b).post_json("/api/hub/items", doc)

    def put(self, item_id, data, browser=None, content_type="application/zip", **kw):
        return (browser or self.b).send("PUT", "/api/hub/items/%s/file" % item_id, data, content_type=content_type, **kw)

    def upload(self, data, browser=None, **fields):
        r = self.new(browser, **fields)
        self.assertEqual(r.status_code, 200, r.content)
        item_id = body(r)["id"]
        r = self.put(item_id, data, browser)
        self.assertEqual(r.status_code, 200, r.content)
        return item_id

    def scan(self):
        hub_worker.once(self.worker, "ClamAV test")
        return hub.collect(force=True)

    def public_list(self, **query):
        hub.forget()
        r = self.client.get("/api/hub/items", query, secure=True, HTTP_HOST=HOST)
        self.assertEqual(r.status_code, 200, r.content)
        return body(r)

    def live(self, data=None, **fields):
        item_id = self.upload(data or make_zip({"Better/" + k: v for k, v in MOD.items()}), **fields)
        self.scan()
        hub.approve(HubItem.objects.get(pk=item_id), "op")
        return item_id


class Flow(HubTest):
    def test_upload_check_review_publish(self):
        data = make_zip({"Better/" + k: v for k, v in MOD.items()}) + b""
        r = self.new()
        self.assertEqual(r.status_code, 200, r.content)
        doc = body(r)
        self.assertEqual((doc["state"], doc["upload"], doc["max_bytes"]),
                         ("draft", "/api/hub/items/%s/file" % doc["id"], 512 << 20))
        item_id = doc["id"]
        self.assertEqual(body(self.put(item_id, data)), {"ok": True, "state": "queued"})
        job = json.loads((self.dirs / "incoming" / (item_id + ".json")).read_text())
        self.assertEqual(job, {"version": 1, "id": item_id, "kind": "mod", "title": "Better title", "size": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.public_list()["items"], [])  # nothing public before the review
        self.assertEqual(self.scan(), 1)
        item = HubItem.objects.get(pk=item_id)
        self.assertEqual((item.state, item.folder, item.files), ("review", "Better-title", len(MOD)))
        self.assertFalse((self.dirs / "incoming" / (item_id + ".zip")).exists())  # the upload is gone
        self.assertEqual(list((self.dirs / "public").iterdir()), [])
        mine = body(self.b.get_json("/api/hub/mine"))["items"]
        self.assertEqual([(m["id"], m["state"], m["reason"]) for m in mine], [(item_id, "review", "")])

        hub.approve(item, "op")
        public = self.dirs / "public" / item.sha256 / "Better-title.zip"
        self.assertEqual(hashlib.sha256(public.read_bytes()).hexdigest(), item.sha256)
        with zipfile.ZipFile(public) as z:
            self.assertEqual(sorted(z.namelist()), sorted("Better-title/" + k for k in MOD))
        listed = self.public_list()
        self.assertEqual(listed["downloads"], "https://downloads.example")
        entry = listed["items"][0]
        self.assertEqual({k: entry[k] for k in ("id", "kind", "title", "description", "version", "author", "sha256",
                                                "folder", "likes")},
                         {"id": item_id, "kind": "mod", "title": "Better title", "description": "Line one\nLine two",
                          "version": "1.2", "author": "DriverB", "sha256": item.sha256, "folder": "Better-title",
                          "likes": 0})
        self.assertEqual(entry["download"], "https://downloads.example/%s/Better-title.zip" % item.sha256)
        text = json.dumps(listed)
        self.assertNotIn("%016X" % XUID_B, text)
        self.assertNotIn(self.user_b.username, text)
        one = self.client.get("/api/hub/items/" + item_id, secure=True, HTTP_HOST=HOST)
        self.assertEqual((one.status_code, one["Cache-Control"]), (200, "public, max-age=60"))
        self.assertEqual(self.client.get("/api/hub/items/AAAAAAAAAAAA", secure=True, HTTP_HOST=HOST).status_code, 404)

    def test_a_refused_upload_says_why_and_malware_is_named(self):
        bad = self.upload(make_zip(dict(MOD, **{"media/x.zip": b"PK\x03\x04"})))
        virus = self.upload(make_zip(dict(MOD, **{"media/note.txt": EICAR})), title="Free cars")
        self.scan()
        reasons = {m["id"]: (m["state"], m["reason"]) for m in body(self.b.get_json("/api/hub/mine"))["items"]}
        self.assertEqual(reasons[bad][0], "refused")
        self.assertIn(".zip files are not allowed", reasons[bad][1])
        self.assertEqual(reasons[virus][0], "refused")
        self.assertIn("malware scan found Eicar-Signature", reasons[virus][1])
        self.assertEqual(sorted(p.name for p in (self.dirs / "checked").iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.dirs / "incoming").iterdir()), [])

    def test_a_texture_pack(self):
        item_id = self.upload(make_zip(PACK), kind="pack", title="HD roads")
        self.scan()
        item = HubItem.objects.get(pk=item_id)
        self.assertEqual((item.state, item.kind, json.loads(item.manifest)["name"]), ("review", "pack", "HD roads"))


class TheUpload(HubTest):
    def test_off_unless_the_flag(self):
        with override_settings(HUB=False):
            self.assertEqual((self.new().status_code, body(self.new())), (404, {"error": "not available"}))
            self.assertEqual(self.client.get("/api/hub/items", secure=True, HTTP_HOST=HOST).status_code, 404)
            self.assertEqual(self.put("AAAAAAAAAAAA", b"PK\x03\x04xx").status_code, 404)

    def test_only_linked_accounts_and_only_their_own_work(self):
        anon = Client(HTTP_HOST=HOST)
        r = anon.post("/api/hub/items", "{}", content_type="application/json", secure=True)
        self.assertEqual(r.status_code, 401)
        unlinked, _ = self.signed_in(uid="999", ip="203.0.113.99")
        self.assertEqual(body(self.new(unlinked)), {"error": "link your game first"})
        self.assertEqual((self.new(own_work=False).status_code, body(self.new(own_work=False))),
                         (400, {"error": "only your own work"}))
        self.assertEqual(body(self.new(title=" ‮\n ")), {"error": "give it a title"})
        r = self.new(title="A" * 100, description="<script>x</script>\u0007" * 3, version="v" * 30)
        item = HubItem.objects.get(pk=body(r)["id"])
        self.assertEqual((len(item.title), item.version, item.description[:20]), (60, "v" * 20, "<script>x</script> <"))
        self.assertEqual(self.new(kind="exe").status_code, 422)

    def test_a_big_upload_is_streamed_not_held(self):
        data = make_zip(dict(MOD, **{"media/big.bin": os.urandom(200_000)}))
        self.assertGreater(len(data), 16384 * 4)
        item_id = self.upload(data)
        self.assertEqual((self.dirs / "incoming" / (item_id + ".zip")).read_bytes(), data)

    def test_what_the_upload_must_be(self):
        item_id = body(self.new())["id"]
        self.assertEqual(self.put(item_id, b"MZ\x90\x00" + b"\x00" * 100).status_code, 415)
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "draft")  # it can be sent again
        self.assertEqual(self.put(item_id, b"PK\x03\x04x", content_type="text/html").status_code, 415)
        self.assertEqual(self.put(item_id, b"PK").status_code, 400)
        with override_settings(HUB_MAX_BYTES=100):
            r = self.put(item_id, b"PK\x03\x04" + b"x" * 200)
            self.assertEqual((r.status_code, body(r)), (413, {"error": "bigger than 0 MB"}))
        with override_settings(HUB_QUOTA_BYTES=10):
            self.assertEqual(self.put(item_id, make_zip(MOD)).status_code, 503)
        self.assertEqual(self.put(item_id, make_zip(MOD), token=False).status_code, 403)              # CSRF
        self.assertEqual(self.put(item_id, make_zip(MOD), origin="https://evil.example").status_code, 403)
        self.assertEqual(self.put(item_id, make_zip(MOD), fetch_site="cross-site").status_code, 403)
        self.assertEqual(self.put(item_id, make_zip(MOD)).status_code, 200)
        self.assertEqual(self.put(item_id, make_zip(MOD)).status_code, 409)  # it has its file
        c, _ = self.linked("333", SEED_C, XUID_C)
        other = body(self.new())["id"]
        self.assertEqual(self.put(other, make_zip(MOD), browser=c).status_code, 404)  # not theirs
        self.assertIn(self.put("../../etc/x", make_zip(MOD)).status_code, (404, 415))
        self.assertEqual(sorted(p.name for p in (self.dirs / "incoming").iterdir()),
                         sorted([item_id + ".zip", item_id + ".json"]))

    def test_caps_and_limits(self):
        for _ in range(3):
            self.assertEqual(self.new().status_code, 200)
        r = self.new()
        self.assertEqual((r.status_code, body(r)), (409, {"error": "wait until your other uploads are checked"}))
        HubItem.objects.update(state=HubItem.LIVE)
        self.assertEqual(self.new().status_code, 200)
        self.assertEqual(self.new().status_code, 200)
        self.assertEqual(self.new().status_code, 429)  # 5 a day
        with override_settings(HUB_QUEUE_MAX=0):
            HubItem.objects.update(state=HubItem.LIVE)
            c, _ = self.linked("333", SEED_C, XUID_C)
            HubItem.objects.create(id="QUEUEDAAAAAA", kind="mod", state="queued", owner="X", title="t",
                                   created=HubItem.objects.first().created, updated=HubItem.objects.first().created)
            self.assertEqual(self.new(c).status_code, 503)


class Moderation(HubTest):
    def test_the_admin_review(self):
        item_id = self.upload(make_zip(MOD))
        operator = User.objects.create(is_staff=True, is_superuser=True, username="OPERATOR")
        admin = Client(HTTP_HOST=HOST, REMOTE_ADDR="127.0.0.1")
        admin.force_login(operator)
        hub_worker.once(self.worker, "ClamAV test")
        page = admin.get("/admin/members/hubitem/%s/change/" % item_id, secure=True)
        self.assertContains(page, "Download the clean zip")
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "review")  # the admin collected the result
        f = admin.get("/admin/members/hubitem/%s/file/" % item_id, secure=True)
        self.assertEqual((f.status_code, f["Content-Type"], f["X-Content-Type-Options"]),
                         (200, "application/octet-stream", "nosniff"))
        self.assertIn("attachment", f["Content-Disposition"])
        outside = Client(HTTP_HOST=HOST, REMOTE_ADDR="198.51.100.9")
        outside.force_login(operator)
        self.assertEqual(outside.get("/admin/members/hubitem/%s/file/" % item_id, secure=True).status_code, 404)
        r = admin.post("/admin/members/hubitem/", {"action": "approve", "_selected_action": [item_id]}, secure=True)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "live")
        admin.post("/admin/members/hubitem/", {"action": "hide", "_selected_action": [item_id]}, secure=True)
        self.assertEqual(list((self.dirs / "public").iterdir()), [])  # no more downloads
        admin.post("/admin/members/hubitem/", {"action": "unhide", "_selected_action": [item_id]}, secure=True)
        self.assertEqual(len(list((self.dirs / "public").iterdir())), 1)
        admin.post("/admin/members/hubitem/", {"action": "take_down", "_selected_action": [item_id]}, secure=True)
        self.assertEqual((HubItem.objects.get(pk=item_id).state, list((self.dirs / "public").iterdir())), ("removed", []))

    def test_a_tampered_clean_zip_is_not_published(self):
        item_id = self.upload(make_zip(MOD))
        self.scan()
        (self.dirs / "checked" / (item_id + ".zip")).write_bytes(make_zip({"media/evil.bin": b"x"}))
        with self.assertRaisesRegex(hub.HubError, "SHA-256"):
            hub.approve(HubItem.objects.get(pk=item_id), "op")
        self.assertEqual(list((self.dirs / "public").iterdir()), [])

    def test_bad_worker_results_are_not_believed(self):
        item_id = self.upload(make_zip(MOD))
        for doc in ({"version": 1, "id": item_id, "ok": True}, {"version": 1, "id": "OTHERAAAAAAA", "ok": True},
                    {"version": 1, "id": item_id, "ok": True, "sha256": "0" * 64, "size": 5, "files": 1, "unpacked": 1,
                     "folder": "../../x"}, "junk"):
            (self.dirs / "checked" / (item_id + ".json")).write_text(json.dumps(doc))
            self.assertEqual(hub.collect(force=True), 0)
            self.assertEqual(HubItem.objects.get(pk=item_id).state, "queued")

    def test_reports_likes_and_takedowns(self):
        item_id = self.live()
        c, user_c = self.linked("333", SEED_C, XUID_C)
        self.assertEqual(body(self.like(c, item_id, kind="mod"))["ok"], True)
        self.assertEqual(self.public_list()["items"][0]["likes"], 1)
        self.assertEqual(self.like(self.b, item_id, kind="mod").status_code, 403)  # its own
        mine = body(self.b.get_json("/api/likes/mine?kind=mod"))
        self.assertEqual(mine["own"], [item_id])
        self.assertEqual(self.b.post_json("/api/hub/reports", {"item": item_id, "reason": "other"}).status_code, 403)
        r = c.post_json("/api/hub/reports", {"item": item_id, "reason": "broken", "note": "crashes"})
        self.assertEqual(body(r), {"ok": True})
        self.assertEqual(c.post_json("/api/hub/reports", {"item": item_id, "reason": "broken"}).status_code, 409)
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "live")  # one report: still live
        d, _ = self.signed_in(uid="444", ip="203.0.113.44")
        self.assertEqual(d.post_json("/api/hub/reports", {"item": item_id, "reason": "malware"}).status_code, 403)
        HubReport.objects.all().delete()
        c.post_json("/api/hub/reports", {"item": item_id, "reason": "malware"})
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "hidden")  # malware: hidden at once
        self.assertEqual(self.public_list()["items"], [])
        self.assertEqual(list((self.dirs / "public").iterdir()), [])
        hub.unhide(HubItem.objects.get(pk=item_id), "op")
        # The owner takes it down: a recent sign-in, then its files and likes go.
        r = self.b.post_json("/api/hub/items/%s/delete" % item_id, {})
        self.assertEqual(body(r), {"ok": True})
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "removed")
        self.assertFalse(Like.objects.filter(kind="mod").exists())
        self.assertEqual(list((self.dirs / "public").iterdir()), [])
        self.assertIn("deleted the mod %s" % item_id, AuditEvent.objects.filter(user=self.user_b).values_list("event", flat=True))
        self.assertEqual(self.like(c, item_id, kind="mod").status_code, 404)

    def test_deleting_the_account_takes_its_uploads_down(self):
        item_id = self.live()
        r = self.b.post_json("/api/account/delete", {"confirm": self.user_b.public_id})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "removed")
        self.assertEqual(list((self.dirs / "public").iterdir()), [])

    def test_upkeep(self):
        from datetime import timedelta
        from django.utils import timezone
        draft = body(self.new())["id"]
        HubItem.objects.filter(pk=draft).update(updated=timezone.now() - timedelta(days=2))
        stuck = body(self.new())["id"]
        HubItem.objects.filter(pk=stuck).update(state="uploading", updated=timezone.now() - timedelta(hours=2))
        report = hub.upkeep()
        self.assertEqual((report["drafts_deleted"], report["uploads_reset"]), (1, 1))
        self.assertEqual(HubItem.objects.get(pk=stuck).state, "draft")

    def test_likes_on_photos_are_unchanged(self):
        self.assertEqual(body(self.like(self.b))["ok"], True)
        likes.forget_counts()
        self.assertEqual(self.totals(), {"PHOTOAAAAAAA": 1})
        self.assertEqual(self.b.post_json("/api/me/items/delete", {"kind": "mod", "id": "AAAAAAAAAAAA"}).status_code, 422)

    def test_the_upload_s_bytes_never_reach_a_zip_reader_here(self):
        data = make_zip(MOD)
        with mock.patch("zipfile.ZipFile", side_effect=AssertionError("the accounts service opened a zip")):
            item_id = self.upload(data)
            hub.collect(force=True)
        self.assertEqual(HubItem.objects.get(pk=item_id).state, "queued")
