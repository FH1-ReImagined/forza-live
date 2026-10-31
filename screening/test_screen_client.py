"""The worker against a real forza-live server (this repository's, started on 127.0.0.1 with
FORZA_COMMUNITY_HOLD=1 and its photos seeded as held), with stand-in models: the queue, release,
hide, the operator deciding first, bad pictures, refused requests. And the server's admin checks
for the address a worker on Docker's default bridge network has."""

import hashlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
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


class LocalServer:
    """forza_live_server.py from this checkout, on 127.0.0.1, with the token and the hold."""

    def __init__(self, pictures, extra_args=()):
        self.data = tempfile.TemporaryDirectory()
        self.ids = seed(self.data.name, pictures)
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
        self.calls = []
        self.on_score = on_score

    def score(self, img, photo=None):
        self.calls.append(photo["id"])
        if self.on_score:
            self.on_score(photo)
        s = json.loads(json.dumps(CLEAN))
        colour = img.getpixel((0, 0))
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
