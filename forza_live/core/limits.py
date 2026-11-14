"""Abuse limits for a public Forza Live server: every upload is untrusted.

accept() is called before any blob (ghost, shared car, livery, thumbnail, auction or gift
car) is written: one blob may not exceed MAX_BLOB bytes and the server's blobs together
stay under DISK_CAP (env FORZA_DISK_CAP_MB, default 4096). A ValueError becomes a "bad
parameters" failure for the game, which carries on without the upload.
"""

import ipaddress
import os
import time
import unicodedata
import urllib.parse
import zlib

MAX_BLOB = 2 << 20
DISK_CAP = int(os.environ.get("FORZA_DISK_CAP_MB", "4096")) << 20
MAX_PLAYERS = int(os.environ.get("FORZA_MAX_PLAYERS", "20000"))  # player records on disk
PER_PLAYER = {"auctions": 30, "shared_cars": 100, "gifts": 50}  # open items per player
BLOB_DIRS = ("ghosts", "market", "clubs", "photos", "designs", "community")  # videos: their own budget

_data_dir = "forza_live_data"
_cache = [float("-inf"), 0]  # time of the last scan, bytes found
_added = [0]       # bytes accepted since


def configure(data_dir):
    global _data_dir
    _data_dir = data_dir
    _cache[:] = [float("-inf"), 0]  # another folder: scan it at the next upload
    _added[0] = 0


def usage():
    now = time.monotonic()
    if now - _cache[0] > 30:
        total = 0
        for sub in BLOB_DIRS:
            for root, _, names in os.walk(os.path.join(_data_dir, sub)):
                for name in names:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        _cache[0], _cache[1], _added[0] = now, total, 0
    return _cache[1] + _added[0]


def accept(nbytes, limit=MAX_BLOB):
    if nbytes > limit:
        raise ValueError("upload of %d bytes refused" % nbytes)
    if usage() + nbytes > DISK_CAP:
        raise ValueError("server disk quota reached")
    _added[0] += nbytes


# Trusted proxies: a tunnel agent (playit.gg) on the host or LAN that relays every player's
# UDP and TCP to the server, so all of them arrive from the agent's address (each player on
# its own source port). Such addresses are exempt from the per-address limits and never count
# as "this machine" for the operator endpoints. Without any, nothing changes.
TRUSTED_PROXIES = []  # ip_network objects


def parse_proxies(*specs):
    """specs: strings of IPs / CIDRs, each possibly a comma or space separated list."""
    nets = []
    for spec in specs:
        for item in (spec or "").replace(",", " ").split():
            nets.append(ipaddress.ip_network(item, strict=False))
    return nets


def configure_proxies(*specs):
    TRUSTED_PROXIES[:] = parse_proxies(*specs)


def is_trusted_proxy(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def address_key(ip):
    """What the per-address limits count an address under: an IPv4 address as it is, an IPv6
    one by its /64 (a subscriber has all of it, so each of its addresses would be a new budget;
    the website accounts service counts the same way). An IPv4-mapped IPv6 address is the IPv4
    one. Anything else is returned unchanged."""
    if ":" not in ip:
        return ip
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version == 4:
        return str(addr)
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if addr.is_loopback:
        return str(addr)
    return str(ipaddress.ip_network((int(addr) >> 64 << 64, 64)))


# Addresses that are never a client's on the internet: this machine and the home network. The
# operator panel exists only for them (panel/routes.py), and the community media routes count
# no budget of their own against them (media.py: the website's own web server and builder).
LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10"))


def local_address(ip):
    """Whether ip (an address, or address_key's /64) is a private, link-local or loopback one."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0].split("/")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return any(a in n for n in LOCAL_NETWORKS)


def loggable(path):
    """A request's path for the log: without the value of a token= in its query (the operator's
    token, however the name is written or encoded)."""
    head, mark, query = path.partition("?")
    if not mark:
        return path
    query, hash_mark, fragment = query.partition("#")
    parts = []
    for part in query.split("&"):
        name, eq, _ = part.partition("=")
        if eq and urllib.parse.unquote_plus(name).strip().lower() == "token":
            part = name + "=..."
        parts.append(part)
    return head + "?" + "&".join(parts) + hash_mark + fragment


# ---- Player-chosen display strings: plain ASCII only ----
# Xbox 360 gamertags were ASCII letters, digits and spaces (at most 15). A name of dozens of
# combining marks or wide glyphs would cover every other player's screen, so nothing but
# plain ASCII reaches the game. Accented Latin is transliterated first ("Jose" for "Jose").

MAX_GAMERTAG = 15
CLUB_TAG_MAX, CLUB_NAME_MAX, CLUB_DESC_MAX = 4, 20, 128


def ascii_fold(text):
    """NFKD, drop the combining marks, keep only ASCII (anything else is dropped)."""
    text = unicodedata.normalize("NFKD", text if isinstance(text, str) else "")
    return "".join(c for c in text if c.isascii() and not unicodedata.combining(c))


def clean_gamertag(name, xuid=None):
    """A gamertag the game may show: ASCII letters, digits and single spaces, trimmed, at
    most 15. Nothing left: "Driver" and 4 hex digits of the XUID (or of the original text
    when no XUID is known), the same every time."""
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("utf-8", "replace")
    raw = name if isinstance(name, str) else ""
    kept = "".join(c if c.isalnum() else " " for c in ascii_fold(raw))
    kept = " ".join(kept.split())[:MAX_GAMERTAG].strip()
    if kept:
        return kept
    seed = (xuid & 0xFFFF) if isinstance(xuid, int) else zlib.crc32(raw.encode("utf-8", "replace")) & 0xFFFF
    return "Driver%04X" % seed


def clean_ascii_text(text, limit, fallback=""):
    """Printable ASCII (0x20-0x7E) on one line, trimmed, at most `limit` characters."""
    kept = "".join(c if " " <= c <= "~" else " " for c in ascii_fold(text))
    return " ".join(kept.split())[:limit].strip() or fallback
