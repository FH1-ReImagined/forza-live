"""friends on the server's database (#75; social/friends.py on core/storage.py): the import of
friends.json at the first start, every read the same before and after it and after a restart,
what a kill loses (at most the change that was not committed yet, never the database), the
stop, a start with the database and no file and with both, the event loop never held by a
friends change (core/watchdog.py), an enrollment that waits for its binding off the loop, the
website accounts service's read-only view, and the operator's export and import.

The friends.json here is made up, of the public server's shape and about its size (1.8 MB):
players with a public key, with an older game's friends key, and records without a key (a name
someone blocked or asked), with friends, requests both ways, blocks and mutes.

Run: python3 -m unittest discover -s tests -t .
"""

import asyncio
import contextlib
import io
import json
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from forza_live import server as srv
from forza_live.core import auth, storage, watchdog
from forza_live.social import friends as fr
from tests.support import PYTHON, ROOT, FakeTransport, child_env

DAY = 86400.0
NOW = 1795000000.0  # the made-up file's "now"
PLAYERS = 4800  # about the public server's 1.8 MB
KEY = bytes(range(16))


def key_of(n):
    """The raw friends key of the made-up player n that signs in (an older game's)."""
    return KEY[:14] + struct.pack(">H", n)


def made_up(players=PLAYERS, seed=75, now=NOW, online=0):
    """friends.json's records ({xuid hex: record}), made up. The first `online` players have an
    older game's friends key (key_of), so that a test can sign them in."""
    rnd = random.Random(seed)
    keys = {}
    while len(keys) < players:
        keys["%016X" % (0x0009000000000000 + rnd.randrange(1 << 40))] = None
    keys = list(keys)
    recs, keyed = {}, []
    for i, k in enumerate(keys):
        p = {"name": "Driver%05d" % i if rnd.random() > 0.01 else "Same Name",
             "seen": now - rnd.random() * 400 * DAY, "key": "", "friends": {}, "in": {}, "out": {},
             "blocked": {}}
        kind = rnd.random()
        if i < online:
            p["key"] = fr.key_hash(key_of(i))
        elif kind < 0.80:
            p["pub"] = "%064x" % rnd.getrandbits(256)
        elif kind < 0.90:
            p["key"] = "%064x" % rnd.getrandbits(256)  # an older game's, hashed
        recs[k] = p  # else no key: a name another player blocked or asked
        if p["key"] or p.get("pub"):
            keyed.append(k)
    for k in keyed:
        p = recs[k]
        for _ in range(rnd.randrange(6)):
            o = rnd.choice(keyed)
            if o != k:
                since = now - rnd.random() * 100 * DAY
                p["friends"][o] = recs[o]["friends"][k] = since
        if rnd.random() < 0.15:
            o = rnd.choice(keys)
            if o != k and o not in p["friends"]:
                p["out"][o] = recs[o]["in"][k] = now - rnd.random() * 9 * DAY
        if rnd.random() < 0.05:
            o = rnd.choice(keys)
            if o != k:
                p["blocked"][o] = now - rnd.random() * 9 * DAY
        if rnd.random() < 0.03:
            p["muted"] = {rnd.choice(keyed): now - rnd.random() * DAY}
    return recs


def as_before(players, now=None):
    """The store a start made of friends.json before the database: the previous version's
    Store.load (the records it forgets left out), in memory."""
    cutoff = (time.time() if now is None else now) - fr.FORGET_DAYS * DAY
    store = fr.Store()
    store.players = {k: p for k, p in json.loads(json.dumps(players)).items()
                     if p["friends"] or p["in"] or p["out"] or p["blocked"] or p.get("pub")
                     or p.get("key") or p["seen"] >= cutoff}
    store.count()
    return store


def reads(store, names=200, pairs=3000, seed=1):
    """What every caller reads from the store: each record by Store.get and its name, its binding
    as auth reads it, the relation and the block of every pair the maps name and of random pairs,
    gamertag searches by name, and the counts."""
    out = {"players": store.players, "order": list(store.players), "keyed": store.keyed,
           "full": store.full()}
    rnd = random.Random(seed)
    keys = list(store.players)
    for k, p in store.players.items():
        x = int(k, 16)
        out["get", k] = store.get(x)
        out["name", k] = store.name(x)
        pub = auth.bound_pub(store, x)
        out["auth", k] = (pub.hex() if pub else None, auth.legacy_hash(store, x),
                          auth.bound(store, x))
        for m in ("friends", "in", "out", "blocked", "muted"):
            for o in p.get(m, ()):
                y = int(o, 16)
                out["rel", k, o] = (store.relation(x, y), store.relation(y, x), store.blocks(x, y),
                                    store.blocks(y, x))
    for _ in range(pairs):
        a, b = int(rnd.choice(keys), 16), int(rnd.choice(keys), 16)
        out["pair", a, b] = (store.relation(a, b), store.blocks(a, b))
    for p in rnd.sample(list(store.players.values()), names):
        out["find", p["name"]] = store.find(p["name"])
    out["find", "Same Name"] = store.find("Same Name")
    out["find", "nobody at all"] = store.find("nobody at all")
    return out


def friends_reads(store, queries=("driver0", "same", "Driver01234", "9")):
    """What the FRIEND messages answer from it: the lists, the searches, the status."""
    server = srv.ForzaLive("test", 64)
    server.transport = FakeTransport()
    server.friends.store = store
    out = {"status": server.friends.status()}
    for k in list(store.players)[:300]:
        out["list", k] = server.friends.entries(int(k, 16))
    peer = srv.Peer(1, ("127.0.0.1", 20001), 1, int(next(iter(store.players)), 16), "Asker",
                    "#TU4", (0, 0))
    for n, q in enumerate(queries):
        server.friends.search(peer, n, q)
    out["searches"] = [d for _, d in server.transport.sent]
    server.events.db.close()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(storage.close_all)  # before the folder goes: Windows keeps open files
        self.path = os.path.join(self.dir, fr.FILE)

    def file(self, players, path=None):
        with open(path or self.path, "w") as f:
            json.dump({"version": 1, "players": players}, f)  # as the previous versions wrote it

    def start(self, logs="INFO"):
        """A start of the store on the folder, as server.main's (the previous one stopped)."""
        storage.close_all()
        store = fr.Store(self.path)
        if logs:
            with self.assertLogs(level=logs) as said:  # forza-live's, and durable's forza_live
                store.load()
            self.said = said.output
        else:
            store.load()
        return store

    def on_disk(self):
        """What the database holds now, read by sqlite3 itself, and its integrity check."""
        conn = sqlite3.connect(os.path.join(self.dir, storage.FILE))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            return conn.execute("SELECT count(*) FROM friends_players").fetchone()[0]
        finally:
            conn.close()

    def names(self):
        return sorted(os.listdir(self.dir))


class ImportTest(Base):
    def test_the_first_start_imports_friends_json_and_keeps_the_file(self):
        players = made_up()
        self.file(players)
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertTrue(1.5 < len(raw) / 1048576 < 2.2, len(raw))  # the public server's size
        before = as_before(players)
        store = self.start()
        self.assertRegex(self.said[0], r"friends: %d record\(s\) imported from friends\.json into "
                                       r"forza_live\.sqlite3 in \d+\.\d\d s; the file is kept as "
                                       r"friends\.json\.imported$" % len(players))
        self.assertIn("forza_live.sqlite3", self.names())
        self.assertNotIn(fr.FILE, self.names())
        with open(self.path + ".imported", "rb") as f:
            self.assertEqual(f.read(), raw)  # the file as it was: never changed, never deleted
        self.assertLess(len(store.players), len(players))  # it has records a start forgets
        self.assertEqual(reads(store), reads(before))
        self.assertEqual(friends_reads(store), friends_reads(before))
        self.assertTrue(store.save())  # what the start forgot is gone from the database too
        self.assertEqual(self.on_disk(), len(before.players))
        again = self.start()  # and a start from the database alone reads the same
        self.assertEqual(reads(again), reads(before))
        self.assertEqual(friends_reads(again), friends_reads(before))

    def test_every_read_is_the_same_after_any_change_and_a_restart(self):
        players = made_up(1500, seed=7)
        self.file(players)
        rnd, clock = random.Random(75), [NOW]
        with mock.patch.object(fr.time, "time", lambda: clock[0]):
            # the database's store, and one in memory only, with the same clock
            store, kept = self.start(), as_before(players, now=NOW)
            keys = list(kept.players)
            for round_ in range(6):
                for _ in range(150):
                    clock[0] += 0.25  # the same time for both stores' change
                    a, b = int(rnd.choice(keys), 16), int(rnd.choice(keys), 16)
                    new = 0x000A000000000000 + rnd.randrange(1 << 20)
                    op = rnd.randrange(10)
                    for s in (store, kept):
                        if a == b:
                            continue
                        if op == 0:
                            s.befriend(a, b)
                        elif op == 1:
                            s.ask(a, b)
                        elif op == 2:
                            s.block(a, b)
                        elif op == 3:
                            s.unblock(a, b)
                        elif op == 4:
                            s.unlink(a, b)
                        elif op == 5:
                            s.seen(new, "Newcomer%d" % (new & 0xFFFF))  # a record made anew
                        elif op == 6:
                            s.claim(new + 1, key_of(round_))  # an older game's key, bound
                        elif op == 7:
                            auth.rebind(s, a, bytes([round_]) * 32, "upgraded")
                        elif op == 8:  # chat's mute: the record changed in place, then touch()
                            s.get(a, True).setdefault("muted", {})["%016X" % b] = NOW
                            s.touch(a)
                        else:
                            s.seen(a, "Renamed%d" % round_)
                    if op == 5 or op == 6:
                        keys += ["%016X" % new, "%016X" % (new + 1)]
                keys = [k for k in dict.fromkeys(keys) if k in kept.players]
                self.assertTrue(store.save())
                self.assertEqual(store.players, kept.players)
                again = self.start(logs=None)
                kept.count()
                self.assertEqual(reads(again, names=50, pairs=500), reads(kept, names=50, pairs=500))
                store = again

    def test_a_record_of_any_shape_reads_back_as_it_was(self):
        odd = {"0009000000000001": {"name": "A", "seen": 5, "key": "x", "friends": {},
                                    "in": {}, "out": {}, "blocked": {}, "muted": {},
                                    "note": [1, {"x": None}]},  # an empty map, a field of its own
               "0009000000000002": {"name": None, "seen": NOW, "key": "k", "pub": "",
                                    "friends": {"0009000000000001": 3}, "in": {}, "out": {},
                                    "blocked": {}, "muted": {"bad": "not a time"}}}
        self.file(odd)
        store = self.start()
        self.assertEqual(store.players, odd)
        self.assertEqual(self.start(logs=None).players, odd)

    def test_a_damaged_file_is_imported_from_its_bak(self):
        players = made_up(300, seed=3)
        self.file(players, self.path + ".bak")
        with open(self.path, "wb") as f:
            f.write(b"\0" * 4096)  # zeros, as after a power cut
        store = self.start("WARNING")
        self.assertIn("using its previous copy friends.json.bak", self.said[0])
        self.assertEqual(store.players, as_before(players).players)
        self.assertIn("friends.json.imported", self.names())  # the zeros, kept all the same
        self.assertIn("friends.json.bak", self.names())

    def test_a_file_that_cannot_be_read_refuses_the_start(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        store = fr.Store(self.path)
        with self.assertLogs("forza-live", "ERROR") as logs, self.assertRaises(storage.Refused):
            store.load()
        self.assertIn("friends.json is there and cannot be read", logs.output[-1])
        self.assertIn("not started", logs.output[-1])
        self.assertEqual(store.players, {})
        self.assertIn(fr.FILE, self.names())  # left as it is
        players = made_up(200, seed=4)
        self.file(players)  # once it is readable, the next start imports it
        self.assertEqual(self.start().players, as_before(players).players)

    def test_a_file_of_another_shape_refuses_the_start(self):
        for data in ({"players": []}, {"players": {"0009000000000001": {"name": "A"}}}, []):
            with self.subTest(data=data):
                with open(self.path, "w") as f:
                    json.dump(data, f)
                with self.assertLogs("forza-live", "ERROR"), self.assertRaises(storage.Refused):
                    fr.Store(self.path).load()

    def test_the_server_does_not_start_on_a_friends_json_it_cannot_read(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        done = subprocess.run([PYTHON, os.path.join(ROOT, "forza_live_server.py"), "--host",
                               "127.0.0.1", "--port", "0", "--data-dir", self.dir],
                              cwd=ROOT, capture_output=True, text=True, timeout=60, env=child_env())
        self.assertNotEqual(done.returncode, 0)
        said = [line for line in done.stderr.splitlines() if "friends" in line]
        self.assertRegex(said[-1], r"friends: friends\.json is there and cannot be read .* not started")
        self.assertNotIn("listening", done.stderr)
        self.assertNotIn("Traceback", done.stderr)


class BothTest(Base):
    """A start with the database and a friends.json beside it: which is the store, and why."""

    def imported(self, players):
        self.file(players)
        store = self.start()
        store.seen(0x0009000000ABCDEF, "AfterImport")  # a change the file does not have
        self.assertTrue(store.save())
        return store

    def test_a_start_with_the_database_and_no_file(self):
        store = self.imported(made_up(500))
        again = self.start()
        self.assertEqual(again.players, store.players)
        self.assertEqual(self.said, ["INFO:forza-live:friends: %d player(s) from "
                                     "forza_live.sqlite3, %d with a key"
                                     % (len(store.players), store.keyed)])

    def test_the_imported_file_put_back_is_kept_aside_the_database_is_the_store(self):
        store = self.imported(made_up(500))
        shutil.copy(self.path + ".imported", self.path)  # by hand, or a backup restored over it
        again = self.start()
        self.assertEqual(again.players, store.players)  # with the change made after the import
        self.assertIn("friends.json is the file forza_live.sqlite3 imported", self.said[0])
        self.assertEqual([n for n in self.names() if n.startswith(fr.FILE)],
                         ["friends.json.imported", "friends.json.imported.1"])

    def test_after_an_export_the_previous_version_s_file_is_taken(self):
        """The rollback docs/operating.md describes: stop, export, the previous version runs on
        the export and changes things; back to this version: its file has everything."""
        store = self.imported(made_up(500))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(storage.main(["--data-dir", self.dir, "export", "friends",
                                           self.path]), 0)
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data, {"version": 1, "players": store.players})
        data["players"]["0009000000FEDCBA"] = {"name": "DuringRollback", "seen": NOW, "key": "",
                                               "friends": {}, "in": {}, "out": {}, "blocked": {}}
        self.file(data["players"])  # the previous version's write
        again = self.start()
        self.assertIn("imported again", self.said[0])
        self.assertEqual(again.players, as_before(data["players"]).players)
        self.assertEqual(again.name(0x0009000000FEDCBA), "DuringRollback")
        self.assertNotIn(fr.FILE, self.names())

    def test_an_export_nobody_started_from_is_kept_aside(self):
        store = self.imported(made_up(500))
        with contextlib.redirect_stderr(io.StringIO()):
            storage.main(["--data-dir", self.dir, "export", "friends", self.path])
        again = self.start()
        self.assertEqual(again.players, store.players)
        self.assertIn("friends.json is the file forza_live.sqlite3 exported", self.said[0])
        self.assertIn("friends.json.exported", self.names())

    def test_a_file_that_does_not_continue_the_database_is_kept_aside(self):
        for case in ("no export", "changed since the export", "a new server"):
            with self.subTest(case=case):
                storage.close_all()
                for name in os.listdir(self.dir):
                    os.remove(os.path.join(self.dir, name))
                if case == "a new server":
                    store = self.start()  # no file: nothing to import, and none to come
                    store.seen(0x0009000000ABCDEF, "AfterImport")
                    store.save()
                else:
                    store = self.imported(made_up(500))
                if case == "changed since the export":
                    with contextlib.redirect_stderr(io.StringIO()):
                        storage.main(["--data-dir", self.dir, "export", "friends", self.path])
                    os.remove(self.path)
                    store = self.start()
                    store.seen(0x0009000000ABCDE0, "AfterExport")
                    self.assertTrue(store.save())
                # a previous version that started without the export: a store of its own
                small = {"0009000000000099": {"name": "Alone", "seen": NOW, "key": "x",
                                              "friends": {}, "in": {}, "out": {}, "blocked": {}}}
                self.file(small)
                again = self.start("WARNING")
                self.assertIn("does not continue it", self.said[0])
                self.assertIn("kept as friends.json.unused", self.said[0])
                self.assertEqual(again.players, store.players)
                self.assertIn("friends.json.unused", self.names())
                # and the operator can take it instead, with the server stopped
                storage.close_all()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(storage.main(["--data-dir", self.dir, "import", "friends",
                                                   self.path + ".unused"]), 0)
                self.assertEqual(self.start().players, small)


CHILD = r"""
import asyncio, os, sys, threading, time
from forza_live.core import storage
from forza_live.social import friends as fr

folder, mode, first = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = fr.Store(os.path.join(folder, fr.FILE))


async def main():
    store.load()
    n = first
    if mode == "held":  # one change committed, then the writer is held before the next commit
        store.seen(0x000B000000000000 + n, "Kept%d" % n)
        assert await store.written()
        print("kept %d" % n, flush=True)
        real, go = fr.write_changes, threading.Event()

        def held(*args):
            print("writing %d" % (n + 1), flush=True)
            go.wait()
            return real(*args)
        fr.write_changes = held
        store.seen(0x000B000000000000 + n + 1, "Lost%d" % (n + 1))
        await asyncio.sleep(3600)
    while True:  # changes, each told once it is committed, as fast as they come
        store.seen(0x000B000000000000 + n, "Kept%d" % n)
        if n % 3:
            store.befriend(0x000B000000000000 + n, 0x000B000000000000 + n - 1)
        written = asyncio.ensure_future(store.written())
        if n % 5 == 0:
            await written
            print("kept %d" % n, flush=True)
        else:
            written.add_done_callback(lambda w, n=n: w.result() and print("kept %d" % n, flush=True))
            await asyncio.sleep(0)
        n += 1

asyncio.run(main())
"""


class KillTest(Base):
    """A process killed (SIGKILL; TerminateProcess on Windows) while it changes the store."""

    def child(self, mode, first):
        errors = open(os.path.join(self.dir, "child-%d.log" % first), "w+")
        self.addCleanup(errors.close)
        proc = subprocess.Popen([PYTHON, "-c", CHILD, self.dir, mode, str(first)], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=errors, text=True, env=child_env())
        self.addCleanup(proc.kill)
        return proc, errors

    def end(self, proc, errors, lines):
        proc.kill()
        lines += proc.stdout.read().splitlines(keepends=True)
        proc.stdout.close()
        proc.wait()
        errors.seek(0)
        self.assertNotIn("Traceback", errors.read())
        return [line.split() for line in lines if line.endswith("\n")]

    def test_a_kill_between_a_change_and_its_commit_loses_that_change_only(self):
        self.file(made_up(2000))
        proc, errors = self.child("held", 1)
        lines = []
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("writing"):
                break
        told = self.end(proc, errors, lines)
        self.assertEqual(told, [["kept", "1"], ["writing", "2"]])
        store = self.start()
        self.assertEqual(store.name(0x000B000000000001), "Kept1")
        self.assertIsNone(store.get(0x000B000000000002))  # the change that was not committed
        self.assertEqual(len(store.players), len(as_before(made_up(2000)).players) + 1)
        self.on_disk()  # and the database is whole

    def test_killed_at_any_moment_nothing_committed_is_lost(self):
        self.file(made_up(2000))
        self.start()  # imported once, here
        rnd, told, first = random.Random(75), set(), 0
        for kill in range(5):
            storage.close_all()
            proc, errors = self.child("run", first)
            lines, after = [], rnd.randrange(5, 200)
            for line in proc.stdout:
                lines.append(line)
                if len(lines) >= after:
                    break
            time.sleep(rnd.random() * 0.05)
            for what, n in self.end(proc, errors, lines):
                told.add(int(n))
            first = max(told) + 1 if told else first
            store = self.start()  # every start reads it: never refused, never damaged
            self.on_disk()
            for n in told:
                self.assertEqual(store.name(0x000B000000000000 + n), "Kept%d" % n)
                if n % 3:
                    self.assertEqual(store.relation(0x000B000000000000 + n,
                                                    0x000B000000000000 + n - 1), fr.FRIEND)
        self.assertGreater(len(told), 20)


class StopTest(Base):
    def server(self):
        server = srv.ForzaLive("test", 4)
        self.addCleanup(server.events.db.close)
        server.transport = FakeTransport()
        server.state_path = os.path.join(self.dir, srv.STATE_FILE)
        server.friends.store = store = fr.Store(self.path)
        store.load()
        return server

    def test_the_stop_commits_every_change_before_the_stores_with_a_journal(self):
        self.file(made_up(1000))
        with self.assertLogs("forza-live", "INFO"):
            server = self.server()
        store = server.friends.store
        real, order = fr.write_changes, []

        def slow(*args):  # a slow disk: each write takes 20 ms
            time.sleep(0.02)
            return real(*args)

        async def changes():
            for n in range(60):
                store.seen(0x000C000000000000 + n, "Late%d" % n)
                await asyncio.sleep(0)  # each its own write, queued behind the slow ones
        with mock.patch.object(fr, "write_changes", slow):
            asyncio.run(changes())
            self.assertTrue(store.saving)  # most of them still queued
            with mock.patch.object(srv.notifications.STORE, "stop",
                                   lambda: order.append(("notifications", self.on_disk(),
                                                         dict(storage._OPEN)))), \
                    mock.patch.object(srv.scores.STORE, "stop", lambda: order.append("boards")), \
                    mock.patch.object(srv.coop_ledger, "LEDGER", mock.Mock(dirty=False)):
                srv.save_at_stop(server)
        self.assertEqual(order[0][0], "notifications")
        self.assertEqual(order[0][2], {})  # the database closed before the stores with a journal
        self.assertEqual(order[-1], "boards")
        again = self.start()
        for n in range(60):
            self.assertEqual(again.name(0x000C000000000000 + n), "Late%d" % n)
        self.assertEqual(order[0][1], len(again.players))  # all of it on disk by then

    def test_a_write_that_fails_is_made_again(self):
        self.file(made_up(300))
        with self.assertLogs("forza-live", "INFO"):
            server = self.server()
        store = server.friends.store
        with mock.patch.object(fr, "write_changes", side_effect=OSError("no space left")):
            store.seen(0x000C000000000001, "Retried")
            with self.assertLogs("forza-live", "WARNING") as logs:
                self.assertFalse(store.save())
        self.assertIn("written again with the next ones", logs.output[-1])
        self.assertTrue(store.pending())
        with self.assertLogs("forza-live", "INFO") as logs:
            store.maybe_save()  # the next tick, without an event loop: here
        self.assertIn("written to forza_live.sqlite3 again", logs.output[0])
        self.assertFalse(store.pending())
        self.assertEqual(self.start().name(0x000C000000000001), "Retried")


class LoopTest(Base, unittest.IsolatedAsyncioTestCase):
    """The store of the public server's size on a slow disk, its players sending FRIEND messages
    to the server's handler, timed by the event loop's watchdog (#74) at its default limit."""

    ONLINE = 40

    async def asyncSetUp(self):
        await asyncio.to_thread(lambda: self.file(made_up(online=self.ONLINE)))
        self.server = srv.ForzaLive("test", 64)
        self.addCleanup(self.server.events.db.close)
        self.server.transport = FakeTransport()
        self.server.friends.store = self.server.chat.store = self.store = fr.Store(self.path)
        with self.assertLogs("forza-live", "INFO"):
            await asyncio.to_thread(self.store.load)  # the start's load: before the loop serves
        self.watch = watchdog.WATCH
        self.addCleanup(self.watch.configure, 0)  # as every other test finds it: off
        self.watch.configure(watchdog.LIMIT_MS, watchdog.STACK_MS)
        self.watch.start()
        self.addCleanup(self.watch.stop)
        self.loop_thread = threading.get_ident()
        self.peers = [self.sign_in(n) for n in range(self.ONLINE)]

    def sign_in(self, n):
        k = list(self.store.players)[n]
        x, name = int(k, 16), self.store.players[k]["name"]
        body = struct.pack(">HQQIH", srv.VERSION, n + 1, x, 0, 0)
        body += bytes([4]) + b"#TU4" + bytes([len(name)]) + name.encode()
        body += bytes([srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS, 16]) + key_of(n)
        addr = ("127.0.%d.1" % (n + 1), 21000 + n)  # an address each: the limits are per address
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body, addr)
        peer = self.server.by_addr[addr]
        self.assertTrue(peer.friends_ok)
        return peer

    def friend(self, peer, op, other):
        body = struct.pack(">BQ", op, other.xuid)
        if op in (fr.REQUEST, fr.BLOCK):
            body += fr.text8(other.name)
        self.server.datagram_received(srv.MAGIC + bytes([srv.FRIEND]) + body, peer.addr)

    async def test_friends_changes_never_hold_the_loop(self):
        real, threads = fr.write_changes, set()

        def slow(*args):  # a slow disk: 30 ms a write, in the writer's thread
            threads.add(threading.get_ident())
            time.sleep(0.03)
            return real(*args)
        rnd = random.Random(74)
        with mock.patch.object(fr, "write_changes", slow), \
                mock.patch.object(fr, "ACTIONS_PER_MIN", 10 ** 6), \
                mock.patch.object(fr, "REQUESTS_PER_MIN", 10 ** 6), \
                self.assertNoLogs("forza-live", "WARNING"):
            for n in range(400):
                a, b = rnd.sample(self.peers, 2)
                self.friend(a, rnd.choice((fr.REQUEST, fr.REQUEST, fr.ACCEPT, fr.DECLINE,
                                           fr.REMOVE, fr.BLOCK, fr.UNBLOCK)), b)
                if n % 50 == 0:
                    self.watch.call("friends.maybe_save", self.store.maybe_save)  # the tick
                await asyncio.sleep(0.002)
            self.assertTrue(self.store.saving)  # the slow disk is behind: the loop never waited
            self.assertTrue(await self.store.written())
            await asyncio.sleep(0.1)  # the heartbeat's last look
        status = self.watch.status()["event_loop"]
        self.assertEqual(status["over_limit_since_start"], 0, status)
        self.assertGreater(len(threads), 0)
        self.assertNotIn(self.loop_thread, threads)
        self.watch.stop()
        again = await asyncio.to_thread(self.start, None)
        self.assertEqual(again.players, self.store.players)

    async def test_the_watchdog_sees_a_friends_write_that_holds_the_loop(self):
        """The control: the same watchdog sees a save() made on the loop (as events.enroll made
        one before #75) while the disk is slow."""
        real = fr.write_changes

        def slow(*args):
            time.sleep(0.15)
            return real(*args)
        self.store.seen(self.peers[0].xuid, "Renamed")
        with mock.patch.object(fr, "write_changes", slow), \
                self.assertLogs("forza-live", "WARNING") as logs:
            self.watch.call("friends.save", self.store.save)
            await asyncio.sleep(0.1)
        self.assertTrue(any("task friends.save held the event loop" in line
                            for line in logs.output), logs.output)

    async def test_an_enrollment_waits_for_its_binding_off_the_loop(self):
        peer = self.sign_in_new()
        go, ticks, real = threading.Event(), [], fr.write_changes

        def held(*args):
            go.wait(30)
            return real(*args)

        async def ticker():
            while not go.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
        with mock.patch.object(fr, "write_changes", held):
            self.store.seen(peer.xuid, "Enrolled")  # a change of its record, not yet on disk
            result = self.server.events.handle("POST", "/admin/events/enroll",
                                               {"xuid": "%016X" % peer.xuid})
            self.assertTrue(asyncio.iscoroutine(result))
            enrolling = asyncio.ensure_future(result)
            ticking = asyncio.ensure_future(ticker())
            await asyncio.sleep(0.2)
            self.assertFalse(enrolling.done())  # it waits for the disk...
            self.assertGreater(len(ticks), 10)  # ...and the loop goes on meanwhile
            go.set()
            self.assertEqual(await enrolling, {"xuid": "%016X" % peer.xuid, "enrolled": True})
            await ticking
        self.assertFalse(self.store.pending())
        # with nothing waiting to be written, it answers at once
        self.assertEqual(self.server.events.handle("POST", "/admin/events/enroll",
                                                   {"xuid": "%016X" % peer.xuid}),
                         {"xuid": "%016X" % peer.xuid, "enrolled": True})

    async def test_an_enrollment_whose_binding_cannot_be_written_is_refused(self):
        peer = self.sign_in_new()
        with mock.patch.object(fr, "write_changes", side_effect=OSError("no space left")), \
                self.assertLogs("forza-live", "WARNING"):
            self.store.seen(peer.xuid, "NotKept")
            with self.assertRaisesRegex(OSError, "could not persist"):
                await self.server.events.handle("POST", "/admin/events/enroll",
                                                {"xuid": "%016X" % peer.xuid})

    def sign_in_new(self):
        """A player of its own (the others' sign-ins may have changed records)."""
        return self.peers[self.ONLINE - 1]


class ReaderTest(Base):
    """The website accounts service's view (accounts/members/game.py): read-only, a record at a
    time, as fresh as the last commit."""

    def test_it_reads_what_the_store_has(self):
        self.file(made_up(800))
        store = self.start()
        reader = fr.Reader(self.dir)
        self.addCleanup(reader.close)
        for k in list(store.players)[:200]:
            self.assertEqual(reader.get(int(k, 16)), store.players[k])
            pub = auth.bound_pub(reader, int(k, 16))
            self.assertEqual(pub.hex() if pub else None, store.players[k].get("pub") or None)
        k = next(k for k, p in store.players.items() if p.get("pub"))
        auth.rebind(store, int(k, 16), b"\x07" * 32, "upgraded")
        self.assertTrue(store.save())
        self.assertEqual(auth.bound_pub(reader, int(k, 16)), b"\x07" * 32)  # at once
        self.assertIsNone(reader.get(0x0009FFFFFFFFFFFF))
        storage.close_all()  # the server stopped
        self.assertEqual(auth.bound_pub(reader, int(k, 16)), b"\x07" * 32)

    def test_it_forgets_what_a_start_forgets(self):
        old = {"0009000000000001": {"name": "Old", "seen": NOW - 400 * DAY, "key": "",
                                    "friends": {}, "in": {}, "out": {}, "blocked": {}}}
        storage.ensure(storage.database(self.dir), fr.NAME, fr.VERSION, fr.TABLES)
        storage.database(self.dir).call(fr.replace_all, old)
        reader = fr.Reader(self.dir)
        self.addCleanup(reader.close)
        self.assertIsNone(reader.get(0x0009000000000001))
        self.assertEqual(reader.local.conn.connection.dbapi_connection.execute(
            "SELECT name FROM friends_players").fetchall(), [("Old",)])  # there, and forgotten

    def test_no_database_is_no_record(self):
        reader = fr.Reader(self.dir)
        self.addCleanup(reader.close)
        self.assertIsNone(reader.get(0x0009000000000001))


class ExportTest(Base):
    def test_the_export_is_friends_json_as_the_previous_version_reads_it(self):
        players = made_up(1000)
        self.file(players)
        store = self.start()
        storage.close_all()  # the operator's command, in its own process: its own database
        out = io.TextIOWrapper(io.BytesIO())  # stdout, with its buffer
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            self.assertEqual(storage.main(["--data-dir", self.dir, "export", "friends"]), 0)
        data = json.loads(out.buffer.getvalue())
        self.assertEqual(data["version"], 1)
        self.assertEqual(as_before(data["players"]).players, store.players)


if __name__ == "__main__":
    unittest.main()
