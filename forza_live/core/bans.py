"""The operator's ban list: a banned XUID is banned from the whole server.

DATA_DIR/banned_xuids.json is a JSON list of hex XUIDs. The operator edits it by hand, from the
operator panel (Bans and chat, a player's page) or with POST /admin/photos/ban/<XUID> and
/admin/photos/unban/<XUID> (community.py). It is read again when it changes, so no restart is
needed. A banned XUID:

- cannot sign in: its HELLO is refused with error 4, as a timed sign-in ban's (server.py), and
  its Logon2 fails as a call that is not the player's own does (lsp.service_call); a player who
  is online when the ban comes is dropped at once (listen(), server.py), or within a few seconds
  when the file was edited by hand (the server's expiry tick);
- writes nothing: every title service call carrying its XUID fails (lsp.service_call), every
  datagram of a peer with it is dropped (server.py), and the signed HTTP routes (shader lists,
  /coop, /challenges/progress) take it for an unknown player (shader_lists.authenticate). The
  stores refuse it too (scores, stats, objectives, the co-op ledger, photos and designs), in case
  a path around those checks is ever added;
- is listed to nobody: its leaderboard rows, 1000 Club medals, online XP, rollups, stats ranks,
  challenge completions, auctions, friend requests, photos and designs stay where they are but
  are left out of every answer, the game's and the website's. Lifting the ban shows them again.

Nothing is deleted by a ban. Players a banned player wrote to (gifts, Message Center messages,
friendships) keep what they got.
"""

import json
import logging
import os
import time
import weakref

from forza_live.core import durable

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
FILE = "banned_xuids.json"
LOG_EVERY = 600.0  # seconds between the log lines of one banned XUID's refused writes

_state = {"mtime": None, "xuids": frozenset(), "keys": frozenset()}
_listeners = []    # weak references to what is called after set_ban changed the list
_logged = {}       # (xuid, what) -> when it was last logged


def configure(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir
    _state.update(mtime=None, xuids=frozenset(), keys=frozenset())


def path():
    return os.path.join(DATA_DIR, FILE)


def banned_xuids():
    """The ban list (a frozenset of int XUIDs), read again when the file changed."""
    p = path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = None
    if mtime != _state["mtime"]:
        xuids = set()
        if mtime is not None:
            try:
                with open(p) as f:
                    xuids = {int(str(v), 16) for v in json.load(f)}
            except (OSError, ValueError, TypeError):
                log.warning("bans: %s is not a JSON list of hex XUIDs: no bans", p)
        _state.update(mtime=mtime, xuids=frozenset(xuids),
                      keys=frozenset("%016X" % v for v in xuids))
    return _state["xuids"]


def banned_keys():
    """The ban list as the stores key players: 16 upper-case hex digits."""
    banned_xuids()
    return _state["keys"]


def cached():
    """The list as last read, without looking at the file: for the per-datagram check (the
    server's tick reads the file every few seconds)."""
    return _state["xuids"]


def banned(xuid):
    """Whether an XUID (an int, or a hex string as the stores keep them) is banned."""
    if isinstance(xuid, str):
        return xuid.upper() in banned_keys()
    return xuid in banned_xuids()


def refused(xuid, what):
    """True (and a log line, at most every LOG_EVERY seconds for the same XUID and write) when
    the XUID is banned: the caller then writes nothing."""
    if not xuid or not banned(xuid):
        return False
    x = int(xuid, 16) if isinstance(xuid, str) else xuid
    now = time.monotonic()
    if now - _logged.get((x, what), -LOG_EVERY) >= LOG_EVERY:
        if len(_logged) > 10000:
            _logged.clear()
        _logged[(x, what)] = now
        log.info("bans: %016X is banned: %s refused", x, what)
    return True


def visible(entries, field="xuid"):
    """The entries (dicts) whose player is not banned; `field` holds the XUID (int or hex)."""
    keys = banned_keys()
    if not keys:
        return list(entries)
    out = []
    for e in entries:
        v = e.get(field)
        k = ("%016X" % v) if isinstance(v, int) else str(v or "").upper()
        if k not in keys:
            out.append(e)
    return out


def listen(fn):
    """fn(xuid, banned) after set_ban changed the list. A bound method is held weakly (a server
    that is gone is not called)."""
    ref = weakref.WeakMethod(fn) if hasattr(fn, "__self__") else (lambda: fn)
    _listeners.append(ref)


def set_ban(xuid, ban=True):
    """Puts the XUID on the ban list (or takes it off), keeping the rest of the file as the
    operator wrote it, and tells the listeners. True when the list changed; ValueError when the
    file is not a JSON list (it is left alone)."""
    p = path()
    try:
        with open(p) as f:
            entries = json.load(f)
    except FileNotFoundError:
        entries = []
    except (OSError, ValueError) as e:
        raise ValueError("%s cannot be read (%s): fix it by hand" % (p, e)) from None
    if not isinstance(entries, list):
        raise ValueError("%s is not a JSON list: fix it by hand" % p)

    def same(v):
        try:
            return int(str(v), 16) == xuid
        except ValueError:
            return False
    if any(same(v) for v in entries) == ban:
        return False
    kept = [v for v in entries if not same(v)] + (["%016X" % xuid] if ban else [])
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(kept, f, indent=1)
    durable.replace(tmp, p)
    _state["mtime"] = None  # read again at once, whatever the file system's clock resolution
    banned_xuids()
    log.info("bans: %016X %s", xuid, "banned from the server" if ban else "unbanned")
    for ref in list(_listeners):
        fn = ref()
        if fn is None:
            _listeners.remove(ref)
            continue
        try:
            fn(xuid, ban)
        except Exception:  # one listener's failure must not keep the others from the change
            log.exception("bans: a listener failed")
    return True
