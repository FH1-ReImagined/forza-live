"""Signed requests (#515): no secret of a player is sent or kept by the server.

Games up to 0.2.x proved their XUID with the friends key itself (16 random bytes in
live_identity.txt): sent in the clear in every HELLO and title service call (X-Friends-Key), to
whatever server the player chose, and kept here as its SHA-256 (forza_friends.Store "key"). Newer
games and launchers have an Ed25519 key pair (the seed is in live_identity.txt too) and sign each
request with it (forza_ed25519.py). The server keeps only the public key (Store "pub"): neither
what is sent nor what is stored can sign.

Canonical string, the ten lines joined by "\\n" (no "\\n" after the last), signed as it is:

    FORZA-ED25519-V1
    method            the request line's method; HELLO for the UDP sign-in
    target            the request line's path and query as sent; "-" for UDP
    server name       the host the player configured: lowercased, no port, no brackets, no
                      trailing dot (one of --server-name; any name when none is set)
    XUID              16 upper-case hex digits
    unix time         decimal, the client's clock corrected by the server's
    nonce             32 lower-case hex digits: random per request; for UDP the challenge the
                      server sent this address
    X-ClassName       the header's value, or empty
    X-Method          the header's value, or empty
    body SHA-256      lower-case hex of the request body; for a HELLO, of the HELLO up to and
                      including its features byte

HTTP. One header,

    X-Forza-Auth: FORZA-ED25519-V1 Server=<name>, Xuid=<16 hex>, Time=<unix>, Nonce=<32 hex>,
                  Signature=<128 hex>

checked with the public key the XUID is bound to. Every answer carries X-Forza-Time: <unix>.
A request is refused when its time is more than WINDOW seconds off, its nonce was seen in the
window, its name is not one of the server's or its signature is not the XUID's key's.

UDP. HELLO's friends key slot (after the features byte: u8 length + bytes) holds
    16 bytes          a raw friends key (games before signed requests), or
    an auth block     0xA2, flags u8 (2: an upgrade, the raw friends key follows), time u64,
                      name (u8 length + bytes), signature (64: of the HELLO canonical string,
                      the nonce being this address's challenge), public key (32),
                      [raw friends key (16)]
and the WELCOME ends (after the events byte, when there is one) with 0xA2, flags u8 (1 the XUID
is bound to a public key, 2 this HELLO proved the XUID, 4 this HELLO had an auth block, 8 the
XUID is bound to a raw friends key: upgrade it), server time u64 and the challenge (16 random
bytes, new for each address). A game sends its first HELLO with an empty slot and answers the
WELCOME's challenge at once. Older games read nothing after the WELCOME's HTTP port.
  Binding: an XUID with no key on record is bound to the public key of a valid auth block (trust
on first use, as the raw key was). An XUID bound to a raw friends key moves to a public key only
with an upgrade: the raw key, whose SHA-256 must be the one on record, in the same signed HELLO.
From then on raw keys are refused for that XUID, so a key copied before stops working the moment
its owner upgrades. An XUID bound to another public key stays bound to it ("linked to another
installation": recovery by hand). --rebind-legacy (a server only its operator uses, This PC)
binds an XUID bound to a raw key to the first valid public key instead.

Transition. Raw keys are taken for XUIDs still bound to them until --raw-key-cutoff (unix time;
env FORZA_RAW_KEY_CUTOFF); counters of their use are logged and on the status page.

Cost: a check is about 1 ms (forza_ed25519). A proven player's keep-alive HELLOs that change
nothing are checked once a minute (HELLO_RECHECK), not every 5 s; VERIFY_RATE bounds the whole
server's checks and HELLO_CHECKS each player's, so floods of bad signatures cannot take the CPU."""

import collections
import hashlib
import hmac
import logging
import os
import struct
import time

import forza_ed25519 as ed25519

log = logging.getLogger("forza-live")

TAG = "FORZA-ED25519-V1"
HEADER = "x-forza-auth"
KEY_SIZE = 16  # a raw friends key
PUB_SIZE, SIG_SIZE = 32, 64
WINDOW = 120  # seconds a signed time may be off the server's
# The main server's names (the game and the launcher have the same list): a server that does not
# know signed requests gets only a key made from the friends key and its name (old_server_key),
# except these, which get nothing.
MAIN_NAMES = ("forza-reimagined.playit.plus", "forza.dyotson.dev")
OLD_SERVER_LABEL = b"forza-live server key v1|"
BLOCK = 0xA2  # HELLO auth block and WELCOME auth part
F_UPGRADE = 0x02  # HELLO block flags
W_BOUND, W_PROVEN, W_CHECKED, W_LEGACY = 0x01, 0x02, 0x04, 0x08  # WELCOME flags
MAX_NONCES = 200000
LOG_SECONDS = 600
VERIFY_RATE = (500, 1000)  # signature checks per second and burst, the whole server
HELLO_CHECKS = (0.5, 6)  # HELLO signature checks per second and burst, one player
HELLO_RECHECK = 60.0  # seconds a proven peer's unchanged keep-alive HELLOs are not checked again
LINKED_ELSEWHERE = ("this account is linked to another installation. To get it back, ask on "
                    "Discord (or the operator of this server)")

SERVER_NAMES = set()  # --server-name; empty: any name
RAW_KEY_CUTOFF = None  # --raw-key-cutoff: unix time after which raw keys are refused
REBIND_LEGACY = False  # --rebind-legacy
COUNTS = collections.Counter()  # "raw http", "raw hello", "signed", "refused <why>"...
_raw_players = set()
_nonces = collections.OrderedDict()  # nonce -> monotonic time it may be forgotten
_logged = {"raw": 0.0, "refused": {}}
_budget = [float(VERIFY_RATE[1]), time.monotonic()]


def add_arguments(parser):
    parser.add_argument("--server-name", action="append", default=[], metavar="HOST",
                        help="a name players reach this server by, as they type it without the "
                             "port (repeatable; env FORZA_SERVER_NAMES, comma separated). "
                             "Signatures name their server: those for other names are refused. "
                             "Without any, every name is taken")
    cutoff = os.environ.get("FORZA_RAW_KEY_CUTOFF", "")
    parser.add_argument("--raw-key-cutoff", type=int, default=int(cutoff) if cutoff else None,
                        metavar="UNIX_TIME",
                        help="from this time on, refuse the raw friends keys of games before "
                             "signed requests for XUIDs that have a key on record (env "
                             "FORZA_RAW_KEY_CUTOFF; default: still accepted until each player "
                             "upgrades)")
    parser.add_argument("--rebind-legacy", action="store_true",
                        default=os.environ.get("FORZA_REBIND_LEGACY", "") == "1",
                        help="bind an XUID bound to a raw friends key to the first signed game "
                             "that signs in with it, without an upgrade (env "
                             "FORZA_REBIND_LEGACY=1): only for a server nobody else uses")


def configure(args):
    global RAW_KEY_CUTOFF, REBIND_LEGACY
    names = list(args.server_name)
    names += [n for n in os.environ.get("FORZA_SERVER_NAMES", "").split(",") if n.strip()]
    SERVER_NAMES.clear()
    SERVER_NAMES.update(server_name(n) for n in names if server_name(n))
    RAW_KEY_CUTOFF = args.raw_key_cutoff
    REBIND_LEGACY = args.rebind_legacy
    ed25519.public_key(bytes(32))  # builds the base point table now, not at a player's request
    if SERVER_NAMES:
        log.info("signed requests for %s%s", ", ".join(sorted(SERVER_NAMES)),
                 "; bindings to raw friends keys go to the first signed game" if REBIND_LEGACY else "")
    else:
        # Every server sees the same public keys: without its names, a signature a player made
        # for another server (passed on as it is made) is taken here too.
        log.warning("signed requests for any server name: start the server with --server-name "
                    "<the name players type> so signatures made for other servers are refused")
    if RAW_KEY_CUTOFF is not None:
        log.info("raw friends keys refused from %s",
                 time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(RAW_KEY_CUTOFF)))


# --- The scheme (the game's live_signing.h and the launcher's live_auth.py do the same) ---------

def server_name(host):
    """A server's name in signatures: the configured host, lowercased, without its port."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def old_server_key(friends_key, name):
    """What a game with signed requests sends a server without them in place of its friends key:
    a key made from it and the server's name, useless anywhere else (nothing for a main name)."""
    return hmac.new(bytes(friends_key), OLD_SERVER_LABEL + name.encode(), hashlib.sha256).digest()[:KEY_SIZE]


def canonical(method, target, name, xuid, when, nonce, classname, xmethod, body_hash):
    return "\n".join((TAG, method, target, name, "%016X" % xuid, str(int(when)), nonce,
                      classname, xmethod, body_hash)).encode()


def request_canonical(method, target, name, xuid, when, nonce, headers, body):
    return canonical(method, target, name, xuid, when, nonce, headers.get("x-classname", ""),
                     headers.get("x-method", ""), hashlib.sha256(body).hexdigest())


def auth_header(seed, name, xuid, method, target, body, headers=None, when=None, nonce=None):
    """The X-Forza-Auth value of a request (tests and tools; the launcher has its own)."""
    when = int(time.time()) if when is None else int(when)
    nonce = nonce or os.urandom(16).hex()
    text = request_canonical(method, target, name, xuid, when, nonce, headers or {}, body)
    return "%s Server=%s, Xuid=%016X, Time=%d, Nonce=%s, Signature=%s" % (
        TAG, name, xuid, when, nonce, ed25519.sign(seed, text).hex())


def parse_header(value):
    """{"server", "xuid", "time", "nonce", "signature"} of an X-Forza-Auth value, or None."""
    scheme, _, rest = (value or "").strip().partition(" ")
    if scheme != TAG:
        return None
    fields = {}
    for part in rest.split(","):
        k, eq, v = part.strip().partition("=")
        if eq:
            fields[k.lower()] = v.strip()
    try:
        nonce, sig = fields["nonce"].lower(), bytes.fromhex(fields["signature"])
        if len(nonce) != 32 or len(sig) != SIG_SIZE:
            return None
        bytes.fromhex(nonce)
        return {"server": server_name(fields["server"]), "xuid": int(fields["xuid"], 16),
                "time": int(fields["time"]), "nonce": nonce, "signature": sig}
    except (KeyError, ValueError):
        return None


# --- Bindings (forza_friends.Store: "pub" a public key, "key" SHA-256 of a raw friends key) ------

def bound_pub(store, xuid):
    """The public key the XUID is bound to, or None."""
    p = store.get(xuid) if store is not None and xuid else None
    try:
        pub = bytes.fromhex(p.get("pub") or "") if p else b""
    except ValueError:
        return None
    return pub if len(pub) == PUB_SIZE else None


def legacy_hash(store, xuid):
    """SHA-256 (hex) of the raw friends key the XUID is still bound to, or None."""
    p = store.get(xuid) if store is not None and xuid else None
    return p["key"] if p and p.get("key") and not p.get("pub") else None


def bound(store, xuid):
    return bound_pub(store, xuid) is not None or legacy_hash(store, xuid) is not None


def binding(record):
    """What identifies a player's binding (forza_events.py): its public key, else its old hash."""
    return ("pub:" + record["pub"]) if record.get("pub") else record.get("key", "")


def rebind(store, xuid, pub, why):
    p = store.get(xuid, create=True)
    old = binding(p)
    p["pub"], p["key"] = pub.hex(), ""
    store.dirty = True
    COUNTS[why] += 1
    for fn in getattr(store, "on_rebind", ()):
        fn(xuid, old, binding(p))


# --- Checks ------------------------------------------------------------------------------------

def name_ok(name):
    return bool(name) and (not SERVER_NAMES or name in SERVER_NAMES)


def spend(bucket, rate):
    """Token bucket [tokens, last]: False when empty (the check is skipped)."""
    now = time.monotonic()
    bucket[0] = min(rate[1], bucket[0] + (now - bucket[1]) * rate[0])
    bucket[1] = now
    if bucket[0] < 1:
        return False
    bucket[0] -= 1
    return True


def checked_signature(pub, text, sig):
    """ed25519.verify within the server's budget: None when over it."""
    if not spend(_budget, VERIFY_RATE):
        COUNTS["checks over budget"] += 1
        return None
    return ed25519.verify(pub, text, sig)


def seen_nonce(nonce):
    """True when the nonce was used in the window; else remembers it."""
    now = time.monotonic()
    while _nonces:
        oldest, until = next(iter(_nonces.items()))
        if until > now and len(_nonces) < MAX_NONCES:
            break
        if until > now:
            COUNTS["nonces dropped early"] += 1
        del _nonces[oldest]
    if nonce in _nonces:
        return True
    _nonces[nonce] = now + 2 * WINDOW + 1
    return False


def refused(why, xuid, what, detail=""):
    """Counts a refusal and logs it once per XUID and reason every LOG_SECONDS."""
    COUNTS["refused " + why] += 1
    now = time.monotonic()
    last = _logged["refused"]
    if now - last.get((xuid, why), -LOG_SECONDS) >= LOG_SECONDS:
        last[(xuid, why)] = now
        if len(last) > 20000:
            last.clear()
        log.info("auth: %s for %016X refused: %s%s", what, xuid, why, detail)


class Checked:
    """The outcome of a signed request: ok, the XUID it is signed for, and why it failed
    (name, clock, unbound, signature, replay, busy or malformed)."""

    def __init__(self, ok, xuid=0, why=""):
        self.ok, self.xuid, self.why = ok, xuid, why

    def __repr__(self):
        return "Checked(%r, %016X, %r)" % (self.ok, self.xuid, self.why)


def check_request(store, method, target, headers, body, now=None):
    """None for a request without X-Forza-Auth, else a Checked."""
    value = headers.get(HEADER)
    if value is None:
        return None
    a = parse_header(value)
    if a is None:
        refused("malformed", 0, "%s %s" % (method, target[:60]))
        return Checked(False, 0, "malformed")
    now = time.time() if now is None else now
    xuid = a["xuid"]
    why = detail = ""
    pub = bound_pub(store, xuid)
    if not name_ok(a["server"]):
        why, detail = "name", " %r" % a["server"][:40]
    elif abs(a["time"] - now) > WINDOW:
        why, detail = "clock", " %+d s" % (a["time"] - now)
    elif pub is None:
        COUNTS["signed unbound"] += 1
        return Checked(False, xuid, "unbound")
    else:
        text = request_canonical(method, target, a["server"], xuid, a["time"], a["nonce"],
                                 headers, body)
        good = checked_signature(pub, text, a["signature"])
        if good is None:
            why = "busy"
        elif not good:
            why = "signature"
        elif seen_nonce(a["nonce"]):
            why = "replay"
    if why:
        refused(why, xuid, headers.get("x-method") or target[:60], detail)
        return Checked(False, xuid, why)
    COUNTS["signed"] += 1
    return Checked(True, xuid)


def raw_refused(now=None):
    return RAW_KEY_CUTOFF is not None and (time.time() if now is None else now) >= RAW_KEY_CUTOFF


def note_raw(kind, xuid):
    """A raw friends key from a game or launcher before signed requests: counted, and logged
    every LOG_SECONDS, so the operator sees when the old ones are gone."""
    COUNTS["raw " + kind] += 1
    if xuid and len(_raw_players) < 100000:
        _raw_players.add(xuid)
    now = time.monotonic()
    if now - _logged["raw"] >= LOG_SECONDS:
        _logged["raw"] = now
        log.info("auth: raw friends keys (games before signed requests) since start: %d title "
                 "calls, %d HELLOs, %d players%s", COUNTS["raw http"], COUNTS["raw hello"],
                 len(_raw_players), ", refused now" if raw_refused() else "")


def raw_key_ok(store, xuid, key, kind):
    """A raw friends key for the XUID: True when the XUID is still bound to it, False when it is
    not, the XUID has upgraded to a public key or raw keys are past the cutoff, None when the
    XUID has no key on record."""
    note_raw(kind, xuid)
    if len(key) != KEY_SIZE:
        return False if bound(store, xuid) else None
    if bound_pub(store, xuid) is not None:
        refused("raw key", xuid, kind, " (its owner signs now)")
        return False
    h = legacy_hash(store, xuid)
    if h is None:
        return None
    if raw_refused():
        refused("raw key", xuid, kind, " (after the cutoff)")
        return False
    return hmac.compare_digest(h, hashlib.sha256(bytes(key)).hexdigest())


def status():
    return {"auth": {"server_names": sorted(SERVER_NAMES), "raw_key_cutoff": RAW_KEY_CUTOFF,
                     "raw_key_players": len(_raw_players),
                     **{k.replace(" ", "_"): v for k, v in sorted(COUNTS.items())}}}


# --- UDP sign-in ---------------------------------------------------------------------------------

def challenge(peer):
    """The peer's challenge: 16 random bytes, new whenever its address changes."""
    if getattr(peer, "auth_addr", None) != peer.addr or not getattr(peer, "auth_challenge", None):
        peer.auth_challenge = os.urandom(16)
        peer.auth_addr = peer.addr
    return peer.auth_challenge


def parse_block(data):
    """(flags, time, name, signature, public key, raw friends key) of a HELLO auth block, or
    None."""
    try:
        if len(data) < 11 or data[0] != BLOCK:
            return None
        flags = data[1]
        (when,) = struct.unpack_from(">Q", data, 2)
        n = data[10]
        name = data[11:11 + n].decode("ascii").lower()
        pos = 11 + n
        sig, pub = data[pos:pos + SIG_SIZE], data[pos + SIG_SIZE:pos + SIG_SIZE + PUB_SIZE]
        pos += SIG_SIZE + PUB_SIZE
        raw = data[pos:pos + KEY_SIZE] if flags & F_UPGRADE else b""
        if len(sig) != SIG_SIZE or len(pub) != PUB_SIZE or len(raw) != (KEY_SIZE if flags & F_UPGRADE else 0):
            return None
        return flags, when, name, sig, pub, raw
    except (struct.error, UnicodeDecodeError, IndexError):
        return None


def hello_text(name, xuid, when, nonce, signed):
    return canonical("HELLO", "-", name, xuid, when, nonce.hex(), "", "", hashlib.sha256(signed).hexdigest())


def hello_block(seed, name, xuid, nonce, signed, when=None, upgrade=None):
    """A game's HELLO auth block (tests; the game builds it in live_signing.h). `upgrade`: the
    raw friends key, for an XUID bound to it."""
    when = int(time.time()) if when is None else int(when)
    out = bytes([BLOCK, F_UPGRADE if upgrade else 0]) + struct.pack(">Q", when)
    out += bytes([len(name)]) + name.encode()
    out += ed25519.sign(seed, hello_text(name, xuid, when, nonce, signed)) + ed25519.public_key(seed)
    return out + (bytes(upgrade) if upgrade else b"")


def hello(store, peer, data, signed, now=None):
    """Whether the HELLO's friends key slot (`data`) proves the peer's XUID. `signed`: the
    HELLO up to and including its features byte. Binds an XUID with no key on record, and
    upgrades one bound to a raw friends key."""
    now = time.time() if now is None else now
    xuid = peer.xuid
    nonce = challenge(peer)
    peer.auth_checked = peer.auth_outdated = peer.auth_conflict = False
    # An empty slot: a game that signs, before it has the challenge (its first HELLO).
    peer.auth_pending = not data
    if len(data) == KEY_SIZE:  # a raw key: a game before signed requests
        ok = raw_key_ok(store, xuid, data, "hello")
        peer.auth_outdated = ok is False and (raw_refused(now) or bound_pub(store, xuid) is not None)
        return store.claim(xuid, data) if ok is None else ok
    block = parse_block(data) if data else None
    if block is None:
        return False
    peer.auth_checked = True
    flags, when, name, sig, pub, raw = block
    what = "HELLO #%d" % peer.id
    if not name_ok(name):
        refused("name", xuid, what, " %r" % name[:40])
        return False
    if abs(when - now) > WINDOW:
        refused("clock", xuid, what, " %+d s" % (when - now))
        return False
    # A keep-alive HELLO (every 5 s) that changes nothing the last checked one proved: the same
    # HELLO fields, key, name and challenge (so the same address).
    same = (signed, pub, name, nonce)
    if getattr(peer, "friends_ok", False) and getattr(peer, "auth_proved", None) == same and \
            time.monotonic() - getattr(peer, "auth_proved_at", 0.0) < HELLO_RECHECK:
        COUNTS["hello unchanged"] += 1
        return True
    budget = getattr(peer, "auth_budget", None)
    if budget is None:
        budget = peer.auth_budget = [float(HELLO_CHECKS[1]), time.monotonic()]
    if not spend(budget, HELLO_CHECKS):
        COUNTS["hello checks skipped"] += 1
        return bool(getattr(peer, "friends_ok", False))  # unchanged
    good = checked_signature(pub, hello_text(name, xuid, when, nonce, signed), sig)
    if good is None:
        return bool(getattr(peer, "friends_ok", False))
    if not good:
        refused("signature", xuid, what)
        return False
    have = bound_pub(store, xuid)
    if have is not None:
        if hmac.compare_digest(have, pub):
            COUNTS["signed hello"] += 1
            peer.auth_proved, peer.auth_proved_at = same, time.monotonic()
            return True
        peer.auth_conflict = True
        refused("another installation", xuid, what, " (bound to another public key)")
        return False
    old = legacy_hash(store, xuid)
    if old is None:
        rebind(store, xuid, pub, "registered")
        log.info("auth: %016X bound to its public key (%s)", xuid, name)
        return True
    if flags & F_UPGRADE:
        if hmac.compare_digest(old, hashlib.sha256(raw).hexdigest()):
            rebind(store, xuid, pub, "upgraded")
            log.info("auth: %016X upgraded from its friends key to its public key (%s); raw keys "
                     "are refused for it from now on", xuid, name)
            return True
        refused("upgrade", xuid, what, " (not the friends key on record)")
        return False
    if REBIND_LEGACY:
        rebind(store, xuid, pub, "rebound")
        log.info("auth: %016X bound to its public key in place of its friends key (%s, "
                 "--rebind-legacy)", xuid, name)
        return True
    refused("legacy", xuid, what, " (bound to a friends key; no upgrade)")
    return False


def refusal(peer, off):
    """The notice for a peer whose HELLO did not prove its XUID, or None for the default one.
    `off`: "Friends are off", "Chat is off"."""
    if getattr(peer, "auth_conflict", False):
        return "%s: %s" % (off, LINKED_ELSEWHERE)
    if getattr(peer, "auth_outdated", False):
        return "%s: this version of the game is too old for this server. Update the game" % off
    return None


def welcome(store, peer, now=None):
    """The WELCOME's auth part for this peer."""
    now = time.time() if now is None else now
    flags = (W_BOUND if bound_pub(store, peer.xuid) is not None else 0) | (
        W_LEGACY if legacy_hash(store, peer.xuid) is not None else 0) | (
        W_PROVEN if getattr(peer, "friends_ok", False) else 0) | (
        W_CHECKED if getattr(peer, "auth_checked", False) else 0)
    return bytes([BLOCK, flags]) + struct.pack(">Q", int(now)) + challenge(peer)
