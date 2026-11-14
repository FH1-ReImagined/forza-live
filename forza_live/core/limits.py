"""Abuse limits for a public Forza Live server: every upload is untrusted.

accept() is called before any blob (ghost, shared car, livery, thumbnail, auction or gift
car) is written: one blob may not exceed MAX_BLOB bytes and the server's blobs together
stay under DISK_CAP (env FORZA_DISK_CAP_MB, default 4096). A ValueError becomes a "bad
parameters" failure for the game, which carries on without the upload.

The blobs' total is kept in memory (Stored, below): counting it means a walk over every stored
file, seconds on a big store, and it is never done on the event loop, where nothing else was
answered meanwhile (#64). Whoever writes or deletes a file under BLOB_DIRS says so here:
accept() before a write, wrote() for a file nobody asked accept() about, replacing() before a
file is written over, remove() to delete one, release() for accepted bytes that were not
written.
"""

import asyncio
import ipaddress
import os
import threading
import unicodedata
import urllib.parse
import zlib

MAX_BLOB = 2 << 20
DISK_CAP = int(os.environ.get("FORZA_DISK_CAP_MB", "4096")) << 20
MAX_PLAYERS = int(os.environ.get("FORZA_MAX_PLAYERS", "20000"))  # player records on disk
PER_PLAYER = {"auctions": 30, "shared_cars": 100, "gifts": 50}  # open items per player
BLOB_DIRS = ("ghosts", "market", "clubs", "photos", "designs", "community")  # videos: their own budget
RESCAN_SECONDS = 300        # the stored files are counted again this often, in a thread
START_ALLOWANCE = 64 << 20  # bytes taken in all before the first count is done

_data_dir = "forza_live_data"


class Stored:
    """The bytes of the files under some folders, kept in memory.

    Counting them is a walk over every file with a stat of each: 4 to 5 s for 25,000 files on
    a slow disk. count() does it and belongs in a thread (it refuses to run on an event loop):
    the server calls it at the start and every RESCAN_SECONDS after (server.keep_counted). In
    between the total follows the files: take() and add() as they are written, remove() and
    release() as they go. A count puts right what those missed: a write that failed half way, a
    file another process wrote or deleted.

    Before the first count is done nothing is known of what the folders hold, and uploads
    arrive all the same (a ghost the game sends once and never again). They are taken up to
    START_ALLOWANCE bytes in all: a store that was full already is passed by that much at
    most, once per start, and a restart costs the players no upload. After it the total is
    what the count found plus what was taken while it walked.

    A count cannot tell whether it saw a file that was written or deleted while it walked. It
    takes every byte added meanwhile as not seen, and gives none back for what was removed
    meanwhile: the total errs on the full side, by no more than those bytes, until the next
    count."""

    def __init__(self, folders, stat=os.stat):
        """folders(): the folders counted. stat: os.stat, or os.lstat to count a link as the
        link and never as what it names."""
        self._folders, self._stat = folders, stat
        self._lock = threading.Lock()      # the numbers below: the event loop's and a count's thread
        self._counting = threading.Lock()  # one count at a time
        self._round = 0                    # reset() and close() end the count under way
        self.reset()

    def reset(self):
        """Nothing is known any more (another data folder): to be counted again."""
        with self._lock:
            self._round += 1
            self._bytes = 0       # the last count's bytes, and what changed since
            self._added = 0       # bytes added since the count under way began
            self.counted = False  # a count has been done
            self.files = 0        # files at the last count

    def close(self):
        """The server is stopping: a count under way ends at its next file."""
        with self._lock:
            self._round += 1

    def total(self):
        """The bytes stored, as far as known: before the first count, only those taken since
        the start."""
        return self._bytes

    def count(self):
        """Counts the files again. (files, their bytes, how far off the running total was: None
        at the first count), or None when reset() or close() ended it."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Stored.count() walks every stored file: run it in a thread "
                               "(asyncio.to_thread), never on the event loop")
        with self._counting:
            with self._lock:
                round_, self._added = self._round, 0
            files = found = 0
            for top in self._folders():
                for root, _, names in os.walk(top):
                    for name in names:
                        if self._round != round_:
                            return None
                        try:
                            found += self._stat(os.path.join(root, name)).st_size
                            files += 1
                        except OSError:
                            pass
            with self._lock:
                if self._round != round_:
                    return None
                total = found + self._added
                drift = self._bytes - total if self.counted else None
                self._bytes, self.files, self.counted = total, files, True
            return files, found, drift

    def take(self, nbytes, cap, full, upload=True):
        """Room for nbytes more under cap, counted from here on. ValueError(full) when they do
        not fit; before the first count, when START_ALLOWANCE is used up. upload: False for
        bytes that are no player's upload (a file the server makes of one it already took, held
        to its own caps): the allowance is not theirs to run out of."""
        with self._lock:
            if self._bytes + nbytes > cap:
                raise ValueError(full)
            if upload and not self.counted and self._bytes + nbytes > START_ALLOWANCE:
                raise ValueError("the stored files are still being counted and %d MB were taken "
                                 "meanwhile" % (START_ALLOWANCE >> 20))
            self._bytes += nbytes
            self._added += nbytes

    def add(self, nbytes):
        """nbytes were written that take() was not asked about."""
        with self._lock:
            self._bytes += nbytes
            self._added += nbytes

    def release(self, nbytes):
        """nbytes are gone, or were taken and never written."""
        with self._lock:
            self._bytes = max(0, self._bytes - nbytes)

    def size(self, path):
        """The bytes a count sees at path; 0 when there is no file."""
        try:
            return self._stat(path).st_size
        except OSError:
            return 0

    def remove(self, path):
        """os.remove(path), its bytes given back. OSError as os.remove raises it."""
        size = self.size(path)
        os.remove(path)
        self.release(size)

    def replacing(self, path):
        """A file that was taken is about to be written over path: what is there now, if
        anything, is given back."""
        self.release(self.size(path))


STORED = Stored(lambda: [os.path.join(_data_dir, sub) for sub in BLOB_DIRS])


def configure(data_dir):
    global _data_dir
    _data_dir = data_dir
    STORED.reset()  # another folder: counted by the server's task (or count(), in a tool or a test)


def usage():
    """The blobs' bytes, from memory: never a walk (Stored)."""
    return STORED.total()


def count():
    """Counts the blobs again (Stored.count: slow, a thread's work)."""
    return STORED.count()


def accept(nbytes, limit=MAX_BLOB):
    if nbytes > limit:
        raise ValueError("upload of %d bytes refused" % nbytes)
    STORED.take(nbytes, DISK_CAP, "server disk quota reached")


def wrote(nbytes):
    """A file of nbytes was written under BLOB_DIRS without accept() (one the server makes from
    an upload it accepted, or moves): it counts, and is never refused."""
    STORED.add(nbytes)


def release(nbytes):
    """nbytes that accept() counted were not written after all."""
    STORED.release(nbytes)


def remove(path):
    """os.remove for a file under BLOB_DIRS: its bytes are given back. OSError as os.remove."""
    STORED.remove(path)


def replacing(path):
    """Before an accepted file is written over `path`: the one there now is not counted twice."""
    STORED.replacing(path)


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
