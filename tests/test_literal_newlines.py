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


if __name__ == "__main__":
    unittest.main()
