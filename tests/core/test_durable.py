"""durable: saves survive a power cut that empties the file (2026-10-26)."""
import errno
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from forza_live.core import durable


class Durable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "scoreboards.json")

    def tearDown(self):
        self.dir.cleanup()

    def save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        durable.replace(tmp, self.path)

    def test_keeps_the_previous_file_as_bak(self):
        self.save({"n": 1})
        self.assertFalse(os.path.exists(self.path + ".bak"))
        self.save({"n": 2})
        self.assertEqual(durable.load(self.path), {"n": 2})
        with open(self.path + ".bak") as f:
            self.assertEqual(json.load(f), {"n": 1})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_flushes_the_new_contents_before_the_rename(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                mock.patch.object(os, "replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            self.save({"n": 1})
        self.assertEqual(order[:2], ["fsync", "replace"])

    def test_an_emptied_file_falls_back_to_the_previous_copy(self):
        self.save({"n": 1})
        self.save({"n": 2})
        open(self.path, "w").close()  # what the power cut left
        with self.assertLogs("forza_live", "WARNING"):
            self.assertEqual(durable.load(self.path), {"n": 1})

    def test_damaged_without_a_copy_still_raises(self):
        with open(self.path, "w") as f:
            f.write("{")
        with self.assertRaises(ValueError):
            durable.load(self.path)
        with self.assertRaises(FileNotFoundError):
            durable.load(os.path.join(self.dir.name, "missing.json"))

    def test_an_empty_previous_file_is_not_kept(self):
        open(self.path, "w").close()
        self.save({"n": 1})
        self.assertFalse(os.path.exists(self.path + ".bak"))


class TwoFaults(Durable):
    """The cases the reviews of #69 left (#70): a file that cannot be read at all, and the
    previous copy at every step of the next save."""

    def unreadable(self, path=None):
        """durable's open() fails on `path` (the file in place) as a disk's read error does."""
        path, real = path or self.path, open

        def failing(name, *args, **kw):
            if os.path.abspath(name) == os.path.abspath(path):
                raise OSError(errno.EIO, "Input/output error", name)
            return real(name, *args, **kw)
        return mock.patch.object(durable, "open", failing, create=True)

    def test_a_read_error_falls_back_to_the_previous_copy(self):
        self.save({"n": 1})
        self.save({"n": 2})
        with self.unreadable(), self.assertLogs("forza_live", "WARNING") as logs:
            self.assertEqual(durable.load(self.path), {"n": 1})
            self.assertEqual(durable.load_digest(self.path)[0], {"n": 1})
        self.assertIn("cannot be read", logs.output[0])
        os.mkdir(self.path + ".dir")  # a folder where the file should be: also a read error
        with self.assertRaises(OSError) as raised:  # IsADirectoryError; Windows: PermissionError
            durable.load(self.path + ".dir")
        self.assertNotIsInstance(raised.exception, FileNotFoundError)

    def test_a_read_error_without_a_usable_copy_raises_it(self):
        self.save({"n": 1})
        with self.unreadable(), self.assertRaises(OSError) as raised:
            durable.load(self.path)
        self.assertEqual(raised.exception.errno, errno.EIO)
        with open(self.path + ".bak", "w") as f:
            f.write("{")
        with self.unreadable(), self.assertRaises(OSError) as raised:
            durable.load(self.path)
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_a_save_after_a_start_from_the_copy_keeps_that_copy(self):
        for damage in ("cut", "unreadable"):
            with self.subTest(damage=damage):
                for name in os.listdir(self.dir.name):
                    os.remove(os.path.join(self.dir.name, name))
                self.save({"n": 1})
                self.save({"n": 2})
                if damage == "cut":
                    with open(self.path, "w") as f:
                        f.write('{"n": ')
                    with self.assertLogs("forza_live", "WARNING"):
                        self.assertEqual(durable.load(self.path), {"n": 1})
                else:
                    with self.unreadable(), self.assertLogs("forza_live", "WARNING"):
                        self.assertEqual(durable.load(self.path), {"n": 1})
                self.save({"n": 3})  # the store's next save, from what it read
                with open(self.path + ".bak") as f:
                    self.assertEqual(json.load(f), {"n": 1})  # not the file that failed
                self.save({"n": 4})  # and the save after that keeps the file before it again
                with open(self.path + ".bak") as f:
                    self.assertEqual(json.load(f), {"n": 3})

    def test_a_load_that_reads_the_file_again_forgets_the_copy_it_used(self):
        self.save({"n": 1})
        self.save({"n": 2})
        with open(self.path, "w") as f:
            f.write("{")
        with self.assertLogs("forza_live", "WARNING"):
            durable.load(self.path)
        with open(self.path, "w") as f:
            json.dump({"n": 5}, f)  # put right by hand, and read again
        self.assertEqual(durable.load(self.path), {"n": 5})
        self.save({"n": 6})
        with open(self.path + ".bak") as f:
            self.assertEqual(json.load(f), {"n": 5})

    def stopped_at_every_step(self, save):
        """The folder as a kill leaves it before each call durable.replace makes."""
        copies = []

        def before(real):
            def call(*args, **kw):
                copies.append(os.path.join(self.dir.name + "-stops", str(len(copies))))
                shutil.copytree(self.dir.name, copies[-1])
                return real(*args, **kw)
            return call
        self.addCleanup(shutil.rmtree, self.dir.name + "-stops", True)
        with mock.patch.object(os, "remove", before(os.remove)), \
                mock.patch.object(os, "link", before(os.link)), \
                mock.patch.object(os, "replace", before(os.replace)), \
                mock.patch.object(os, "fsync", before(os.fsync)):
            save()
        return copies

    def test_the_copy_is_replaced_in_one_step(self):
        for after_a_start_from_it in (False, True):
            with self.subTest(after_a_start_from_it=after_a_start_from_it):
                shutil.rmtree(self.dir.name + "-stops", True)
                for name in os.listdir(self.dir.name):
                    os.remove(os.path.join(self.dir.name, name))
                self.save({"n": 1})
                self.save({"n": 2})
                if after_a_start_from_it:
                    with open(self.path, "w") as f:
                        f.write("{")
                    with self.assertLogs("forza_live", "WARNING"):
                        self.assertEqual(durable.load(self.path), {"n": 1})
                copies = self.stopped_at_every_step(lambda: self.save({"n": 3}))
                # the flush and the rename, and without a start from the .bak its three steps
                # (Windows makes no flush of the folder)
                self.assertGreaterEqual(len(copies), 2 if after_a_start_from_it else 5)
                for folder in copies:
                    path = os.path.join(folder, "scoreboards.json")
                    with self.subTest(stop=os.path.basename(folder)), \
                            self.assertNoLogs("forza_live", "ERROR"):
                        try:
                            got = durable.load(path)
                        except ValueError:
                            got = None
                        # what a start there reads: the store as it was, or as it is now
                        self.assertIn(got, ({"n": 1}, {"n": 2}, {"n": 3}))
                        if after_a_start_from_it:  # never the damaged file and nothing else
                            self.assertNotEqual(got, None)


if __name__ == "__main__":
    unittest.main()
