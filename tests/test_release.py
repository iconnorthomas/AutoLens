"""scripts/release.py: the version guard and release checks."""
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import release  # noqa: E402

CHANGELOG = """# Changelog

## Unreleased

- New thing.

## 0.10.30 - 2026-09-13

- Guide frames.

## 0.10.29

- Workspace.
"""

RELEASED = CHANGELOG.replace("## Unreleased\n\n- New thing.\n",
                             "## Unreleased\n\n## 0.11.0 - 2026-10-23\n\n- New thing.\n")
INIT = '__version__ = "0.10.30"\nPANEL_ID = "x"\n'


def synced(version):
    return {
        "install_autolens.py": 'import os\n\nVERSION = "%s"\n' % version,
        "install_autolens.bat": ("@echo off\r\ntitle AutoLens NukeX v%s Installer\r\n"
                                 "echo AutoLens NukeX v%s Installer\r\n" % (version, version)),
        "AutoLens/engine/autolens_engine.py": 'import os\n\nENGINE_VERSION = "%s"\n' % version,
        "README.md": "# AutoLens\n\nCurrent build: **v%s**\n" % version,
    }


class ParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(release.parse("v0.10.30"), (0, 10, 30))
        for bad in ("0.10", "0.10.30-beta", "latest", ""):
            with self.assertRaises(release.CheckError):
                release.parse(bad)

    def test_changelog(self):
        self.assertEqual(release.unreleased_notes(CHANGELOG), "- New thing.")
        self.assertEqual(release.changelog_section(CHANGELOG, "0.10.30"), "- Guide frames.")
        self.assertEqual(release.changelog_section(CHANGELOG, "0.10.29"), "- Workspace.")
        self.assertIsNone(release.changelog_section(CHANGELOG, "0.10.3"))   # no partial matches
        self.assertEqual(release.unreleased_notes(RELEASED), "")
        self.assertEqual(release.changelog_section(RELEASED, "0.11.0"), "- New thing.")

    def test_release_tags_sorted_numerically(self):
        self.assertEqual(release.release_tags(["v0.9.0", "v0.10.30", "junk", "v0.10.4"]),
                         ["v0.10.30", "v0.10.4", "v0.9.0"])

    def test_the_repo_itself_is_in_sync(self):
        version = release.version_in(release.read(release.VERSION_FILE))
        self.assertEqual(release.sync_problems(version, release.synced_files()), [])


class PullRequestTests(unittest.TestCase):
    def test_branches_that_dont_touch_the_version_pass(self):
        self.assertEqual(release.check_pr("0.10.30", "0.10.30", [], CHANGELOG), [])

    def test_a_proper_bump_passes(self):
        self.assertEqual(release.check_pr("0.11.0", "0.10.30", ["v0.10.30"], RELEASED,
                                          synced("0.11.0")), [])

    def test_installer_must_match(self):
        problems = release.check_pr("0.11.0", "0.10.30", [], RELEASED, synced("0.10.30"))
        self.assertTrue(any("install_autolens.py says 0.10.30" in p for p in problems), problems)
        self.assertTrue(any("install_autolens.bat says 0.10.30" in p for p in problems), problems)
        # Even a branch that leaves __version__ alone can't let the installer drift.
        self.assertTrue(release.check_pr("0.10.30", "0.10.30", [], CHANGELOG, synced("0.10.29")))

    def test_another_branch_got_there_first(self):
        problems = release.check_pr("0.11.0", "0.11.1", ["v0.11.1"], RELEASED)
        self.assertTrue(any("higher than main's 0.11.1" in p for p in problems), problems)

    def test_must_beat_posted_releases(self):
        problems = release.check_pr("0.11.0", "0.10.30", ["v0.11.0", "v0.11.5"], RELEASED)
        self.assertTrue(any("latest posted release v0.11.5" in p for p in problems), problems)
        self.assertTrue(any("already posted" in p for p in problems), problems)

    def test_bump_needs_its_changelog_section_and_no_leftovers(self):
        problems = release.check_pr("0.11.0", "0.10.30", [], CHANGELOG)
        self.assertTrue(any('no "## 0.11.0' in p for p in problems), problems)
        self.assertTrue(any("Unreleased" in p for p in problems), problems)

    def test_lowering_or_garbage_fails(self):
        self.assertTrue(release.check_pr("0.10.29", "0.10.30", [], RELEASED))
        self.assertTrue(release.check_pr("0.11", "0.10.30", [], RELEASED))


class ReleaseTests(unittest.TestCase):
    def test_good_release(self):
        self.assertEqual(release.check_release("v0.11.0", "0.11.0", ["v0.10.30", "v0.11.0"],
                                               RELEASED, on_main=True), [])

    def test_tag_must_match_the_code(self):
        problems = release.check_release("v0.11.1", "0.11.0", [], RELEASED, on_main=True)
        self.assertTrue(any("doesn't match __version__ 0.11.0" in p for p in problems), problems)

    def test_must_be_on_main_newer_and_documented(self):
        problems = release.check_release("v0.10.30", "0.10.30", ["v0.11.0", "v0.10.30"],
                                         RELEASED, on_main=False)
        self.assertTrue(any("isn't on main" in p for p in problems), problems)
        self.assertTrue(any("isn't newer than" in p for p in problems), problems)
        problems = release.check_release("v1.0.0", "1.0.0", [], RELEASED, on_main=True)
        self.assertTrue(any("no notes" in p for p in problems), problems)


class PackageTests(unittest.TestCase):
    def test_package_of_this_repo(self):
        version = release.version_in(release.read(release.VERSION_FILE))
        with tempfile.TemporaryDirectory() as tmp:
            path = release.build_package(version, tmp)
            self.assertEqual(os.path.basename(path), "AutoLens_NukeX_v%s.zip" % version)
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                init = zf.read(release.package_prefix(version) + release.VERSION_FILE)
            self.assertEqual(release.check_package(names, init.decode("utf-8"), version), [])

    def test_bad_packages(self):
        prefix = release.package_prefix("0.11.0")
        problems = release.check_package([prefix + "README.md", "stray.txt",
                                          prefix + "AutoLens/x.pyc"], INIT, "0.11.0")
        self.assertTrue(any("missing" in p for p in problems), problems)
        self.assertTrue(any("outside" in p for p in problems), problems)
        self.assertTrue(any("compiled" in p for p in problems), problems)
        self.assertTrue(any("carries AutoLens 0.10.30" in p for p in problems), problems)


class BumpTests(unittest.TestCase):
    def test_bump(self):
        init, log, files = release.bump(INIT, CHANGELOG, "0.11.0", today="2026-10-23",
                                        files=synced("0.10.30"))
        self.assertIn('__version__ = "0.11.0"', init)
        self.assertEqual(log, RELEASED)
        self.assertEqual(files, synced("0.11.0"))
        self.assertEqual(release.check_pr("0.11.0", "0.10.30", ["v0.10.30"], log, files), [])

    def test_bump_refuses_mistakes(self):
        for version, log in (("0.10.30", CHANGELOG),         # not higher
                             ("0.11", CHANGELOG),             # not X.Y.Z
                             ("0.11.0", RELEASED),            # already has a section
                             ("0.11.0", CHANGELOG.replace("- New thing.\n", ""))):  # nothing new
            with self.assertRaises(release.CheckError, msg=version):
                release.bump(INIT, log, version)


if __name__ == "__main__":
    unittest.main()
