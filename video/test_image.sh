#!/usr/bin/env bash
# The video converter's image, run the way compose.yaml runs it (no network, a read-only root, no
# capabilities, its own user, its memory and process limits), on clips made here by the image's
# own ffmpeg from a test pattern and a tone: one to convert, one blank picture, one that is not a
# clip. Nothing is downloaded but the image's packages.
#   video/test_image.sh          (docker, or podman when there is no docker)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE="${ENGINE:-$(command -v docker || command -v podman)}"
IMAGE=forza-video-converter:image-test
WORK="$(mktemp -d)"
mkdir -p "$WORK"/{incoming,converted}
"$ENGINE" build -q -t "$IMAGE" "$HERE" >/dev/null
chmod -R a+rwX "$WORK"
USERNS=() Z=""
if [ "$(basename "$ENGINE")" = podman ]; then USERNS=(--userns=keep-id:uid=10003,gid=10003) Z=",Z"; fi
# The results' folders belong to the converter's user: it opens them up before they are removed.
cleanup() {
    "$ENGINE" run --rm --network none --user 10003:10003 "${USERNS[@]}" -v "$WORK/converted:/c${Z/,/:}" \
        --entrypoint chmod "$IMAGE" -R a+rwX /c >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
make() {  # id, the lavfi video source
    "$ENGINE" run --rm --network none --entrypoint ffmpeg "${USERNS[@]}" --user 10003:10003 \
        -v "$WORK/incoming:/out${Z/,/:}" "$IMAGE" -nostdin -v error -f lavfi -i "$2" \
        -f lavfi -i "sine=f=440:d=4:sample_rate=48000" -c:v wmv2 -b:v 800k -c:a wmav2 -f asf "/out/$1.wmv"
    printf '{"version":1,"id":"%s","maxSeconds":120,"maxWidth":1280,"frames":8,"maxBytes":33554432}' "$1" \
        > "$WORK/incoming/$1.json"
}
make GOODCLIPAAAA "testsrc2=s=640x360:r=20:d=4"
make BLANKCLIPAAA "color=c=black:s=640x360:r=20:d=4"
printf 'not a clip' > "$WORK/incoming/NOTACLIPAAAA.wmv"
printf '{"version":1,"id":"NOTACLIPAAAA"}' > "$WORK/incoming/NOTACLIPAAAA.json"
"$ENGINE" run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges "${USERNS[@]}" \
    --user 10003:10003 --tmpfs /tmp:size=16m --pids-limit 64 --memory 1536m \
    -v "$WORK/incoming:/incoming:ro$Z" -v "$WORK/converted:/converted${Z/,/:}" \
    "$IMAGE" --incoming /incoming --converted /converted --once
python3 -I - "$WORK" <<'PY'
import json, sys
from pathlib import Path
c = Path(sys.argv[1]) / "converted"
r = {p.name: json.loads((p / "result.json").read_text()) for p in c.iterdir() if p.is_dir()}
want = {"GOODCLIPAAAA": True, "BLANKCLIPAAA": False, "NOTACLIPAAAA": False}
bad = [k for k, ok in want.items() if r.get(k, {}).get("ok") is not ok]
if bad:
    sys.exit("unexpected results: %s" % {k: r.get(k) for k in bad})
good = c / "GOODCLIPAAAA"
assert (good / "video.mp4").read_bytes()[4:8] == b"ftyp"
assert sorted(p.name for p in good.iterdir()) == sorted(
    ["result.json", "video.mp4", "poster.jpg"] + ["frame-%d.jpg" % n for n in range(1, 9)])
assert "blank" in r["BLANKCLIPAAA"]["reason"], r["BLANKCLIPAAA"]
print("image test passed: %s" % r["GOODCLIPAAAA"])
PY
