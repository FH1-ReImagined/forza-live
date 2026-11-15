"""The server's records in one SQLite database, through SQLAlchemy (#75).

Most of the server's data was JSON files written whole when something changed, on the event
loop. A store moved here (friends first, social/friends.py) keeps its records in tables of
<data folder>/forza_live.sqlite3, and follows these rules:

- Reads come from memory. The store reads its tables once, at the start (read(): a connection
  of the calling thread, its own transaction), and keeps its records in memory as before, so
  that every caller reads what it always read, as fast.
- Writes are a thread's. The store copies what changed (on the loop: a few records, a copy each)
  and hands submit() a function that writes it. One writer thread takes the submits from one
  queue, in order: the changes reach the file in the order they were made. What is waiting when
  the thread takes it becomes one transaction, with a savepoint for each submit, so that one that
  fails takes nothing else with it. A submit's future has its result only once that transaction
  is committed, which with synchronous=FULL is on disk: a store that must answer only once a
  change is kept waits for it (await asyncio.wrap_future(future)), off the loop.
- Every submit is answered. Whatever fails (the connection, the BEGIN, a statement, the commit,
  the writer thread itself), each submit the writer took from the queue ends with its result or
  with an exception, and an exception means nothing of it is in the database. A writer thread
  that stopped (a bug) answers what is queued with Closed and takes no more submits (died); a
  store (Records) then opens a new writer at its next write.
- WAL. Readers (the export below, an operator's sqlite3, the website accounts service's
  read-only mount) never wait for the writer, nor it for them. A kill or a power cut leaves the
  database as of its last commit: a change that was not committed is lost, and nothing else;
  SQLite never leaves half a transaction.
- The stop. close() lets the thread commit what is still queued, then ends it
  (server.save_at_stop, among the stores that have no journal, before the boards).
- Each store has a row in `stores`: its tables' version, a revision that each of its commits
  counts (bump()), and what it knows of the JSON file it came from (import_once()). A store
  describes its tables with a Kind; ensure() makes them, and upgrades those of an older version
  with the Kind's steps, in one transaction, after a copy of the database (backup()).
- Records keeps a store's records in memory and writes what changed: mark() a record, and right
  after the work that changed it (loop.call_soon) its rows are made on the loop and handed to
  the writer; a write that fails is made again with the next; save() and written() wait for
  every change so far. A store is a subclass with its Kind (social/friends.py's Store).

The import from a JSON file, once (import_once): at the first start with the database, the
store's file is read (durable.load: its .bak when the file is damaged) and written into its
tables in one transaction, which also notes the file's SHA-256; once that is committed the file
is renamed <file>.imported (a number after it when that name is taken), never deleted. A file
that is there but cannot be read, nor its .bak, refuses the start (Refused), as the boards'
and the notifications' do. At a later start, a file of that name next to the database is:
- the one the database imported or wrote for a rollback (the same SHA-256): nothing new in it.
  The database is the store, and the file is renamed <file>.imported.<n> or <file>.rollback.<n>
  (a rollback nobody started from: the rollback is over);
- written after the operator's rollback command (rollback()) by a previous version of the
  server, while the database has not changed since: the rollback's way back. The file has
  everything the database had and what the previous version changed: it is imported again, in
  place of the store's tables, and renamed like the first one;
- anything else (a previous version started without the rollback command, a file put back by
  hand, an export): the database is the store, because the file does not continue it; the file
  is renamed <file>.unused.<n> and the log says so, and how to take it instead (the import
  command, docs/operating.md).
Reading the database (the export, a backup, sqlite3) never changes what a start does: only the
rollback command does, and only until the database's next commit.

For the operator (docs/operating.md, "The database"):
  python3 -m forza_live.core.storage --data-dir DIR backup FILE   a copy, while the server runs
  python3 -m forza_live.core.storage --data-dir DIR export friends [FILE]   friends.json as the
      previous versions wrote it (stdout without FILE), read-only
  python3 -m forza_live.core.storage --data-dir DIR rollback friends   DIR/friends.json for a
      previous version, with the server stopped; this version's next start takes it back
  python3 -m forza_live.core.storage --data-dir DIR import friends FILE   that file in place of
      the store's records, with the server stopped
"""

import argparse
import asyncio
import atexit
import concurrent.futures
import hashlib
import json
import logging
import os
import pickle
import queue
import sqlite3
import sys
import threading
import time

from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, event, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from forza_live.core import durable
from forza_live.core import journal

log = logging.getLogger("forza-live")

FILE = "forza_live.sqlite3"
BATCH = 256  # submits at most in one transaction
CLOSE_WAIT = 15.0  # s the stop waits for the queue (compose gives the stop 20 s)
BUSY_WAIT_MS = 10000  # another process's transaction (a backup, an export) is waited for this long
REOPEN_WAIT = 10.0  # s between two new writers for a store whose writer stopped (Records)

META = MetaData()
# One row per store: the version of its tables, a revision counted by its commits, and what it
# knows of the JSON file it came from (import_once), as JSON.
STORES = Table("stores", META,
               Column("name", Text, primary_key=True),
               Column("version", Integer, nullable=False),
               Column("revision", Integer, nullable=False, default=0),
               Column("file", Text))


class Refused(journal.Unreadable):
    """The server does not start: a store's JSON file is there and can be read neither by itself
    nor by its .bak, or the database has a version of a store's tables this server cannot use
    (a newer one, or an older one without its upgrade). Logged where it is raised; server.run()
    ends the process (as for the boards' journal.Unreadable)."""


class Closed(RuntimeError):
    """A submit after close(), or after the writer thread stopped: nothing of it written."""


class Kind:
    """A store's tables, as the layer needs to know them (a store's module makes one: KIND).

    name: its row in `stores` and its name in the operator's commands; version: of its tables;
    tables: their MetaData; file: the JSON file it kept before the database (in the data
    folder), None for a store that never had one; read_all(conn): {key: record} from the tables;
    replace_all(conn, records): the tables made to hold just those; rows_of(key, record): what
    write_rows needs of one record, made on the loop (a copy: the record changes after);
    write_rows(conn, made, drop): the rows rows_of made put, and the records whose key is in
    drop deleted first (gone, or made anew: so that a new record's rows come last);
    parse_file(data): the records of a JSON file of the store (ValueError, KeyError, TypeError
    for one of another shape); export_data(conn): the data of that file, from the tables; steps:
    {version: step(conn)}, the tables of that version made into those of the next."""

    def __init__(self, name, version, tables, read_all, replace_all, rows_of, write_rows,
                 file=None, parse_file=None, export_data=None, steps=None):
        self.name, self.version, self.tables, self.file = name, version, tables, file
        self.read_all, self.replace_all = read_all, replace_all
        self.rows_of, self.write_rows = rows_of, write_rows
        self.parse_file, self.export_data = parse_file, export_data
        self.steps = dict(steps or {})


def _on_connect(dbapi, _record):
    # pysqlite's own transaction handling off: SQLAlchemy's begin() below sends BEGIN, so that
    # savepoints work and a PRAGMA is never inside a transaction (SQLAlchemy's pysqlite recipe).
    dbapi.isolation_level = None
    cursor = dbapi.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=%d" % BUSY_WAIT_MS)
        if not _read_only(dbapi):
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _read_only(dbapi):
    try:
        return bool(dbapi.execute("PRAGMA query_only").fetchone()[0])
    except sqlite3.Error:
        return False


def _on_begin(conn):
    conn.exec_driver_sql("BEGIN")


def engine_for(path, read_only=False, fallback=True, immutable=False):
    """An Engine on the database file: WAL, synchronous=FULL, foreign keys; read_only opens the
    file with mode=ro (no write is possible, and none is made to open it), and when that cannot
    be done (a read-only folder, and no server has the file open) reads it as it is (immutable),
    unless not `fallback`; `immutable` reads it so at once."""
    if read_only:
        uri = "file:%s?mode=ro%s" % (urlpath(path), "&immutable=1" if immutable else "")
        engine = create_engine("sqlite+pysqlite://", poolclass=NullPool,
                               creator=lambda: _connect_ro(uri, fallback and not immutable))
    else:
        engine = create_engine(URL.create("sqlite+pysqlite", database=path), poolclass=NullPool,
                               connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "begin", _on_begin)
    return engine


def urlpath(path):
    path = os.path.abspath(path).replace("\\", "/").replace("%", "%25").replace("?", "%3f")
    path = path.replace("#", "%23")
    return path if path.startswith("/") else "/" + path  # C:/... is file:/C:/...


def _connect_ro(uri, fallback):
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        conn.close()
        if not fallback:
            raise
    # A read-only folder, and no server has the database open (it removed its -wal and -shm
    # files as it stopped, and they cannot be made here): nothing is writing it, so it is read
    # as it is (immutable). A server that starts meanwhile is seen at the next connection.
    conn = sqlite3.connect(uri + "&immutable=1", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only=1")
    return conn


def _answer(work, error):
    """Every future of `work` not answered yet answered with `error` (one cancelled before it
    ran stays cancelled): nothing taken from the queue is left waiting."""
    for _, _, future in work:
        if future.done():
            continue
        if future.running() or future.set_running_or_notify_cancel():
            future.set_exception(error)


class Database:
    """The database file at `path` and its writer thread (the module's docstring)."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.engine = engine_for(path)
        with self.engine.begin() as conn:  # the stores' table; WAL is set on every connection
            META.create_all(conn, checkfirst=True)
        self.queue = queue.SimpleQueue()
        self.closed = False
        self.died = None  # what stopped the writer thread, when something did (not close())
        self._lock = threading.Lock()  # submit() against close() and the writer's end
        self._failing = 0  # transactions that failed in a row (the log says the first)
        self.thread = threading.Thread(target=self._run, name="storage writer", daemon=True)
        self.thread.start()

    # --- the writer ---

    def submit(self, fn, *args):
        """fn(connection, *args) run by the writer thread inside its transaction; the future has
        what it returned once that transaction is committed (or what it, or the commit, raised:
        then nothing of it is in the database). Closed at once after close(), and after the
        writer thread stopped."""
        future = concurrent.futures.Future()
        with self._lock:
            if not self.closed and not self.thread.is_alive():  # it ended without a word
                self._stopped(RuntimeError("the writer thread is not running"))
            if self.closed:
                future.set_exception(Closed(
                    "%s is closed" % self.name if self.died is None else
                    "%s: its writer stopped (%r)" % (self.name, self.died)))
            else:
                self.queue.put((fn, args, future))
        return future

    def call(self, fn, *args, timeout=None):
        """submit() and wait: what fn returned, once committed. Not on the event loop while the
        server serves (the start's loads, tools and tests)."""
        return self.submit(fn, *args).result(timeout)

    def _run(self):
        conn, work = None, []
        try:
            while True:
                work = [self.queue.get()]
                while len(work) < BATCH:
                    try:
                        work.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                stop = work[-1] is None  # close() puts None last: nothing comes after it
                work = [w for w in work if w is not None]
                if work:
                    conn = self._commit(conn, work)
                work = []
                if stop:
                    break
        except BaseException as e:  # noqa: BLE001 - a bug here must not leave submits waiting
            try:
                log.exception("storage: the writer of %s stopped", self.name)
            finally:
                with self._lock:
                    self._stopped(e, work)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - the stop goes on
                    pass

    def _stopped(self, error, work=()):
        """The writer is gone (self._lock held): no more submits, and every one it had or that
        is queued answered with Closed."""
        self.closed = True
        self.died = error
        closed = Closed("%s: its writer stopped (%r)" % (self.name, error))
        _answer(work, closed)
        while True:
            try:
                w = self.queue.get_nowait()
            except queue.Empty:
                return
            if w is not None:
                _answer([w], closed)

    def _commit(self, conn, work):
        """One transaction for `work`, a savepoint for each; the futures answered once it is
        committed. The connection kept, or None after a failed transaction (the next one is
        new). Every future of `work` is answered here, whatever fails (_answer)."""
        done = []
        try:
            if conn is None:
                conn = self.engine.connect()
            with conn.begin():
                for fn, args, future in work:
                    if not future.set_running_or_notify_cancel():
                        continue
                    # Its savepoint's own statements (SAVEPOINT, ROLLBACK TO, RELEASE) failing
                    # fail the whole transaction: then nothing says what of it is kept.
                    savepoint = conn.begin_nested()
                    try:
                        value = fn(conn, *args)
                    except Exception as e:  # noqa: BLE001 - its own savepoint is rolled back
                        savepoint.rollback()
                        done.append((future, False, e))
                    else:
                        savepoint.commit()
                        done.append((future, True, value))
        except Exception as e:  # noqa: BLE001 - the connection, BEGIN, a savepoint or COMMIT
            if not self._failing:
                log.error("storage: %d change(s) could not be committed to %s: %s", len(work),
                          self.name, e)
            self._failing += 1
            _answer(work, e)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            return None
        if self._failing:
            log.info("storage: %s takes changes again (after %d failed transaction(s))",
                     self.name, self._failing)
            self._failing = 0
        for future, ok, value in done:
            if ok:
                future.set_result(value)
            else:
                future.set_exception(value)
        return conn

    def close(self, timeout=CLOSE_WAIT):
        """What is queued committed, the thread ended, the files closed (the WAL folded into the
        database by SQLite as the last connection closes). False, logged, when the thread did
        not finish in `timeout` seconds (a disk that does not answer)."""
        with self._lock:
            if self.closed and not self.thread.is_alive():
                self.engine.dispose()
                return True
            if not self.closed:
                self.closed = True
                self.queue.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            log.error("storage: %s: the last changes were not committed in %g s", self.name,
                      timeout)
            return False
        self.engine.dispose()
        return True

    # --- reads ---

    def read(self, fn, *args):
        """fn(connection, *args) in a transaction of its own, on the calling thread (the start's
        loads, tools): it sees every commit made before it began."""
        with self.engine.connect() as conn, conn.begin():
            return fn(conn, *args)


# --- one Database per file in this process ---

_OPEN = {}
_OPEN_LOCK = threading.Lock()


def database(folder):
    """The data folder's Database, opened at the first call (and again after close_all, or
    after its writer stopped)."""
    path = os.path.abspath(os.path.join(folder, FILE))
    with _OPEN_LOCK:
        db = _OPEN.get(path)
        if db is None or db.closed:
            db = _OPEN[path] = Database(path)
        return db


def close_all(timeout=CLOSE_WAIT):
    """Every open Database closed (the server's stop; tests). False when one could not finish."""
    with _OPEN_LOCK:
        open_now = list(_OPEN.values())
        _OPEN.clear()
    ok = True
    for db in open_now:
        ok = db.close(timeout) and ok
    return ok


atexit.register(close_all)  # a process that ends without the stop still commits its queue


# --- a store's row and its tables' version ---

def ensure(db, kind):
    """The store's tables made when missing, and its row. Tables of an older version are made
    into this one's by kind.steps, each version's step in turn, in one transaction, after a copy
    of the database (<file>.before-<name>-v<version>, logged). Refused, logged, for tables of a
    newer version, or of an older one with a step missing."""
    found = db.read(_version, kind.name)
    if found is not None and found > kind.version:
        log.error("%s: %s has version %d of its tables, written by a newer server (this one "
                  "knows %d): not started", kind.name, db.name, found, kind.version)
        raise Refused("%s: tables of version %d in %s" % (kind.name, found, db.name))
    if found is not None and found < kind.version:
        missing = [v for v in range(found, kind.version) if v not in kind.steps]
        if missing:
            log.error("%s: %s has version %d of its tables and this server has no upgrade from "
                      "version %d: not started", kind.name, db.name, found, missing[0])
            raise Refused("%s: no upgrade from version %d" % (kind.name, missing[0]))
        copy = next_name(db.path, ".before-%s-v%d" % (kind.name, kind.version))
        backup(db.path, copy)
        log.info("%s: %s copied to %s before its tables go from version %d to %d", kind.name,
                 db.name, os.path.basename(copy), found, kind.version)

    def make(conn):
        now = _version(conn, kind.name)
        if now is None:
            kind.tables.create_all(conn, checkfirst=True)
            conn.execute(STORES.insert().values(name=kind.name, version=kind.version,
                                                revision=0))
            return
        for v in range(now, kind.version):
            kind.steps[v](conn)
        kind.tables.create_all(conn, checkfirst=True)  # what is new in this version
        if now != kind.version:
            conn.execute(STORES.update().where(STORES.c.name == kind.name)
                         .values(version=kind.version))

    try:
        db.call(make)
    except Exception as e:  # noqa: BLE001 - an upgrade that fails leaves the tables as they were
        log.error("%s: the tables in %s could not be made or upgraded (%s): not started",
                  kind.name, db.name, e)
        raise Refused("%s: %s" % (db.name, e)) from e
    if found is not None and found < kind.version:
        log.info("%s: its tables in %s went from version %d to %d", kind.name, db.name, found,
                 kind.version)


def _version(conn, name):
    row = conn.execute(select(STORES.c.version).where(STORES.c.name == name)).first()
    return None if row is None else row.version


def bump(conn, name):
    """The store's revision counted up: in each of its write functions (the writer's)."""
    conn.execute(STORES.update().where(STORES.c.name == name)
                 .values(revision=STORES.c.revision + 1))


def state(conn, name):
    """(revision, what it knows of its JSON file: a dict, {} for nothing) of the store."""
    row = conn.execute(select(STORES.c.revision, STORES.c.file)
                       .where(STORES.c.name == name)).first()
    if row is None:
        return 0, {}
    return row.revision, json.loads(row.file) if row.file else {}


def holds(revision, note):
    """Whether the database is the store, by its note (state()): it imported its file, or
    started without one, and no rollback is waiting for the previous version's file. The
    website accounts service reads the file instead when it is not (social/friends.py, Reader)."""
    return bool(note) and not rolled_back(revision, note)


def rolled_back(revision, note):
    """The rollback command wrote the file and nothing changed the database since."""
    return note.get("how") == "rollback" and note.get("revision") == revision


def started(db, name):
    """The store starts with no JSON file to import (a new server, a test): noted, so that a file
    of its name that turns up later is not taken over the database (import_once)."""
    def note(conn):
        if not state(conn, name)[1]:
            _note_file(conn, name, "none", None)
    db.call(note)


def _note_file(conn, name, how, sha256, records=None):
    revision, _ = state(conn, name)
    note = {"how": how, "sha256": sha256, "revision": revision, "at": round(time.time(), 3)}
    if records is not None:
        note["records"] = records
    conn.execute(STORES.update().where(STORES.c.name == name).values(file=json.dumps(note)))


def next_name(path, suffix):
    """path + suffix, or path + suffix + .<n> with n one more than the last there is: an earlier
    one is never written over."""
    first = path + suffix
    if not os.path.lexists(first):
        return first
    folder, start = os.path.split(first + ".")
    taken = [0]
    for name in os.listdir(folder or "."):
        tail = name[len(start):]
        if name.startswith(start) and tail.isdecimal():
            try:
                taken.append(int(tail))
            except ValueError:
                pass
    return "%s.%d" % (first, max(taken) + 1)


def _put_aside(path, suffix):
    target = next_name(path, suffix)
    os.replace(path, target)
    durable.sync_folder(path)
    return target


def import_once(db, kind, path):
    """The store's JSON file at `path` taken into its tables at the first start, or again after a
    rollback (the module's docstring). Returns the number of records imported, or None when
    the database is the store (and its tables were not changed). Refused, logged, when a file it
    has to take cannot be read."""
    name = kind.name
    revision, note = db.read(state, name)
    if not os.path.lexists(path):
        if not note:  # a new store: nothing to import from, and none to come
            started(db, name)
        return None
    back = rolled_back(revision, note)
    if note and note.get("sha256"):
        try:
            sha = durable.digest(path)
        except OSError:
            sha = None
        if sha == note["sha256"]:
            imported = note.get("how") == "imported"
            kept = _put_aside(path, ".imported" if imported else ".rollback")
            if back:  # the previous version did not run on it: the rollback is over
                db.call(lambda conn: _note_file(conn, name, "rollback over", sha))
            log.info("%s: %s is the file %s %s: the database is the store, the file is kept as "
                     "%s", name, os.path.basename(path), db.name,
                     "imported" if imported else "wrote for a rollback", os.path.basename(kept))
            return None
    if note and not back:
        kept = _put_aside(path, ".unused")
        log.warning("%s: %s is not the file %s imported and does not continue it (a previous "
                    "version started without the rollback command, a file put back by hand, an "
                    "export): not read. %s is the store; the file is kept as %s "
                    "(docs/operating.md, \"The database\", says how to take it instead)", name,
                    os.path.basename(path), db.name, db.name, os.path.basename(kept))
        return None
    began = time.perf_counter()
    count = take(db, kind, path)
    kept = _put_aside(path, ".imported")
    log.info("%s: %d record(s) %s from %s into %s in %.2f s; the file is kept as %s", name,
             count, "imported again after the rollback" if back else "imported",
             os.path.basename(path), db.name, time.perf_counter() - began, os.path.basename(kept))
    return count


def take(db, kind, path):
    """The JSON file at `path` (or its .bak, when it is damaged) put in place of the store's
    tables, in one transaction that also notes its SHA-256: the number of records. Refused,
    logged, when it cannot be read or written."""
    name = kind.name
    try:
        data, sha = durable.load_digest(path)
        records = kind.parse_file(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.error("%s: %s is there and cannot be read (%s): not started, so that nothing is "
                  "lost (docs/operating.md, \"The database\")", name, os.path.basename(path), e)
        raise Refused("%s cannot be read: %s" % (path, e)) from e

    def write(conn):
        kind.replace_all(conn, records)
        bump(conn, name)
        _note_file(conn, name, "imported", sha, len(records))

    try:
        db.call(write)
    except Exception as e:  # noqa: BLE001 - a full disk, a database that cannot be written
        log.error("%s: %s could not be written into %s (%s): not started", name,
                  os.path.basename(path), db.name, e)
        raise Refused("%s: %s" % (db.name, e)) from e
    return len(records)


def export(path, kind):
    """The bytes of the store's JSON file as a previous version wrote it, read from the database
    file at `path` read-only: nothing is written, and what a start does is the same after it.
    Refused, logged, when the database has no tables of the store, or a version of them this
    server does not know."""
    engine = engine_for(path, read_only=True)
    try:
        with engine.connect() as conn, conn.begin():
            version = _version(conn, kind.name)
            if version != kind.version:
                log.error("%s: %s has %s: no export", kind.name, os.path.basename(path),
                          "no tables of it" if version is None else
                          "version %d of its tables (this server knows %d)"
                          % (version, kind.version))
                raise Refused("%s: version %s" % (kind.name, version))
            return json.dumps(kind.export_data(conn)).encode()
    finally:
        engine.dispose()


def rollback(db, kind, folder):
    """The store's JSON file written in the data folder as a previous version reads it, for a
    rollback to it (the server stopped), and noted: the next start of this version takes the
    file that version leaves there (import_once), until the database's next commit. Refused,
    logged, when the file is already there."""
    path = os.path.join(folder, kind.file)
    if os.path.lexists(path):
        log.error("%s: %s is already in %s: no rollback (move it away first)", kind.name,
                  kind.file, folder)
        raise Refused("%s is there" % path)

    def write(conn):
        text = json.dumps(kind.export_data(conn)).encode()
        _note_file(conn, kind.name, "rollback", hashlib.sha256(text).hexdigest())
        return text

    text = db.call(write)  # noted first: a file that is there is always one the note knows
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(text)
        durable.replace(tmp, path, backup=False)
    except OSError as e:
        db.call(lambda conn: _note_file(conn, kind.name, "rollback over", None))
        log.error("%s: %s could not be written (%s): no rollback", kind.name, path, e)
        raise Refused("%s: %s" % (path, e)) from e
    return len(text)


# --- a store's records in memory ---

def write_whole(conn, kind, pickled):
    """A whole write (Records.dirty = True, records replaced): the records as they were
    pickled."""
    kind.replace_all(conn, pickle.loads(pickled))
    bump(conn, kind.name)


def write_changes(conn, kind, made, drop):
    """The changed records' rows (as Records.flush made them, in the order they changed)."""
    kind.write_rows(conn, made, drop)
    bump(conn, kind.name)


class Records:
    """A store's records in memory ({key: record}), its tables in the database written by the
    writer thread. A subclass sets KIND (a Kind). path: the store's JSON file in the data folder
    (the database is beside it); None: in memory only.

    A change is mark(key) (fresh: the record was made anew); right after the work that made it
    (loop.call_soon, flush) the marked records' rows are made, on the loop, and handed to the
    writer as one write; the loop never waits for the disk. A write that fails is made again
    with the records as they are then, at the next flush (tick(), every second). save() waits
    for every change so far to be committed: the stop, and a tool; written() is the same for a
    coroutine, off the loop. A writer that stopped is replaced at the next flush (REOPEN_WAIT
    between two)."""

    KIND = None

    def __init__(self, path=None):
        self.path = path
        self._records = {}
        self.db = None  # the Database, once loaded (or first written)
        # What changed since the last flush: the keys of the changed records, in the order they
        # changed, those of them made anew, or everything (_whole).
        self._changed = {}
        self._fresh = set()
        self._whole = False
        self._soon = False  # a flush is due on the loop (call_soon)
        self._writes = []  # (what it held: (changed, fresh), None for whole; its future)
        self._failed = 0  # writes that failed in a row (the log says the first)
        self._reopen_at = 0.0  # monotonic time before which no new writer is opened

    @property
    def records(self):
        return self._records

    @records.setter
    def records(self, records):
        """Replaced as a whole (a test, a tool): written as a whole."""
        self._records = records
        self._whole = True
        self._schedule()

    @property
    def dirty(self):
        """A change not handed to the writer yet."""
        return bool(self._changed) or self._whole

    @dirty.setter
    def dirty(self, value):
        """True: everything is written again (a caller that changed records without mark());
        False: what changed is forgotten."""
        if value:
            self._whole = True
            self._schedule()
        else:
            self._changed, self._fresh, self._whole = {}, set(), False

    @property
    def saving(self):
        """A write handed to the writer is not committed yet."""
        return any(not f.done() for _, f in self._writes)

    def pending(self):
        """Something changed that is not committed yet (or whose write failed)."""
        self._reap()
        return self.dirty or bool(self._writes)

    def mark(self, key, fresh=False):
        """The record of `key` changed (or went: it is not in records any more)."""
        self._changed[key] = None
        if fresh:
            self._fresh.add(key)
        self._schedule()

    def _schedule(self):
        if self._soon or self.db is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # a tool, a test: save() writes it
        self._soon = True
        loop.call_soon(self.flush)

    def _folder(self):
        return os.path.dirname(os.path.abspath(self.path))

    def open(self):
        """The store's records as its tables have them ({key: record}), its JSON file imported
        at the first start (import_once). Refused, logged, when they cannot be read: the server
        does not start on an empty store, whose first change would put it over what it could not
        read. Then loaded() with what the store keeps of them."""
        name = self.KIND.name
        try:
            db = database(self._folder())
            ensure(db, self.KIND)
            if self.KIND.file:
                import_once(db, self.KIND, self.path)
            else:
                started(db, name)
            records = db.read(self.KIND.read_all)
        except Refused:
            raise
        except (SQLAlchemyError, OSError) as e:
            log.error("%s: %s cannot be read (%s): not started", name, FILE, e)
            raise Refused("%s: %s" % (FILE, e)) from e
        self.db = db
        return records

    def loaded(self, records, gone=()):
        """The store's records after open(): `gone`, the keys of those it did not keep, are
        deleted from its tables too."""
        self._records = records
        self._changed = dict.fromkeys(gone)
        self._fresh, self._whole = set(), False
        self._schedule()

    def _attach(self):
        """A store with a path that was never loaded (a test, a tool) gets its database at its first
        write, which is whole: what is in memory is the store, as when it wrote the whole file."""
        self.db = database(self._folder())
        ensure(self.db, self.KIND)
        started(self.db, self.KIND.name)
        self._whole = True

    def _writer(self):
        """The database to write to: a new writer when this one's stopped (died), at most one
        every REOPEN_WAIT seconds; until then the write fails and is made again."""
        if self.db is None:
            self._attach()
        elif self.db.died is not None and time.monotonic() >= self._reopen_at:
            self._reopen_at = time.monotonic() + REOPEN_WAIT
            died, self.db = self.db.died, database(self._folder())
            log.warning("%s: the writer of %s had stopped (%r): a new one takes the changes",
                        self.KIND.name, FILE, died)
        return self.db

    def flush(self):
        """What changed since the last flush handed to the writer, as rows made here from the
        records as they are (a few records: microseconds; a whole write pickles them, a few
        milliseconds for the public server's 1.8 MB of friends). The future of that write, or
        None when nothing changed."""
        self._soon = False
        self._reap()
        if not self.path or not self.dirty:
            return None
        db, kind = self._writer(), self.KIND
        if self._whole:
            held = None
            future = db.submit(write_whole, kind,
                               pickle.dumps(self._records, pickle.HIGHEST_PROTOCOL))
        else:
            made, drop = [], []
            for k in self._changed:
                r = self._records.get(k)
                if r is None or k in self._fresh:
                    drop.append(k)
                if r is not None:  # its rows are made now: what changes after is the next write's
                    made.append(kind.rows_of(k, r))
            held = (self._changed, self._fresh)
            future = db.submit(write_changes, kind, made, drop)
        self._changed, self._fresh, self._whole = {}, set(), False
        self._writes.append((held, future))
        return future

    def _reap(self):
        """The writes that are over: one that failed has its records written again (as they are
        by then) at the next flush."""
        if not self._writes:
            return
        left = []
        for held, future in self._writes:
            if not future.done():
                left.append((held, future))
                continue
            error = None if future.cancelled() else future.exception()
            if error is None and not future.cancelled():
                if self._failed:
                    log.info("%s: written to %s again (after %d failed write(s))",
                             self.KIND.name, FILE, self._failed)
                    self._failed = 0
                continue
            if not self._failed:
                log.warning("%s: changes could not be written to %s (%s): they are written "
                            "again with the next ones", self.KIND.name, FILE, error)
            self._failed += 1
            if held is None:
                self._whole = True
            else:
                for k in held[0]:
                    self._changed.setdefault(k, None)
                self._fresh |= held[1]
        self._writes = left

    def save(self, timeout=CLOSE_WAIT):
        """Every change so far committed, waiting here: True when it is. On the event loop only
        at the stop (and in a tool); a coroutine awaits written() instead."""
        if not self.path:
            return False
        self.flush()
        futures = [f for _, f in self._writes]
        concurrent.futures.wait(futures, timeout)
        ok = all(f.done() and not f.cancelled() and f.exception() is None for f in futures)
        self._reap()
        return ok and not self.dirty

    async def written(self):
        """save() for a coroutine: True once every change made before the call is committed,
        awaited off the loop."""
        if not self.path:
            return False
        self.flush()
        futures = [f for _, f in self._writes]
        if futures:
            await asyncio.gather(*map(asyncio.wrap_future, futures), return_exceptions=True)
        ok = all(not f.cancelled() and f.exception() is None for f in futures)
        self._reap()
        return ok

    def tick(self):
        """Every second from the main loop: what is left (a write that failed) handed to the
        writer; without an event loop (a tool, a test), save() here. Whether anything changed."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            changed = self.dirty or bool(self._writes)
            self.save()
            return changed
        changed = self.dirty or bool(self._writes)
        self.flush()
        return changed


# --- the operator's commands ---

# A store's module has STORAGE (a Kind with a file, parse_file and export_data).
MODULES = {"friends": "forza_live.social.friends"}


def backup(db_path, target):
    """A copy of the database at `target`, with SQLite's online backup: consistent, while the
    server runs."""
    source = sqlite3.connect("file:%s?mode=ro" % urlpath(db_path), uri=True)
    try:
        tmp = target + ".tmp"
        copy = sqlite3.connect(tmp)
        try:
            source.backup(copy)
        finally:
            copy.close()
        durable.replace(tmp, target, backup=False)
    finally:
        source.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m forza_live.core.storage",
                                     description="The server's database (%s): a backup, an "
                                                 "export of a store as JSON, a rollback, an "
                                                 "import" % FILE)
    parser.add_argument("--data-dir", default=os.environ.get("FORZA_LIVE_DATA", "forza_live_data"),
                        help="the server's data folder (default: as the server's, forza_live_data)")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="a consistent copy of the database, while the server runs")
    b.add_argument("file")
    e = sub.add_parser("export", help="a store's records as the JSON file of the versions before "
                                      "it moved (stdout, or FILE), to read or keep: read-only, "
                                      "it changes nothing")
    e.add_argument("store", choices=sorted(MODULES))
    e.add_argument("file", nargs="?")
    r = sub.add_parser("rollback", help="a store's JSON file written in the data folder for a "
                                        "previous version, with the server stopped; this "
                                        "version's next start takes back the file that version "
                                        "leaves")
    r.add_argument("store", choices=sorted(MODULES))
    i = sub.add_parser("import", help="a JSON file of a store put in place of its records, with "
                                      "the server stopped (the file is not changed)")
    i.add_argument("store", choices=sorted(MODULES))
    i.add_argument("file")
    args = parser.parse_args(argv)
    path = os.path.join(args.data_dir, FILE)
    if args.command != "import" and not os.path.exists(path):
        parser.error("no %s in %s" % (FILE, args.data_dir))
    if args.command == "backup":
        backup(path, args.file)
        print("%s copied to %s" % (path, args.file), file=sys.stderr)
        return 0
    import importlib
    kind = importlib.import_module(MODULES[args.store]).STORAGE
    if args.command == "export":
        if args.file and os.path.abspath(args.file) == os.path.abspath(
                os.path.join(args.data_dir, kind.file)):
            parser.error("%s is the file a start looks at: for a rollback, use the rollback "
                         "command; to read or keep the records, export elsewhere" % kind.file)
        try:
            text = export(path, kind)
        except (Refused, SQLAlchemyError) as err:
            print("%s: no export (%s)" % (args.store, err), file=sys.stderr)
            return 1
        if args.file:
            tmp = args.file + ".tmp"
            with open(tmp, "wb") as f:
                f.write(text)
            durable.replace(tmp, args.file, backup=False)
            print("%s: %d bytes to %s" % (args.store, len(text), args.file), file=sys.stderr)
        else:
            sys.stdout.buffer.write(text + b"\n")
        return 0
    db = database(args.data_dir)
    try:
        ensure(db, kind)
        if args.command == "import":
            count = take(db, kind, args.file)
            print("%s: %d record(s) from %s" % (args.store, count, args.file), file=sys.stderr)
            return 0
        size = rollback(db, kind, args.data_dir)
        print("%s: %d bytes to %s; start the previous version now. This version's next start "
              "takes back the %s that version leaves" % (args.store, size, os.path.join(
                  args.data_dir, kind.file), kind.file), file=sys.stderr)
        return 0
    except Refused:
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
