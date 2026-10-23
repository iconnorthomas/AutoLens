"""autolens.plate_cache and autolens.plate_source with fake sources and nodes (no Qt, no Nuke)."""
import contextlib
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import plate_cache as pc  # noqa: E402
from autolens import plate_source as ps  # noqa: E402


class FakeSource:
    """Renders frames by writing small files; signature = graph + per-frame tag."""

    def __init__(self, frames, graph="g1", size=(64, 36), direct=None, fail=()):
        self._frames = list(frames)
        self.graph = graph
        self.size = size
        self.direct = dict(direct or {})   # frame -> path
        self.fail = set(fail)
        self.tags = {}                     # frame -> extra signature
        self.rendered = []
        self.refreshes = 0

    def frames(self):
        return list(self._frames)

    def cache_size(self):
        return self.size

    def key(self):
        return ["fake", self.graph]

    def signature(self, frame):
        return [self.graph, self.tags.get(frame, 0)]

    def direct_path(self, frame):
        return self.direct.get(frame)

    @contextlib.contextmanager
    def renderer(self, directory):
        def render(frames):
            out = {}
            for f in frames:
                if f in self.fail:
                    raise RuntimeError("boom %d" % f)
                path = os.path.join(directory, "frame.%04d.jpg" % f)
                with open(path, "w") as fh:
                    fh.write("frame %d %s" % (f, self.graph))
                self.rendered.append(f)
                out[f] = path
            return out
        yield render

    def refresh(self):
        self.refreshes += 1


class Clock:
    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def manager(self, source, **kw):
        kw.setdefault("cache_root", self.root)
        kw.setdefault("clock", Clock())
        return pc.PlateCacheManager(source, **kw)

    def test_opening_renders_nothing(self):
        source = FakeSource(range(1, 11))
        mgr = self.manager(source)
        self.assertEqual(source.rendered, [])
        self.assertFalse(mgr.done)
        self.assertIsNone(mgr.path(1))
        self.assertEqual(mgr.cached_frames(), [])

    def test_priority_is_nearest_the_playhead_forward_first(self):
        mgr = self.manager(FakeSource(range(1, 11)))
        mgr.request(5)
        self.assertEqual(mgr.pending_frames(), [5, 6, 4, 7, 3, 8, 2, 9, 1, 10])
        mgr.request(10)
        self.assertEqual(mgr.pending_frames()[:3], [10, 9, 8])

    def test_step_respects_the_time_budget(self):
        source = FakeSource(range(1, 21))
        # Every clock read advances 10 ms: a frame "takes" 10-20 ms.
        mgr = self.manager(source, clock=Clock(step=0.01))
        mgr.request(10)
        got = mgr.step(0.06)
        self.assertGreaterEqual(len(got), 1)
        self.assertLess(len(got), 20)
        self.assertEqual(got[:2], [10, 11])
        self.assertEqual(sorted(got), mgr.cached_frames())
        # A tiny budget still renders one frame per call.
        self.assertEqual(len(mgr.step(0.0)), 1)

    def test_rendered_frames_survive_reopening(self):
        source = FakeSource(range(1, 6))
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        self.assertEqual(sorted(source.rendered), [1, 2, 3, 4, 5])
        self.assertTrue(os.path.isfile(os.path.join(mgr.directory, pc.MANIFEST)))
        self.assertFalse(os.listdir(os.path.join(mgr.directory, "_render")))

        again = FakeSource(range(1, 6))
        reopened = self.manager(again)
        self.assertEqual(reopened.directory, mgr.directory)
        self.assertTrue(reopened.done)
        self.assertEqual(again.rendered, [])
        self.assertEqual(reopened.path(3), mgr.path(3))

    def test_signature_change_invalidates_only_that_frame(self):
        source = FakeSource(range(1, 6))
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        source.tags[3] = 1              # e.g. that frame's source file changed
        stale = mgr.refresh()
        self.assertEqual(stale, [3])
        self.assertEqual(mgr.pending_frames(), [3])
        source.rendered = []
        mgr.step(1.0)
        self.assertEqual(source.rendered, [3])
        self.assertTrue(mgr.done)

    def test_cache_file_change_invalidates(self):
        source = FakeSource(range(1, 4))
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        with open(mgr.path(2), "a") as fh:
            fh.write("tampered")
        reopened = self.manager(FakeSource(range(1, 4)))
        self.assertEqual(reopened.pending_frames(), [2])

    def test_graph_change_switches_folder_and_back(self):
        source = FakeSource(range(1, 4), graph="g1")
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        first_dir = mgr.directory

        source.graph = "g2"             # e.g. a Grade added above AutoLens
        stale = mgr.refresh()
        self.assertEqual(stale, [1, 2, 3])
        self.assertNotEqual(mgr.directory, first_dir)
        while not mgr.done:
            mgr.step(1.0)

        source.graph = "g1"             # undo: the first renders are still valid
        source.rendered = []
        self.assertEqual(sorted(mgr.refresh()), [1, 2, 3])
        self.assertEqual(mgr.directory, first_dir)
        self.assertTrue(mgr.done)
        self.assertEqual(source.rendered, [])

    def test_direct_frames_are_never_rendered(self):
        with tempfile.TemporaryDirectory() as plates:
            direct = {}
            for f in (1, 2, 3):
                direct[f] = os.path.join(plates, "plate.%04d.jpg" % f)
                open(direct[f], "w").close()
            source = FakeSource(range(1, 5), direct=direct)
            mgr = self.manager(source)
            self.assertEqual(mgr.path(2), direct[2])
            self.assertEqual(mgr.pending_frames(), [4])
            mgr.step(1.0)
            self.assertEqual(source.rendered, [4])
            self.assertFalse(mgr.direct)    # frame 4 is a render
            mgr.invalidate()
            self.assertEqual(mgr.path(1), direct[1])
            self.assertEqual(mgr.pending_frames(), [4])

            all_direct = self.manager(FakeSource([1, 2, 3], direct=direct, graph="d"))
            self.assertTrue(all_direct.direct and all_direct.done)

    def test_failed_frames_are_skipped_until_refresh(self):
        source = FakeSource(range(1, 4), fail={2})
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        self.assertEqual(mgr.cached_frames(), [1, 3])
        self.assertIn("boom 2", mgr.failed[2])
        source.fail = set()
        mgr.refresh()
        self.assertEqual(mgr.pending_frames(), [2])
        mgr.step(1.0)
        self.assertEqual(mgr.failed, {})
        self.assertEqual(mgr.cached_frames(), [1, 2, 3])

    def test_invalidate_rerenders(self):
        source = FakeSource(range(1, 4))
        mgr = self.manager(source)
        while not mgr.done:
            mgr.step(1.0)
        mgr.invalidate([1])
        self.assertEqual(mgr.pending_frames(), [1])
        mgr.invalidate()
        self.assertEqual(sorted(mgr.pending_frames()), [1, 2, 3])
        reopened = self.manager(FakeSource(range(1, 4)))
        self.assertEqual(sorted(reopened.pending_frames()), [1, 2, 3])

    def test_damaged_manifest_falls_back_to_backup(self):
        source = FakeSource(range(1, 4))
        mgr = self.manager(source, clock=Clock(step=0.01))
        mgr.step(0.0)                   # frame 1 -> manifest
        while not mgr.done:
            mgr.step(1.0)               # second save: first manifest becomes .bak
        manifest = os.path.join(mgr.directory, pc.MANIFEST)
        with open(manifest, "w") as fh:
            fh.write("{not json")
        reopened = self.manager(FakeSource(range(1, 4)))
        self.assertIn(1, reopened.cached_frames())
        self.assertTrue(reopened.warnings)

        os.remove(manifest)
        os.remove(manifest + ".bak")
        empty = self.manager(FakeSource(range(1, 4)))
        self.assertEqual(empty.cached_frames(), [])

    def test_cache_size_is_part_of_the_folder(self):
        a = self.manager(FakeSource([1], size=(64, 36)))
        b = self.manager(FakeSource([1], size=(128, 72)))
        self.assertNotEqual(a.directory, b.directory)

    def test_atomic_json(self):
        path = os.path.join(self.root, "x.json")
        pc.write_json_atomic(path, {"a": 1})
        pc.write_json_atomic(path, {"a": 2}, backup=True)
        self.assertEqual(pc.read_json(path), {"a": 2})
        self.assertEqual(pc.read_json(path + ".bak"), {"a": 1})
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [])
        self.assertIsNone(pc.read_json(os.path.join(self.root, "missing.json")))
        with open(path, "w") as fh:
            json.dump([1, 2], fh)
        self.assertIsNone(pc.read_json(path))   # not an object

    def test_default_cache_root(self):
        self.assertEqual(
            pc.default_cache_root({pc.CACHE_DIR_ENV: "/x/cache"}, "win32", "/home"),
            "/x/cache",
        )
        self.assertEqual(
            pc.default_cache_root({"LOCALAPPDATA": "C:/Local"}, "win32", "C:/Users/u"),
            os.path.join("C:/Local", "AutoLens", "cache"),
        )
        self.assertEqual(
            pc.default_cache_root({}, "linux", "/home/u"),
            os.path.join("/home/u", ".cache", "autolens"),
        )
        self.assertEqual(
            pc.default_cache_root({}, "darwin", "/Users/u"),
            os.path.join("/Users/u", "Library", "Caches", "AutoLens"),
        )
        self.assertNotIn(
            tempfile.gettempdir(),
            pc.default_cache_root({"LOCALAPPDATA": "C:/Local"}, "win32", "C:/Users/u"),
        )


# ---------------------------------------------------------------------------
# plate_source with fake Nuke nodes


class Knob:
    def __init__(self, value):
        self._v = value

    def value(self):
        return self._v

    def setValue(self, v):
        self._v = v


class Node:
    def __init__(self, cls, inputs=(), name=None, **knobs):
        self.cls = cls
        self._inputs = list(inputs)
        self._name = name or cls + "1"
        self._knobs = {k: Knob(v) for k, v in knobs.items()}

    def Class(self):
        return self.cls

    def fullName(self):
        return self._name

    def inputs(self):
        return len(self._inputs)

    def input(self, i):
        return self._inputs[i] if i < len(self._inputs) else None

    def knob(self, name):
        return self._knobs.get(name)

    def knobs(self):
        return dict(self._knobs)


class FakeNuke:
    def __init__(self):
        self._root = Node("Root", colorManagement="Nuke", format="HD_1080")

    def root(self):
        return self._root


def read_node(pattern, first=1, last=10, **knobs):
    return Node("Read", file=pattern, first=first, last=last, **knobs)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name.replace("\\", "/")
        for f in range(1, 11):
            open(os.path.join(self.dir, "plate.%04d.jpg" % f), "w").close()
        self.pattern = self.dir + "/plate.%04d.jpg"

    def tearDown(self):
        self._tmp.cleanup()

    def source(self, node, exts=("jpg", "png")):
        return ps.NukePlateSource(node, range(1, 11), 1920, 1080, max_size=1024,
                                  readable_exts=exts, nuke=FakeNuke())

    def test_cache_size(self):
        self.assertEqual(ps.cache_size(4096, 2160, 2048), (2048, 1080))
        self.assertEqual(ps.cache_size(1920, 1080, 2048), (1920, 1080))
        self.assertEqual(ps.cache_size(1080, 1920, 1024), (576, 1024))

    def test_format_pattern_and_regex(self):
        self.assertEqual(ps.format_pattern("/a/p.%04d.exr", 7), "/a/p.0007.exr")
        self.assertEqual(ps.format_pattern("/a/p.####.exr", 12), "/a/p.0012.exr")
        self.assertEqual(ps.format_pattern("/a/p.%d.exr", 7), "/a/p.7.exr")
        rx = ps.pattern_regex("/a/p.%04d.exr")
        self.assertTrue(rx.match("p.0001.exr") and rx.match("P.-001.EXR"))
        self.assertFalse(rx.match("p.0001.jpg"))

    def test_read_file_frame(self):
        r = read_node(self.pattern, 1001, 1010)
        self.assertEqual(ps.read_file_frame(r, 1005), 1005)
        self.assertEqual(ps.read_file_frame(r, 990), 1001)          # default hold
        r = read_node(self.pattern, 1, 10, before="black", after="loop")
        self.assertIsNone(ps.read_file_frame(r, 0))
        self.assertEqual(ps.read_file_frame(r, 12), 2)
        r = read_node(self.pattern, 1, 4, after="bounce")
        self.assertEqual([ps.read_file_frame(r, t) for t in (5, 6, 7, 8)], [3, 2, 1, 2])
        r = read_node(self.pattern, 1, 10, frame="100", frame_mode="start at")
        self.assertEqual(ps.read_file_frame(r, 103), 4)
        r = read_node(self.pattern, 1, 10, frame="2", frame_mode="offset")
        self.assertEqual(ps.read_file_frame(r, 5), 3)
        r = read_node(self.pattern, 1, 10, frame="frame*2", frame_mode="expression")
        self.assertIs(ps.read_file_frame(r, 5), False)

    def test_split_script(self):
        self.assertEqual(
            ps.split_script('file "/a b/c.exr"\nlabel {two words}\nsize 3'),
            [("file", '"/a b/c.exr"'), ("label", "{two words}"), ("size", "3")],
        )

    def test_graph_signature_ignores_names_and_positions(self):
        nuke = FakeNuke()
        a = Node("Grade", [read_node(self.pattern)], name="Grade1", white=1.2, xpos=10)
        b = Node("Grade", [read_node(self.pattern)], name="Grade7", white=1.2, xpos=900)
        c = Node("Grade", [read_node(self.pattern)], white=1.3)
        self.assertEqual(ps.graph_signature(a, nuke), ps.graph_signature(b, nuke))
        self.assertNotEqual(ps.graph_signature(a, nuke), ps.graph_signature(c, nuke))
        other_root = FakeNuke()
        other_root._root._knobs["OCIO_config"] = Knob("aces_1.2")
        self.assertNotEqual(ps.graph_signature(a, nuke), ps.graph_signature(a, other_root))

    def test_direct_read_through_dots(self):
        read = read_node(self.pattern)
        src = self.source(Node("Dot", [read]))
        self.assertTrue(src.direct)
        self.assertEqual(src.direct_path(3), self.pattern.replace("%04d", "0003"))
        # Missing files aren't direct (the cache renders them instead).
        os.remove(os.path.join(self.dir, "plate.0004.jpg"))
        src.refresh()
        self.assertIsNone(src.direct_path(4))

    def test_not_direct(self):
        read = read_node(self.pattern)
        self.assertFalse(self.source(Node("Grade", [read])).direct)
        self.assertFalse(self.source(read, exts=()).direct)
        self.assertFalse(self.source(read_node(self.pattern, raw="True")).direct)
        self.assertFalse(self.source(read_node(self.pattern, colorspace="ACES - ACEScg")).direct)
        self.assertTrue(self.source(read_node(self.pattern, colorspace="sRGB")).direct)
        self.assertFalse(self.source(read_node(self.dir + "/plate.%04d.exr")).direct)
        self.assertFalse(self.source(Node("TimeOffset", [read])).direct)

    def test_signature_follows_the_source_file(self):
        src = self.source(Node("Grade", [read_node(self.pattern)], white=2))
        before = src.signature(3)
        self.assertEqual(before, src.signature(3))
        self.assertNotEqual(before, src.signature(4))
        path = os.path.join(self.dir, "plate.0003.jpg")
        with open(path, "w") as fh:
            fh.write("new pixels")
        later = time.time() + 5
        os.utime(path, (later, later))
        src.refresh()
        self.assertNotEqual(before, src.signature(3))

    def test_time_nodes_use_a_sequence_fingerprint(self):
        src = self.source(Node("TimeOffset", [read_node(self.pattern)]))
        sig = src.signature(1)
        self.assertEqual(sig, src.signature(7))      # every frame depends on all files
        self.assertEqual(sig[1][0][0], "sequence")
        self.assertEqual(sig[1][0][2], 10)


if __name__ == "__main__":
    unittest.main()
