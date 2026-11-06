#!/usr/bin/env python3
"""The mods and texture packs hub's scanning worker (website #2; README.md in this folder).

It runs in its own container with no network, a read-only root and no privileges, and talks to
the website accounts service only through two folders:

  /incoming   read-only here: the accounts service writes <id>.zip (the upload as it came) and
              then <id>.json, the job: {"version": 1, "id", "kind": "mod"|"pack", "title",
              "size", "sha256"}
  /checked    written here: <id>.zip, the new zip of only the checked files (hub_check.repack),
              then <id>.json, the result (below). The accounts service reads it, moves the zip
              to the download host's folder once a maintainer approved it, and deletes the job

For each job without a result: the upload's size and SHA-256 are checked against the job, the zip
against hub_check's rules (decompressed into /work/<id>, never under a name from the zip), the
files it would publish are scanned with ClamAV (clamscan, the signatures in the read-only
--clamav-db folder that the updater container keeps fresh), and a clean copy is written. Then the
result, written last (so a result is always whole):

  {"version": 1, "id", "ok": true, "sha256", "size", "files", "unpacked", "textures", "folder",
   "manifest": {"name", "version", "author", "description"}, "names": [first 500 paths], "scanner"}
  {"version": 1, "id", "ok": false, "reason": "<for the uploader>", "error": true|false,
   "infected": "<signature>" (when ClamAV found something), "scanner"}

"error" means the check itself failed (ClamAV missing, its signatures missing, a timeout): the
upload is not judged and can be sent again.

  python3 -I hub_worker.py [--incoming /incoming] [--checked /checked] [--work /work]
                           [--clamav-db /clamav-db] [--clamscan clamscan] [--once]
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_check  # noqa: E402

log = logging.getLogger("hub-worker")

ID_RE = re.compile(r"[A-Z2-7]{12}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
JOB_MAX = 4096
POLL_SECONDS = 5
SCAN_TIMEOUT = 1800
NAMES_LISTED = 500
MAX_UPLOAD = int(os.environ.get("FORZA_HUB_MAX_MB", "512")) << 20
_stop = [False]


def write_atomic(path, data):
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_job(path):
    """The job, checked, or None (logged)."""
    try:
        if path.stat().st_size > JOB_MAX:
            raise ValueError("too big")
        job = json.loads(path.read_text(encoding="utf-8"))
        if not (isinstance(job, dict) and job.get("version") == 1 and isinstance(job.get("id"), str)
                and ID_RE.fullmatch(job["id"]) and job["id"] == path.stem and job.get("kind") in hub_check.KINDS
                and isinstance(job.get("title"), str) and isinstance(job.get("size"), int)
                and isinstance(job.get("sha256"), str) and SHA_RE.fullmatch(job["sha256"])):
            raise ValueError("not a job")
        return job
    except (OSError, ValueError) as e:
        log.warning("hub: job %s skipped: %s", path.name[:40], e)
        return None


def scanner_version(clamscan):
    try:
        out = subprocess.run([clamscan, "--version"], capture_output=True, text=True, timeout=60)
        return out.stdout.strip().split("/")[0][:60] or "ClamAV"
    except (OSError, subprocess.SubprocessError):
        return "ClamAV (not found)"


def clamscan(args, folder, names):
    """(None, None) when ClamAV finds nothing; ("signature", path) when it finds something;
    raises RuntimeError when the scan could not be done."""
    cmd = [args.clamscan, "--database=" + args.clamav_db, "--recursive", "--infected", "--no-summary",
           "--stdout", "--scan-archive=yes", "--max-filesize=1024M", "--max-scansize=4096M",
           "--max-files=100000", "--max-recursion=8", "--alert-encrypted=yes", "--heuristic-alerts=yes",
           "--", str(folder)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("clamscan did not run (%s)" % type(e).__name__) from None
    if out.returncode == 0:
        return None, None
    if out.returncode == 1:
        for line in out.stdout.splitlines():
            if line.endswith(" FOUND") and ": " in line:
                where, sig = line[:-len(" FOUND")].rsplit(": ", 1)
                return sig.strip()[:120], names.get(os.path.basename(where), "?")
        return "unknown", "?"
    raise RuntimeError("clamscan failed (%d): %s" % (out.returncode, (out.stderr or out.stdout).strip()[-300:]))


def process(job, args, version):
    """The result of a job (a dict); the clean zip is in place when it says ok."""
    jid, incoming, checked = job["id"], Path(args.incoming), Path(args.checked)
    base = {"version": 1, "id": jid, "scanner": version}
    upload = incoming / (jid + ".zip")
    try:
        size = upload.stat().st_size
    except OSError:
        return dict(base, ok=False, error=True, reason="the upload is missing: send it again")
    if size != job["size"] or size > MAX_UPLOAD:
        return dict(base, ok=False, error=False, reason="the upload is not the size it should be")
    if sha256_of(upload) != job["sha256"]:
        return dict(base, ok=False, error=True, reason="the upload changed on the way: send it again")
    work = Path(args.work) / jid
    shutil.rmtree(work, ignore_errors=True)
    (work / "files").mkdir(parents=True)
    try:
        result = hub_check.check(str(upload), job["kind"], str(work / "files"))
        if not result.ok:
            return dict(base, ok=False, error=False, reason=result.reason)
        names = {str(i): rel for i, (rel, _, _) in enumerate(result.files)}
        try:
            sig, where = clamscan(args, work / "files", names)
        except RuntimeError as e:
            log.warning("hub: %s not scanned: %s", jid, e)
            return dict(base, ok=False, error=True, reason="the malware scan could not be done: send it again later")
        if sig:
            log.warning("hub: %s: ClamAV found %s in %s", jid, sig, where)
            return dict(base, ok=False, error=False, infected=sig,
                        reason="the malware scan found %s in %s" % (sig, hub_check._shown(where)))
        folder = hub_check.folder_name(result.manifest.get("name") or job["title"], jid)
        part = checked / (jid + ".zip.part")
        with open(part, "wb") as out:
            hub_check.repack(result, folder, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, checked / (jid + ".zip"))
        clean = checked / (jid + ".zip")
        return dict(base, ok=True, sha256=sha256_of(clean), size=clean.stat().st_size, files=len(result.files),
                    unpacked=result.unpacked, textures=result.textures, folder=folder, manifest=result.manifest,
                    names=[rel for rel, _, _ in result.files[:NAMES_LISTED]])
    finally:
        shutil.rmtree(work, ignore_errors=True)


SIGNATURES = (".cvd", ".cld", ".cud", ".ndb", ".hdb", ".hsb", ".ldb", ".yar", ".yara")


def signatures_ready(folder):
    """Whether the signature folder holds a database (the updater fills it at its first run)."""
    try:
        return any(p.suffix in SIGNATURES and p.stat().st_size > 0 for p in Path(folder).iterdir())
    except OSError:
        return False


def once(args, version):
    """Every job without a result, oldest first. The number done. Nothing is done while there
    are no signatures yet: the jobs wait."""
    incoming, checked = Path(args.incoming), Path(args.checked)
    if not signatures_ready(args.clamav_db):
        log.warning("hub: no ClamAV signatures in %s yet: the uploads wait", args.clamav_db)
        return 0
    jobs = sorted((p for p in incoming.glob("*.json") if ID_RE.fullmatch(p.stem)), key=lambda p: p.stat().st_mtime)
    done = 0
    for path in jobs:
        if _stop[0]:
            break
        if (checked / path.name).exists():
            continue
        job = read_job(path)
        if job is None:
            continue
        started = time.monotonic()
        result = process(job, args, version)
        write_atomic(checked / path.name, json.dumps(result, separators=(",", ":")).encode())
        log.info("hub: %s (%s) %s in %.1f s%s", job["id"], job["kind"], "passed" if result["ok"] else "refused",
                 time.monotonic() - started, "" if result["ok"] else ": " + result["reason"])
        done += 1
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--incoming", default="/incoming")
    ap.add_argument("--checked", default="/checked")
    ap.add_argument("--work", default="/work")
    ap.add_argument("--clamav-db", default="/clamav-db")
    ap.add_argument("--clamscan", default="clamscan")
    ap.add_argument("--once", action="store_true", help="do the jobs waiting now, then stop")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *a: _stop.__setitem__(0, True))
    version = scanner_version(args.clamscan)
    log.info("hub: worker started (%s)", version)
    while not _stop[0]:
        try:
            once(args, version)
        except OSError as e:
            log.warning("hub: %s", e)
        if args.once:
            break
        for _ in range(POLL_SECONDS * 10):
            if _stop[0]:
                break
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
