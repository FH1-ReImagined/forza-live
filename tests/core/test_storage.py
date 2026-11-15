"""The server's database (core/storage.py, #75): one writer thread that keeps the order of the
writes, a savepoint for each and a commit for all, futures answered only once committed, the
stop that commits what is queued, reads that see every commit, WAL, the operator's backup, and
a database of a newer version refused.

Run: python3 -m unittest discover -s tests -t .
"""

import concurrent.futures
import contextlib
import io
import logging
import os
import random
import sqlite3
import tempfile
import threading
import unittest

from sqlalchemy import Column, Integer, MetaData, Table, Text, event, select

from forza_live.core import storage

TABLES = MetaData()
ROWS = Table("test_rows", TABLES, Column("n", Integer, primary_key=True), Column("text", Text))


KIND = storage.Kind("test", 1, TABLES, read_all=None, replace_all=None, rows_of=None,
                    write_rows=None)


def put(conn, n, text="row"):
    conn.execute(ROWS.insert().values(n=n, text=text))
    return n


def plain(path):
    """sqlite3's own connection (an operator's), closed after the with: Windows deletes no folder
    with an open file in it."""
    return contextlib.closing(sqlite3.connect(path))


def rows(conn):
    return [tuple(r) for r in conn.execute(select(ROWS).order_by(ROWS.c.n))]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(storage.close_all)  # before the folder goes: Windows keeps open files
        self.db = storage.database(self.dir)
        storage.ensure(self.db, KIND)

    def held(self):
        """A submit that holds the writer until the returned event is set."""
        go, holding = threading.Event(), threading.Event()

        def hold(conn):
            holding.set()
            go.wait(30)
        self.db.submit(hold)
        self.assertTrue(holding.wait(30))
        return go


class WriterTest(Base):
    def test_the_writes_are_made_in_the_order_they_were_submitted(self):
        go = self.held()
        futures = [self.db.submit(put, n) for n in range(300)]  # more than one transaction's worth
        self.assertFalse(any(f.done() for f in futures))  # nothing answered before its commit
        go.set()
        self.assertEqual([f.result(30) for f in futures], list(range(300)))
        self.assertEqual([r[0] for r in self.db.read(rows)], list(range(300)))

    def test_the_writes_are_the_writer_thread_s(self):
        threads = []
        self.db.call(lambda conn: threads.append(threading.current_thread().name))
        self.assertEqual(threads, ["storage writer"])

    def test_a_write_that_fails_takes_nothing_else_with_it(self):
        go = self.held()
        first = self.db.submit(put, 1)
        twice = self.db.submit(put, 1)  # the same key: refused by its own savepoint
        after = self.db.submit(put, 2)
        go.set()
        self.assertEqual(first.result(30), 1)
        with self.assertRaises(Exception):
            twice.result(30)
        self.assertEqual(after.result(30), 2)
        self.assertEqual(self.db.read(rows), [(1, "row"), (2, "row")])

    def test_a_commit_that_fails_answers_every_write_of_it_and_keeps_none(self):
        go = self.held()
        futures = [self.db.submit(put, n) for n in range(5)]

        def fail_commit(conn):  # an I/O error at the commit, as a full disk gives
            raise sqlite3.OperationalError("disk I/O error")
        event.listen(self.db.engine, "commit", fail_commit)
        with self.assertLogs("forza-live", "ERROR") as logs:
            go.set()
            for f in futures:
                with self.assertRaises(Exception):
                    f.result(30)
        event.remove(self.db.engine, "commit", fail_commit)
        self.assertIn("could not be committed", logs.output[0])
        self.assertEqual(self.db.read(rows), [])
        with self.assertLogs("forza-live", "INFO") as logs:
            self.assertEqual(self.db.call(put, 9, timeout=30), 9)  # and the next one is kept
        self.assertIn("takes changes again", logs.output[0])
        self.assertEqual(self.db.read(rows), [(9, "row")])

    def test_a_read_sees_every_commit_made_before_it(self):
        for n in range(20):
            self.db.call(put, n, timeout=30)
            self.assertEqual(len(self.db.read(rows)), n + 1)

    def test_wal_and_a_full_sync(self):
        with plain(self.db.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.db.read(lambda c: c.exec_driver_sql("PRAGMA synchronous").scalar()),
                         2)  # FULL
        self.assertEqual(self.db.read(lambda c: c.exec_driver_sql("PRAGMA foreign_keys").scalar()), 1)


class Faults:
    """The writer's calls to SQLite broken on purpose: fail(point, n) makes the next n calls at
    that point raise as a full disk or a lost file does. The points: connect (a new
    connection), begin (BEGIN), savepoint (SAVEPOINT), execute (a write's INSERT), rollback_to
    (ROLLBACK TO, after a write that failed), release (RELEASE), commit (COMMIT)."""

    POINTS = ("connect", "begin", "savepoint", "execute", "rollback_to", "release", "commit")
    STATEMENTS = {"BEGIN": "begin", "SAVEPOINT": "savepoint", "INSERT": "execute",
                  "ROLLBACK TO": "rollback_to", "RELEASE": "release"}

    def __init__(self, test, db):
        self.db = db
        self.left = dict.fromkeys(self.POINTS, 0)
        self.hits = dict.fromkeys(self.POINTS, 0)
        self.after_commit = []  # points armed once the transaction under way is committed
        self.lock = threading.Lock()
        dialect = db.engine.dialect
        real_connect, real_commit = dialect.connect, dialect.do_commit

        def connect(*args, **kwargs):
            self.check("connect", "unable to open database file")
            return real_connect(*args, **kwargs)

        def do_commit(dbapi_connection):
            self.check("commit", "disk I/O error")
            real_commit(dbapi_connection)
            with self.lock:
                for point in self.after_commit:
                    self.left[point] += 1
                self.after_commit = []

        def before(conn, cursor, statement, parameters, context, executemany):
            for start, point in self.STATEMENTS.items():
                if statement.lstrip().upper().startswith(start):
                    self.check(point, "database or disk is full")
        dialect.connect, dialect.do_commit = connect, do_commit
        event.listen(db.engine, "before_cursor_execute", before)

        def undo():
            del dialect.connect, dialect.do_commit
            event.remove(db.engine, "before_cursor_execute", before)
        test.addCleanup(undo)

    def fail(self, point, n=1):
        with self.lock:
            self.left[point] += n

    def next_batch(self, *points):
        """These points fail in the next transaction (the one under way commits first)."""
        with self.lock:
            self.after_commit += points

    def clear(self):
        with self.lock:
            self.left = dict.fromkeys(self.POINTS, 0)
            self.after_commit = []

    def check(self, point, message):
        if threading.current_thread() is not self.db.thread:  # the test's own reads go on
            return
        with self.lock:
            if not self.left[point]:
                return
            self.left[point] -= 1
            self.hits[point] += 1
        raise sqlite3.OperationalError(message)


class FaultTest(Base):
    """Every write the writer takes from the queue ends with its result or an exception,
    whatever fails and wherever, alone or one after the other; a result means it is in the
    database, an exception that nothing of it is."""

    def setUp(self):
        super().setUp()
        self.faults = Faults(self, self.db)
        self.futures = {}  # n -> its future
        self.n = 0

    def writes(self, count=1):
        """`count` writes submitted at once (one transaction, as far as the writer takes them so)."""
        made = []
        for _ in range(count):
            self.n += 1
            self.futures[self.n] = self.db.submit(put, self.n)
            made.append(self.futures[self.n])
        return made

    def settle(self):
        """Each future answered within 30 s (none left waiting), and the database holds just the
        writes whose future has a result."""
        concurrent.futures.wait(list(self.futures.values()), 30)
        waiting = [n for n, f in self.futures.items() if not f.done()]
        self.assertEqual(waiting, [], "writes left without an answer")
        kept = {n for n, f in self.futures.items() if f.exception() is None}
        for n in kept:
            self.assertEqual(self.futures[n].result(), n)
        self.assertEqual({r[0] for r in self.db.read(rows)}, kept)
        return kept

    def test_the_reviewed_case_a_failed_commit_then_a_failed_connection(self):
        """A commit fails (the writer lets its connection go), then the next connection fails:
        that batch's writes are answered too, and the next write is kept."""
        self.faults.fail("commit")
        with self.assertLogs("forza-live", "ERROR"):
            first = self.writes()[0]
            with self.assertRaises(Exception):
                first.result(30)
            self.faults.fail("connect")
            second = self.writes()[0]
            with self.assertRaises(Exception):
                second.result(30)  # before the fix: never answered
        third = self.writes()[0]
        self.assertEqual(third.result(30), self.n)
        self.assertEqual(self.settle(), {self.n})

    def test_each_point_breaks_alone(self):
        for point in Faults.POINTS:
            with self.subTest(point=point):
                hits = self.faults.hits[point]
                if point == "connect":  # a connection is made only after a failed one
                    self.faults.next_batch("commit")
                go = self.held()
                batch = self.writes(5)  # one transaction, behind the held one
                self.faults.next_batch(*{"rollback_to": ("execute", "rollback_to"),
                                         "connect": ()}.get(point, (point,)))
                with self.assertLogs("forza-live", "INFO"):  # the error, or nothing at all
                    logging.getLogger("forza-live").info("break %s", point)
                    go.set()
                    concurrent.futures.wait(batch, 30)
                self.assertTrue(all(f.done() for f in batch))
                if point == "connect":  # that batch failed at its commit; then the connection
                    self.faults.fail("connect")
                    batch = self.writes(5)
                    concurrent.futures.wait(batch, 30)
                    self.assertTrue(all(f.done() for f in batch))
                self.assertEqual(self.faults.hits[point], hits + 1, point)
                kept = self.settle()
                if point == "execute":  # its own savepoint: the other four are kept
                    self.assertEqual(sum(1 for f in batch if f.exception() is None), 4)
                else:  # the transaction failed: nothing of it kept
                    self.assertFalse(any(f.exception() is None for f in batch), point)
                self.assertEqual(self.writes()[0].result(30), self.n)  # and the next one is kept
                self.assertEqual(self.settle(), kept | {self.n})
                self.faults.clear()

    def test_the_points_break_one_after_the_other(self):
        """Every pair: a batch broken at one point, the next batch broken at another (a connection
        given up after the first leaves the second to make a new one), then a write that goes
        through."""
        for first in Faults.POINTS:
            for second in Faults.POINTS:
                with self.subTest(first=first, second=second):
                    with self.assertLogs("forza-live", "INFO"):
                        logging.getLogger("forza-live").info("break %s then %s", first, second)
                        for point in (first, second):
                            if point == "rollback_to":
                                self.faults.fail("execute")
                            self.faults.fail(point)
                            batch = self.writes(3)
                            concurrent.futures.wait(batch, 30)
                            self.assertTrue(all(f.done() for f in batch), (first, second))
                    self.faults.clear()  # a point no batch reached (connect, with none failed)
                    self.assertEqual(self.writes()[0].result(30), self.n)
                    self.settle()

    def test_random_breaks_under_a_stream_of_writes(self):
        rnd = random.Random(75)
        with self.assertLogs("forza-live", "INFO"):
            for _ in range(300):
                if rnd.random() < 0.4:
                    self.faults.fail(rnd.choice(Faults.POINTS))
                self.writes(rnd.randrange(1, 6))
                if rnd.random() < 0.7:  # else the next ones join the same transaction
                    concurrent.futures.wait(list(self.futures.values()), 30)
        kept = self.settle()
        self.faults.clear()
        self.assertEqual(self.writes()[0].result(30), self.n)
        self.assertGreater(len(kept), 0)
        self.assertGreater(sum(self.faults.hits.values()), 60)
        self.assertGreater(min(self.faults.hits.values()), 0)  # every point was reached
        with plain(self.db.path) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class Boom(BaseException):
    """What no `except Exception` catches: a bug of the writer itself, as far as it knows."""


class WriterDeathTest(Base):
    def test_a_writer_that_stops_answers_everything_and_takes_nothing_more(self):
        go = self.held()

        def boom(conn):
            raise Boom("a bug")
        before = self.db.submit(put, 1)  # in the same transaction as the bug
        bug = self.db.submit(boom)
        queued = [self.db.submit(put, n) for n in range(2, 600)]  # and behind it
        with self.assertLogs("forza-live", "ERROR") as logs:
            go.set()
            self.db.thread.join(30)
        self.assertIn("the writer of forza_live.sqlite3 stopped", logs.output[0])
        self.assertFalse(self.db.thread.is_alive())
        for f in [before, bug] + queued:
            self.assertTrue(f.done())
            self.assertIsInstance(f.exception(), storage.Closed)
        self.assertTrue(self.db.closed)
        self.assertIsInstance(self.db.died, Boom)
        later = self.db.submit(put, 9999)  # not queued for nobody: refused at once
        self.assertTrue(later.done())
        self.assertIsInstance(later.exception(), storage.Closed)
        self.assertTrue(self.db.queue.empty())
        self.assertEqual(self.db.read(rows), [])  # nothing of that transaction kept
        again = storage.database(self.dir)  # a new writer for the next one who asks
        self.assertIsNot(again, self.db)
        self.assertEqual(again.call(put, 5, timeout=30), 5)

    def test_a_writer_thread_gone_without_a_word_is_noticed(self):
        """However the thread ended (here: it was told to stop behind close()'s back), the next
        submit sees it, and is refused rather than queued for nobody."""
        self.db.queue.put(None)
        self.db.thread.join(30)
        self.assertFalse(self.db.thread.is_alive())
        self.assertFalse(self.db.closed)
        f = self.db.submit(put, 1)
        self.assertIsInstance(f.exception(5), storage.Closed)
        self.assertTrue(self.db.closed)
        self.assertTrue(self.db.queue.empty())


class CloseTest(Base):
    def test_the_stop_commits_what_is_queued(self):
        go = self.held()
        futures = [self.db.submit(put, n) for n in range(1000)]
        threading.Timer(0.2, go.set).start()  # the disk answers while the stop waits
        self.assertTrue(storage.close_all())
        self.assertTrue(all(f.done() and f.exception() is None for f in futures))
        self.assertFalse(self.db.thread.is_alive())
        self.assertFalse(os.path.exists(self.db.path + "-wal"))  # folded into the file
        with plain(self.db.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM test_rows").fetchone()[0], 1000)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_write_after_the_stop_is_refused(self):
        self.db.close()
        with self.assertRaises(storage.Closed):
            self.db.submit(put, 1).result(5)
        again = storage.database(self.dir)  # a new start in this process opens it again
        self.assertIsNot(again, self.db)
        self.assertEqual(again.call(put, 3, timeout=30), 3)

    def test_a_stop_that_cannot_finish_says_so(self):
        go = self.held()
        with self.assertLogs("forza-live", "ERROR") as logs:
            self.assertFalse(self.db.close(timeout=0.1))
        self.assertIn("were not committed", logs.output[0])
        go.set()
        self.assertTrue(self.db.close())


class VersionTest(Base):
    def test_tables_of_a_newer_version_refuse_the_start(self):
        storage.ensure(self.db, KIND)  # again: nothing to do
        self.db.call(lambda conn: conn.execute(storage.STORES.update().values(version=2)))
        with self.assertLogs("forza-live", "ERROR") as logs, self.assertRaises(storage.Refused):
            storage.ensure(self.db, KIND)
        self.assertIn("written by a newer server", logs.output[0])

    def kind_v2(self, steps):
        tables = MetaData()
        Table("test_rows", tables, Column("n", Integer, primary_key=True), Column("text", Text),
              Column("extra", Text))
        Table("test_more", tables, Column("k", Text, primary_key=True))
        return storage.Kind("test", 2, tables, None, None, None, None, steps=steps)

    def test_older_tables_are_upgraded_after_a_copy(self):
        for n in range(3):
            self.db.call(put, n, timeout=30)

        def step(conn):
            conn.exec_driver_sql("ALTER TABLE test_rows ADD COLUMN extra TEXT")
            conn.exec_driver_sql("UPDATE test_rows SET extra = 'from v1'")
        with self.assertLogs("forza-live", "INFO") as logs:
            storage.ensure(self.db, self.kind_v2({1: step}))
        self.assertIn("copied to forza_live.sqlite3.before-test-v2", logs.output[0])
        self.assertIn("went from version 1 to 2", logs.output[-1])
        with plain(self.db.path) as conn:
            self.assertEqual(conn.execute("SELECT version FROM stores WHERE name = 'test'")
                             .fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT n, extra FROM test_rows").fetchall(),
                             [(0, "from v1"), (1, "from v1"), (2, "from v1")])
            conn.execute("SELECT k FROM test_more")  # the new table, made
        with plain(self.db.path + ".before-test-v2") as conn:  # the copy: version 1, as it was
            self.assertEqual(conn.execute("SELECT version FROM stores WHERE name = 'test'")
                             .fetchone()[0], 1)
            self.assertEqual(len(conn.execute("SELECT * FROM test_rows").fetchall()), 3)
        storage.ensure(self.db, self.kind_v2({1: step}))  # again: nothing to do

    def test_an_upgrade_that_fails_leaves_the_tables_as_they_were(self):
        self.db.call(put, 1, timeout=30)

        def step(conn):
            conn.exec_driver_sql("ALTER TABLE test_rows ADD COLUMN extra TEXT")
            raise ValueError("a bug in the step")
        with self.assertLogs("forza-live", "INFO") as logs, self.assertRaises(storage.Refused):
            storage.ensure(self.db, self.kind_v2({1: step}))
        self.assertIn("could not be made or upgraded", logs.output[-1])
        with plain(self.db.path) as conn:
            self.assertEqual(conn.execute("SELECT version FROM stores WHERE name = 'test'")
                             .fetchone()[0], 1)
            self.assertEqual([c[1] for c in conn.execute("PRAGMA table_info(test_rows)")],
                             ["n", "text"])

    def test_older_tables_without_their_upgrade_refuse_the_start(self):
        with self.assertLogs("forza-live", "ERROR") as logs, self.assertRaises(storage.Refused):
            storage.ensure(self.db, self.kind_v2({}))
        self.assertIn("no upgrade from version 1", logs.output[0])

    def test_the_revision_counts_the_commits(self):
        for n in range(3):
            self.db.call(lambda conn, n=n: (put(conn, n), storage.bump(conn, "test")))
        self.assertEqual(self.db.read(storage.state, "test")[0], 3)


class OperatorTest(Base):
    def test_a_backup_while_writes_go_on(self):
        for n in range(100):
            self.db.call(put, n, timeout=30)
        stop = threading.Event()

        def writing():
            n = 100
            while not stop.is_set():
                self.db.call(put, n, timeout=30)
                n += 1
        writer = threading.Thread(target=writing)
        writer.start()
        try:
            target = os.path.join(self.dir, "copy.sqlite3")
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(storage.main(["--data-dir", self.dir, "backup", target]), 0)
            self.assertIn("copied to", said.getvalue())
        finally:
            stop.set()
            writer.join(30)
        with plain(target) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            found = [r[0] for r in conn.execute("SELECT n FROM test_rows ORDER BY n")]
        self.assertEqual(found, list(range(len(found))))  # a moment of the database, whole
        self.assertGreaterEqual(len(found), 100)

    def test_the_names_put_aside_are_never_written_over(self):
        path = os.path.join(self.dir, "x.json")
        for n in range(4):
            with open(path, "w") as f:
                f.write(str(n))
            storage._put_aside(path, ".unused")
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n.startswith("x.json")),
                         ["x.json.unused", "x.json.unused.1", "x.json.unused.2", "x.json.unused.3"])
        with open(path + ".unused") as f:
            self.assertEqual(f.read(), "0")


class ReadOnlyTest(Base):
    """A read-only engine (the website accounts service's read-only mount of the data folder)."""

    def test_it_reads_while_the_server_writes_and_after_it_stopped(self):
        self.db.call(put, 1, timeout=30)
        engine = storage.engine_for(self.db.path, read_only=True)
        self.addCleanup(engine.dispose)
        with engine.connect() as conn:
            self.assertEqual(rows(conn), [(1, "row")])
        self.db.call(put, 2, timeout=30)
        with engine.connect() as conn:
            self.assertEqual(len(rows(conn)), 2)
            with self.assertRaises(Exception):  # and it can write nothing
                put(conn, 3)
        storage.close_all()  # the server stopped: no -wal, no -shm
        if os.name == "posix":
            os.chmod(self.dir, 0o555)  # and a folder it cannot make them in
            self.addCleanup(os.chmod, self.dir, 0o755)
        with engine.connect() as conn:
            self.assertEqual(len(rows(conn)), 2)
        if os.name == "posix":  # it made nothing there (Windows' reader keeps its -wal and -shm)
            self.assertEqual(sorted(os.listdir(self.dir)), [storage.FILE])


if __name__ == "__main__":
    unittest.main()
