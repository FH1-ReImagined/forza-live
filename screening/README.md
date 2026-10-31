# Screening worker for community photos

With `FORZA_COMMUNITY_HOLD=1`, every new community photo (a 1920x1080 PNG the server makes from
a photo mode Big Shot; [`docs/hosting.md`](../docs/hosting.md#community-photos)) starts `held`.
This worker screens the held photos automatically on the server's own machine, with no hosted
API:

- It polls `GET /admin/photos/queue` every 30 s and fetches each held photo.
- It scores the photo with three CPU models (onnxruntime) and word lists.
- It then **releases** clear photos, **hides** confident hits with a note naming the rule, and
  **leaves held** the doubtful ones for the maintainer, logging why.

The rules come from the moderation research of October 2026: hide only on a confident hit, hold
what is doubtful, and never let one uncertain score hide a photo. Player reports stay the safety
net for what screening misses.

## Decision table

`screen_policy.py` makes the decision from the thresholds in `screening.toml`. Any hide rule
wins; otherwise any hold rule holds; otherwise the photo is released.

| Signal | Release | Hold (left for the maintainer) | Hide (note: `screening: hide (<rule>: ...)`) |
|---|---|---|---|
| NSFW classifier, P(medium) + P(high) | < 0.30 | ≥ 0.30 (`nsfw-suspect`) | P(high) ≥ 0.90 (`nsfw-high`) |
| SigLIP2 hate-symbol prompts, highest sigmoid over the frame and the 3x2 tiles | < 0.30 | ≥ 0.30 (`hate-suspect`) | ≥ 0.90 **and** a `[hate]` or `[hide]` word in the photo or title (`hate-symbol+text`) |
| SigLIP2 gore prompts | < 0.30 | ≥ 0.30 (`gore-suspect`) | never |
| SigLIP2 nudity prompts (backs up the classifier on small regions) | < 0.30 | ≥ 0.30 (`nudity-suspect`) | never |
| Words read in the photo (OCR confidence ≥ 0.50) or in its title | no hit | an LDNOOBW word (`text-word`); a `[hate]` word (`text-hate`); a `[hide]` word read with confidence < 0.80 (`text-unsure`); a `[hide]` or `[hate]` word inside a longer word | a `[hide]` word read with confidence ≥ 0.80, or in the title (`text-hide`) |
| Ambiguous codes: 14, 18, 28, 88, SS, HH, 311, 2316 (race numbers, model badges), the characters 卐 and 卍 | alone | with a hate score ≥ 0.20 (`code+symbol`) | never |
| The credited gamertag | no hit | any listed word (`gamertag-word`) | never |
| pHash within 6 bits (of 64) of a hidden photo | | `like-hidden` | never |
| A trained probe (`[probe]`, none yet) | below its hold threshold | `probe-<label>` | at its hide threshold, if configured |
| A stage failed, or the picture could not be decoded | | `error-<stage>` | |

`[hide]` words are unambiguous slurs and extremist slogans: "heil hitler", 1488, "14 88", KKK
and similar. `[hate]` words have other uses, such as history or film titles: "nazi", "hitler",
"swastika". Both lists are in `wordlists/terms.txt`. The swastika characters 卐 and 卍 are also
Buddhist and Hindu signs, so they count with the ambiguous codes.

Racing liveries are never hidden for their numbers or their logos:

- A race number such as 14, 18 or 88, or a badge such as SS, is only logged.
- It holds the photo only when the same photo also has a hate-symbol score of 0.20 or more.
- Sponsor names are not in any list.

Calibration and the hold rate:

- The thresholds were set on 64 game screenshots (menus, HUD, races, a festival video) plus drawn
  pictures. **None of the 64 was held.** Among them, the highest scores were:
  - hate 0.11, from a minimap's crossing roads;
  - nudity 0.11, from a bare leg in the intro video;
  - P(medium + high) 0.01 (a drawn all-black frame reaches 0.12).
- A clearly drawn swastika (drawn for this check, not kept) scored 0.95 or more, even when it was
  only 120 px wide on a car door. A plain red cross or star on white scores 0.14-0.16.
- Real photo mode shots have less text than menus, so they should hold even less. The target is to
  hold at most about 5% of photos.
- Watch the share of `"decision":"hold"` lines in the worker's log over the first weeks.

## Models and licences

All the models run on the CPU, in onnxruntime. Each file is downloaded when the image is built,
pinned by its revision and SHA-256 (`models.lock.json`), so the image downloads nothing at run
time. No weight file is in this repository. Licences are in `NOTICE.md`.

**NSFW: [`Freepik/nsfw_image_detector`](https://huggingface.co/Freepik/nsfw_image_detector) (MIT).**

- It is an EVA02-B/14 model at 448 px, with four classes: neutral, low, medium and high.
- `build_models.py` exports it to ONNX in fp32 and checks it against PyTorch (difference 3.5e-9).
- It takes about 0.5 s a photo on 4 threads, which is acceptable.
- We did not use the lighter `Falconsai/nsfw_image_detection` (Apache-2.0, a ViT-B/16 at 224
  px). It would take about 0.2 s (the research's estimate, not measured here), but Freepik's
  benchmark puts it at 31% on suggestive images ("low") and 79% on "medium", against 98% and
  97% for Freepik's model.
- To fall back to it on a much slower machine, export its ViT to ONNX the same way and map its
  `nsfw` probability to the `high` threshold.

**Hate symbols, gore and nudity: [`google/siglip2-base-patch16-224`](https://huggingface.co/google/siglip2-base-patch16-224) (Apache-2.0).**

- The worker uses the ONNX conversion by onnx-community, pinned. It runs the vision model on the
  whole frame and on 6 tiles of 640x540 (`zeroshot.grids`), and scores each region with SigLIP's
  own head (sigmoid of the logit scale x cosine + bias) against the prompts in `prompts.json`.
- The text model is used only at build time, to embed those prompts. Changing a prompt needs a
  rebuild.
- There is no trained probe yet. `[probe]` loads one from an `.npz` file (`screen_models.Probe`),
  and `[state] save_embeddings` keeps every photo's embeddings to train it on. The research
  suggests about 200 in-game liveries with symbols and 1,000 normal ones, with the threshold set
  for 95% recall.

**Text: [RapidOCR](https://github.com/RapidAI/RapidOCR) 3.10.0 (Apache-2.0)**, with the PP-OCRv6
small models (Chinese and English) that ship in its wheel. The wheel is pinned by hash.

**Word lists:**

- [LDNOOBW](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words)
  (CC-BY-4.0): 27 languages, fetched at a pinned commit.
- This project's `wordlists/terms.txt`, with the sections `[hide]`, `[hate]`, `[ambiguous]` and
  `[allow]`.
- `wordlists/ldnoobw_skip.txt`: about 80 entries of LDNOOBW's non-English lists that are ordinary
  English words, such as "con", "del", "fan", "pinto" and "regina".
- Before matching, the text is normalised: NFKC, case folding, accents, Cyrillic and Greek
  look-alike letters, leetspeak, repeated letters and spaced-out letters (`screen_text.py`).

## Cost (measured)

Measured on a Ryzen 7 7800X3D with 4 threads per model, 1920x1080 pictures:

| Step | Time |
|---|---|
| NSFW | 0.45-0.50 s |
| SigLIP2 (7 regions) | 0.50-0.55 s |
| OCR | 0.1-0.3 s; up to 1.3 s on menus full of text |
| **Total per photo** | **median 1.24 s, p90 1.55 s, max 2.7 s** |

The same container limited to 2 CPUs (`--cpus 2`, `threads = 2`) took a median of 2.5 s per
photo (at most 3.8 s), with a peak of 1.38 GB.

- Loading the models takes about 1 s, and the self-test at start a few seconds.
- Memory is about 0.95 GB between photos and peaks at about 1.3-1.45 GB.
- The onnxruntime sessions run without their memory arena or prepacked weights. That halves the
  resident size for about 5-10% more time.
- An older homelab CPU may be 2-4 times slower, which still means a few seconds a photo. The
  server caps uploads at 60 an hour.
- Set `[runtime] threads` to the number of CPUs you give the container (`--cpus`).

## Deploy on Unraid

The worker has to reach the server from an address that is not a trusted proxy:

- The public server runs with `--network host` and `--trusted-proxy 127.0.0.1` (and the tunnel
  agent's address), and it refuses moderation requests from trusted-proxy addresses.
- So the worker runs on Docker's default **bridge** network, not `host`. It reaches the server
  at the bridge's gateway, `http://172.17.0.1:21201`.

This works because of how Docker routes the request and how the server checks it:

- Docker delivers the request locally on `docker0` without NAT, so the server sees the
  container's own address, 172.17.0.x.
- `forza_live_server.http_handler` finds 172.17.0.x is not a trusted proxy, so it requires no
  PROXY header and counts the worker under the normal per-address limit.
- `forza_live_lsp.is_local_admin` admits the request: it has no `X-Forwarded-For`, `X-Real-IP` or
  `Forwarded` header, does not come from a trusted proxy, and carries the right `X-Admin-Token`.
- `forza_community` refuses requests with an `Origin` header. The worker sends none, follows no
  redirects and ignores proxy environment variables.

`test_screen_client.BridgeAddress` checks all of this against the server's code.

Two settings on the server side matter:

- The server must listen on all addresses (`--host 0.0.0.0`, as in `docs/hosting.md`).
- `--trusted-proxy` must not cover 172.17.0.0/16.

Steps, in the Unraid terminal:

1. Build the image, from the copy of forza-live the server is built from:

   ```
   cd /mnt/user/appdata/forza-live/src/screening
   docker build -t forza-screening .
   ```

   The build downloads about 2.5 GB from Hugging Face, GitHub, PyPI and download.pytorch.org.
   The image is about 1.25 GB. Nothing is downloaded at run time.

2. Put the server's admin token in an env file only root can read. This command copies it from
   the server container without printing it:

   ```
   mkdir -p /mnt/user/appdata/forza-screening
   ( umask 077; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' forza-live \
       | grep '^FORZA_ADMIN_TOKEN=' > /mnt/user/appdata/forza-screening/token.env )
   ```

   Alternatively, write `FORZA_ADMIN_TOKEN=<token>` into that file with an editor, then run
   `chmod 600` on it.

3. Check the bridge's gateway address. It is usually 172.17.0.1:

   ```
   docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'
   ```

4. Do a dry run. It screens the queue once and changes nothing; the log shows what it would do:

   ```
   docker run --rm --network bridge \
     --env-file /mnt/user/appdata/forza-screening/token.env \
     -e FORZA_SERVER=http://172.17.0.1:21201 \
     --memory 3g --memory-swap 3g --read-only --tmpfs /tmp:size=64m \
     --cap-drop ALL --security-opt no-new-privileges \
     forza-screening --once --dry-run
   ```

5. Run it:

   ```
   docker run -d --name forza-screening --restart unless-stopped \
     --network bridge \
     --env-file /mnt/user/appdata/forza-screening/token.env \
     -e FORZA_SERVER=http://172.17.0.1:21201 \
     --memory 3g --memory-swap 3g --cpus 4 --pids-limit 256 \
     --read-only --tmpfs /tmp:size=64m \
     --cap-drop ALL --security-opt no-new-privileges \
     --log-opt max-size=20m --log-opt max-file=5 \
     forza-screening
   docker logs -f forza-screening
   ```

At start, the log shows three events:

- `{"event":"start",...}`, with the model versions;
- `{"event":"selftest","ok":true,...}`: the models find a drawn red star and read a drawn word,
  and the worker exits if they do not;
- `{"event":"polling",...}`.

The container runs as `nobody` with a read-only root filesystem and no capabilities. The token
is never printed. The worker keeps nothing on disk, and it has no volume unless you set
`[state]`.

**Network.** The worker only needs the server's HTTP port. The default bridge still allows
outgoing connections, which the worker never makes.

onnxruntime's Linux packages would send usage telemetry to Microsoft
(`mobile.events.data.microsoft.com`). We saw version 1.31.0 connect there in a sandbox, and
`onnxruntime.disable_telemetry_events()` did not stop it. The image and the worker set
`ORT_DISABLE_TELEMETRY=1`, which does stop it.

To forbid outgoing connections entirely, use a network of its own and drop everything it
forwards:

```
docker network create --subnet 172.31.250.0/24 forza-screening
iptables -I DOCKER-USER -s 172.31.250.0/24 -j DROP   # add to /boot/config/go to keep it
```

Then run the container with `--network forza-screening` and
`-e FORZA_SERVER=http://172.31.250.1:21201`. The rule blocks only forwarded traffic, so the
server on the host stays reachable. If the host has a firewall of its own, allow TCP 21201 from
the worker's subnet.

**Changing thresholds.** Mount your copy over the image's file, then restart the container:

```
-v /mnt/user/appdata/forza-screening/screening.toml:/app/screening.toml:ro
```

Keep its relative paths (`wordlists/...`) or point them at mounted files.

**Updating.** Run `docker build` again, then `docker rm -f forza-screening` and the run command
again. Photos that were left held are screened again after a restart.

## The log

The worker writes one JSON line per photo on stdout. The line holds:

- the `id` and the start of its SHA-256;
- `models` (the revisions of the models and lists);
- `nsfw` (four probabilities);
- `zeroshot` (per category: `p`, the region `at` and the best `prompt`);
- `ocr` (each line and its confidence);
- `hits` (term, level, how it matched, source, confidence);
- `phash`;
- `decision` (`release`, `hide`, `hold`, or `retry` when the server could not be reached);
- `rule`, `detail` and every rule that fired (`rules`);
- `done` (what the server answered, for example `operator-decided-first` when the operator hid
  the photo while it was being scored);
- `ms` (time per stage and in total) and `rss_mb`.

The log has no XUID: the worker drops the queue's `owner` field.

For example, to list the photos left held and why:

```
docker logs forza-screening | grep '"decision":"hold"'
```

The maintainer then looks at the photo (`GET /admin/photos/img/<id>.png`) and releases or hides
it ([`docs/hosting.md`](../docs/hosting.md#community-photos)).

Released and hidden photos also carry the worker's note in `/admin/photos`. A held photo has no
note: the API can set a note only together with a change of status.

## Tests

The tests use only pictures drawn with Pillow (`synth.py`). They never download or use real NSFW
or hate imagery. From the repository's root:

```
python3 -m unittest discover -s screening -p 'test_screen_*.py' -v
```

- `test_screen_policy`: the decision table, with made-up scores.
- `test_screen_text`: the word matching, with test words, and the shipped lists on benign racing
  text.
- `test_screen_client`: the worker, with stand-in models, against this repository's server. The
  test starts the server on 127.0.0.1 with `FORZA_COMMUNITY_HOLD=1` and held photos seeded. It
  covers:
  - release, hide and hold;
  - an operator who hides a photo while it is being scored (the release gets 409 and the photo
    stays hidden);
  - dry runs;
  - refusals from a trusted-proxy address;
  - redirects, proxy variables and the token never printed;
  - the server's admin checks for a bridge address;
  - hostile pictures and pHash.
- `test_screen_e2e`: the real models, through `screen_worker.main --once`, on five drawn pictures
  held on a local server. It prints the time per picture and the peak memory. It needs the built
  models (`SCREENING_MODELS`, default `/models`), so it is skipped elsewhere, including CI. In
  the image:

  ```
  docker run --rm --read-only --tmpfs /tmp --entrypoint python3 -v "$PWD":/src:ro -w /src \
    forza-screening -m unittest discover -s screening -p 'test_screen_*.py' -v
  ```

- `screen_worker.py --score FILE...` scores local pictures and prints the decisions, with no
  server.

## Updating the pins

- Python packages: edit `requirements.in` or `requirements-build.in`, then run
  `uv pip compile --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28 requirements.in -o requirements.txt`
  (and the same for the build file).
- Models and word lists: change the revision, URL and SHA-256 in `models.lock.json`.
- `models.lock.json` `built` lists the hashes of the exported files. The build reports whether
  they match; a difference is expected when `prompts.json` changes.

## Known limits

- **Hate symbols.**
  - This is zero-shot only: clear and reasonably large symbols score high.
  - Small, stylised, partial or perspective-distorted vinyls will be missed.
  - Crossing lines, crosses and stars give weak scores that may hold a photo.
  - Expect misses until a probe is trained on in-game examples. Player reports remain the net.
- **NSFW.**
  - The classifier sees the whole frame squashed to 448 px, so a small decal can escape it. The
    nudity prompts on the tiles back it up, but they only hold.
  - Anime liveries ("itasha") are the grey zone.
  - The gore and nudity thresholds have not been checked on positive examples (none were used).
- **Text.**
  - OCR misses curved, rotated or heavily stylised livery text.
  - Word lists are easy to evade.
  - LDNOOBW's lists still hold on some cross-language false friends.
- **No account history.** There are no stricter rules for a new player's first uploads and no
  bans after repeated rejections: the worker does not keep the XUID.
- **Reasons for held photos are only in the worker's log.**
- **pHash finds only near-identical re-uploads** of hidden photos. Their hashes are read again
  every hour.
- **No CSAM hash matching.** No such tool runs locally under these constraints.
