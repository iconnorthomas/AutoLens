"""autolens.startup: the script-load upgrade check and profiling (no Nuke needed)."""
import ast
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "AutoLens" / "python"))

from autolens import __version__, paths, startup  # noqa: E402


class FakeKnob:
    def __init__(self, value=""):
        self._value = value

    def value(self):
        return self._value


class FakeNode:
    def __init__(self, knobs):
        self._knobs = dict(knobs)

    def knob(self, name):
        return self._knobs.get(name)


def controller(version=__version__, extra=(), missing=()):
    knobs = {"autolens_output": FakeKnob(), "autolens_open": FakeKnob()}
    for name in startup.RUNTIME_KNOBS:
        knobs[name] = FakeKnob()
    if version is not None:
        knobs[startup.VERSION_KNOB] = FakeKnob(version)
    for name in extra:
        knobs[name] = FakeKnob()
    for name in missing:
        knobs.pop(name, None)
    return FakeNode(knobs)


class UpgradeCheckTests(unittest.TestCase):
    def test_current_node_is_skipped(self):
        self.assertFalse(startup.needs_upgrade(controller()))

    def test_unmarked_node_from_older_plugin_upgrades(self):
        self.assertTrue(startup.needs_upgrade(controller(version=None)))
        self.assertTrue(startup.needs_upgrade(controller(version="")))

    def test_other_versions_upgrade(self):
        self.assertTrue(startup.needs_upgrade(controller(version="0.10.29")))
        self.assertTrue(startup.needs_upgrade(controller(version="99.0.0")))
        self.assertTrue(startup.needs_upgrade(controller(version="0.10.29"), current="0.10.30"))
        self.assertFalse(startup.needs_upgrade(controller(version=" 1.2.3 "), current="1.2.3"))

    def test_missing_runtime_knob_upgrades(self):
        for name in startup.RUNTIME_KNOBS:
            self.assertTrue(startup.needs_upgrade(controller(missing=[name])), name)

    def test_leftover_legacy_knob_upgrades(self):
        for name in startup.LEGACY_KNOBS:
            self.assertTrue(startup.needs_upgrade(controller(extra=[name])), name)

    def test_broken_knob_lookup_counts_as_missing(self):
        class Broken(FakeNode):
            def knob(self, name):
                raise RuntimeError("deleted node")

        self.assertFalse(startup.is_controller(Broken({})))
        self.assertEqual(startup.stored_version(Broken({})), "")
        self.assertTrue(startup.needs_upgrade(Broken({})))

    def test_controllers_needing_upgrade(self):
        current = controller()
        old = controller(version="0.10.20")
        other_group = FakeNode({"label": FakeKnob()})
        found = startup.controllers_needing_upgrade([current, old, other_group])
        self.assertEqual(found, [old])
        self.assertEqual(startup.controllers_needing_upgrade(None), [])

    def test_is_controller(self):
        self.assertTrue(startup.is_controller(controller()))
        self.assertFalse(startup.is_controller(FakeNode({"autolens_output": FakeKnob()})))


class EnvFlagTests(unittest.TestCase):
    def test_profile_flag(self):
        self.assertFalse(startup.profiling_enabled({}))
        self.assertFalse(startup.profiling_enabled({"AUTOLENS_PROFILE": "0"}))
        for value in ("1", "true", "YES", " on "):
            self.assertTrue(startup.profiling_enabled({"AUTOLENS_PROFILE": value}), value)

    def test_full_refresh_flag(self):
        self.assertFalse(startup.full_refresh_on_load({}))
        self.assertTrue(startup.full_refresh_on_load({"AUTOLENS_FULL_REFRESH_ON_LOAD": "1"}))

    def test_defer_preview_defaults_on(self):
        self.assertTrue(startup.defer_initial_preview({}))
        self.assertTrue(startup.defer_initial_preview({"AUTOLENS_DEFER_PREVIEW": "1"}))
        for value in ("0", "false", "off"):
            self.assertFalse(startup.defer_initial_preview({"AUTOLENS_DEFER_PREVIEW": value}), value)


class ProfilingTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("AUTOLENS_PROFILE")
        del startup.TIMINGS[:]

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("AUTOLENS_PROFILE", None)
        else:
            os.environ["AUTOLENS_PROFILE"] = self._saved
        del startup.TIMINGS[:]

    def test_timed_is_silent_when_off(self):
        os.environ.pop("AUTOLENS_PROFILE", None)
        out = io.StringIO()
        with redirect_stdout(out):
            with startup.timed("step"):
                pass
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(startup.TIMINGS, [])

    def test_timed_records_and_prints_when_on(self):
        os.environ["AUTOLENS_PROFILE"] = "1"
        out = io.StringIO()
        with redirect_stdout(out):
            with startup.timed("step"):
                pass
            listed = startup.report()
        self.assertEqual([label for label, _ in startup.TIMINGS], ["step"])
        self.assertEqual(len(listed), 1)
        self.assertIn("[AutoLens profile] step:", out.getvalue())

    def test_timed_records_even_when_block_raises(self):
        os.environ["AUTOLENS_PROFILE"] = "1"
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                with startup.timed("failing"):
                    raise ValueError("x")
        self.assertEqual(startup.TIMINGS[0][0], "failing")


class CheapStartupTests(unittest.TestCase):
    def test_startup_module_does_not_import_nuke(self):
        source = (ROOT / "AutoLens" / "python" / "autolens" / "startup.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                self.assertNotIn("nuke", [alias.name for alias in node.names])
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "nuke")

    def test_menu_does_not_import_controller_module_at_launch(self):
        source = (ROOT / "AutoLens" / "menu.py").read_text(encoding="utf-8")
        top_level = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                top_level.add(node.module or "")
        for heavy in ("autolens.node", "autolens.dialog", "autolens.panel",
                      "autolens.surface_guides", "autolens.tracking_masks", "autolens.importer"):
            self.assertNotIn(heavy, top_level)

    def test_node_module_keeps_heavy_imports_lazy(self):
        source = (ROOT / "AutoLens" / "python" / "autolens" / "node.py").read_text(encoding="utf-8")
        top_level = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.ImportFrom) and node.level:
                top_level.add(node.module or "")
        self.assertFalse(
            top_level & {"dialog", "panel", "surface_guides", "tracking_masks", "importer"},
            top_level,
        )


class ScriptLoadCallbackTests(unittest.TestCase):
    """upgrade_on_script_load() with stand-ins for `nuke` and autolens.node."""

    def run_callback(self, groups, environ=None):
        import types
        from unittest import mock

        import autolens

        fake_nuke = types.ModuleType("nuke")
        fake_nuke.allNodes = lambda cls=None: list(groups)
        calls = []
        fake_node_module = types.ModuleType("autolens.node")
        fake_node_module.upgrade_nodes = lambda nodes, refresh=False: calls.append((list(nodes), refresh))

        env = {k: v for k, v in os.environ.items() if not k.startswith("AUTOLENS_")}
        env.update(environ or {})
        with mock.patch.dict(sys.modules, {"nuke": fake_nuke, "autolens.node": fake_node_module}), \
                mock.patch.object(autolens, "node", fake_node_module, create=True), \
                mock.patch.dict(os.environ, env, clear=True):
            startup.upgrade_on_script_load()
        return calls

    def test_current_script_does_not_load_controller_module(self):
        self.assertEqual(self.run_callback([controller(), FakeNode({})]), [])

    def test_old_nodes_get_knob_upgrade_without_folder_scan(self):
        old = controller(version="0.10.1")
        self.assertEqual(self.run_callback([controller(), old]), [([old], False)])

    def test_full_refresh_env_restores_old_behaviour(self):
        current = controller()
        calls = self.run_callback([current, FakeNode({})], {"AUTOLENS_FULL_REFRESH_ON_LOAD": "1"})
        self.assertEqual(calls, [([current], True)])


class CountImageFilesTests(unittest.TestCase):
    def test_counts_matching_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("a.0001.exr", "a.0002.EXR", "b.jpg", "notes.txt"):
                (folder / name).write_text("x")
            (folder / "sub.exr").mkdir()
            self.assertEqual(paths.count_image_files(folder, {".exr", ".jpg"}), 3)
            self.assertEqual(paths.count_image_files(folder / "missing", {".exr"}), 0)


if __name__ == "__main__":
    unittest.main()
