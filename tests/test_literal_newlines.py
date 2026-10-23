"""Callback scripts, labels and OBJ files use real line breaks, not a literal backslash-n."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "AutoLens", "python"))
sys.path.insert(0, os.path.join(ROOT, "AutoLens", "engine"))
sys.modules.setdefault("nuke", types.ModuleType("nuke"))

from autolens import node  # noqa: E402

BROKEN = ("import nuke\\nimport autolens.node as _al\\n"
          "_al.autolens_knob_changed(nuke.thisNode(), nuke.thisKnob())")


class KnobChangedTests(unittest.TestCase):
    def test_new_script_compiles(self):
        script = node.knob_changed_script("")
        self.assertEqual(script, node.KNOB_CHANGED_COMMAND)
        compile(script, "<knobChanged>", "exec")

    def test_appends_after_existing_code(self):
        script = node.knob_changed_script("print('mine')  ")
        self.assertEqual(script, "print('mine')\n" + node.KNOB_CHANGED_COMMAND)
        compile(script, "<knobChanged>", "exec")

    def test_repairs_broken_callback_from_older_builds(self):
        script = node.knob_changed_script("print('mine')\\n" + BROKEN)
        self.assertNotIn("nuke\\nimport", script)
        self.assertIn(node.KNOB_CHANGED_COMMAND, script)

    def test_working_callback_left_alone(self):
        self.assertIsNone(node.knob_changed_script(node.KNOB_CHANGED_COMMAND))


class MeshfitObjTests(unittest.TestCase):
    def test_two_surfaces_read_back(self):
        import autolens_engine as engine

        surfaces = [
            {"classification": "wall", "surface": 1, "corners": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]},
            {"classification": "floor", "surface": 2, "corners": [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planes.obj"
            engine.write_meshfit_planes_obj(path, surfaces)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("\\n", text)
        lines = text.splitlines()
        self.assertEqual(sum(1 for l in lines if l.startswith("v ")), 8)
        self.assertEqual([l for l in lines if l.startswith("f ")], ["f 1 2 3 4", "f 5 6 7 8"])
        self.assertIn("g WALL_01", lines)


class FakeKnob(object):
    def __init__(self, node, name, value="", values=()):
        self.node, self._name, self._value, self._values = node, name, value, list(values)

    def name(self):
        return self._name

    def value(self):
        return self._value

    def values(self):
        return list(self._values)

    def setValue(self, value):
        self._value = value
        self.node.changed(self)

    def setValues(self, values):
        self._values = list(values)
        self.node.changed(self)


class FakeNode(object):
    """Fires knobChanged synchronously on every knob write, like Nuke."""

    def __init__(self):
        self.knobs = {
            "autolens_output_root": FakeKnob(self, "autolens_output_root", "/shots/a/AutoLens"),
            "autolens_version": FakeKnob(self, "autolens_version"),
            "autolens_output": FakeKnob(self, "autolens_output"),
        }
        self.callbacks = 0

    def fullName(self):
        return "AutoLens1"

    def knob(self, name):
        return self.knobs.get(name)

    def changed(self, knob):
        self.callbacks += 1
        if self.callbacks > 50:
            raise RuntimeError("knobChanged loop")
        node.autolens_knob_changed(self, knob)


class KnobChangedLoopTests(unittest.TestCase):
    def setUp(self):
        self.scheduled = []
        self._saved = (node.discover_solve_versions, node._next_solve_version,
                       node._schedule_version_ui_refresh, node.refresh_result_status,
                       node.refresh_create_items)
        node.discover_solve_versions = lambda root: ["v1", "v2"]
        node._next_solve_version = lambda root: "v3"
        node._schedule_version_ui_refresh = self.scheduled.append
        node.refresh_result_status = lambda n: None
        node.refresh_create_items = lambda n, force_rebuild=False: None

    def tearDown(self):
        (node.discover_solve_versions, node._next_solve_version,
         node._schedule_version_ui_refresh, node.refresh_result_status,
         node.refresh_create_items) = self._saved

    def test_own_knob_writes_do_not_retrigger(self):
        fake = FakeNode()
        node.autolens_knob_changed(fake, fake.knob("autolens_output_root"))
        self.assertEqual(fake.knob("autolens_version").value(), "Latest (v2)")
        self.assertEqual(len(self.scheduled), 1)

    def test_show_panel_twice_writes_nothing_the_second_time(self):
        fake = FakeNode()
        panel = FakeKnob(fake, "showPanel")
        node.autolens_knob_changed(fake, panel)
        writes = fake.callbacks
        node.autolens_knob_changed(fake, panel)
        self.assertEqual(fake.callbacks, writes)
        self.assertEqual(self.scheduled, [])


if __name__ == "__main__":
    unittest.main()
