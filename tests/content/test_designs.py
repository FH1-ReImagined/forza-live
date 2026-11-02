"""The design storefront (forza_designs.py): the calls the game's Paint Shop storefront makes, as
its type descriptors encode them (forza_xtsw_all.py), through the server's dispatch with signed
and unsigned callers. Synthetic payloads and previews only (no game data).

Run: python3 -m unittest discover -p 'test_*.py'
"""

import json
import os
import struct
import tempfile
import time
import unittest
import zlib
from types import SimpleNamespace

import forza_auth as auth
import forza_clubs as clubs
import forza_designs as designs
import forza_ed25519 as ed25519
import forza_friends as friends
import forza_limits as limits
import forza_live_lsp as lsp
import forza_photos as photos
import forza_xtsw as x
import forza_xtsw_all as A

ALICE, BOB, CAROL, DAVE, EVE = (0x0009000000000A01, 0x0009000000000B02, 0x0009000000000C03,
                               0x0009000000000D04, 0x0009000000000E05)
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", DAVE: "Dave", EVE: "Eve"}
SEEDS = {xuid: bytes([n + 1]) * 32 for n, xuid in enumerate(NAMES)}
OLD = 0x0009000000000F06  # a game before signed requests: no key on record, its calls unverified
TITLE = "/Services.Horizon/o.xtsw"
MAKE, MODEL = 21, 1398
ANY = {"PriceMax": -1, "MakeId": -1, "ModelId": -1, "ClassId": -1, "KeywordOneId": -1,
       "KeywordTwoId": -1, "ManualSearchKeywords": "", "EnvironmentId": -1, "TrackId": -1,
       "NumLayers": -1, "GamerTag": ""}
FLAG = {"High": 0x800000000, "Low": 1}  # what the game sends as clientContentFlag


def cxds(width=288, height=160):
    """A synthetic preview: cxds v2 header, then a raw-deflated texture of zeros."""
    texture = bytes(width * height * 4)
    c = zlib.compressobj(wbits=-15)
    packed = c.compress(texture) + c.flush()
    return b"cxds" + struct.pack(">IIII", 2, len(texture), len(packed), len(texture)) + packed


def livery_payload(n=600):
    return b"crlv" + bytes([3, 0, 0, 1]) + os.urandom(n - 8)


def group_payload(n=134):
    return b"lvyg" + os.urandom(n - 4)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        lsp.DATA_DIR = self.dir.name
        auth.SERVER_NAMES.clear()
        auth._nonces.clear()
        limits.configure(self.dir.name)
        photos.configure(self.dir.name)
        designs.configure(self.dir.name, proven=lsp.caller_signed, trusted=lsp.caller_trusted)
        clubs.configure(self.dir.name, lambda xuid: {"gamertag": NAMES.get(xuid, "")})
        store = friends.Store()
        for xuid, seed in SEEDS.items():
            store.get(xuid, create=True)["pub"] = ed25519.public_key(seed).hex()
        self.server = SimpleNamespace(friends=SimpleNamespace(store=store))

    def tearDown(self):
        lsp.DATA_DIR = self.old_dir
        self.dir.cleanup()

    def raw_call(self, xuid, cls, method, ptype, params, signed=True):
        headers = {"x-classname": "%s, Forza.WebServices" % cls, "x-method": method,
                   "x-userid": "0x%x" % xuid}
        body = bytes(x.encode(ptype, params))
        if signed:
            headers[auth.HEADER] = auth.auth_header(SEEDS[xuid], "forza.dyotson.dev", xuid, "POST",
                                                    TITLE, body, headers)
        return lsp.service_call(headers, body, self.server, "POST", TITLE)

    def call(self, xuid, cls, method, ptype, params, rtype=None, signed=True):
        """A call through the server's dispatch; the decoded results, {} for a success without
        results, None for a failure."""
        reply = self.raw_call(xuid, cls, method, ptype, params, signed)
        if reply[:4] != bytes(4):
            return None
        return x.decode(rtype, reply, 12)[0] if rtype else {}

    def status(self, reply):
        return struct.unpack_from(">II", reply)

    def create_livery(self, xuid, payload_size=600, preview=None, signed=True, title="PAINTJOB1",
                      description="flames", model=MODEL, keywords=(2, 1)):
        params = {"fileHeader": {"Title": title, "Description": description, "Price": 5000,
                                 "Quantity": 3, "SubmissionTime": "2026-10-30T17:58:18.547Z",
                                 "Type": 2, "KeywordOne": keywords[0], "KeywordTwo": keywords[1]},
                  "liveryDesc": {"base": {"MakeId": MAKE, "ModelId": model, "TextureId": 1,
                                          "ForStorefrontUse": True}},
                  "payloadSize": payload_size,
                  "previewData": cxds() if preview is None else preview}
        r = self.call(xuid, "Forza.WebServices.StorefrontLivery", "Create",
                      A.ForzaStorefrontLiveryCreate_Params, params,
                      A.ForzaStorefrontLiveryCreate_Results, signed)
        return r and r["contentFileId"]

    def create_group(self, xuid, payload_size=134, layers=3, title="VINYLGROUP1",
                     description="star"):
        params = {"fileHeader": {"Title": title, "Description": description, "Quantity": -1,
                                 "SubmissionTime": "2026-10-30T17:53:40.630Z", "Type": 1},
                  "layerGroupDesc": {"base": {"NumLayers": layers, "ForStorefrontUse": True}},
                  "payloadSize": payload_size, "previewData": cxds(256, 128)}
        r = self.call(xuid, "Forza.WebServices.StorefrontLayerGroup", "Create",
                      A.ForzaStorefrontLayerGroupCreate_Params, params,
                      A.ForzaStorefrontLayerGroupCreate_Results)
        return r and r["contentFileId"]

    def upload(self, xuid, fid, data, offset=0, total=None):
        r = self.call(xuid, "Xls.WebServices.Content", "UploadContent", A.ContentUploadContent_Params,
                      {"contentId": fid, "payloadOffset": offset,
                       "payloadTotalSize": total or len(data), "uploadData": data},
                      A.ContentUploadContent_Results)
        return r and r["payloadUploadedSize"]

    def share_livery(self, xuid, **kw):
        payload = livery_payload()
        fid = self.create_livery(xuid, len(payload), **kw)
        self.assertEqual(self.upload(xuid, fid, payload), len(payload))
        return fid, payload

    def share_group(self, xuid, **kw):
        payload = group_payload()
        fid = self.create_group(xuid, len(payload), **kw)
        self.assertEqual(self.upload(xuid, fid, payload), len(payload))
        return fid, payload

    def storefront(self, xuid, method, ptype, params, rtype=None, signed=True):
        return self.call(xuid, "Forza.WebServices.Storefront", method, ptype, params, rtype, signed)

    def pages(self, viewer, owner):
        return self.storefront(viewer, "GetDesignPageList", A.ForzaStorefrontGetDesignPageList_Params,
                               {"ownerXuid": owner, "maxPages": 10},
                               A.ForzaStorefrontGetDesignPageList_Results)["Pages"]

    def file_list(self, viewer, owner, page):
        return self.storefront(viewer, "GetFileListForPage", A.ForzaStorefrontGetFileListForPage_Params,
                               {"ownerXuid": owner, "startAt": 0, "maxFiles": 25, "page": page},
                               A.ForzaStorefrontGetFileListForPage_Results)

    def featured(self, viewer):
        return self.storefront(viewer, "GetFeaturedDesigns", A.ForzaStorefrontGetFeaturedDesigns_Params,
                               {"clientContentFlag": FLAG, "maxFiles": 10},
                               A.ForzaStorefrontGetFeaturedDesigns_Results)["FeaturedFiles"]

    def search_liveries(self, viewer, **q):
        return [r["Id"] for r in self.storefront(
            viewer, "SearchLiveryFiles", A.ForzaStorefrontSearchLiveryFiles_Params,
            {"searchParams": dict(ANY, **q), "clientContentFlag": FLAG, "maxFiles": 8192},
            A.ForzaStorefrontSearchLiveryFiles_Results)["searchResults"]]

    def search_groups(self, viewer, **q):
        return [r["Id"] for r in self.storefront(
            viewer, "SearchLayergroupFiles", A.ForzaStorefrontSearchLayergroupFiles_Params,
            {"searchParams": dict(ANY, **q), "maxFiles": 8192},
            A.ForzaStorefrontSearchLayergroupFiles_Results)["searchResults"]]

    def heavy(self, viewer, *ids):
        return self.storefront(viewer, "GetFileHeavyWeightData",
                               A.ForzaStorefrontGetFileHeavyWeightData_Params,
                               {"fileCount": len(ids), "FileIds": list(ids)},
                               A.ForzaStorefrontGetFileHeavyWeightData_Results)["fileData"]

    def purchase(self, viewer, fid, max_payload=1 << 20, max_preview=153600, signed=True):
        return self.storefront(viewer, "Purchase", A.ForzaStorefrontPurchase_Params,
                               {"fileId": fid, "price": 0, "maxPayloadSize": max_payload,
                                "maxPreviewSize": max_preview},
                               A.ForzaStorefrontPurchase_Results, signed)

    def by_id(self, viewer, method, ptype, params, signed=True):
        return self.storefront(viewer, method, ptype, params, None, signed)

    def code(self, fid):
        return designs.load()["files"][str(fid)]["code"]


class UploadTest(Base):
    def test_livery_upload_and_record(self):
        fid, payload = self.share_livery(ALICE)
        self.assertGreaterEqual(fid, designs.FIRST_ID)
        files = self.file_list(ALICE, ALICE, 1)
        self.assertEqual(files["totalFiles"], 1)
        r = files["Files"][0]
        self.assertEqual((r["Id"], r["Type"], r["Title"], r["Description"]), (fid, 2, "PAINTJOB1", "flames"))
        self.assertEqual((r["Price"], r["Quantity"], r["IsUnderReview"]), (0, -1, False))
        self.assertEqual((r["Ordinal"], r["PayloadSize"], r["PreviewPayload"]), (MODEL, len(payload), cxds()))
        self.assertEqual(r["Owner"]["wzGamerTag"], "Alice")
        self.assertEqual((r["KeywordOne"], r["KeywordTwo"]), (2, 1))

    def test_submission_time_is_always_valid_iso(self):
        self.share_livery(ALICE)
        self.share_group(ALICE)
        for r in self.featured(BOB):
            self.assertRegex(r["SubmissionTime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_layer_group_record(self):
        fid, payload = self.share_group(ALICE, layers=7)
        r = self.file_list(BOB, ALICE, 2)["Files"][0]
        self.assertEqual((r["Id"], r["Type"], r["Ordinal"], r["PreviewPayload"]), (fid, 1, 7, cxds(256, 128)))

    def test_upload_in_chunks(self):
        payload = livery_payload(5000)
        fid = self.create_livery(ALICE, len(payload))
        self.assertEqual(self.upload(ALICE, fid, payload[:2000], 0, 5000), 2000)
        self.assertEqual(self.featured(BOB), [])  # not listed before it is complete
        self.assertIsNone(self.upload(ALICE, fid, payload[3000:], 3000, 5000))  # a gap
        self.assertEqual(self.upload(ALICE, fid, payload[2000:], 2000, 5000), 5000)
        self.assertEqual(self.purchase(BOB, fid)["payload"], payload)

    def test_unsigned_uploads_are_refused(self):
        reply = self.raw_call(ALICE, "Forza.WebServices.StorefrontLivery", "Create",
                              A.ForzaStorefrontLiveryCreate_Params,
                              {"liveryDesc": {"base": {"ModelId": MODEL}}, "payloadSize": 600,
                               "previewData": cxds()}, signed=False)
        self.assertEqual(self.status(reply), (2, 5))
        self.assertEqual(designs.load()["files"], {})

    def test_raw_friends_key_is_not_enough_to_upload(self):
        store = self.server.friends.store
        key = bytes(range(16))
        store.players[store.hx(ALICE)] = {"name": "Alice", "pub": "", "key": friends.key_hash(key),
                                          "friends": {}, "in": {}, "out": {}, "blocked": {}}
        headers = {"x-classname": "Forza.WebServices.StorefrontLivery, Forza.WebServices",
                   "x-method": "Create", "x-userid": "0x%x" % ALICE, "x-friends-key": key.hex()}
        body = bytes(x.encode(A.ForzaStorefrontLiveryCreate_Params,
                              {"payloadSize": 600, "previewData": cxds()}))
        self.assertEqual(self.status(lsp.service_call(headers, body, self.server, "POST", TITLE)), (2, 5))

    def test_another_players_signature_is_refused(self):
        headers = {"x-classname": "Forza.WebServices.StorefrontLivery, Forza.WebServices",
                   "x-method": "Create", "x-userid": "0x%x" % ALICE}
        body = bytes(x.encode(A.ForzaStorefrontLiveryCreate_Params,
                              {"payloadSize": 600, "previewData": cxds()}))
        headers[auth.HEADER] = auth.auth_header(SEEDS[BOB], "forza.dyotson.dev", BOB, "POST", TITLE,
                                                body, headers)
        self.assertEqual(self.status(lsp.service_call(headers, body, self.server, "POST", TITLE)), (2, 5))

    def test_payload_must_be_the_kinds_design(self):
        fid = self.create_livery(ALICE, 134)
        self.assertIsNone(self.upload(ALICE, fid, group_payload()))  # a vinyl group as a paint job
        self.assertNotIn(str(fid), designs.load()["files"])
        self.assertIsNone(self.create_livery(ALICE, designs.MAX_PAYLOAD + 1))
        self.assertIsNone(self.create_livery(ALICE, 0))

    def test_preview_must_be_a_small_cxds(self):
        self.assertIsNone(self.create_livery(ALICE, preview=b"\xff\xd8\xff\xe0" + bytes(500)))
        self.assertIsNone(self.create_livery(ALICE, preview=b""))
        big = cxds()[:20] + os.urandom(designs.MAX_PREVIEW)
        big = big[:4] + struct.pack(">IIII", 2, 1, len(big) - 20, 1) + big[20:]
        self.assertIsNone(self.create_livery(ALICE, preview=big))
        broken = bytearray(cxds())
        broken[12:16] = struct.pack(">I", 7)  # a length that is not the file's
        self.assertIsNone(self.create_livery(ALICE, preview=bytes(broken)))
        self.assertIsNotNone(self.create_livery(ALICE))

    def test_only_the_owner_uploads_into_a_file(self):
        fid = self.create_livery(ALICE, 600)
        self.assertIsNone(self.upload(BOB, fid, livery_payload()))

    def test_slots_per_player_and_kind(self):
        designs.PER_DAY, old = 100, designs.PER_DAY
        try:
            for _ in range(designs.SLOTS):
                self.share_livery(ALICE)
            self.assertIsNone(self.create_livery(ALICE))
            self.assertIsNotNone(self.create_group(ALICE))  # another kind's page
            self.assertIsNotNone(self.create_livery(BOB))
            pages = {p["Page"]: p for p in self.pages(ALICE, ALICE)}
            self.assertEqual(sorted(pages), [1, 2])  # no tuning (0) or replay (4) tab
            self.assertEqual((pages[1]["TotalSlots"], pages[1]["UsedSlots"], pages[2]["UsedSlots"]),
                             (25, 25, 1))
        finally:
            designs.PER_DAY = old

    def test_an_unfinished_upload_frees_its_slot(self):
        fid = self.create_livery(ALICE, 600)
        designs.load()["files"][str(fid)]["created"] -= designs.STALE_UPLOAD + 1
        self.assertEqual(self.pages(ALICE, ALICE)[0]["UsedSlots"], 0)
        self.assertNotIn(str(fid), designs.load()["files"])

    def test_daily_limit(self):
        for _ in range(designs.PER_DAY):
            self.assertIsNotNone(self.create_livery(ALICE))
        self.assertIsNone(self.create_group(ALICE))
        self.assertIsNotNone(self.create_livery(BOB))
        d = designs.load()
        d["uploads"][designs.hx(ALICE)] = [t - 86401 for t in d["uploads"][designs.hx(ALICE)]]
        self.assertIsNotNone(self.create_group(ALICE))

    def test_server_hourly_limit(self):
        designs.PER_HOUR, old = 3, designs.PER_HOUR
        try:
            for who in (ALICE, BOB, CAROL):
                self.assertIsNotNone(self.create_livery(who))
            self.assertIsNone(self.create_livery(DAVE))
        finally:
            designs.PER_HOUR = old

    def test_ban_list(self):
        fid, _ = self.share_livery(ALICE)
        path = os.path.join(self.dir.name, "banned_xuids.json")
        with open(path, "w") as f:
            json.dump(["%016X" % ALICE], f)
        os.utime(path, (time.time() + 5, time.time() + 5))  # a new mtime, however fast the test
        self.assertIsNone(self.create_livery(ALICE))
        self.assertIsNotNone(self.create_livery(BOB))
        self.assertEqual(self.featured(CAROL), [])  # a banned player's files are not listed
        self.assertIsNone(self.purchase(CAROL, fid))
        self.assertEqual(self.file_list(ALICE, ALICE, 1)["totalFiles"], 1)
        with open(path, "w") as f:
            f.write("not json")
        os.utime(path, (time.time() + 10, time.time() + 10))
        self.assertEqual([r["Id"] for r in self.featured(CAROL)], [fid])  # no bans

    def test_title_and_description_are_cleaned(self):
        fid, _ = self.share_livery(ALICE, title="\u00e9t\u00e9\x07PAINTJOB", description="a\nb")
        r = self.heavy(BOB, fid)[0]
        self.assertEqual((r["Title"], r["Description"]), ("ete PAINTJOB", "a b"))

    def test_the_index_survives_a_restart(self):
        fid, payload = self.share_livery(ALICE)
        designs.configure(self.dir.name, proven=lsp.caller_signed, trusted=lsp.caller_trusted)
        self.assertEqual(self.purchase(BOB, fid)["payload"], payload)


class BrowseTest(Base):
    def test_featured_most_downloaded_top_rated(self):
        a, _ = self.share_livery(ALICE)
        b, _ = self.share_group(BOB)
        c, _ = self.share_livery(CAROL)
        self.assertEqual([r["Id"] for r in self.featured(DAVE)], [c, b, a])  # newest first
        self.purchase(DAVE, a)
        self.purchase(EVE, a)
        self.purchase(DAVE, b)
        most = self.storefront(DAVE, "GetMostDownloadedDesigns",
                               A.ForzaStorefrontGetMostDownloadedDesigns_Params,
                               {"clientContentFlag": FLAG, "maxFiles": 10},
                               A.ForzaStorefrontGetMostDownloadedDesigns_Results)
        self.assertEqual([r["Id"] for r in most["MostDownloadedDesigns"]], [a, b, c])
        self.by_id(DAVE, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params, {"fileId": c, "rating": 5})
        self.by_id(DAVE, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params, {"fileId": b, "rating": 3})
        top = self.storefront(DAVE, "GetTopRatedDesigns", A.ForzaStorefrontGetTopRatedDesigns_Params,
                              {"clientContentFlag": FLAG, "maxFiles": 10},
                              A.ForzaStorefrontGetTopRatedDesigns_Results)
        self.assertEqual([r["Id"] for r in top["TopRatedDesigns"]], [c, b, a])

    def test_photos_are_not_in_the_design_lists(self):
        fid = photos.create(ALICE, 100, True, "x", "photo", b"\xff\xd8", {"forStorefront": True})
        self.assertLess(fid, designs.FIRST_ID)
        self.assertEqual(self.featured(BOB), [])

    def test_search_wildcards_and_filters(self):
        mine, _ = self.share_livery(ALICE)
        other, _ = self.share_livery(BOB, model=MODEL + 1, keywords=(4, 0), description="stripes")
        self.assertEqual(sorted(self.search_liveries(CAROL)), sorted([mine, other]))
        self.assertEqual(self.search_liveries(CAROL, MakeId=MAKE, ModelId=MODEL), [mine])
        self.assertEqual(self.search_liveries(CAROL, KeywordOneId=4), [other])
        self.assertEqual(self.search_liveries(CAROL, GamerTag="ali"), [mine])
        self.assertEqual(self.search_liveries(CAROL, ManualSearchKeywords="STRIPES"), [other])
        self.assertEqual(self.search_groups(CAROL), [])

    def test_search_by_share_code(self):
        a, _ = self.share_livery(ALICE)
        b, _ = self.share_livery(BOB)
        g, _ = self.share_group(BOB)
        code = self.code(a)
        self.assertRegex(code, "^[%s]{%d}$" % (designs.CODE_CHARS, designs.CODE_LEN))
        self.assertEqual(self.search_liveries(CAROL, GamerTag=code.lower()), [a])
        self.assertEqual(self.search_liveries(CAROL, ManualSearchKeywords=" %s " % code), [a])
        # a code ignores the car filters (it names one file) but not the kind
        self.assertEqual(self.search_liveries(CAROL, ModelId=1, GamerTag=code), [a])
        self.assertEqual(self.search_groups(CAROL, ManualSearchKeywords=self.code(g)), [g])
        self.assertEqual(self.search_groups(CAROL, ManualSearchKeywords=code), [])
        self.assertNotEqual(self.code(a), self.code(b))

    def test_heavy_weight_data_for_designs_and_photos(self):
        d, _ = self.share_livery(ALICE)
        p = photos.create(ALICE, 3, True, "pic", "photo", b"\xff\xd8",
                          {"title": "pic", "description": "", "price": 0, "quantity": 0, "type": 5,
                           "keywordOne": 0, "keywordTwo": 0, "envId": 63, "trackId": 0,
                           "makeId": 0, "modelId": 0, "forStorefront": True, "downloads": 0,
                           "ratings": 0, "ratingSum": 0})
        photos.load()["files"][str(p)]["done"] = True
        self.assertEqual([r["Id"] for r in self.heavy(BOB, d, p, 12345, designs.FIRST_ID + 999)], [d, p])

    def test_photo_pages_still_work(self):
        r = self.file_list(BOB, ALICE, photos.PHOTO_PAGE)
        self.assertEqual(r["totalFiles"], 0)


class PurchaseTest(Base):
    def test_download_and_purchase_are_open_to_all(self):
        fid, payload = self.share_livery(ALICE)
        dl = self.call(OLD, "Xls.WebServices.Content", "DownloadContent", A.ContentDownloadContent_Params,
                       {"contentId": fid, "payloadOffset": 0, "requestedSize": 262144, "userInitiated": False},
                       A.ContentDownloadContent_Results, signed=False)
        self.assertEqual((dl["downloadData"], dl["isLastBuffer"], dl["totalSize"]), (payload, True, len(payload)))
        p = self.purchase(OLD, fid, len(payload), len(cxds()), signed=False)
        self.assertEqual((p["payload"], p["previewPayload"], p["returnCredits"]), (payload, cxds(), 0))
        info = p["purchasedFileInfo"]
        self.assertEqual((info["Title"], info["Ordinal"], info["Type"], info["Owner"]["wzGamerTag"]),
                         ("PAINTJOB1", MODEL, 2, "Alice"))

    def test_purchase_respects_the_sizes_asked_for(self):
        fid, payload = self.share_livery(ALICE)
        self.assertIsNone(self.purchase(BOB, fid, len(payload) - 1))
        self.assertEqual(self.purchase(BOB, fid, len(payload), 10)["previewPayload"], b"")

    def test_one_download_per_player_and_not_the_owner(self):
        fid, _ = self.share_livery(ALICE)
        self.purchase(BOB, fid)
        self.purchase(BOB, fid)
        self.purchase(ALICE, fid)
        self.assertIsNotNone(self.purchase(OLD, fid, signed=False))  # served, not counted
        self.assertEqual(self.heavy(DAVE, fid)[0]["DownloadCount"], 1)

    def test_unknown_or_unfinished_files(self):
        fid = self.create_livery(ALICE, 600)
        self.assertIsNone(self.purchase(BOB, fid))
        self.assertIsNone(self.purchase(BOB, designs.FIRST_ID + 77))
        dl = self.call(BOB, "Xls.WebServices.Content", "DownloadContent", A.ContentDownloadContent_Params,
                       {"contentId": fid, "payloadOffset": 0, "requestedSize": 100, "userInitiated": True},
                       A.ContentDownloadContent_Results)
        self.assertEqual((dl["downloadData"], dl["totalSize"]), (b"", 0))


class OwnerTest(Base):
    def test_rating_once_per_player(self):
        fid, _ = self.share_livery(ALICE)
        rate = lambda who, n, **kw: self.by_id(who, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params,
                                               {"fileId": fid, "rating": n}, **kw)
        rate(BOB, 5)
        rate(BOB, 1)          # replaces Bob's 5
        rate(CAROL, 4)
        rate(ALICE, 5)        # not the owner's own
        rate(DAVE, 9)         # out of range
        rate(OLD, 5, signed=False)  # an unproven caller
        self.assertEqual(self.heavy(BOB, fid)[0]["Rating"], 2.5)

    def test_three_reporters_hide_a_file(self):
        fid, _ = self.share_livery(ALICE)
        report = lambda who, **kw: self.by_id(who, "ReportContent", A.ForzaStorefrontReportContent_Params,
                                              {"fileId": fid}, **kw)
        report(BOB)
        report(BOB)
        report(ALICE)
        report(OLD, signed=False)
        self.assertEqual([r["Id"] for r in self.featured(DAVE)], [fid])
        report(CAROL)
        report(DAVE)
        self.assertEqual(self.featured(EVE), [])
        self.assertEqual(self.search_liveries(EVE, GamerTag=self.code(fid)), [])
        self.assertEqual(self.heavy(EVE, fid), [])
        self.assertIsNone(self.purchase(EVE, fid))
        self.assertEqual(self.file_list(EVE, ALICE, 1)["totalFiles"], 0)
        self.assertEqual(self.file_list(ALICE, ALICE, 1)["totalFiles"], 1)  # still the owner's

    def test_held_files_are_their_owners_only(self):
        designs.hold_new, old = (lambda xuid, kind, meta: designs.HELD), designs.hold_new
        try:
            fid, payload = self.share_livery(ALICE)
        finally:
            designs.hold_new = old
        self.assertEqual(self.featured(BOB), [])
        self.assertIsNone(self.purchase(BOB, fid))
        self.assertEqual(self.file_list(ALICE, ALICE, 1)["Files"][0]["Id"], fid)
        self.assertEqual(self.purchase(ALICE, fid)["payload"], payload)

    def test_only_the_owner_removes(self):
        fid, _ = self.share_livery(ALICE)
        params = {"fileId": fid}
        self.assertIsNone(self.by_id(BOB, "RemoveFile", A.ForzaStorefrontRemoveFile_Params, params))
        self.assertEqual(self.by_id(ALICE, "RemoveFile", A.ForzaStorefrontRemoveFile_Params, params), {})
        self.assertEqual(self.featured(BOB), [])
        self.assertFalse(os.path.exists(designs.file_path(fid, ".bin")))
        self.assertEqual(self.pages(ALICE, ALICE)[0]["UsedSlots"], 0)

    def test_an_unsigned_remove_for_the_owner_is_refused(self):
        fid, _ = self.share_livery(ALICE)
        reply = self.raw_call(ALICE, "Forza.WebServices.Storefront", "RemoveFile",
                              A.ForzaStorefrontRemoveFile_Params, {"fileId": fid}, signed=False)
        self.assertEqual(self.status(reply), (2, 5))
        self.assertEqual(len(self.featured(BOB)), 1)

    def test_change_description_and_price(self):
        fid, _ = self.share_livery(ALICE)
        desc = lambda who, text: self.by_id(who, "ChangeFileDescription",
                                            A.ForzaStorefrontChangeFileDescription_Params,
                                            {"fileId": fid, "newDescription": text})
        self.assertIsNone(desc(BOB, "mine now"))
        self.assertEqual(desc(ALICE, "new\ttext"), {})
        self.assertEqual(self.by_id(ALICE, "ChangeFilePriceQuantity",
                                    A.ForzaStorefrontChangeFilePriceQuantity_Params,
                                    {"fileId": fid, "newPrice": 1000, "newQuantity": 1}), {})
        r = self.heavy(BOB, fid)[0]
        self.assertEqual((r["Description"], r["Price"], r["Quantity"]), ("new text", 0, -1))

    def test_stats(self):
        a, _ = self.share_livery(ALICE)
        self.share_livery(ALICE)
        g, _ = self.share_group(ALICE)
        self.purchase(BOB, a)
        self.purchase(CAROL, a)
        self.purchase(BOB, g)
        stats = self.storefront(BOB, "GetStats", A.ForzaStorefrontGetStats_Params, {"xuid": ALICE},
                                A.ForzaStorefrontGetStats_Results)["stats"]
        self.assertEqual((stats["liveriesUploaded"], stats["liveriesDownloaded"],
                          stats["layerGroupsUploaded"], stats["layerGroupsDownloaded"]), (2, 2, 1, 1))
        self.assertEqual((stats["tuningFilesUploaded"], stats["photosUploaded"]), (0, 0))


class PrivacyTest(Base):
    def test_nothing_over_http(self):
        fid, _ = self.share_livery(ALICE)
        for path in ("/designs", "/designs.json", "/storage/designs/%d.bin" % fid,
                     "/storage/../designs/%d.bin" % fid, "/storage/designs.json"):
            status, _, _ = lsp.handle(None, "GET", path, {}, b"", ("127.0.0.1", 1))
            self.assertEqual(status, 404, path)

    def test_the_index_keeps_no_payload(self):
        fid, payload = self.share_livery(ALICE)
        with open(designs.index_path()) as f:
            text = f.read()
        self.assertNotIn(payload.hex(), text)
        self.assertNotIn(payload[8:40].hex(), text)


if __name__ == "__main__":
    unittest.main()
