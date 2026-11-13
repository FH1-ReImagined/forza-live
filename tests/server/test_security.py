"""Abuse tests for the public server: operator endpoints, malformed input, limits."""

from contextlib import ExitStack
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from forza_live.core import limits
from forza_live.title_services import lsp
from forza_live import server as srv
from forza_live.network import xtsw as x
from tests.support import ROOT

MAGIC = b"RLV1"


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        return urllib.request.urlopen(req, timeout=3).status
    except urllib.error.HTTPError as e:
        return e.code


class AdminTest(unittest.TestCase):
    def test_is_admin(self):
        lsp.ADMIN_TOKEN = ""
        self.assertTrue(lsp.is_admin(("127.0.0.1", 1), {}, {}))
        self.assertFalse(lsp.is_admin(("203.0.113.9", 1), {}, {}))
        self.assertFalse(lsp.is_admin(("192.168.1.5", 1), {}, {}))
        self.assertFalse(lsp.is_admin(("127.0.0.1", 1), {"x-forwarded-for": "203.0.113.9"}, {}))
        lsp.ADMIN_TOKEN = "s3cret"
        try:
            self.assertTrue(lsp.is_admin(("203.0.113.9", 1), {"x-admin-token": "s3cret"}, {}))
            self.assertTrue(lsp.is_admin(("203.0.113.9", 1), {}, {"token": ["s3cret"]}))
            self.assertFalse(lsp.is_admin(("203.0.113.9", 1), {"x-admin-token": "nope"}, {}))
        finally:
            lsp.ADMIN_TOKEN = ""


class AdminTokenTest(unittest.TestCase):
    """A wrong operator token used to be a plain 403: not counted, not limited and, for most
    endpoints, not logged, and any text was a token, the examples' placeholders too."""

    LONG = "0f" * 32

    def setUp(self):
        lsp._token_fails.clear()
        self.addCleanup(lsp._token_fails.clear)
        self.addCleanup(setattr, lsp, "ADMIN_TOKEN", lsp.ADMIN_TOKEN)
        self.addCleanup(limits.configure_proxies, "")
        lsp.ADMIN_TOKEN = self.LONG

    def ask(self, token, ip="203.0.113.9", query=False):
        return lsp.is_admin((ip, 40000), {} if query else {"x-admin-token": token},
                            {"token": [token]} if query else {})

    def test_wrong_tokens_are_counted_and_logged(self):
        with self.assertLogs("forza-live", "WARNING") as logs:
            for i in range(lsp.TOKEN_FAILS):
                self.assertFalse(self.ask("guess-%d" % i, query=bool(i % 2)))
        self.assertEqual(len(logs.output), lsp.TOKEN_FAILS)
        self.assertTrue(all("a wrong admin token from 203.0.113.9" in line for line in logs.output))
        self.assertNotIn("guess", "\n".join(logs.output))  # never the token
        self.assertNotIn(self.LONG, "\n".join(logs.output))
        # that address gets nowhere now, with the right token either, and without more log lines
        with self.assertNoLogs("forza-live", "WARNING"):
            self.assertFalse(self.ask(self.LONG))
            self.assertFalse(self.ask("guess-again"))
        # the operator's own addresses are not locked out by it
        self.assertTrue(self.ask(self.LONG, ip="192.168.1.5"))
        self.assertTrue(lsp.is_admin(("127.0.0.1", 1), {}, {}))
        # and the address is taken again once the window has passed
        for times in lsp._token_fails.values():
            for i in range(len(times)):
                times[i] -= lsp.TOKEN_FAIL_WINDOW + 1
        self.assertTrue(self.ask(self.LONG))

    def test_a_few_wrong_tokens_lock_nobody_out(self):
        with self.assertLogs("forza-live", "WARNING"):
            for i in range(lsp.TOKEN_FAILS - 1):
                self.assertFalse(self.ask("typo-%d" % i))
        self.assertTrue(self.ask(self.LONG))

    def test_through_the_tunnel_is_one_address(self):
        """Whoever comes through the tunnel without an address of its own cannot lock out the
        operator's (the LAN, this machine)."""
        limits.configure_proxies("192.0.2.10")
        with self.assertLogs("forza-live", "WARNING") as logs:
            for i in range(lsp.TOKEN_FAILS):
                self.assertFalse(self.ask("guess-%d" % i, ip="192.0.2.10"))
        self.assertIn("from the tunnel", logs.output[0])
        self.assertFalse(self.ask(self.LONG, ip="192.0.2.10"))
        self.assertTrue(self.ask(self.LONG, ip="192.168.1.5"))

    def test_a_placeholder_is_no_token(self):
        for placeholder in ("choose-a-long-random-token", "choose-a-token", "Choose-A-Long-Random-Token "):
            lsp.ADMIN_TOKEN = placeholder
            self.assertEqual(lsp.admin_token(), "")
            self.assertFalse(self.ask(placeholder))
            self.assertFalse(self.ask(placeholder, query=True))
            self.assertTrue(lsp.is_admin(("127.0.0.1", 1), {}, {}))  # this machine needs none
            with self.assertLogs("forza-live", "ERROR") as logs:
                lsp.check_admin_token()
            self.assertIn("placeholder", logs.output[0])
        from forza_live.panel import routes as panel
        self.assertFalse(panel.reachable(("192.168.1.5", 1), {}))
        self.assertFalse(panel.token_ok({"x-admin-token": lsp.ADMIN_TOKEN}))

    def test_a_short_token_is_warned_about(self):
        lsp.ADMIN_TOKEN = "s3cret"
        with self.assertLogs("forza-live", "WARNING") as logs:
            lsp.check_admin_token()
        self.assertIn("only 6 characters", logs.output[0])
        self.assertNotIn("s3cret", logs.output[0])
        self.assertTrue(self.ask("s3cret"))  # it still works
        lsp.ADMIN_TOKEN = self.LONG
        with self.assertNoLogs("forza-live", "WARNING"):
            lsp.check_admin_token()

    def test_a_token_in_the_url_is_not_logged(self):
        self.assertEqual(lsp.loggable("/gift?to=x&token=%s&credits=5" % self.LONG), "/gift?to=x&token=...&credits=5")
        self.assertEqual(lsp.loggable("/world/event?TOKEN=abc"), "/world/event?TOKEN=...")
        self.assertEqual(lsp.loggable("/leaderboards?board=1"), "/leaderboards?board=1")
        with self.assertLogs("forza-live", "INFO") as logs:
            lsp.handle(None, "GET", "/gift?to=nobody&credits=1&token=" + self.LONG, {}, b"", ("203.0.113.9", 1))
        self.assertNotIn(self.LONG, "\n".join(logs.output))


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port, cls.http = free_port(), free_port(socket.SOCK_STREAM)
        cls.data = tempfile.TemporaryDirectory()
        env = dict(os.environ, FORZA_ADMIN_TOKEN="tok123")
        cls.proc = subprocess.Popen(
            # Windows venv's python.exe is a launcher; terminating it leaves its
            # child server alive, now holding events.sqlite3 open at cleanup.
            [getattr(sys, "_base_executable", sys.executable), os.path.join(ROOT, "forza_live_server.py"), "--host", "127.0.0.1",
             "--port", str(cls.port), "--http-port", str(cls.http), "--data-dir", cls.data.name],
            stderr=subprocess.DEVNULL, env=env)
        cls.base = "http://127.0.0.1:%d" % cls.http
        # Ready when the status page answers (the UDP port is bound before the HTTP one).
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(cls.base + "/status", timeout=2).read()
                break
            except (urllib.error.URLError, OSError):
                if cls.proc.poll() is not None or time.monotonic() > deadline:
                    cls.tearDownClass()
                    raise RuntimeError("the server did not start")
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
        cls.data.cleanup()

    def test_admin_endpoints_refuse_proxied_requests(self):
        proxied = {"X-Forwarded-For": "203.0.113.9"}
        for path in ("/gift?to=x&credits=1", "/notify?to=x", "/world/event?session=all",
                     "/admin/scores?state=all"):
            self.assertEqual(get(self.base + path, proxied), 403, path)
            self.assertEqual(get(self.base + path + "&token=wrong", proxied), 403, path)
            self.assertNotEqual(get(self.base + path + "&token=tok123", proxied), 403, path)
        # from this machine (no proxy header) without a token
        self.assertEqual(get(self.base + "/gift?to=nobody&credits=1"), 404)
        self.assertEqual(get(self.base + "/admin/scores?build=none"), 200)

    def test_score_changes_refuse_browser_requests(self):
        def post(path, headers):
            req = urllib.request.Request(self.base + path, data=b"", method="POST", headers=headers)
            try:
                return urllib.request.urlopen(req, timeout=3).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(post("/admin/scores/delete?build=x", {"Origin": "https://example.org"}), 403)
        self.assertEqual(post("/admin/scores/delete?build=x", {}), 200)
        self.assertEqual(post("/admin/scores/delete", {}), 400)  # no filter

    def test_status_page_has_no_addresses(self):
        text = urllib.request.urlopen(self.base + "/status", timeout=3).read().decode()
        self.assertNotRegex(text, r"\d+\.\d+\.\d+\.\d+")

    def test_garbage_udp_and_http(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for kind in range(0, 40):
            for body in (b"", b"\xff", b"\x00" * 3, b"\xff" * 40):
                sock.sendto(MAGIC + bytes([kind]) + body, ("127.0.0.1", self.port))
        sock.close()
        for raw in (b"\xff\xfe\r\n\r\n", b"POST / HTTP/1.1\r\nContent-Length: 99999999999\r\n\r\n",
                    b"POST / HTTP/1.1\r\nContent-Length: -1\r\n\r\n", b"A" * 100000 + b"\r\n\r\n"):
            s = socket.create_connection(("127.0.0.1", self.http), timeout=3)
            s.sendall(raw)
            s.settimeout(2)
            try:
                s.recv(100)
            except (socket.timeout, ConnectionError):
                pass
            s.close()
        self.assertEqual(get(self.base + "/"), 200)

    def test_the_udp_port_outlives_replies_to_closed_ports(self):
        """Answers to a socket closed meanwhile (a game that quit) come back as ICMP port
        unreachable, which on Windows once stopped the server's UDP port for good
        (server.ignore_port_unreachable); test_garbage_udp_and_http closes its socket
        so too. Registers no player (test_welcome_is_small_and_peers_per_ip_capped counts them
        from this address): a HELLO of another protocol version is answered with an ERROR."""
        other = MAGIC + b"\x01" + struct.pack(">HQQIH", 0xFFFF, 7, 0x0009000000000399, 0, 1) + b"\0\2Pn"
        for _ in range(10):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                for _ in range(40):
                    s.sendto(other, ("127.0.0.1", self.port))
        time.sleep(0.5)  # their ERRORs went to closed ports
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            s.sendto(other, ("127.0.0.1", self.port))
            try:
                reply, _ = s.recvfrom(4096)
            except socket.timeout:
                self.fail("the server's UDP port stopped answering")
            self.assertEqual(reply[:5], MAGIC + b"\x03")  # ERROR (protocol version)

    def test_welcome_is_small_and_peers_per_ip_capped(self):
        welcomes = 0
        with ExitStack() as sockets:
            # Keep endpoints alive: closing each socket lets the OS reuse its
            # port, making a later HELLO refresh an existing peer instead of
            # testing the new-player cap.
            clients = []
            for i in range(24):
                s = sockets.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                s.bind(("127.0.0.1", 0))
                s.settimeout(0.5)
                hello = MAGIC + b"\x01" + struct.pack(">HQQIH", 1, 1, 0x0009000000000300 + i, 0, 1) + b"\0\2Pn"
                clients.append((s, hello))
                s.sendto(hello, ("127.0.0.1", self.port))
                try:
                    reply, _ = s.recvfrom(4096)
                    if reply[4] == 2:
                        welcomes += 1
                        self.assertLess(len(reply), 3 * len(hello))
                except socket.timeout:
                    pass
            self.assertEqual(welcomes, 16)

            # Retransmitting an accepted HELLO still gets a WELCOME at the cap;
            # WELCOME count alone is only a player count for distinct endpoints.
            first, hello = clients[0]
            first.sendto(hello, ("127.0.0.1", self.port))
            reply, _ = first.recvfrom(4096)
            self.assertEqual(reply[:5], MAGIC + b"\x02")
            self.assertLess(len(reply), 3 * len(hello))


@unittest.skipUnless(sys.platform == "win32", "Windows' UDP sockets")
class PortUnreachableTest(unittest.TestCase):
    """A datagram to a closed port makes a Windows UDP socket's next receive fail
    (WSAECONNRESET), which stopped the server's UDP port: ignore_port_unreachable."""

    def test_a_closed_port_fails_no_receive(self):
        gone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gone.bind(("127.0.0.1", 0))
        closed = gone.getsockname()
        gone.close()
        for ignore in (False, True):
            with self.subTest(ignore=ignore), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(("127.0.0.1", 0))
                s.settimeout(0.5)
                if ignore:
                    self.assertTrue(srv.ignore_port_unreachable(s))
                s.sendto(b"x", closed)
                with self.assertRaises(socket.timeout if ignore else ConnectionResetError):
                    s.recvfrom(16)


class LimitsTest(unittest.TestCase):
    def test_blob_cap(self):
        with self.assertRaises(ValueError):
            limits.accept(limits.MAX_BLOB + 1)

    def test_array_length_is_bounded(self):
        data = struct.pack(">I", 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            x.decode(x.ARR(x.RAW(4)), data)


class StorageTest(unittest.TestCase):
    """GET /storage/<path> serves DATA_DIR/storage only: DATA_DIR itself holds friends.json."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        os.makedirs(os.path.join(root, "storage", "sub"))
        with open(os.path.join(root, "storage", "sub", "a.txt"), "wb") as f:
            f.write(b"public")
        with open(os.path.join(root, "friends.json"), "wb") as f:
            f.write(b"secret")
        old, lsp.DATA_DIR = lsp.DATA_DIR, root
        self.addCleanup(setattr, lsp, "DATA_DIR", old)

    def test_a_stored_file_is_served(self):
        self.assertEqual(lsp.stored_file("/storage/sub/a.txt"), b"public")
        self.assertEqual(lsp.stored_file("/storage/sub/../sub/a.txt"), b"public")

    def test_nothing_outside_storage_is_served(self):
        for path in ("/storage/../friends.json", "/storage/sub/../../friends.json",
                     "/storage/./../friends.json", "/storage/../../etc/passwd",
                     "/storage/", "/storage/sub", "/friends.json"):
            self.assertIsNone(lsp.stored_file(path), path)

    def test_a_link_out_of_storage_is_not_followed(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        try:
            os.symlink(os.path.join(self.tmp.name, "friends.json"),
                       os.path.join(self.tmp.name, "storage", "link.json"))
        except OSError:
            self.skipTest("cannot create symlinks here")
        self.assertIsNone(lsp.stored_file("/storage/link.json"))


if __name__ == "__main__":
    unittest.main()
