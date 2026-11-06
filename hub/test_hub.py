"""The hub's checks and its worker (hub_check.py, hub_worker.py), on zips made here, with a
stand-in for clamscan that finds the EICAR test string (the real one is tested in the image,
README.md). With FORZA_GAME_REPO set to a checkout of the game's repository, the zips the worker
writes are also added with the launcher's own Add mod and Add pack code.

Refused: zip slip (.., absolute and drive paths, backslashes), links, special files, encryption,
other compressions, zip bombs (the ratio, the whole, one file, the entry count, sizes the zip
lies about), nested archives, programs and scripts by name and by what they start with, names
the game cannot use, names that differ only in case, a file that is also a folder, files outside
the allow-list, textures the game would not load, broken manifests, anything that is not a zip.
Passed: mods and packs at the zip's top or in its one folder, and the clean zip: only the checked
files, in one folder, fixed times, no comment, no extra fields, plain permissions.

  python3 -m unittest discover -s hub -p 'test_*.py' -v
"""

import hashlib
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hub_check  # noqa: E402
import hub_worker  # noqa: E402

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def dds(w=64, h=64, fourcc=b"DXT1", short=False):
    head = bytearray(128)
    head[0:4] = b"DDS "
    struct.pack_into("<IIII", head, 4, 124, 0x1007, h, w)
    struct.pack_into("<I4s", head, 80, 0x4, fourcc)
    body = (w // 4) * (h // 4) * (8 if fourcc in (b"DXT1", b"ATI1") else 16)
    return bytes(head) + b"\x11" * (body - (1 if short else 0))


def png(w=8, h=8):
    import zlib as z
    raw = b"".join(b"\x00" + b"\x80\x40\x20\xff" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", z.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", z.compress(raw)) + chunk(b"IEND", b""))


def make_zip(entries, compression=zipfile.ZIP_DEFLATED):
    """entries: {name: bytes} or [(name, bytes) or ZipInfo-making tuples]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for item in (entries.items() if isinstance(entries, dict) else entries):
            if isinstance(item[0], zipfile.ZipInfo):
                z.writestr(item[0], item[1])
            else:
                z.writestr(item[0], item[1])
    return buf.getvalue()


MOD = {"mod.toml": b'name = "Better title"\nversion = "1.2"\nauthor = "Sam"\ndescription = "A new title\\nscreen"\n',
       "README.txt": b"How to use it.\n", "media/stringtables/en/main.str": b"\x00\x01strings",
       "media/db/gamedb.slt": b"SQLite format 3\x00" + b"\x00" * 100, "media/ui/logo.dds": dds()}
PACK = {"pack.toml": b'name = "HD roads"\n', "roads/0123456789abcdef.dds": dds(128, 128, b"DXT5"),
        "roads/FEDCBA9876543210.png": png(), "LICENSE.txt": b"CC-BY\n"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0

    def check(self, data, kind="mod", limits=None):
        self.n += 1
        path = self.root / ("u%d.zip" % self.n)
        path.write_bytes(data)
        work = self.root / ("w%d" % self.n)
        work.mkdir()
        return hub_check.check(str(path), kind, str(work), limits)

    def refused(self, data, words, kind="mod", limits=None):
        r = self.check(data, kind, limits)
        self.assertFalse(r.ok, "passed, but should be refused for: %s" % words)
        self.assertIn(words, r.reason)
        return r


class Passes(Base):
    def test_a_mod_at_the_top_or_in_one_folder(self):
        for prefix in ("", "BetterTitle/"):
            r = self.check(make_zip({prefix + k: v for k, v in MOD.items()}))
            self.assertTrue(r.ok, r.reason)
            self.assertEqual(sorted(rel for rel, _, _ in r.files), sorted(MOD))
            self.assertEqual(r.manifest, {"name": "Better title", "version": "1.2", "author": "Sam",
                                          "description": "A new title\nscreen"})
            self.assertEqual(r.unpacked, sum(len(v) for v in MOD.values()))

    def test_a_texture_pack(self):
        r = self.check(make_zip({"pack/" + k: v for k, v in PACK.items()}), "pack")
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.textures, 2)
        r = self.check(make_zip(dict(PACK, **{"dir/": b""})), "pack")
        self.assertTrue(r.ok, r.reason)

    def test_the_clean_zip(self):
        r = self.check(make_zip({"x/" + k: v for k, v in MOD.items()}))
        out = io.BytesIO()
        hub_check.repack(r, "Better-title", out)
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:
            self.assertEqual(sorted(i.filename for i in z.infolist()), sorted("Better-title/" + k for k in MOD))
            for i in z.infolist():
                self.assertEqual(i.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(i.external_attr >> 16, stat.S_IFREG | 0o644)
                self.assertEqual(i.comment, b"")
                self.assertEqual(z.read(i), MOD[i.filename.split("/", 1)[1]])
            self.assertEqual(z.comment, b"")
        again = io.BytesIO()
        hub_check.repack(r, "Better-title", again)
        self.assertEqual(hashlib.sha256(out.getvalue()).digest(), hashlib.sha256(again.getvalue()).digest())

    def test_folder_names(self):
        self.assertEqual(hub_check.folder_name("Better Title: v2!", "ID"), "Better-Title-v2")
        self.assertEqual(hub_check.folder_name("../..", "ID"), "ID")
        self.assertEqual(hub_check.folder_name(".hidden", "ID"), "hidden")
        self.assertEqual(hub_check.folder_name("Café | *", "ID"), "Cafe")
        self.assertEqual(hub_check.folder_name("x" * 80, "ID"), "x" * 40)

    def test_manifest_text_is_cleaned(self):
        r = self.check(make_zip(dict(MOD, **{"mod.toml": 'name = "A\\u202eB\\u0007C"\nversion = 2\nauthor = true\n'.encode()})))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.manifest, {"name": "AB C", "version": "2"})


class ZipSlipAndNames(Base):
    def test_paths_that_leave_the_folder(self):
        for name in ("../evil.str", "media/../../evil.str", "/etc/passwd", "C:/x/media/a.str", "media/./a.str",
                     "media//a.str", "media\\a.str", "media/a.str/", "media/ a.str", "media/a."):
            with self.subTest(name=name):
                r = self.check(make_zip(dict(MOD, **{name: b"x"})))
                self.assertFalse(r.ok, name)

    def test_names_the_game_cannot_use(self):
        for name in ("media/caf\u00e9.str", "media/a\x01.str", "media/a|b.str", "media/a:b.str", "media/" + "a" * 260 + ".str"):
            with self.subTest(name=name):
                self.assertFalse(self.check(make_zip(dict(MOD, **{name: b"x"}))).ok)

    def test_names_that_differ_only_in_case_and_file_folder_clashes(self):
        self.refused(make_zip(dict(MOD, **{"media/DB/gamedb.SLT": b"x"})), "differ only in case")
        self.refused(make_zip(dict(MOD, **{"media/db": b"x"})), "both a file and a folder")
        self.refused(make_zip(list(MOD.items()) + [("README.txt", b"again")]), "differ only in case")

    def test_links_and_special_files(self):
        link = zipfile.ZipInfo("media/link.str")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.refused(make_zip(list(MOD.items()) + [(link, b"/etc/passwd")]), "is a link")
        fifo = zipfile.ZipInfo("media/fifo.str")
        fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
        self.refused(make_zip(list(MOD.items()) + [(fifo, b"")]), "not a plain file")
        folder = zipfile.ZipInfo("media/dir/")
        folder.external_attr = (stat.S_IFDIR | 0o755) << 16
        self.assertTrue(self.check(make_zip(list(MOD.items()) + [(folder, b"")])).ok)

    def test_the_layout(self):
        self.refused(make_zip({"a/media/x.str": b"x", "b/media/y.str": b"y"}), "there is no mod in it")
        self.refused(make_zip({"notes.txt": b"x"}), "there is no mod in it")
        self.refused(make_zip({"mod.toml": b'name = "x"'}), "no files under media")
        self.refused(make_zip({"readme.txt": b"x"}), "no texture pack", "pack")


class AllowList(Base):
    def test_what_a_mod_may_hold(self):
        self.refused(make_zip(dict(MOD, **{"tool.bin": b"x"})), "only mod.toml, .txt and .md")
        self.refused(make_zip(dict(MOD, **{"other/a.str": b"x"})), "go under media/")
        self.refused(make_zip(dict(MOD, **{"media/a.weird": b"x"})), "not one of the game's file types")
        self.refused(make_zip(dict(MOD, **{"media/noext": b"x"})), "a file without a type")
        for name in ("media/x.zip", "media/run.sh", "media/s.lua", "media/p.py", "media/page.html", "setup.exe",
                     "media/a.7z", "media/x.dll", "media/x.lnk", "install.bat", "media/x.svg"):
            with self.subTest(name=name):
                self.refused(make_zip(dict(MOD, **{name: b"data"})), "not allowed")

    def test_programs_and_archives_by_what_they_start_with(self):
        for head, what in ((b"MZ\x90\x00", "a Windows program"), (b"\x7fELF\x02", "a Linux program"),
                           (b"PK\x03\x04rest", "an archive"), (b"7z\xbc\xaf\x27\x1c", "an archive"),
                           (b"#!/bin/sh\nrm -rf ~", "a script"), (b"\xef\xbb\xbf  <script>x", "a script"),
                           (b"\n<!DOCTYPE HTML>", "a web page"), (b"\x1f\x8b\x08", "an archive"),
                           (b"\xcf\xfa\xed\xfe", "a macOS program")):
            with self.subTest(what=what):
                self.refused(make_zip(dict(MOD, **{"media/data.bin": head + b"\x00" * 32})), what)
        self.refused(make_zip(dict(MOD, **{"README.txt": b"MZ program"})), "a Windows program")

    def test_what_a_pack_may_hold(self):
        self.refused(make_zip(dict(PACK, **{"tex/abc.dds": dds()})), "<fingerprint>.dds or .png", "pack")
        self.refused(make_zip(dict(PACK, **{"tex/0123456789abcdee.jpg": b"x"})), "<fingerprint>.dds or .png", "pack")
        self.refused(make_zip(dict(PACK, **{"tex/0123456789abcdee.dds": b"MZ" + b"\x00" * 200})), "a Windows program", "pack")

    def test_textures_the_game_would_not_load(self):
        for data, words in ((dds(short=True), "shorter than its header"), (dds(fourcc=b"ABCD"), "does not read"),
                            (b"DDS " + b"\x00" * 200, "damaged DDS"), (png(20000, 8), "over 16384"),
                            (b"GIF89a" + b"\x00" * 200, "neither a DDS nor a PNG")):
            with self.subTest(words=words):
                self.refused(make_zip(dict(PACK, **{"tex/0123456789abcdee.dds": data})), words, "pack")
        self.refused(make_zip(dict(MOD, **{"media/ui/x.dds": b"\x00" * 200})), "not the image its name says")

    def test_manifests(self):
        self.refused(make_zip(dict(MOD, **{"mod.toml": b"name = "})), "mod.toml could not be read")
        self.refused(make_zip(dict(MOD, **{"mod.toml": b"\xff\xfe"})), "mod.toml could not be read")
        self.refused(make_zip(dict(MOD, **{"mod.toml": b"# " + b"x" * (70 << 10)})), "bigger than 64 KB")
        self.refused(make_zip(dict(MOD, **{"README.md": os.urandom(600_000).hex().encode()})), "bigger than 1 MB")


class Bombs(Base):
    def test_the_ratio(self):
        self.refused(make_zip(dict(MOD, **{"media/zeros.bin": b"\x00" * (8 << 20)})), "zip bomb")
        ok = hub_check.Limits(max_ratio=100000)
        self.assertTrue(self.check(make_zip(dict(MOD, **{"media/zeros.bin": b"\x00" * (8 << 20)})), limits=ok).ok)

    def test_the_whole_one_file_and_the_count(self):
        data = make_zip(dict(MOD, **{"media/a.bin": os.urandom(300_000), "media/b.bin": os.urandom(300_000)}))
        self.refused(data, "unpacks to more than", limits=hub_check.Limits(max_unpacked=500_000))
        self.refused(data, "bigger than", limits=hub_check.Limits(max_file=200_000))
        self.refused(data, "entries, more than", limits=hub_check.Limits(max_entries=6))
        self.assertTrue(self.check(data, limits=hub_check.Limits(max_entries=7)).ok)

    def test_sizes_the_zip_lies_about(self):
        data = bytearray(make_zip(dict(MOD, **{"media/big.bin": os.urandom(100_000)}), zipfile.ZIP_STORED))
        # The central directory says media/big.bin is 10 bytes.
        i = 0
        while True:
            i = data.find(b"PK\x01\x02", i)
            if i < 0:
                break
            n = struct.unpack_from("<H", data, i + 28)[0]
            if data[i + 46:i + 46 + n] == b"media/big.bin":
                struct.pack_into("<I", data, i + 24, 10)
                struct.pack_into("<I", data, i + 20, 10)
            i += 4
        self.assertFalse(self.check(bytes(data)).ok)

    def test_encryption_and_other_compressions(self):
        self.refused(make_zip(MOD, zipfile.ZIP_BZIP2), "compression")
        self.refused(make_zip(MOD, zipfile.ZIP_LZMA), "compression")
        data = bytearray(make_zip(MOD, zipfile.ZIP_STORED))
        i = 0
        while (i := data.find(b"PK\x01\x02", i)) >= 0:
            data[i + 8] |= 1
            i += 4
        i = 0
        while (i := data.find(b"PK\x03\x04", i)) >= 0:
            data[i + 6] |= 1
            i += 4
        r = self.check(bytes(data))
        self.assertFalse(r.ok)

    def test_a_directory_bigger_than_the_limits_is_refused_before_it_is_read(self):
        data = bytearray(make_zip(MOD))
        i = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, i + 8, 60000, 60000)  # says 60,000 entries
        self.refused(bytes(data), "entries, more than")
        data = bytearray(make_zip(MOD))
        struct.pack_into("<I", data, i + 12, 1 << 31)       # a directory of 2 GB
        self.refused(bytes(data), "list of files is too big")

    def test_a_damaged_header_offset(self):
        data = bytearray(make_zip(MOD, zipfile.ZIP_STORED))
        i = data.rfind(b"PK\x05\x06")
        struct.pack_into("<I", data, i + 16, 0)             # the directory starts at 0: offsets go wrong
        r = self.check(bytes(data))
        self.assertFalse(r.ok)

    def test_not_zips(self):
        for data in (b"", b"MZ" + b"\x00" * 100, b"PK\x03\x04" + os.urandom(100), make_zip({}), os.urandom(5000)):
            self.assertFalse(self.check(data).ok)


FAKE_CLAMSCAN = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if "--version" in args:
    print("ClamAV 1.4.3/27000/Fake"); sys.exit(0)
db = [a for a in args if a.startswith("--database=")][0].split("=", 1)[1]
if not os.path.isdir(db):
    print("LibClamAV Error: no database", file=sys.stderr); sys.exit(2)
root = args[-1]
for d, _, files in os.walk(root):
    for f in sorted(files):
        p = os.path.join(d, f)
        if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in open(p, "rb").read():
            print(p + ": Eicar-Signature FOUND"); sys.exit(1)
sys.exit(0)
'''


class Worker(Base):
    def setUp(self):
        super().setUp()
        for d in ("incoming", "checked", "work", "db"):
            (self.root / d).mkdir()
        (self.root / "db" / "test.ndb").write_text("Eicar-Signature:0:*:45494341522d5354414e44415244\n")
        fake = self.root / "clamscan"
        fake.write_text(FAKE_CLAMSCAN.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        fake.chmod(0o755)
        self.args = type("A", (), dict(incoming=str(self.root / "incoming"), checked=str(self.root / "checked"),
                                       work=str(self.root / "work"), clamav_db=str(self.root / "db"),
                                       clamscan=str(fake)))

    def job(self, jid, data, kind="mod", title="Better title", **over):
        (self.root / "incoming" / (jid + ".zip")).write_bytes(data)
        doc = dict(version=1, id=jid, kind=kind, title=title, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        doc.update(over)
        (self.root / "incoming" / (jid + ".json")).write_text(json.dumps(doc))

    def result(self, jid):
        return json.loads((self.root / "checked" / (jid + ".json")).read_text())

    def run_once(self):
        return hub_worker.once(self.args, "ClamAV test")

    def test_a_good_mod_gets_a_clean_zip(self):
        self.job("AAAAAAAAAAAA", make_zip({"x/" + k: v for k, v in MOD.items()}))
        self.assertEqual(self.run_once(), 1)
        r = self.result("AAAAAAAAAAAA")
        self.assertTrue(r["ok"], r)
        clean = (self.root / "checked" / "AAAAAAAAAAAA.zip").read_bytes()
        self.assertEqual(r["sha256"], hashlib.sha256(clean).hexdigest())
        self.assertEqual((r["size"], r["files"], r["folder"]), (len(clean), len(MOD), "Better-title"))
        self.assertEqual(r["manifest"]["author"], "Sam")
        self.assertEqual(sorted(r["names"]), sorted(MOD))
        self.assertEqual(list((self.root / "work").iterdir()), [])  # nothing left behind
        self.assertEqual(self.run_once(), 0)  # done once

    def test_malware_and_refusals(self):
        self.job("BBBBBBBBBBBB", make_zip(dict(MOD, **{"media/readme.txt": EICAR})))
        self.job("CCCCCCCCCCCC", make_zip(dict(MOD, **{"../x.str": b"x"})))
        self.run_once()
        r = self.result("BBBBBBBBBBBB")
        self.assertEqual((r["ok"], r["error"], r["infected"]), (False, False, "Eicar-Signature"))
        self.assertIn("media/readme.txt", r["reason"])
        self.assertFalse((self.root / "checked" / "BBBBBBBBBBBB.zip").exists())
        self.assertFalse(self.result("CCCCCCCCCCCC")["ok"])

    def test_a_scanner_that_fails_is_an_error_not_a_pass(self):
        (self.root / "db" / "test.ndb").unlink()
        self.job("DDDDDDDDDDDD", make_zip(MOD))
        self.assertEqual(self.run_once(), 0)  # no signatures yet: the job waits
        self.assertFalse((self.root / "checked" / "DDDDDDDDDDDD.json").exists())
        (self.root / "db" / "test.ndb").write_text("x")
        self.args.clamscan = self.args.clamscan + "-fails"
        (self.root / "clamscan-fails").write_text("#!/bin/sh\necho 'LibClamAV Error: broken' >&2\nexit 2\n")
        (self.root / "clamscan-fails").chmod(0o755)
        self.run_once()
        r = self.result("DDDDDDDDDDDD")
        self.assertEqual((r["ok"], r["error"]), (False, True))
        self.args.clamscan = str(self.root / "missing")
        self.job("EEEEEEEEEEEE", make_zip(MOD))
        self.run_once()
        self.assertEqual(self.result("EEEEEEEEEEEE")["error"], True)

    def test_a_job_that_crashes_the_checker_does_not_stop_the_queue(self):
        self.job("LLLLLLLLLLLL", make_zip(MOD))
        self.job("MMMMMMMMMMMM", make_zip(MOD))
        real = hub_worker.process

        def crash(job, *a):
            if job["id"] == "LLLLLLLLLLLL":
                raise OSError(22, "Invalid argument")
            return real(job, *a)
        hub_worker.process = crash
        try:
            self.run_once()
        finally:
            hub_worker.process = real
        self.assertEqual((self.result("LLLLLLLLLLLL")["ok"], self.result("MMMMMMMMMMMM")["ok"]), (False, True))
        # A job that was killed twice (out of memory) is refused the third time.
        self.job("NNNNNNNNNNNN", make_zip(MOD))
        (self.root / "work" / "NNNNNNNNNNNN.attempts").write_text("2")
        self.run_once()
        self.assertIn("stopped the checker", self.result("NNNNNNNNNNNN")["reason"])

    def test_jobs_that_do_not_match_their_upload(self):
        self.job("FFFFFFFFFFFF", make_zip(MOD), sha256="0" * 64)
        self.job("GGGGGGGGGGGG", make_zip(MOD), size=5)
        self.job("HHHHHHHHHHHH", make_zip(MOD), id="../../etc")   # not its own name: skipped
        self.job("IIIIIIIIIIII", make_zip(MOD), kind="exe")       # skipped
        (self.root / "incoming" / "junk.json").write_text("{}")
        self.run_once()
        self.assertFalse(self.result("FFFFFFFFFFFF")["ok"])
        self.assertFalse(self.result("GGGGGGGGGGGG")["ok"])
        self.assertFalse((self.root / "checked" / "HHHHHHHHHHHH.json").exists())
        self.assertFalse((self.root / "checked" / "IIIIIIIIIIII.json").exists())

    @unittest.skipUnless(os.environ.get("FORZA_GAME_REPO"), "FORZA_GAME_REPO: the game's repository")
    def test_the_launcher_adds_what_the_worker_writes(self):
        sys.path.insert(0, os.path.join(os.environ["FORZA_GAME_REPO"], "launcher"))
        from forza_launcher import mods, texture_packs
        self.job("JJJJJJJJJJJJ", make_zip(MOD))
        self.job("KKKKKKKKKKKK", make_zip(PACK), kind="pack", title="HD roads")
        self.run_once()
        mod = mods.install(self.root / "checked" / "JJJJJJJJJJJJ.zip", self.root / "mods")
        self.assertEqual((mod.folder, mod.name, mod.files, mod.problem), ("Better-title", "Better title", 3, ""))
        pack = texture_packs.install(self.root / "checked" / "KKKKKKKKKKKK.zip", self.root / "textures")
        self.assertEqual((pack.folder, pack.files, pack.problem), ("HD-roads", 2, ""))


if __name__ == "__main__":
    unittest.main()
