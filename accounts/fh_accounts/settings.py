"""Settings of the website accounts service. Everything that differs between a test run and the
public site comes from environment variables, and every secret can come from a file instead
(NAME_FILE=/run/secrets/...); README.md lists them all.

Sign-in is django-allauth in headless mode (its JSON API for the static website), with Discord,
GitHub and an emailed code. Our own endpoints (me, logout-all, account delete, the game link) are
django-ninja. Linking an account to the game's key checks the launcher's signed requests with the
game server's own code (forza_live.core.auth), imported from the repository (or the image's copy).
"""

import atexit
import ipaddress
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent
# The game server's package (forza_live/), for its signed-request checks: beside manage.py in the
# image, one folder up in the repository.
if not (BASE_DIR / "forza_live").is_dir() and (BASE_DIR.parent / "forza_live").is_dir():
    sys.path.insert(1, str(BASE_DIR.parent))


def env(name, default=""):
    """$NAME, or the contents of the file $NAME_FILE names (for secrets)."""
    path = os.environ.get(name + "_FILE")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get(name, default).strip()


def env_flag(name, default=False):
    value = env(name)
    return default if not value else value.lower() in ("1", "true", "yes", "on")


def networks(spec):
    return [ipaddress.ip_network(item, strict=False) for item in spec.replace(",", " ").split()]


DEBUG = env_flag("FORZA_ACCOUNTS_DEBUG")
TESTING = env_flag("FORZA_ACCOUNTS_TESTING") or sys.argv[1:2] == ["test"]

# The website's origin: the only origin trusted for CSRF, the host the service answers for, and
# the base of every redirect.
ORIGIN = env("FORZA_ACCOUNTS_ORIGIN", "https://forza.dyotson.dev").rstrip("/")
SITE_HOST = urlsplit(ORIGIN).hostname or "forza.dyotson.dev"

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not (DEBUG or TESTING):
        raise RuntimeError("set DJANGO_SECRET_KEY or DJANGO_SECRET_KEY_FILE (README.md, Settings)")
    SECRET_KEY = secrets.token_urlsafe(50)

ALLOWED_HOSTS = [SITE_HOST, "localhost", "127.0.0.1"]

# The website's nginx is the only way in from the internet. Its address (or network) is trusted
# for X-Real-IP; any other peer is taken at its own address. The admin is answered only for
# client addresses in FORZA_ADMIN_ALLOW (default this machine only).
TRUSTED_PROXIES = networks(env("FORZA_ACCOUNTS_PROXY"))
ADMIN_ALLOW = networks(env("FORZA_ADMIN_ALLOW", "127.0.0.1/32 ::1/128"))
API_DOCS = env_flag("FORZA_API_DOCS")  # the OpenAPI page at /api/docs (off in production)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "members",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.discord",
    "allauth.socialaccount.providers.github",
    "allauth.headless",
]

MIDDLEWARE = [
    "members.middleware.ApiHeaders",
    "members.middleware.AdminOnlyFromAllowed",
    "members.middleware.AuthRoutes",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # the admin's CSS and scripts
    "django.contrib.sessions.middleware.SessionMiddleware",
    "members.middleware.FetchSiteCheck",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "members.middleware.SessionLifetime",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "fh_accounts.urls"
WSGI_APPLICATION = "fh_accounts.wsgi.application"
AUTH_USER_MODEL = "members.User"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

# ---- Data: one SQLite file (and the email pepper beside it) ----

if TESTING and not env("FORZA_ACCOUNTS_DATA"):
    DATA_DIR = Path(tempfile.mkdtemp(prefix="accounts-test-"))
    atexit.register(shutil.rmtree, DATA_DIR, True)
else:
    DATA_DIR = Path(env("FORZA_ACCOUNTS_DATA", str(BASE_DIR / "data")))
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("FORZA_ACCOUNTS_DB", str(DATA_DIR / "accounts.sqlite3")),
        "OPTIONS": {
            # WAL, and synchronous=FULL: a power cut must not lose a committed write.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 5,
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
EMAIL_PEPPER_FILE = Path(env("FORZA_EMAIL_PEPPER_FILE", str(DATA_DIR / "email_pepper.bin")))
BACKUP_DIR = Path(env("FORZA_ACCOUNTS_BACKUPS", str(DATA_DIR / "backups")))

# allauth's rate limits live in the cache: in the database, so every worker shares them.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cache"}}

# ---- Sessions and cookies ----

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "__Host-fh_session"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
# Lax, not Strict: allauth keeps the OAuth state in the session, and the provider sends the
# browser back with a cross-site top-level GET. Lax still keeps the cookie off every cross-site
# POST, frame and fetch.
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_PATH = "/"
SESSION_IDLE_DAYS = 30   # SESSION_COOKIE_AGE: refreshed at most once a day (members.middleware)
SESSION_MAX_DAYS = 90    # and never longer than this from the sign-in
SESSION_COOKIE_AGE = SESSION_IDLE_DAYS * 86400
MAX_SESSIONS = 20        # per account: a new sign-in ends the oldest

CSRF_COOKIE_NAME = "__Host-fh_csrf"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = False  # the page reads it into X-CSRFToken
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_PATH = "/"
CSRF_HEADER_NAME = "HTTP_X_CSRFTOKEN"
CSRF_TRUSTED_ORIGINS = [ORIGIN]

# The website's nginx speaks plain HTTP to this service and says the visitor came over HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"  # the admin's; no cookie
# HSTS and the redirect to HTTPS are the TLS proxy's: this service only ever sees the web nginx.
SILENCED_SYSTEM_CHECKS = ["security.W004", "security.W008"]
DATA_UPLOAD_MAX_MEMORY_SIZE = 16384

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# ---- allauth: headless, email by code, Discord and GitHub ----

HEADLESS_ONLY = True
HEADLESS_CLIENTS = ("browser",)
HEADLESS_SERVE_SPECIFICATION = False
# Where allauth sends the browser for its few non-API pages: back to the site's account page.
HEADLESS_FRONTEND_URLS = {
    "socialaccount_login_error": ORIGIN + "/account.html",
    "account_signup": ORIGIN + "/account.html",
}

ACCOUNT_ADAPTER = "members.adapters.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "members.adapters.SocialAccountAdapter"
HEADLESS_ADAPTER = "members.adapters.HeadlessAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = "username"  # the account id (members.models.User)
ACCOUNT_EMAIL_VERIFICATION = "none"  # the emailed code is the proof
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_BY_CODE_MAX_ATTEMPTS = 5
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 600
ACCOUNT_LOGIN_BY_CODE_FORMAT = {"length": 8, "numeric": True, "dashed": False}
ACCOUNT_LOGIN_BY_CODE_SUPPORTS_RESEND = False  # a new code is a new request (members.views)
ACCOUNT_REAUTHENTICATION_TIMEOUT = 600  # "a recent sign-in" for deleting the account
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_LOGOUT_ON_GET = False

EMAIL_DAILY = int(env("FORZA_ACCOUNTS_EMAIL_DAILY", "200"))
LINKS_DAILY = int(env("FORZA_ACCOUNTS_LINKS_DAILY", "200"))
# The design's limits (community-accounts design, section 5) as allauth rate limits, which
# members.views and members.adapters consume. Rates are amount/period/per: "ip" is the visitor's
# address (X-Real-IP from the trusted nginx), "key" the email address's HMAC, or "all".
ACCOUNT_RATE_LIMITS = {
    "signin_start": "20/h/ip",                       # Discord and GitHub sign-in starts
    "new_account": "5/d/ip,1000/d/key",              # new accounts, per IP and (key "all") in all
    "request_login_code": "5/h/ip,3/h/key,5/d/key",  # code mails: per IP and per address
    "code_failed": "20/h/ip",                        # wrong codes, per IP (5 tries per code besides)
    "login_code_mail_all": "%d/d/key" % EMAIL_DAILY,  # code mails in all, then email sign-in pauses
    # The game link (members.links). "key" is the XUID, the account, or "all".
    "game_call": "600/h/key",          # signed launcher calls per XUID (polling included)
    "link_start": "5/h/key",           # link requests per XUID
    "link_try": "10/10m/key",          # claims, and peeks that found nothing, per account
    "link_code_failed": "100/m/key",   # codes that found nothing, in all: then codes stop 10 minutes
    "link_change": "1/d/key",          # a new link, per XUID and per account
    "new_link": "%d/d/key" % LINKS_DAILY,  # new links in all
    # allauth's password and signup forms are not routed (no passwords); closed all the same.
    "login": "20/h/ip",
    "signup": "5/d/ip",
    "login_failed": "20/h/ip,5/10m/key",
}

SOCIALACCOUNT_ONLY = False
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_REQUIRED = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False   # accounts are never merged by email
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_QUERY_EMAIL = False
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_LOGIN_ON_GET = False


def provider(name, **extra):
    client_id = env("FORZA_%s_CLIENT_ID" % name.upper())
    secret = env("FORZA_%s_CLIENT_SECRET" % name.upper())
    apps = [{"client_id": client_id, "secret": secret}] if client_id and secret else []
    return {"APPS": apps, "OAUTH_PKCE_ENABLED": True, **extra}


SOCIALACCOUNT_PROVIDERS = {
    # identify: the user's id (and name, which is not kept); no email address. prompt=none skips
    # the consent screen for a returning player.
    "discord": provider("discord", SCOPE=["identify"], AUTH_PARAMS={"prompt": "none"}),
    # No scope: public profile only, of which only the numeric id is kept.
    "github": provider("github", SCOPE=[]),
}

# ---- The sign-in mail: an authenticated SMTP relay over TLS ----

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("FORZA_SMTP_HOST")
EMAIL_PORT = int(env("FORZA_SMTP_PORT", "587"))
EMAIL_HOST_USER = env("FORZA_SMTP_USER")
EMAIL_HOST_PASSWORD = env("FORZA_SMTP_PASSWORD")
EMAIL_USE_SSL = EMAIL_PORT == 465
EMAIL_USE_TLS = not EMAIL_USE_SSL
EMAIL_TIMEOUT = 20
DEFAULT_FROM_EMAIL = env("FORZA_SMTP_FROM", "signin@" + SITE_HOST)
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_SIGNIN_ENABLED = bool(EMAIL_HOST) or TESTING

# ---- The game link (members.links) ----

# The game server's data folder, mounted read-only: friends.json (the keys), players/ (gamertags
# and signed sign-ins) and banned_xuids.json. Without it, linking is off.
GAME_DATA = Path(env("FORZA_GAME_DATA")) if env("FORZA_GAME_DATA") else None
LINK_MINUTES = 10        # a link request lasts this long
LINK_CODES_OFF_MINUTES = 10  # codes stop this long after too many wrong ones (link_code_failed)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
# The admin's static files, served by WhiteNoise; like /admin/, nginx does not pass /static/ on.
STATIC_URL = "/static/"
STATIC_ROOT = Path(env("FORZA_ACCOUNTS_STATIC", str(BASE_DIR / "static")))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if not TESTING else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    # Request lines carry codes and states in their query strings: never log them.
    "loggers": {"django.request": {"level": "ERROR"}, "django.server": {"level": "ERROR"},
                # The game server's modules (members.game): their notes on each reload of
                # friends.json and on each refused signature are the game server's to log.
                "forza-live": {"level": "WARNING"}, "forza_live": {"level": "WARNING"}},
}
