"""autolens.ux: quality presets, step indicator, progress/ETA and failure text (no Nuke needed)."""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import ux  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "AutoLens" / "python" / "autolens"


class PresetTests(unittest.TestCase):
    def test_preset_values_unchanged(self):
        # The engine settings behind each preset must not drift: a Normal solve
        # made by this build should match one made by v0.10.30.
        self.assertEqual(ux.QUALITY_PRESETS["Preview"], {
            "quality": "preview", "resolution": 1800, "target": 8, "minimum": 5, "coverage": 0.90, "sources": 6,
        })
        self.assertEqual(ux.QUALITY_PRESETS["Normal"], {
            "quality": "normal", "resolution": 3200, "target": 12, "minimum": 6, "coverage": 0.97, "sources": 8,
        })
        self.assertEqual(ux.QUALITY_PRESETS["High"], {
            "quality": "high", "resolution": 4000, "target": 18, "minimum": 8, "coverage": 0.99, "sources": 12,
        })
        self.assertEqual(ux.QUALITY_ORDER, ("Preview", "Normal", "High"))

    def test_preset_name_falls_back_to_normal(self):
        self.assertEqual(ux.preset_name("high"), "High")
        self.assertEqual(ux.preset_name(" Preview "), "Preview")
        self.assertEqual(ux.preset_name(""), "Normal")
        self.assertEqual(ux.preset_name(None), "Normal")
        self.assertEqual(ux.preset_name("Ultra"), "Normal")

    def test_preset_settings_is_a_copy(self):
        settings = ux.preset_settings("High")
        settings["resolution"] = 1
        self.assertEqual(ux.QUALITY_PRESETS["High"]["resolution"], 4000)

    def test_preset_summary(self):
        for name in ux.QUALITY_ORDER:
            text = ux.preset_summary(name)
            self.assertIn(str(ux.QUALITY_PRESETS[name]["resolution"]), text)
        self.assertIn("Recommended", ux.preset_summary("Normal"))

    def test_settings_match_preset(self):
        p = ux.QUALITY_PRESETS["Normal"]
        args = (p["resolution"], p["target"], p["minimum"], p["coverage"], p["sources"])
        self.assertTrue(ux.settings_match_preset("Normal", *args))
        self.assertFalse(ux.settings_match_preset("High", *args))
        self.assertFalse(ux.settings_match_preset("Normal", 2000, *args[1:]))
        self.assertFalse(ux.settings_match_preset("Normal", *args, selection="all"))
        # QDoubleSpinBox rounding must not count as an override.
        self.assertTrue(ux.settings_match_preset("Normal", args[0], args[1], args[2], 0.9700000001, args[4]))


class RunModeTests(unittest.TestCase):
    def test_run_mode_args(self):
        self.assertEqual(len(ux.RUN_MODES), 4)
        self.assertEqual(ux.run_mode_args(0), [])
        self.assertEqual(ux.run_mode_args(1), ["--force-dense"])
        self.assertEqual(ux.run_mode_args(2), ["--force"])
        self.assertEqual(ux.run_mode_args(3), ["--skip-dense"])
        self.assertEqual(ux.run_mode_args(-1), [])


class StepTests(unittest.TestCase):
    def test_current_step(self):
        self.assertEqual(ux.current_step(False, False), ux.STEP_SETUP)
        self.assertEqual(ux.current_step(True, False), ux.STEP_SETUP)
        self.assertEqual(ux.current_step(False, True), ux.STEP_SETUP)
        self.assertEqual(ux.current_step(True, True), ux.STEP_SOLVE)
        self.assertEqual(ux.current_step(True, True, running=True), ux.STEP_SOLVE)
        self.assertEqual(ux.current_step(True, True, has_result=True), ux.STEP_REVIEW)
        self.assertEqual(ux.current_step(True, True, has_result=True, imported=True), ux.STEP_CREATE)
        # A finished solve can be reviewed even when the plate is offline.
        self.assertEqual(ux.current_step(False, False, has_result=True), ux.STEP_REVIEW)
        # A re-run in progress wins over an older result.
        self.assertEqual(ux.current_step(True, True, running=True, has_result=True), ux.STEP_SOLVE)

    def test_indicator_html(self):
        html = ux.step_indicator_html(ux.STEP_REVIEW)
        for name in ux.STEPS:
            self.assertIn(name, html)
        # Earlier steps are ticked, the current one is bold.
        self.assertEqual(html.count("&#10003;"), 2)
        self.assertIn("font-weight:700;\">3. Review", html)
        self.assertEqual(ux.step_indicator_html(99), ux.step_indicator_html(ux.STEP_CREATE))
        self.assertTrue(ux.step_hint(ux.STEP_SOLVE))
        self.assertEqual(ux.step_hint(-5), ux.step_hint(0))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def test_parse_status_line(self):
        line = "AUTOLENS STATUS  [######################--------]  8/11  (73%)"
        self.assertEqual(ux.parse_status_line(line), (8, 11, 73))
        self.assertIsNone(ux.parse_status_line(">>> Computing dense depth maps"))
        self.assertIsNone(ux.parse_status_line(None))

    def test_format_duration(self):
        self.assertEqual(ux.format_duration(0), "0s")
        self.assertEqual(ux.format_duration(45.4), "45s")
        self.assertEqual(ux.format_duration(185), "3m 05s")
        self.assertEqual(ux.format_duration(3600 + 12 * 60), "1h 12m")
        self.assertEqual(ux.format_duration(-3), "0s")

    def test_format_eta(self):
        self.assertEqual(ux.format_eta(30), "less than a minute left")
        self.assertEqual(ux.format_eta(6 * 60 + 10), "about 6 min left")
        self.assertEqual(ux.format_eta(90 * 60), "about 1.5 h left")

    def test_estimator(self):
        clock = FakeClock()
        est = ux.ProgressEstimator(clock=clock)
        self.assertEqual(est.text(), "Starting")
        est.start()
        clock.now += 5
        est.update(9, "Extracting features", 1, 11)
        # Too early for an ETA.
        self.assertIsNone(est.remaining())
        self.assertEqual(est.text(), "Step 1 of 11: Extracting features \u00b7 5s elapsed")
        clock.now += 295  # 300 s total
        est.update(50, "Computing dense depth maps", 8, 11)
        self.assertAlmostEqual(est.remaining(), 300.0)
        self.assertEqual(
            est.text(),
            "Step 8 of 11: Computing dense depth maps \u00b7 5m 00s elapsed \u00b7 about 5 min left",
        )
        est.update(100, "Complete")
        self.assertIsNone(est.remaining())
        self.assertEqual(est.text(), "Complete \u00b7 5m 00s elapsed")

    def test_estimator_starts_on_first_update(self):
        clock = FakeClock()
        est = ux.ProgressEstimator(clock=clock)
        est.update(20, "Matching frames", 3, 11)
        clock.now += 60
        self.assertEqual(est.elapsed(), 60)
        self.assertAlmostEqual(est.remaining(), 240.0)


def _failed_log(message, before=""):
    return before + "\n\nAUTOLENS FAILED:\n" + message + "\n"


class FailureTests(unittest.TestCase):
    def assertTitle(self, log, title, exit_code=1, **kwargs):
        info = ux.explain_failure(log, exit_code, **kwargs)
        self.assertEqual(info["title"], title, info)
        self.assertTrue(info["next_step"])
        return info

    def test_last_engine_error(self):
        log = _failed_log("first") + _failed_log("Best reconstruction registered only 2 images.")
        self.assertEqual(ux.last_engine_error(log), "Best reconstruction registered only 2 images.")
        self.assertEqual(ux.last_engine_error("all good"), "")

    def test_known_engine_errors(self):
        cases = {
            "COLMAP was not found. Run the AutoLens installer or pass --colmap.": "COLMAP is not installed correctly",
            "Managed COLMAP environment is missing: C:/x": "COLMAP is not installed correctly",
            "Need at least 3 images. Found 2 in C:/plate": "Not enough frames",
            "Images folder does not exist: C:/plate": "The plate folder can't be found",
            "Tracking mask folder does not exist: C:/m": "The tracking mask could not be exported",
            "Input image sequence has changed since the cached solve. Run again with --force to rebuild safely.":
                "The plate changed since the last solve",
            "Neither incremental nor global SfM could reconstruct this shot.": "The camera could not be solved",
            "Best reconstruction registered only 2 images.": "The camera could not be solved",
            "COLMAP created no sparse reconstruction.": "The camera could not be solved",
            "[Errno 28] No space left on device": "The output disk is full",
            "[Errno 13] Permission denied: 'C:/out/x.json'": "AutoLens can't write to the output folder",
        }
        for message, title in cases.items():
            with self.subTest(message=message):
                info = self.assertTitle(_failed_log(message), title)
                self.assertEqual(info["detail"], message)

    def test_generic_command_failure_uses_specific_cause_from_log(self):
        log = _failed_log(
            "Command failed with exit code 1 (0x00000001)",
            before="F20231023 cuda_util.cc] CUDA error: out of memory\n",
        )
        self.assertTitle(log, "Ran out of GPU or system memory")

    def test_generic_command_failure(self):
        self.assertTitle(_failed_log("Command failed with exit code 3 (0x00000003)"), "A COLMAP step failed")

    def test_dll_error(self):
        log = _failed_log("Command failed with exit code -1073741502 (0xC0000142)\nWindows DLL initialization failed")
        self.assertTitle(log, "COLMAP failed to start (Windows DLL error)")

    def test_failed_to_start(self):
        self.assertTitle("\n[AUTOLENS] Failed to start: No such file\n", "The AutoLens engine could not start", exit_code=-1)

    def test_missing_python_packages(self):
        log = "AutoLens requires the Python packages OpenEXR and Imath.\nImport error: No module named 'OpenEXR'"
        self.assertTitle(log, "AutoLens' Python packages are missing")

    def test_cancel(self):
        self.assertTitle("anything", "Solve cancelled", cancelled=True)
        self.assertTitle("\nCancelled.\n", "Solve cancelled")
        self.assertTitle("", "Solve cancelled", exit_code=130)

    def test_unknown_error_keeps_engine_message(self):
        info = self.assertTitle(_failed_log("Something odd happened"), "AutoLens did not finish")
        self.assertEqual(info["reason"], "Something odd happened")
        info = self.assertTitle("", "AutoLens did not finish", exit_code=7)
        self.assertIn("7", info["reason"])

    def test_only_log_tail_is_searched(self):
        old = "Neither incremental nor global SfM could reconstruct this shot.\n"
        log = old + ("x" * 30000) + _failed_log("Something odd happened")
        self.assertTitle(log, "AutoLens did not finish")

    def test_failure_message(self):
        info = ux.explain_failure(_failed_log("Need at least 3 images. Found 1 in C:/p"))
        text = ux.failure_message(info)
        self.assertIn("What to do:", text)
        self.assertIn("Engine message: Need at least 3 images", text)
        info = ux.explain_failure(_failed_log("Something odd happened"))
        self.assertNotIn("Engine message", ux.failure_message(info))


class NodeSourceTests(unittest.TestCase):
    """node.py imports nuke, so check its tables from the source instead."""

    @classmethod
    def setUpClass(cls):
        cls.source = (PACKAGE / "node.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _dict_literal(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return ast.literal_eval(node.value)
        self.fail(f"{name} not found in node.py")

    def test_tooltips_name_real_knobs(self):
        tooltips = self._dict_literal("_KNOB_TOOLTIPS")
        self.assertGreaterEqual(len(tooltips), 10)
        for knob in tooltips:
            # Each knob is created somewhere else in node.py.
            self.assertGreaterEqual(self.source.count(f'"{knob}"'), 2, knob)
            self.assertTrue(tooltips[knob].strip())

    def test_legacy_knob_names_kept(self):
        # Controllers are found by these knob names (autolens.startup.is_controller).
        startup = (PACKAGE / "startup.py").read_text(encoding="utf-8")
        self.assertIn('_knob(node, "autolens_output") and _knob(node, "autolens_open")', startup)
        labels = self._dict_literal("_KNOB_LABELS")
        self.assertEqual(set(labels), {"autolens_open", "autolens_import_result"})

    def test_json_imported(self):
        # Create from solve parses autolens_result.json.
        self.assertRegex(self.source, r"(?m)^import json$")


if __name__ == "__main__":
    unittest.main()
