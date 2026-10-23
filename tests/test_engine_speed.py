"""Speed settings in the engine: matcher choice, mapper options and COLMAP option filtering.

Imports AutoLens/engine/autolens_engine.py directly; it needs numpy and OpenEXR
(the engine's own dependencies) but not COLMAP or Nuke.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ENGINE = os.path.join(os.path.dirname(__file__), "..", "AutoLens", "engine", "autolens_engine.py")

try:
    _spec = importlib.util.spec_from_file_location("autolens_engine", ENGINE)
    engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(engine)
except (ImportError, SystemExit) as exc:      # the engine exits if OpenEXR is missing
    engine = None
    _missing = str(exc)


def brute_force_sequential_pairs(n, overlap):
    """COLMAP's sequential matcher with quadratic overlap, pair by pair."""
    pairs = set()
    for i in range(n):
        for k in range(overlap):
            for j in (i + k + 1, i + 2 ** k):
                if i < j < n:
                    pairs.add((i, j))
    return len(pairs)


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class MatchingPlanTests(unittest.TestCase):
    def test_short_shots_stay_exhaustive(self):
        plan = engine.choose_matching_plan(40, profile="fast", exhaustive_max_frames=60)
        self.assertEqual(plan["command"], "exhaustive_matcher")
        self.assertEqual(plan["pair_count"], 40 * 39 // 2)
        self.assertFalse(plan["retry_exhaustive"])

    def test_long_shots_match_sequentially_with_retry(self):
        plan = engine.choose_matching_plan(250, profile="fast", exhaustive_max_frames=60)
        self.assertEqual(plan["command"], "sequential_matcher")
        names = [name for name, _ in plan["options"]]
        self.assertIn("SequentialMatching.overlap", names)
        self.assertIn("SequentialMatching.quadratic_overlap", names)
        # Exhaustive was the old behaviour at 250 frames, so a weak sequential
        # solve must be able to fall back to it.
        self.assertTrue(plan["retry_exhaustive"])
        # Several times fewer pairs than exhaustive matching.
        self.assertLess(plan["pair_count"] * 4, 250 * 249 // 2)

    def test_no_retry_above_old_exhaustive_limit(self):
        plan = engine.choose_matching_plan(400, profile="fast")
        self.assertEqual(plan["command"], "sequential_matcher")
        self.assertFalse(plan["retry_exhaustive"])

    def test_legacy_profile_matches_old_pipeline(self):
        plan = engine.choose_matching_plan(250, profile="legacy")
        self.assertEqual(plan["command"], "exhaustive_matcher")
        self.assertEqual(plan["options"], [])
        plan = engine.choose_matching_plan(251, profile="legacy")
        self.assertEqual(plan["command"], "sequential_matcher")
        self.assertEqual(plan["options"], [])
        self.assertFalse(plan["retry_exhaustive"])
        self.assertEqual(engine.mapper_speed_options("legacy"), [])

    def test_unpadded_frame_names_stay_exhaustive(self):
        padded = [f"plate.{i:04d}.exr" for i in range(1001, 1201)]
        unpadded = [f"plate.{i}.exr" for i in range(1, 201)]
        self.assertTrue(engine.names_in_frame_order(padded))
        self.assertFalse(engine.names_in_frame_order(unpadded))
        plan = engine.choose_matching_plan(200, profile="fast", names_are_ordered=False)
        self.assertEqual(plan["command"], "exhaustive_matcher")

    def test_sequential_pair_estimate_matches_brute_force(self):
        for n, overlap in ((5, 20), (61, 20), (250, 20), (300, 10), (17, 3)):
            self.assertEqual(
                engine.estimate_sequential_pairs(n, overlap),
                brute_force_sequential_pairs(n, overlap),
                msg=f"n={n} overlap={overlap}",
            )

    def test_retry_decision(self):
        plan = engine.choose_matching_plan(200, profile="fast")
        self.assertTrue(engine.should_retry_with_exhaustive(plan, False, 0, 200))
        self.assertTrue(engine.should_retry_with_exhaustive(plan, True, 150, 200))
        self.assertFalse(engine.should_retry_with_exhaustive(plan, True, 195, 200))
        exhaustive = engine.choose_matching_plan(30, profile="fast")
        self.assertFalse(engine.should_retry_with_exhaustive(exhaustive, False, 0, 30))

    def test_env_var_selects_legacy(self):
        old = os.environ.get("AUTOLENS_SPEED_PROFILE")
        try:
            os.environ["AUTOLENS_SPEED_PROFILE"] = "Legacy"
            self.assertEqual(engine.default_speed_profile(), "legacy")
            os.environ["AUTOLENS_SPEED_PROFILE"] = "nonsense"
            self.assertEqual(engine.default_speed_profile(), "fast")
        finally:
            if old is None:
                os.environ.pop("AUTOLENS_SPEED_PROFILE", None)
            else:
                os.environ["AUTOLENS_SPEED_PROFILE"] = old


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class ColmapOptionTests(unittest.TestCase):
    HELP = """
    Options can either be specified via command-line or by defining
      -h [ --help ]
      --database_path arg
      --Mapper.ba_global_frames_ratio arg (=1.1)
      --Mapper.ba_global_points_ratio arg (=1.1)
      --SequentialMatching.overlap arg (=10)
    """

    def test_help_parsing(self):
        options = engine.parse_colmap_help_options(self.HELP)
        self.assertIn("database_path", options)
        self.assertIn("Mapper.ba_global_frames_ratio", options)
        self.assertIn("SequentialMatching.overlap", options)

    def test_select_supported_uses_first_known_spelling(self):
        supported = engine.parse_colmap_help_options(self.HELP)
        args = engine.select_supported_options(engine.mapper_speed_options("fast"), supported)
        self.assertEqual(args, [
            "--Mapper.ba_global_frames_ratio", "1.2",
            "--Mapper.ba_global_points_ratio", "1.2",
        ])
        older = {"Mapper.ba_global_images_ratio"}
        self.assertEqual(
            engine.select_supported_options(engine.mapper_speed_options("fast"), older),
            ["--Mapper.ba_global_images_ratio", "1.2"],
        )

    def test_unknown_help_passes_nothing(self):
        self.assertEqual(
            engine.select_supported_options(engine.mapper_speed_options("fast"), set()),
            [],
        )


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class DenseWorkspaceTests(unittest.TestCase):
    def test_missing_and_subset_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            dense = Path(tmp)
            (dense / "images").mkdir()
            for name in ("a.jpg", "c.jpg"):
                (dense / "images" / name).write_bytes(b"x")
            poses = [{"name": n} for n in ("a.jpg", "b.jpg", "c.jpg")]
            self.assertEqual(engine.missing_undistorted_images(dense, ["a.jpg", "b.jpg"]), ["b.jpg"])
            self.assertTrue(engine.undistort_subset_in_workspace(dense, poses))
            (dense / "images" / "b.jpg").write_bytes(b"x")
            self.assertFalse(engine.undistort_subset_in_workspace(dense, poses))


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class TimingTests(unittest.TestCase):
    def test_stage_timer_records(self):
        before = len(engine.STAGE_TIMINGS)
        with engine.stage_timer("unit_test_stage"):
            pass
        self.assertEqual(len(engine.STAGE_TIMINGS), before + 1)
        self.assertEqual(engine.STAGE_TIMINGS[-1]["stage"], "unit_test_stage")
        self.assertGreaterEqual(engine.STAGE_TIMINGS[-1]["seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
