"""The engine's vectorised hot loops give the same answers as the loops they replaced.

Each test runs the optimised function and a plain reference version of the old
code on synthetic data. Imports AutoLens/engine/autolens_engine.py directly; it
needs numpy and OpenEXR but not COLMAP or Nuke.
"""
import importlib.util
import math
import os
import random
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


def write_images_txt(path, images):
    """images: list of (name, [(x, y, point_id), ...])."""
    lines = ["# Image list with two lines of data per image:"]
    for i, (name, obs) in enumerate(images, 1):
        lines.append(f"{i} 1 0 0 0 0 0 0 1 {name}")
        lines.append(" ".join(f"{x} {y} {pid}" for x, y, pid in obs))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def reference_observations(images, width, height, grid_x=8, grid_y=5):
    """The per-point loop parse_image_observations used to run."""
    result = {}
    for name, obs in images:
        point_ids, occupied = set(), set()
        for x, y, pid in obs:
            if pid < 0:
                continue
            point_ids.add(pid)
            gx = int(np.clip((x / max(width, 1)) * grid_x, 0, grid_x - 1))
            gy = int(np.clip((y / max(height, 1)) * grid_y, 0, grid_y - 1))
            occupied.add((gx, gy))
        result[name] = {
            "point_ids": point_ids,
            "point_count": len(point_ids),
            "grid_coverage": len(occupied) / float(grid_x * grid_y),
            "occupied_grid_cells": len(occupied),
        }
    return result


def synthetic_shot(rng, frames=40, points=600):
    """A sliding window of scene points seen by a camera dollying and panning."""
    poses, images = [], []
    for i in range(frames):
        yaw = math.radians(0.8 * i + rng.uniform(-0.3, 0.3))
        rot = np.array([
            [math.cos(yaw), 0.0, math.sin(yaw)],
            [0.0, 1.0, 0.0],
            [-math.sin(yaw), 0.0, math.cos(yaw)],
        ])
        matrix = np.eye(4)
        matrix[:3, :3] = rot
        center = [0.05 * i + rng.uniform(-0.01, 0.01), rng.uniform(-0.01, 0.01), 0.02 * i]
        matrix[:3, 3] = center
        name = f"frame.{1001 + i:04d}.jpg"
        poses.append({"name": name, "center": center, "nuke_matrix": matrix.tolist()})
        lo = int(i * points / (frames + 8))
        visible = [p for p in range(lo, min(points, lo + 140)) if rng.random() < 0.8]
        obs = [(rng.uniform(0, 1920), rng.uniform(0, 1080), p) for p in visible]
        obs += [(rng.uniform(0, 1920), rng.uniform(0, 1080), -1) for _ in range(30)]
        rng.shuffle(obs)
        images.append((name, obs))
    return poses, images


def reference_smart_views(poses, observations, target_frames, min_frames, coverage_target):
    """The old greedy selector: every round rescores each candidate against every selected view."""
    names = [p["name"] for p in poses if p["name"] in observations]
    target_frames = min(max(4, int(target_frames)), len(names))
    min_frames = min(max(4, int(min_frames)), target_frames)
    centers, forwards, frame_order = engine._pose_maps(poses)
    all_points = set()
    for n in names:
        all_points |= observations[n]["point_ids"]
    max_count = max(max(observations[n]["point_count"] for n in names), 1.0)
    center_array = np.asarray([centers[n] for n in names])
    max_baseline = max(
        float(np.max(np.linalg.norm(center_array[:, None] - center_array[None], axis=2))),
        1.0e-12,
    )
    quality = {
        n: 0.45 * math.sqrt(observations[n]["point_count"] / max_count)
        + 0.55 * observations[n]["grid_coverage"]
        for n in names
    }
    first = max(names, key=lambda n: quality[n])
    selected, covered = [first], set(observations[first]["point_ids"])
    while len(selected) < target_frames:
        unselected = [n for n in names if n not in selected]
        if not unselected:
            break
        gains = {n: len(observations[n]["point_ids"] - covered) for n in unselected}
        max_gain = max(gains.values())
        if (
            len(selected) >= min_frames
            and len(covered) / float(len(all_points)) >= coverage_target
            and max_gain / float(len(all_points)) < 0.01
        ):
            break
        best_name, best_score = None, -1.0
        for name in unselected:
            overlap = max(
                engine._pair_geometry(name, c, centers, forwards, observations, max_baseline)["overlap"]
                for c in selected
            )
            nearest = min(np.linalg.norm(centers[name] - centers[c]) for c in selected)
            dot = max(float(np.dot(forwards[name], forwards[c])) for c in selected)
            score = (
                0.42 * gains[name] / float(max(max_gain, 1))
                + 0.20 * min(1.0, float(nearest) / max_baseline)
                + 0.18 * min(1.0, overlap / 0.65)
                + 0.12 * quality[name]
                + 0.08 * min(1.0, float(np.degrees(np.arccos(np.clip(dot, -1.0, 1.0)))) / 45.0)
            )
            if overlap < 0.05 and len(selected) >= 2:
                score *= 0.35
            if score > best_score:
                best_name, best_score = name, score
        covered |= observations[best_name]["point_ids"]
        selected.append(best_name)
    return sorted(selected, key=lambda n: frame_order[n])


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class ObservationParsingTests(unittest.TestCase):
    def test_vectorised_parse_matches_loop(self):
        rng = random.Random(7)
        _, images = synthetic_shot(rng, frames=12)
        # Points exactly on and beyond the image border exercise the clipping.
        images.append(("edge.jpg", [(0.0, 0.0, 1), (1920.0, 1080.0, 2), (-5.0, 2000.0, 3), (12.5, 7.25, -1)]))
        images.append(("empty.jpg", []))
        with tempfile.TemporaryDirectory() as tmp:
            images_txt = Path(tmp) / "images.txt"
            write_images_txt(images_txt, images)
            parsed = engine.parse_image_observations(images_txt, 1920, 1080)
            xy = engine.parse_image_xy_point_observations(images_txt)
        self.assertEqual(parsed, reference_observations(images, 1920, 1080))
        for name, obs in images:
            expected = [(float(x), float(y), pid) for x, y, pid in obs if pid >= 0]
            self.assertEqual(xy[name], expected)

    def test_malformed_triples_fall_back_to_loop(self):
        self.assertIsNone(engine.parse_xy_id_triples("1.0 2.0 5 bad 3.0 6"))
        xs, ys, ids = engine.parse_xy_id_triples("1.0 2.0 5 3.0 4.0 -1 7.5 8.5 9 1.0")
        self.assertEqual(ids.tolist(), [5, 9])
        self.assertEqual(xs.tolist(), [1.0, 7.5])
        self.assertEqual(ys.tolist(), [2.0, 8.5])


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class SmartViewSelectionTests(unittest.TestCase):
    def test_incremental_selector_matches_full_rescoring(self):
        for seed in range(6):
            rng = random.Random(seed)
            poses, images = synthetic_shot(rng, frames=30 + 5 * seed)
            observations = reference_observations(images, 1920, 1080)
            for target, minimum, coverage in ((12, 6, 0.97), (8, 5, 0.90), (18, 8, 0.99)):
                got = engine.choose_smart_dense_views(poses, observations, target, minimum, coverage)
                want = reference_smart_views(poses, observations, target, minimum, coverage)
                self.assertEqual(got["selected_names"], want, msg=f"seed={seed} target={target}")


@unittest.skipIf(engine is None, "engine dependencies missing (pip install numpy OpenEXR)")
class MeshTests(unittest.TestCase):
    @staticmethod
    def grid_mesh(n=12, rng=None):
        verts, tris = [], []
        for j in range(n):
            for i in range(n):
                z = 0.0 if rng is None else rng.uniform(-0.01, 0.01)
                verts.append((float(i), float(j), z))
        for j in range(n - 1):
            for i in range(n - 1):
                a = j * n + i
                tris.append([a, a + 1, a + n])
                tris.append([a + 1, a + n + 1, a + n])
        return np.asarray(verts, dtype=np.float64), tris

    def test_binary_ply_round_trip_uses_fast_reader(self):
        verts, tris = self.grid_mesh()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mesh.ply"
            engine.write_ply_mesh(path, verts, tris)
            v, faces = engine.read_ply_mesh(path)
        self.assertEqual(faces, tris)
        np.testing.assert_allclose(v, verts.astype(np.float32))

    def test_mixed_polygon_ply_falls_back(self):
        # One quad among triangles: the fast reader must rewind and the slow
        # reader must still return every face.
        header = (
            "ply\nformat binary_little_endian 1.0\n"
            "element vertex 5\nproperty float x\nproperty float y\nproperty float z\n"
            "element face 2\nproperty list uchar int vertex_indices\nend_header\n"
        ).encode("ascii")
        body = np.arange(15, dtype="<f4").tobytes()
        body += bytes([3]) + np.array([0, 1, 2], dtype="<i4").tobytes()
        body += bytes([4]) + np.array([1, 2, 3, 4], dtype="<i4").tobytes()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.ply"
            path.write_bytes(header + body)
            _, faces = engine.read_ply_mesh(path)
        self.assertEqual(faces, [[0, 1, 2], [1, 2, 3, 4]])
        self.assertEqual(engine.triangulate_faces(faces).tolist(), [[0, 1, 2], [1, 2, 3], [1, 3, 4]])

    def test_triangle_adjacency_matches_dict_version(self):
        rng = random.Random(3)
        verts, tris = self.grid_mesh(15, rng)
        tris = np.asarray(tris + [[0, 1, 200]], dtype=np.int64)   # a non-manifold fan on edge 0-1
        fast = engine.build_triangle_adjacency(tris)
        slow = engine.build_triangle_adjacency_slow(tris)
        self.assertEqual([sorted(x) for x in fast], [sorted(x) for x in slow])

    def test_meshfit_surfaces_unchanged(self):
        rng = random.Random(11)
        verts, tris = self.grid_mesh(20, rng)
        poses = [{"center": [0.0, 0.0, 5.0], "nuke_matrix": np.eye(4).tolist()}]
        triangles = engine.triangulate_faces(tris)
        triangles, *_ = engine.mesh_triangle_geometry(verts, triangles)
        slow_adj = engine.build_triangle_adjacency_slow(triangles)
        self.assertEqual(
            [sorted(x) for x in engine.build_triangle_adjacency(triangles)],
            [sorted(x) for x in slow_adj],
        )
        fast, _ = engine.extract_meshfit_surfaces(verts, tris, poses, min_faces=10)
        original = engine.build_triangle_adjacency
        engine.build_triangle_adjacency = engine.build_triangle_adjacency_slow
        try:
            slow, _ = engine.extract_meshfit_surfaces(verts, tris, poses, min_faces=10)
        finally:
            engine.build_triangle_adjacency = original
        self.assertGreaterEqual(len(fast), 1)
        self.assertEqual(len(fast), len(slow))
        for a, b in zip(fast, slow):
            self.assertEqual(a["face_count"], b["face_count"])
            self.assertEqual(a["vertex_count"], b["vertex_count"])
            self.assertAlmostEqual(a["area"], b["area"], places=9)
            np.testing.assert_allclose(a["corners"], b["corners"], atol=1e-9)

    def test_cleanup_local_scale_matches_add_at(self):
        rng = np.random.default_rng(5)
        pairs = rng.integers(0, 50, size=(400, 2))
        lengths = rng.random(400)
        want = np.zeros(50)
        np.add.at(want, pairs[:, 0], lengths)
        np.add.at(want, pairs[:, 1], lengths)
        got = np.bincount(
            np.concatenate([pairs[:, 0], pairs[:, 1]]),
            weights=np.concatenate([lengths, lengths]),
            minlength=50,
        )
        self.assertTrue(np.array_equal(got, want))

    def test_cleanup_keeps_clean_grid(self):
        verts, tris = self.grid_mesh(10)
        clean_v, clean_t, report = engine.cleanup_proxy_spanning_triangles(verts, tris)
        self.assertEqual(report["removed_total_triangles"], 0)
        self.assertEqual(len(clean_t), len(tris))


if __name__ == "__main__":
    unittest.main()
