"""autolens.frame_cache: viewer cache, prefetch order and playback timing (no Qt, no Nuke)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import frame_cache as fc  # noqa: E402


class FrameCacheTests(unittest.TestCase):
    def test_evicts_least_recently_used_by_bytes(self):
        cache = fc.FrameCache(max_bytes=300)
        cache.put(1, "a", 100)
        cache.put(2, "b", 100)
        cache.put(3, "c", 100)
        self.assertEqual(cache.total_bytes, 300)
        cache.get(1)                      # 1 is now the most recent
        evicted = cache.put(4, "d", 100)
        self.assertEqual(evicted, [2])
        self.assertNotIn(2, cache)
        self.assertEqual(sorted(cache.keys()), [1, 3, 4])
        self.assertEqual(cache.total_bytes, 300)

    def test_peek_does_not_refresh(self):
        cache = fc.FrameCache(max_bytes=200)
        cache.put(1, "a", 100)
        cache.put(2, "b", 100)
        cache.peek(1)
        self.assertEqual(cache.put(3, "c", 100), [1])

    def test_protected_entries_survive(self):
        cache = fc.FrameCache(max_bytes=200)
        cache.put(1, "a", 100)
        cache.put(2, "b", 100)
        evicted = cache.put(3, "c", 100, protect={1})
        self.assertEqual(evicted, [2])
        self.assertIn(1, cache)

    def test_replace_and_oversize(self):
        cache = fc.FrameCache(max_bytes=100)
        cache.put(1, "a", 60)
        cache.put(1, "b", 40)
        self.assertEqual(cache.get(1), "b")
        self.assertEqual(cache.total_bytes, 40)
        self.assertEqual(cache.put(2, "huge", 101), [])
        self.assertNotIn(2, cache)
        self.assertEqual(cache.total_bytes, 40)
        cache.clear()
        self.assertEqual((len(cache), cache.total_bytes), (0, 0))
        self.assertIsNone(cache.get(1))

    def test_budget_from_environment(self):
        self.assertEqual(fc.cache_budget_bytes({}), fc.DEFAULT_CACHE_MB * 1024 * 1024)
        self.assertEqual(fc.cache_budget_bytes({fc.CACHE_MB_ENV: "256"}), 256 * 1024 * 1024)
        self.assertEqual(fc.cache_budget_bytes({fc.CACHE_MB_ENV: "1"}), 64 * 1024 * 1024)
        self.assertEqual(
            fc.cache_budget_bytes({fc.CACHE_MB_ENV: "lots"}),
            fc.DEFAULT_CACHE_MB * 1024 * 1024,
        )


class SizeTests(unittest.TestCase):
    def test_bucket_width(self):
        self.assertEqual(fc.bucket_width(100), 480)
        self.assertEqual(fc.bucket_width(960), 960)
        self.assertEqual(fc.bucket_width(961), 1280)
        self.assertEqual(fc.bucket_width(10000), 3840)

    def test_fit_size(self):
        self.assertEqual(fc.fit_size(4096, 2160, 1280), (1280, 675))
        self.assertEqual(fc.fit_size(1920, 1080, 4000), (1920, 1080))      # never upscale
        self.assertEqual(fc.fit_size(1920, 1080, 4000, 540), (960, 540))
        # 2:1 anamorphic: displayed twice as wide, so decode half as many columns.
        self.assertEqual(fc.fit_size(2048, 858, 2048, 0, 2.0), (1024, 429))


class FrameStepTests(unittest.TestCase):
    def test_step_frame(self):
        self.assertEqual(fc.step_frame(10, 1, 1, 10), 10)
        self.assertEqual(fc.step_frame(10, 1, 1, 10, loop=True), 1)
        self.assertEqual(fc.step_frame(1, -1, 1, 10, loop=True), 10)
        self.assertEqual(fc.step_frame(5, -10, 1, 10), 1)
        self.assertEqual(fc.step_frame(1001, 25, 1001, 1010, loop=True), 1006)

    def test_prefetch_while_paused_alternates(self):
        self.assertEqual(
            fc.prefetch_order(10, 1, 100, 0, ahead=3, behind=2),
            [11, 9, 12, 8, 13],
        )
        # Clipped at the range ends, current frame never included.
        self.assertEqual(fc.prefetch_order(1, 1, 3, 0, ahead=5, behind=5), [2, 3])

    def test_prefetch_while_playing_runs_ahead(self):
        order = fc.prefetch_order(10, 1, 100, 1, ahead=8, behind=2)
        self.assertEqual(order[:4], [11, 12, 13, 14])
        self.assertEqual(sorted(order), [8, 9] + list(range(11, 19)))
        self.assertLess(order.index(9), order.index(18))     # a frame behind is interleaved
        backwards = fc.prefetch_order(10, 1, 100, -1, ahead=3, behind=1)
        self.assertEqual(backwards, [9, 8, 7, 11])

    def test_prefetch_loops(self):
        self.assertEqual(
            fc.prefetch_order(9, 1, 10, 1, ahead=4, behind=0, loop=True),
            [10, 1, 2, 3],
        )
        # A range shorter than the window lists each frame once.
        self.assertEqual(
            sorted(fc.prefetch_order(2, 1, 3, 1, ahead=10, behind=10, loop=True)),
            [1, 3],
        )

    def test_nearest_cached(self):
        self.assertIsNone(fc.nearest_cached(5, []))
        self.assertEqual(fc.nearest_cached(5, [1, 5, 9]), 5)
        self.assertEqual(fc.nearest_cached(4, [1, 9]), 1)
        self.assertEqual(fc.nearest_cached(7, {1, 9}), 9)
        self.assertEqual(fc.nearest_cached(0, [3, 4]), 3)
        self.assertEqual(fc.nearest_cached(50, [3, 4]), 4)
        # Tie: keep the side the drag came from.
        self.assertEqual(fc.nearest_cached(5, [3, 7], direction=1), 3)
        self.assertEqual(fc.nearest_cached(5, [3, 7], direction=-1), 7)

    def test_runs(self):
        self.assertEqual(fc.contiguous_runs([]), [])
        self.assertEqual(
            fc.contiguous_runs([5, 3, 4, 9, 10, 12, 4]),
            [(3, 5), (9, 10), (12, 12)],
        )
        self.assertEqual(
            fc.merge_runs([(1, 5), (7, 10), (30, 31)], 1),
            [(1, 10), (30, 31)],
        )
        self.assertEqual(fc.merge_runs([(1, 5), (7, 10)], 0), [(1, 5), (7, 10)])
        self.assertEqual(fc.merge_runs([(1, 2), (3, 4)], 0), [(1, 4)])
        self.assertEqual(
            fc.split_runs([(1, 10), (20, 21)], 4),
            [(1, 4), (5, 8), (9, 10), (20, 21)],
        )


class PlaybackClockTests(unittest.TestCase):
    def make(self, **kwargs):
        now = [100.0]
        clock = fc.PlaybackClock(clock=lambda: now[0], **kwargs)
        return clock, now

    def test_real_time_with_frame_dropping(self):
        clock, now = self.make(fps=24, first=1, last=100)
        self.assertEqual(clock.start(10), 10)
        self.assertEqual(clock.target(), (10, False))
        now[0] += 1.0 / 24
        self.assertEqual(clock.target(), (11, False))
        # A slow tick doesn't slow playback: we jump to where time says.
        now[0] += 0.5
        self.assertEqual(clock.target(), (23, False))

    def test_loop_and_stop(self):
        clock, now = self.make(fps=10, first=1, last=10, loop=True)
        clock.start(9)
        now[0] += 0.3
        self.assertEqual(clock.target(), (2, False))

        clock, now = self.make(fps=10, first=1, last=10, loop=False)
        clock.start(9)
        now[0] += 0.3
        self.assertEqual(clock.target(), (10, True))

    def test_restart_at_end_and_backwards(self):
        clock, now = self.make(fps=10, first=1, last=10, loop=False)
        self.assertEqual(clock.start(10), 1)

        clock, now = self.make(fps=10, first=1, last=10, loop=False, direction=-1)
        self.assertEqual(clock.start(1), 10)
        now[0] += 0.25
        self.assertEqual(clock.target(), (8, False))
        now[0] += 5
        self.assertEqual(clock.target(), (1, True))


class PathTests(unittest.TestCase):
    def test_expand_frame_pattern(self):
        self.assertEqual(fc.expand_frame_pattern("/a/plate.%04d.jpg", 7), "/a/plate.0007.jpg")
        self.assertEqual(fc.expand_frame_pattern("/a/plate.%d.jpg", 7), "/a/plate.7.jpg")
        self.assertEqual(fc.expand_frame_pattern("/a/plate.####.png", 1001), "/a/plate.1001.png")
        self.assertEqual(fc.expand_frame_pattern("/a/plate.@@@.png", 5), "/a/plate.005.png")
        self.assertEqual(fc.expand_frame_pattern("/a/plate.%04d.jpg", -3), "/a/plate.-003.jpg")
        self.assertEqual(fc.expand_frame_pattern("/a/still.jpg", 5), "/a/still.jpg")
        self.assertTrue(fc.has_frame_token("x.%04d.exr"))
        self.assertTrue(fc.has_frame_token("x.####.exr"))
        self.assertFalse(fc.has_frame_token("x.exr"))

    def test_is_directly_readable(self):
        formats = {"jpg", "jpeg", "png", "tif"}
        self.assertTrue(fc.is_directly_readable("/a/b.0001.JPG", formats))
        self.assertTrue(fc.is_directly_readable("C:\\a\\b.png", formats))
        self.assertFalse(fc.is_directly_readable("/a/b.0001.exr", formats))
        self.assertFalse(fc.is_directly_readable("/a/noext", formats))

    def test_proxy_names(self):
        key = fc.proxy_key("/a/plate.%04d.exr", (1, 2), 0.5)
        self.assertEqual(key, fc.proxy_key("/a/plate.%04d.exr", (1, 2), 0.5))
        self.assertNotEqual(key, fc.proxy_key("/a/plate.%04d.exr", (1, 3), 0.5))
        self.assertEqual(len(key), 16)
        # Must match what Nuke writes for "proxy.%06d.jpg".
        self.assertEqual(fc.proxy_frame_name(12), "proxy.000012.jpg")
        self.assertEqual(fc.proxy_frame_name(-1), "proxy.-00001.jpg")

    def test_prune_old_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("old", "new", "kept"):
                os.mkdir(os.path.join(root, name))
            old_time = time.time() - 10 * 86400
            os.utime(os.path.join(root, "old"), (old_time, old_time))
            os.utime(os.path.join(root, "kept"), (old_time, old_time))
            removed = fc.prune_old_dirs(root, 7.0, keep={"kept"})
            self.assertEqual(removed, ["old"])
            self.assertEqual(sorted(os.listdir(root)), ["kept", "new"])
        self.assertEqual(fc.prune_old_dirs(os.path.join(root, "gone")), [])


if __name__ == "__main__":
    unittest.main()
