"""autolens.importer: the text clean-up and node lookup that need no running Nuke."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

# importer.py imports nuke at module level; these tests only touch pure helpers.
sys.modules.setdefault("nuke", types.ModuleType("nuke"))

from autolens import importer  # noqa: E402


class _Node:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class FindPastedTests(unittest.TestCase):
    def test_redistort_does_not_match_its_input_dot(self):
        # The engine writes the Dot before the LensDistortion node.
        dot = _Node("AUTOLENS_REDISTORT_INPUT")
        lens = _Node("AUTOLENS_REDISTORT")
        pasted = [dot, lens]
        self.assertIs(importer._find_pasted(pasted, "AUTOLENS_REDISTORT"), lens)
        self.assertIs(importer._find_pasted(pasted, "AUTOLENS_REDISTORT_INPUT"), dot)

    def test_numeric_suffix_from_a_second_import(self):
        pasted = [_Node("AUTOLENS_REDISTORT_INPUT1"), _Node("AUTOLENS_REDISTORT1")]
        self.assertEqual(importer._find_pasted(pasted, "AUTOLENS_REDISTORT").name(),
                         "AUTOLENS_REDISTORT1")

    def test_missing(self):
        self.assertIsNone(importer._find_pasted([_Node("AUTOLENS_SCENE")], "AUTOLENS_CAMERA"))


class TextTests(unittest.TestCase):
    def test_strip_root_block(self):
        text = "Root {\n name x\n format {a b}\n}\nRead {\n file f\n}\n"
        self.assertEqual(importer._strip_root_block(text), "\nRead {\n file f\n}\n")
        self.assertEqual(importer._strip_root_block("Read {}\n"), "Read {}\n")

    def test_sanitize_lensdistortion(self):
        text = "LensDistortion2 {\n lensType Spherical\n projection Rectilinear\n name A\n}\n"
        self.assertEqual(importer._sanitize_lensdistortion_text(text),
                         "LensDistortion2 {\n name A\n}\n")


if __name__ == "__main__":
    unittest.main()
