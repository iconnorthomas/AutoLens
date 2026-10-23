"""Track -> Draw -> Output workspace logic and the theme (no Nuke or Qt needed)."""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import theme, ux  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "AutoLens" / "python" / "autolens"


class StepTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(ux.WORKSPACE_KEYS, ("track", "draw", "output"))
        self.assertEqual(ux.step_index("draw"), 1)
        self.assertEqual(ux.step_index("bogus"), 0)

    def test_states_fresh_shot(self):
        self.assertEqual(
            ux.workspace_step_states("track"),
            {"track": "current", "draw": "todo", "output": "todo"},
        )

    def test_states_after_track(self):
        states = ux.workspace_step_states("draw", solved=True)
        self.assertEqual(states, {"track": "done", "draw": "current", "output": "todo"})
        states = ux.workspace_step_states("output", solved=True, surfaces=2, created=True)
        self.assertEqual(states, {"track": "done", "draw": "done", "output": "current"})

    def test_failed_track_warns_unless_current(self):
        self.assertEqual(ux.workspace_step_states("draw", failed=True)["track"], "warn")
        self.assertEqual(ux.workspace_step_states("track", failed=True)["track"], "current")
        # An older good solve wins over a later failure.
        self.assertEqual(ux.workspace_step_states("draw", solved=True, failed=True)["track"], "done")

    def test_next_step_after_solve(self):
        self.assertEqual(ux.next_step_after_solve(0), "draw")
        self.assertEqual(ux.next_step_after_solve(3), "output")

    def test_hints(self):
        self.assertIn("Connect", ux.workspace_hint("track", has_plate=False))
        self.assertIn("install", ux.workspace_hint("draw", installed=False))
        self.assertIn("click Track", ux.workspace_hint("track"))
        self.assertIn("Tracking", ux.workspace_hint("track", running=True))
        self.assertIn("Tracked", ux.workspace_hint("track", solved=True))
        self.assertIn("Add a plane", ux.workspace_hint("draw"))
        self.assertIn("1 surface.", ux.workspace_hint("draw", surfaces=1))
        self.assertIn("2 surfaces", ux.workspace_hint("draw", surfaces=2, solved=True))
        self.assertIn("Track the camera first", ux.workspace_hint("output"))
        self.assertIn("Build geometry", ux.workspace_hint("output", solved=True))
        self.assertIn("Create All", ux.workspace_hint("output", solved=True, geometry_ready=True))
        self.assertIn("again", ux.workspace_hint("output", solved=True, geometry_ready=True, created=True))


class ChipTests(unittest.TestCase):
    def test_chip(self):
        self.assertEqual(ux.solve_chip(), ("NOT SOLVED", "none"))
        self.assertEqual(ux.solve_chip(running=True, percent=42), ("TRACKING 42%", "running"))
        self.assertEqual(ux.solve_chip(running=True, percent=140), ("TRACKING 100%", "running"))
        self.assertEqual(ux.solve_chip(solved=True, error_px=0.2913), ("SOLVED · 0.29 px", "solved"))
        self.assertEqual(ux.solve_chip(solved=True), ("SOLVED", "solved"))
        self.assertEqual(ux.solve_chip(solved=True, error_px="n/a"), ("SOLVED", "solved"))
        self.assertEqual(ux.solve_chip(failed=True), ("FAILED", "failed"))
        # Running wins: a re-track shows progress, not the old result.
        self.assertEqual(ux.solve_chip(running=True, solved=True)[1], "running")

    def test_chip_styles_exist(self):
        for state in ("none", "running", "solved", "failed"):
            self.assertIn("background:", theme.chip_style(state))
        self.assertEqual(theme.chip_style("bogus"), theme.chip_style("none"))


class ResultTests(unittest.TestCase):
    def test_load_result(self):
        self.assertEqual(ux.load_result(""), {})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ux.load_result(tmp), {})
            path = Path(tmp) / "autolens_result.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(ux.load_result(tmp), {})
            path.write_text(json.dumps({"mean_sparse_reprojection_error_px": 0.41}), encoding="utf-8")
            result = ux.load_result(tmp)
            self.assertAlmostEqual(ux.result_error_px(result), 0.41)
        self.assertIsNone(ux.result_error_px({}))
        self.assertIsNone(ux.result_error_px({"mean_sparse_reprojection_error_px": "x"}))

    def test_geometry_status(self):
        overall, items = ux.geometry_status({})
        self.assertEqual(overall, "none")
        self.assertEqual([ready for _l, ready in items], [False, False, False])

        camera_only = {"geometry_outputs": {"proxy_mesh_obj": None, "meshfit_planes_obj": None}}
        self.assertEqual(ux.geometry_status(camera_only, exists=lambda p: True)[0], "missing")

        built = {"geometry_outputs": {
            "proxy_mesh_obj": "a.obj", "meshfit_planes_obj": "b.obj", "guided_surfaces_obj": None,
        }}
        overall, items = ux.geometry_status(built, exists=lambda p: True)
        self.assertEqual(overall, "ready")
        self.assertEqual(items, [("Proxy mesh", True), ("Mesh-fit planes", True), ("Guided surfaces", False)])
        # Files deleted since the solve count as not built.
        self.assertEqual(ux.geometry_status(built, exists=lambda p: False)[0], "missing")

    def test_solve_args(self):
        self.assertEqual(ux.solve_args(True), [])
        self.assertEqual(ux.solve_args(False), ["--skip-dense"])
        self.assertEqual(ux.geometry_only_args(), ["--geometry-only"])
        # The engine refuses --geometry-only with --skip-dense or --force.
        self.assertFalse(set(ux.geometry_only_args()) & {"--skip-dense", "--force"})

    def _engine(self):
        return (PACKAGE.parent.parent / "engine" / "autolens_engine.py").read_text(encoding="utf-8")

    def test_engine_flags_and_keys(self):
        engine = self._engine()
        for flag in ux.CAMERA_ONLY_ARGS:
            self.assertIn(f'"{flag}"', engine)
        for key, _label in ux.GEOMETRY_ITEMS:
            self.assertIn(f'"{key}"', engine)
        self.assertIn('"mean_sparse_reprojection_error_px"', engine)

    def test_geometry_only_engine_flag(self):
        engine = self._engine()
        if '"--geometry-only"' not in engine:
            self.skipTest("needs the camera-first engine from perf/solve-10x")
        for flag in ux.BUILD_GEOMETRY_ARGS:
            self.assertIn(f'"{flag}"', engine)
        self.assertIn(ux.GEOMETRY_STATUS_FILE, engine)


class GeometryOverviewTests(unittest.TestCase):
    CAMERA_ONLY = {"geometry_status": "none", "geometry_outputs": {
        "proxy_mesh_obj": None, "meshfit_planes_obj": None, "guided_surfaces_obj": None}}
    BUILT = {"geometry_status": "ready", "geometry_outputs": {
        "proxy_mesh_obj": "p.obj", "meshfit_planes_obj": "m.obj", "guided_surfaces_obj": None}}

    def overview(self, result, status=None, running=False):
        return ux.geometry_overview(result, status, running=running, exists=lambda p: True)

    def test_no_solve(self):
        self.assertEqual(self.overview({})[0], "none")
        self.assertEqual(self.overview({}, {"status": "building"}, running=True)[0], "none")

    def test_camera_only(self):
        self.assertEqual(self.overview(self.CAMERA_ONLY)[0], "missing")
        self.assertEqual(self.overview(self.CAMERA_ONLY, {"status": "none"})[0], "missing")

    def test_building(self):
        status = {"status": "building", "step": 8, "total": 11, "stage": "Computing dense depth maps"}
        overall, _items, detail = self.overview(self.CAMERA_ONLY, status, running=True)
        self.assertEqual(overall, "building")
        self.assertEqual(detail, "Step 8 of 11: Computing dense depth maps")
        self.assertEqual(self.overview(self.CAMERA_ONLY, {"status": "building"}, running=True)[2], "")

    def test_killed_build_counts_as_failed(self):
        overall, _items, detail = self.overview(self.CAMERA_ONLY, {"status": "building"}, running=False)
        self.assertEqual(overall, "failed")
        self.assertIn("interrupted", detail)

    def test_failed(self):
        overall, _items, detail = self.overview(self.CAMERA_ONLY, {"status": "failed", "error": "no cached solve"})
        self.assertEqual((overall, detail), ("failed", "no cached solve"))
        # An older failure doesn't hide geometry that's on disk.
        self.assertEqual(self.overview(self.BUILT, {"status": "failed"})[0], "ready")

    def test_ready_uses_status_paths(self):
        status = {"status": "ready", "proxy_mesh_obj": "p.obj", "meshfit_planes_obj": "m.obj",
                  "guided_surfaces_obj": "g.obj"}
        overall, items, _detail = self.overview(self.CAMERA_ONLY, status)
        self.assertEqual(overall, "ready")
        self.assertEqual([ready for _l, ready in items], [True, True, True])

    def test_load_geometry_status(self):
        self.assertEqual(ux.load_geometry_status(""), {})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ux.load_geometry_status(tmp), {})
            (Path(tmp) / ux.GEOMETRY_STATUS_FILE).write_text('{"status": "ready"}', encoding="utf-8")
            self.assertEqual(ux.load_geometry_status(tmp)["status"], "ready")
            (Path(tmp) / ux.GEOMETRY_STATUS_FILE).write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(ux.load_geometry_status(tmp), {})

    def test_building_chip_and_hints(self):
        self.assertEqual(ux.solve_chip(running=True, percent=60, building=True),
                         ("BUILDING GEOMETRY 60%", "running"))
        self.assertIn("Building geometry", ux.workspace_hint("output", solved=True, geometry="building"))
        hint = ux.workspace_hint("output", solved=True, geometry="failed", geometry_detail="out of memory")
        self.assertIn("out of memory", hint)
        self.assertIn("Build geometry again", hint)


class TrackTimeTests(unittest.TestCase):
    RESULT = {"timings": {
        "total_seconds_before_nuke_script": 546.2,
        "stages": [
            {"stage": "feature_extractor", "seconds": 40.0},
            {"stage": "patch_match_stereo", "seconds": 300.0},
            {"stage": "feature_extractor", "seconds": 10.0},
            ["mapper", 120.5],
            {"stage": "broken", "seconds": "x"},
            "nonsense",
        ],
    }}

    def test_result_timing(self):
        total, stages = ux.result_timing(self.RESULT)
        self.assertAlmostEqual(total, 546.2)
        self.assertEqual(stages, [("feature_extractor", 50.0), ("patch_match_stereo", 300.0), ("mapper", 120.5)])
        self.assertEqual(ux.result_timing({}), (None, []))

    def test_engine_only(self):
        info = ux.track_time(self.RESULT)
        self.assertAlmostEqual(info["total"], 546.2)
        self.assertIsNone(info["prep"])
        self.assertEqual(ux.track_time_text(info), "9m 06s")
        self.assertEqual(ux.track_time_text(ux.track_time({})), "")

    def test_with_nuke_prep(self):
        ui = ux.ui_timing_payload(12.4, 550.0, finished_unix=1)
        self.assertEqual(ui, {"kind": "track", "prep_seconds": 12.4, "engine_seconds": 550.0,
                              "total_seconds": 562.4, "finished_unix": 1})
        info = ux.track_time(self.RESULT, ui)
        self.assertAlmostEqual(info["total"], 562.4)
        self.assertAlmostEqual(info["prep"], 12.4)
        self.assertEqual(ux.track_time_text(info), "9m 22s")

    def test_timing_lines(self):
        info = ux.track_time(self.RESULT, ux.ui_timing_payload(12, 550))
        lines = ux.timing_lines(info)
        self.assertEqual(lines[0], ("Nuke prep (plate, mask, guides)", "12s"))
        self.assertEqual(lines[1], ("patch_match_stereo", "5m 00s"))  # longest first
        self.assertEqual(lines[-1], ("Total", "9m 22s"))
        self.assertEqual(ux.timing_lines({}), [])
        many = {"total": 10.0, "stages": [("s%d" % i, 1.0) for i in range(5)]}
        lines = ux.timing_lines(many, limit=2)
        self.assertEqual(lines[2], ("3 more stages", "3s"))

    def test_load_ui_timing(self):
        self.assertEqual(ux.load_ui_timing(""), {})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ux.load_ui_timing(tmp), {})
            (Path(tmp) / ux.UI_TIMING_FILE).write_text('{"track": {"engine_seconds": 5}}', encoding="utf-8")
            self.assertEqual(ux.load_ui_timing(tmp)["track"]["engine_seconds"], 5)

    def test_chip_shows_duration(self):
        self.assertEqual(ux.solve_chip(solved=True, error_px=0.3, duration="9m 06s"),
                         ("SOLVED · 0.30 px · 9m 06s", "solved"))
        self.assertEqual(ux.solve_chip(solved=True, duration="9m 06s"),
                         ("SOLVED · 9m 06s", "solved"))


class TimelineTests(unittest.TestCase):
    def test_tick_step(self):
        self.assertEqual(ux.timeline_tick_step(87, 1800), 5)
        self.assertEqual(ux.timeline_tick_step(87, 300), 25)
        self.assertEqual(ux.timeline_tick_step(10, 2000), 1)
        self.assertEqual(ux.timeline_tick_step(100000, 100), 10000)
        self.assertEqual(ux.timeline_tick_step(0, 0), ux.timeline_tick_step(1, 1))

    def test_shot_summary(self):
        self.assertEqual(ux.shot_summary(), "No plate")
        self.assertEqual(
            ux.shot_summary("Read1", 1920, 1080, 1001, 1087, "v2"),
            "Plate: Read1 · 1920×1080 · 1001–1087 · v2",
        )
        self.assertEqual(ux.shot_summary("Read1", 0, 0), "Plate: Read1")


class ThemeTests(unittest.TestCase):
    def test_colours(self):
        names = [n for n in dir(theme) if n.isupper() and isinstance(getattr(theme, n), str)]
        for name in names:
            value = getattr(theme, name)
            if value.startswith("#"):
                self.assertEqual(len(theme.rgb(value)), 3, name)
        self.assertEqual(theme.rgb("#27cfe8"), (39, 207, 232))
        with self.assertRaises(ValueError):
            theme.rgb("#fff")

    def test_stylesheet_formatted(self):
        # No unformatted {placeholders} left, braces balanced.
        self.assertNotRegex(theme.STYLESHEET, r"\{[a-z]+\}")
        self.assertEqual(theme.STYLESHEET.count("{"), theme.STYLESHEET.count("}"))
        for name in ("AutoLensWorkspace", "TopBar", "SidePanel", "BottomBar", "StatusBar",
                     "Step", "Primary", "Chip", "TrackButton", "Stop", "Toggle", "Disclosure"):
            self.assertIn(name, theme.STYLESHEET)


class WorkspaceSourceTests(unittest.TestCase):
    """dialog.py and icons.py import Qt, so check them from the source."""

    def test_icons_used_exist(self):
        icons_src = (PACKAGE / "icons.py").read_text(encoding="utf-8")
        tree = ast.parse(icons_src)
        names = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "NAMES" for t in node.targets):
                names = set(ast.literal_eval(node.value))
        self.assertTrue(names)
        for name in names:
            self.assertIn(f'name == "{name}"', icons_src.replace('name in ("', 'name == "').replace('", "', '" name == "'))
        dialog_src = (PACKAGE / "dialog.py").read_text(encoding="utf-8")
        used = set(re.findall(r'icons\.icon\(\s*"([a-z_]+)"', dialog_src))
        used |= set(re.findall(r'_icon_button\(\s*"([a-z_]+)"', dialog_src))
        used |= set(re.findall(r'button\(\s*"([a-z_]+)",\s*"', dialog_src))
        self.assertTrue(used)
        self.assertEqual(used - names, set())

    def test_every_step_has_a_page_and_tip(self):
        src = (PACKAGE / "dialog.py").read_text(encoding="utf-8")
        for key in ux.WORKSPACE_KEYS:
            self.assertIn(f'"{key}":', src)
        self.assertEqual(src.count("self.sidebar.addWidget("), len(ux.WORKSPACE_KEYS))


if __name__ == "__main__":
    unittest.main()
