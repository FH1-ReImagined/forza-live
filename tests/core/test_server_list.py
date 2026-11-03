"""The signed server list (server_list.py, FH1 #587): what the operator's server_list.json may
hold, this server listed first with its id, the list's own signing key, the signature and its
expiry, re-signing when the file changes, and GET /servers.json through lsp.handle."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from forza_live.core import ed25519, server_list
from forza_live.title_services import lsp

SELF = {"name": "Main server", "address": "your.host:21200", "region": "Europe",
        "contact": "operator@example.org"}
OTHER = {"name": "Example community", "address": "192.0.2.10:21200", "region": "North America",
         "contact": "owner@example.org", "verified": True, "id": "example-1"}
LIST_SEED = bytes([7] * 32)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name
        server_list.configure(self.dir, lambda: "main")
        self.addCleanup(server_list.configure, "forza_live_data", lambda: "")
        self.key_path = os.path.join(self.dir, server_list.KEY_FILE)
        with open(self.key_path, "w") as f:
            f.write(LIST_SEED.hex() + "\n")

    def write(self, data):
        path = os.path.join(self.dir, server_list.LIST_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        # A later write within the same tick of the clock still counts as a change.
        st = os.stat(path)
        bump = getattr(self, "_bump", 0) + 1
        self._bump = bump
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1000000))
        return path

    def published(self):
        status, kind, body, extra = server_list.handle("GET")
        self.assertEqual((status, kind), (200, "application/json"))
        self.assertIn("Cache-Control: max-age=300", extra)
        doc = json.loads(body)
        return doc, server_list.check_document(doc, bytes.fromhex(doc["key"]))


class Entries(Base):
    def test_self_first_verified_with_this_servers_id(self):
        self.write({"servers": [OTHER], "self": SELF})
        entries, problems = server_list.load_entries()
        self.assertEqual(problems, [])
        self.assertEqual([e["address"] for e in entries], ["your.host:21200", "192.0.2.10:21200"])
        self.assertEqual(entries[0], dict(SELF, verified=True, id="main", main=True))
        self.assertEqual(entries[1], OTHER)
        self.assertNotIn("main", entries[1])

    def test_unverified_by_default_and_the_id_optional(self):
        self.write({"servers": [{"name": "LAN party", "address": "192.0.2.20"}]})
        entries, _ = server_list.load_entries()
        self.assertEqual(entries, [{"name": "LAN party", "address": "192.0.2.20", "region": "",
                                    "contact": "", "verified": False}])

    def test_bad_entries_are_left_out_and_named(self):
        bad = [{"address": "192.0.2.11"},                                   # no name
               {"name": "x", "address": "http://192.0.2.12/"},              # not host[:port]
               {"name": "x", "address": "192.0.2.13:65535"},                # no HTTP port after it
               {"name": "x", "address": "192.0.2.14", "verified": "yes"},
               {"name": "x" * 49, "address": "192.0.2.15"},
               {"name": "a\x07b", "address": "192.0.2.16"},
               "not an object",
               dict(OTHER, name="Same address")]
        self.write({"self": SELF, "servers": [OTHER] + bad})
        entries, problems = server_list.load_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(problems), len(bad))
        self.assertIn("servers[1]: name: missing", problems)
        self.assertTrue(any("already listed" in p for p in problems))

    def test_addresses(self):
        for good in ("your.host", "your.host:21200", "192.0.2.1:56677", "[2001:db8::1]:21200",
                     "[2001:db8::1]", "a-b.example.org."):
            self.assertTrue(server_list.valid_address(good), good)
        for bad in ("", "your.host:", "your.host:0", "your host", "user@your.host", "-a.example",
                    "2001:db8::1", "[2001:db8::1]x", "your.host:21200/x", "a..b", "ünï.example"):
            self.assertFalse(server_list.valid_address(bad), bad)

    def test_at_most_max_servers(self):
        many = [{"name": f"s{i}", "address": f"192.0.2.{i % 250}:{20000 + i}"} for i in range(250)]
        self.write({"self": SELF, "servers": many})
        entries, problems = server_list.load_entries()
        self.assertEqual(len(entries), server_list.MAX_SERVERS)
        self.assertTrue(problems[-1].endswith(f"more than {server_list.MAX_SERVERS} servers"))


class Publishing(Base):
    def test_no_file_no_list(self):
        os.remove(self.key_path)
        self.assertEqual(server_list.handle("GET")[0], 404)
        self.assertFalse(os.path.exists(self.key_path))

    def test_get_only(self):
        self.write({"self": SELF})
        self.assertEqual(server_list.handle("POST")[0], 405)

    def test_signed_with_a_key_of_its_own_kept_in_the_data_folder(self):
        os.remove(self.key_path)
        self.write({"self": SELF, "servers": [OTHER]})
        with self.assertLogs("forza-live", "WARNING") as logged:
            doc, servers = self.published()
        self.assertIn("made a new signing key", logged.output[0])
        self.assertIn(doc["key"], logged.output[0])
        self.assertEqual(doc["format"], server_list.FORMAT)
        self.assertEqual([s["name"] for s in servers], ["Main server", "Example community"])
        key_path = self.key_path
        with open(key_path) as f:
            seed = bytes.fromhex(f.read().strip())
        self.assertEqual(doc["key"], ed25519.public_key(seed).hex())
        if os.name == "posix":
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
        server_list.configure(self.dir)  # a restart keeps the key
        self.assertEqual(self.published()[0]["key"], doc["key"])

    def test_the_signature_covers_the_list(self):
        self.write({"self": SELF, "servers": [OTHER]})
        doc, _ = self.published()
        key = bytes.fromhex(doc["key"])
        forged = dict(doc, list=doc["list"].replace("192.0.2.10", "192.0.2.66"))
        with self.assertRaisesRegex(ValueError, "bad signature"):
            server_list.check_document(forged, key)
        with self.assertRaisesRegex(ValueError, "bad signature"):
            server_list.check_document(doc, ed25519.public_key(bytes(32)))
        for broken in ({}, dict(doc, format="other"), dict(doc, signature="zz"), [doc]):
            with self.assertRaises(ValueError):
                server_list.check_document(broken, key)

    def test_a_list_expires(self):
        self.write({"self": SELF})
        doc, _ = self.published()
        data = json.loads(doc["list"])
        self.assertEqual(data["expires"] - data["issued"], server_list.EXPIRES)
        self.assertLessEqual(abs(data["issued"] - time.time()), 5)
        with self.assertRaisesRegex(ValueError, "expired"):
            server_list.check_document(doc, bytes.fromhex(doc["key"]), now=data["expires"] + 1)

    def test_signed_again_when_the_file_changes_and_once_a_day(self):
        self.write({"self": SELF})
        first = server_list.document()
        self.assertIs(server_list.document(), first)  # the same file: the signed copy
        self.write({"self": SELF, "servers": [OTHER]})
        second = server_list.document()
        self.assertEqual(len(json.loads(json.loads(second)["list"])["servers"]), 2)
        later = time.time() + server_list.RESIGN + 1
        with mock.patch("time.time", return_value=later):
            third = json.loads(server_list.document())
        self.assertEqual(json.loads(third["list"])["issued"], int(later))

    def test_a_damaged_file_keeps_the_last_good_list(self):
        self.write({"self": SELF})
        good = server_list.document()
        with self.assertLogs("forza-live", "ERROR"):
            self.write("{not json")
            self.assertEqual(server_list.document(), good)

    def test_a_damaged_key_publishes_nothing(self):
        self.write({"self": SELF})
        with open(self.key_path, "w") as f:
            f.write("not hex\n")
        with self.assertLogs("forza-live", "ERROR"):
            self.assertEqual(server_list.handle("GET")[0], 404)


class Route(Base):
    def test_lsp_answers_servers_json(self):
        self.write({"self": SELF, "servers": [OTHER]})
        answer = lsp.handle(None, "GET", "/servers.json", {}, b"", ("192.0.2.50", 1))
        self.assertEqual(answer[0], 200)
        doc = json.loads(answer[2])
        names = [s["name"] for s in server_list.check_document(doc, bytes.fromhex(doc["key"]))]
        self.assertEqual(names, ["Main server", "Example community"])


class Command(Base):
    def test_checks_the_file_and_prints_the_public_key(self):
        self.write({"self": SELF, "servers": [OTHER, {"name": "x"}]})
        out = io.StringIO()
        with redirect_stdout(out):
            code = server_list.main(["--data-dir", self.dir])
        self.assertEqual(code, 1)  # one entry left out
        text = out.getvalue()
        self.assertIn("listed: Main server (your.host:21200, Europe, verified)", text)
        self.assertIn("left out: servers[1]: address: missing", text)
        self.assertIn("public key: " + ed25519.public_key(LIST_SEED).hex(), text)


if __name__ == "__main__":
    unittest.main()
