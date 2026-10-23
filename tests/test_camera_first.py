"""Camera-first solves: direct COLMAP launches and guide fitting without a dense proxy.

Imports AutoLens/engine/autolens_engine.py directly; it needs numpy and OpenEXR
(the engine's own dependencies) but not COLMAP or Nuke.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ENGINE = os.path.join(os.path.dirname(__file__), "..", "AutoLens", "engine", "autolens_engine.py")

try:
    _spec = importlib.util.spec_from_file_location("autolens_engine", ENGINE)
    engine = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(engine)
    import numpy as np
except (ImportError, SystemExit) as exc:      # the engine exits if OpenEXR is missing
    engine = None
    _missing = str(exc)


def fake_env(root):
    """A conda env with a colmap executable where conda puts it on this OS."""
    prefix = Path(root) / "colmap_env"
    if os.name == "nt":
        exe = prefix / "Library" / "bin" / "colmap.exe"
    else:
        exe = prefix / "bin" / "colmap"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    conda = Path(root) / "conda.exe"
    conda.write_bytes(b"")
    launcher = Path(root) / "launcher.json"
    launcher.write_text(json.dumps({
        "type": "autolens-managed-colmap",
        "conda": str(conda),
        "env_prefix": str(prefix),
        "command": "colmap",
    }))
    return prefix, exe, conda, launcher


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class DirectColmapTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("AUTOLENS_COLMAP_VIA_CONDA", None)

    def tearDown(self):
        os.environ.pop("AUTOLENS_COLMAP_VIA_CONDA", None)
        if self._old is not None:
            os.environ["AUTOLENS_COLMAP_VIA_CONDA"] = self._old

    def test_launcher_runs_env_executable_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, exe, conda, launcher = fake_env(tmp)
            argv, _, env = engine.prepare_colmap_command([str(launcher), "mapper", "--x", "1"])
            self.assertEqual(Path(argv[0]), exe)
            self.assertEqual(argv[1:], ["mapper", "--x", "1"])
            path = env[next(k for k in env if k.upper() == "PATH")]
            self.assertIn(str(exe.parent), path.split(os.pathsep))
            self.assertEqual(env["CONDA_PREFIX"], str(prefix))

    def test_conda_route_is_still_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, conda, launcher = fake_env(tmp)
            os.environ["AUTOLENS_COLMAP_VIA_CONDA"] = "1"
            argv, _, env = engine.prepare_colmap_command([str(launcher), "mapper"])
            self.assertEqual(argv[:2], [str(conda), "run"])
            self.assertIsNone(env)

    def test_missing_executable_falls_back_to_conda(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, exe, conda, launcher = fake_env(tmp)
            exe.unlink()
            argv, _, _ = engine.prepare_colmap_command([str(launcher), "mapper"])
            self.assertEqual(argv[:2], [str(conda), "run"])

    def test_nuke_qt_and_python_variables_are_dropped(self):
        env = engine.direct_colmap_environment(
            "/env", {"PATH": "/usr/bin", "QT_PLUGIN_PATH": "/nuke/qt", "PYTHONPATH": "/nuke/py", "HOME": "/h"},
        )
        self.assertNotIn("QT_PLUGIN_PATH", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["HOME"], "/h")
        self.assertTrue(env["PATH"].endswith("/usr/bin"))


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class GeometryStatusTests(unittest.TestCase):
    def read(self, folder):
        return json.loads((Path(folder) / engine.GEOMETRY_STATUS_FILE).read_text())

    def test_building_progress_then_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine.write_geometry_status(tmp, "building", step=7, total=engine.TOTAL_STEPS, stage="Preparing")
            self.assertEqual(self.read(tmp)["status"], "building")
            engine.show_progress(8, "Computing dense depth maps")
            state = self.read(tmp)
            self.assertEqual((state["status"], state["step"], state["stage"]),
                             ("building", 8, "Computing dense depth maps"))
            engine.write_geometry_status(tmp, "ready", proxy_mesh_obj="proxy.obj")
            self.assertEqual(self.read(tmp)["status"], "ready")
            # Once finished, progress banners no longer touch the file.
            engine.show_progress(9, "Later")
            self.assertEqual(self.read(tmp)["proxy_mesh_obj"], "proxy.obj")

    def test_failed_carries_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine.write_geometry_status(tmp, "failed", error="boom")
            state = self.read(tmp)
            self.assertEqual((state["status"], state["error"]), ("failed", "boom"))
            self.assertIn("pid", state)


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class SparseOnlyGuideTests(unittest.TestCase):
    """A camera looking down +Z at a textured wall 5 units away; no dense proxy."""

    CAM = {"model": "RADIAL", "width": 1920, "height": 1080, "f_px": 1000.0,
           "cx": 960.0, "cy": 540.0, "k1": 0.0, "k2": 0.0}

    def scene(self, tmp):
        rng = np.random.default_rng(4)
        xyz = np.column_stack([rng.uniform(-3, 3, 400), rng.uniform(-2, 2, 400), np.full(400, 5.0)])
        points = [{"id": i + 1, "xyz": p.tolist(), "rgb": [128, 128, 128], "error": 0.3, "track_length": 3}
                  for i, p in enumerate(xyz)]
        u = self.CAM["f_px"] * xyz[:, 0] / xyz[:, 2] + self.CAM["cx"]
        v = self.CAM["f_px"] * xyz[:, 1] / xyz[:, 2] + self.CAM["cy"]
        images_txt = Path(tmp) / "images.txt"
        obs = " ".join(f"{a:.3f} {b:.3f} {i + 1}" for i, (a, b) in enumerate(zip(u, v)))
        images_txt.write_text(f"1 1 0 0 0 0 0 0 1 0001.png\n{obs}\n", encoding="utf-8")
        poses = engine.parse_colmap_images(images_txt, [Path("0001.png")])
        guide = Path(tmp) / "guides.json"
        guide.write_text(json.dumps({
            "width": 1920, "height": 1080, "coordinate_system": "colmap_top_left_pixels", "frame": 1,
            "surfaces": [{"name": "Wall", "points": [[600, 300], [1300, 300], [1300, 800], [600, 800]]}],
        }), encoding="utf-8")
        return points, images_txt, poses, guide

    def test_guides_fit_without_dense_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            points, images_txt, poses, guide = self.scene(tmp)
            obj, report, meta = engine.build_guided_surfaces(
                guide, dict(self.CAM), poses, points, images_txt,
                np.zeros((0, 3)), [], Path(tmp),
            )
            self.assertIsNotNone(obj)
            self.assertEqual(meta["successful_surface_count"], 1)
            text = Path(obj).read_text()
            verts = np.array([[float(x) for x in line.split()[1:4]]
                              for line in text.splitlines() if line.startswith("v ")])
            # Every corner lies on the z = 5 wall the sparse points came from.
            np.testing.assert_allclose(np.abs(verts[:, 2]), 5.0, atol=0.05)


if __name__ == "__main__":
    unittest.main()
