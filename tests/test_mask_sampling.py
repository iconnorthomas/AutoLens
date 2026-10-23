"""Mask overlay sampling reads the requested frame, so animated masks follow the plate."""
import importlib
import os
import sys
import types
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "AutoLens", "python"))


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Anything(object):
    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()


def _import_tracking_masks():
    saved = {name: sys.modules.get(name) for name in
             ("nuke", "nuke.rotopaint", "PySide6", "PySide6.QtCore", "PySide6.QtGui",
              "PySide6.QtWidgets")}
    nuke = _stub("nuke")
    nuke.rotopaint = _stub("nuke.rotopaint")
    qt = _stub("PySide6", QtCore=_Anything(), QtGui=_Anything(), QtWidgets=_Anything())
    _stub("PySide6.QtCore")
    _stub("PySide6.QtGui")
    _stub("PySide6.QtWidgets")
    try:
        return importlib.import_module("autolens.tracking_masks")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        del qt


class FrameAwareNode(object):
    """sample(c, x, y, dx, dy, frame) like Nuke 13+; alpha is 1 only on frame 12."""

    def __init__(self, accepts_frame=True):
        self.accepts_frame = accepts_frame
        self.calls = 0

    def sample(self, channel, x, y, dx=None, dy=None, frame=None):
        self.calls += 1
        if frame is None:
            return 0.0  # whatever frame the viewer last evaluated
        if not self.accepts_frame:
            raise TypeError("sample() takes at most 5 arguments")
        return 1.0 if frame == 12 else 0.0


class ResolveSamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.masks = _import_tracking_masks()
        except Exception as exc:  # pragma: no cover - depends on stub coverage
            raise unittest.SkipTest(f"tracking_masks not importable without Nuke: {exc}")

    def test_samples_the_requested_frame(self):
        node = FrameAwareNode()
        self.assertEqual(self.masks.resolve_sampler(node, "rgba.alpha", 12)(5, 5), 1.0)
        self.assertEqual(self.masks.resolve_sampler(node, "rgba.alpha", 13)(5, 5), 0.0)

    def test_falls_back_when_frame_is_not_accepted(self):
        node = FrameAwareNode(accepts_frame=False)
        sampler = self.masks.resolve_sampler(node, "rgba.alpha", 12)
        node.calls = 0
        for _ in range(10):
            sampler(1, 1)
        self.assertEqual(node.calls, 10)  # no failed attempts per pixel

    def test_no_sample_api(self):
        with self.assertRaises(RuntimeError):
            self.masks.resolve_sampler(object(), "rgba.alpha", 1)


if __name__ == "__main__":
    unittest.main()
