# Website accounts (forza.dyotson.dev/api/)

Optional accounts for the project website: players sign in with **Discord**, **GitHub** or an
**8-digit code sent by email**. For now (milestone M1) an account can only browse. Linking the
game's signing key, likes and "My items" come in later milestones.

This is a Django project with its own dependencies, image and tests. The game server
(`forza_live/`) stays dependency-free and does not import anything from here.

- **Sign-in flows:** [django-allauth](https://docs.allauth.org/) in headless mode, the JSON API the
  static website calls (`/api/auth/browser/v1/...`), plus the providers' OAuth callbacks
  (`/api/auth/{discord,github}/login/callback/`).
- **Our own endpoints:** [django-ninja](https://django-ninja.dev/) at `/api/` (`me`, `logout-all`,
  `account/delete`, `health`), with a Pydantic schema for every request and answer.
- **Data:** one SQLite file, with Django migrations, WAL and `synchronous=FULL`. Sessions are kept
  in the database.

| Path | What |
|------|------|
| `fh_accounts/settings.py` | every setting; the environment variables are below |
| `fh_accounts/urls.py` | the routes |
| `members/models.py` | the account (`User`, whose username is the public id such as `K7Q2-XM4P`), `SignIn` (sessions per account), `AuditEvent` |
| `members/adapters.py` | allauth's rules: the allowed return pages, no passwords, no profile data, the sign-in mail |
| `members/views.py` | allauth's code request, code confirm and provider redirect views, with the limits and the email stand-in |
| `members/api.py` | the django-ninja endpoints and their schemas |
| `members/middleware.py` | answer headers, the allauth routes that exist, Sec-Fetch-Site, the admin allow list, session lifetime |
| `members/identity.py` | the email stand-in: an HMAC of the address |
| `members/management/commands/` | `checkdb` (integrity check) and `nightly` (purge and backup) |
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
- The game server is not involved in M1. From M2 this service reads the game server's
  `friends.json` and player records, read-only, to check a key-signed link request.

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
| `GET /api/me` | `{"id": "K7Q2-XM4P", "created": "2026-11-03", "methods": ["discord"], "sessions": 1, "recent": true, "link": null, "events": [{"at", "event"}]}`; 401 when signed out |
| `POST /api/logout-all` | `{}` -> `{"ok": true, "ended": n}`: every session of the account ends, this one too |
| `POST /api/account/delete` | `{"confirm": "<account id>"}` -> `{"ok": true, "deleted": true}`; 403 `{"error": "sign in again"}` unless the last sign-in was in the past 10 minutes |

No answer ever contains an email address, a provider's user id or an XUID. With
`FORZA_API_DOCS=1` the OpenAPI page is at `/api/docs` (and `/api/openapi.json`). Like the admin, it
answers only `FORZA_ADMIN_ALLOW`. Leave it off on the public site.

Every change needs the CSRF token from the `__Host-fh_csrf` cookie in `X-CSRFToken` (or the
form's `csrfmiddlewaretoken`), an `Origin` of exactly `FORZA_ACCOUNTS_ORIGIN`, and, when the
browser sends `Sec-Fetch-Site`, `same-origin`. `OPTIONS` is 405, and no answer carries a CORS
header. Every `/api/` answer is `Cache-Control: no-store` and `nosniff`.

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

    docker build -t forza-accounts accounts/
    docker volume create forza-accounts-data
    docker run -d --name forza-accounts --restart unless-stopped \
        --network <web-network> --env-file <accounts.env> \
        -v forza-accounts-data:/data forza-accounts

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
- accounts not used for 30 days (none is linked to a game before M2);
- email accounts whose first code was never used, after a day.

Then it copies the database and the pepper to `backups/` (7 days kept). To restore, stop the
container, copy `backups/accounts-<date>.sqlite3` over `accounts.sqlite3` (and delete
`accounts.sqlite3-wal` and `-shm`), and start it again.

**The admin** (for moderation later): `/admin/`, Django's admin. nginx does not pass it on, and
the service answers it only for `FORZA_ADMIN_ALLOW`. Make a staff user with
`docker exec -it forza-accounts python manage.py createsuperuser`. To open it, either:
- publish the port on the host's loopback only (`-p 127.0.0.1:<port>:8080`), set
  `FORZA_ADMIN_ALLOW` to the Docker bridge's gateway address (`<bridge-gateway-ip>/32`, the
  address such connections come from), and reach it through an SSH tunnel to the host; or
- use `docker exec` with `curl` or the Django shell.

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
- **Audit events:** sign-ins, sign-outs, account creation; 90 days, with no IP address.
- **Rate-limit counters:** in the cache table. They hold IP addresses and address hashes for an
  hour or a day.
- Deleting the account removes all of it at once. Backups age out in 7 days.

## Limits

| Who | Limit |
|-----|-------|
| per IP | 20 Discord or GitHub sign-in starts an hour; 5 new accounts a day; 5 code mails an hour; 20 wrong codes an hour |
| per email address | 3 codes an hour, 5 a day; one live code per browser; 5 tries per code; a code lasts 10 minutes |
| per account | 20 sessions |
| whole service | `FORZA_ACCOUNTS_EMAIL_DAILY` code mails a day (then email sign-in pauses); 1,000 new accounts a day |

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

## Development

    python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
    .venv/bin/python manage.py test members                 # the tests
    FORZA_ACCOUNTS_DEBUG=1 .venv/bin/python manage.py migrate
    FORZA_ACCOUNTS_DEBUG=1 .venv/bin/python manage.py runserver

To change a dependency, edit `requirements.in`, then run
`pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.txt requirements.in`
(pip-tools). CI (`.github/workflows/tests.yml`, the `accounts` and `docker` jobs) installs by
hash, checks the migrations, runs the tests and builds the image.
