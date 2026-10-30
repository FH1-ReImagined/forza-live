import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import forza_photos as photos


PLUGIN = Path(__file__).parent / "plugins" / "public_photos.py"
SPEC = importlib.util.spec_from_file_location("public_photos", PLUGIN)
public_photos = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(public_photos)


class PublicPhotoPluginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        photos.configure(self.directory.name)
        self.fid = photos.create(0x1234, 3, False, "Sunset", "photo", b"\x89PNG\r\n\x1a\npreview",
                                 {"title": "Sunset", "downloads": 2, "ratings": 1, "ratingSum": 5})
        Path(photos.file_path(self.fid)).write_bytes(b"\x89PNG\r\n\x1a\nimage")
        photos.load()["files"][str(self.fid)]["done"] = True

    def tearDown(self):
        self.directory.cleanup()

    def test_lists_completed_photo_with_public_urls(self):
        status, content_type, body = public_photos.list_photos(
            None, __import__("urllib.parse").parse.urlsplit("/api/v1/photos?limit=1"), {}, b"", None)
        payload = json.loads(body)

        self.assertEqual(200, status)
        self.assertEqual("application/json", content_type)
        self.assertEqual("Sunset", payload["items"][0]["title"])
        self.assertIn("variant=preview", payload["items"][0]["previewUrl"])

    def test_serves_uploaded_image(self):
        status, content_type, body = public_photos.photo_content(
            None, __import__("urllib.parse").parse.urlsplit(
                "/api/v1/photos/content?id=%d&variant=image" % self.fid), {}, b"", None)

        self.assertEqual(200, status)
        self.assertEqual("image/png", content_type)
        self.assertEqual(b"\x89PNG\r\n\x1a\nimage", body)


if __name__ == "__main__":
    unittest.main()
