# Mods and texture packs hub

Players share the mods and texture packs they make with the launcher's tools; the website
lists them (`mods.html`), and a separate download host serves the files (website #2). It is
**off** until the accounts service runs with `FORZA_HUB=1`.

**Nothing uploaded is trusted.** The web-facing service never opens an upload: it only stores
the bytes. An isolated worker checks and scans every upload. A maintainer approves what passes
before anyone can download it. Downloads come from another host name, only as attachments.

```
browser --PUT zip--> website nginx --/api/--> forza-accounts --writes--> [incoming]
                                                    ^                       | (read-only)
                                                    |                       v
                                             reads results          hub-scanner (no network,
                                             approves: copies       read-only root, ClamAV)
                                                    |                       |
                                                    v                       v
                                                [public] <-- clean zips -- [checked]
                                                    | (read-only)
                                                    v
browser --GET--> TLS proxy --> hub-downloads (nginx, another host name, attachments only)
```

| File | What |
|------|------|
| `hub_check.py` | the checks of a zip: allow-list, zip slip, zip bombs, sizes and counts, the launcher's rules; and the clean zip (`repack`) |
| `hub_worker.py` | the worker: jobs in, ClamAV, clean zips and results out |
| `test_hub.py` | their tests (a stand-in for clamscan; `FORZA_GAME_REPO=<game repo>` also adds the clean zips with the launcher's own Add mod and Add pack) |
| `test_image.sh` | the image with the real ClamAV, run as compose runs it (no network, read-only), on test uploads |
| `Dockerfile`, `freshclam.conf` | the image of the worker and of its signature updater |
| `downloads.nginx.conf`, `downloads.http.conf` | the download host |
| `compose.yaml` | the hub's containers: `hub-init`, `hub-scanner`, `hub-signatures`, `hub-downloads` |

The accounts service's side (the routes, the admin, the settings) is in
[`accounts/README.md`](../accounts/README.md#mods-and-texture-packs-hub) and `accounts/members/hub.py`.

## What an upload goes through

1. **The draft.** A signed-in account linked to its game (the same rule as likes) asks for a
   draft: kind, title, description, version, and a tick for "this is my own work". There are
   limits: 5 a day per account and 20 per address, 3 waiting per key, 100 waiting in all, and
   the hub's disk quota.
2. **The upload.** `PUT /api/hub/items/<id>/file`, with the zip as the body. The service streams
   it to `incoming/<id>.zip` without parsing it or holding it whole. It checks the size (at most
   `FORZA_HUB_MAX_MB`, 512) and that the file starts like a zip, and keeps its SHA-256. Then it
   writes the job `incoming/<id>.json`.
3. **The check** (`hub_check.py`), in the worker. The sizes the zip states are never believed:
   every byte is counted as it is decompressed, into `/work/<id>/` under numbers, never under a
   name from the zip. Refused:
   - **Zip slip:** absolute paths, drive letters, `..`, `.` or empty parts, backslashes.
   - **Special entries:** links, devices and other special files, encrypted entries, and any
     compression but stored and deflated.
   - **Zip bombs:** an entry that unpacks to more than 100 times its packed size (above 1 MB);
     more than 4 GB unpacked in all, 1 GB in one file, or 20,000 entries.
   - **Names** the game cannot use (plain printable ASCII without `:*?"<>|`, at most 255
     bytes), and names that differ only in case. A name that is both a file and a folder.
   - **Anything outside the allow-list:**
     - a mod: `mod.toml`, `.txt` and `.md` files beside `media/`, and under `media/` only the
       game's own file types (`GAME_TYPES`, taken from the game's archives; not `.sh`, `.lua`,
       `.py` or `.html`);
     - a texture pack: `<16 hex digits>.dds` or `.png` textures (in folders too), `pack.toml`,
       and `.txt`, `.md` and `.toml` files.
   - **Archives, programs and scripts,** by name (`REFUSED_TYPES`) and by what the file starts
     with (`MAGIC`: Windows, Linux and macOS programs, zip, 7z, rar, gzip, bzip2, xz, zstd, cab,
     OLE, PDF, `#!` scripts, web pages). A renamed `.exe` is refused like an `.exe`.
   - **What the launcher would not add:** the layout (the files at the zip's top or in its one
     folder); textures checked the way the game reads them (DDS and PNG headers, formats,
     16384 px at most, length); `.dds`/`.png` files under a mod's `media/` that are not images;
     `mod.toml` and `pack.toml` that are not UTF-8 TOML of 64 KB at most.
4. **The scan.** ClamAV (`clamscan`) scans the decompressed files with the signatures the
   updater keeps fresh. A finding refuses the upload and names the signature to its uploader.
   A scan that fails, or no signatures yet, never passes an upload: the jobs wait for
   signatures, and a failed scan says "send it again later". An upload that crashes the
   checker is refused, and one that stops it twice (killed for its memory, say) is refused the
   third time, so no upload can stop the queue. The zip's own list of files is measured before
   it is read (at most 20,000 entries, and their names), so it cannot exhaust the memory.
5. **The clean zip.** Only the checked files, in one folder named after the item (letters,
   digits, `-` and `_`; the launcher uses it as the mod's or pack's folder name). Fixed times,
   no comments, no extra fields, plain permissions. The worker writes `checked/<id>.zip`, then
   the result `checked/<id>.json` last.
6. **The review.** The accounts service reads the result. A refused upload shows its reason on
   the uploader's account page, and its files are deleted. So does a clean zip that is already
   on the hub (the same SHA-256 as another item): nobody can take over, or take down, someone
   else's download by uploading it again. A clean one waits for a maintainer
   (state `review`), who downloads the clean zip from the admin (an attachment), looks at it,
   and approves it. The approval copies it, checking its SHA-256 again, to
   `public/<sha256>/<folder>.zip`.
7. **The download.** The download host serves `/<sha256>/<folder>.zip` from the public volume,
   read-only, as `Content-Disposition: attachment` with `X-Content-Type-Options: nosniff`, a CSP
   of `default-src 'none'; sandbox`, no folder listing and no links followed. The website shows
   the SHA-256, so the launcher (and anyone) can check the file.

**Reports.** A signed-in, linked account reports an item once, with a reason. A "malware or a
harmful file" report from a key whose likes count hides it at once (its public copy is deleted).
Three reports from such keys, for any reason, hide it too. A maintainer checks it, then shows it again (Show again) or takes it
down.

**Takedowns.** The uploader deletes their item from their account page, and a maintainer takes
one down in the admin (a takedown request, a rule broken). Either way the files and the likes go,
and the record stays as `removed`. Deleting the account takes its items down. A takedown request
comes through the website's issues or the Discord, like the photos'. Packs and mods must be the
uploader's own work, never the game's original files.

## The containers

`compose.yaml`:

| Service | Network | Root | Mounts |
|---------|---------|------|--------|
| `hub-init` | none | read-only | sets the volumes' owners and modes once, then exits |
| `hub-scanner` | **none** | read-only, no capabilities, no new privileges, 64 processes, 2 GB, 1 CPU | `incoming` read-only, `checked`, `work`, `signatures` read-only |
| `hub-signatures` | ClamAV's mirror only | read-only, no capabilities | `signatures` |
| `hub-downloads` | the TLS proxy's side | read-only, no capabilities, unprivileged nginx | `public` read-only |

**The folders** (named volumes):

| Volume | Owner, mode | Written by | Read by |
|--------|-------------|------------|---------|
| `forza-hub-incoming` | 10001:10001, 2750 | the accounts service (uid 10001) | the scanner (group 10001), read-only |
| `forza-hub-checked` | 10002:10001, 2770 | the scanner (uid 10002) | the accounts service, which deletes what it has read |
| `forza-hub-public` | 10001:10001, 0755 | the accounts service | the download host, read-only |
| `forza-hub-work` | 10002:10002, 0700 | the scanner | |
| `forza-hub-signatures` | 10002:10002, 0755 | the updater | the scanner, read-only |

None of them is inside a web root: the website's nginx never sees them, and the download host
sees only `public`.

## Running it

1. Build and start the hub's containers:

       docker compose -f hub/compose.yaml up -d --build

   `hub-signatures` downloads ClamAV's signatures at its first start; until then the scanner
   leaves uploads waiting.
2. Give the accounts service the three folders and turn the hub on (its other settings are in
   `accounts/README.md`):

       -v forza-hub-incoming:/hub/incoming -v forza-hub-checked:/hub/checked \
       -v forza-hub-public:/hub/public \
       -e FORZA_HUB=1 -e FORZA_HUB_INCOMING=/hub/incoming -e FORZA_HUB_CHECKED=/hub/checked \
       -e FORZA_HUB_PUBLIC=/hub/public -e FORZA_HUB_DOWNLOADS=https://<download host>

3. Put `hub-downloads` behind the TLS proxy at the download host name. Use a host name that is
   not the website's. A domain of its own is best: then the download host is another site
   entirely, and none of the website's cookies can ever be sent to it. Pin
   `nginx-unprivileged` by digest.
4. In the website's nginx, the upload route takes bigger bodies than the rest of `/api/`. Let
   nginx buffer each upload to its own disk first, so slow uploads hold no worker of the
   service:

   ```nginx
   location ~ ^/api/hub/items/[A-Z2-7]{12}/file$ {
       limit_req  zone=accounts burst=5 nodelay;
       limit_conn accounts_conn 2;
       client_max_body_size 512m;
       client_body_timeout 60s;
       proxy_request_buffering on;
       proxy_pass http://forza-accounts:8080;
       proxy_set_header Host              $host;
       proxy_set_header X-Real-IP         $remote_addr;
       proxy_set_header X-Forwarded-Proto https;
       proxy_set_header X-Forwarded-For   "";
   }
   ```

5. On the website, add "Mods and Textures" to the navbar next to Leaderboards and take the
   `noindex` off `mods.html` (website, `README.md`).

`nightly` also deletes files in the three folders that no item needs any more (an upload deleted
while the worker had it, a public copy no live item is served from).

The hub's admin is the accounts service's (`/admin/`, Hub items): approve, hide, show again,
refuse and take down. The same steps from the command line: `python manage.py hub list`,
`approve <id>`, and so on.

## Testing

    python3 -m unittest discover -s hub -p 'test_*.py' -v
    FORZA_GAME_REPO=<the game's repository> python3 -m unittest discover -s hub -p 'test_*.py'
    hub/test_image.sh       # builds the image, runs it with no network on test uploads

`test_image.sh` uses a signature made for the test (the EICAR test string), so it downloads no
signatures.
