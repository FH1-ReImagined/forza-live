"""Signed requests (#515): no secret of a player is sent or kept by the server.

Games up to 0.2.x proved their XUID with the friends key itself (16 random bytes in
live_identity.txt): sent in the clear in every HELLO and title service call (X-Friends-Key), to
whatever server the player chose, and kept here as its SHA-256 (friends.Store "key"). Newer
games and launchers have an Ed25519 key pair (the seed is in live_identity.txt too) and sign each
request with it (ed25519.py). The server keeps only the public key (Store "pub"): neither
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
  First use has a price (FirstUse): a few new bindings an hour from one address, and so many a
second for the whole server, far fewer for raw keys (one datagram binds them, and its source may
be forged: they have a budget apart, so they cannot spend an address's signed bindings) and for
the games a tunnel names no address for (one small budget between them). Over them, or once
limits.MAX_PLAYERS records have a key, the XUID stays unbound and the game's next HELLOs try
again.

Transition. Raw keys are taken for XUIDs still bound to them until --raw-key-cutoff (unix time;
env FORZA_RAW_KEY_CUTOFF), and bind no new XUID after it; counters of their use are logged and on
the status page. Upgrades end too: from --upgrade-cutoff (env FORZA_UPGRADE_CUTOFF; without it
UPGRADE_GRACE after the raw key cutoff) the raw key no longer moves its XUID to a public key, as
whoever copied the key from an old game's traffic could do it first: the operator releases such
an XUID by hand (the same recovery as "linked to another installation").

Cost: a check is about 1 ms (ed25519). A proven player's keep-alive HELLOs that change
nothing are checked once a minute (HELLO_RECHECK), not every 5 s. A check is charged to who asks
for it before the server's own budget (VERIFY_RATE): to its TCP connection (CONN_CHECKS), to its
player's HELLOs (HELLO_CHECKS) and to its address (SOURCE_CHECKS; one pool, POOL_CHECKS, for the
clients of a tunnel that names no address). An address whose signatures keep failing (BAD_LIMIT
in BAD_WINDOW s) gets no checks from its budget for BAD_SKIP s, and one warning in the log.
  An address is not a player: a household, a carrier's NAT or a VPN exit is many, and behind a
tunnel a UDP source may be forged. So what an address's budget and failures decide is only what
the unproven get. A connection or registration whose signature for an XUID checked out (Client)
has that key's own budget (KEY_CHECKS) for that XUID whenever its address's is spent or stopped,
and one that has had no check yet gets its first from the server's reserve (FIRST_CHECKS), which
a check that fails does not give back: whoever shares an address with a flood of bad signatures
still proves its key and goes on. Nothing is counted against the XUID a request merely names:
anyone can name another player's. A HELLO is checked only when it could be right: one for an XUID
bound to another key, or sent before its address was told its challenge, is refused without a
check. So floods of bad signatures cannot take the CPU, nor one sender the checks of the others.
A HELLO left unchecked proves nothing, unless it is an unchanged keep-alive."""

import collections
import contextvars
import hashlib
import hmac
import logging
import os
import struct
import time

from forza_live.core import ed25519
from forza_live.core import limits

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
# Checks per second and burst for one client address (HELLOs and HTTP together: a LAN's 16 games
# and the burst of calls each makes as it signs in), for one TCP connection, and for all the
# clients whose address is unknown together.
SOURCE_CHECKS = (20, 100)
CONN_CHECKS = (20, 100)
POOL_CHECKS = (100, 200)
# What a client that proved a key has of its own for that key's XUID when its address's budget is
# spent or stopped (all the connections and registrations that proved the key together), and the
# server's reserve for the first check of a client that finds its address's so: one each, which
# comes back when the signature checks out.
KEY_CHECKS = (2, 30)
FIRST_CHECKS = (50, 100)
# An address, a TCP connection or a key (KEY_CHECKS) whose signatures fail BAD_LIMIT times in
# BAD_WINDOW s gets no checks from that budget for BAD_SKIP s. A game signs with the challenge it
# was told and the key it has, so its signatures fail only a few at a time (after a restart of
# the server, or as another installation's XUID).
BAD_LIMIT, BAD_WINDOW, BAD_SKIP = 40, 60.0, 60.0
MAX_SOURCES = 20000  # addresses (and keys) remembered for it; beyond them a new one is charged to the pool
# First-use bindings (FirstUse): per second and burst. One address: as many at once as it may
# have players (server.MAX_PEERS_PER_IP), then as many an hour. The whole server: signed games,
# whose address answered a challenge, and games before signed requests, whose raw key binds
# with one datagram from a source that may be forged.
FIRST_USE_SOURCE = (16 / 3600.0, 16)
FIRST_USE_SIGNED = (2.0, 60)
FIRST_USE_RAW = (1 / 60.0, 10)
# All the tunnelled games without an address together, in place of an address's: they cannot be
# told apart (server.source_key), so each socket having a budget would be none at all.
FIRST_USE_POOL = (1 / 30.0, 32)
UPGRADE_GRACE = 30 * 86400  # seconds after --raw-key-cutoff that upgrades are still taken
HELLO_RECHECK = 60.0  # seconds a proven peer's unchanged keep-alive HELLOs are not checked again
LINKED_ELSEWHERE = ("this account is linked to another installation. To get it back, ask on "
                    "Discord (or the operator of this server)")

SERVER_NAMES = set()  # --server-name; empty: any name
RAW_KEY_CUTOFF = None  # --raw-key-cutoff: unix time after which raw keys are refused
UPGRADE_CUTOFF = None  # --upgrade-cutoff: unix time after which upgrades are (upgrade_cutoff())
REBIND_LEGACY = False  # --rebind-legacy
COUNTS = collections.Counter()  # "raw http", "raw hello", "signed", "refused <why>"...
_raw_players = set()
_nonces = collections.OrderedDict()  # nonce -> monotonic time it may be forgotten
_logged = {"raw": 0.0, "refused": {}, "told": {}}
_budget = [float(VERIFY_RATE[1]), time.monotonic()]
# The client of the HTTP connection being handled (server.http_handler sets it for its task).
# None: a caller that has none (the website accounts service, a test): the server's budget only.
_client = contextvars.ContextVar("auth_client", default=None)


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
    upgrades = os.environ.get("FORZA_UPGRADE_CUTOFF", "")
    parser.add_argument("--upgrade-cutoff", type=int, default=int(upgrades) if upgrades else None,
                        metavar="UNIX_TIME",
                        help="from this time on, an XUID still bound to a raw friends key is no "
                             "longer moved to a public key by that key (an upgrade): the operator "
                             "releases it by hand (env FORZA_UPGRADE_CUTOFF; default: 30 days "
                             "after --raw-key-cutoff, never without one)")
    parser.add_argument("--rebind-legacy", action="store_true",
                        default=os.environ.get("FORZA_REBIND_LEGACY", "") == "1",
                        help="bind an XUID bound to a raw friends key to the first signed game "
                             "that signs in with it, without an upgrade (env "
                             "FORZA_REBIND_LEGACY=1): only for a server nobody else uses")


def configure(args):
    global RAW_KEY_CUTOFF, UPGRADE_CUTOFF, REBIND_LEGACY
    names = list(args.server_name)
    names += [n for n in os.environ.get("FORZA_SERVER_NAMES", "").split(",") if n.strip()]
    SERVER_NAMES.clear()
    SERVER_NAMES.update(server_name(n) for n in names if server_name(n))
    RAW_KEY_CUTOFF = args.raw_key_cutoff
    UPGRADE_CUTOFF = getattr(args, "upgrade_cutoff", None)
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
    if upgrade_cutoff() is not None:
        log.info("upgrades from a raw friends key to a public key refused from %s",
                 time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(upgrade_cutoff())))


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


# --- Bindings (friends.Store: "pub" a public key, "key" SHA-256 of a raw friends key) ------

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
    """What identifies a player's binding (events.py): its public key, else its old hash."""
    return ("pub:" + record["pub"]) if record.get("pub") else record.get("key", "")


def rebind(store, xuid, pub, why):
    p = store.get(xuid, create=True, own=True)
    old = binding(p)
    store.bind(p, pub=pub.hex())
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


def room(bucket, rate):
    """Whether spend() would succeed, without spending."""
    now = time.monotonic()
    bucket[0] = min(rate[1], bucket[0] + (now - bucket[1]) * rate[0])
    bucket[1] = now
    return bucket[0] >= 1


def told(key, level, message, *args):
    """Logs at most once per key every LOG_SECONDS (what a flood would repeat)."""
    now = time.monotonic()
    last = _logged["told"]
    if now - last.get(key, -LOG_SECONDS) >= LOG_SECONDS:
        if len(last) > 20000:
            last.clear()
        last[key] = now
        log.log(level, message, *args)


class Source:
    """One budget of signature checks (an address's, a key's, a connection's count): its tokens,
    and its signatures that failed."""
    __slots__ = ("bucket", "bad", "since", "skip", "seen")

    def __init__(self, now, burst=None):
        self.bucket = [float(SOURCE_CHECKS[1] if burst is None else burst), now]
        self.bad, self.since, self.skip, self.seen = 0, now, 0.0, now

    def failed(self, now):
        """Counts a failed signature: True when it is the one that stops the checks."""
        if now - self.since > BAD_WINDOW:
            self.bad, self.since = 0, now
        self.bad += 1
        if self.bad < BAD_LIMIT or now < self.skip:
            return False
        self.bad, self.since, self.skip = 0, now, now + BAD_SKIP
        return True


def remembered(sources, key, now, burst=None):
    """The Source of `key` in `sources`, made when there is room (MAX_SOURCES); else None."""
    found = sources.get(key)
    if found is None:
        if len(sources) >= MAX_SOURCES:
            for old in [k for k, s in sources.items()
                        if now - s.seen > 2 * BAD_WINDOW and now >= s.skip]:
                del sources[old]
            if len(sources) >= MAX_SOURCES:
                return None
        found = sources[key] = Source(now, burst)
    found.seen = now
    return found


class Checks:
    """One server's budgets of signature checks: per client address, per proven key, the pool's
    and the reserve for first checks (a friends.Store has one)."""

    def __init__(self):
        now = time.monotonic()
        self.sources = {}  # client address -> Source
        self.keys = {}  # XUID a client proved -> Source (KEY_CHECKS)
        self.pool = [float(POOL_CHECKS[1]), now]
        self.first = [float(FIRST_CHECKS[1]), now]


CHECKS = Checks()  # for a Client made without one

BY_ADDRESS, BY_KEY, BY_RESERVE = "address", "key", "reserve"  # what paid for a Client's check


class Client:
    """Who asks for signature checks: a TCP connection (`connection`), or a player's UDP
    registration (checker). `address`: its own (a tunnelled one's from its PROXY header), or None
    when it is unknown: a tunnel that names none, whose clients share POOL_CHECKS. A TCP
    connection has a budget and a count of failed signatures of its own besides.

    `keys`: the XUIDs it proved here, with a signature of the key each is bound to (prove). For
    those it does not depend on its address, which others share or may claim: when the address's
    budget is spent or stopped, their checks come from the key's own (KEY_CHECKS). Before it has
    had any check, its first comes from the server's reserve in that case. `proved`, `player` and
    `used`: a title call on it was signed by its player's key, whose, and when last
    (server.http_handler keeps such a connection open between calls)."""

    def __init__(self, address=None, connection=False, checks=None):
        now = time.monotonic()
        self.address = address
        self.checks = CHECKS if checks is None else checks
        self.bucket = [float(CONN_CHECKS[1]), now] if connection else None
        self.own = Source(now) if connection else None
        self.keys = set()
        self.tried = False  # it has had a check
        self.paid = None  # what its last check was charged to (BY_ADDRESS, BY_KEY or BY_RESERVE)
        # Its last check was refused for signatures that failed, not for a budget that refills
        # in a moment: no use asking again at once (server.lsp_answer).
        self.halted = False
        self.proved, self.player, self.used = False, 0, 0.0

    def source(self, now):
        """The Source of its address; None for no address, or too many to remember."""
        if self.address is None:
            return None
        return remembered(self.checks.sources, self.address, now)

    def may_check(self, xuid=0):
        """A check of a signature for `xuid` from its budgets, in front of the server's: False
        when it gets none."""
        now = time.monotonic()
        self.paid, self.halted = None, False
        if self.own is not None and now < self.own.skip:
            COUNTS["checks skipped after bad signatures"] += 1
            self.halted = True
            return False
        if self.bucket is not None and not spend(self.bucket, CONN_CHECKS):
            COUNTS["checks over a connection's budget"] += 1
            return False
        source = self.source(now)
        stopped = source is not None and now < source.skip
        if not stopped and (spend(source.bucket, SOURCE_CHECKS) if source is not None
                            else spend(self.checks.pool, POOL_CHECKS)):
            self.paid = BY_ADDRESS
            return True
        # Its address's budget is spent or stopped, by whoever is there (or says so). What it
        # proved itself still counts: the key's own budget, or its first check from the reserve.
        soon = False  # a budget that refills in a moment had none: asking again may do
        if xuid in self.keys:
            key = remembered(self.checks.keys, xuid, now, KEY_CHECKS[1])
            if key is not None and now >= key.skip:
                if spend(key.bucket, KEY_CHECKS):
                    COUNTS["checks on a key's own budget"] += 1
                    self.paid = BY_KEY
                    return True
                soon = True
        elif not self.tried:
            if spend(self.checks.first, FIRST_CHECKS):
                COUNTS["first checks from the reserve"] += 1
                self.paid = BY_RESERVE
                return True
            soon = True
        if stopped:
            COUNTS["checks skipped after bad signatures"] += 1
            self.halted = not soon
        else:
            COUNTS["checks over an address's budget" if source is not None
                   else "checks over the pool's budget"] += 1
        return False

    def unchecked(self):
        """The check it was given (may_check) was not made after all (the server's own budget
        had none): what the reserve gave comes back."""
        if self.paid == BY_RESERVE:
            self.checks.first[0] = min(float(FIRST_CHECKS[1]), self.checks.first[0] + 1)

    def checked(self, good, xuid=0):
        """The check it was given (may_check) is done: a signature that did not check out is
        counted against what paid for it."""
        self.tried = True
        if good:
            self.unchecked()  # only those that fail cost the reserve
            return
        self.failed(xuid)

    def failed(self, xuid=0):
        """One of its signatures did not check out."""
        now = time.monotonic()
        what = []
        if self.own is not None and self.own.failed(now):
            what.append("this connection")
        if self.paid == BY_KEY:
            key = self.checks.keys.get(xuid)
            if key is not None and key.failed(now):
                what.append("the key's own budget")
        else:
            source = self.source(now)
            if source is not None and source.failed(now):
                what.append("its address's budget")
        if what:
            COUNTS["bad signature sources"] += 1
            log.warning("auth: %d signatures from %s did not check out within %d s: no checks from "
                        "%s for %d s", BAD_LIMIT, self.address or "a connection without an address",
                        BAD_WINDOW, " and ".join(what), BAD_SKIP)

    def prove(self, xuid):
        """A signature of the key `xuid` is bound to checked out here (and was no replay)."""
        if len(self.keys) >= 16:  # a game has one profile at a time
            self.keys.clear()
        self.keys.add(xuid)


def client():
    """The Client of the HTTP connection being handled, or None."""
    return _client.get()


def enter(who):
    """`who` is the client of what this task handles from here on (an HTTP connection), until
    leave() with what this returns."""
    return _client.set(who)


def leave(entered):
    _client.reset(entered)


def checked_signature(pub, text, sig, who=None, xuid=0):
    """ed25519.verify of a signature for `xuid` within the budgets of who asks (a Client, or
    None) and the server's: None when over one."""
    if who is not None and not who.may_check(xuid):
        return None
    if not spend(_budget, VERIFY_RATE):
        COUNTS["checks over budget"] += 1
        if who is not None:
            who.unchecked()
        return None
    good = ed25519.verify(pub, text, sig)
    if who is not None:
        who.checked(good, xuid)
    return good


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
        refused("malformed", 0, "%s %s" % (method, limits.loggable(target)[:60]))
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
        who = _client.get()
        good = checked_signature(pub, text, a["signature"], who, xuid)
        if good is None:
            why = "busy"
        elif not good:
            why = "signature"
        elif seen_nonce(a["nonce"]):
            why = "replay"
            if who is not None:  # a call somebody else made: it cost a check all the same
                who.failed(xuid)
        elif who is not None:
            who.prove(xuid)
    if why:
        refused(why, xuid, headers.get("x-method") or limits.loggable(target)[:60], detail)
        return Checked(False, xuid, why)
    COUNTS["signed"] += 1
    return Checked(True, xuid)


def raw_refused(now=None):
    return RAW_KEY_CUTOFF is not None and (time.time() if now is None else now) >= RAW_KEY_CUTOFF


def upgrade_cutoff():
    """When upgrades end: --upgrade-cutoff, else UPGRADE_GRACE after --raw-key-cutoff, else None."""
    if UPGRADE_CUTOFF is not None:
        return UPGRADE_CUTOFF
    return RAW_KEY_CUTOFF + UPGRADE_GRACE if RAW_KEY_CUTOFF is not None else None


def upgrades_over(now=None):
    end = upgrade_cutoff()
    return end is not None and (time.time() if now is None else now) >= end


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
    not, the XUID has upgraded to a public key or raw keys are past the cutoff (which then bind
    no new XUID either), None when the XUID has no key on record."""
    note_raw(kind, xuid)
    if len(key) != KEY_SIZE:
        return False if bound(store, xuid) else None
    if bound_pub(store, xuid) is not None:
        refused("raw key", xuid, kind, " (its owner signs now)")
        return False
    h = legacy_hash(store, xuid)
    if raw_refused():
        if h is None:  # any XUID a sender makes up: counted, not a log line each
            COUNTS["refused raw key"] += 1
        else:
            refused("raw key", xuid, kind, " (after the cutoff)")
        return False
    if h is None:
        return None
    return hmac.compare_digest(h, hashlib.sha256(bytes(key)).hexdigest())


def status():
    return {"auth": {"server_names": sorted(SERVER_NAMES), "raw_key_cutoff": RAW_KEY_CUTOFF,
                     "upgrade_cutoff": upgrade_cutoff(),
                     "raw_key_players": len(_raw_players),
                     **{k.replace(" ", "_"): v for k, v in sorted(COUNTS.items())}}}


# --- UDP sign-in ---------------------------------------------------------------------------------

def challenge(peer):
    """The peer's challenge: 16 random bytes, new whenever its address changes. `auth_told`:
    a WELCOME has carried it to that address (welcome())."""
    if getattr(peer, "auth_addr", None) != peer.addr or not getattr(peer, "auth_challenge", None):
        peer.auth_challenge = os.urandom(16)
        peer.auth_addr = peer.addr
        peer.auth_told = False
    return peer.auth_challenge


def source_of(peer):
    """What a peer's first-use bindings and signature checks are charged to (server.source_key
    sets `source`): its address, a string; for a tunnelled game without one the agent's socket,
    a tuple."""
    return getattr(peer, "source", None) or peer.addr[0]


def answered(peer):
    """The peer's game answered the challenge the server sent to its address: a signature over it
    by the key its XUID is bound to checked out. So it is where it says it is (a forged source
    never sees the challenge) and whose XUID it names."""
    was = getattr(peer, "auth_proved", None)
    return (bool(getattr(peer, "friends_ok", False)) and was is not None
            and was[3] == getattr(peer, "auth_challenge", None))


def checker(store, peer):
    """The Client a peer's HELLO checks are charged to, kept with the registration: its address
    (the pool without one), and for an XUID it signed for here (hello) that key's own budget."""
    source = source_of(peer)
    who = getattr(peer, "auth_client", None)
    if who is None:
        who = peer.auth_client = Client(checks=getattr(store, "checks", None))
    who.address = source if isinstance(source, str) else None
    return who


def origin(peer):
    """Where a peer is, for the log: its address (the PROXY header's behind a tunnel)."""
    real = getattr(peer, "real_addr", None)
    if real:
        return real[0]
    return "the tunnel (no address)" if getattr(peer, "proxied", False) else peer.addr[0]


class FirstUse:
    """The budgets of first-use bindings (friends.Store has one): each takes one from its source
    (FIRST_USE_SOURCE; FIRST_USE_POOL for all the games without an address together) and one
    from the whole server's, for signed games or for raw keys. A raw key's source is charged
    apart from a signed game's: one datagram with a forged source binds a raw key, and must not
    spend what the games that are at that address have."""

    def __init__(self):
        now = time.monotonic()
        self.sources = {}
        self.pool = [float(FIRST_USE_POOL[1]), now]
        self.signed = [float(FIRST_USE_SIGNED[1]), now]
        self.raw = [float(FIRST_USE_RAW[1]), now]

    def buckets(self, source, raw):
        now = time.monotonic()
        if not isinstance(source, str):  # a tunnelled game without an address: the pool
            mine = (self.pool, FIRST_USE_POOL)
        else:
            key = ("raw", source) if raw else source
            bucket = self.sources.get(key)
            if bucket is None:
                if len(self.sources) > MAX_SOURCES:  # forged sources: only those not yet refilled stay
                    rate, burst = FIRST_USE_SOURCE
                    self.sources = {k: b for k, b in self.sources.items()
                                    if b[0] + (now - b[1]) * rate < burst}
                bucket = self.sources[key] = [float(FIRST_USE_SOURCE[1]), now]
            mine = (bucket, FIRST_USE_SOURCE)
        return mine, ((self.raw, FIRST_USE_RAW) if raw else (self.signed, FIRST_USE_SIGNED))

    def short(self, source, raw=False):
        """None when a binding is left for `source`, else the budget that has none: "source",
        "pool" or "server"."""
        mine, server = self.buckets(source, raw)
        if not room(*mine):
            return "source" if isinstance(source, str) else "pool"
        return None if room(*server) else "server"

    def take(self, source, raw=False):
        """Spends a binding; like short(), None when there was one."""
        why = self.short(source, raw)
        if why is None:
            for bucket, rate in self.buckets(source, raw):
                spend(bucket, rate)
        return why


def first_use(store, peer, raw=False, take=True):
    """Whether the peer's XUID, which has no key on record, may be bound now: the store has room
    for its record and the budgets (FirstUse) have a binding left, which `take` spends."""
    full = getattr(store, "full", None)
    if full is not None and full():
        COUNTS["first use refused"] += 1
        peer.auth_full = True
        told("store full", logging.WARNING, "auth: no room for another player's key (%d records "
             "have one, FORZA_MAX_PLAYERS): new players stay unbound", store.keyed)
        return False
    budgets = getattr(store, "first_use", None)
    if budgets is None:
        return True
    source = source_of(peer)
    why = budgets.take(source, raw) if take else budgets.short(source, raw)
    if why is None:
        return True
    COUNTS["first use refused"] += 1
    peer.auth_waiting = True
    if why == "server":
        told(("first use", raw), logging.WARNING, "auth: more new %s than the server binds at once: "
             "the others are bound at a later HELLO", "raw friends keys" if raw else "players")
    elif why == "pool":
        told(("first use", "pool"), logging.WARNING, "auth: more new players without an address of "
             "their own (through the tunnel) than the server binds at once: the others are bound "
             "later")
    else:
        told(("first use", source), logging.WARNING, "auth: more new players from %s than one "
             "address binds in an hour: the others are bound later", origin(peer))
    return False


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


def proved(peer, same):
    """A checked HELLO proved the peer's XUID: what it proved, for its keep-alives."""
    peer.auth_proved, peer.auth_proved_at = same, time.monotonic()
    return True


def hello(store, peer, data, signed, now=None):
    """Whether the HELLO's friends key slot (`data`) proves the peer's XUID. `signed`: the
    HELLO up to and including its features byte. Binds an XUID with no key on record, and
    upgrades one bound to a raw friends key."""
    now = time.time() if now is None else now
    xuid = peer.xuid
    nonce = challenge(peer)
    peer.auth_checked = peer.auth_outdated = peer.auth_conflict = peer.auth_waiting = False
    peer.auth_full = False
    # An empty slot: a game that signs, before it has the challenge (its first HELLO).
    peer.auth_pending = not data
    if len(data) == KEY_SIZE:  # a raw key: a game before signed requests
        ok = raw_key_ok(store, xuid, data, "hello")
        peer.auth_outdated = ok is False and (raw_refused(now) or bound_pub(store, xuid) is not None)
        if ok is not None:
            return ok
        if not first_use(store, peer, raw=True) or not store.claim(xuid, data):
            return False
        log.info("auth: %016X bound to a raw friends key (a game before signed requests) from %s",
                 xuid, origin(peer))
        return True
    block = parse_block(data) if data else None
    if block is None:
        return False
    peer.auth_checked = True
    flags, when, name, sig, pub, raw = block
    what = "HELLO #%d" % peer.id
    if not getattr(peer, "auth_told", False):
        # Signed before this address was told its challenge (a first HELLO, a restored player's,
        # a forged source): it cannot be right. Its WELCOME carries the challenge.
        COUNTS["hello before its challenge"] += 1
        return False
    if not name_ok(name):
        refused("name", xuid, what, " %r" % name[:40])
        return False
    if abs(when - now) > WINDOW:
        refused("clock", xuid, what, " %+d s" % (when - now))
        return False
    # A keep-alive HELLO (every 5 s) that changes nothing the last checked one proved: the same
    # HELLO fields (XUID, gamertag...), key, name and challenge (so the same address). It is
    # checked again every HELLO_RECHECK, and stays proven when the budgets leave no check for
    # it. Any other HELLO keeps nothing of what the peer proved before: left unchecked (no
    # budget), it does not prove the peer.
    same = (signed, pub, name, nonce)
    unchanged = bool(getattr(peer, "friends_ok", False)) and getattr(peer, "auth_proved", None) == same
    if unchanged and time.monotonic() - getattr(peer, "auth_proved_at", 0.0) < HELLO_RECHECK:
        COUNTS["hello unchanged"] += 1
        return True
    # What the record says before anything is checked: a HELLO that could not prove the XUID
    # whatever its signature costs no check.
    have = bound_pub(store, xuid)
    old = legacy_hash(store, xuid) if have is None else None
    if have is not None:
        if not hmac.compare_digest(have, pub):
            peer.auth_conflict = True  # unchecked: whoever names the XUID with a key of its own
            refused("another installation", xuid, what,
                    " (bound to another public key; from %s)" % origin(peer))
            return False
    elif old is None:
        if not first_use(store, peer, take=False):
            return False
    elif flags & F_UPGRADE:
        if upgrades_over(now):
            peer.auth_conflict = True  # told how to get it back: the operator releases the XUID
            refused("upgrade", xuid, what, " (after the upgrade cutoff: released by the operator)")
            return False
        if not hmac.compare_digest(old, hashlib.sha256(raw).hexdigest()):
            refused("upgrade", xuid, what, " (not the friends key on record)")
            return False
    elif not REBIND_LEGACY:
        peer.auth_conflict = upgrades_over(now)
        refused("legacy", xuid, what, " (bound to a friends key; no upgrade)")
        return False
    budget = getattr(peer, "auth_budget", None)
    if budget is None:
        budget = peer.auth_budget = [float(HELLO_CHECKS[1]), time.monotonic()]
    if not spend(budget, HELLO_CHECKS):
        COUNTS["hello checks skipped"] += 1
        return unchanged
    who = checker(store, peer)
    good = checked_signature(pub, hello_text(name, xuid, when, nonce, signed), sig, who, xuid)
    if good is None:
        return unchanged
    if not good:
        refused("signature", xuid, what, " from %s" % origin(peer))
        return False
    # Its key's signature for this XUID over its own challenge: whatever comes of this HELLO (a
    # first use may have to wait), the registration's later checks for the XUID are its own.
    who.prove(xuid)
    if have is not None:
        COUNTS["signed hello"] += 1
        return proved(peer, same)
    if old is None:
        if not first_use(store, peer):
            return False
        rebind(store, xuid, pub, "registered")
        log.info("auth: %016X bound to its public key (%s) from %s", xuid, name, origin(peer))
        return proved(peer, same)
    if flags & F_UPGRADE:
        rebind(store, xuid, pub, "upgraded")
        log.warning("auth: %016X upgraded from its friends key to its public key (%s) from %s; raw "
                    "keys are refused for it from now on", xuid, name, origin(peer))
        return proved(peer, same)
    rebind(store, xuid, pub, "rebound")
    log.info("auth: %016X bound to its public key in place of its friends key (%s, "
             "--rebind-legacy) from %s", xuid, name, origin(peer))
    return proved(peer, same)


def refusal(peer, off):
    """The notice for a peer whose HELLO did not prove its XUID, or None for the default one.
    `off`: "Friends are off", "Chat is off"."""
    if getattr(peer, "auth_conflict", False):
        return "%s: %s" % (off, LINKED_ELSEWHERE)
    if getattr(peer, "auth_outdated", False):
        return "%s: this version of the game is too old for this server. Update the game" % off
    if getattr(peer, "auth_full", False):
        return "%s for now: this server has no room for another new player. Try again later" % off
    if getattr(peer, "auth_waiting", False):
        return "%s for now: the server is signing in many new players and will get to this one" % off
    return None


def welcome(store, peer, now=None):
    """The WELCOME's auth part for this peer. W_LEGACY asks the game for an upgrade: not once
    upgrades are over, when it would only say which XUIDs a copied raw key is still good for."""
    now = time.time() if now is None else now
    flags = (W_BOUND if bound_pub(store, peer.xuid) is not None else 0) | (
        W_LEGACY if legacy_hash(store, peer.xuid) is not None and not upgrades_over(now) else 0) | (
        W_PROVEN if getattr(peer, "friends_ok", False) else 0) | (
        W_CHECKED if getattr(peer, "auth_checked", False) else 0)
    nonce = challenge(peer)
    peer.auth_told = True
    return bytes([BLOCK, flags]) + struct.pack(">Q", int(now)) + nonce
