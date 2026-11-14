"""journal: a change is a line appended and flushed, the file is written whole now and then
(#67). A start reads the same store whatever moment the server was stopped at; a line or a
file cut short costs nothing anyone was told was kept; a file the journal does not continue is
left as it is.

Run: python3 -m unittest discover -s tests -t .
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from forza_live.core import durable
from forza_live.core import journal


class Kv:
    """The smallest store with a journal: a dict in store.json, a change the keys it sets."""

    def __init__(self, folder):
        self.journal = journal.Journal(os.path.join(folder, "store.json"))
        data, changes = self.journal.load()
        self.data = data or {}
        self.applied = len(changes)
        for change in changes:
            self.data.update(change)

    def set(self, **change):
        self.data.update(change)
        self.journal.append(change)

    def copy(self):
        return self.journal.mark(), dict(self.data)

    def checkpoint(self, copy=None, dump=json.dump):
        mark, data = copy or self.copy()
        return self.journal.checkpoint(mark, lambda f: dump(data, f))


class Stops:
    """Inside it, a copy of the folder is made before every flush to disk and every rename: the
    folder as a kill at that moment leaves it. `copies` are their paths, in order."""

    def __init__(self, folder, into):
        self.folder, self.into, self.copies = folder, into, []

    def copy(self):
        self.copies.append(os.path.join(self.into, "stop-%d" % len(self.copies)))
        shutil.copytree(self.folder, self.copies[-1])

    def __enter__(self):
        def before(real):
            def call(*args, **kw):
                self.copy()
                return real(*args, **kw)
            return call
        self.patches = [mock.patch.object(os, "fsync", before(os.fsync)),
                        mock.patch.object(os, "replace", before(os.replace))]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.dir = os.path.join(self.tmp, "data")
        os.mkdir(self.dir)
        self.opened = []
        self.addCleanup(lambda: [kv.journal.close() for kv in self.opened])

    def path(self, name):
        return os.path.join(self.dir, name)

    def kv(self, folder=None):
        self.opened.append(Kv(folder or self.dir))
        return self.opened[-1]

    def lines(self, name="store.journal"):
        with open(self.path(name), "rb") as f:
            return f.read().split(b"\n")


class JournalTest(Base):
    def test_a_change_is_a_line_and_the_file_is_not_written(self):
        kv = self.kv()
        kv.set(a=1)
        kv.set(b={"x": [1, 2]}, a=2)
        self.assertFalse(os.path.exists(self.path("store.json")))
        head, first, second, end = self.lines()
        self.assertEqual(json.loads(head), {"journal": 1, "continues": {journal.NO_FILE: 0}})
        self.assertEqual((json.loads(first), json.loads(second), end),
                         ({"a": 1}, {"b": {"x": [1, 2]}, "a": 2}, b""))
        again = self.kv()  # a start with no file yet: the journal alone
        self.assertEqual((again.data, again.applied), ({"a": 2, "b": {"x": [1, 2]}}, 2))

    def test_an_append_is_flushed_before_it_returns(self):
        kv = self.kv()
        kv.set(a=1)
        size, flushed = [], []
        real = os.fsync

        def fsync(fd):
            flushed.append(os.fstat(fd).st_size)
            return real(fd)

        with mock.patch.object(os, "fsync", fsync):
            size.append(os.path.getsize(self.path("store.journal")))
            kv.set(b=2)
        self.assertEqual(len(flushed), 1)  # one flush, of the journal with the line in it
        self.assertEqual(flushed[0], os.path.getsize(self.path("store.journal")))
        self.assertGreater(flushed[0], size[0])

    def test_a_checkpoint_writes_the_file_and_the_journal_keeps_what_the_file_before_lacked(self):
        kv = self.kv()
        kv.set(a=1)
        self.assertTrue(kv.journal.behind())
        self.assertTrue(kv.checkpoint())
        self.assertFalse(kv.journal.behind())
        self.assertEqual(durable.load(self.path("store.json")), {"a": 1})
        first = durable.digest(self.path("store.json"))
        self.assertEqual(json.loads(self.lines()[0])["continues"], {journal.NO_FILE: 0, first: 1})
        kv.set(b=2)
        kv.set(c=3)
        kv.checkpoint()
        second = durable.digest(self.path("store.json"))
        head, *rest = self.lines()
        # it starts at the file that is now the .bak: the two changes since, which the file has
        self.assertEqual(json.loads(head)["continues"], {first: 0, second: 2})
        self.assertEqual([json.loads(line) for line in rest[:-1]], [{"b": 2}, {"c": 3}])
        self.assertEqual(durable.digest(self.path("store.json.bak")), first)
        kv.set(d=4)
        again = self.kv()
        self.assertEqual((again.data, again.applied), ({"a": 1, "b": 2, "c": 3, "d": 4}, 1))
        self.assertFalse(os.path.exists(self.path("store.journal.bak")))  # the journal has no copy

    def test_a_big_file_is_flushed_as_it_is_written(self):
        kv = self.kv()
        kv.set(a=1)
        flushed, real = [], os.fsync

        def fsync(fd):
            flushed.append(os.fstat(fd).st_size)
            return real(fd)

        def dump(data, f):  # 3.5 MB, 64 KB at a time
            f.write("{\"big\": \"")
            for _ in range(56):
                f.write("x" * 65536)
            f.write("\"}")

        with mock.patch.object(os, "fsync", fsync):
            self.assertTrue(kv.checkpoint(None, dump))
        size = os.path.getsize(self.path("store.json"))
        sizes = flushed[:flushed.index(size) + 1]  # the new file's flushes, to its last
        # never more than FLUSH_EVERY waiting for the disk, behind which the journal's own
        # flush (the event loop's) would wait
        self.assertEqual(len(sizes), 4)
        for before, after in zip([0] + sizes, sizes):
            self.assertLessEqual(after - before, journal.FLUSH_EVERY + 65536)
        self.assertEqual(len(self.kv().data["big"]), 56 * 65536)

    def test_the_previous_copy_and_the_journal_give_the_same_store(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        kv.checkpoint()
        kv.set(c=3)
        with open(self.path("store.json"), "r+") as f:
            f.truncate(4)  # the file itself cut short: durable reads the .bak
        with self.assertLogs("forza_live", "WARNING"):
            again = self.kv()
        self.assertEqual((again.data, again.applied), ({"a": 1, "b": 2, "c": 3}, 2))
        again.set(d=4)
        again.checkpoint()
        self.assertEqual(self.kv().data, {"a": 1, "b": 2, "c": 3, "d": 4})

    def test_stopped_at_any_flush_or_rename_the_store_is_what_was_acknowledged(self):
        kv = self.kv()
        steps = [("set", {"a": 1}), ("checkpoint", None), ("set", {"b": 2}), ("set", {"a": 3}),
                 ("checkpoint", None), ("set", {"c": 4}), ("checkpoint", None), ("set", {"d": 5}),
                 ("checkpoint", None), ("checkpoint", None)]
        stops, checked = Stops(self.dir, self.tmp), 0
        for what, change in steps:
            before = dict(kv.data)
            with stops:
                kv.set(**change) if what == "set" else kv.checkpoint()
            after = dict(kv.data)
            for folder in stops.copies[checked:]:
                with self.subTest(step=what, stop=os.path.basename(folder)):
                    found = self.kv(folder)
                    # a change being appended is there or not; one acknowledged always is
                    self.assertIn(found.data, (before, after))
                    was = dict(found.data)
                    found.set(z=9)  # and the server goes on from there
                    found.checkpoint()
                    found.set(y=8)
                    self.assertEqual(self.kv(folder).data, dict(was, z=9, y=8))
            checked = len(stops.copies)
        self.assertGreater(checked, 40)
        self.assertEqual(self.kv().data, {"a": 3, "b": 2, "c": 4, "d": 5})

    def test_a_file_half_written_is_never_the_file(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)

        def half(data, f):
            f.write(json.dumps(data)[:7])
            f.flush()
            shutil.copytree(self.dir, os.path.join(self.tmp, "killed"))
            raise OSError("the disk is full")

        self.assertRaises(OSError, kv.checkpoint, None, half)
        self.assertEqual(durable.load(self.path("store.json")), {"a": 1})  # still the one before
        self.assertEqual(self.kv(os.path.join(self.tmp, "killed")).data, {"a": 1, "b": 2})
        kv.set(c=3)  # a write that failed loses nothing: the next one is whole
        self.assertTrue(kv.journal.behind())
        kv.checkpoint()
        self.assertEqual(durable.load(self.path("store.json")), {"a": 1, "b": 2, "c": 3})
        self.assertEqual(self.kv().data, {"a": 1, "b": 2, "c": 3})

    def test_a_line_cut_short_is_dropped_and_the_next_start_is_whole(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        whole = os.path.getsize(self.path("store.journal"))
        kv.set(c={"long": "x" * 40})
        full = os.path.getsize(self.path("store.journal"))
        with open(self.path("store.journal"), "rb") as f:
            raw = f.read()
        for size in list(range(whole + 1, full)) + [full + 3]:  # every cut, and zeros after it
            with self.subTest(size=size):
                folder = os.path.join(self.tmp, "cut-%d" % size)
                shutil.copytree(self.dir, folder)
                with open(os.path.join(folder, "store.journal"), "wb") as f:
                    f.write(raw[:size] + bytes(max(0, size - full)))
                with self.assertLogs("forza-live", "INFO") as logs:
                    found = self.kv(folder)
                if size < full:
                    self.assertEqual(found.data, {"a": 1, "b": 2})  # never told it was kept
                    self.assertIn("cut short", logs.output[0])
                else:
                    self.assertEqual(found.data, {"a": 1, "b": 2, "c": {"long": "x" * 40}})
                found.set(d=4)  # appended to a journal made whole, not after the cut
                again = self.kv(folder)
                self.assertEqual(again.data, dict(found.data))
                with open(os.path.join(folder, "store.journal"), "rb") as f:
                    for line in f.read().split(b"\n")[:-1]:
                        json.loads(line)

    def test_damaged_lines_are_skipped_and_the_others_applied(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        kv.set(c=3)
        kv.checkpoint()
        kv.set(d=4)
        kv.set(e=5)
        head, b, c, d, e, end = self.lines()
        with open(self.path("store.journal"), "wb") as f:  # one the file has, one it lacks
            f.write(b"\n".join([head, b, b"{\"c\": ", b"[1, 2]", e, end]))
        with self.assertLogs("forza-live", "WARNING") as logs:
            found = self.kv()
        self.assertIn("2 damaged lines skipped, 1 changes applied", logs.output[0])
        self.assertEqual(found.data, {"a": 1, "b": 2, "c": 3, "e": 5})
        found.set(f=6)
        self.assertEqual(self.kv().data, {"a": 1, "b": 2, "c": 3, "e": 5, "f": 6})

    def test_a_file_the_journal_does_not_continue_is_left_as_it_is(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)  # in the journal only: the server is killed here
        # a previous version starts on the file, and writes it its own way
        with open(self.path("store.json"), "w") as f:
            json.dump({"a": 1, "old": True}, f, indent=1)
        with self.assertLogs("forza-live", "WARNING") as logs:
            found = self.kv()
        self.assertEqual((found.data, found.applied), ({"a": 1, "old": True}, 0))
        self.assertRegex(logs.output[0], "store.journal does not continue the store.json that is "
                                         "here .* At least 1 of its changes were in no file")
        self.assertFalse(os.path.exists(self.path("store.journal")))
        self.assertEqual(len(self.lines("store.journal.unused")), 4)  # kept, for the operator
        found.set(c=3)  # and a new journal continues the file that is here
        self.assertEqual(self.kv().data, {"a": 1, "old": True, "c": 3})

    def test_a_file_changed_after_a_clean_stop_loses_nothing(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        kv.checkpoint()  # the stop: the file has everything
        with open(self.path("store.json"), "w") as f:
            json.dump({"a": 1, "b": 2, "edited": True}, f)
        with self.assertLogs("forza-live", "INFO") as logs:
            found = self.kv()
        self.assertEqual(found.data, {"a": 1, "b": 2, "edited": True})
        self.assertEqual([r.levelname for r in logs.records], ["INFO"])
        self.assertIn("nothing is lost", logs.output[0])

    def test_a_file_deleted_starts_empty_whatever_the_journal_has(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        kv.checkpoint()
        kv.set(c=3)
        os.remove(self.path("store.json"))  # the journal continues that file, not no file
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(self.kv().data, {})

    def test_a_file_that_cannot_be_read_and_has_no_copy(self):
        with open(self.path("store.json"), "w") as f:
            f.write("{\"a\": ")
        with self.assertLogs("forza-live", "ERROR") as logs:
            kv = self.kv()
        self.assertIn("cannot be read", logs.output[0])
        self.assertEqual(kv.data, {})
        kv.set(b=2)
        with self.assertLogs("forza-live", "ERROR"):
            again = self.kv()  # the file is as unreadable as before: its journal still counts
        self.assertEqual(again.data, {"b": 2})
        again.checkpoint()
        self.assertEqual(self.kv().data, {"b": 2})

    def test_a_damaged_first_line_puts_the_journal_aside(self):
        kv = self.kv()
        kv.set(a=1)
        kv.checkpoint()
        kv.set(b=2)
        raw = b"\n".join(self.lines()[1:])
        for n, head in enumerate((b"", b"{\"journal\": 1}", b"[]", b"{\"continues\": {\"x\": \"y\"}}")):
            with self.subTest(head=head):
                with open(self.path("store.journal"), "wb") as f:
                    f.write(head + b"\n" + raw)
                with self.assertLogs("forza-live", "WARNING") as logs:
                    self.assertEqual(self.kv().data, {"a": 1})
                self.assertIn("is damaged", logs.output[0])
        with open(self.path("store.journal"), "wb"):
            pass  # an empty one
        with self.assertLogs("forza-live", "WARNING"):
            self.assertEqual(self.kv().data, {"a": 1})

    def test_an_append_that_fails_is_not_in_the_journal_and_the_next_one_is(self):
        kv = self.kv()
        kv.set(a=1)
        with mock.patch.object(os, "fsync", side_effect=OSError("no space left")):
            self.assertRaises(OSError, kv.set, b=2)
        self.assertEqual(kv.journal.mark(), 1)
        kv.set(c=3)  # the journal is made anew: what got there of the failed line is gone
        self.assertEqual([json.loads(line) for line in self.lines()[1:-1]], [{"a": 1}, {"c": 3}])
        self.assertEqual(self.kv().data, {"a": 1, "c": 3})

    def test_an_older_copy_never_replaces_a_later_file(self):
        kv = self.kv()
        kv.set(a=1)
        older = kv.copy()
        kv.set(b=2)
        self.assertTrue(kv.checkpoint())
        self.assertFalse(kv.checkpoint(older))
        self.assertEqual(durable.load(self.path("store.json")), {"a": 1, "b": 2})
        kv.journal.close()
        self.assertFalse(kv.checkpoint())  # nor anything once the store is configured again

    def test_changes_appended_while_the_file_is_written_stay_in_the_journal(self):
        kv = self.kv()
        kv.set(a=1)
        writing, go = threading.Event(), threading.Event()

        def slow(data, f):
            writing.set()
            go.wait(30)
            json.dump(data, f)

        thread = threading.Thread(target=kv.checkpoint, args=(kv.copy(), slow))
        thread.start()
        self.assertTrue(writing.wait(30))
        kv.set(b=2)  # answered while the thread writes: the lock is not held for the file
        kv.set(a=5)
        self.assertEqual(self.kv().data, {"a": 5, "b": 2})  # a kill now
        with Stops(self.dir, self.tmp) as stops:  # or at any step of the rest of it
            go.set()
            thread.join(30)
        self.assertGreaterEqual(len(stops.copies), 6)
        for folder in stops.copies:
            with self.subTest(stop=os.path.basename(folder)):
                self.assertEqual(self.kv(folder).data, {"a": 5, "b": 2})
        self.assertEqual(durable.load(self.path("store.json")), {"a": 1})
        self.assertTrue(kv.journal.behind())
        again = self.kv()
        self.assertEqual((again.data, again.applied), ({"a": 5, "b": 2}, 2))
        kv.checkpoint()
        self.assertEqual(durable.load(self.path("store.json")), {"a": 5, "b": 2})


class DurableTest(Base):
    def test_load_digest_names_the_file_it_read(self):
        with open(self.path("f.json"), "w") as f:
            f.write("{\"n\": 1}")
        data, name = durable.load_digest(self.path("f.json"))
        self.assertEqual((data, name), ({"n": 1}, durable.digest(self.path("f.json"))))
        with open(self.path("f.json.tmp"), "w") as f:
            f.write("{\"n\": 2}")
        durable.replace(self.path("f.json.tmp"), self.path("f.json"))
        with open(self.path("f.json"), "w"):
            pass
        with self.assertLogs("forza_live", "WARNING"):
            self.assertEqual(durable.load_digest(self.path("f.json")), ({"n": 1}, name))
        self.assertRaises(FileNotFoundError, durable.load_digest, self.path("none.json"))

    def test_replace_without_a_copy(self):
        for n in range(2):
            with open(self.path("f.tmp"), "w") as f:
                f.write(str(n))
            durable.replace(self.path("f.tmp"), self.path("f"), backup=False)
        self.assertEqual(sorted(os.listdir(self.dir)), ["f"])


if __name__ == "__main__":
    unittest.main()
