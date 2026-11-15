"""Friends on the Forza Live server (#92): requests by gamertag, accept or decline, remove
and block, kept per player (XUID) in the server's database (core/storage.py, #75; before it in
DATA_DIR/friends.json, which the first start imports).

Games up to v0.1.14 count every player in their room as a friend (the room's roster,
PEERS). A game that sends FEATURE_FRIENDS in its HELLO asks the server for its friends
list instead and shows only those players in the game's friends list (Xbox Guide friends,
presence, join a friend) and first in the invite picker. Everything else stays as it was:
the roster still lists the whole room (matchmaking, QoS and stats need it), and old games
neither send nor get FRIEND messages.

Who is who: the XUID a game reports is not proven, so the first game that uses the friends
of an XUID binds them to its key: games with signed requests sign the HELLO with their Ed25519
key and the server keeps the public key ("pub", auth.py); games before them sent their
friends key (16 random bytes in live_identity.txt) after the HELLO features byte and the server
kept a SHA-256 of it ("key"), until that game's player upgrades. A game with the same XUID and
another key gets no friends list and cannot change it.

Message FRIEND (UDP kind 22), body op u8 then (big endian; strings u8 length + UTF-8):
  client -> server
    1 LIST     version u32 (of the last list it got, 0 for none)
    2 SEARCH   request u16, query
    3 REQUEST  xuid u64 (0: find the gamertag), gamertag
    4 ACCEPT   xuid u64
    5 DECLINE  xuid u64 (an incoming request; also takes back one we sent)
    6 REMOVE   xuid u64
    7 BLOCK    xuid u64, gamertag
    8 UNBLOCK  xuid u64
  server -> client
    16 LIST    version u32, total u16, first u16, count u16, then count entries:
               xuid u64, relation u8 (1 friend, 2 asks us, 3 we asked, 4 blocked),
               state u32 (a friend's X_ONLINE_FRIENDSTATE flags, 0 offline), gamertag,
               status (a friend's presence line). In parts that fit a datagram.
    17 SAME    version u32: the list has not changed
    18 RESULTS request u16, count u8, then xuid u64, online u8, relation u8 (0 none),
               gamertag
    19 NOTICE  event u8 (1 a request from, 2 accepted by, 3 text), xuid u64, gamertag, text

Limits: MAX_FRIENDS friends, MAX_PENDING requests waiting each way, MAX_BLOCKED blocks;
REQUESTS_PER_MIN requests sent, ACTIONS_PER_MIN other changes, SEARCHES_PER_MIN searches
per player. A request to a player who blocked you looks sent but is dropped. The file holds at
most limits.MAX_PLAYERS records with a key (the cap of the player records; new bindings come at a
bounded rate, auth.FirstUse) and as many without one, which only other players' actions make (a
name someone blocked, a request to a player who has not signed in with a key): those never take
a new player's place, and the oldest that hold nothing but a name go when there are too many. So
no sender can grow the file without end, nor keep new players out of it.

On disk (#75): two tables of the server's database (storage.FILE). friends_players has a row per
record (xuid, name, seen, key, pub, the names of its maps, and any other field as JSON in extra);
friends_relations a row per entry of its maps (xuid, map: friends, in, out, blocked or muted,
other, since). The Store keeps every record in memory, as before, and every read is from there.
How a change reaches the tables is storage.Records' (the Store is one, with KIND): a change
marks the record (touch); right after the work that made it (loop.call_soon), the changed
records' rows are made, on the loop, and handed to the database's writer thread as one write
(write_rows), which replaces their rows; the loop never waits for the disk. save() waits for
every change so far to be committed: the stop, and a tool; written() is the same for a
coroutine, off the loop (an event's enrollment, events.enroll). A write that fails is made again
with the records as they are then, at the next tick (maybe_save). A kill loses at most the
changes of the last few milliseconds that were not committed yet; the database is never left
half written.
"""

import collections
import hashlib
import json
import logging
import os
import struct
import threading
import time
import zlib

from sqlalchemy import (Column, Float, ForeignKey, MetaData, Table, Text, bindparam,
                        literal_column, select)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from forza_live.core import auth
from forza_live.core import bans
from forza_live.core import durable
from forza_live.core import limits
from forza_live.core import storage

log = logging.getLogger("forza-live")

KIND = 22
FEATURE = 0x02  # HELLO features byte: the game uses the server's friends list
LIST, SEARCH, REQUEST, ACCEPT, DECLINE, REMOVE, BLOCK, UNBLOCK = range(1, 9)
R_LIST, R_SAME, R_RESULTS, R_NOTICE = 16, 17, 18, 19
FRIEND, INCOMING, OUTGOING, BLOCKED = 1, 2, 3, 4
N_REQUEST, N_ACCEPTED, N_TEXT = 1, 2, 3

FILE = "friends.json"  # the store before the database (imported once, then kept as .imported)
NAME = "friends"  # its row in the database's stores table
VERSION = 1  # of the tables below
MAX_FRIENDS = 200
MAX_PENDING = 50
MAX_BLOCKED = 200
MAX_RESULTS = 20
REQUESTS_PER_MIN = 10
ACTIONS_PER_MIN = 60
SEARCHES_PER_MIN = 30
LISTS = (0.5, 4)  # list answers per second and burst
# A record with no key, friends, requests or blocks (a name someone blocked) is forgotten after this
FORGET_DAYS = 180
# When the records without a key are at their cap and none of them can go (Store.spare), the
# store is not searched again for this long: the search reads every record.
SPARE_SEARCH = 60.0
KEY_SIZE = 16
STATUS_MAX = 60

# --- the tables (the module's docstring, "On disk") ---

TABLES = MetaData()
PLAYERS = Table(
    "friends_players", TABLES,
    Column("xuid", Text, primary_key=True),  # the record's key: its XUID, 16 hex digits
    Column("name", Text),  # NULL: the record has no such field (and so for seen, key and pub)
    Column("seen", Float),
    Column("key", Text),  # SHA-256 of a raw friends key, "" for none
    Column("pub", Text),  # the public key it is bound to (hex)
    Column("maps", Text, nullable=False),  # the record's maps, comma separated
    Column("extra", Text))  # any other field, as JSON (NULL: none)
RELATIONS = Table(
    "friends_relations", TABLES,
    Column("xuid", Text, ForeignKey("friends_players.xuid", ondelete="CASCADE"),
           primary_key=True),
    Column("map", Text, primary_key=True),  # friends, in, out, blocked, muted
    Column("other", Text, primary_key=True),  # the other player's XUID
    Column("since", Float))
SCALARS = {"name": str, "seen": (int, float), "key": str, "pub": str}
MAPS = ("friends", "in", "out", "blocked", "muted")
ROWS_AT_ONCE = 500  # keys in one IN (...) of a write


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def rows_of(k, p):
    """The record's row and its relations' rows. A field in its usual form is a column (a map of
    XUID -> time is relations' rows); one in any other form goes as it is into extra, so that
    every record reads back as it was."""
    row = {"xuid": k, "name": None, "seen": None, "key": None, "pub": None}
    maps, extra, relations = [], {}, []
    for field, value in p.items():
        kind = SCALARS.get(field)
        if kind is not None and isinstance(value, kind) and (kind is str or _number(value)):
            row[field] = value
        elif field in MAPS and isinstance(value, dict) and all(
                isinstance(o, str) and _number(t) for o, t in value.items()):
            maps.append(field)
            relations += [{"xuid": k, "map": field, "other": o, "since": t}
                          for o, t in value.items()]
        else:
            extra[field] = value
    row["maps"] = ",".join(maps)
    row["extra"] = json.dumps(extra) if extra else None
    return row, relations


def read_all(conn):
    """{xuid hex: record} as the tables have them, in the order the records were made (rowid)."""
    maps = collections.defaultdict(dict)
    for r in conn.execute(select(RELATIONS.c.xuid, RELATIONS.c.map, RELATIONS.c.other,
                                 RELATIONS.c.since).order_by(literal_column("rowid"))):
        maps[r.xuid, r.map][r.other] = r.since
    players = {}
    for r in conn.execute(select(PLAYERS).order_by(literal_column("rowid"))):
        players[r.xuid] = _record(r, maps)
    return players


ONE = (select(PLAYERS, RELATIONS.c.map.label("rel_map"), RELATIONS.c.other, RELATIONS.c.since)
       .select_from(PLAYERS.outerjoin(RELATIONS, RELATIONS.c.xuid == PLAYERS.c.xuid))
       .where(PLAYERS.c.xuid == bindparam("k")).order_by(RELATIONS.c.xuid, literal_column(
           "friends_relations.rowid")))


def read_one(conn, k):
    """The record of xuid hex k as the tables have it, or None: one statement."""
    found = conn.execute(ONE, {"k": k}).all()
    if not found:
        return None
    maps = collections.defaultdict(dict)
    for e in found:
        if e.rel_map is not None:
            maps[k, e.rel_map][e.other] = e.since
    return _record(found[0], maps)


def _record(r, maps):
    p = {f: getattr(r, f) for f in SCALARS if getattr(r, f) is not None}
    for m in r.maps.split(",") if r.maps else ():
        p[m] = maps.get((r.xuid, m), {})
    if r.extra:
        p.update(json.loads(r.extra))
    return p


def _chunks(items):
    items = list(items)
    for i in range(0, len(items), ROWS_AT_ONCE):
        yield items[i:i + ROWS_AT_ONCE]


def _insert(conn, rows, relations, upsert=False):
    if rows:
        stmt = sqlite_insert(PLAYERS)
        if upsert:
            stmt = stmt.on_conflict_do_update(
                index_elements=[PLAYERS.c.xuid],
                set_={c.name: stmt.excluded[c.name] for c in PLAYERS.c if c.name != "xuid"})
        conn.execute(stmt, rows)
    if relations:
        conn.execute(RELATIONS.insert(), relations)


def replace_all(conn, players):
    """The tables made to hold just `players` ({xuid hex: record}; the writer's)."""
    conn.execute(RELATIONS.delete())
    conn.execute(PLAYERS.delete())
    rows, relations = [], []
    for k, p in players.items():
        row, rel = rows_of(k, p)
        rows.append(row)
        relations += rel
    _insert(conn, rows, relations)


def write_rows(conn, made, drop):
    """The changed records' rows (rows_of's, as storage.Records.flush made them, in the order the
    records changed): their relations replaced, the records in `drop` (gone, or made anew:
    deleted first, so that a new one's row comes last, as its record does in memory) deleted,
    the others put."""
    rows = [row for row, _ in made]
    relations = [r for _, rel in made for r in rel]
    put = [r["xuid"] for r in rows]
    keys = put + sorted(set(drop) - set(put))
    for chunk in _chunks(keys):
        conn.execute(RELATIONS.delete().where(RELATIONS.c.xuid.in_(chunk)))
    for chunk in _chunks(drop):
        conn.execute(PLAYERS.delete().where(PLAYERS.c.xuid.in_(chunk)))
    _insert(conn, rows, relations, upsert=True)


def parse_file(data):
    """The records of a friends.json ({"version": 1, "players": {...}}), checked as the store's
    load before the database read them: a file of another shape raises."""
    players = data["players"]
    if not isinstance(players, dict):
        raise TypeError("players: not an object")
    for k, p in players.items():
        if not isinstance(p, dict) or not all(isinstance(p[m], dict) for m in MAPS[:4]):
            raise TypeError("players[%r]: not a record" % k)
    return players


def export_data(conn):
    """friends.json as the versions before the database wrote it (storage export)."""
    return {"version": 1, "players": read_all(conn)}


# The friends' tables for the storage layer (core/storage.py): its writes, import and export.
STORAGE = storage.Kind(NAME, VERSION, TABLES, read_all, replace_all, rows_of, write_rows,
                       file=FILE, parse_file=parse_file, export_data=export_data)


def forgotten(p, cutoff):
    """A record the start forgets: no key, nothing in its maps, not seen since cutoff. A record
    with a key stays however long its player is away: forgotten, its XUID would go to the first
    key that names it, with the player's record and scores."""
    return not (p.get("friends") or p.get("in") or p.get("out") or p.get("blocked")
                or p.get("pub") or p.get("key") or not _number(p.get("seen"))
                or p["seen"] >= cutoff)


def text8(s):
    b = s.encode()[:255]
    while True:
        try:
            b.decode()
            break
        except UnicodeDecodeError:
            b = b[:-1]
    return bytes([len(b)]) + b


def read8(body, pos):
    n = body[pos]
    return body[pos + 1:pos + 1 + n].decode("utf-8", "replace"), pos + 1 + n


def key_hash(key):
    return hashlib.sha256(bytes(key)).hexdigest()


class Full(Exception):
    """The store has no room for another record without a key (Store.get)."""


class Store(storage.Records):
    """The players' relations: {xuid: {"name", "seen", "key", "friends", "in", "out",
    "blocked"}}, each relation a {xuid hex: since} map, kept symmetric (A's "out" is B's
    "in"). path: friends.json in the data folder, whose database (storage.FILE) the store is
    kept in once loaded; None: in memory only. Its records, their changes and their writes are
    storage.Records' (mark, flush, save, written)."""

    KIND = STORAGE

    def __init__(self, path=None):
        super().__init__(path)
        self.on_rebind = []  # fn(xuid, old binding, new binding): auth.rebind
        self.first_use = auth.FirstUse()  # the budgets of new bindings
        self.checks = auth.Checks()  # and of signature checks per client address
        # The records with a key ("pub" or "key"): counted as they are bound (bind) and again
        # at every load, and every 2 s while the store changes (maybe_save).
        self.keyed = 0
        self.searched = 0.0  # monotonic time until which spare() does not look for room again
        self._counted = 0.0  # monotonic time of the last count() by maybe_save

    @property
    def players(self):
        return self.records

    @players.setter
    def players(self, players):
        """Replaced as a whole (a test, a tool): written as a whole."""
        self.records = players

    def touch(self, xuid):
        """The XUID's record changed (a caller that changes it in place, chat's mutes)."""
        self.mark(self.hx(xuid))

    @staticmethod
    def hx(xuid):
        return "%016X" % xuid

    @staticmethod
    def has_key(p):
        return bool(p.get("pub") or p.get("key"))

    def count(self):
        self.keyed = sum(1 for p in self.players.values() if self.has_key(p))

    def full(self):
        """No room for another player's key: limits.MAX_PLAYERS records have one (records
        already here stay, and are all loaded). Only a player's own first sign-in makes such a
        record, so nothing anyone else does fills this."""
        return self.keyed >= limits.MAX_PLAYERS

    def spare(self):
        """Whether a record without a key may be added: there are fewer than limits.MAX_PLAYERS
        of them, once those that hold nothing but a name (the oldest first) have made room."""
        cap = limits.MAX_PLAYERS
        if len(self.players) - self.keyed < cap:
            return True
        now = time.monotonic()
        if now < self.searched:  # looked a moment ago, and found none to drop
            return False
        self.count()  # exact, before anything is dropped for it
        over = len(self.players) - self.keyed - cap
        if over < 0:
            return True
        names = sorted((p["seen"], k) for k, p in self.players.items()
                       if not self.has_key(p) and not (p["friends"] or p["in"] or p["out"]
                                                       or p["blocked"] or p.get("muted")))
        for _, k in names[:over + max(1, cap // 10)]:  # a tenth at once: not a scan for each
            del self.players[k]
            self.mark(k)
        if len(self.players) - self.keyed < cap:
            return True
        self.searched = now + SPARE_SEARCH
        return False

    def get(self, xuid, create=False, own=False):
        """The XUID's record, made when `create`. `own`: for the key its player is being bound
        to (the caller saw to full()); else a record without a key, which Full refuses when
        there is no room for one (the message that asked for it is dropped,
        server.datagram_received, or answered without it)."""
        k = self.hx(xuid)
        p = self.players.get(k)
        if p is None and create:
            if not own and not self.spare():
                raise Full("friends store: no room for a record without a key (%d records)"
                           % len(self.players))
            p = self.players[k] = {"name": "", "seen": time.time(), "key": "", "friends": {},
                                   "in": {}, "out": {}, "blocked": {}}
            self.mark(k, fresh=True)
        return p

    def bind(self, p, pub="", key="", xuid=None):
        """Binds the record (of `xuid`) to a public key (hex), or to the SHA-256 of a raw friends
        key."""
        if not self.has_key(p):
            self.keyed += 1
        if pub:
            p["pub"], p["key"] = pub, ""
        else:
            p["key"] = key
        if xuid is not None:
            self.mark(self.hx(xuid))
        else:  # found by the record itself: a binding is rare, a scan is cheap enough
            for k, q in self.players.items():
                if q is p:
                    self.mark(k)
                    break

    def relation(self, a, b):
        """What b is to a (FRIEND, INCOMING, OUTGOING, BLOCKED, or 0)."""
        p = self.get(a)
        if not p:
            return 0
        k = self.hx(b)
        for rel, field in ((BLOCKED, "blocked"), (FRIEND, "friends"), (INCOMING, "in"),
                           (OUTGOING, "out")):
            if k in p[field]:
                return rel
        return 0

    def blocks(self, a, b):
        """a has blocked b."""
        p = self.get(a)
        return bool(p) and self.hx(b) in p["blocked"]

    def seen(self, xuid, name):
        p = self.get(xuid, create=True)
        name = limits.clean_gamertag(name, xuid) if name else name
        if name and p["name"] != name:
            p["name"] = name
            self.touch(xuid)
        if time.time() - p["seen"] > 3600:
            p["seen"] = time.time()
            self.touch(xuid)

    def claim(self, xuid, key):
        """True if this raw friends key may use the XUID's friends (binding it on first use).
        Never for an XUID bound to a public key (auth.py)."""
        if len(key) != KEY_SIZE:
            return False
        p = self.get(xuid)
        if (p is None or not self.has_key(p)) and self.full():
            return False
        if p is None:
            p = self.get(xuid, create=True, own=True)
        if p.get("pub"):
            return False
        h = key_hash(key)
        if not p["key"]:
            self.bind(p, key=h, xuid=xuid)
            return True
        return p["key"] == h

    def unlink(self, a, b):
        """Drops every relation between a and b except blocks."""
        pa, pb = self.get(a), self.get(b)
        ka, kb = self.hx(a), self.hx(b)
        changed = False
        for p, k, own in ((pa, kb, ka), (pb, ka, kb)):
            if not p:
                continue
            for field in ("friends", "in", "out"):
                if p[field].pop(k, None) is not None:
                    changed = True
                    self.mark(own)
        return changed

    def befriend(self, a, b):
        self.unlink(a, b)
        now = time.time()
        self.get(a, True)["friends"][self.hx(b)] = now
        self.get(b, True)["friends"][self.hx(a)] = now
        self.touch(a)
        self.touch(b)

    def ask(self, a, b):
        now = time.time()
        self.get(a, True)["out"][self.hx(b)] = now
        self.get(b, True)["in"][self.hx(a)] = now
        self.touch(a)
        self.touch(b)

    def block(self, a, b):
        self.unlink(a, b)
        self.get(a, True)["blocked"][self.hx(b)] = time.time()
        self.touch(a)

    def unblock(self, a, b):
        p = self.get(a)
        if p and p["blocked"].pop(self.hx(b), None) is not None:
            self.touch(a)
            return True
        return False

    def find(self, name):
        """XUIDs of the players with this gamertag (any case), most recently seen first."""
        name = name.lower()
        found = [(p["seen"], int(k, 16)) for k, p in self.players.items()
                 if p["name"] and p["name"].lower() == name]
        return [x for _, x in sorted(found, reverse=True)]

    def name(self, xuid):
        p = self.get(xuid)
        return limits.clean_gamertag(p["name"] if p else "", xuid)

    # --- the database (the module's docstring, "On disk"; core/storage.py's Records) ---

    def load(self):
        """The records from the database in the data folder (friends.json imported into it at
        the first start: storage.import_once), what a start forgets left out (and deleted from
        the tables). storage.Refused, logged, when they cannot be read: the server does not start
        on an empty store, whose first change would put it over what it could not read."""
        if not self.path:
            return
        players = self.open()
        cutoff = time.time() - FORGET_DAYS * 86400
        kept = {k: p for k, p in players.items() if not forgotten(p, cutoff)}
        self.loaded(kept, gone=[k for k in players if k not in kept])
        self.count()
        log.info("friends: %d player(s) from %s, %d with a key", len(self.players), self.db.name,
                 self.keyed)

    def save(self, timeout=storage.CLOSE_WAIT):
        ok = super().save(timeout)
        if self.path:
            self.count()
        return ok

    def maybe_save(self):
        """Every second from the main loop: a write that failed is made again (Records.tick),
        and the records with a key are counted again every 2 s while the store changes. A change
        is handed to the writer right after the work that made it; this is for what is left."""
        changed = self.tick()
        now = time.monotonic()
        if changed and now - self._counted >= 2:
            self._counted = now
            self.count()


class Reader:
    """The records as the server has them now, one at a time and read-only, for another process:
    the website accounts service reads the data folder through a read-only mount
    (accounts/members/game.py). get() as Store.get (what a start forgets left out), with the
    has_key and hx of a Store, so that auth.bound_pub and auth.check_request take it as one.

    It reads whichever the server keeps the friends in, so that the accounts service and the
    server can be updated in either order: the database once it holds the store
    (storage.holds: it imported friends.json, or started without one, and no rollback is
    waiting), else friends.json while it is there (a server from before the database, one that
    has not imported it yet, a rollback to one), read whole as the previous versions read it,
    again whenever it changes. Each thread keeps its connection to the database while the file
    stays the same file (an open connection reads every commit; a restored copy is a new file);
    one that could only read the file as it is (the server stopped, the folder read-only) is not
    kept, so the next get sees a server that started meanwhile."""

    hx = staticmethod(lambda xuid: "%016X" % xuid)

    def __init__(self, folder):
        self.path = os.path.join(folder, storage.FILE)
        self.file = os.path.join(folder, FILE)
        self.engines = None
        self.local = threading.local()
        self._lock = threading.Lock()  # the file's records, read again by one thread at a time
        self._from_file = (None, {})  # (the file's stat, its records)

    @staticmethod
    def has_key(p):
        return Store.has_key(p)

    def get(self, xuid, create=False, own=False):
        p = self._read(self.hx(xuid))
        if p is None or forgotten(p, time.time() - FORGET_DAYS * 86400):
            return None
        return p

    def _read(self, k):
        try:
            file_there = os.path.lexists(self.file)
            st = os.stat(self.path)
        except OSError:  # no database: a server from before it (or none yet)
            self._drop()
            return self._file_record(k) if file_there else None
        if self.engines is None:
            self.engines = (storage.engine_for(self.path, read_only=True, fallback=False),
                            storage.engine_for(self.path, read_only=True, immutable=True))
        local, same = self.local, (st.st_dev, st.st_ino)
        if getattr(local, "conn", None) is not None and local.file != same:
            self._drop()
        try:
            conn, kept = getattr(local, "conn", None), True
            if conn is None:
                try:
                    conn = self.engines[0].connect()
                except SQLAlchemyError:
                    conn, kept = self.engines[1].connect(), False
                if kept:
                    local.conn, local.file = conn, same
            try:
                with conn.begin():
                    # friends.json beside it: the database is the store only once it holds it
                    from_file = file_there and not storage.holds(*_note(conn))
                    p = None if from_file else read_one(conn, k)
            finally:
                if not kept:
                    conn.close()
        except SQLAlchemyError as e:  # no tables yet (a server before its first start), a lock
            self._drop()
            if file_there:
                return self._file_record(k)
            log.warning("friends: %s cannot be read now: %s", self.path, e)
            return None
        return self._file_record(k) if from_file else p

    def _file_record(self, k):
        """The record in friends.json, read whole when the file changed (durable.load: its .bak
        when it is damaged), or None."""
        with self._lock:
            try:
                st = os.stat(self.file)
                stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            except OSError:
                return None
            if self._from_file[0] != stamp:
                try:
                    players = parse_file(durable.load(self.file))
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    log.warning("friends: %s cannot be read now: %s", self.file, e)
                    return None
                self._from_file = (stamp, players)
            return self._from_file[1].get(k)

    def _drop(self):
        conn = getattr(self.local, "conn", None)
        self.local.conn = None
        if conn is not None:
            try:
                conn.close()
            except SQLAlchemyError:
                pass

    def close(self):
        """This thread's connection closed, and the engines (another thread's connection closes
        with its thread)."""
        self._drop()
        for engine in self.engines or ():
            engine.dispose()
        self.engines = None
        self._from_file = (None, {})


def _note(conn):
    """The friends' (revision, note) in the stores table, (0, {}) without one."""
    try:
        return storage.state(conn, NAME)
    except SQLAlchemyError:  # a database without the stores table
        return 0, {}


class Friends:
    """The FRIEND messages. `server` is the ForzaLive (its peers, send and spend)."""

    def __init__(self, server, path=None):
        self.server = server
        self.store = Store(path)
        self.buckets = {}  # (xuid, what) -> token bucket

    # --- who is online ---

    def online(self, xuid):
        """The peers signed in with this XUID."""
        return [p for p in self.server.peers.values() if p.xuid == xuid]

    def users(self, xuid):
        """The XUID's peers that use the friends list (and own it)."""
        return [p for p in self.online(xuid)
                if p.features & FEATURE and getattr(p, "friends_ok", False)]

    def limited(self, xuid, what, per_minute, burst=None):
        rate = per_minute / 60.0
        burst = burst or max(1, per_minute // 2)
        now = time.monotonic()
        tokens, last = self.buckets.get((xuid, what), (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        ok = tokens >= 1
        self.buckets[(xuid, what)] = (tokens - 1 if ok else tokens, now)
        if len(self.buckets) > 20000:
            self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 120}
        return not ok

    # --- HELLO ---

    def hello(self, peer, key, signed=b""):
        """A HELLO with FEATURE: checks what its friends key slot holds (`key`: a signed auth
        block, auth.py, or the raw key of an older game; `signed`: the HELLO up to its
        features byte, which the signature covers) and records the gamertag if it proves the
        XUID."""
        if not peer.features & FEATURE:
            peer.friends_ok = False
            return
        ok = auth.hello(self.store, peer, key, signed)
        if ok:  # the gamertag friends see: only from a game that proves the XUID
            self.store.seen(peer.xuid, peer.name)
        if ok != getattr(peer, "friends_ok", None):
            if not ok and len(key) == KEY_SIZE:
                log.info("friends #%d %r: XUID %016X has another friends key", peer.id,
                         peer.name, peer.xuid)
            peer.friends_ok = ok
        # Once a minute for each game: not for each XUID, which any other game can name.
        if getattr(peer, "auth_conflict", False) and not self.limited(("game", peer.id), "linked", 1):
            self.tell(peer, auth.refusal(peer, "Friends and chat are off"))

    def came_or_went(self, xuid):
        """A player signed in or out: its friends' lists show it."""
        p = self.store.get(xuid)
        if not p:
            return
        for k in list(p["friends"])[:MAX_FRIENDS]:
            self.push(int(k, 16))

    # --- the list ---

    def entries(self, xuid):
        p = self.store.get(xuid)
        if not p:
            return []
        rows = []
        hidden = bans.banned_keys()
        for rel, field in ((FRIEND, "friends"), (INCOMING, "in"), (OUTGOING, "out"),
                           (BLOCKED, "blocked")):
            for k in p[field]:
                if rel == INCOMING and k.upper() in hidden:
                    continue  # a banned player's request waits, unseen, until the ban is lifted
                x = int(k, 16)
                state, status = 0, ""
                if rel == FRIEND:
                    on = self.users(x)  # games that prove the XUID: any game can name it
                    if on:
                        state, status = on[0].state or 1, on[0].status
                rows.append((rel, 0 if state else 1, self.store.name(x).lower(), x, state,
                             status))
        rows.sort()
        return [struct.pack(">QBI", x, rel, state) + text8(self.store.name(x)) +
                text8(status[:STATUS_MAX]) for rel, _, _, x, state, status in rows]

    def send_list(self, peer, known=0):
        entries = self.entries(peer.xuid)
        version = zlib.crc32(b"".join(entries)) or 1
        if known == version:
            self.server.send(peer.addr, KIND, struct.pack(">BI", R_SAME, version))
            return
        start = 0
        while True:
            part, size = [], 11
            while start + len(part) < len(entries) and size + len(entries[start + len(part)]) <= 1200:
                size += len(entries[start + len(part)])
                part.append(entries[start + len(part)])
            self.server.send(peer.addr, KIND, struct.pack(">BIHHH", R_LIST, version, len(entries),
                                                          start, len(part)) + b"".join(part))
            start += len(part)
            if start >= len(entries) or not part:
                break

    def push(self, xuid):
        for peer in self.users(xuid):
            self.send_list(peer)

    def notice(self, xuid, event, other, text=""):
        body = struct.pack(">BBQ", R_NOTICE, event, other) + text8(
            self.store.name(other) if other else "") + text8(text)
        for peer in self.users(xuid):
            self.server.send(peer.addr, KIND, body)

    def tell(self, peer, text):
        self.server.send(peer.addr, KIND, struct.pack(">BBQ", R_NOTICE, N_TEXT, 0) + text8("") +
                         text8(text))

    # --- messages ---

    def on_message(self, peer, body):
        if not peer.features & FEATURE:
            return
        if not getattr(peer, "friends_ok", False):
            # A game that signs sends its first FRIEND messages before its answer to the
            # challenge arrives (auth.py): no notice for those.
            if not getattr(peer, "auth_pending", False) and not self.limited(peer.xuid, "denied", 2):
                self.tell(peer, auth.refusal(peer, "Friends are off") or "Friends are off: this XUID's "
                                "friends belong to another installation (live_identity.txt)")
            return
        op, me = body[0], peer.xuid
        if op == LIST:
            if len(body) >= 5 and not self.limited(me, "list", LISTS[0] * 60, LISTS[1]):
                self.send_list(peer, struct.unpack_from(">I", body, 1)[0])
        elif op == SEARCH:
            if not self.limited(me, "search", SEARCHES_PER_MIN, 10):
                (request,) = struct.unpack_from(">H", body, 1)
                query, _ = read8(body, 3)
                self.search(peer, request, query)
        elif op in (REQUEST, ACCEPT, DECLINE, REMOVE, BLOCK, UNBLOCK):
            (other,) = struct.unpack_from(">Q", body, 1)
            name = read8(body, 9)[0] if op in (REQUEST, BLOCK) and len(body) > 9 else ""
            if op == REQUEST:
                if self.limited(me, "request", REQUESTS_PER_MIN):
                    self.tell(peer, "Too many friend requests; wait a minute")
                    return
            elif self.limited(me, "action", ACTIONS_PER_MIN, 30):
                self.tell(peer, "Too many changes; wait a minute")
                return
            if op == REQUEST:
                self.request(peer, other, name)
            elif op == ACCEPT:
                self.accept(peer, other)
            elif op == DECLINE:
                self.decline(peer, other)
            elif op == REMOVE:
                self.remove(peer, other)
            elif op == BLOCK:
                self.block(peer, other, name)
            else:
                self.unblock(peer, other)

    def resolve(self, peer, xuid, name):
        """The XUID a request names: given, else the player with that gamertag (online in
        the same room first, then online, then the most recently seen)."""
        if xuid:
            if self.store.get(xuid) or self.online(xuid):
                return xuid
            return 0
        name = name.strip()
        if not name:
            return 0
        on = sorted((p for p in self.server.peers.values()
                     if p.name.lower() == name.lower() and p.xuid != peer.xuid),
                    key=lambda p: p.room != peer.room)
        if on:
            return on[0].xuid
        found = [x for x in self.store.find(name) if x != peer.xuid]
        return found[0] if found else 0

    def request(self, peer, other, name):
        me = peer.xuid
        other = self.resolve(peer, other, name)
        if not other:
            self.tell(peer, "No player named %s" % (name or "that"))
            return
        if other == me:
            self.tell(peer, "That's you")
            return
        try:
            for p in self.online(other):  # it may have signed in before it had the feature
                self.store.seen(other, p.name)
            theirs = self.store.get(other, True)
        except Full:  # a player with no record, and no room for one without a key
            self.tell(peer, "%s can't take friend requests right now" % (name or "That player"))
            return
        tag = self.store.name(other)
        rel = self.store.relation(me, other)
        if rel == FRIEND:
            self.tell(peer, "%s is already your friend" % tag)
            return
        if rel == BLOCKED:
            self.tell(peer, "You blocked %s; unblock them first" % tag)
            return
        if rel == INCOMING:  # they asked us: that's a yes
            self.accept(peer, other)
            return
        if rel == OUTGOING:
            self.tell(peer, "You already asked %s" % tag)
            return
        mine = self.store.get(me, True)
        if len(mine["friends"]) >= MAX_FRIENDS:
            self.tell(peer, "Your friends list is full (%d)" % MAX_FRIENDS)
            return
        if len(mine["out"]) >= MAX_PENDING:
            self.tell(peer, "You have %d requests waiting; cancel some first" % MAX_PENDING)
            return
        if self.store.blocks(other, me):
            log.info("friends #%d %r -> %r: request dropped (blocked)", peer.id, peer.name, tag)
            self.tell(peer, "Friend request sent to %s" % tag)
            return
        if len(theirs["in"]) >= MAX_PENDING or len(theirs["friends"]) >= MAX_FRIENDS:
            self.tell(peer, "%s can't take more friend requests right now" % tag)
            return
        self.store.ask(me, other)
        log.info("friends #%d %r asks %r (%016X)", peer.id, peer.name, tag, other)
        self.tell(peer, "Friend request sent to %s" % tag)
        self.notice(other, N_REQUEST, me)
        self.push(me)
        self.push(other)

    def accept(self, peer, other):
        me = peer.xuid
        if self.store.relation(me, other) != INCOMING:
            return
        mine, theirs = self.store.get(me, True), self.store.get(other, True)
        if len(mine["friends"]) >= MAX_FRIENDS or len(theirs["friends"]) >= MAX_FRIENDS:
            self.tell(peer, "A friends list is full (%d)" % MAX_FRIENDS)
            return
        self.store.befriend(me, other)
        log.info("friends #%d %r and %r are friends", peer.id, peer.name, self.store.name(other))
        self.tell(peer, "You and %s are friends" % self.store.name(other))
        self.notice(other, N_ACCEPTED, me)
        self.push(me)
        self.push(other)

    def decline(self, peer, other):
        if self.store.relation(peer.xuid, other) in (INCOMING, OUTGOING):
            self.store.unlink(peer.xuid, other)
            self.push(peer.xuid)
            self.push(other)

    def remove(self, peer, other):
        if self.store.relation(peer.xuid, other) == FRIEND:
            self.store.unlink(peer.xuid, other)
            log.info("friends #%d %r removed %r", peer.id, peer.name, self.store.name(other))
            self.tell(peer, "%s is no longer your friend" % self.store.name(other))
            self.push(peer.xuid)
            self.push(other)

    def block(self, peer, other, name):
        me = peer.xuid
        if not other or other == me:
            return
        p = self.store.get(me, True)
        if len(p["blocked"]) >= MAX_BLOCKED and self.store.hx(other) not in p["blocked"]:
            self.tell(peer, "You blocked %d players already" % MAX_BLOCKED)
            return
        if name and not self.store.get(other):
            try:  # the name the blocker saw, for its list: a record nobody's key is bound to
                self.store.seen(other, name[:15])
            except Full:
                pass  # no room for one: blocked all the same, under the name its XUID gives
        self.store.block(me, other)
        log.info("friends #%d %r blocked %r", peer.id, peer.name, self.store.name(other))
        self.tell(peer, "%s is blocked" % self.store.name(other))
        self.push(me)
        self.push(other)

    def unblock(self, peer, other):
        if self.store.unblock(peer.xuid, other):
            self.tell(peer, "%s is no longer blocked" % self.store.name(other))
            self.push(peer.xuid)

    def search(self, peer, request, query):
        """Players whose gamertag contains the query: online ones first."""
        q = query.strip().lower()
        found = {}
        hidden = bans.banned_xuids()  # banned players are found by nobody (core/bans.py)
        if q:
            for p in self.server.peers.values():
                if p.xuid != peer.xuid and q in p.name.lower() and p.xuid not in hidden:
                    found.setdefault(p.xuid, (p.name, 1))
            if len(q) >= 2 or len(found) < MAX_RESULTS:
                for k, p in self.store.players.items():
                    x = int(k, 16)
                    # Stored names can predate the ASCII rule: match and send the cleaned one.
                    name = self.store.name(x) if p["name"] else ""
                    if name and q in name.lower() and x != peer.xuid and x not in hidden:
                        found.setdefault(x, (name, 0))
        rows = sorted(found.items(), key=lambda kv: (kv[1][0].lower() != q, -kv[1][1],
                                                     kv[1][0].lower()))[:MAX_RESULTS]
        body = struct.pack(">BHB", R_RESULTS, request, len(rows))
        for x, (name, online) in rows:
            body += struct.pack(">QBB", x, online, self.store.relation(peer.xuid, x)) + text8(
                limits.clean_gamertag(name, x))
        self.server.send(peer.addr, KIND, body)

    def status(self):
        players = self.store.players.values()
        return {"friends": {"players": len(self.store.players), "with_key": self.store.keyed,
                            "friendships": sum(len(p["friends"]) for p in players) // 2,
                            "requests": sum(len(p["out"]) for p in players)}}
