"""The designs on the website (forza_design_feed.py) and their previews (forza_cxds.py): cxds to
PNG, the web states and what they do in the game, the public feed and images, the operator's
routes and their rules, car names. Every preview here is drawn by this file (make_cxds): no game
data.

Run: python3 -m unittest discover -p 'test_*.py'
"""

import argparse
import asyncio
import functools
import hashlib
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

import forza_auth as auth
import forza_cxds as cxds
import forza_design_feed as feed
import forza_designs as designs
import forza_ed25519 as ed25519
import forza_friends as friends
import forza_limits as limits
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_xtsw as x
import forza_xtsw_all as A
from test_forza_community import decode_png

ALICE, BOB, CAROL, DAVE, EVE = (0x0009000000000A01, 0x0009000000000B02, 0x0009000000000C03,
                               0x0009000000000D04, 0x0009000000000E05)
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", DAVE: "Dave", EVE: "Eve"}
SEEDS = {xuid: bytes([n + 1]) * 32 for n, xuid in enumerate(NAMES)}
NONAME = 0x0009000000000F06  # signed, but no gamertag on record
SEEDS[NONAME] = bytes([9]) * 32
TITLE = "/Services.Horizon/o.xtsw"
TOKEN = "designs-admin-test"
LAN = ("192.168.1.20", 50000)
MAKE, MODEL = 21, 1398
CAR = "2012 Lamborghini Aventador LP700-4"
FLAG = {"High": 0x800000000, "Low": 1}
ANY = {"PriceMax": -1, "MakeId": -1, "ModelId": -1, "ClassId": -1, "KeywordOneId": -1,
       "KeywordTwoId": -1, "ManualSearchKeywords": "", "EnvironmentId": -1, "TrackId": -1,
       "NumLayers": -1, "GamerTag": ""}
FEED_FIELDS = {"id", "type", "code", "title", "description", "gamertag", "car", "layers",
               "downloads", "rating", "createdAt", "preview", "w", "h", "sha256"}
# The game repository's tools/xdc_decode.py, the decoder forza_cxds ports: beside this folder
# when it is that repository's server/ submodule, or where FORZA_REPO_TOOLS says (the CI checks
# it out).
TOOLS = os.environ.get("FORZA_REPO_TOOLS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools")
HAVE_XDC = os.path.isfile(os.path.join(TOOLS, "xdc_decode.py"))


# ---- synthetic previews ----

def colour(x, y):
    """A picture where every pixel is different: where it lands shows in its colour."""
    return (x * 7 & 255, y * 11 & 255, (x + y) * 3 & 255)


@functools.lru_cache(maxsize=16)
def make_cxds(width=288, height=160, pixel=colour, fmt=0x86, version=2, dims=None,
              extra=b"", cut=0):
    """A cxds texture as the game writes a design's preview: the 52-byte header (the format and
    size words at 0x20), then pixel(x, y) as A, R, G, B at its tiled offset; raw-deflated behind
    the cxds header. dims, extra, cut: the size word, bytes after the texture, bytes cut off the
    pixels, to make bad ones."""
    pitch = (width + 31) & ~31
    pixels = bytearray(pitch * ((height + 31) & ~31) * 4)
    for y in range(height):
        for xx in range(width):
            o = cxds.tiled_offset(xx, y, pitch)
            assert o + 4 <= len(pixels)
            pixels[o:o + 4] = bytes((255,) + pixel(xx, y))
    header = bytearray(52)
    struct.pack_into(">II", header, 0x20, fmt,
                     dims if dims is not None else (width - 1) | (height - 1) << 13)
    body = bytes(header) + bytes(pixels[:len(pixels) - cut])
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = c.compress(body) + c.flush() + extra
    return b"cxds" + struct.pack(">IIII", version, len(body), len(packed), len(body)) + packed


def solid(rgb):
    return lambda x, y: rgb


def livery_payload(n=600):
    return b"crlv" + bytes([3, 0, 0, 1]) + bytes(range(256))[:n - 8] + bytes(max(0, n - 264))


def group_payload(n=134):
    return b"lvyg" + bytes(n - 4)


class CxdsTest(unittest.TestCase):
    def test_pixels_come_out_where_they_belong_with_the_gamma(self):
        for w, h in ((288, 160), (256, 128), (40, 24)):
            width, height, rgb = cxds.decode(make_cxds(w, h))
            self.assertEqual((width, height), (w, h))
            for (px, py) in ((0, 0), (1, 0), (31, 0), (32, 5), (w - 1, h - 1), (17, h // 2)):
                at = (py * w + px) * 3
                self.assertEqual(rgb[at:at + 3], bytes(colour(px, py)).translate(cxds.LUT))

    def test_the_tiled_order_is_xdc_decodes(self):
        # SHA-256 of the offsets of every pixel, row by row, as tools/xdc_decode.py computes them
        golden = {(288, 160): "51ed94df0249bdfd66b35e2a85bf77e4d400fe0c839141bbba768e7d11b99210",
                  (256, 128): "ab09426894f0d1b2061c04e761559da9f954e91464fea9522c763159b83af41a",
                  (40, 24): "75cb2bc8939d8b432dac94a7c66d0791e50e1c902b8d923133cd8e69838677fa"}
        for (w, h), want in golden.items():
            pitch = (w + 31) & ~31
            offs = [cxds.tiled_offset(px, py, pitch) for py in range(h) for px in range(w)]
            self.assertEqual(hashlib.sha256(struct.pack(">%dI" % len(offs), *offs)).hexdigest(), want)

    @unittest.skipUnless(HAVE_XDC, "the game repository's tools/xdc_decode.py is not here (set FORZA_REPO_TOOLS)")
    def test_the_same_pixels_as_xdc_decode_with_the_gamma(self):
        sys.path.insert(0, TOOLS)
        try:
            import xdc_decode
        finally:
            sys.path.remove(TOOLS)
        for w, h in ((288, 160), (256, 128)):
            data = make_cxds(w, h)
            xw, xh, rgba = xdc_decode.decode(data)
            raw = bytearray(xw * xh * 3)
            raw[0::3], raw[1::3], raw[2::3] = rgba[0::4], rgba[1::4], rgba[2::4]
            self.assertEqual(cxds.decode(data), (xw, xh, bytes(raw).translate(cxds.LUT)))

    def test_gamma(self):
        self.assertEqual((cxds.LUT[0], cxds.LUT[255]), (0, 255))
        self.assertEqual(cxds.LUT[128], 186)  # 255 * (128/255) ** (1/2.2), rounded
        self.assertEqual(cxds.LUT[20], 80)    # the dark pixels as stored come up
        self.assertEqual(list(cxds.LUT), sorted(cxds.LUT))

    def test_png_holds_ihdr_idat_iend_only_and_the_pixels(self):
        png, w, h = cxds.to_png(make_cxds(256, 128))
        dw, dh, rows = decode_png(png)  # asserts the three chunks, 8-bit RGB
        self.assertEqual((dw, dh, w, h), (256, 128, 256, 128))
        self.assertEqual(b"".join(rows), cxds.decode(make_cxds(256, 128))[2])
        self.assertNotIn(b"cxds", png)

    def test_one_pixel(self):
        width, height, rgb = cxds.decode(make_cxds(1, 1, solid((10, 20, 30))))
        self.assertEqual((width, height, rgb), (1, 1, bytes((10, 20, 30)).translate(cxds.LUT)))

    def test_anything_malformed_is_refused(self):
        good = make_cxds(64, 32)
        body = zlib.decompressobj(-15).decompress(good[20:])

        def pack(body, version=2, size=None, again=None, packed=None, tail=b""):
            c = zlib.compressobj(9, zlib.DEFLATED, -15)
            data = c.compress(body) + c.flush() + tail
            return b"cxds" + struct.pack(">IIII", version, len(body) if size is None else size,
                                         len(data) if packed is None else packed,
                                         len(body) if again is None else again) + data
        bad = {
            "not bytes": "cxds",
            "empty": b"",
            "short": good[:19],
            "magic": b"xdsc" + good[4:],
            "version": pack(body, version=1),
            "packed size": pack(body, packed=len(good) - 19),
            "cut": good[:-10],
            "trailing bytes": pack(body, tail=b"\x00\x00"),
            "trailing texture": make_cxds(64, 32, extra=b"junk"),
            "size claims more": pack(body, size=len(body) + 4, again=len(body) + 4),
            "size claims less": pack(body, size=len(body) - 4, again=len(body) - 4),
            "sizes differ": pack(body, again=len(body) + 1),
            "header only": pack(body[:52]),
            "no header": pack(body[:40]),
            "damaged": good[:30] + bytes(20) + good[50:],
            "format": make_cxds(64, 32, fmt=0x52),
            "not tiled": make_cxds(64, 32, fmt=0x06),
            "too wide": make_cxds(64, 32, dims=(2047) | (31 << 13)),
            "too tall": make_cxds(64, 32, dims=(63) | (1100 << 13)),
            "pixels missing": make_cxds(64, 32, cut=4),
            "a size the pixels do not fill": make_cxds(64, 32, dims=(127) | (31 << 13)),
            "too big a file": good + bytes(cxds.MAX_PACKED),
        }
        for name, data in bad.items():
            with self.subTest(name):
                self.assertRaises(cxds.BadPreview, cxds.to_png, data)

    def test_a_bomb_is_not_inflated(self):
        texture = bytes(52) + bytes(64 << 20)
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = c.compress(texture) + c.flush()
        bomb = b"cxds" + struct.pack(">IIII", 2, len(texture), len(data), len(texture)) + data
        self.assertRaises(cxds.BadPreview, cxds.decode, bomb)
        small = b"cxds" + struct.pack(">IIII", 2, 4096, len(data), 4096) + data  # lies about it
        self.assertRaises(cxds.BadPreview, cxds.decode, small)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        parser = argparse.ArgumentParser()
        lsp.add_arguments(parser)
        lsp.configure(parser.parse_args(["--data-dir", self.dir.name,  # the real wiring
                                         "--server-name", "forza.dyotson.dev"]))
        auth._nonces.clear()
        for xuid, name in NAMES.items():
            lsp.save_player(xuid, {"gamertag": name})
        store = friends.Store()
        for xuid, seed in SEEDS.items():
            store.get(xuid, create=True)["pub"] = ed25519.public_key(seed).hex()
        self.server = SimpleNamespace(friends=SimpleNamespace(store=store))
        self.token = mock.patch.object(lsp, "ADMIN_TOKEN", TOKEN)
        self.token.start()

    def tearDown(self):
        self.token.stop()
        limits.configure_proxies()
        lsp.DATA_DIR = self.old_dir
        self.dir.cleanup()

    # the game's calls

    def call(self, xuid, cls, method, ptype, params, rtype=None, signed=True):
        headers = {"x-classname": "%s, Forza.WebServices" % cls, "x-method": method,
                   "x-userid": "0x%x" % xuid}
        body = bytes(x.encode(ptype, params))
        if signed:
            headers[auth.HEADER] = auth.auth_header(SEEDS[xuid], "forza.dyotson.dev", xuid, "POST",
                                                    TITLE, body, headers)
        reply = lsp.service_call(headers, body, self.server, "POST", TITLE)
        if reply[:4] != bytes(4):
            return None
        return x.decode(rtype, reply, 12)[0] if rtype else {}

    def share(self, xuid, kind="livery", preview=None, model=MODEL, title="PAINTJOB1",
              description="flames", layers=3):
        """A design shared from the game (Create, then the payload): its file id."""
        header = {"Title": title, "Description": description, "Quantity": -1,
                  "SubmissionTime": "2026-10-30T17:58:18.547Z", "Type": 2 if kind == "livery" else 1}
        if kind == "livery":
            payload = livery_payload()
            params = {"fileHeader": header, "payloadSize": len(payload),
                      "liveryDesc": {"base": {"MakeId": MAKE, "ModelId": model, "TextureId": 1,
                                              "ForStorefrontUse": True}},
                      "previewData": make_cxds() if preview is None else preview}
            cls, ptype, rtype = ("Forza.WebServices.StorefrontLivery", A.ForzaStorefrontLiveryCreate_Params,
                                 A.ForzaStorefrontLiveryCreate_Results)
        else:
            payload = group_payload()
            params = {"fileHeader": header, "payloadSize": len(payload),
                      "layerGroupDesc": {"base": {"NumLayers": layers, "ForStorefrontUse": True}},
                      "previewData": make_cxds(256, 128) if preview is None else preview}
            cls, ptype, rtype = ("Forza.WebServices.StorefrontLayerGroup",
                                 A.ForzaStorefrontLayerGroupCreate_Params,
                                 A.ForzaStorefrontLayerGroupCreate_Results)
        fid = self.call(xuid, cls, "Create", ptype, params, rtype)["contentFileId"]
        got = self.call(xuid, "Xls.WebServices.Content", "UploadContent", A.ContentUploadContent_Params,
                        {"contentId": fid, "payloadOffset": 0, "payloadTotalSize": len(payload),
                         "uploadData": payload}, A.ContentUploadContent_Results)
        self.assertEqual(got["payloadUploadedSize"], len(payload))
        return fid

    def storefront(self, xuid, method, ptype, params, rtype=None, signed=True):
        return self.call(xuid, "Forza.WebServices.Storefront", method, ptype, params, rtype, signed)

    def featured(self, viewer):
        return [r["Id"] for r in self.storefront(
            viewer, "GetFeaturedDesigns", A.ForzaStorefrontGetFeaturedDesigns_Params,
            {"clientContentFlag": FLAG, "maxFiles": 10},
            A.ForzaStorefrontGetFeaturedDesigns_Results)["FeaturedFiles"]]

    def search_code(self, viewer, fid):
        return [r["Id"] for r in self.storefront(
            viewer, "SearchLiveryFiles", A.ForzaStorefrontSearchLiveryFiles_Params,
            {"searchParams": dict(ANY, GamerTag=self.rec(fid)["code"]), "clientContentFlag": FLAG,
             "maxFiles": 100}, A.ForzaStorefrontSearchLiveryFiles_Results)["searchResults"]]

    def heavy(self, viewer, fid):
        return [r["Id"] for r in self.storefront(
            viewer, "GetFileHeavyWeightData", A.ForzaStorefrontGetFileHeavyWeightData_Params,
            {"fileCount": 1, "FileIds": [fid]},
            A.ForzaStorefrontGetFileHeavyWeightData_Results)["fileData"]]

    def purchase(self, viewer, fid):
        return self.storefront(viewer, "Purchase", A.ForzaStorefrontPurchase_Params,
                               {"fileId": fid, "price": 0, "maxPayloadSize": 1 << 20,
                                "maxPreviewSize": 153600}, A.ForzaStorefrontPurchase_Results)

    def own_files(self, viewer, owner, page=1):
        return [r["Id"] for r in self.storefront(
            viewer, "GetFileListForPage", A.ForzaStorefrontGetFileListForPage_Params,
            {"ownerXuid": owner, "startAt": 0, "maxFiles": 25, "page": page},
            A.ForzaStorefrontGetFileListForPage_Results)["Files"]]

    def in_game(self, fid, viewer=EVE):
        """Whether another player finds the design in the game, by every route."""
        seen = [fid in self.featured(viewer), fid in self.search_code(viewer, fid),
                self.heavy(viewer, fid) == [fid], self.purchase(viewer, fid) is not None]
        self.assertEqual(len(set(seen)), 1, seen)
        return seen[0]

    def report(self, who, fid):
        self.storefront(who, "ReportContent", A.ForzaStorefrontReportContent_Params, {"fileId": fid})

    # HTTP

    def http(self, method, path, headers=None, body=b"", peer=LAN):
        answer = lsp.handle(self.server, method, path, {k.lower(): v for k, v in (headers or {}).items()},
                            body, peer)
        status, ctype, data = answer[:3]
        extra = dict(line.split(": ", 1) for line in (answer[3] if len(answer) > 3 else ()))
        return status, ctype, data, extra

    def site(self):
        status, ctype, data, extra = self.http("GET", "/community/designs.json")
        self.assertEqual((status, ctype), (200, "application/json"))
        return json.loads(data)

    def listed(self):
        return [d["id"] for d in self.site()["designs"]]

    def admin(self, method, path, body=None, **kw):
        headers = {"X-Admin-Token": TOKEN}
        headers.update(kw.pop("headers", {}))
        raw = json.dumps(body).encode() if body is not None else b""
        status, _, data, _ = self.http(method, "/admin/designs" + path, headers, raw, **kw)
        return status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    def queue(self):
        status, doc = self.admin("GET", "/queue")
        self.assertEqual(status, 200)
        return doc

    def rec(self, fid):
        return designs.load()["files"][str(fid)]

    def wid(self, fid):
        return self.rec(fid)["webId"]

    def release(self, fid, **body):
        return self.admin("POST", "/%s/release" % self.wid(fid), body or None)


class StateTest(Base):
    def test_new_uploads_start_pending_and_stay_in_the_game(self):
        fid = self.share(ALICE)
        r = self.rec(fid)
        self.assertEqual(r["web"], "pending")
        self.assertRegex(r["webId"], r"^[A-Z2-7]{12}$")
        self.assertTrue(self.in_game(fid))  # designs go live in the game at once, as before
        self.assertEqual(self.listed(), [])
        q = self.queue()
        self.assertEqual(([d["id"] for d in q["designs"]], q["pending"]), ([r["webId"]], 1))
        self.assertEqual(q["designs"][0]["preview"], "/admin/designs/img/%s.png" % r["webId"])

    def test_release_puts_it_on_the_site(self):
        fid = self.share(ALICE)
        status, doc = self.release(fid, note="screening: release (clear)")
        self.assertEqual((status, doc["state"], doc["note"]), (200, "clear", "screening: release (clear)"))
        self.assertEqual(self.listed(), [self.wid(fid)])
        self.assertEqual(self.queue()["designs"], [])
        self.assertTrue(self.in_game(fid))

    def test_hold_keeps_it_off_the_site_but_in_the_game(self):
        fid = self.share(ALICE)
        self.assertEqual(self.admin("POST", "/%s/hold" % self.wid(fid), {"note": "screening: hold"})[0], 200)
        self.assertEqual((self.rec(fid)["web"], self.listed()), ("held", []))
        self.assertTrue(self.in_game(fid))
        self.assertEqual(self.queue()["designs"], [])
        status, doc = self.admin("GET", "?state=held")
        self.assertEqual([d["id"] for d in doc["designs"]], [self.wid(fid)])
        self.assertEqual(self.release(fid)[0], 200)  # the operator looked and lets it through
        self.assertEqual(self.listed(), [self.wid(fid)])

    def test_hide_hides_it_in_the_game_too(self):
        fid = self.share(ALICE)
        other = self.share(BOB, title="OTHER")
        self.release(other)
        self.assertEqual(self.admin("POST", "/%s/hide" % self.wid(fid), {"note": "screening: hide (nsfw-high)"})[0], 200)
        self.assertFalse(self.in_game(fid, EVE))
        self.assertFalse(self.in_game(fid, BOB))
        self.assertEqual(self.own_files(ALICE, ALICE), [fid])  # its owner still has it, as after reports
        self.assertEqual(self.own_files(EVE, ALICE), [])
        self.assertTrue(self.in_game(other))
        self.assertEqual(self.listed(), [self.wid(other)])
        self.assertEqual(self.http("GET", "/community/design-img/%s.png" % self.wid(fid))[0], 404)
        # a hidden design is the operator's: no release, whoever asks
        status, doc = self.release(fid)
        self.assertEqual(status, 409)
        self.assertEqual(self.rec(fid)["web"], "hidden")
        # unhide: back in the game and on the site
        self.assertEqual(self.admin("POST", "/%s/unhide" % self.wid(fid))[0], 200)
        self.assertTrue(self.in_game(fid))
        self.assertEqual(sorted(self.listed()), sorted([self.wid(fid), self.wid(other)]))

    def test_the_web_state_survives_a_restart(self):
        fid = self.share(ALICE)
        self.admin("POST", "/%s/hide" % self.wid(fid))
        wid = self.wid(fid)
        designs.configure(self.dir.name, proven=lsp.caller_signed, trusted=lsp.caller_trusted)
        feed.configure()
        self.assertEqual((self.rec(fid)["web"], self.wid(fid)), ("hidden", wid))
        self.assertFalse(self.in_game(fid))

    def test_existing_designs_start_pending(self):
        fid = self.share(ALICE)
        f = self.rec(fid)
        for key in ("webId", "web", "webNote", "webAt", "png"):
            f.pop(key)
        designs.save()
        designs.configure(self.dir.name, proven=lsp.caller_signed, trusted=lsp.caller_trusted)
        feed.configure()
        with open(designs.index_path()) as src:  # written back at once, not only in memory
            self.assertEqual(json.load(src)["files"][str(fid)]["web"], "pending")
        r = self.rec(fid)
        self.assertEqual(r["web"], "pending")
        self.assertRegex(r["webId"], r"^[A-Z2-7]{12}$")
        self.assertEqual([d["id"] for d in self.queue()["designs"]], [r["webId"]])
        self.assertTrue(self.in_game(fid))

    def test_actions_and_their_states(self):
        fid = self.share(ALICE)
        wid = self.wid(fid)
        steps = [("unhide", 409, "pending"), ("hold", 200, "held"), ("hold", 409, "held"),
                 ("hide", 200, "hidden"), ("hide", 409, "hidden"), ("release", 409, "hidden"),
                 ("hold", 409, "hidden"), ("unhide", 200, "clear"), ("release", 409, "clear"),
                 ("hold", 200, "held"), ("release", 200, "clear"), ("frobnicate", 404, "clear")]
        for action, status, state in steps:
            with self.subTest(action):
                self.assertEqual(self.admin("POST", "/%s/%s" % (wid, action))[0], status)
                self.assertEqual(self.rec(fid)["web"], state)

    def test_expect_keeps_the_worker_from_overriding_the_operator(self):
        fid = self.share(ALICE)
        self.admin("POST", "/%s/hold" % self.wid(fid), {"note": "operator: looking"})
        status, doc = self.release(fid, note="screening: release (clear)", expect="pending")
        self.assertEqual(status, 409)
        self.assertEqual(self.rec(fid)["web"], "held")
        self.assertEqual(self.rec(fid)["webNote"], "operator: looking")
        self.assertEqual(self.admin("POST", "/%s/hide" % self.wid(fid), {"expect": "pending"})[0], 409)

    def test_reports_hide_it_from_the_site_and_unhide_lists_it_again(self):
        fid = self.share(ALICE)
        self.release(fid)
        for who in (BOB, CAROL, DAVE):
            self.report(who, fid)
        self.assertFalse(self.in_game(fid))
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.http("GET", "/community/design-img/%s.png" % self.wid(fid))[0], 404)
        self.assertEqual(self.release(fid)[0], 409)  # already clear: the reports keep it hidden
        self.assertEqual(self.admin("POST", "/%s/unhide" % self.wid(fid))[0], 200)
        self.assertEqual((self.rec(fid)["status"], self.rec(fid)["reporters"]), ("live", []))
        self.assertTrue(self.in_game(fid))
        self.assertEqual(self.listed(), [self.wid(fid)])

    def test_delete(self):
        fid = self.share(ALICE)
        self.release(fid)
        wid = self.wid(fid)
        status, doc = self.admin("POST", "/%s/delete" % wid)
        self.assertEqual((status, doc), (200, {"deleted": wid}))
        self.assertNotIn(str(fid), designs.load()["files"])
        for suffix in (".bin", ".prev", ".png"):
            self.assertFalse(os.path.exists(designs.file_path(fid, suffix)))
        self.assertIsNone(self.purchase(EVE, fid))
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.admin("POST", "/%s/delete" % wid)[0], 404)

    def test_ban(self):
        fid = self.share(ALICE)
        self.release(fid)
        status, doc = self.admin("POST", "/%s/ban" % self.wid(fid))
        self.assertEqual((status, doc["banned"]), (200, True))
        self.assertEqual(self.listed(), [])
        self.assertFalse(self.in_game(fid))
        self.assertEqual(self.http("GET", "/community/design-img/%s.png" % self.wid(fid))[0], 404)

    def test_a_preview_that_does_not_convert_is_held(self):
        fid = self.share(ALICE, preview=make_cxds(64, 32, fmt=0x52))  # a cxds, but not 8_8_8_8
        self.assertTrue(self.in_game(fid))  # the game takes it as before
        self.assertEqual(self.queue(), {"designs": [], "pending": 0})
        r = self.rec(fid)
        self.assertEqual(r["web"], "held")
        self.assertIn("preview: format 0x52", r["webNote"])
        self.assertEqual(self.release(fid)[0], 200)  # an operator's release
        self.assertEqual(self.listed(), [])  # still nothing to show
        self.assertEqual(self.admin("GET", "/img/%s.png" % self.wid(fid))[0], 404)

    def test_unfinished_uploads_are_not_queued(self):
        r = self.call(ALICE, "Forza.WebServices.StorefrontLivery", "Create",
                      A.ForzaStorefrontLiveryCreate_Params,
                      {"fileHeader": {"Title": "HALF", "Type": 2, "SubmissionTime": "2026-10-30T17:58:18.547Z"},
                       "liveryDesc": {"base": {"MakeId": MAKE, "ModelId": MODEL}},
                       "payloadSize": 600, "previewData": make_cxds()},
                      A.ForzaStorefrontLiveryCreate_Results)
        self.assertEqual(self.rec(r["contentFileId"])["web"], "pending")
        self.assertEqual(self.queue(), {"designs": [], "pending": 0})

    def test_the_queue_is_oldest_first_and_capped(self):
        fids = [self.share(ALICE, title="D%d" % i) for i in range(4)]
        with mock.patch.object(feed, "QUEUE_MAX", 3):
            q = self.queue()
        self.assertEqual([d["id"] for d in q["designs"]], [self.wid(f) for f in fids[:3]])
        self.assertEqual(q["pending"], 4)


class FeedTest(Base):
    def write_names(self, cars):
        with open(os.path.join(self.dir.name, "leaderboard_names.json"), "w") as f:
            json.dump({"boards": {}, "cars": cars}, f)

    def test_the_feed_has_exactly_the_contract(self):
        self.write_names({str(MODEL): CAR})
        livery = self.share(ALICE, title="FLAMES", description="red and gold")
        group = self.share(BOB, kind="layergroup", title="STAR", description="a star", layers=7)
        for fid in (livery, group):
            self.release(fid)
        self.storefront(CAROL, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params,
                        {"fileId": livery, "rating": 4})
        self.storefront(DAVE, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params,
                        {"fileId": livery, "rating": 5})
        self.purchase(CAROL, livery)
        doc = self.site()
        self.assertEqual(set(doc), {"version", "generated", "designs"})
        self.assertEqual(doc["version"], 1)
        self.assertLessEqual(abs(doc["generated"] - time.time()), 5)
        g, li = doc["designs"]  # newest first
        for d in (g, li):
            self.assertEqual(set(d), FEED_FIELDS)
        self.assertEqual(li, {
            "id": self.wid(livery), "type": "livery", "code": self.rec(livery)["code"],
            "title": "FLAMES", "description": "red and gold", "gamertag": "Alice",
            "car": {"name": CAR}, "layers": None, "downloads": 1,
            "rating": {"avg": 4.5, "count": 2}, "createdAt": int(self.rec(livery)["created"]),
            "preview": "/community/design-img/%s.png" % self.wid(livery), "w": 288, "h": 160,
            "sha256": li["sha256"]})
        self.assertEqual((g["type"], g["car"], g["layers"], g["gamertag"], g["w"], g["h"]),
                         ("vinylgroup", None, 7, "Bob", 256, 128))
        self.assertEqual(g["rating"], {"avg": 0, "count": 0})
        for d in (g, li):  # the image is the one the feed describes
            status, ctype, png, extra = self.http("GET", d["preview"])
            self.assertEqual((status, ctype), (200, "image/png"))
            self.assertEqual(hashlib.sha256(png).hexdigest(), d["sha256"])
            self.assertEqual(decode_png(png)[:2], (d["w"], d["h"]))

    def test_no_xuid_owner_or_payload_anywhere(self):
        fids = [self.share(ALICE), self.share(BOB, kind="layergroup")]
        for fid in fids:
            self.release(fid)
        status, ctype, body, extra = self.http("GET", "/community/designs.json")
        text = body.decode()
        for xuid in (ALICE, BOB):
            for form in ("%016X" % xuid, "%016x" % xuid, "%X" % xuid, str(xuid)):
                self.assertNotIn(form, text)
        for word in ("owner", "xuid", "Xuid", "payload", "crlv", "lvyg", "reporters", "downloaders",
                     "fileId", "webNote"):
            self.assertNotIn(word, text)
        for fid in fids:  # the public id is not the storefront's file id
            self.assertNotIn(str(fid), text)
            self.assertNotIn(livery_payload().hex()[:32], text)
            with open(designs.file_path(fid, ".bin"), "rb") as src:
                self.assertNotIn(src.read()[8:40].hex(), text)
        for d in json.loads(body)["designs"]:
            _, _, png, _ = self.http("GET", d["preview"])
            self.assertNotIn(b"crlv", png)
            self.assertNotIn(struct.pack(">Q", ALICE), png)

    def test_only_live_and_clear_designs_newest_first_and_capped(self):
        fids = [self.share(ALICE, title="D%d" % i) for i in range(5)]
        for fid in fids[:4]:
            self.release(fid)
        self.admin("POST", "/%s/hide" % self.wid(fids[0]))
        self.assertEqual(self.listed(), [self.wid(f) for f in reversed(fids[1:4])])
        with mock.patch.object(feed, "FEED_MAX", 2):
            feed.configure()
            self.assertEqual(self.listed(), [self.wid(f) for f in (fids[3], fids[2])])

    def test_the_websites_rules(self):
        # what the website's build checks before it takes the feed
        import re
        self.write_names({str(MODEL): CAR})
        fids = [self.share(ALICE), self.share(BOB, kind="layergroup"), self.share(CAROL, model=7),
                self.share(DAVE, kind="layergroup", layers=0)]
        for fid in fids:
            self.release(fid)
        for who, stars in ((BOB, 5), (CAROL, 1), (DAVE, 4)):
            self.storefront(who, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params,
                            {"fileId": fids[0], "rating": stars})
        designs_ = self.site()["designs"]
        self.assertEqual(len(designs_), 4)
        codes = [d["code"].lower() for d in designs_]
        self.assertEqual(len(set(codes)), len(codes))
        for d in designs_:
            self.assertRegex(d["id"], r"^[A-Za-z0-9]{6,32}$")
            self.assertRegex(d["code"], r"^[A-Za-z0-9]{4,15}$")
            self.assertIs(type(d["createdAt"]), int)
            self.assertLess(abs(d["createdAt"] - time.time()), 60)  # seconds, not ms
            self.assertTrue(0 <= d["rating"]["avg"] <= 5)
            self.assertIs(type(d["rating"]["count"]), int)
            self.assertIs(type(d["downloads"]), int)
            self.assertIn(d["type"], ("livery", "vinylgroup"))
            if d["type"] == "vinylgroup":
                self.assertIsNone(d["car"])
            self.assertTrue(d["preview"].startswith("/community/design-img/"))  # the feed's host
            status, ctype, png, _ = self.http("GET", d["preview"])
            self.assertEqual((status, ctype, png[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
            self.assertLessEqual(len(png), 1 << 20)
            self.assertTrue(32 <= d["w"] <= 1024 and 32 <= d["h"] <= 1024)

    def test_previews_the_website_would_refuse_are_held(self):
        small = self.share(ALICE, preview=make_cxds(31, 64))
        self.queue()
        self.assertEqual(self.rec(small)["web"], "held")
        self.assertIn("the website takes 32 to 1024 pixels a side", self.rec(small)["webNote"])
        big = self.share(BOB)
        with mock.patch.object(feed, "SITE_MAX_PNG", 1000):
            self.queue()
            self.assertEqual(self.rec(big)["web"], "held")
            self.assertIn("1000 at most", self.rec(big)["webNote"])
            for fid in (small, big):
                self.assertTrue(self.in_game(fid))  # the game is not affected
                self.release(fid)
            self.assertEqual(self.listed(), [])  # an operator's release cannot list them either

    def test_no_gamertag_on_record_no_listing(self):
        fid = self.share(NONAME)
        self.release(fid)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.http("GET", "/community/design-img/%s.png" % self.wid(fid))[0], 404)

    def test_car_names(self):
        known = self.share(ALICE)
        unknown = self.share(BOB, model=4242)
        group = self.share(CAROL, kind="layergroup")
        for fid in (known, unknown, group):
            self.release(fid)
        cars = lambda: {d["id"]: d["car"] for d in self.site()["designs"]}
        self.assertEqual(set(cars().values()), {None})  # no names file: no names
        self.write_names({str(MODEL): CAR, "4243": "2010 Other Car"})
        feed.configure()
        self.assertEqual(cars(), {self.wid(known): {"name": CAR}, self.wid(unknown): None,
                                  self.wid(group): None})
        self.write_names({str(MODEL): "  1999 Évil\x07 <b>Car</b>\nNew  "})
        os.utime(os.path.join(self.dir.name, "leaderboard_names.json"), (1, 1))  # a new mtime
        feed.configure()
        self.assertEqual(cars()[self.wid(known)], {"name": "1999 Evil <b>Car</b> New"})
        with open(os.path.join(self.dir.name, "leaderboard_names.json"), "w") as f:
            f.write('{"cars": ["not", "a", "map"]}')
        os.utime(os.path.join(self.dir.name, "leaderboard_names.json"), (2, 2))
        feed.configure()
        self.assertEqual(set(cars().values()), {None})

    def test_feed_headers_etag_and_cache(self):
        fid = self.share(ALICE)
        self.release(fid)
        status, ctype, body, extra = self.http("GET", "/community/designs.json")
        self.assertEqual(extra["Cache-Control"], "public, max-age=60")
        self.assertEqual(extra["X-Content-Type-Options"], "nosniff")
        self.assertEqual(extra["Content-Security-Policy"], "default-src 'none'")
        etag = extra["ETag"]
        status, _, body2, extra2 = self.http("GET", "/community/designs.json", {"If-None-Match": etag})
        self.assertEqual((status, body2, extra2["ETag"]), (304, b"", etag))
        with mock.patch.object(time, "time", return_value=time.time() + 3600):
            self.assertEqual(self.http("GET", "/community/designs.json")[3]["ETag"], etag)  # unchanged
        self.storefront(BOB, "SubmitFileRating", A.ForzaStorefrontSubmitFileRating_Params,
                        {"fileId": fid, "rating": 3})
        self.assertNotEqual(self.http("GET", "/community/designs.json")[3]["ETag"], etag)
        self.assertEqual(self.http("POST", "/community/designs.json")[0], 405)

    def test_image_route_headers_and_budget(self):
        fid = self.share(ALICE)
        self.release(fid)
        path = "/community/design-img/%s.png" % self.wid(fid)
        status, ctype, png, extra = self.http("GET", path)
        self.assertEqual((status, ctype), (200, "image/png"))
        self.assertEqual(extra["X-Content-Type-Options"], "nosniff")
        self.assertEqual(extra["Content-Security-Policy"], "default-src 'none'")
        self.assertEqual(extra["Cache-Control"], "public, max-age=86400")
        self.assertEqual(self.http("GET", path, {"If-None-Match": extra["ETag"]})[0], 304)
        for bad in ("/community/design-img/%s.jpg" % self.wid(fid),
                    "/community/design-img/%s.png" % self.wid(fid).lower(),
                    "/community/design-img/AAAAAAAAAAAA.png", "/community/design-img/",
                    "/community/design-img/../designs.json", "/community/design-img/%d.png" % fid):
            self.assertEqual(self.http("GET", bad)[0], 404, bad)
        feed.configure()
        with mock.patch.object(feed, "IMAGES_PER_MINUTE", 2):
            self.assertEqual([self.http("GET", path)[0] for _ in range(3)], [200, 200, 429])
        # the photos' own budget is not the designs'
        self.assertEqual(self.http("GET", "/community/gallery.json")[0], 200)

    def test_pending_and_held_designs_have_no_public_image(self):
        fid = self.share(ALICE)
        path = "/community/design-img/%s.png" % self.wid(fid)
        self.assertEqual(self.http("GET", path)[0], 404)
        self.queue()  # converted now, still pending
        self.assertTrue(os.path.exists(designs.file_path(fid, ".png")))
        self.assertEqual(self.http("GET", path)[0], 404)
        self.admin("POST", "/%s/hold" % self.wid(fid))
        self.assertEqual(self.http("GET", path)[0], 404)
        status, ctype, png, extra = self.http("GET", "/admin/designs/img/%s.png" % self.wid(fid),
                                              {"X-Admin-Token": TOKEN})
        self.assertEqual((status, ctype, extra["Cache-Control"]), (200, "image/png", "no-store"))

    def test_the_png_is_kept_beside_the_design_and_made_again_when_lost(self):
        fid = self.share(ALICE)
        self.release(fid)
        sha = self.site()["designs"][0]["sha256"]
        os.remove(designs.file_path(fid, ".png"))
        _, _, png, _ = self.http("GET", "/community/design-img/%s.png" % self.wid(fid))
        self.assertEqual(hashlib.sha256(png).hexdigest(), sha)

    def test_the_owner_removing_it_takes_it_off_the_site(self):
        fid = self.share(ALICE)
        self.release(fid)
        self.storefront(ALICE, "RemoveFile", A.ForzaStorefrontRemoveFile_Params, {"fileId": fid})
        self.assertEqual(self.listed(), [])
        self.assertFalse(os.path.exists(designs.file_path(fid, ".png")))

    def test_photos_still_have_their_routes(self):
        self.assertEqual(self.http("GET", "/community/gallery.json")[0], 200)
        self.assertEqual(self.http("GET", "/admin/photos", {"X-Admin-Token": TOKEN})[0], 200)


class AdminTest(Base):
    def setUp(self):
        super().setUp()
        self.fid = self.share(ALICE)

    def status(self, headers, peer=LAN, path="/queue", method="GET"):
        return self.http(method, "/admin/designs" + path, headers, b"", peer)[0]

    def test_the_queue_has_no_xuid(self):
        status, ctype, body, _ = self.http("GET", "/admin/designs/queue", {"X-Admin-Token": TOKEN})
        self.assertEqual((status, ctype), (200, "application/json"))
        text = body.decode()
        self.assertNotIn("owner", text)
        self.assertNotIn("%016X" % ALICE, text)
        self.assertNotIn(str(self.fid), text)
        d, = json.loads(body)["designs"]
        self.assertEqual(set(d), {"id", "type", "code", "title", "description", "gamertag", "state",
                                  "note", "decidedAt", "createdAt", "w", "h", "sha256", "preview"})
        self.assertEqual((d["gamertag"], d["state"], d["w"], d["h"]), ("Alice", "pending", 288, 160))

    def test_the_full_list_is_the_operators(self):
        status, doc = self.admin("GET", "")
        d, = doc["designs"]
        self.assertEqual((d["owner"], d["fileId"], d["listed"], d["state"]),
                         ("%016X" % ALICE, self.fid, True, "pending"))
        self.assertEqual(self.admin("GET", "?state=bogus")[0], 400)

    def test_the_token_in_its_header_only(self):
        self.assertEqual(self.status({}), 403)
        self.assertEqual(self.status({"X-Admin-Token": "wrong"}), 403)
        self.assertEqual(self.status({}, path="/queue?token=" + TOKEN), 403)
        self.assertEqual(self.status({"X-Admin-Token": TOKEN}), 200)
        wid = designs.load()["files"][str(self.fid)]["webId"]
        self.assertEqual(self.status({}, path="/%s/hide?token=%s" % (wid, TOKEN), method="POST"), 403)
        self.assertEqual(self.status({}, path="/img/%s.png" % wid), 403)
        self.assertEqual(self.rec(self.fid)["web"], "pending")

    def test_this_machine_without_a_token(self):
        self.assertEqual(self.status({}, peer=("127.0.0.1", 40000)), 200)

    def test_never_through_the_tunnel_or_a_proxy(self):
        token = {"X-Admin-Token": TOKEN}
        for proxy in ({"X-Forwarded-For": "203.0.113.9"}, {"X-Real-IP": "203.0.113.9"},
                      {"Forwarded": "for=203.0.113.9"}):
            self.assertEqual(self.status(dict(token, **proxy)), 403)
            self.assertEqual(self.status(dict(proxy), peer=("127.0.0.1", 40000)), 403)
        limits.configure_proxies("127.0.0.1")  # the tunnel agent on this machine
        self.assertEqual(self.status(token, peer=("127.0.0.1", 40000)), 403)
        self.assertEqual(self.status({}, peer=("127.0.0.1", 40000)), 403)
        self.assertEqual(self.status(token), 200)  # the LAN still works
        self.assertEqual(self.http("GET", "/community/designs.json", {}, b"", ("127.0.0.1", 1))[0], 200)

    def test_browser_requests_are_refused(self):
        wid = self.wid(self.fid)
        self.assertEqual(self.status({"X-Admin-Token": TOKEN, "Origin": "http://192.168.1.20:21201"}), 403)
        self.assertEqual(self.status({"Origin": "https://evil.example"}, peer=("127.0.0.1", 1),
                                     path="/%s/delete" % wid, method="POST"), 403)
        self.assertIn(str(self.fid), designs.load()["files"])

    def test_bad_requests(self):
        wid = self.wid(self.fid)
        self.assertEqual(self.http("POST", "/admin/designs/%s/hide" % wid, {"X-Admin-Token": TOKEN},
                                   b"not json")[0], 400)
        self.assertEqual(self.admin("POST", "/AAAAAAAAAAAA/hide")[0], 404)
        self.assertEqual(self.admin("POST", "/%d/hide" % self.fid)[0], 404)
        self.assertEqual(self.admin("PUT", "/%s/hide" % wid)[0], 405)
        self.assertEqual(self.admin("GET", "/img/%s.jpg" % wid)[0], 404)
        self.assertEqual(self.rec(self.fid)["web"], "pending")


class HttpServerTest(Base, unittest.IsolatedAsyncioTestCase):
    """The routes through the server's HTTP handler: the extra headers reach the wire, and
    /admin/designs is not taken by the other /admin/ endpoints (whose rules differ)."""

    async def request(self, path, headers=()):
        server = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                            "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n%s\r\n"
                          % (path, "".join(h + "\r\n" for h in headers))).encode())
            raw = await reader.read()
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
        head, body = raw.split(b"\r\n\r\n", 1)
        return head.decode(), body

    async def test_headers_on_the_wire(self):
        fid = self.share(ALICE)
        self.release(fid)
        head, body = await self.request("/community/design-img/%s.png" % self.wid(fid))
        self.assertIn("HTTP/1.1 200", head)
        self.assertIn("Content-Type: image/png", head)
        self.assertIn("X-Content-Type-Options: nosniff", head)
        self.assertIn("Content-Security-Policy: default-src 'none'", head)
        head, body = await self.request("/community/designs.json")
        self.assertIn("ETag: ", head)
        self.assertEqual(json.loads(body)["designs"][0]["id"], self.wid(fid))
        head, _ = await self.request("/admin/designs/queue?token=" + TOKEN, ["X-Forwarded-For: 203.0.113.9"])
        self.assertIn("HTTP/1.1 403", head)
        head, _ = await self.request("/admin/designs/queue", ["X-Forwarded-For: 203.0.113.9",
                                                              "X-Admin-Token: " + TOKEN])
        self.assertIn("HTTP/1.1 403", head)
        head, _ = await self.request("/admin/designs/queue")  # this machine, no proxy: allowed
        self.assertIn("HTTP/1.1 200", head)


if __name__ == "__main__":
    unittest.main()
