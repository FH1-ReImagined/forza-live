"""The worker against a real forza-live server (this repository's, started on 127.0.0.1 with
FORZA_COMMUNITY_HOLD=1 and its photos seeded as held, its designs as pending), with stand-in
models: the queues, release, hide, hold, the operator deciding first, bad pictures, refused
requests. And the server's admin checks for the address a worker on Docker's default bridge
network has."""

import hashlib
import http.server
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
import zlib
from contextlib import redirect_stdout

import screen_client
import screen_image
import screen_worker
import synth

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOKEN = "test-admin-token-0123456789"
with open(os.path.join(HERE, "screening.toml"), "rb") as f:
    CFG = tomllib.load(f)
CFG["_dir"] = HERE


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def seed(data_dir, pictures):
    """Held community photos in a data folder, as forza_community keeps them: {id: (PNG bytes,
    title)}. Returns the ids."""
    os.makedirs(os.path.join(data_dir, "community"), exist_ok=True)
    photos = {}
    for i, (pid, (png, title)) in enumerate(pictures.items()):
        with open(os.path.join(data_dir, "community", pid + ".png"), "wb") as f:
            f.write(png)
        photos[pid] = {"owner": 0x0009000000000001 + i, "fileId": 100 + i,
                       "gamertag": "Tester%d" % i, "title": title, "type": "png",
                       "takenAt": 1793000000 + i, "created": 1793000000.0 + i,
                       "status": "held", "approvedAt": None, "decidedAt": None, "note": "",
                       "sha256": hashlib.sha256(png).hexdigest(),
                       "w": 1920, "h": 1080, "size": len(png)}
    with open(os.path.join(data_dir, "community.json"), "w") as f:
        json.dump({"photos": photos, "uploads": {}, "serverUploads": [], "deleted": []}, f)
    return list(pictures)


def cxds_of(img):
    """A design preview as the game uploads it (forza_cxds.py: a tiled 8_8_8_8 texture in a cxds
    file) of a Pillow picture, its pixels darkened by the gamma the server takes off again."""
    sys.path.insert(0, ROOT)
    try:
        import forza_cxds
    finally:
        sys.path.remove(ROOT)
    w, h = img.size
    pitch = (w + 31) & ~31
    dark = bytes(round(255 * (v / 255) ** 2.2) for v in range(256))
    pixels = bytearray(pitch * ((h + 31) & ~31) * 4)
    rgb = img.convert("RGB").tobytes().translate(dark)
    for y in range(h):
        for x in range(w):
            o, at = forza_cxds.tiled_offset(x, y, pitch), (y * w + x) * 3
            pixels[o:o + 4] = b"\xff" + rgb[at:at + 3]
    header = bytearray(52)
    header[0x20:0x28] = struct.pack(">II", 0x86, (w - 1) | (h - 1) << 13)
    body = bytes(header) + bytes(pixels)
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = c.compress(body) + c.flush()
    return b"cxds" + struct.pack(">IIII", 2, len(body), len(packed), len(body)) + packed


def seed_designs(data_dir, previews):
    """Pending designs in a data folder, as forza_designs keeps them: {public id: (cxds preview,
    title, description)}; even ones are paint jobs, odd ones vinyl groups. Returns the ids."""
    os.makedirs(os.path.join(data_dir, "designs"), exist_ok=True)
    files = {}
    for i, (wid, (prev, title, desc)) in enumerate(previews.items()):
        fid = 100000000 + i
        livery = i % 2 == 0
        payload = (b"crlv" if livery else b"lvyg") + bytes(60)
        for suffix, data in ((".bin", payload), (".prev", prev)):
            with open(os.path.join(data_dir, "designs", "%d%s" % (fid, suffix)), "wb") as f:
                f.write(data)
        meta = {"title": title, "description": desc, "keywordOne": 0, "keywordTwo": 0}
        meta.update({"makeId": 21, "modelId": 1398, "textureId": 1} if livery else {"numLayers": 3})
        owner = 0x0009000000000101 + i
        os.makedirs(os.path.join(data_dir, "players"), exist_ok=True)
        with open(os.path.join(data_dir, "players", "%016X.json" % owner), "w") as f:
            json.dump({"gamertag": "Designer%d" % i}, f)
        files[str(fid)] = {
            "kind": "livery" if livery else "layergroup", "type": 2 if livery else 1,
            "owner": owner, "gamertag": "Designer%d" % i, "size": len(payload),
            "got": len(payload), "done": True, "created": 1793000000.0 + i, "code": "CODE%02d" % i,
            "status": "live", "meta": meta, "downloaders": [], "ratings": {}, "reporters": [],
            "views": 0, "webId": wid, "web": "pending", "webNote": "", "webAt": None, "png": None}
    with open(os.path.join(data_dir, "designs.json"), "w") as f:
        json.dump({"nextId": 100000000 + len(files), "files": files, "uploads": {},
                   "serverUploads": []}, f)
    return list(previews)


class LocalServer:
    """forza_live_server.py from this checkout, on 127.0.0.1, with the token and the hold."""

    def __init__(self, pictures, extra_args=(), designs=None):
        self.data = tempfile.TemporaryDirectory()
        self.ids = seed(self.data.name, pictures)
        self.design_ids = seed_designs(self.data.name, designs or {})
        self.http = free_port()
        env = dict(os.environ, FORZA_ADMIN_TOKEN=TOKEN, FORZA_COMMUNITY_HOLD="1")
        env.pop("FORZA_TRUSTED_PROXIES", None)
        self.proc = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable),
             os.path.join(ROOT, "forza_live_server.py"), "--host", "127.0.0.1",
             "--port", str(free_port(socket.SOCK_DGRAM)), "--http-port", str(self.http),
             "--data-dir", self.data.name, *extra_args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=ROOT)
        self.base = "http://127.0.0.1:%d" % self.http
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(self.base + "/status", timeout=2).read()
                return
            except (urllib.error.URLError, OSError):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self.close()
                    raise RuntimeError("the server did not start")
                time.sleep(0.05)

    def client(self, token=TOKEN):
        return screen_client.AdminClient(self.base, token, timeout=10)

    def statuses(self):
        doc = json.loads(self.client()._request("GET", "/admin/photos")[0])
        return {p["id"]: (p["status"], p["note"]) for p in doc["photos"]}

    def gallery(self):
        doc = json.loads(urllib.request.urlopen(self.base + "/community/gallery.json").read())
        return [p["id"] for p in doc["photos"]]

    def design_states(self):
        doc = json.loads(self.client()._request("GET", "/admin/designs")[0])
        return {d["id"]: (d["state"], d["note"], d["listed"]) for d in doc["designs"]}

    def site(self):
        doc = json.loads(urllib.request.urlopen(self.base + "/community/designs.json").read())
        return [d["id"] for d in doc["designs"]]

    def close(self):
        self.proc.terminate()
        self.proc.wait()
        self.data.cleanup()


def plain(colour):
    from PIL import Image
    return synth.png(Image.new("RGB", (1920, 1080), colour))


GREEN, RED, BLUE, GREY = (40, 160, 60), (200, 20, 20), (20, 40, 200), (128, 128, 128)
CLEAN = {"nsfw": {"neutral": 0.99, "low": 0.01, "medium": 0.0, "high": 0.0},
         "zeroshot": {"hate": {"sigmoid": 0.01, "region": "full", "prompt": "-"}}, "hits": [],
         "errors": {}}


class StubScreener:
    """Stands in for the models: a red picture is a confident NSFW hit, a blue one has a doubtful
    hate-symbol score, anything else is clear. on_score(photo) runs while it 'thinks'."""

    def __init__(self, on_score=None):
        self.versions = {"stub": "1"}
        self.hidden = {}
        self.hidden_designs = {}
        self.calls = []
        self.grids = {}
        self.on_score = on_score

    def score(self, img, photo=None, grids=None, hidden=None):
        self.calls.append(photo["id"])
        self.grids[photo["id"]] = (grids, img.size)
        if self.on_score:
            self.on_score(photo)
        s = json.loads(json.dumps(CLEAN))
        c = img.getpixel((0, 0))
        colour = min((GREEN, RED, BLUE, GREY), key=lambda k: sum((a - b) ** 2 for a, b in zip(k, c)))
        if colour == RED:
            s["nsfw"]["high"] = 0.97
        elif colour == BLUE:
            s["zeroshot"]["hate"]["sigmoid"] = 0.45
        return s, {"ms": {"stub": 0}}


def run(worker):
    out = io.StringIO()
    with redirect_stdout(out):
        result = worker.run_once()
    return result, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class WorkerAgainstServer(unittest.TestCase):
    IDS = {"AAAAAAAAAAAA": "clean", "BBBBBBBBBBBB": "nsfw", "CCCCCCCCCCCC": "doubtful",
           "DDDDDDDDDDDD": "broken", "EEEEEEEEEEEE": "clean2"}

    def setUp(self):
        broken = plain(GREEN)[:2000]  # a PNG cut short
        self.server = LocalServer({
            "AAAAAAAAAAAA": (plain(GREEN), ""), "BBBBBBBBBBBB": (plain(RED), ""),
            "CCCCCCCCCCCC": (plain(BLUE), ""), "DDDDDDDDDDDD": (broken, ""),
            "EEEEEEEEEEEE": (plain(GREY), "My Photo")})

    def tearDown(self):
        self.server.close()

    def test_queue_has_no_xuid(self):
        q = self.server.client().queue()
        self.assertEqual([p["id"] for p in q], list(self.IDS))  # oldest first
        for p in q:
            self.assertNotIn("owner", p)
            self.assertEqual(p["status"], "held")

    def test_release_hide_and_hold(self):
        stub = StubScreener()
        worker = screen_worker.Worker(CFG, self.server.client(), stub)
        (done, waiting), lines = run(worker)
        self.assertEqual((done, waiting), (5, 0))
        st = self.server.statuses()
        self.assertEqual(st["AAAAAAAAAAAA"][0], "live")
        self.assertEqual(st["EEEEEEEEEEEE"][0], "live")
        self.assertEqual(st["BBBBBBBBBBBB"][0], "hidden")
        self.assertIn("nsfw-high", st["BBBBBBBBBBBB"][1])
        self.assertTrue(st["BBBBBBBBBBBB"][1].startswith("screening: hide"))
        self.assertIn("screening: release (clear)", st["AAAAAAAAAAAA"][1])
        self.assertEqual(st["CCCCCCCCCCCC"], ("held", ""))  # left for the maintainer
        self.assertEqual(st["DDDDDDDDDDDD"], ("held", ""))
        self.assertEqual(sorted(self.server.gallery()), ["AAAAAAAAAAAA", "EEEEEEEEEEEE"])
        by_id = {r["id"]: r for r in lines if r.get("event") == "photo"}
        self.assertEqual(by_id["CCCCCCCCCCCC"]["rule"], "hate-suspect")
        self.assertEqual(by_id["DDDDDDDDDDDD"]["rule"], "error-decode")
        for r in by_id.values():
            for key in ("id", "models", "decision", "rule", "ms"):
                self.assertIn(key, r)
            self.assertNotIn("owner", r)
            self.assertNotIn("00090000", json.dumps(r).upper())  # no XUID anywhere
        # the held ones are not screened again at the next poll
        stub.calls.clear()
        (done, waiting), _ = run(worker)
        self.assertEqual((done, waiting, stub.calls), (0, 0, []))

    def test_operator_decides_first(self):
        operator = self.server.client()

        def hide_first(photo):
            if photo["id"] == "AAAAAAAAAAAA":
                operator.act("AAAAAAAAAAAA", "hide", "operator: no")

        worker = screen_worker.Worker(CFG, self.server.client(), StubScreener(hide_first))
        _, lines = run(worker)
        rec = next(r for r in lines if r.get("id") == "AAAAAAAAAAAA")
        self.assertEqual((rec["decision"], rec["done"]), ("release", "operator-decided-first"))
        self.assertEqual(self.server.statuses()["AAAAAAAAAAAA"], ("hidden", "operator: no"))
        self.assertNotIn("AAAAAAAAAAAA", self.server.gallery())
        # the API refuses the release whoever asks: a hidden photo is the operator's
        with self.assertRaises(screen_client.AdminError) as e:
            self.server.client().act("AAAAAAAAAAAA", "release")
        self.assertEqual(e.exception.status, 409)

    def test_dry_run_changes_nothing(self):
        worker = screen_worker.Worker(CFG, self.server.client(), StubScreener(), dry_run=True)
        _, lines = run(worker)
        self.assertEqual({s for s, _ in self.server.statuses().values()}, {"held"})
        self.assertIn("dry-run", {r.get("done") for r in lines})

    def test_proxy_from_environment_is_not_used(self):
        old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        os.environ.update(http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        try:
            self.assertEqual(len(self.server.client().queue()), 5)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_token_is_never_printed(self):
        client = screen_client.AdminClient("http://127.0.0.1:%d" % free_port(), TOKEN, timeout=2)
        self.assertNotIn(TOKEN, repr(client))
        worker = screen_worker.Worker(CFG, client, StubScreener())
        out, err = io.StringIO(), io.StringIO()
        import logging
        handler = logging.StreamHandler(err)
        screen_worker.log.addHandler(handler)
        try:
            with redirect_stdout(out):
                with self.assertRaises(screen_client.AdminError) as e:
                    worker.run_once()
        finally:
            screen_worker.log.removeHandler(handler)
        self.assertNotIn(TOKEN, str(e.exception) + out.getvalue() + err.getvalue())


def preview(colour, size=(288, 160)):
    from PIL import Image
    return cxds_of(Image.new("RGB", size, colour))


class StandIn:
    """Stand-in models for the real Screener (no onnxruntime): clean scores, no text read; the
    zero-shot regions it was given are kept."""

    def __init__(self):
        self.regions = []

    def nsfw(self, img):
        return {"neutral": 0.99, "low": 0.01, "medium": 0.0, "high": 0.0}

    def embed(self, img, grids):
        names = ["full"] + ["tile%d" % i for g in grids for i in range(g[0] * g[1])]
        self.regions.append((img.size, names))
        return names, None

    def score(self, names, vectors, categories=None):
        return {c: {"sigmoid": 0.01, "region": "full", "prompt": "-"}
                for c in ("hate", "gore", "nudity")}

    def ocr(self, img):
        return []


class DesignsAgainstServer(unittest.TestCase):
    """The design storefront's pending designs: the same decisions, and the worker moves a
    doubtful one to held itself (the photos' queue holds by default; designs are in the game
    already and wait only for the website)."""
    IDS = {"AAAAAAAAAAAA": "clean livery", "BBBBBBBBBBBB": "nsfw group", "CCCCCCCCCCCC": "doubtful livery",
           "DDDDDDDDDDDD": "clean group", "EEEEEEEEEEEE": "word in the description"}

    def setUp(self):
        self.server = LocalServer(
            {"PPPPPPPPPPPP": (plain(GREEN), "")},
            designs={"AAAAAAAAAAAA": (preview(GREEN), "FLAMES", "red and gold"),
                     "BBBBBBBBBBBB": (preview(RED, (256, 128)), "STAR", ""),
                     "CCCCCCCCCCCC": (preview(BLUE), "BLUE", ""),
                     "DDDDDDDDDDDD": (preview(GREY, (256, 128)), "GREY", ""),
                     "EEEEEEEEEEEE": (preview(GREEN), "NICE", "look zqxtestword here")})

    def tearDown(self):
        self.server.close()

    def test_the_designs_queue(self):
        designs, pending = self.server.client().design_queue()
        self.assertEqual(([d["id"] for d in designs], pending), (list(self.IDS), 5))
        for d in designs:
            self.assertNotIn("owner", d)
            self.assertEqual((d["kind"], d["status"]), ("design", "pending"))
        self.assertEqual(designs[0]["type"], "livery")
        self.assertEqual(designs[1]["type"], "vinylgroup")
        self.assertEqual(designs[4]["description"], "look zqxtestword here")

    def test_release_hide_and_hold(self):
        stub = StubScreener()
        worker = screen_worker.Worker(CFG, self.server.client(), stub)
        (done, waiting), lines = run(worker)
        self.assertEqual((done, waiting), (6, 0))  # the photo and the five designs
        st = self.server.design_states()
        self.assertEqual(st["AAAAAAAAAAAA"][:2], ("clear", "screening: release (clear)"))
        self.assertEqual(st["DDDDDDDDDDDD"][0], "clear")
        self.assertEqual(st["BBBBBBBBBBBB"][0], "hidden")
        self.assertTrue(st["BBBBBBBBBBBB"][1].startswith("screening: hide (nsfw-high"))
        self.assertFalse(st["BBBBBBBBBBBB"][2])  # hidden in the game too
        self.assertEqual(st["CCCCCCCCCCCC"][0], "held")  # moved to held, with why
        self.assertTrue(st["CCCCCCCCCCCC"][1].startswith("screening: hold (hate-suspect"))
        self.assertTrue(st["CCCCCCCCCCCC"][2])  # still in the game
        self.assertEqual(sorted(self.server.site()), ["AAAAAAAAAAAA", "DDDDDDDDDDDD", "EEEEEEEEEEEE"])
        by_id = {r["id"]: r for r in lines if r.get("event") == "design"}
        self.assertEqual(set(by_id), set(self.IDS))
        self.assertEqual((by_id["AAAAAAAAAAAA"]["type"], by_id["BBBBBBBBBBBB"]["type"]),
                         ("livery", "vinylgroup"))
        self.assertEqual(by_id["CCCCCCCCCCCC"]["done"], "hold")
        for r in by_id.values():
            for key in ("id", "type", "models", "decision", "rule", "ms", "size"):
                self.assertIn(key, r)
            self.assertNotIn("owner", r)
            self.assertNotIn("00090000", json.dumps(r).upper())  # no XUID anywhere
        # a design's preview is scored whole, at its own size
        self.assertEqual(stub.grids["AAAAAAAAAAAA"], ([], (288, 160)))
        self.assertEqual(stub.grids["BBBBBBBBBBBB"], ([], (256, 128)))
        self.assertEqual(stub.grids["PPPPPPPPPPPP"], (None, (1920, 1080)))  # photos as before
        stub.calls.clear()
        (done, waiting), _ = run(worker)
        self.assertEqual((done, waiting, stub.calls), (0, 0, []))

    def test_the_description_and_title_are_read(self):
        cfg = dict(CFG, _dir=HERE)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        terms = os.path.join(tmp.name, "terms.txt")
        with open(os.path.join(HERE, "wordlists", "terms.txt"), encoding="utf-8") as f:
            text = f.read()
        with open(terms, "w", encoding="utf-8") as f:
            f.write(text + "\n[hide]\nzqxtestword\n")
        cfg["text"] = dict(CFG["text"], terms=terms, ldnoobw=os.path.join(tmp.name, "none"))
        cfg["runtime"] = dict(CFG["runtime"], models_dir=tmp.name)
        m = StandIn()
        screener = screen_worker.Screener(cfg, models={"nsfw": m.nsfw, "siglip": m, "ocr": m.ocr})
        worker = screen_worker.Worker(cfg, self.server.client(), screener)
        _, lines = run(worker)
        rec = next(r for r in lines if r.get("id") == "EEEEEEEEEEEE")
        self.assertEqual((rec["decision"], rec["rule"]), ("hide", "text-hide"))
        self.assertEqual(rec["hits"][0]["source"], "description")
        st = self.server.design_states()
        self.assertEqual(st["EEEEEEEEEEEE"][0], "hidden")
        self.assertIn("'zqxtestword' in the description", st["EEEEEEEEEEEE"][1])
        self.assertEqual(st["AAAAAAAAAAAA"][0], "clear")
        # the zero-shot model saw the whole frame only for designs, and the tiles for the photo
        self.assertIn(((288, 160), ["full"]), m.regions)
        self.assertIn(((1920, 1080), ["full"] + ["tile%d" % i for i in range(6)]), m.regions)

    def test_the_operator_decides_first(self):
        operator = self.server.client()

        def hold_first(item):
            if item["id"] == "AAAAAAAAAAAA":
                operator._request("POST", "/admin/designs/AAAAAAAAAAAA/hold",
                                  {"note": "operator: looking"})

        worker = screen_worker.Worker(CFG, self.server.client(), StubScreener(hold_first))
        _, lines = run(worker)
        rec = next(r for r in lines if r.get("id") == "AAAAAAAAAAAA")
        self.assertEqual((rec["decision"], rec["done"]), ("release", "operator-decided-first"))
        self.assertEqual(self.server.design_states()["AAAAAAAAAAAA"][:2], ("held", "operator: looking"))
        self.assertNotIn("AAAAAAAAAAAA", self.server.site())
        # nothing releases a design the operator hid
        operator._request("POST", "/admin/designs/DDDDDDDDDDDD/hide")
        with self.assertRaises(screen_client.AdminError) as e:
            operator._request("POST", "/admin/designs/DDDDDDDDDDDD/release")
        self.assertEqual(e.exception.status, 409)

    def test_dry_run_changes_nothing(self):
        stub = StubScreener()
        worker = screen_worker.Worker(CFG, self.server.client(), stub, dry_run=True)
        _, lines = run(worker)
        self.assertEqual({s for s, _, _ in self.server.design_states().values()}, {"pending"})
        self.assertIn("dry-run", {r.get("done") for r in lines if r.get("event") == "design"})
        stub.calls.clear()
        (done, _), _ = run(worker)  # what it would hold is not scored again
        self.assertNotIn("CCCCCCCCCCCC", stub.calls)

    def test_designs_can_be_turned_off(self):
        cfg = dict(CFG, designs={"enabled": False})
        worker = screen_worker.Worker(cfg, self.server.client(), StubScreener())
        (done, _), lines = run(worker)
        self.assertEqual(done, 1)
        self.assertEqual({s for s, _, _ in self.server.design_states().values()}, {"pending"})


class OlderServer(unittest.TestCase):
    def test_a_server_without_designs_still_gets_its_photos_screened(self):
        server = LocalServer({"AAAAAAAAAAAA": (plain(GREEN), "")})
        try:
            client = server.client()
            real = client._request

            def no_designs(method, path, *a, **kw):
                if path.startswith("/admin/designs"):
                    raise screen_client.AdminError(404, "not found")
                return real(method, path, *a, **kw)
            client._request = no_designs
            worker = screen_worker.Worker(CFG, client, StubScreener())
            (done, waiting), _ = run(worker)
            self.assertEqual((done, waiting), (1, 0))
            self.assertEqual(server.statuses()["AAAAAAAAAAAA"][0], "live")
            self.assertGreater(worker._no_designs_until, time.monotonic())
        finally:
            server.close()


class TrustedProxyServer(unittest.TestCase):
    # (A wrong token cannot be tested from this machine: its loopback is admin without one
    # unless it is a trusted proxy. BridgeAddress checks the token for other addresses.)
    def test_worker_at_a_trusted_proxy_address_is_refused(self):
        # what happens with --network host on the public server: 127.0.0.1 is a trusted proxy
        server = LocalServer({"AAAAAAAAAAAA": (plain(GREEN), "")},
                             ["--trusted-proxy", "127.0.0.0/8",
                              "--trusted-proxy", "192.168.100.105"])
        try:
            with self.assertRaises(screen_client.AdminError) as e:
                server.client().queue()
            self.assertEqual(e.exception.status, 403)
            with self.assertRaises(screen_client.AdminError) as e:
                server.client().design_queue()
            self.assertEqual(e.exception.status, 403)
        finally:
            server.close()


class Redirects(unittest.TestCase):
    def test_not_followed(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("X-Admin-Token")))
                if self.path.startswith("/admin"):
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            client = screen_client.AdminClient("http://127.0.0.1:%d" % httpd.server_port, TOKEN)
            with self.assertRaises(screen_client.AdminError):
                client.queue()
            self.assertEqual([p for p, _ in seen], ["/admin/photos/queue"])
        finally:
            httpd.shutdown()
            httpd.server_close()


class BridgeAddress(unittest.TestCase):
    """The worker runs on Docker's default bridge network and reaches the server (host network)
    at the bridge's gateway, 172.17.0.1: the server sees the container's address, 172.17.0.x.
    With the public server's trusted proxies that address is admin with the token; the
    loopback and the tunnel agent's address are not."""

    def setUp(self):
        sys.path.insert(0, ROOT)
        import forza_limits
        import forza_live_lsp
        self.limits, self.lsp = forza_limits, forza_live_lsp
        self.old_token = forza_live_lsp.ADMIN_TOKEN
        forza_live_lsp.ADMIN_TOKEN = TOKEN
        forza_limits.configure_proxies("127.0.0.0/8", "192.168.100.105")

    def tearDown(self):
        self.lsp.ADMIN_TOKEN = self.old_token
        self.limits.configure_proxies()
        sys.path.remove(ROOT)

    def test_is_local_admin(self):
        ok = {"x-admin-token": TOKEN, "accept": "application/json", "user-agent": "forza-screening"}
        admin = self.lsp.is_local_admin
        self.assertTrue(admin(("172.17.0.2", 41000), ok))
        self.assertFalse(admin(("172.17.0.2", 41000), dict(ok, **{"x-admin-token": "no"})))
        self.assertFalse(admin(("172.17.0.2", 41000), {}))
        self.assertFalse(admin(("127.0.0.1", 41000), ok))       # --network host: trusted proxy
        self.assertFalse(admin(("192.168.100.105", 41000), ok))  # the tunnel agent
        for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
            self.assertFalse(admin(("172.17.0.2", 41000), dict(ok, **{header: "1.2.3.4"})))

    def test_origin_is_refused(self):
        import forza_community
        answer = forza_community.handle("GET", "/admin/photos/queue",
                                        {"origin": "http://x", "x-admin-token": TOKEN}, b"",
                                        lambda: True)
        self.assertEqual(answer[0], 403)

    def test_the_client_sends_no_proxy_or_origin_header(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update({k.lower(): v for k, v in self.headers.items()})
                body = b'{"photos": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            screen_client.AdminClient("http://127.0.0.1:%d" % httpd.server_port, TOKEN).queue()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(seen.get("x-admin-token"), TOKEN)
        self.assertTrue(self.lsp.is_local_admin(("172.17.0.2", 1), seen))
        for h in ("origin", "x-forwarded-for", "x-real-ip", "forwarded"):
            self.assertNotIn(h, seen)


class Pictures(unittest.TestCase):
    def test_decode_refuses_bad_input(self):
        from PIL import Image
        gif = io.BytesIO()
        Image.new("RGB", (1920, 1080)).save(gif, "GIF")
        tiny = synth.png(Image.new("RGB", (100, 100)))
        for name, data in {"empty": b"", "junk": b"\x89PNG" + bytes(100), "gif": gif.getvalue(),
                           "tiny": tiny, "cut": plain(GREEN)[:3000], "none": None}.items():
            with self.subTest(name):
                self.assertRaises(screen_image.BadPicture, screen_image.decode, data)

    def test_decode_refuses_a_bomb(self):
        import struct
        import zlib

        def chunk(kind, body):
            crc = struct.pack(">I", zlib.crc32(kind + body))
            return struct.pack(">I", len(body)) + kind + body + crc
        header = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)  # 3.6 gigapixels
        bomb = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" * 1000)) + chunk(b"IEND", b""))
        self.assertRaises(screen_image.BadPicture, screen_image.decode, bomb)

    def test_decode_scales_down(self):
        from PIL import Image
        img = screen_image.decode(synth.png(Image.new("RGB", (3840, 2160), GREEN)))
        self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(img.mode, "RGB")

    def test_phash(self):
        a = synth.car(synth.scene(), "88")
        b = synth.car(synth.scene(), "18")
        c = synth.selftest_star()
        ha, hb, hc = (screen_image.phash(x) for x in (a, b, c))
        self.assertLessEqual(screen_image.distance(ha, hb), 6)  # the same scene, a number changed
        self.assertGreater(screen_image.distance(ha, hc), 16)
        self.assertEqual(screen_image.distance(ha, ha), 0)


class Telemetry(unittest.TestCase):
    def test_onnxruntime_telemetry_is_off(self):
        # onnxruntime's Linux wheels upload telemetry to Microsoft unless this is set before the
        # library loads: the worker sets it, and so does the image for both stages
        self.assertEqual(os.environ.get("ORT_DISABLE_TELEMETRY"), "1")
        with open(os.path.join(HERE, "Dockerfile")) as f:
            self.assertEqual(f.read().count("ORT_DISABLE_TELEMETRY=1"), 2)


if __name__ == "__main__":
    unittest.main()
