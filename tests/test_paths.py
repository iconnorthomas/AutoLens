"""autolens.paths: result discovery and solve-version folders (no Nuke needed)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import paths  # noqa: E402


class VersionFolderTests(unittest.TestCase):
    def test_next_version_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "AutoLens"
            root.mkdir()
            self.assertEqual(paths.next_version_folder(root), root / "v2")
            (root / "v2").mkdir()
            (root / "v3").mkdir()
            self.assertEqual(paths.next_version_folder(root), root / "v4")
            # Pointing at a version folder continues from it, skipping ones that exist.
            self.assertEqual(paths.next_version_folder(root / "v2"), root / "v4")
            self.assertEqual(paths.next_version_folder(root / "V7"), root / "v8")


class ResultTests(unittest.TestCase):
    def test_find_result_nk(self):
        self.assertIsNone(paths.find_result_nk(""))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(paths.find_result_nk(folder / "missing"))
            self.assertFalse(paths.output_has_result(folder))
            older = folder / "AutoLens_v07.nk"
            older.write_text("x")
            self.assertEqual(paths.find_result_nk(folder), older)
            newer = folder / "AutoLens_v09.nk"
            newer.write_text("x")
            os.utime(newer, (time.time() + 10, time.time() + 10))
            self.assertEqual(paths.find_result_nk(folder), newer)
            preferred = folder / "AutoLens_v08.nk"
            preferred.write_text("x")
            self.assertEqual(paths.find_result_nk(folder), preferred)
            self.assertTrue(paths.output_has_result(folder))

    def test_engine_path(self):
        self.assertTrue(paths.engine_path().is_file())


if __name__ == "__main__":
    unittest.main()
