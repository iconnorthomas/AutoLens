"""autolens.frame_cache: viewer cache, prefetch order and playback timing (no Qt, no Nuke)."""
import os
import sys
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


class PlaybackStepTests(unittest.TestCase):
    def step(self, current, target, ready, direction=1, loop=True):
        return fc.playback_step(current, target, lambda f: f in ready, direction, 1, 10, loop)

    def test_target_ready_is_shown(self):
        self.assertEqual(self.step(3, 5, {5}), (5, False))
        self.assertEqual(self.step(3, 3, {3}), (None, False))

    def test_waits_or_steps_in_order_when_behind(self):
        # Target not decoded yet: show the next frame and slow down to it.
        self.assertEqual(self.step(3, 6, {4, 5}), (4, True))
        # Nothing ready: hold and wait.
        self.assertEqual(self.step(3, 6, set()), (None, True))
        self.assertEqual(self.step(3, 1, {2}, direction=-1), (2, True))
        # Wraps when looping; stops at the end when not.
        self.assertEqual(self.step(10, 2, {1}), (1, True))
        self.assertEqual(self.step(10, 10, set(), loop=False), (None, False))

    def test_rebase(self):
        now = [0.0]
        clock = fc.PlaybackClock(10, 1, 100, clock=lambda: now[0])
        clock.start(1)
        now[0] = 1.0
        self.assertEqual(clock.target(), (11, False))
        clock.rebase(4)                 # we were only able to show frame 4
        self.assertEqual(clock.target(), (4, False))
        now[0] = 1.1
        self.assertEqual(clock.target(), (5, False))


if __name__ == "__main__":
    unittest.main()
