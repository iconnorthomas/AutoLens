"""install_autolens.py: subprocess helpers and the plugin copy (no network, conda or Nuke)."""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

INSTALLER = os.path.join(os.path.dirname(__file__), "..", "install_autolens.py")
_spec = importlib.util.spec_from_file_location("install_autolens", INSTALLER)
installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(installer)


class _InstallerTestCase(unittest.TestCase):
    """Point the installer's log and report files at a temp folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        logs = self.tmp / "logs"
        for name, value in (
            ("TARGET", logs),
            ("LIVE_LOG", logs / "live.log"),
            ("REPORT_JSON", logs / "report.json"),
            ("REPORT_TXT", logs / "report.txt"),
        ):
            patcher = mock.patch.object(installer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()


class RunStreamTests(_InstallerTestCase):
    def test_utf8_and_undecodable_output(self):
        code = (
            "import sys;"
            "sys.stdout.buffer.write('\\u2501\\u2501 solved\\n'.encode('utf-8'));"
            "sys.stdout.buffer.write(b'\\xff\\xfe bytes\\n')"
        )
        rc, output = installer.run_stream([sys.executable, "-c", code])
        self.assertEqual(rc, 0, output)
        self.assertIn("━━ solved", output)
        self.assertIn("bytes", output)

    def test_timeout_kills_a_silent_child(self):
        started = time.monotonic()
        rc, output = installer.run_stream(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=1)
        self.assertEqual(rc, -1)
        self.assertIn("timed out", output)
        self.assertLess(time.monotonic() - started, 30)

    def test_run_capture_undecodable_output(self):
        rc, out, _err = installer.run_capture(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff')"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("ok"))


class InstallPluginTests(_InstallerTestCase):
    def _package(self):
        source = self.tmp / "download" / "AutoLens"
        (source / "python").mkdir(parents=True)
        (source / "init.py").write_text("# new plugin\n")
        return source

    def test_upgrade_keeps_runtimes_and_registers_plugin(self):
        source = self._package()
        nuke_dir = self.tmp / ".nuke"
        target = nuke_dir / "AutoLens"
        (target / ".venv").mkdir(parents=True)
        (target / ".venv" / "marker").write_text("env")
        (target / "stale.py").write_text("old")

        installer.install_plugin(source, target, nuke_dir)

        self.assertEqual((target / "init.py").read_text(), "# new plugin\n")
        self.assertEqual((target / ".venv" / "marker").read_text(), "env")
        self.assertFalse((target / "stale.py").exists())
        self.assertIn("pluginAddPath('./AutoLens')", (nuke_dir / "init.py").read_text())
        self.assertFalse((nuke_dir / ".autolens_upgrade_preserve").exists())

    def test_package_unzipped_into_the_target(self):
        nuke_dir = self.tmp / ".nuke"
        target = nuke_dir / "AutoLens"
        target.mkdir(parents=True)
        (target / "init.py").write_text("# plugin\n")

        installer.install_plugin(target, target, nuke_dir)

        self.assertEqual((target / "init.py").read_text(), "# plugin\n")

    def test_package_inside_the_target_is_refused(self):
        nuke_dir = self.tmp / ".nuke"
        target = nuke_dir / "AutoLens"
        source = target / "AutoLens_NukeX_v0" / "AutoLens"
        source.mkdir(parents=True)
        (source / "init.py").write_text("# plugin\n")

        with self.assertRaises(RuntimeError):
            installer.install_plugin(source, target, nuke_dir)
        self.assertTrue((source / "init.py").exists())

    def test_recovers_runtimes_from_an_interrupted_upgrade(self):
        source = self._package()
        nuke_dir = self.tmp / ".nuke"
        target = nuke_dir / "AutoLens"
        parked = nuke_dir / ".autolens_upgrade_preserve" / ".colmap_env"
        parked.mkdir(parents=True)
        (parked / "marker").write_text("colmap")

        installer.install_plugin(source, target, nuke_dir)

        self.assertEqual((target / ".colmap_env" / "marker").read_text(), "colmap")


if __name__ == "__main__":
    unittest.main()
