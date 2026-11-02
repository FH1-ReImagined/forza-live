"""forza_durable: saves survive a power cut that empties the file (2026-10-26)."""
import json
import os
import tempfile
import unittest
from unittest import mock

import forza_durable as durable


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


if __name__ == "__main__":
    unittest.main()
