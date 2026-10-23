"""Sparse-solve recovery: retry-ladder decisions, option probing and coverage stats.

Imports AutoLens/engine/autolens_sparse_recovery.py directly. Only the CLAHE tests
need numpy; nothing needs COLMAP, OpenEXR or Nuke. COLMAP is replaced by a fake
runner that writes text models.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

MODULE = os.path.join(os.path.dirname(__file__), "..", "AutoLens", "engine",
                      "autolens_sparse_recovery.py")
_spec = importlib.util.spec_from_file_location("autolens_sparse_recovery", MODULE)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)

try:
    import numpy as np
except ImportError:
    np = None


def names(n, start=1001):
    return [f"plate.{start + i}.png" for i in range(n)]


class OptionTests(unittest.TestCase):
    HELP = """
      --database_path arg
      --SiftExtraction.max_num_features arg (=8192)
      --SiftExtraction.estimate_affine_shape arg (=0)
      --FeatureExtraction.use_gpu arg (=1)
      --FeatureMatching.guided_matching arg (=0)
    """

    def test_parse_help_options(self):
        found = rec.parse_help_options(self.HELP)
        self.assertIn("SiftExtraction.max_num_features", found)
        self.assertIn("FeatureMatching.guided_matching", found)
        self.assertNotIn("database_path", found)   # no dotted section

    def test_resolve_options_picks_supported_candidate_and_drops_unknown(self):
        supported = rec.parse_help_options(self.HELP)
        spec = [
            (("FeatureExtraction.max_num_features", "SiftExtraction.max_num_features"), 16384),
            (("SiftExtraction.domain_size_pooling",), "1"),
        ]
        pairs, dropped = rec.resolve_options(spec, supported)
        self.assertEqual(pairs, [("SiftExtraction.max_num_features", "16384")])
        self.assertEqual(dropped, ["SiftExtraction.domain_size_pooling"])

    def test_resolve_options_without_probe_uses_first_candidate(self):
        pairs, dropped = rec.resolve_options([(("A.x", "B.x"), 1)], None)
        self.assertEqual(pairs, [("A.x", "1")])
        self.assertEqual(dropped, [])

    def test_with_options_replaces_and_appends(self):
        cmd = ["colmap", "mapper", "--database_path", "a.db", "--output_path", "s"]
        out = rec.with_options(cmd, [("database_path", "b.db"), ("Mapper.min_num_matches", "8")])
        self.assertEqual(out, ["colmap", "mapper", "--database_path", "b.db",
                               "--output_path", "s", "--Mapper.min_num_matches", "8"])
        self.assertEqual(cmd[3], "a.db")    # original untouched
        self.assertEqual(rec.option_value(out, "Mapper.min_num_matches"), "8")
        self.assertIsNone(rec.option_value(out, "Mapper.nope"))

    def test_dense_features_turn_gpu_off_and_cap_cpu_features(self):
        cpu = dict((c[0], v) for c, v in rec.dense_feature_extraction_options(False))
        gpu = dict((c[0], v) for c, v in rec.dense_feature_extraction_options(True))
        self.assertEqual(cpu["FeatureExtraction.use_gpu"], "0")
        self.assertLess(int(cpu["SiftExtraction.max_num_features"]),
                        int(gpu["SiftExtraction.max_num_features"]))


class DecisionTests(unittest.TestCase):
    def test_needs_recovery(self):
        self.assertTrue(rec.needs_recovery(2, 4, 0.1))      # under 3 frames always retries
        self.assertTrue(rec.needs_recovery(50, 100, 0.8))
        self.assertFalse(rec.needs_recovery(80, 100, 0.8))
        self.assertFalse(rec.needs_recovery(0, 0, 0.8))

    def test_plan_ladder(self):
        self.assertEqual(rec.plan_ladder("off", True), [])
        auto = rec.plan_ladder("auto", False)
        self.assertEqual(auto, ["relaxed_init", "low_parallax", "merge_partials", "dense_features"])
        self.assertIn("contrast_plate", rec.plan_ladder("auto", True))
        self.assertNotIn("learned_features", rec.plan_ladder("auto", True))
        self.assertEqual(rec.plan_ladder("max", True)[-1], "learned_features")
        for rung in rec.plan_ladder("max", True):
            self.assertIn(rung, rec.RUNG_REASONS)

    def test_choose_best_prefers_more_frames_then_earlier(self):
        attempts = [
            {"rung": "baseline", "registered": 40, "mean_error_px": 3.0},
            {"rung": "relaxed_init", "registered": 60, "mean_error_px": 0.9},
            {"rung": "low_parallax", "registered": 60, "mean_error_px": 0.8},
        ]
        self.assertEqual(rec.choose_best_attempt(attempts)["rung"], "relaxed_init")

    def test_relaxed_attempt_with_high_error_is_rejected(self):
        attempts = [
            {"rung": "baseline", "registered": 40, "mean_error_px": 3.0},   # baseline kept anyway
            {"rung": "low_parallax", "registered": 90, "mean_error_px": 5.0},
        ]
        self.assertEqual(rec.choose_best_attempt(attempts)["rung"], "baseline")
        self.assertIsNone(rec.choose_best_attempt([{"rung": "baseline", "registered": 0}]))

    def test_choose_mapper_rung(self):
        self.assertEqual(rec.choose_mapper_rung([]), "relaxed_init")
        attempts = [
            {"rung": "relaxed_init", "registered": 20, "mean_error_px": 1.0},
            {"rung": "low_parallax", "registered": 35, "mean_error_px": 1.0},
        ]
        self.assertEqual(rec.choose_mapper_rung(attempts), "low_parallax")

    def test_plan_merge_needs_overlap(self):
        a = names(30)                    # 1001-1030
        b = names(30, start=1026)        # overlaps 5 frames
        c = names(10, start=2000)        # disjoint
        order = rec.plan_merge([{"key": "c", "names": c}, {"key": "b", "names": b},
                                {"key": "a", "names": a}])
        self.assertEqual(order[1:], ["b"] if order[0] == "a" else ["a"])
        self.assertNotIn("c", order)
        self.assertEqual(rec.plan_merge([{"key": "a", "names": a}, {"key": "c", "names": c}]), [])
        self.assertEqual(rec.plan_merge([{"key": "a", "names": a}]), [])


class CoverageTests(unittest.TestCase):
    def test_coverage_ranges_and_gap(self):
        all_names = names(10)                                   # 1001..1010
        registered = [n for n in all_names if n not in
                      ("plate.1003.png", "plate.1004.png", "plate.1005.png", "plate.1010.png")]
        stats = rec.coverage_stats(all_names, registered)
        self.assertEqual(stats["registered_frames"], 6)
        self.assertAlmostEqual(stats["registered_fraction"], 0.6)
        self.assertEqual(stats["missing_frame_ranges"], [[1003, 1005], [1010, 1010]])
        self.assertEqual(stats["registered_frame_ranges"], [[1001, 1002], [1006, 1009]])
        self.assertEqual(stats["longest_unregistered_run"], 3)
        lines = rec.coverage_log_lines(stats)
        self.assertIn("6/10", lines[0])
        self.assertIn("1003-1005, 1010", lines[1])

    def test_full_coverage_logs_one_line(self):
        stats = rec.coverage_stats(names(3), names(3))
        self.assertEqual(stats["missing_frames"], [])
        self.assertEqual(len(rec.coverage_log_lines(stats)), 1)

    def test_frame_number_fallback(self):
        self.assertEqual(rec.frame_number("shot_v002.0042.exr", 0), 42)
        self.assertEqual(rec.frame_number("still.jpg", 7), 7)

    def test_format_ranges_truncates(self):
        text = rec.format_ranges([[i, i] for i in range(0, 40, 2)], limit=3)
        self.assertTrue(text.startswith("0, 2, 4, ..."))


class ModelReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_registered_names_handles_empty_points_line(self):
        path = self.tmp / "images.txt"
        path.write_text(
            "# Image list\n"
            "1 1 0 0 0 0 0 0 1 plate.1001.png\n"
            "10.5 20.5 3 11.0 12.0 -1\n"
            "2 1 0 0 0 0 0 0 1 my plate 1002.png\n"
            "\n",
            encoding="utf-8",
        )
        self.assertEqual(rec.read_registered_names(path), ["plate.1001.png", "my plate 1002.png"])
        self.assertEqual(rec.read_registered_names(self.tmp / "missing.txt"), [])

    def test_read_mean_point_error(self):
        path = self.tmp / "points3D.txt"
        path.write_text("# x\n1 0 0 0 255 255 255 0.5 1 0\n2 0 0 0 255 255 255 1.5 1 0\n",
                        encoding="utf-8")
        self.assertAlmostEqual(rec.read_mean_point_error(path), 1.0)
        self.assertIsNone(rec.read_mean_point_error(self.tmp / "none.txt"))


def write_text_model(model_dir, image_names, error=0.7):
    for d in (model_dir, model_dir / "txt"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "cameras.txt").write_text("1 RADIAL 100 100 50 50 50 0 0\n", encoding="utf-8")
        rows = []
        for i, name in enumerate(image_names, start=1):
            rows.append(f"{i} 1 0 0 0 0 0 0 1 {name}")
            rows.append("1.0 2.0 1")
        (d / "images.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (d / "points3D.txt").write_text(f"1 0 0 0 1 1 1 {error} 1 0\n", encoding="utf-8")


class FakeColmap:
    """Stands in for COLMAP: ``results`` maps a command label to registered frame
    counts (or None = command fails)."""

    def __init__(self, all_names, results, error=0.7):
        self.all_names = all_names
        self.results = results
        self.error = error
        self.labels = []

    def run_try(self, cmd, label=None):
        self.labels.append(label)
        outcome = self.results.get(label, True)
        if outcome is None or outcome is False:
            return False
        if cmd[1] == "mapper" and outcome is not True:
            out = Path(cmd[cmd.index("--output_path") + 1])
            write_text_model(out / "0", self.all_names[:outcome], self.error)
        return True

    @staticmethod
    def prepare_command(cmd):
        raise RuntimeError("no COLMAP in tests")


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.images = [self.tmp / "plate" / n for n in names(10)]
        self.all_names = [p.name for p in self.images]
        self.sparse = self.tmp / "out" / "sparse"
        self.sparse.mkdir(parents=True)
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ladder(self, fake, mode="auto"):
        out = self.tmp / "out"
        return rec.RecoveryLadder(
            "colmap", fake.run_try, fake.prepare_command, out, self.tmp / "plate", self.images,
            ["colmap", "feature_extractor", "--database_path", str(out / "database.db"),
             "--image_path", str(self.tmp / "plate")],
            ["colmap", "exhaustive_matcher", "--database_path", str(out / "database.db"),
             "--FeatureMatching.use_gpu", "0"],
            ["colmap", "mapper", "--database_path", str(out / "database.db"),
             "--image_path", str(self.tmp / "plate"), "--output_path", str(self.sparse)],
            mode=mode, min_fraction=0.8, log=self.logs.append,
        )

    def test_good_baseline_runs_nothing(self):
        write_text_model(self.sparse / "0", self.all_names[:9])
        fake = FakeColmap(self.all_names, {})
        result = self.ladder(fake).run(self.tmp / "out" / "database.db", self.sparse, True)
        self.assertIsNone(result["sparse"])
        self.assertFalse(result["report"]["triggered"])
        self.assertEqual(result["report"]["baseline_registered"], 9)
        self.assertEqual(fake.labels, [])

    def test_relaxed_rung_wins_and_stops_ladder(self):
        write_text_model(self.sparse / "0", self.all_names[:4])
        fake = FakeColmap(self.all_names, {"recovery_mapper_relaxed_init": 9})
        result = self.ladder(fake).run(self.tmp / "out" / "database.db", self.sparse, True)
        self.assertEqual(result["solve_method"], "incremental_recovery:relaxed_init")
        self.assertEqual(result["sparse"].name, "relaxed_init")
        self.assertEqual(result["report"]["selected_rung"], "relaxed_init")
        self.assertEqual(fake.labels, ["recovery_mapper_relaxed_init"])
        self.assertTrue(any("SELECTED relaxed_init" in line for line in self.logs))

    def test_failed_baseline_falls_through_when_everything_fails(self):
        fake = FakeColmap(self.all_names, {
            "recovery_mapper_relaxed_init": None,
            "recovery_mapper_low_parallax": None,
            "recovery_feature_extractor_dense_features": None,
        })
        result = self.ladder(fake).run(self.tmp / "out" / "database.db", self.sparse, False)
        self.assertIsNone(result["sparse"])
        self.assertIsNone(result["report"]["selected_rung"])
        self.assertEqual(result["report"]["baseline_registered"], 0)
        rungs = [a["rung"] for a in result["report"]["attempts"]]
        self.assertIn("dense_features", rungs)

    def test_dense_feature_rung_uses_new_database(self):
        write_text_model(self.sparse / "0", self.all_names[:3])
        fake = FakeColmap(self.all_names, {
            "recovery_mapper_relaxed_init": 4,
            "recovery_mapper_low_parallax": 5,
            "recovery_mapper_dense_features": 10,
        })
        result = self.ladder(fake).run(self.tmp / "out" / "database.db", self.sparse, True)
        self.assertEqual(result["report"]["selected_rung"], "dense_features")
        self.assertEqual(result["database"].name, "database_dense_features.db")
        self.assertIn("mapper settings: low_parallax",
                      result["report"]["attempts"][-1]["note"])

    def test_baseline_kept_when_no_rung_improves(self):
        write_text_model(self.sparse / "0", self.all_names[:5])
        fake = FakeColmap(self.all_names, {
            "recovery_mapper_relaxed_init": 5,
            "recovery_mapper_low_parallax": 4,
            "recovery_feature_extractor_dense_features": None,
            "recovery_feature_extractor_contrast_plate": None,
        })
        result = self.ladder(fake).run(self.tmp / "out" / "database.db", self.sparse, True)
        self.assertIsNone(result["sparse"])
        self.assertEqual(result["report"]["selected_rung"], "baseline")

    def test_off_mode(self):
        write_text_model(self.sparse / "0", self.all_names[:2])
        fake = FakeColmap(self.all_names, {})
        result = self.ladder(fake, mode="off").run(self.tmp / "out" / "database.db", self.sparse, True)
        self.assertIsNone(result["sparse"])
        self.assertEqual(result["report"]["baseline_registered"], 2)
        self.assertEqual(fake.labels, [])

    def test_work_paths_cover_ladder_outputs(self):
        paths = {p.name for p in rec.work_paths(self.tmp)}
        self.assertIn(rec.RECOVERY_SPARSE_DIR, paths)
        self.assertIn(rec.TRACKING_PLATE_DIR, paths)
        self.assertIn("database_dense_features.db", paths)


class EngineWiringTests(unittest.TestCase):
    def test_engine_cli_has_recovery_flags(self):
        import subprocess
        import sys

        engine = os.path.join(os.path.dirname(MODULE), "autolens_engine.py")
        proc = subprocess.run([sys.executable, engine, "--help"],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 and "OpenEXR" in (proc.stdout + proc.stderr):
            self.skipTest("engine dependencies missing (pip install numpy OpenEXR)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--sparse-recovery", proc.stdout)
        self.assertIn("--min-registered-fraction", proc.stdout)


@unittest.skipIf(np is None, "numpy missing")
class ClaheTests(unittest.TestCase):
    def test_clahe_stretches_low_contrast(self):
        rng = np.random.default_rng(1)
        flat = 0.45 + 0.03 * rng.random((96, 128))
        out = rec.clahe(flat)
        self.assertEqual(out.shape, flat.shape)
        self.assertGreaterEqual(float(out.min()), 0.0)
        self.assertLessEqual(float(out.max()), 1.0)
        self.assertGreater(float(out.std()), 2.0 * float(flat.std()))

    def test_clahe_constant_image_stays_constant(self):
        out = rec.clahe(np.full((40, 50), 0.3))
        # Uneven tile sizes clip slightly differently; stay within one 8-bit level.
        self.assertLess(float(out.max() - out.min()), 1.0 / 255.0)

    def test_clahe_tiny_image(self):
        out = rec.clahe(np.linspace(0, 1, 6).reshape(2, 3))
        self.assertEqual(out.shape, (2, 3))

    def test_tone_map_linear(self):
        lin = np.array([[0.0, 0.01, 0.18, 4.0, np.nan]], dtype=np.float32)
        out = rec.tone_map_linear(lin)
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertGreaterEqual(float(out.min()), 0.0)
        self.assertLessEqual(float(out.max()), 1.0)
        self.assertGreater(float(out[0, 2]), float(out[0, 1]))


if __name__ == "__main__":
    unittest.main()
