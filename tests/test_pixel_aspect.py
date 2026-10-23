"""autolens.pixel_aspect: square-pixel solve proxies, with a tiny fake of the Nuke API."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

_nuke = sys.modules.setdefault("nuke", types.ModuleType("nuke"))
sys.modules.setdefault("nuke.rotopaint", types.ModuleType("nuke.rotopaint"))
if not hasattr(_nuke, "rotopaint"):
    _nuke.rotopaint = sys.modules["nuke.rotopaint"]

from autolens import pixel_aspect  # noqa: E402


class _Knob:
    def __init__(self, value=None):
        self._value = value

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value


class _Node:
    def __init__(self, cls, knobs=None):
        self._cls = cls
        self._knobs = {k: _Knob(v) for k, v in (knobs or {}).items()}

    def Class(self):
        return self._cls

    def knob(self, name):
        return self._knobs.setdefault(name, _Knob())

    def __getitem__(self, name):
        return self.knob(name)

    def setInput(self, index, node):
        pass

    def name(self):
        return self._cls + "1"


class _Format:
    def __init__(self, pa):
        self._pa = pa

    def pixelAspect(self):
        return self._pa


class _Read(_Node):
    def __init__(self, first, last, pa=2.0):
        super().__init__("Read", {"first": first, "last": last})
        self._pa = pa

    def format(self):
        return _Format(self._pa)

    def width(self):
        return 100

    def height(self):
        return 50


class _FakeNuke:
    """Records the Write node and 'renders' empty PNGs for nuke.execute."""

    def __init__(self, pattern):
        self.pattern = pattern
        self.write = None

    def _make(self, cls):
        node = _Node(cls)
        if cls == "Write":
            self.write = node
        return node

    def execute(self, _name, first, last):
        file_pattern = self.write["file"].value()
        for frame in range(first, last + 1):
            Path(file_pattern % frame).write_bytes(b"png")


class ProxyTests(unittest.TestCase):
    def test_dimensions(self):
        self.assertEqual(pixel_aspect.square_pixel_proxy_dimensions(100, 50, 2.0, 1000), (200, 50))
        self.assertEqual(pixel_aspect.square_pixel_proxy_dimensions(2048, 858, 2.0, 2048), (2048, 429))

    def _render(self, fake, read, output):
        nodes = types.SimpleNamespace(Reformat=lambda: fake._make("Reformat"),
                                      Write=lambda: fake._make("Write"))
        with mock.patch.object(_nuke, "nodes", nodes, create=True), \
                mock.patch.object(_nuke, "execute", fake.execute, create=True), \
                mock.patch.object(_nuke, "delete", lambda node: None, create=True), \
                mock.patch.object(_nuke, "filename", lambda node: fake.pattern, create=True):
            return pixel_aspect.ensure_square_pixel_proxy(None, read, output, 1000)

    def test_rerender_removes_frames_left_by_another_plate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "AutoLens"
            first = self._render(_FakeNuke("shotA.%04d.exr"), _Read(1, 5), output)
            self.assertTrue(first["created"])
            proxy = Path(first["images_dir"])
            self.assertEqual(len(list(proxy.glob("frame.*.png"))), 5)

            # Same size and PAR, so the same cache folder, but a shorter plate.
            second = self._render(_FakeNuke("shotB.%04d.exr"), _Read(1, 3), output)
            self.assertTrue(second["created"])
            self.assertEqual(Path(second["images_dir"]), proxy)
            self.assertEqual(sorted(p.name for p in proxy.glob("frame.*.png")),
                             ["frame.000001.png", "frame.000002.png", "frame.000003.png"])

            # Unchanged source reuses the cache.
            third = self._render(_FakeNuke("shotB.%04d.exr"), _Read(1, 3), output)
            self.assertFalse(third["created"])

    def test_square_pixels_need_no_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._render(_FakeNuke("a.%04d.exr"), _Read(1, 3, pa=1.0), Path(tmp))
            self.assertIsNone(result["images_dir"])


if __name__ == "__main__":
    unittest.main()
