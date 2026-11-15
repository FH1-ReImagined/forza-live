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
- WAL. Readers (the export below, an operator's sqlite3, the website accounts service's
  read-only mount) never wait for the writer, nor it for them. A kill or a power cut leaves the
  database as of its last commit: a change that was not committed is lost, and nothing else;
  SQLite never leaves half a transaction.
- The stop. close() lets the thread commit what is still queued, then ends it
  (server.save_at_stop, among the stores that have no journal, before the boards).
- Each store has a row in `stores`: its tables' version, a revision that each of its commits
  counts (bump()), and what it knows of the JSON file it came from (import_once()).

The import from a JSON file, once (import_once): at the first start with the database, the
store's file is read (durable.load: its .bak when the file is damaged) and written into its
tables in one transaction, which also notes the file's SHA-256; once that is committed the file
is renamed <file>.imported (a number after it when that name is taken), never deleted. A file
that is there but cannot be read, nor its .bak, refuses the start (Refused), as the boards'
and the notifications' do. At a later start, a file of that name next to the database is:
- the one the database imported or exported (the same SHA-256): nothing new in it. The
  database is the store, and the file is renamed <file>.imported.<n> (imported) or
  <file>.exported.<n> (an export nobody started from);
- written after an export (export()) by a previous version of the server, while the database
  has not changed since that export: the rollback the operator's guide describes. The file has
  everything the database has and what the previous version changed: it is imported again,
  in place of the store's tables, and renamed like the first one;
- anything else (a previous version started without the export, a file put back by hand): the
  database is the store, because the file does not continue it; the file is renamed
  <file>.unused.<n> and the log says so, and how to take it instead (docs/operating.md).

For the operator (docs/operating.md, "The database"):
  python3 -m forza_live.core.storage --data-dir DIR backup FILE   a copy, while the server runs
  python3 -m forza_live.core.storage --data-dir DIR export friends [FILE]   friends.json as the
      previous versions wrote it (stdout without FILE)
  python3 -m forza_live.core.storage --data-dir DIR import friends FILE   that file in place of
      the store's records, with the server stopped
"""

import argparse
import atexit
import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import sqlite3
import sys
import threading
import time

from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, event, select
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from forza_live.core import durable
from forza_live.core import journal

log = logging.getLogger("forza-live")

FILE = "forza_live.sqlite3"
BATCH = 256  # submits at most in one transaction
CLOSE_WAIT = 15.0  # s the stop waits for the queue (compose gives the stop 20 s)
BUSY_WAIT_MS = 10000  # another process's transaction (a backup, an export) is waited for this long

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
    nor by its .bak, or the database was written by a newer version of a store's tables. Logged
    where it is raised; server.run() ends the process (as for the boards' journal.Unreadable)."""


class Closed(RuntimeError):
    """A submit after close(): nothing written."""


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
        self._lock = threading.Lock()  # submit() against close()
        self._failing = 0  # transactions that failed in a row (the log says the first)
        self.thread = threading.Thread(target=self._run, name="storage writer", daemon=True)
        self.thread.start()

    # --- the writer ---

    def submit(self, fn, *args):
        """fn(connection, *args) run by the writer thread inside its transaction; the future has
        what it returned once that transaction is committed (or what it, or the commit, raised:
        then nothing of it is in the database)."""
        future = concurrent.futures.Future()
        with self._lock:
            if self.closed:
                future.set_exception(Closed("%s is closed" % self.name))
            else:
                self.queue.put((fn, args, future))
        return future

    def call(self, fn, *args, timeout=None):
        """submit() and wait: what fn returned, once committed. Not on the event loop while the
        server serves (the start's loads, tools and tests)."""
        return self.submit(fn, *args).result(timeout)

    def _run(self):
        conn = None
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
                if stop:
                    break
        except BaseException as e:  # noqa: BLE001 - a bug here must not leave submits waiting
            log.exception("storage: the writer of %s stopped", self.name)
            with self._lock:
                self.closed = True
            self._fail_waiting(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - the stop goes on
                    pass

    def _fail_waiting(self, error):
        while True:
            try:
                w = self.queue.get_nowait()
            except queue.Empty:
                return
            if w is not None and w[2].set_running_or_notify_cancel():
                w[2].set_exception(Closed("%s: its writer stopped (%r)" % (self.name, error)))

    def _commit(self, conn, work):
        """One transaction for `work`, a savepoint for each; the futures answered once it is
        committed. The connection kept, or None after a failed commit (the next one is new)."""
        done = []
        try:
            if conn is None:
                conn = self.engine.connect()
            with conn.begin():
                for fn, args, future in work:
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        with conn.begin_nested():
                            done.append((future, True, fn(conn, *args)))
                    except Exception as e:  # noqa: BLE001 - its own savepoint is rolled back
                        done.append((future, False, e))
        except Exception as e:  # noqa: BLE001 - the commit itself: none of the work is kept
            if not self._failing:
                log.error("storage: %d change(s) could not be committed to %s: %s", len(work),
                          self.name, e)
            self._failing += 1
            for _, _, future in work:
                if future.running():
                    future.set_exception(e)
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
    """The data folder's Database, opened at the first call (and again after close_all)."""
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


# --- a store's row ---

def ensure(db, name, version, metadata):
    """The store's tables made when missing, and its row; Refused (logged) when the database
    has a newer version of them than this server knows."""
    def make(conn):
        metadata.create_all(conn, checkfirst=True)
        row = conn.execute(select(STORES.c.version).where(STORES.c.name == name)).first()
        if row is None:
            conn.execute(STORES.insert().values(name=name, version=version, revision=0))
            return version
        return row.version

    found = db.call(make)
    if found > version:
        log.error("%s: %s has version %d of its tables, written by a newer server (this one "
                  "knows %d): not started", name, db.name, found, version)
        raise Refused("%s: tables of version %d in %s" % (name, found, db.name))


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


def started(db, name):
    """The store starts with no JSON file to import (a new server, a test): noted, so that a file
    of its name that turns up later is not taken over the database (import_once)."""
    def note(conn):
        if not state(conn, name)[1]:
            _note_file(conn, name, "none", None)
    db.call(note)


def _note_file(conn, name, how, sha256, players=None):
    revision, _ = state(conn, name)
    note = {"how": how, "sha256": sha256, "revision": revision, "at": round(time.time(), 3)}
    if players is not None:
        note["records"] = players
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


def import_once(db, name, path, parse, replace):
    """The store's JSON file at `path` taken into its tables at the first start, or again after a
    rollback (the module's docstring). parse(data): the records the file holds (ValueError,
    KeyError, TypeError for a file of another shape); replace(conn, records): the store's tables
    made to hold just those (the writer's). Returns the number of records imported, or None when
    the database is the store (and its tables were not changed). Refused, logged, when a file it
    has to take cannot be read."""
    revision, note = db.read(state, name)
    if not os.path.lexists(path):
        if not note:  # a new store: nothing to import from, and none to come
            started(db, name)
        return None
    if note and note.get("sha256"):
        try:
            sha = durable.digest(path)
        except OSError:
            sha = None
        if sha == note["sha256"]:
            kept = _put_aside(path, ".imported" if note.get("how") == "imported" else ".exported")
            log.info("%s: %s is the file %s %s: the database is the store, the file is kept as "
                     "%s", name, os.path.basename(path), db.name,
                     "imported" if note.get("how") == "imported" else "exported",
                     os.path.basename(kept))
            return None
    after_export = note.get("how") == "exported" and note.get("revision") == revision
    if note and not after_export:
        kept = _put_aside(path, ".unused")
        log.warning("%s: %s is newer than %s's import of it and does not continue it (a previous "
                    "version started without the export, or a file put back by hand): not read. "
                    "%s is the store; the file is kept as %s (docs/operating.md, \"The "
                    "database\", says how to take it instead)", name, os.path.basename(path),
                    db.name, db.name, os.path.basename(kept))
        return None
    began = time.perf_counter()
    count = take(db, name, path, parse, replace)
    kept = _put_aside(path, ".imported")
    log.info("%s: %d record(s) %s from %s into %s in %.2f s; the file is kept as %s", name,
             count, "imported again" if after_export else "imported", os.path.basename(path),
             db.name, time.perf_counter() - began, os.path.basename(kept))
    return count


def take(db, name, path, parse, replace):
    """The JSON file at `path` (or its .bak, when it is damaged) put in place of the store's
    tables, in one transaction that also notes its SHA-256: the number of records. Refused,
    logged, when it cannot be read or written."""
    try:
        data, sha = durable.load_digest(path)
        records = parse(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.error("%s: %s is there and cannot be read (%s): not started, so that nothing is "
                  "lost (docs/operating.md, \"The database\")", name, os.path.basename(path), e)
        raise Refused("%s cannot be read: %s" % (path, e)) from e

    def write(conn):
        replace(conn, records)
        bump(conn, name)
        _note_file(conn, name, "imported", sha, len(records))

    try:
        db.call(write)
    except Exception as e:  # noqa: BLE001 - a full disk, a database that cannot be written
        log.error("%s: %s could not be written into %s (%s): not started", name,
                  os.path.basename(path), db.name, e)
        raise Refused("%s: %s" % (db.name, e)) from e
    return len(records)


def export(db, name, dump):
    """The bytes of the store's JSON file as a previous version wrote it: dump(conn) gives its
    data, read in a transaction of the writer's; the database notes their SHA-256 and its own
    revision, so that a previous version's file written after it is taken at the next start
    (import_once)."""
    def take(conn):
        text = json.dumps(dump(conn)).encode()
        _note_file(conn, name, "exported", hashlib.sha256(text).hexdigest())
        return text

    return db.call(take)


# --- the operator's commands ---

# A store's module has NAME, VERSION, TABLES (its MetaData), export_data(conn), parse_file(data)
# and replace_all(conn, records).
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
                                                 "export of a store as JSON" % FILE)
    parser.add_argument("--data-dir", default=os.environ.get("FORZA_LIVE_DATA", "forza_live_data"),
                        help="the server's data folder (default: as the server's, forza_live_data)")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="a consistent copy of the database, while the server runs")
    b.add_argument("file")
    e = sub.add_parser("export", help="a store's records as the JSON file of the versions before "
                                      "it moved (stdout, or FILE)")
    e.add_argument("store", choices=sorted(MODULES))
    e.add_argument("file", nargs="?")
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
    module = importlib.import_module(MODULES[args.store])
    db = database(args.data_dir)
    try:
        ensure(db, module.NAME, module.VERSION, module.TABLES)
        if args.command == "import":
            count = take(db, module.NAME, args.file, module.parse_file, module.replace_all)
            print("%s: %d record(s) from %s" % (args.store, count, args.file), file=sys.stderr)
            return 0
        text = export(db, args.store, module.export_data)
    except Refused:
        return 1
    finally:
        db.close()
    if args.file:
        tmp = args.file + ".tmp"
        with open(tmp, "wb") as f:
            f.write(text)
        durable.replace(tmp, args.file, backup=False)
        print("%s: %d bytes to %s" % (args.store, len(text), args.file), file=sys.stderr)
    else:
        sys.stdout.buffer.write(text + b"\n")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
