# Website accounts (forza.dyotson.dev/api/)

Optional accounts for the project website: players sign in with **Discord**, **GitHub** or an
**8-digit code sent by email**, then link the account to their game from the launcher (milestone
M2). The link is the account's second factor: likes on the community photos and designs, "My
items" (the player's own photos and designs, which they can delete) (milestone M3) and later
uploads are only for linked accounts. An account that is not linked can only browse.

This is a Django project with its own dependencies, image and tests. The game server
(`forza_live/`) stays dependency-free and does not import anything from here. This service
imports the game server's signed-request check (`forza_live/core/auth.py`) and a few of its
readers, and uses them on the game server's data folder, mounted read-only.

- **Sign-in flows:** [django-allauth](https://docs.allauth.org/) in headless mode, the JSON API the
  static website calls (`/api/auth/browser/v1/...`), plus the providers' OAuth callbacks
  (`/api/auth/{discord,github}/login/callback/`).
- **Our own endpoints:** [django-ninja](https://django-ninja.dev/) at `/api/` (`me`, `logout-all`,
  `account/delete`, `health`, the game link, likes and My items), with a Pydantic schema for every
  request and answer.
- **Data:** one SQLite file, with Django migrations, WAL and `synchronous=FULL`. Sessions are kept
  in the database.

| Path | What |
|------|------|
| `fh_accounts/settings.py` | every setting; the environment variables are below |
| `fh_accounts/urls.py` | the routes |
| `members/models.py` | the account (`User`, whose username is the public id such as `K7Q2-XM4P`), `SignIn` (sessions per account), `AuditEvent`, `GameLink`, `LinkRequest`, `SignedNonce`, `Like` |
| `members/adapters.py` | allauth's rules: the allowed return pages, no passwords, no profile data, the sign-in mail |
| `members/views.py` | allauth's code request, code confirm and provider redirect views, with the limits and the email stand-in |
| `members/api.py` | the django-ninja endpoints and their schemas |
| `members/links.py` | the game link: the launcher's signed calls and the website's peek, claim and remove |
| `members/likes.py` | likes, the public like totals, and My items with its delete |
| `members/items.py` | the game server's photos and designs, read-only, with its own rules for which are public |
| `members/game.py` | the game server's data, read-only (keys, players, bans), the signed-request check, and which keys' likes count |
| `members/moderation.py` | the operator's tools (void likes, suspend, sign out, unlink, stats) and the purge of likes whose item is gone |
| `members/admin.py` | the Django admin: accounts, game links, likes, audit events (see "Moderation") |
| `members/middleware.py` | answer headers, the allauth routes that exist, Sec-Fetch-Site, the admin allow list, session lifetime |
| `members/identity.py` | the email stand-in: an HMAC of the address |
| `members/management/commands/` | `checkdb` (integrity check), `nightly` (purge and backup), and the operator's `void_likes`, `purge_likes`, `stats`, `backup` and `restore` |
| `members/tests/` | the tests (Django's test client, a fake OAuth provider, a fake `smtplib.SMTP`) |
| `requirements.in`, `requirements.txt` | pinned dependencies; the `.txt` has hashes |
| `Dockerfile`, `docker-entrypoint.sh` | the image: a digest-pinned `python:3.12-slim`, a user without privileges, gunicorn |

## How the pieces fit

    browser -> TLS proxy -> website nginx --/api/--> forza-accounts:8080 (this service)
                                          \-- everything else: the static site

- The website's nginx is the only way in. The container publishes no port to the internet, and
  is never on the game server's tunnel.
- `/admin/` and `/static/` are not passed on by nginx. They answer only the addresses in
  `FORZA_ADMIN_ALLOW` (see "The admin" below).
- The launcher's link calls come the same way, to `https://forza.dyotson.dev/api/game/...`,
  signed with the player's key (`X-Forza-Auth`).
- This service reads the game server's data folder (`FORZA_GAME_DATA`, mounted read-only):
  `friends.json` for the public key each XUID is bound to, `players/<XUID>.json` for the gamertag
  and the signed sign-ins, `banned_xuids.json`, and `community.json` and `designs.json` for the
  photos and designs and who owns them.
- It calls the game server for one thing only: deleting a player's own photo or design, through
  the game server's owner-only route (`forza_live/content/owners.py`, `FORZA_GAME_URL`,
  `FORZA_OWNER_TOKEN`), straight to its HTTP port from the internal network, never through the
  tunnel or a proxy.

## The API

Sign-in (allauth headless, browser client). Only these routes exist; allauth's password, signup,
email-management and other routes answer 404.

| Route | Use |
|-------|-----|
| `GET /api/auth/browser/v1/config` | the sign-in methods on offer; sets the CSRF cookie |
| `GET /api/auth/browser/v1/auth/session` | 200 when signed in, 401 when not |
| `DELETE /api/auth/browser/v1/auth/session` | sign out in this browser |
| `POST /api/auth/browser/v1/auth/provider/redirect` | a plain form (`provider`, `callback_url`, `process=login`, `csrfmiddlewaretoken`) that sends the browser to Discord or GitHub |
| `GET /api/auth/{discord,github}/login/callback/` | where the provider sends the browser back |
| `POST /api/auth/browser/v1/auth/code/request` | `{"email"}`: mails a code. Known and unknown addresses get the same answer (401, a code login pending) |
| `POST /api/auth/browser/v1/auth/code/confirm` | `{"code"}`: signs in |

Our own endpoints (django-ninja, session auth, CSRF checked on every change, JSON bodies only):

| Route | Answer |
|-------|--------|
| `GET /api/health` | `{"ok": true}` |
| `GET /api/me` | `{"id": "K7Q2-XM4P", "created": "2026-11-03", "methods": ["discord"], "sessions": 1, "recent": true, "link": null, "events": [{"at", "event"}]}`; 401 when signed out. A linked account's `link` is `{"state", "gamertag", "since"}`; `state` is `active`, `frozen`, `waiting` or `banned` (see "The game link") |
| `POST /api/logout-all` | `{}` -> `{"ok": true, "ended": n}`: every session of the account ends, this one too |
| `POST /api/account/delete` | `{"confirm": "<account id>"}` -> `{"ok": true, "deleted": true}`; 403 `{"error": "sign in again"}` unless the last sign-in was in the past 10 minutes |

| `POST /api/link/peek` | `{"token"}` or `{"code"}` -> `{"state", "gamertag", "expires", "replaces"}`: the link request the launcher made. `replaces` is the gamertag this account is linked to now, when it is another game |
| `POST /api/link/claim` | `{"token"}` or `{"code"}` -> `{"ok": true, "state": "claimed", "account": "K7Q2-XM4P"}`: this account asks for the link; the launcher then confirms it |
| `POST /api/link/remove` | `{}` -> `{"ok": true, "unlinked": true}`; 403 `{"error": "sign in again"}` unless the last sign-in was in the past 10 minutes |

The launcher's calls (signed with the player's key in `X-Forza-Auth`, the game server's scheme;
no cookie is read, and a request with an `Origin` or `Sec-Fetch-Site` header is refused). Every
answer carries `X-Forza-Time`, and a refused signature is a 401 `{"error": "refused"}`, whatever
the reason, so the launcher can correct its clock and try once more.

| Route | Answer |
|-------|--------|
| `POST /api/game/link/start` | `{"v": 1}` -> `{"id", "url", "code", "expires"}`: `url` is `https://forza.dyotson.dev/account.html#link=<token>` for the browser, `code` (`K7M2-QXPA`) for another device. 403 `not available` (banned), 409 `play online first` (no gamertag or signed sign-in on the game server yet), 429 `changed in the past day` or `too many requests` |
| `GET /api/game/link/<id>` | `{"state", "account", "replaces"}`: `pending`, `claimed` (with `account: {"id", "method", "created"}` and `replaces`, the account the key is linked to now), `linked`, `declined`, `cancelled` or `expired` |
| `POST /api/game/link/<id>/confirm` | `{"account": "K7Q2-XM4P"}`, the account the player was shown -> `{"ok": true, "state": "linked"}`; 409 when it is not that request's claimer |
| `POST /api/game/link/<id>/cancel` | `{}` -> `{"ok": true, "state": "declined"}` ("Not me") |
| `GET /api/game/account` | `{"linked", "state", "account"}`: the key's link |
| `POST /api/game/unlink` | `{}` -> `{"ok": true, "unlinked": true}` |
| `POST /api/game/signout` | `{}` -> `{"ok": true, "ended": n}`: every session of the linked account ends |

Likes and My items (session; every change also needs the CSRF token, the Origin and JSON).
Everything but the totals needs an account linked to its game, with the link usable: active,
the XUID still bound to the pinned key (else the link freezes) and not banned. Otherwise the
answer is 403 `link your game first` or `not available` (banned), or 409 `this link needs the
operator` (frozen) or `play online first` (the game server forgot the key after a long time
away).

| Route | Answer |
|-------|--------|
| `POST /api/likes` | `{"kind": "photo"\|"design", "item": "<id>", "on": true\|false}` -> `{"ok": true, "on", "counted"}`. `counted`: this key's likes are in the public totals (see below). 404 `not found` when the item is not in the public feeds (taking a like back works for any item), 403 `your own`, 429 over the limits |
| `GET /api/likes/mine?kind=photo\|design` | `{"kind", "liked": [ids], "own": [ids], "counted"}`: what this key liked, and its own public items (which it cannot like) |
| `GET /api/likes/counts?kind=photo\|design` | anyone, no session: `{"version": 1, "kind", "generated", "counts": {"<id>": n}}`, the public totals (items with no counted like are left out). `Cache-Control: public, max-age=30` and an `ETag` (`If-None-Match` gives 304); counted again at most every 60 s in each worker. The website's build copies them into its pages |
| `GET /api/me/items` | `{"items": [{"kind", "id", "title", "state", "public", "likes", "created"}], "counted"}`: the key's photos and designs, newest first, whatever their state: `live`, `pending` (waiting for the screening), `held` (left for the operator) or `hidden`. `public`: in the public feed (the website shows it) |
| `POST /api/me/items/delete` | `{"kind", "id"}` -> `{"ok": true, "deleted": true}`; 403 `sign in again` unless the last sign-in was in the past 10 minutes, 404 when it is not this key's, 502 `try again later` when the game server did not delete it, 503 when deleting is not configured |

Which likes count in the public totals (`members.game.counted`): those of a key whose link is
active, whose account is not suspended, whose XUID is still bound to the pinned key and not banned, and whose first sign-in
signed by its key (`firstSigned`, written by the game server) is 72 hours old, with signed
sign-ins on 2 days (`signedDays`). A key that is too new keeps its likes, which count once it
qualifies. A frozen, banned or unlinked key's likes are kept but not counted; deleting the account
deletes them. Self-likes are refused, and only items in the public feeds are counted (the same
rules as the game server's feeds: `community.public`, `design_feed.on_site`, and their caps).

Deleting an item: the service checks the owner, then asks the game server
(`POST /owner/photos/<id>/delete` or `/owner/designs/<id>/delete`, body `{"owner": "<XUID>"}`,
header `X-Owner-Token`), which checks the owner again and refuses any request that came through
the tunnel or a proxy or carries an `Origin`. A photo is dropped as the operator's delete drops
it, but its picture may be uploaded again; a design leaves the game's storefront too. The item's
likes are deleted with it.

No answer ever contains an email address, a provider's user id or an XUID. With
`FORZA_API_DOCS=1` the OpenAPI page is at `/api/docs` (and `/api/openapi.json`). Like the admin, it
answers only `FORZA_ADMIN_ALLOW`. Leave it off on the public site.

Every change needs the CSRF token from the `__Host-fh_csrf` cookie in `X-CSRFToken` (or the
form's `csrfmiddlewaretoken`), an `Origin` of exactly `FORZA_ACCOUNTS_ORIGIN`, and, when the
browser sends `Sec-Fetch-Site`, `same-origin`. `OPTIONS` is 405, and no answer carries a CORS
header. Every `/api/` answer is `Cache-Control: no-store` and `nosniff`.

## The game link

Both sides approve every link, so a token, code or address that leaks is useless alone:

1. In the launcher, Settings > Online > Website account > **Link**. The launcher signs
   `game/link/start`. The service checks the signature with the key `friends.json` binds the
   XUID to (`forza_live.core.auth.check_request`: the server name `forza.dyotson.dev` only, the
   120-second clock window, the nonce used once in every worker), that the XUID is not banned,
   and that `players/<XUID>.json` has a gamertag and a signed sign-in (`firstSigned`, which the
   game server writes only for a sign-in signed by the player's key). It makes a request for 10
   minutes, with a token (256 bits) and a code (8 characters); it keeps only their HMACs.
2. The launcher opens `account.html#link=<token>` in the browser (or shows the code, to type at
   `account.html` on another device). The page moves the token out of the address, has the
   player sign in if needed, and shows "Link the game of *Gamertag*?". **Link** claims the
   request for this account.
3. The launcher polls the request, then asks "Link this game to website account K7Q2-XM4P
   (Discord, created ...)?". **Link** confirms it by that account id; **Not me** declines it.
   One transaction drops any older link of the key and of the account and writes the new link,
   with the key's public key pinned. Every other session of the account ends.

Rules:
- One key per account, and one account per key. A key, or an account, links at most once a day.
- The key holder always wins: the launcher can unlink and sign the account out everywhere,
  even if someone else took over the web account. A player who lost their Discord login or
  mailbox makes a new account and moves the link to it.
- The website can unlink too (with a sign-in in the past 10 minutes).
- **Pinning.** The link keeps the public key it was made with. When `friends.json` binds the XUID
  to another key (the operator re-keyed it, or someone bound a recycled XUID), the link is
  **frozen**: the new key cannot see the account, unlink or sign it out. The operator checks
  with the player and unfreezes it in the admin (GameLink, "Unfreeze, pinning the key the XUID
  is bound to now"). When the game server has forgotten the XUID (`FORGET_DAYS`), the link
  **waits**; the same identity's next sign-in binds the same key again, and the link works.
- A banned XUID cannot start a link, and its account shows the link as banned.
- Deleting the account removes its link.

## Settings

Every setting is an environment variable. Each secret can instead come from a file:
`NAME_FILE=/run/secrets/name` reads the file and wins over `NAME`. Put the variables in one env
file (mode 600) that only this container reads. Never commit it.

| Variable | Default | What |
|----------|---------|------|
| `DJANGO_SECRET_KEY` (`_FILE`) | required | 50+ random characters (`python3 -c "import secrets; print(secrets.token_urlsafe(50))"`). Changing it signs everyone out |
| `FORZA_ACCOUNTS_ORIGIN` | `https://forza.dyotson.dev` | the website's origin: the only CSRF-trusted origin and the host this service answers for |
| `FORZA_ACCOUNTS_DATA` | `/data` in the image | the folder of `accounts.sqlite3`, `email_pepper.bin` and `backups/` |
| `FORZA_ACCOUNTS_DB` | `$FORZA_ACCOUNTS_DATA/accounts.sqlite3` | the database file |
| `FORZA_EMAIL_PEPPER_FILE` | `$FORZA_ACCOUNTS_DATA/email_pepper.bin` | 32 random bytes made at the first email sign-in. Back it up with the database: without it, no email account can be matched again |
| `FORZA_ACCOUNTS_BACKUPS` | `$FORZA_ACCOUNTS_DATA/backups` | where `nightly` writes its copies |
| `FORZA_ACCOUNTS_PROXY` | none | the website nginx's address or network (`<web-nginx-ip>` or `<web-network-cidr>`): `X-Real-IP` is believed only from there. Without it every visitor counts as nginx for the rate limits |
| `FORZA_GAME_DATA` | none | the game server's data folder, mounted read-only (`friends.json`, `players/`, `banned_xuids.json`, `community.json`, `designs.json`). Without it, linking, likes and My items are off (503) |
| `FORZA_GAME_URL` | none | the game server's HTTP port as this container reaches it (`http://<game-server-address>:21201`), for deleting a player's own items. Straight to the port, never through the tunnel or a proxy: the game server refuses those. The address this container comes from must not be one of the game server's `--trusted-proxy` addresses |
| `FORZA_OWNER_TOKEN` (`_FILE`) | none | the game server's `FORZA_OWNER_TOKEN` (`openssl rand -hex 32`), a token of its own, never the admin token. Without `FORZA_GAME_URL` and this, deleting from the account page is off (503) |
| `FORZA_ACCOUNTS_LINKS_DAILY` | `200` | new game links a day in all |
| `FORZA_ADMIN_ALLOW` | `127.0.0.1/32 ::1/128` | addresses that may open `/admin/` (and `/api/docs`) |
| `FORZA_API_DOCS` | off | `1` serves the OpenAPI page to `FORZA_ADMIN_ALLOW` |
| `FORZA_DISCORD_CLIENT_ID`, `FORZA_DISCORD_CLIENT_SECRET` (`_FILE`) | none | the Discord app. Without both, Discord sign-in is off |
| `FORZA_GITHUB_CLIENT_ID`, `FORZA_GITHUB_CLIENT_SECRET` (`_FILE`) | none | the GitHub OAuth app. Without both, GitHub sign-in is off |
| `FORZA_SMTP_HOST` | none | the mail relay's SMTP host. Without it, email sign-in is off |
| `FORZA_SMTP_PORT` | `587` | 465 means TLS from the start; anything else means STARTTLS |
| `FORZA_SMTP_USER`, `FORZA_SMTP_PASSWORD` (`_FILE`) | none | the relay's SMTP credentials (often a user name and an API key) |
| `FORZA_SMTP_FROM` | `signin@<site host>` | the sender |
| `FORZA_ACCOUNTS_EMAIL_DAILY` | `200` | code mails a day in all; then email sign-in pauses until the day has passed. Keep it under the relay's own daily cap |
| `FORZA_ACCOUNTS_WORKERS`, `FORZA_ACCOUNTS_BIND` | `2`, `0.0.0.0:8080` | gunicorn |

## What the owner registers before going live

**Discord** ([Developer Portal](https://discord.com/developers/applications) > New Application):
- Name it "Forza Horizon 1: ReImagined" and add the logo.
- OAuth2 > Redirects: add exactly `https://forza.dyotson.dev/api/auth/discord/login/callback/`
  (with the trailing slash).
- Scopes: the service asks for `identify` only. Nothing else needs turning on, and "Public
  Client" stays off.
- Copy the Client ID into `FORZA_DISCORD_CLIENT_ID` and the Client Secret into
  `FORZA_DISCORD_CLIENT_SECRET`.

**GitHub** (the FH1-ReImagined organization > Settings > Developer settings > OAuth Apps > New
OAuth App; an OAuth App, not a GitHub App):
- Application name: "Forza Horizon 1: ReImagined". Homepage URL: `https://forza.dyotson.dev`.
- Authorization callback URL: `https://forza.dyotson.dev/api/auth/github/login/callback/`.
- Leave "Enable Device Flow" off. No scopes are requested, so GitHub shares only the public
  profile, and only its numeric id is kept.
- Generate a client secret. The Client ID goes into `FORZA_GITHUB_CLIENT_ID` and the secret into
  `FORZA_GITHUB_CLIENT_SECRET`.

Both providers use the authorization code flow with PKCE (S256) and a state kept in the session.

**Email sender:** a transactional relay that speaks authenticated SMTP over TLS, on a free tier.
For example Resend (100 mails a day; set `FORZA_ACCOUNTS_EMAIL_DAILY=90`) or Brevo (300 a day).
- Sender `signin@forza.dyotson.dev`.
- Add the DNS records the relay asks for to `forza.dyotson.dev`'s zone:
  - its DKIM key (TXT or CNAME records);
  - its SPF include, on the domain or bounce subdomain it names (often `send.` or `bounce.`, with
    an MX record);
  - a DMARC record, `_dmarc.forza.dyotson.dev TXT "v=DMARC1; p=quarantine; adkim=s; aspf=r"`.
- Wait until the relay shows the domain as verified.
- In the relay's settings, turn open tracking and click tracking off.
- Its SMTP host, port (465 or 587), user and password (or API key) go into `FORZA_SMTP_HOST`,
  `FORZA_SMTP_PORT`, `FORZA_SMTP_USER` and `FORZA_SMTP_PASSWORD`.

The mail is fixed plain text with the code and a same-browser link
(`https://forza.dyotson.dev/account.html#code=12345678`).

## Running it

    docker build -t forza-accounts -f accounts/Dockerfile .
    docker volume create forza-accounts-data
    docker run -d --name forza-accounts --restart unless-stopped \
        --network <web-network> --env-file <accounts.env> \
        -v forza-accounts-data:/data \
        -v <game-server-data>:/game-data:ro -e FORZA_GAME_DATA=/game-data \
        -e FORZA_GAME_URL=http://<game-server-address>:21201 forza-accounts

Build it from the repository's root: the image copies the game server's `forza_live/` package
for its signed-request check. `<game-server-data>` is the game server's data volume or folder
(its `--data-dir`); mount it read-only (`:ro`). The container's user (uid 10001) needs to read
it.

A game server that runs with `--network host` is reached at the host's address on the
container's network (its gateway), as the screening worker reaches it.

Use no `-p`: only the website's nginx, on the same Docker network, reaches it. At every start
the image:
1. runs `checkdb` (`PRAGMA integrity_check`; a damaged database stops it and names the newest
   backup);
2. applies the migrations;
3. makes the rate-limit cache table;
4. runs `nightly` once;
5. starts gunicorn.

**The website's nginx** (example, with placeholders):

```nginx
# In http {}: per-visitor limits in front of the service, and a log line without query strings
# (codes and OAuth states travel in them).
limit_req_zone  $binary_remote_addr zone=accounts:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=accounts_conn:10m;
log_format api '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent "$http_user_agent"';

server {
    # The TLS proxy in front: its address, so $remote_addr is the visitor.
    set_real_ip_from <tls-proxy-ip>;
    real_ip_header   X-Forwarded-For;
    real_ip_recursive on;

    # Kept 14 days by the log rotation.
    access_log /var/log/nginx/access.log api;

    # Add to the site's Content-Security-Policy (default-src does not cover these). form-action
    # names the providers because the sign-in button's form is redirected to them.
    add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://discord.com https://github.com" always;

    location /api/ {
        limit_req  zone=accounts burst=20 nodelay;
        limit_conn accounts_conn 10;
        proxy_pass http://forza-accounts:8080;   # no URI part: the request line arrives unchanged
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For   "";
        client_max_body_size 16k;
    }
    location /admin/  { return 404; }
    location /static/ { return 404; }
}
```

Set `FORZA_ACCOUNTS_PROXY` to that nginx's address on the Docker network. The site's existing
CSP must keep its other directives; the line above shows only what to add.

**Upkeep:** run `nightly` once a day from the host's cron:

    17 4 * * * docker exec forza-accounts python manage.py nightly

It removes:
- expired sessions;
- audit events older than 90 days;
- accounts not used for 30 days that are not linked to a game (a suspended account stays);
- link requests that ended over an hour ago, and the launcher's used nonces past their window;
- email accounts whose first code was never used, after a day;
- the likes of photos and designs that are no longer in the game server's `community.json` or
  `designs.json` (the operator deleted one, the game removed it, the screening took it down),
  once they have been missing for 7 days (`LIKES_PURGE_GRACE_DAYS`). An item that comes back
  within that time keeps its likes. A file that is missing, damaged (with no good `.bak`) or
  empty is not judged at all, so a bad mount deletes nothing. `purge_likes [--dry-run]
  [--grace-days N]` runs the same on demand.

Then it copies the database and the pepper to `backups/` (7 days kept); `backup` makes one now.
To restore one:

    docker exec forza-accounts python manage.py restore 2026-11-03           # checks it, changes nothing
    docker exec forza-accounts python manage.py restore 2026-11-03 --yes
    docker restart forza-accounts

`restore` checks the backup (`PRAGMA integrity_check`, this service's tables), keeps the current
database as `backups/pre-restore-<time>.sqlite3`, and copies the backup in through SQLite's
backup API, which is safe while the service runs. It brings the pepper back from `backups/` when
it is missing. The restart applies the migrations (for a backup from an older version). By hand
instead: stop the container, copy `backups/accounts-<date>.sqlite3` over `accounts.sqlite3`,
delete `accounts.sqlite3-wal` and `-shm`, and start it again.

**The admin:** `/admin/`, Django's admin. nginx does not pass it on, and
the service answers it only for `FORZA_ADMIN_ALLOW`. Make a staff user with
`docker exec -it forza-accounts python manage.py createsuperuser`. To open it, either:
- publish the port on the host's loopback only (`-p 127.0.0.1:<port>:8080`), set
  `FORZA_ADMIN_ALLOW` to the Docker bridge's gateway address (`<bridge-gateway-ip>/32`, the
  address such connections come from), and reach it through an SSH tunnel to the host; or
- use `docker exec` with `curl` or the Django shell.

## Moderation

There is no admin HTTP route on the public side. Everything is in the admin (above) or a
command run with `docker exec forza-accounts python manage.py <command>`. Every change to an
account writes an audit event, which the player sees on the account page (for example "the
operator suspended this account"), and a log line that names the account id only.

| To | Admin | Command |
|----|-------|---------|
| find an account | Accounts: search by account id (`K7Q2-XM4P`, with or without the dash), linked gamertag or XUID; Game links: the same | |
| see an account | its page: the game link, the audit events, and its likes given (a link to the like list) | |
| see who liked what | Likes: read-only; filter by kind, item, key (XUID) and account, with the most liked items and most liking keys and accounts listed; the item's state now (`gone` when the game server no longer has it) | |
| void likes (a key farm, a script) | Likes: "Void the selected likes" or "Void every like of the selected likes' keys"; Accounts and Game links: "Void the likes of the linked key" | `void_likes --account K7Q2-XM4P` or `--key <XUID>` (`--dry-run` counts them) |
| suspend an account | Accounts: "Suspend": every session ends, it cannot sign in again (by email it gets no code and no new account in its place), and its key's likes are left out of the totals (they are kept). "Lift the suspension" undoes it | |
| sign an account out everywhere | Accounts: "Sign out everywhere" | |
| unlink a game | Game links: "Unlink". The key keeps its likes, which stop counting | |
| unfreeze a link | Game links: "Unfreeze, pinning the key the XUID is bound to now" (after checking with the player) | |
| delete an account | Accounts: delete. Its link, audit events and its key's likes go too | |
| numbers | | `stats` (`--json`): accounts, suspended, links, frozen, new today, likes, liked items missing |

Staff accounts are left out of suspend and sign-out. Voided likes can be given again: to stop a
key for good, ban its XUID on the game server (`POST /admin/photos/ban/<XUID>` from the game
server's own machine, or `banned_xuids.json`); a banned key cannot link or like, and its likes do
not count. A link made by a key farm shows in `stats` (`links new today`), and the log warns once
a day when the past day's new links reach half of `FORZA_ACCOUNTS_LINKS_DAILY`.

## What is stored

- **Accounts:** the public id, when the account was made, and when it last signed in.
- **Sign-in identities:**
  - Discord and GitHub: the provider's numeric user id. allauth's `extra_data` is always emptied,
    so no name, username, avatar, email address or token is kept.
  - Email: `HMAC-SHA256(pepper, address)@email.invalid`. The address itself lives only in the
    request that sends the mail.
- **Sessions:** in the database. A session ends 30 days after its last use (refreshed at most
  once a day), and 90 days after its sign-in; an account keeps at most 20. A sign-in code is
  held in the browser's session for 10 minutes.
- **The game link:** the XUID, its public key when it was linked, the gamertag (for the account
  page), when it was made and moved, and whether it is frozen.
- **Link requests:** for 10 minutes (deleted an hour later): the XUID, its public key and
  gamertag, HMACs of the token and the code, and the account that claimed it.
- **Launcher nonces:** the random nonce of each signed call, for 4 minutes, so none works twice.
- **Likes:** the XUID of the key that gave it, the photo's or design's id, and when. Who liked
  what is never shown (only the operator sees it, in the admin); only the totals per item are
  public. A deleted item's likes are deleted with it, those of an item gone from the game server
  7 days after it went, and deleting the account deletes its key's likes.
- **Missing items:** the id of each liked photo or design the nightly purge no longer finds, and
  since when; until it comes back or its likes go.
- **Audit events:** sign-ins, sign-outs, account creation, links made, moved and removed, items
  deleted (with their title); 90 days, with no IP address.
- **Rate-limit counters:** in the cache table. They hold IP addresses and address hashes for an
  hour or a day.
- Deleting the account removes all of it at once. Backups age out in 7 days.

## Limits

| Who | Limit |
|-----|-------|
| per IP | 20 Discord or GitHub sign-in starts an hour; 5 new accounts a day; 5 code mails an hour; 20 wrong codes an hour |
| per email address | 3 codes an hour, 5 a day; one live code per browser; 5 tries per code; a code lasts 10 minutes |
| per account | 20 sessions; 10 link claims (and link peeks that find nothing) per 10 minutes; one new link a day; 120 likes an hour and 500 a day (given or taken back); 20 items deleted a day |
| per XUID (signed) | 600 calls an hour; 5 link starts an hour; one new link a day |
| whole service | `FORZA_ACCOUNTS_EMAIL_DAILY` code mails a day (then email sign-in pauses); 1,000 new accounts a day; `FORZA_ACCOUNTS_LINKS_DAILY` new links a day; 100 link codes that find nothing a minute, after which codes stop for 10 minutes (the browser's token still works) |

The limits are allauth rate limits (`ACCOUNT_RATE_LIMITS` in the settings). Over a limit:
- a Discord or GitHub start goes back to the page with `?error=busy`;
- a code request or confirm gets an error answer;
- a new account over the new-account limits is not made. The code request then gets the
  same answer as for any address, and no mail is sent.

Differences from the design (community accounts, M1):
- **The session cookie is `SameSite=Lax`, not Strict.** allauth keeps the OAuth state in the
  session, and the provider's callback is a cross-site navigation. Lax still keeps the cookie
  off cross-site POSTs, frames and fetches.
- **A request over the per-address code limit gets "too many attempts".** This tells the asker
  only that codes were already asked for that address, not whether it has an account.
- **The mail is sent within the request.** Every address that is not over a limit gets a mail,
  so the timing does not depend on whether the account exists.

Differences from the design (M2):
- **The link page is `account.html`**, not a separate `link.html`: the browser opens
  `account.html#link=<token>`, and a code is typed on the same page.
- **Link requests and nonces are in the database**, not in memory: gunicorn runs several
  workers, and a request started in one must be seen in the others (and a nonce used in one
  refused in the others). The game server's own nonce cache is checked too.
- **A key bound only to a raw friends key gets the same 401 as any refused signature** (the
  design had "update the game"): it has no public key to check a signature with, so the service
  cannot tell it from someone else's request. The launcher's message covers both.
- **The once-a-day rule** is a rate limit per XUID and per account (`link_change`), consumed by
  each new link.
- `firstSigned` (and `signedDays`, `lastSignedDay`) is written by the game server's Logon2 for a
  sign-in signed by the player's key (`forza_live.title_services.lsp.signed_logon`, the design's
  M0 piece, in the same change).

Differences from the design (M3):
- **The items that can be liked come from the game server's data files**, not from fetching its
  public feeds over HTTP every 60 s: the service already reads that folder, and applies the game
  server's own rules to it (`community.public`, `design_feed.on_site`, the feeds' order and caps),
  so the two cannot differ and the service needs the game server's HTTP port only for deleting.
- **`likes/mine` also lists the key's own public items** (`own`), so the pages can leave out the
  thumbs-up on them, and every like answer says whether the key's likes count yet (`counted`),
  so a page does not add a like to a total that will not show it.
- **Taking a like back works for any item**, also one that left the public feeds.
- **An item that is deleted takes its likes with it** (the design kept them stored).

Differences from the design (M4, moderation):
- **The operator's tools are the Django admin and management commands**, not one
  `forza_accounts.py admin ...` CLI: `find` and `show` are the admin's search and account page,
  `suspend`, `unsuspend`, `signout`, `unlink`, `unfreeze` and `void-likes` are admin actions
  (`void-likes` is also the `void_likes` command), and `stats`, `backup` and `restore` are
  commands.
- **A suspended account's key's likes do not count**, and a suspended email account gets no new
  account in its place when it asks for a code again.
- **The likes of an item gone from the game server are purged** after 7 days missing, rather than
  kept forever without counting.

## Development

    python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
    .venv/bin/python manage.py test members                 # the tests
    FORZA_ACCOUNTS_DEBUG=1 .venv/bin/python manage.py migrate
    FORZA_ACCOUNTS_DEBUG=1 .venv/bin/python manage.py runserver

To change a dependency, edit `requirements.in`, then run
`pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.txt requirements.in`
(pip-tools). CI (`.github/workflows/tests.yml`, the `accounts` and `docker` jobs) installs by
hash, checks the migrations, runs the tests and builds the image.
