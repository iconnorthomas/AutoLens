"""autolens.surface_guides: pure helpers (Roto shape sampling, JSON writes) without Nuke."""
import json
import math
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

_nuke = sys.modules.setdefault("nuke", types.ModuleType("nuke"))
sys.modules.setdefault("nuke.rotopaint", types.ModuleType("nuke.rotopaint"))
if not hasattr(_nuke, "rotopaint"):
    _nuke.rotopaint = sys.modules["nuke.rotopaint"]

from autolens import surface_guides  # noqa: E402


class _Curve:
    def __init__(self, fn):
        self._fn = fn

    def getPoint(self, t):
        return self._fn(t)


class _Shape:
    """Stands in for rotopaint.Shape: len() is the CV count, evaluate() the outline."""

    def __init__(self, cvs, fn=None):
        self._cvs = cvs
        self._fn = fn

    def __len__(self):
        return self._cvs

    def evaluate(self, _index, _frame):
        if self._fn is None:
            raise RuntimeError("Shape has no curve")
        return _Curve(self._fn)


def _square(t):
    # Unit-square outline of side 100, traversed once for t in [0, 1).
    side, s = divmod(t * 4.0, 1.0)
    return [(s * 100, 0), (100, s * 100), (100 - s * 100, 100), (0, 100 - s * 100)][int(side) % 4]


class SampleShapeTests(unittest.TestCase):
    def test_square_simplifies_to_its_corners(self):
        points = surface_guides._sample_roto_shape(_Shape(4, _square), 1)
        self.assertEqual(len(points), 4)
        for corner in ((0, 0), (100, 0), (100, 100), (0, 100)):
            self.assertTrue(any(math.hypot(x - corner[0], y - corner[1]) < 1e-6 for x, y in points),
                            (corner, points))

    def test_shape_that_cannot_evaluate_is_skipped(self):
        self.assertEqual(surface_guides._sample_roto_shape(_Shape(1), 1), [])

    def test_collinear_shape_is_skipped(self):
        line = _Shape(2, lambda t: (t * 100.0, 50.0))
        self.assertEqual(surface_guides._sample_roto_shape(line, 1), [])


class AtomicJsonTests(unittest.TestCase):
    def test_write_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "autolens_surface_guides.json"
            path.write_text("{ half-written")
            surface_guides._write_json_atomic(path, {"surfaces": [1, 2]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"surfaces": [1, 2]})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [path.name])


if __name__ == "__main__":
    unittest.main()
