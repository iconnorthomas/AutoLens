"""Build label shown next to the version, and the installer's git build info (no Nuke needed)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "AutoLens", "python"))
sys.path.insert(0, ROOT)

from autolens import build  # noqa: E402
import install_autolens  # noqa: E402


class BuildLabelTests(unittest.TestCase):
    def test_release_install_has_no_label(self):
        self.assertEqual(build.format_build_label({}), "")

    def test_pr_build(self):
        info = {"branch": "claude/x", "commit": "d9bf93a", "dirty": False, "pr": 8}
        self.assertEqual(build.format_build_label(info), "PR #8 · d9bf93a")

    def test_branch_without_pr_and_dirty_tree(self):
        info = {"branch": "chore/build-stamp", "commit": "abc1234", "dirty": True, "pr": None}
        self.assertEqual(build.format_build_label(info), "chore/build-stamp · abc1234+dirty")

    def test_load_build_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build.load_build_info(tmp), {})
            with open(os.path.join(tmp, build.BUILD_FILE), "w", encoding="utf-8") as f:
                json.dump({"commit": "abc1234", "pr": 3}, f)
            self.assertEqual(build.load_build_info(tmp), {"commit": "abc1234", "pr": 3})
            with open(os.path.join(tmp, build.BUILD_FILE), "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertEqual(build.load_build_info(tmp), {})


class InstallerBuildInfoTests(unittest.TestCase):
    @staticmethod
    def fake_git(answers):
        return lambda *args: answers.get(args)

    def test_git_checkout_with_pr(self):
        git = self.fake_git({
            ("rev-parse", "--short", "HEAD"): "d9bf93a",
            ("rev-parse", "--abbrev-ref", "HEAD"): "claude/workspace-playback-a72e95",
            ("status", "--porcelain", "--untracked-files=no"): "",
        })
        urls = []

        def fetch(url):
            urls.append(url)
            return [{"number": 8}]

        info = install_autolens.build_info_from_git(Path("."), git=git, fetch=fetch)
        self.assertEqual(info, {"branch": "claude/workspace-playback-a72e95", "commit": "d9bf93a",
                                "dirty": False, "pr": 8})
        self.assertIn("head=iconnorthomas:claude/workspace-playback-a72e95", urls[0])

    def test_not_a_git_checkout(self):
        self.assertEqual(install_autolens.build_info_from_git(Path("."), git=self.fake_git({})), {})

    def test_offline_or_no_pr(self):
        def offline(url):
            raise OSError("no network")

        self.assertIsNone(install_autolens.find_pr_number("main", fetch=offline))
        self.assertIsNone(install_autolens.find_pr_number("main", fetch=lambda url: []))
        self.assertIsNone(install_autolens.find_pr_number("HEAD", fetch=lambda url: [{"number": 1}]))


if __name__ == "__main__":
    unittest.main()
