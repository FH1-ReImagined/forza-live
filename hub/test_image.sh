#!/usr/bin/env bash
# The hub's scanning worker image with the real ClamAV, run the way compose.yaml runs it (no
# network, a read-only root, no capabilities, its own user), on test uploads: a good mod, a good
# texture pack, one with the EICAR test string and one with a zip slip. The signature is made
# here for the EICAR test string, so nothing is downloaded but the image's packages.
#   hub/test_image.sh            (docker, or podman when there is no docker)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${ENGINE:-$(command -v docker || command -v podman)}"
IMAGE=forza-hub-scanner:image-test
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK"/{incoming,checked,work,db}
"$ENGINE" build -q -t "$IMAGE" "$HERE" >/dev/null
python3 -I - "$HERE" "$WORK" <<'PY'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import test_hub as t
w = Path(sys.argv[2])
(w / "db" / "test.ndb").write_text("Test.EICAR:0:*:%s\n" % b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE".hex())
def job(jid, data, kind="mod", title="Better title"):
    (w / "incoming" / (jid + ".zip")).write_bytes(data)
    (w / "incoming" / (jid + ".json")).write_text(json.dumps(dict(version=1, id=jid, kind=kind, title=title,
        size=len(data), sha256=hashlib.sha256(data).hexdigest())))
job("GOODMODAAAAA", t.make_zip({"x/" + k: v for k, v in t.MOD.items()}))
job("GOODPACKAAAA", t.make_zip(t.PACK), "pack", "HD roads")
job("VIRUSAAAAAAA", t.make_zip(dict(t.MOD, **{"media/note.txt": t.EICAR})))
job("SLIPAAAAAAAA", t.make_zip(dict(t.MOD, **{"../../etc/x.str": b"x"})))
PY
chmod -R a+rwX "$WORK"
USERNS=() Z=""
if [ "$(basename "$ENGINE")" = podman ]; then USERNS=(--userns=keep-id:uid=10002,gid=10002) Z=",Z"; fi
"$ENGINE" run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges "${USERNS[@]}" \
    --user 10002:10002 --tmpfs /tmp:size=64m --pids-limit 64 \
    -v "$WORK/incoming:/incoming:ro$Z" -v "$WORK/checked:/checked${Z/,/:}" -v "$WORK/work:/work${Z/,/:}" \
    -v "$WORK/db:/clamav-db:ro$Z" "$IMAGE" --once
python3 -I - "$WORK" <<'PY'
import json, sys
from pathlib import Path
c = Path(sys.argv[1]) / "checked"
r = {p.stem: json.loads(p.read_text()) for p in c.glob("*.json")}
want = {"GOODMODAAAAA": True, "GOODPACKAAAA": True, "VIRUSAAAAAAA": False, "SLIPAAAAAAAA": False}
bad = [k for k, ok in want.items() if r.get(k, {}).get("ok") is not ok]
assert r["VIRUSAAAAAAA"].get("infected", "").startswith("Test.EICAR"), r["VIRUSAAAAAAA"]
assert (c / "GOODMODAAAAA.zip").is_file() and not (c / "VIRUSAAAAAAA.zip").exists()
assert r["GOODMODAAAAA"]["scanner"].startswith("ClamAV"), r["GOODMODAAAAA"]
if bad:
    sys.exit("unexpected results: %s" % {k: r.get(k) for k in bad})
print("image test passed: %s" % r["GOODMODAAAAA"]["scanner"])
PY
