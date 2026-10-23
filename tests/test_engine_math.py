"""The engine's pure maths: lens model, camera basis and COLMAP parsing.

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
    import numpy as np
except (ImportError, SystemExit) as exc:      # the engine exits if OpenEXR is missing
    engine = None
    _missing = str(exc)


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class LensTests(unittest.TestCase):
    CAM = {"model": "RADIAL", "width": 1920, "height": 1080, "f_px": 1600.0,
           "cx": 960.0, "cy": 540.0, "k1": -0.08, "k2": 0.01}

    def test_radial_inverse_undoes_forward(self):
        x = np.linspace(-0.6, 0.6, 41)
        y = np.linspace(0.3, -0.3, 41)
        xd, yd = engine.radial_forward(x, y, self.CAM["k1"], self.CAM["k2"])
        xu, yu = engine.radial_inverse(xd, yd, self.CAM["k1"], self.CAM["k2"])
        np.testing.assert_allclose(xu, x, atol=1e-6)
        np.testing.assert_allclose(yu, y, atol=1e-6)

    def test_nuke_lens_fit_is_subpixel(self):
        model = engine.fit_nuke_lens_model(dict(self.CAM))
        self.assertEqual(model["model_type"], "Radial Standard")
        self.assertLess(model["fit"]["rms_error_px"], 0.5)
        self.assertLess(model["fit"]["max_abs_error_px"], 1.0)

    def test_only_radial_is_accepted(self):
        with self.assertRaises(RuntimeError):
            engine.fit_nuke_lens_model(dict(self.CAM, model="OPENCV"))


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class CameraTests(unittest.TestCase):
    def test_identity_pose_looks_down_minus_z_in_nuke(self):
        m = engine.colmap_pose_to_nuke_matrix((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 5.0))
        # COLMAP world->camera t=(0,0,5) puts the camera centre at (0,0,-5).
        np.testing.assert_allclose(m[:3, 3], [0.0, 0.0, -5.0])
        # Nuke -Z (forward) must map onto COLMAP +Z (forward).
        np.testing.assert_allclose(m[:3, :3] @ [0.0, 0.0, -1.0], [0.0, 0.0, 1.0])
        np.testing.assert_allclose(m[:3, :3] @ [0.0, 1.0, 0.0], [0.0, -1.0, 0.0])

    def test_quaternion_is_normalised_and_orthonormal(self):
        r = engine.quaternion_to_rotation_matrix(2.0, 0.4, -0.6, 1.0)
        np.testing.assert_allclose(r @ r.T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(float(np.linalg.det(r)), 1.0, places=12)
        with self.assertRaises(RuntimeError):
            engine.quaternion_to_rotation_matrix(0.0, 0.0, 0.0, 0.0)

    def test_parse_frame_number(self):
        self.assertEqual(engine.parse_frame_number("plate_v002.1001.exr", 7), 1001)
        self.assertEqual(engine.parse_frame_number("no_digits.png", 7), 7)


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class ColmapTextTests(unittest.TestCase):
    def test_parse_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cameras.txt"
            path.write_text("# comment\n1 RADIAL 1920 1080 1600 960 540 -0.08 0.01\n")
            cam = engine.parse_camera(path)
            self.assertEqual((cam["width"], cam["height"]), (1920, 1080))
            self.assertAlmostEqual(cam["k1"], -0.08)
            path.write_text("1 PINHOLE 1920 1080 1600 1600 960 540 0\n")
            with self.assertRaises(RuntimeError):
                engine.parse_camera(path)


if __name__ == "__main__":
    unittest.main()
