"""The server's database (core/storage.py, #75): one writer thread that keeps the order of the
writes, a savepoint for each and a commit for all, futures answered only once committed, the
stop that commits what is queued, reads that see every commit, WAL, the operator's backup, and
a database of a newer version refused.

Run: python3 -m unittest discover -s tests -t .
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import threading
import unittest

from sqlalchemy import Column, Integer, MetaData, Table, Text, event, select

from forza_live.core import storage

TABLES = MetaData()
ROWS = Table("test_rows", TABLES, Column("n", Integer, primary_key=True), Column("text", Text))


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
        storage.ensure(self.db, "test", 1, TABLES)

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
        storage.ensure(self.db, "test", 1, TABLES)  # again: nothing to do
        self.db.call(lambda conn: conn.execute(storage.STORES.update().values(version=2)))
        with self.assertLogs("forza-live", "ERROR") as logs, self.assertRaises(storage.Refused):
            storage.ensure(self.db, "test", 1, TABLES)
        self.assertIn("written by a newer server", logs.output[0])

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
