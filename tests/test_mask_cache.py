"""Mask overlay cache: filled in idle time, nearest the playhead first (no Nuke or Qt)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import mask_cache as mc  # noqa: E402


class FakeClock(object):
    """Each call advances time by `tick` seconds."""

    def __init__(self, tick=0.001):
        self.now = 0.0
        self.tick = tick

    def __call__(self):
        self.now += self.tick
        return self.now


class GeometryTests(unittest.TestCase):
    def test_overlay_size(self):
        self.assertEqual(mc.overlay_size(1920, 1080), (160, 90))
        self.assertEqual(mc.overlay_size(2048, 858), (160, 67))
        self.assertEqual(mc.overlay_size(64, 64), (64, 64))
        self.assertEqual(mc.overlay_size(0, 0), (160, 90))   # unknown -> HD
        self.assertEqual(mc.overlay_size(10, 1000), (32, 3200))

    def test_sample_points_flip_y(self):
        xs, ys = mc.sample_points(4, 2, 400, 200)
        self.assertEqual(xs, [50.0, 150.0, 250.0, 350.0])
        # Row 0 is the top of the image: high Nuke y.
        self.assertEqual(ys, [150.0, 50.0])

    def test_row_bytes(self):
        self.assertEqual(mc.row_bytes([0.0, 1.0, 0.5]),
                         bytes((0, 0, 0, 0, 0, 0, 51, 51, 0, 0, 26, 26)))
        self.assertEqual(mc.row_bytes([1.0], inverted=True), bytes(4))
        self.assertEqual(mc.row_bytes([-3.0, 7.0]), bytes((0, 0, 0, 0, 0, 0, 51, 51)))


class JobTests(unittest.TestCase):
    def test_job_respects_the_time_budget(self):
        calls = []

        def sampler(x, y):
            calls.append((x, y))
            return 1.0 if y > 45 else 0.0  # top half masked

        clock = FakeClock(tick=0.001)
        job = mc.OverlayJob(7, sampler, 1920, 1080, clock=clock)
        self.assertEqual((job.width, job.height), (160, 90))
        # Each row reads the clock once (plus the start), so a 3 ms budget
        # samples a few rows, never the whole frame.
        self.assertFalse(job.step(0.003))
        self.assertTrue(0 < job.rows_done < 10)
        while not job.step(0.003):
            pass
        self.assertEqual(len(calls), 160 * 90)
        data = job.data()
        self.assertEqual(len(data), 160 * 90 * 4)
        self.assertEqual(data[3], 51)          # top row masked
        self.assertEqual(data[-1], 0)          # bottom row clear
        self.assertGreater(job.seconds, 0.0)

    def test_always_progresses(self):
        job = mc.OverlayJob(1, lambda x, y: 0.0, 320, 180, clock=FakeClock(1.0))
        job.step(0.0)
        self.assertEqual(job.rows_done, 1)

    def test_errors_are_kept(self):
        def broken(x, y):
            raise ValueError("no alpha")

        job = mc.OverlayJob(3, broken, 320, 180)
        self.assertTrue(job.step(0.01))
        self.assertIsInstance(job.error, ValueError)
        with self.assertRaises(RuntimeError):
            job.data()

    def test_unfinished_data_raises(self):
        job = mc.OverlayJob(3, lambda x, y: 0.0, 320, 180, clock=FakeClock(1.0))
        job.step(0.0)
        with self.assertRaises(RuntimeError):
            job.data()


class FillOrderTests(unittest.TestCase):
    def test_outward_from_playhead(self):
        self.assertEqual(mc.fill_order(5, 1, 9), [5, 6, 4, 7, 3, 8, 2, 9, 1])

    def test_skips_ready_and_clamps(self):
        self.assertEqual(mc.fill_order(1, 1, 5, ready=[1, 2]), [3, 4, 5])
        self.assertEqual(mc.fill_order(99, 1, 4), [4, 3, 2, 1])
        self.assertEqual(mc.fill_order(3, 1, 5, ready=range(1, 6)), [])
        self.assertEqual(mc.fill_order(1, 5, 1), [])

    def test_limit(self):
        self.assertEqual(mc.fill_order(10, 1, 100, limit=3), [10, 11, 9])


class CacheTests(unittest.TestCase):
    def test_dict_like(self):
        cache = mc.OverlayCache()
        self.assertFalse(cache)
        cache[3] = "three"
        cache.add(4, "four", seconds=0.2)
        self.assertTrue(cache)
        self.assertEqual(len(cache), 2)
        self.assertIn(3, cache)
        self.assertEqual(cache.get(4), "four")
        self.assertIsNone(cache.get(5))
        self.assertEqual(cache.frames(), [3, 4])
        self.assertEqual(cache.ranges(), [(3, 4)])
        self.assertAlmostEqual(cache.ms_per_frame(), 200.0)
        cache.clear()
        self.assertEqual((len(cache), cache.filled), (0, 0))

    def test_stand_in_is_the_nearest_ready_frame(self):
        cache = mc.OverlayCache()
        self.assertIsNone(cache.stand_in(10))
        cache[5] = "five"
        cache[20] = "twenty"
        self.assertEqual(cache.stand_in(5), "five")
        self.assertEqual(cache.stand_in(9), "five")
        self.assertEqual(cache.stand_in(17), "twenty")
        # A tie goes to the side we came from.
        self.assertEqual(cache.stand_in(12, direction=1), "five")

    def test_signature(self):
        cache = mc.OverlayCache()
        self.assertFalse(cache.update_signature("a"))   # first signature: nothing to clear
        cache[1] = "one"
        self.assertFalse(cache.update_signature("a"))
        self.assertEqual(len(cache), 1)
        self.assertTrue(cache.update_signature("b"))
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.signature, "b")


class Knob(object):
    def __init__(self, value, keys=None):
        self._value = value
        self._keys = keys

    def toScript(self):
        if self._keys:
            return "{curve " + " ".join("x%d %g" % k for k in self._keys) + "}"
        return str(self._value)


class Node(object):
    def __init__(self, cls, knobs=None, inputs=(), name="Node1", xpos=0):
        self._cls = cls
        self._knobs = dict(knobs or {})
        self._knobs["name"] = Knob(name)
        self._knobs["xpos"] = Knob(xpos)
        self._inputs = list(inputs)

    def Class(self):
        return self._cls

    def knobs(self):
        return self._knobs

    def inputs(self):
        return len(self._inputs)

    def input(self, i):
        return self._inputs[i]


class SignatureTests(unittest.TestCase):
    def graph(self, keys=((1, 0.0), (10, 1.0)), name="Roto1", xpos=0, blur=4):
        read = Node("Read", {"file": Knob("plate.%04d.exr")}, name="Read1")
        roto = Node("Roto", {"opacity": Knob(1, keys=list(keys))}, [read], name=name, xpos=xpos)
        return Node("Blur", {"size": Knob(blur)}, [roto], name="Blur1")

    def test_ignores_names_and_positions(self):
        a = mc.graph_signature(self.graph())
        self.assertEqual(a, mc.graph_signature(self.graph(name="Matte", xpos=500)))

    def test_changes_with_keys_and_values(self):
        a = mc.graph_signature(self.graph())
        self.assertNotEqual(a, mc.graph_signature(self.graph(keys=((1, 0.0), (12, 1.0)))))
        self.assertNotEqual(a, mc.graph_signature(self.graph(blur=8)))
        self.assertNotEqual(a, mc.graph_signature(self.graph(), extra=[True]))

    def test_none(self):
        self.assertIsNone(mc.graph_signature(None))

    def test_write_knobs_path(self):
        class FakeNuke(object):
            WRITE_NON_DEFAULT_ONLY = 1
            TO_SCRIPT = 2

        class Written(Node):
            def __init__(self, text, **kw):
                Node.__init__(self, "Roto", **kw)
                self.text = text

            def writeKnobs(self, flags):
                return self.text

        a = mc.graph_signature(Written("name Roto1\nxpos 10\ncurves {{ key 1 }}"), FakeNuke())
        b = mc.graph_signature(Written("name Other\nxpos 90\ncurves {{ key 1 }}"), FakeNuke())
        c = mc.graph_signature(Written("name Roto1\nxpos 10\ncurves {{ key 2 }}"), FakeNuke())
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_upstream_cycle_and_limit(self):
        a = Node("Dot")
        b = Node("Dot", inputs=[a])
        a._inputs = [b]   # a cycle must not loop forever
        self.assertEqual(len(mc.upstream_nodes(a)), 2)
        chain = Node("Dot")
        for _ in range(10):
            chain = Node("Dot", inputs=[chain])
        self.assertEqual(len(mc.upstream_nodes(chain, limit=4)), 4)


if __name__ == "__main__":
    unittest.main()
