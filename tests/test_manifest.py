"""增量爬取清单的纯函数单测（不联网、不连库）。"""

import tempfile
import unittest
from pathlib import Path

from forage.crawl import _content_hash, load_manifest, save_manifest


class TestManifest(unittest.TestCase):
    def test_content_hash_stable_and_sensitive(self):
        self.assertEqual(_content_hash("abc"), _content_hash("abc"))
        self.assertNotEqual(_content_hash("abc"), _content_hash("abd"))

    def test_missing_manifest_is_empty(self):
        self.assertEqual(load_manifest(Path("/nonexistent/dir/manifest.json")), {})

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            data = {"https://x/y": {"sha256": "deadbeef", "chunks": 7}}
            save_manifest(data, path)
            self.assertEqual(load_manifest(path), data)

    def test_corrupt_manifest_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_manifest(path), {})


if __name__ == "__main__":
    unittest.main()
