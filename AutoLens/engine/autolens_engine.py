#!/usr/bin/env python3
"""
AutoLens V0.8.3
===============

Smart-view dense reconstruction for Nuke-oriented VFX plate reconstruction.

V0.6 keeps every registered movie frame for the sparse lens/camera solve, then
analyzes the solved camera trajectory and sparse 3D observations to choose the
smallest useful set of dense photogrammetry reference views.

The selector evaluates:
- unique sparse-scene coverage
- camera baseline / viewpoint separation
- shared 3D-track overlap
- feature distribution across the image
- camera path shape (push/pull, lateral, orbit-like, rotation-dominant)

The expensive dense solve can therefore spend substantially more resolution on
a small set of geometrically useful frames rather than processing redundant
adjacent movie frames.

Default reconstruction geometry:
    autolens_proxy_mesh.obj
    autolens_meshfit_planes.obj

Optional user-guided geometry:
    autolens_guided_surfaces.obj

Essential outputs:
    autolens_undistort.exr
    autolens_redistort.exr
    autolens_camera_track.json
    autolens_result.json
    AutoLens_v08.nk

Requirements:
- Python 3.9+
- numpy
- OpenEXR
- COLMAP
"""


from __future__ import annotations

import argparse
import hashlib
import math
from collections import defaultdict, deque
import json
import re
import shutil
import subprocess
import struct
import sys
import time
import os
import ctypes
from pathlib import Path

import numpy as np

try:
    import OpenEXR
    import Imath
except Exception as exc:
    raise SystemExit(
        "AutoLens requires the Python packages OpenEXR and Imath.\n"
        "Install with:\n"
        "    pip install numpy OpenEXR\n\n"
        f"Import error: {exc}"
    )

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".exr"
}


TOTAL_STEPS = 11


def show_progress(step: int, label: str, detail: str | None = None) -> None:
    """Print a conspicuous high-level AutoLens progress banner."""
    width = 30
    completed = int(round((step / TOTAL_STEPS) * width))
    bar = "#" * completed + "-" * (width - completed)
    pct = int(round((step / TOTAL_STEPS) * 100))

    print("\n")
    print("=" * 72)
    print(f"AUTOLENS STATUS  [{bar}]  {step}/{TOTAL_STEPS}  ({pct}%)")
    print(f">>> {label}")
    if detail:
        print(f"    {detail}")
    print("=" * 72)
    print()


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {sec:02d}s"



def colmap_root(value: str) -> Path | None:
    if not value:
        return None
    p = Path(value)
    if not p.exists():
        return None
    p = p.resolve()
    if p.suffix.lower() in (".bat", ".cmd"):
        return p.parent
    if p.name.lower() == "colmap.exe" and p.parent.name.lower() == "bin":
        return p.parent.parent
    return p.parent


def normalize_colmap_executable(value: str) -> str:
    if not value:
        return value

    p = Path(value)
    if not p.exists():
        return value
    p = p.resolve()

    if os.name == "nt":
        root = colmap_root(str(p))
        if root is not None:
            for candidate in (
                root / "COLMAP.bat",
                root / "colmap.bat",
                root / "COLMAP.cmd",
                root / "colmap.cmd",
            ):
                if candidate.exists():
                    return str(candidate.resolve())

    return str(p)


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_exe() -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.exists():
        return str(candidate)
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"


def prepare_colmap_command(cmd: list[str], cwd: Path | None = None):
    """
    Resolve AutoLens-managed COLMAP without any nested batch wrapper.

    The installer writes:
        ~/.nuke/AutoLens/.managed_colmap/launcher.json

    which points to Miniforge's conda.exe and the private COLMAP environment.
    We invoke the exact command that the installer already validated:

        conda.exe run -p <env> --no-capture-output colmap <subcommand> ...
    """
    if not cmd:
        raise RuntimeError("Empty command.")

    executable = str(cmd[0])
    args = [str(x) for x in cmd[1:]]
    p = Path(executable)

    # Managed v0.9.4+ launcher descriptor.
    if p.suffix.lower() == ".json" and p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Could not read AutoLens COLMAP launcher descriptor: {exc}"
            )

        if data.get("type") == "autolens-managed-colmap":
            conda = str(data.get("conda") or "")
            env_prefix = str(data.get("env_prefix") or "")
            command = str(data.get("command") or "colmap")

            if not conda or not Path(conda).exists():
                raise RuntimeError(
                    f"Managed Conda executable is missing: {conda}"
                )

            if not env_prefix or not Path(env_prefix).exists():
                raise RuntimeError(
                    f"Managed COLMAP environment is missing: {env_prefix}"
                )

            argv = [
                conda,
                "run",
                "-p", env_prefix,
                "--no-capture-output",
                command,
                *args,
            ]
            return argv, (str(cwd) if cwd else None), None

    if os.name != "nt":
        return [executable] + args, (str(cwd) if cwd else None), None

    # Legacy fallback for explicitly supplied external COLMAP installations.
    p = Path(executable)
    if p.suffix.lower() in (".cmd", ".bat"):
        comspec = os.environ.get(
            "COMSPEC",
            r"C:\Windows\System32\cmd.exe"
        )
        command_line = "call " + subprocess.list2cmdline(
            [str(p)] + args
        )
        return (
            [comspec, "/d", "/s", "/c", command_line],
            str(cwd) if cwd else str(p.parent),
            None,
        )

    return [executable] + args, (str(cwd) if cwd else None), None


def _windows_status(returncode: int) -> str:
    return f"0x{int(returncode) & 0xFFFFFFFF:08X}"


def run(cmd: list[str], cwd: Path | None = None, label: str | None = None) -> None:
    print("\n>>>", " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
    started = time.perf_counter()

    argv, actual_cwd, env = prepare_colmap_command(cmd, cwd)
    proc = subprocess.run(argv, cwd=actual_cwd, env=env)

    elapsed = time.perf_counter() - started
    stage_name = label or (cmd[1] if len(cmd) > 1 else cmd[0])
    print(f"\n[AUTOLENS TIMING] {stage_name}: {format_duration(elapsed)}")

    if proc.returncode != 0:
        code = _windows_status(proc.returncode)
        hint = ""
        if code == "0xC0000142":
            hint = (
                "\nWindows DLL initialization failed (0xC0000142). "
                "AutoLens launched the official COLMAP.bat wrapper through a clean "
                "cmd.exe environment and removed Nuke/Foundry Qt paths. "
                "Verify COLMAP.bat starts from a normal Command Prompt."
            )
        raise RuntimeError(
            f"Command failed with exit code {proc.returncode} ({code}){hint}"
        )


def run_try(cmd: list[str], cwd: Path | None = None, label: str | None = None) -> bool:
    """Run a COLMAP command but return False instead of aborting on failure."""
    print("\n>>>", " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
    started = time.perf_counter()

    argv, actual_cwd, env = prepare_colmap_command(cmd, cwd)
    proc = subprocess.run(argv, cwd=actual_cwd, env=env)

    elapsed = time.perf_counter() - started
    stage_name = label or (cmd[1] if len(cmd) > 1 else cmd[0])
    print(f"\n[AUTOLENS TIMING] {stage_name}: {format_duration(elapsed)}")
    return proc.returncode == 0


def find_images(folder: Path) -> list[Path]:
    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if len(files) < 3:
        raise RuntimeError(
            f"Need at least 3 images. Found {len(files)} in {folder}"
        )
    return files


def write_colmap_image_list(
    output: Path,
    images_dir: Path,
    images: list[Path],
) -> Path:
    """
    Restrict COLMAP feature extraction to the actual source sequence.

    This is essential when the AutoLens output folder lives inside the plate
    directory: COLMAP otherwise recursively discovers AutoLens databases,
    JSON files, STMaps, dense images, etc.
    """
    path = output / "autolens_colmap_image_list.txt"

    names = []
    root = images_dir.resolve()

    for image in images:
        try:
            relative = image.resolve().relative_to(root)
        except Exception:
            relative = Path(image.name)

        # COLMAP image-list paths use forward slashes even on Windows.
        names.append(str(relative).replace("\\", "/"))

    path.write_text(
        "\n".join(names) + "\n",
        encoding="utf-8",
    )
    return path



def locate_colmap(explicit: str | None) -> str:
    if explicit:
        p = Path(explicit)
        if p.exists():
            # AutoLens managed launcher descriptor.
            if p.suffix.lower() == ".json":
                return str(p.resolve())
            return normalize_colmap_executable(str(p))

        found = shutil.which(explicit)
        if found:
            return normalize_colmap_executable(found)

        raise RuntimeError(f"Could not find COLMAP launcher: {explicit}")

    for candidate in ("COLMAP.bat", "colmap.bat", "colmap.exe", "colmap"):
        found = shutil.which(candidate)
        if found:
            return normalize_colmap_executable(found)

    raise RuntimeError(
        "COLMAP was not found. Run the AutoLens installer or pass --colmap."
    )


def iter_models(sparse_root: Path):
    candidates = [p for p in sparse_root.iterdir() if p.is_dir()]
    if not candidates:
        raise RuntimeError("COLMAP created no sparse reconstruction.")

    for model_dir in candidates:
        txt_dir = model_dir / "txt"
        txt_dir.mkdir(exist_ok=True)
        yield model_dir, txt_dir


def count_registered_images(images_txt: Path) -> int:
    if not images_txt.exists():
        return 0

    with images_txt.open("r", encoding="utf-8", errors="replace") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    return len(lines) // 2


def parse_camera(camera_txt: Path) -> dict:
    """
    Expected RADIAL camera line:
    CAMERA_ID RADIAL WIDTH HEIGHT f cx cy k1 k2
    """
    with camera_txt.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 9:
                continue

            camera_id = int(parts[0])
            model = parts[1]
            width = int(parts[2])
            height = int(parts[3])
            params = list(map(float, parts[4:]))

            if model != "RADIAL":
                raise RuntimeError(
                    f"Expected RADIAL model, got {model}. "
                    "Delete the output folder and rerun."
                )

            if len(params) != 5:
                raise RuntimeError(
                    f"Expected RADIAL params [f,cx,cy,k1,k2], got {params}"
                )

            f_px, cx, cy, k1, k2 = params
            return {
                "camera_id": camera_id,
                "model": model,
                "width": width,
                "height": height,
                "f_px": f_px,
                "cx": cx,
                "cy": cy,
                "k1": k1,
                "k2": k2,
            }

    raise RuntimeError(f"No camera found in {camera_txt}")




def parse_frame_number(image_name: str, fallback: int) -> int:
    stem = Path(image_name).stem
    match = re.search(r"(\d+)(?!.*\d)", stem)
    if match:
        return int(match.group(1))
    return fallback


def quaternion_to_rotation_matrix(qw: float, qx: float, qy: float, qz: float) -> np.ndarray:
    q = np.array([qw, qx, qy, qz], dtype=np.float64)
    norm = np.linalg.norm(q)
    if norm <= 1.0e-12:
        raise RuntimeError("Encountered a zero-length camera quaternion.")
    qw, qx, qy, qz = q / norm

    return np.array([
        [1.0 - 2.0 * (qy*qy + qz*qz), 2.0 * (qx*qy - qz*qw),       2.0 * (qx*qz + qy*qw)],
        [2.0 * (qx*qy + qz*qw),       1.0 - 2.0 * (qx*qx + qz*qz), 2.0 * (qy*qz - qx*qw)],
        [2.0 * (qx*qz - qy*qw),       2.0 * (qy*qz + qx*qw),       1.0 - 2.0 * (qx*qx + qy*qy)],
    ], dtype=np.float64)


def colmap_pose_to_nuke_matrix(qvec, tvec) -> np.ndarray:
    qw, qx, qy, qz = qvec
    t = np.asarray(tvec, dtype=np.float64)

    r_wc = quaternion_to_rotation_matrix(qw, qx, qy, qz)
    r_cw = r_wc.T
    center = -r_cw @ t

    # COLMAP local camera: +X right, +Y down, +Z forward
    # Nuke local camera:   +X right, +Y up,   -Z forward
    basis_flip = np.diag([1.0, -1.0, -1.0])
    r_nuke_to_world = r_cw @ basis_flip

    m = np.eye(4, dtype=np.float64)
    m[:3, :3] = r_nuke_to_world
    m[:3, 3] = center
    return m


def parse_colmap_images(images_txt: Path, source_images: list[Path]) -> list[dict]:
    source_order = {
        p.name.replace("\\", "/"): i + 1
        for i, p in enumerate(source_images)
    }

    poses = []
    with images_txt.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 10:
                continue

            try:
                image_id = int(parts[0])
                camera_id = int(parts[8])
                qvec = [float(v) for v in parts[1:5]]
                tvec = [float(v) for v in parts[5:8]]
            except (ValueError, IndexError):
                continue

            name = " ".join(parts[9:])
            fallback = source_order.get(Path(name).name, len(poses) + 1)
            frame = parse_frame_number(Path(name).name, fallback)
            matrix = colmap_pose_to_nuke_matrix(qvec, tvec)

            poses.append({
                "image_id": image_id,
                "camera_id": camera_id,
                "name": name,
                "frame": frame,
                "qvec": qvec,
                "tvec": tvec,
                "center": matrix[:3, 3].tolist(),
                "nuke_matrix": matrix.tolist(),
            })

    poses.sort(key=lambda p: (p["frame"], p["name"]))
    if not poses:
        raise RuntimeError(f"No registered camera poses found in {images_txt}")
    return poses


def _rotation_align_vectors(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)

    a /= max(np.linalg.norm(a), 1.0e-12)
    b /= max(np.linalg.norm(b), 1.0e-12)

    cross = np.cross(a, b)
    dot = float(np.clip(np.dot(a, b), -1.0, 1.0))
    s = float(np.linalg.norm(cross))

    if s < 1.0e-10:
        if dot > 0.0:
            return np.eye(3, dtype=np.float64)

        # 180-degree rotation around any axis perpendicular to a.
        basis = np.asarray([1.0, 0.0, 0.0])
        if abs(float(np.dot(a, basis))) > 0.9:
            basis = np.asarray([0.0, 0.0, 1.0])

        axis = np.cross(a, basis)
        axis /= max(np.linalg.norm(axis), 1.0e-12)

        # Rodrigues for theta=pi.
        k = np.asarray([
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ])
        return (
            np.eye(3)
            + 2.0 * (k @ k)
        )

    axis = cross / s
    k = np.asarray([
        [0.0, -axis[2], axis[1]],
        [axis[2], 0.0, -axis[0]],
        [-axis[1], axis[0], 0.0],
    ])

    angle = math.atan2(s, dot)

    return (
        np.eye(3)
        + math.sin(angle) * k
        + (1.0 - math.cos(angle)) * (k @ k)
    )


def compute_upright_world_rotation(poses: list[dict]):
    """
    Use the robust average camera-up axis to remove COLMAP's arbitrary world
    rotation. This does not affect reprojection; it only makes exported 3D
    coordinates artist-friendly.
    """
    ups = []

    for pose in poses:
        m = np.asarray(
            pose["nuke_matrix"],
            dtype=np.float64,
        )

        up = m[:3, 1]
        norm = np.linalg.norm(up)

        if norm > 1.0e-10:
            ups.append(up / norm)

    if not ups:
        return np.eye(3, dtype=np.float64)

    # Align signs before averaging, just in case one pose contains a numerical
    # 180-degree ambiguity.
    reference = ups[0]
    aligned = []

    for up in ups:
        if np.dot(up, reference) < 0.0:
            up = -up
        aligned.append(up)

    average_up = np.mean(
        np.asarray(aligned, dtype=np.float64),
        axis=0,
    )
    average_up /= max(
        np.linalg.norm(average_up),
        1.0e-12,
    )

    return _rotation_align_vectors(
        average_up,
        np.asarray([0.0, 1.0, 0.0]),
    )


def apply_world_rotation_to_poses(
    poses: list[dict],
    rotation: np.ndarray,
):
    result = []

    for pose in poses:
        item = dict(pose)

        m = np.asarray(
            pose["nuke_matrix"],
            dtype=np.float64,
        ).copy()

        m[:3, :3] = rotation @ m[:3, :3]
        m[:3, 3] = rotation @ m[:3, 3]

        item["nuke_matrix"] = m.tolist()
        item["center"] = m[:3, 3].tolist()

        result.append(item)

    return result


def apply_world_rotation_to_points(
    points: list[dict],
    rotation: np.ndarray,
):
    result = []

    for point in points:
        item = dict(point)
        xyz = np.asarray(
            point["xyz"],
            dtype=np.float64,
        )
        item["xyz"] = (
            rotation @ xyz
        ).tolist()
        result.append(item)

    return result


def apply_world_rotation_to_vertices(
    vertices: np.ndarray,
    rotation: np.ndarray,
):
    vertices = np.asarray(
        vertices,
        dtype=np.float64,
    )
    return vertices @ rotation.T



def parse_points3d(points_txt: Path) -> list[dict]:
    points = []
    with points_txt.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 8:
                continue

            try:
                point_id = int(parts[0])
                x, y, z = map(float, parts[1:4])
                r, g, b = map(int, parts[4:7])
                error = float(parts[7])
            except ValueError:
                continue

            track_length = max((len(parts) - 8) // 2, 0)
            points.append({
                "id": point_id,
                "xyz": [x, y, z],
                "rgb": [r, g, b],
                "error": error,
                "track_length": track_length,
            })

    if not points:
        raise RuntimeError(f"No sparse 3D points found in {points_txt}")
    return points


def write_sparse_obj(path: Path, points: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("# AutoLens sparse COLMAP reconstruction\n")
        f.write(f"# {len(points)} vertices\n")
        for p in points:
            x, y, z = p["xyz"]
            f.write(f"v {x:.10g} {y:.10g} {z:.10g}\n")


def write_sparse_ply(path: Path, points: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write("ply\n")
        f.write("format ascii 1.0\n")
        f.write("comment AutoLens sparse COLMAP reconstruction\n")
        f.write(f"element vertex {len(points)}\n")
        f.write("property float x\n")
        f.write("property float y\n")
        f.write("property float z\n")
        f.write("property uchar red\n")
        f.write("property uchar green\n")
        f.write("property uchar blue\n")
        f.write("end_header\n")

        for p in points:
            x, y, z = p["xyz"]
            r, g, b = p["rgb"]
            f.write(f"{x:.10g} {y:.10g} {z:.10g} {r} {g} {b}\n")


def format_nuke_number(value: float) -> str:
    if abs(value) < 1.0e-12:
        value = 0.0
    return f"{value:.12g}"


def nuke_curve(poses: list[dict], row: int, col: int) -> str:
    keys = []
    for pose in poses:
        frame = pose["frame"]
        value = pose["nuke_matrix"][row][col]
        keys.append(f"x{frame} {format_nuke_number(value)}")
    return "{curve " + " ".join(keys) + "}"


def nuke_camera_matrix_block(poses: list[dict]) -> str:
    rows = []
    for r in range(3):
        components = [nuke_curve(poses, r, c) for c in range(4)]
        rows.append("{" + " ".join(components) + "}")
    rows.append("{0 0 0 1}")
    return "\n".join(rows)


def source_format_from_args(args, solve_cam: dict) -> dict:
    """Resolve the original Nuke format that corresponds to a COLMAP solve."""
    width = int(getattr(args, "source_width", 0) or solve_cam["width"])
    height = int(getattr(args, "source_height", 0) or solve_cam["height"])

    try:
        pa = float(getattr(args, "source_pixel_aspect", 1.0))
    except Exception:
        pa = 1.0

    if not np.isfinite(pa) or pa <= 0.0:
        pa = 1.0

    return {
        "width": width,
        "height": height,
        "pixel_aspect": pa,
        "display_width": float(width) * pa,
        "display_height": float(height),
    }


def camera_to_nuke_display_space(
    solve_cam: dict,
    source_format: dict,
) -> dict:
    """
    Convert a square-pixel COLMAP solve camera into the original Nuke display
    coordinate system.

    Square-pixel footage is an identity conversion. Non-square-PAR footage is
    solved on a desqueezed proxy, so focal length and principal point are
    scaled back into the source DISPLAY raster before fitting LensDistortion2
    and exporting Camera2.
    """
    solve_w = float(solve_cam["width"])
    solve_h = float(solve_cam["height"])

    display_w = float(
        source_format.get(
            "display_width",
            float(source_format["width"])
            * float(source_format.get("pixel_aspect", 1.0)),
        )
    )
    display_h = float(
        source_format.get(
            "display_height",
            source_format["height"],
        )
    )

    scale_x = display_w / max(solve_w, 1.0)
    scale_y = display_h / max(solve_h, 1.0)

    # Proxy dimensions may be rounded by one pixel. A shared-focal camera needs
    # one scale, so average the two nearly-identical display scales.
    shared_scale = 0.5 * (scale_x + scale_y)

    mismatch = abs(scale_x - scale_y) / max(shared_scale, 1.0e-12)
    if mismatch > 0.01:
        print(
            "[AUTOLENS PAR WARNING] Square-pixel solve aspect differs from "
            "the Nuke display aspect by more than 1%. "
            f"scale_x={scale_x:.8f}, scale_y={scale_y:.8f}"
        )

    return {
        "camera_id": solve_cam.get("camera_id", 1),
        "model": solve_cam["model"],
        "width": display_w,
        "height": display_h,
        "f_px": float(solve_cam["f_px"]) * shared_scale,
        "cx": float(solve_cam["cx"]) * scale_x,
        "cy": float(solve_cam["cy"]) * scale_y,
        "k1": float(solve_cam["k1"]),
        "k2": float(solve_cam["k2"]),
        "source_pixel_aspect": float(source_format.get("pixel_aspect", 1.0)),
        "solve_width": int(solve_cam["width"]),
        "solve_height": int(solve_cam["height"]),
    }


def engine_runtime_preflight():
    """
    Fail before launching COLMAP if a packaged engine helper is missing.

    This exists specifically to avoid discovering a packaging/programming
    error after minutes of feature extraction and SfM work.
    """
    required = (
        "source_format_from_args",
        "camera_to_nuke_display_space",
        "compute_upright_world_rotation",
        "apply_world_rotation_to_poses",
        "apply_world_rotation_to_points",
        "fit_nuke_lens_model",
        "write_nuke_script",
    )

    missing = [name for name in required if not callable(globals().get(name))]
    if missing:
        raise RuntimeError(
            "AutoLens engine installation is incomplete. Missing runtime "
            "helper(s): " + ", ".join(missing)
        )

    # Tiny mathematical sanity checks. These do no I/O and complete instantly.
    class _SquareArgs:
        source_width = 3424
        source_height = 2202
        source_pixel_aspect = 1.0

    square_cam = {
        "camera_id": 1,
        "model": "RADIAL",
        "width": 3424,
        "height": 2202,
        "f_px": 2000.0,
        "cx": 1712.0,
        "cy": 1101.0,
        "k1": -0.1,
        "k2": 0.02,
    }

    square_format = source_format_from_args(_SquareArgs(), square_cam)
    converted = camera_to_nuke_display_space(square_cam, square_format)

    if abs(converted["f_px"] - square_cam["f_px"]) > 1.0e-8:
        raise RuntimeError(
            "AutoLens engine preflight failed square-pixel camera conversion."
        )

    if abs(converted["cx"] - square_cam["cx"]) > 1.0e-8:
        raise RuntimeError(
            "AutoLens engine preflight failed principal-point conversion."
        )


def camera_projection_values(cam: dict, aperture_mm: float) -> dict:
    w = cam["width"]
    h = cam["height"]

    focal_mm = cam["f_px"] * aperture_mm / float(w)
    vaperture_mm = aperture_mm * float(h) / float(w)

    win_x = 2.0 * (cam["cx"] - (w * 0.5)) / float(w)
    win_y = -2.0 * (cam["cy"] - (h * 0.5)) / float(h)

    return {
        "focal_mm": focal_mm,
        "haperture_mm": aperture_mm,
        "vaperture_mm": vaperture_mm,
        "win_translate": [win_x, win_y],
    }



# -------------------------------------------------------------------------
# Dense reconstruction / geometry utilities
# -------------------------------------------------------------------------
PLY_SCALAR_TYPES = {
    "char": ("b", "i1"), "int8": ("b", "i1"),
    "uchar": ("B", "u1"), "uint8": ("B", "u1"),
    "short": ("h", "i2"), "int16": ("h", "i2"),
    "ushort": ("H", "u2"), "uint16": ("H", "u2"),
    "int": ("i", "i4"), "int32": ("i", "i4"),
    "uint": ("I", "u4"), "uint32": ("I", "u4"),
    "float": ("f", "f4"), "float32": ("f", "f4"),
    "double": ("d", "f8"), "float64": ("d", "f8"),
}


def read_ply_header(path: Path):
    elements=[]; fmt=None
    with path.open('rb') as f:
        if f.readline().decode('ascii', errors='replace').strip() != 'ply':
            raise RuntimeError(f'Not a PLY file: {path}')
        current=None
        while True:
            raw=f.readline()
            if not raw: raise RuntimeError(f'Unexpected EOF in PLY header: {path}')
            line=raw.decode('ascii', errors='replace').strip()
            if line.startswith('format '): fmt=line.split()[1]
            elif line.startswith('element '):
                _,name,count=line.split()[:3]
                current={'name':name,'count':int(count),'properties':[]}; elements.append(current)
            elif line.startswith('property ') and current is not None:
                parts=line.split()
                if parts[1]=='list': current['properties'].append({'kind':'list','count_type':parts[2],'item_type':parts[3],'name':parts[4]})
                else: current['properties'].append({'kind':'scalar','type':parts[1],'name':parts[2]})
            elif line=='end_header': return fmt,elements,f.tell()


def _endian_prefix(fmt):
    if fmt=='binary_little_endian': return '<'
    if fmt=='binary_big_endian': return '>'
    raise RuntimeError(f'Not a binary PLY format: {fmt}')


def _read_binary_scalar(f,type_name,endian):
    code=PLY_SCALAR_TYPES[type_name][0]; size=struct.calcsize(endian+code)
    data=f.read(size)
    if len(data)!=size: raise RuntimeError('Unexpected EOF in binary PLY')
    return struct.unpack(endian+code,data)[0]


def read_ply_vertices(path: Path):
    fmt,elements,offset=read_ply_header(path)
    ve=next((e for e in elements if e['name']=='vertex'),None)
    if ve is None: raise RuntimeError(f'PLY has no vertices: {path}')
    props=ve['properties']; names=[p['name'] for p in props if p['kind']=='scalar']
    if any(p['kind']!='scalar' for p in props): raise RuntimeError('List vertex properties unsupported')
    for n in ('x','y','z'):
        if n not in names: raise RuntimeError(f'PLY missing {n}: {path}')
    with path.open('rb') as f:
        f.seek(offset)
        # COLMAP writes vertex first for fused/mesh PLYs.
        first_elem=elements[0] if elements else None
        if first_elem is None or first_elem['name']!='vertex':
            raise RuntimeError('AutoLens expects vertex as first PLY element')
        if fmt=='ascii':
            xyz=np.empty((ve['count'],3),dtype=np.float64); ix,iy,iz=names.index('x'),names.index('y'),names.index('z')
            for i in range(ve['count']):
                vals=f.readline().decode('ascii',errors='replace').split(); xyz[i]=[float(vals[ix]),float(vals[iy]),float(vals[iz])]
            return xyz
        endian=_endian_prefix(fmt)
        dtype=np.dtype([(p['name'], endian+PLY_SCALAR_TYPES[p['type']][1]) for p in props])
        arr=np.fromfile(f,dtype=dtype,count=ve['count'])
        return np.column_stack([arr['x'].astype(np.float64),arr['y'].astype(np.float64),arr['z'].astype(np.float64)])


def read_ply_mesh(path: Path):
    fmt,elements,offset=read_ply_header(path); vertices=None; faces=[]
    with path.open('rb') as f:
        f.seek(offset)
        for elem in elements:
            props=elem['properties']
            if elem['name']=='vertex':
                names=[p['name'] for p in props if p['kind']=='scalar']
                if fmt=='ascii':
                    vertices=np.empty((elem['count'],3),dtype=np.float64); ix,iy,iz=names.index('x'),names.index('y'),names.index('z')
                    for i in range(elem['count']):
                        vals=f.readline().decode('ascii',errors='replace').split(); vertices[i]=[float(vals[ix]),float(vals[iy]),float(vals[iz])]
                else:
                    endian=_endian_prefix(fmt)
                    dtype=np.dtype([(p['name'], endian+PLY_SCALAR_TYPES[p['type']][1]) for p in props if p['kind']=='scalar'])
                    arr=np.fromfile(f,dtype=dtype,count=elem['count'])
                    vertices=np.column_stack([arr['x'].astype(np.float64),arr['y'].astype(np.float64),arr['z'].astype(np.float64)])
            elif elem['name']=='face':
                for _ in range(elem['count']):
                    face=None
                    if fmt=='ascii':
                        vals=f.readline().decode('ascii',errors='replace').split(); cursor=0
                        for p in props:
                            if p['kind']=='scalar': cursor+=1
                            else:
                                n=int(vals[cursor]); cursor+=1; items=[int(v) for v in vals[cursor:cursor+n]]; cursor+=n
                                if p['name'] in ('vertex_indices','vertex_index'): face=items
                    else:
                        endian=_endian_prefix(fmt)
                        for p in props:
                            if p['kind']=='scalar': _read_binary_scalar(f,p['type'],endian)
                            else:
                                n=int(_read_binary_scalar(f,p['count_type'],endian)); items=[int(_read_binary_scalar(f,p['item_type'],endian)) for __ in range(n)]
                                if p['name'] in ('vertex_indices','vertex_index'): face=items
                    if face and len(face)>=3: faces.append(face)
            else:
                for _ in range(elem['count']):
                    if fmt=='ascii': f.readline()
                    else:
                        endian=_endian_prefix(fmt)
                        for p in props:
                            if p['kind']=='scalar': _read_binary_scalar(f,p['type'],endian)
                            else:
                                n=int(_read_binary_scalar(f,p['count_type'],endian))
                                for __ in range(n): _read_binary_scalar(f,p['item_type'],endian)
    if vertices is None or not faces: raise RuntimeError(f'Could not parse mesh PLY: {path}')
    return vertices,faces


def write_mesh_obj(path,vertices,faces):
    with path.open('w',encoding='utf-8') as f:
        f.write('# AutoLens projection proxy\n')
        for x,y,z in vertices: f.write(f'v {x:.10g} {y:.10g} {z:.10g}\n')
        for face in faces: f.write('f '+' '.join(str(int(i)+1) for i in face)+'\n')


def write_ply_mesh(path: Path, vertices, faces):
    """
    Write a compact binary little-endian triangle mesh that AutoLens and COLMAP
    can both read back. Only XYZ + vertex_indices are needed for the proxy.
    """
    vertices = np.asarray(vertices, dtype=np.float32)
    triangles = triangulate_faces(faces).astype(np.int32, copy=False)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(vertices)}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        f"element face {len(triangles)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    ).encode("ascii")

    with Path(path).open("wb") as f:
        f.write(header)
        vertices.tofile(f)

        # Face record = uchar count (3) + three int32 indices.
        face_dtype = np.dtype([
            ("count", "u1"),
            ("indices", "<i4", (3,)),
        ])
        records = np.empty(len(triangles), dtype=face_dtype)
        records["count"] = 3
        records["indices"] = triangles
        records.tofile(f)


def _compact_triangle_mesh(vertices, triangles):
    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)

    if len(triangles) == 0:
        return vertices[:0], triangles.reshape(0, 3)

    used = np.unique(triangles.reshape(-1))
    remap = np.full(len(vertices), -1, dtype=np.int64)
    remap[used] = np.arange(len(used), dtype=np.int64)

    return (
        vertices[used],
        remap[triangles],
    )


def cleanup_proxy_spanning_triangles(
    vertices,
    faces,
    *,
    span_local_ratio=4.0,
    span_global_ratio=3.0,
    skinny_quality=0.035,
    skinny_local_ratio=2.5,
):
    """
    Remove the classic Delaunay "bridge" / "invisible wall" triangles.

    The important distinction is local scale. A legitimately large triangle in
    a genuinely sparse region is not automatically bad. A triangle whose edge
    is several times longer than the normal incident edges at its own vertices
    is suspicious, especially when it is also globally long or extremely
    skinny.

    This runs on the already-simplified proxy, so it is fast enough to apply
    automatically even when resuming an existing dense solve.
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = triangulate_faces(faces).astype(np.int64, copy=False)

    diagnostics = {
        "cleanup_version": 1,
        "input_vertices": int(len(vertices)),
        "input_triangles": int(len(triangles)),
        "removed_invalid": 0,
        "removed_degenerate": 0,
        "removed_spanning": 0,
        "removed_skinny_spanning": 0,
    }

    if len(vertices) == 0 or len(triangles) == 0:
        diagnostics["output_vertices"] = int(len(vertices))
        diagnostics["output_triangles"] = int(len(triangles))
        return vertices, triangles, diagnostics

    # Invalid topology / NaN protection.
    valid_index = np.all(
        (triangles >= 0) & (triangles < len(vertices)),
        axis=1,
    )
    finite_vertices = np.all(np.isfinite(vertices), axis=1)
    valid_finite = valid_index.copy()

    if np.any(valid_index):
        valid_finite[valid_index] &= np.all(
            finite_vertices[triangles[valid_index]],
            axis=1,
        )

    diagnostics["removed_invalid"] = int(
        np.count_nonzero(~valid_finite)
    )
    triangles = triangles[valid_finite]

    if len(triangles) == 0:
        clean_v, clean_t = _compact_triangle_mesh(
            vertices,
            triangles,
        )
        diagnostics["output_vertices"] = int(len(clean_v))
        diagnostics["output_triangles"] = int(len(clean_t))
        return clean_v, clean_t, diagnostics

    v0 = vertices[triangles[:, 0]]
    v1 = vertices[triangles[:, 1]]
    v2 = vertices[triangles[:, 2]]

    e01 = np.linalg.norm(v1 - v0, axis=1)
    e12 = np.linalg.norm(v2 - v1, axis=1)
    e20 = np.linalg.norm(v0 - v2, axis=1)

    edge_matrix = np.column_stack(
        [e01, e12, e20]
    )

    twice_area = np.linalg.norm(
        np.cross(v1 - v0, v2 - v0),
        axis=1,
    )
    area = 0.5 * twice_area

    positive_edges = edge_matrix[
        np.isfinite(edge_matrix)
        & (edge_matrix > 1.0e-12)
    ]

    if len(positive_edges) == 0:
        diagnostics["removed_degenerate"] = int(len(triangles))
        clean_v, clean_t = _compact_triangle_mesh(
            vertices,
            triangles[:0],
        )
        diagnostics["output_vertices"] = int(len(clean_v))
        diagnostics["output_triangles"] = int(len(clean_t))
        return clean_v, clean_t, diagnostics

    global_edge_median = float(
        np.median(positive_edges)
    )

    scene_low = np.percentile(
        vertices[np.all(np.isfinite(vertices), axis=1)],
        1.0,
        axis=0,
    )
    scene_high = np.percentile(
        vertices[np.all(np.isfinite(vertices), axis=1)],
        99.0,
        axis=0,
    )
    scene_diag = max(
        float(np.linalg.norm(scene_high - scene_low)),
        1.0e-9,
    )

    degenerate_area = max(
        scene_diag * scene_diag * 1.0e-12,
        global_edge_median * global_edge_median * 1.0e-8,
    )

    nondegenerate = (
        np.all(np.isfinite(edge_matrix), axis=1)
        & np.isfinite(area)
        & (np.min(edge_matrix, axis=1) > 1.0e-12)
        & (area > degenerate_area)
    )

    diagnostics["removed_degenerate"] = int(
        np.count_nonzero(~nondegenerate)
    )

    tri = triangles[nondegenerate]
    edges = edge_matrix[nondegenerate]
    tri_area = area[nondegenerate]

    if len(tri) == 0:
        clean_v, clean_t = _compact_triangle_mesh(
            vertices,
            tri,
        )
        diagnostics["output_vertices"] = int(len(clean_v))
        diagnostics["output_triangles"] = int(len(clean_t))
        return clean_v, clean_t, diagnostics

    # Robust per-vertex local edge scale. Long bridge edges are capped before
    # they contribute to the neighborhood mean so they cannot inflate their
    # own acceptance threshold.
    edge_pairs = np.vstack([
        tri[:, [0, 1]],
        tri[:, [1, 2]],
        tri[:, [2, 0]],
    ])
    edge_lengths = np.concatenate([
        edges[:, 0],
        edges[:, 1],
        edges[:, 2],
    ])

    cap = max(
        global_edge_median * 3.0,
        1.0e-12,
    )
    capped_lengths = np.minimum(
        edge_lengths,
        cap,
    )

    local_sum = np.zeros(
        len(vertices),
        dtype=np.float64,
    )
    local_count = np.zeros(
        len(vertices),
        dtype=np.int64,
    )

    np.add.at(
        local_sum,
        edge_pairs[:, 0],
        capped_lengths,
    )
    np.add.at(
        local_sum,
        edge_pairs[:, 1],
        capped_lengths,
    )
    np.add.at(
        local_count,
        edge_pairs[:, 0],
        1,
    )
    np.add.at(
        local_count,
        edge_pairs[:, 1],
        1,
    )

    vertex_scale = np.full(
        len(vertices),
        global_edge_median,
        dtype=np.float64,
    )
    has_edges = local_count > 0
    vertex_scale[has_edges] = (
        local_sum[has_edges]
        / local_count[has_edges]
    )

    tri_local_scale = np.median(
        vertex_scale[tri],
        axis=1,
    )
    tri_local_scale = np.maximum(
        tri_local_scale,
        global_edge_median * 0.20,
    )

    max_edge = np.max(edges, axis=1)
    min_edge = np.maximum(
        np.min(edges, axis=1),
        1.0e-12,
    )

    # Equilateral = 1.0, increasingly needle-like -> 0.
    quality = (
        4.0
        * math.sqrt(3.0)
        * tri_area
        / np.maximum(
            np.sum(edges * edges, axis=1),
            1.0e-20,
        )
    )

    spanning = (
        (max_edge > tri_local_scale * span_local_ratio)
        & (max_edge > global_edge_median * span_global_ratio)
    )

    skinny_spanning = (
        (quality < skinny_quality)
        & (max_edge > tri_local_scale * skinny_local_ratio)
        & (max_edge > global_edge_median * 2.0)
        & ((max_edge / min_edge) > 6.0)
    )

    remove = spanning | skinny_spanning
    keep = ~remove

    diagnostics["removed_spanning"] = int(
        np.count_nonzero(spanning)
    )
    diagnostics["removed_skinny_spanning"] = int(
        np.count_nonzero(
            skinny_spanning & ~spanning
        )
    )
    diagnostics["global_edge_median"] = global_edge_median
    diagnostics["scene_diagonal"] = scene_diag
    diagnostics["span_local_ratio"] = float(span_local_ratio)
    diagnostics["span_global_ratio"] = float(span_global_ratio)
    diagnostics["skinny_quality_threshold"] = float(skinny_quality)

    clean_vertices, clean_triangles = _compact_triangle_mesh(
        vertices,
        tri[keep],
    )

    diagnostics["output_vertices"] = int(
        len(clean_vertices)
    )
    diagnostics["output_triangles"] = int(
        len(clean_triangles)
    )
    diagnostics["removed_total_triangles"] = int(
        diagnostics["input_triangles"]
        - diagnostics["output_triangles"]
    )
    diagnostics["removed_fraction"] = (
        float(diagnostics["removed_total_triangles"])
        / max(float(diagnostics["input_triangles"]), 1.0)
    )

    return (
        clean_vertices,
        clean_triangles,
        diagnostics,
    )


def ensure_clean_proxy_mesh(
    source_proxy: Path,
    clean_proxy: Path,
    report_path: Path,
):
    """
    Create/reuse the cleaned projection proxy without touching PatchMatch,
    fusion, or the raw meshing result.
    """
    source_proxy = Path(source_proxy)
    clean_proxy = Path(clean_proxy)
    report_path = Path(report_path)

    reusable = (
        clean_proxy.exists()
        and report_path.exists()
        and clean_proxy.stat().st_mtime_ns
        >= source_proxy.stat().st_mtime_ns
    )

    if reusable:
        try:
            report = json.loads(
                report_path.read_text(
                    encoding="utf-8"
                )
            )
            if report.get("cleanup_version") == 1:
                print(
                    "\n[AUTOLENS PROXY CLEANUP] "
                    "Reusing cleaned proxy mesh.\n"
                )
                return clean_proxy, report
        except Exception:
            pass

    print(
        "\n[AUTOLENS PROXY CLEANUP] "
        "Removing unsupported spanning / needle triangles..."
    )

    vertices, faces = read_ply_mesh(
        source_proxy
    )

    (
        clean_vertices,
        clean_faces,
        report,
    ) = cleanup_proxy_spanning_triangles(
        vertices,
        faces,
    )

    # Safety valve: a cleanup bug or pathological reconstruction should never
    # silently erase the artist's entire proxy.
    if (
        len(clean_faces) < 3
        or (
            report["input_triangles"] >= 100
            and report["removed_fraction"] > 0.45
        )
    ):
        print(
            "[AUTOLENS PROXY CLEANUP WARNING] "
            "Cleanup was unusually aggressive; keeping original simplified "
            "proxy instead."
        )
        report["fallback_to_original"] = True
        shutil.copy2(
            source_proxy,
            clean_proxy,
        )
    else:
        report["fallback_to_original"] = False
        write_ply_mesh(
            clean_proxy,
            clean_vertices,
            clean_faces,
        )

    report["source_proxy"] = str(
        source_proxy
    )
    report["clean_proxy"] = str(
        clean_proxy
    )

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "[AUTOLENS PROXY CLEANUP] "
        f"Removed {report['removed_total_triangles']:,} / "
        f"{report['input_triangles']:,} triangle(s) "
        f"({report['removed_fraction'] * 100.0:.2f}%)."
    )

    return clean_proxy, report


def estimate_scene_up(poses):
    ups=[]
    for pose in poses:
        m=np.asarray(pose['nuke_matrix'],dtype=np.float64); u=m[:3,1]; n=np.linalg.norm(u)
        if n>1e-8: ups.append(u/n)
    if not ups: return np.array([0.,1.,0.])
    ref=ups[0]; ups=[u if np.dot(u,ref)>=0 else -u for u in ups]; avg=np.sum(ups,axis=0); n=np.linalg.norm(avg)
    return avg/n if n>1e-8 else np.array([0.,1.,0.])


def _plane_basis(normal):
    normal=normal/np.linalg.norm(normal); axes=np.eye(3); axis=axes[np.argmin(np.abs(axes@normal))]
    u=np.cross(normal,axis); u/=np.linalg.norm(u); v=np.cross(normal,u); v/=np.linalg.norm(v); return u,v


def _fit_plane_pca(points):
    c=np.mean(points,axis=0); q=points-c; cov=q.T@q/max(len(points),1); vals,vecs=np.linalg.eigh(cov); n=vecs[:,np.argmin(vals)]; n/=np.linalg.norm(n); return n,-float(np.dot(n,c)),c


def detect_planar_surfaces(points,poses,plane_count=6,iterations=450,threshold_ratio=0.0025,sample_limit=100000):
    points=np.asarray(points,dtype=np.float64); points=points[np.all(np.isfinite(points),axis=1)]
    if len(points)<100: return [],None,estimate_scene_up(poses),0.0
    lo=np.percentile(points,1,axis=0); hi=np.percentile(points,99,axis=0); trimmed=points[np.all((points>=lo)&(points<=hi),axis=1)]
    if len(trimmed)<100: trimmed=points
    b0=np.percentile(trimmed,2,axis=0); b1=np.percentile(trimmed,98,axis=0); diag=float(np.linalg.norm(b1-b0)); threshold=max(diag*threshold_ratio,1e-6)
    rng=np.random.default_rng(1337); sample=trimmed[rng.choice(len(trimmed),sample_limit,replace=False)] if len(trimmed)>sample_limit else trimmed.copy()
    up=estimate_scene_up(poses); centers=np.asarray([p['center'] for p in poses]); camera_height=float(np.median(centers@up)); remaining=sample; planes=[]
    for pi in range(plane_count):
        if len(remaining)<150: break
        best_mask=None; best_count=0
        for _ in range(iterations):
            ids=rng.choice(len(remaining),3,replace=False); a,b,c=remaining[ids]; n=np.cross(b-a,c-a); nn=np.linalg.norm(n)
            if nn<1e-10: continue
            n/=nn; mask=np.abs((remaining-a)@n)<threshold; count=int(mask.sum())
            if count>best_count: best_count=count; best_mask=mask
        if best_mask is None or best_count<max(120,int(len(sample)*0.0015)): break
        inliers=remaining[best_mask]; n,d,c=_fit_plane_pca(inliers); align=abs(float(np.dot(n,up)))
        cls='horizontal' if align>=0.72 else ('vertical' if align<=0.35 else 'angled')
        if cls=='horizontal' and np.dot(n,up)<0: n=-n; d=-d
        u,v=_plane_basis(n); lu=(inliers-c)@u; lv=(inliers-c)@v; u0,u1=np.percentile(lu,[2,98]); v0,v1=np.percentile(lv,[2,98]); du=max((u1-u0)*0.08,threshold*2); dv=max((v1-v0)*0.08,threshold*2); u0-=du;u1+=du;v0-=dv;v1+=dv
        corners=np.array([c+u*u0+v*v0,c+u*u1+v*v0,c+u*u1+v*v1,c+u*u0+v*v1]); ph=float(np.dot(c,up)); below=camera_height-ph
        planes.append({'index':pi+1,'classification':cls,'normal':n.tolist(),'d':float(d),'centroid':c.tolist(),'corners':corners.tolist(),'support':int(len(inliers)),'support_fraction_sample':float(len(inliers)/max(len(sample),1)),'up_alignment':align,'height_along_up':ph,'below_camera_by':below})
        remaining=remaining[~best_mask]
    candidates=[p for p in planes if p['classification']=='horizontal' and p['below_camera_by']>0] or [p for p in planes if p['classification']=='horizontal']
    ground=max(candidates,key=lambda p:(p['support'],-abs(p['below_camera_by']))) if candidates else None
    return planes,ground,up,threshold


def write_single_plane_obj(path,plane,name):
    with path.open('w',encoding='utf-8') as f:
        f.write(f'g {name}\n')
        for x,y,z in np.asarray(plane['corners']): f.write(f'v {x:.10g} {y:.10g} {z:.10g}\n')
        f.write('f 1 2 3 4\n')


def write_planes_obj(path,planes,ground):
    with path.open('w',encoding='utf-8') as f:
        base=1
        for p in planes:
            is_ground=ground is not None and p['index']==ground['index']
            name='GROUND' if is_ground else (f"WALL_{p['index']:02d}" if p['classification']=='vertical' else (f"HORIZONTAL_{p['index']:02d}" if p['classification']=='horizontal' else f"PLANE_{p['index']:02d}"))
            f.write(f'\ng {name}\n')
            for x,y,z in np.asarray(p['corners']): f.write(f'v {x:.10g} {y:.10g} {z:.10g}\n')
            f.write('f '+' '.join(str(base+i) for i in range(4))+'\n'); base+=4



DENSE_QUALITY_PRESETS = {
    "preview": {
        "max_image_size": 1800,
        "selection": "smart",
        "target_frames": 8,
        "min_frames": 5,
        "coverage_target": 0.90,
        "frame_step": 5,
        "source_views": 6,
        "geom_consistency": False,
        "window_step": 2,
        "window_radius": 3,
        "num_iterations": 3,
        "num_samples": 10,
        "proxy_face_ratio": 0.08,
        "fusion_min_num_pixels": 3,
    },
    "normal": {
        "max_image_size": 3200,
        "selection": "smart",
        "target_frames": 12,
        "min_frames": 6,
        "coverage_target": 0.97,
        "frame_step": 3,
        "source_views": 8,
        "geom_consistency": True,
        "window_step": 1,
        "window_radius": 5,
        "num_iterations": 5,
        "num_samples": 15,
        "proxy_face_ratio": 0.12,
        "fusion_min_num_pixels": 4,
    },
    "high": {
        "max_image_size": 4000,
        "selection": "smart",
        "target_frames": 18,
        "min_frames": 8,
        "coverage_target": 0.99,
        "frame_step": 2,
        "source_views": 12,
        "geom_consistency": True,
        "window_step": 1,
        "window_radius": 5,
        "num_iterations": 6,
        "num_samples": 20,
        "proxy_face_ratio": 0.20,
        "fusion_min_num_pixels": 5,
    },
}


def physical_ram_gb() -> float | None:
    """Best-effort physical RAM detection without third-party packages."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024.0 ** 3)
        else:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) / (1024.0 ** 3)
    except Exception:
        return None
    return None


def auto_cache_gb() -> float:
    ram = physical_ram_gb()
    if ram is None:
        return 32.0
    # Use roughly a quarter of system RAM, capped at 64 GB.
    return round(max(4.0, min(64.0, ram * 0.25)), 1)


def resolve_cache_value(value: str | float | int | None) -> float:
    if value is None or str(value).lower() == "auto":
        return auto_cache_gb()
    parsed = float(value)
    if parsed <= 0:
        raise RuntimeError("Cache size must be greater than zero.")
    return parsed


def detect_nvidia_gpus() -> list[str]:
    """Return NVIDIA GPU indices using nvidia-smi when available."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        proc = subprocess.run(
            [nvidia_smi, "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def resolve_dense_gpus(value: str) -> str:
    if value.lower() != "auto":
        return value
    indices = detect_nvidia_gpus()
    if indices:
        return ",".join(indices)
    # COLMAP's default GPU selection.
    return "-1"


def resolve_dense_settings(args) -> dict:
    preset = dict(DENSE_QUALITY_PRESETS[args.quality])

    overrides = {
        "max_image_size": args.dense_max_image_size,
        "selection": args.dense_selection,
        "target_frames": args.dense_target_frames,
        "min_frames": args.dense_min_frames,
        "coverage_target": args.dense_coverage_target,
        "frame_step": args.dense_frame_step,
        "source_views": args.dense_source_views,
        "window_step": args.dense_window_step,
        "window_radius": args.dense_window_radius,
        "num_iterations": args.dense_iterations,
        "num_samples": args.dense_samples,
        "proxy_face_ratio": args.proxy_face_ratio,
        "fusion_min_num_pixels": args.fusion_min_num_pixels,
    }
    for key, value in overrides.items():
        if value is not None:
            preset[key] = value

    if args.geom_consistency is not None:
        preset["geom_consistency"] = bool(args.geom_consistency)

    preset["quality"] = args.quality
    preset["gpu_index"] = resolve_dense_gpus(args.dense_gpus)
    preset["patchmatch_cache_gb"] = resolve_cache_value(args.patchmatch_cache_gb)
    preset["fusion_cache_gb"] = resolve_cache_value(args.fusion_cache_gb)

    preset["selection"] = str(preset["selection"]).lower()
    if preset["selection"] not in ("smart", "step", "all"):
        raise RuntimeError("Dense selection must be smart, step, or all.")

    preset["target_frames"] = max(4, int(preset["target_frames"]))
    preset["min_frames"] = max(4, int(preset["min_frames"]))
    preset["min_frames"] = min(preset["min_frames"], preset["target_frames"])
    preset["coverage_target"] = float(np.clip(float(preset["coverage_target"]), 0.50, 1.0))
    preset["frame_step"] = max(1, int(preset["frame_step"]))
    preset["source_views"] = max(2, int(preset["source_views"]))
    preset["max_image_size"] = max(320, int(preset["max_image_size"]))
    preset["window_step"] = max(1, int(preset["window_step"]))
    preset["window_radius"] = max(1, int(preset["window_radius"]))
    preset["num_iterations"] = max(1, int(preset["num_iterations"]))
    preset["num_samples"] = max(1, int(preset["num_samples"]))
    preset["proxy_face_ratio"] = float(preset["proxy_face_ratio"])
    preset["fusion_min_num_pixels"] = max(1, int(preset["fusion_min_num_pixels"]))
    return preset


def image_signature(images: list[Path]) -> list[dict]:
    """Fast cache identity based on filename, size and mtime."""
    result = []
    for p in images:
        stat = p.stat()
        result.append({
            "name": p.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    return result


def directory_signature(
    folder: Path | None,
    pattern: str = "*.png",
):
    if folder is None:
        return None

    folder = Path(folder)

    if not folder.is_dir():
        return None

    items = []

    for path in sorted(folder.glob(pattern)):
        if not path.is_file():
            continue

        stat = path.stat()

        items.append({
            "name": path.name,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })

    return items


def clear_reconstruction_cache_for_mask_change(
    output: Path,
    cache_path: Path,
):
    print(
        "\n[AUTOLENS MASK] Tracking masks changed. "
        "Invalidating cached tracking/dense reconstruction.\n"
    )

    for path in (
        output / "database.db",
        output / "database_global.db",
        output / "sparse",
        output / "sparse_global",
        output / "dense",
        output / "autolens_proxy_mesh.obj",
        output / "autolens_meshfit_planes.obj",
        output / "autolens_guided_surfaces.obj",
        output / "autolens_guided_surfaces.json",
        output / "autolens_result.json",
        output / "autolens_camera_track.json",
        output / "autolens_lens_model.json",
        output / "AutoLens_v08.nk",
        cache_path,
    ):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except Exception:
            pass



def load_json_if_exists(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def model_dir_is_valid(path: Path) -> bool:
    if not path.is_dir():
        return False
    has_camera = (path / "cameras.bin").exists() or (path / "cameras.txt").exists()
    has_images = (path / "images.bin").exists() or (path / "images.txt").exists()
    has_points = (path / "points3D.bin").exists() or (path / "points3D.txt").exists()
    return has_camera and has_images and has_points


def sparse_root_has_model(root: Path) -> bool:
    if not root.exists():
        return False
    return any(model_dir_is_valid(p) for p in root.iterdir() if p.is_dir())


def find_cached_sparse_root(output: Path, cache: dict | None) -> Path | None:
    if cache:
        saved = cache.get("sparse_root")
        if saved:
            p = Path(saved)
            if sparse_root_has_model(p):
                return p

    for name in ("sparse", "sparse_global"):
        p = output / name
        if sparse_root_has_model(p):
            return p
    return None


def parse_image_observations(
    images_txt: Path,
    width: int,
    height: int,
    grid_x: int = 8,
    grid_y: int = 5,
) -> dict[str, dict]:
    """
    Parse the registered 2D observations from COLMAP images.txt.

    For each image we keep:
      - the set of triangulated 3D point IDs it sees
      - count of useful 3D observations
      - how broadly those observations cover the image
    """
    with images_txt.open("r", encoding="utf-8", errors="replace") as f:
        raw_lines = [
            line.rstrip("\n")
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    result = {}
    i = 0
    while i < len(raw_lines):
        metadata = raw_lines[i].strip()
        points_line = raw_lines[i + 1].strip() if i + 1 < len(raw_lines) else ""
        i += 2

        parts = metadata.split()
        if len(parts) < 10:
            continue

        try:
            int(parts[0])
            int(parts[8])
            [float(v) for v in parts[1:8]]
        except ValueError:
            continue

        name = " ".join(parts[9:])
        point_ids = set()
        occupied = set()
        valid_xy = []

        values = points_line.split()
        for j in range(0, len(values) - 2, 3):
            try:
                x = float(values[j])
                y = float(values[j + 1])
                point_id = int(values[j + 2])
            except ValueError:
                continue

            if point_id < 0:
                continue

            point_ids.add(point_id)
            valid_xy.append((x, y))

            gx = int(np.clip((x / max(width, 1)) * grid_x, 0, grid_x - 1))
            gy = int(np.clip((y / max(height, 1)) * grid_y, 0, grid_y - 1))
            occupied.add((gx, gy))

        grid_coverage = len(occupied) / float(grid_x * grid_y)

        result[name] = {
            "point_ids": point_ids,
            "point_count": len(point_ids),
            "grid_coverage": grid_coverage,
            "occupied_grid_cells": len(occupied),
        }

    return result


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    if n <= 1.0e-12:
        return np.zeros_like(v)
    return v / n


def characterize_camera_motion(poses: list[dict]) -> dict:
    centers = np.asarray([p["center"] for p in poses], dtype=np.float64)
    forwards = np.asarray(
        [-np.asarray(p["nuke_matrix"], dtype=np.float64)[:3, 2] for p in poses],
        dtype=np.float64,
    )
    forwards = np.asarray([_unit(v) for v in forwards])

    if len(centers) < 2:
        return {
            "classification": "INSUFFICIENT",
            "path_length": 0.0,
            "net_translation": 0.0,
            "linearity": 0.0,
            "optical_axis_alignment": 0.0,
            "view_rotation_degrees": 0.0,
        }

    steps = np.linalg.norm(np.diff(centers, axis=0), axis=1)
    path_length = float(np.sum(steps))
    net_vector = centers[-1] - centers[0]
    net_translation = float(np.linalg.norm(net_vector))
    linearity = net_translation / max(path_length, 1.0e-12)

    avg_forward = _unit(np.mean(forwards, axis=0))
    net_dir = _unit(net_vector)
    optical_axis_alignment = abs(float(np.dot(net_dir, avg_forward))) if net_translation > 0 else 0.0

    dots = np.sum(forwards[:-1] * forwards[1:], axis=1)
    dots = np.clip(dots, -1.0, 1.0)
    cumulative_rotation = float(np.degrees(np.sum(np.arccos(dots))))

    first_last_dot = float(np.clip(np.dot(forwards[0], forwards[-1]), -1.0, 1.0))
    endpoint_rotation = float(np.degrees(np.arccos(first_last_dot)))

    if path_length <= 1.0e-8:
        classification = "LOCKED / ROTATION"
    elif net_translation < 0.05 * max(path_length, 1.0e-8) and cumulative_rotation > 5.0:
        classification = "ORBIT / RETURNING PATH"
    elif linearity >= 0.82 and optical_axis_alignment >= 0.72:
        sign = float(np.dot(net_dir, avg_forward))
        classification = "PUSH-IN / DOLLY" if sign >= 0.0 else "PULL-OUT / DOLLY"
    elif linearity >= 0.82 and optical_axis_alignment <= 0.45:
        classification = "LATERAL / TRUCK"
    elif cumulative_rotation > 20.0 and linearity < 0.80:
        classification = "ORBIT / HANDHELD"
    elif cumulative_rotation > 15.0 and net_translation < 0.25 * max(path_length, 1.0e-8):
        classification = "ROTATION-DOMINANT"
    else:
        classification = "GENERAL CAMERA MOVE"

    return {
        "classification": classification,
        "path_length": path_length,
        "net_translation": net_translation,
        "linearity": float(linearity),
        "optical_axis_alignment": float(optical_axis_alignment),
        "cumulative_rotation_degrees": cumulative_rotation,
        "endpoint_rotation_degrees": endpoint_rotation,
    }


def _pose_maps(poses: list[dict]):
    centers = {}
    forwards = {}
    frame_order = {}
    for index, pose in enumerate(poses):
        name = pose["name"]
        centers[name] = np.asarray(pose["center"], dtype=np.float64)
        matrix = np.asarray(pose["nuke_matrix"], dtype=np.float64)
        forwards[name] = _unit(-matrix[:3, 2])
        frame_order[name] = index
    return centers, forwards, frame_order


def _pair_geometry(
    a: str,
    b: str,
    centers: dict,
    forwards: dict,
    observations: dict,
    max_baseline: float,
) -> dict:
    ca = centers[a]
    cb = centers[b]
    baseline = float(np.linalg.norm(ca - cb))
    baseline_norm = min(1.0, baseline / max(max_baseline, 1.0e-12))

    fa = forwards[a]
    fb = forwards[b]
    angle = float(np.degrees(np.arccos(np.clip(np.dot(fa, fb), -1.0, 1.0))))
    angle_norm = min(1.0, angle / 45.0)

    ids_a = observations.get(a, {}).get("point_ids", set())
    ids_b = observations.get(b, {}).get("point_ids", set())
    shared = len(ids_a & ids_b)
    denom = max(1, min(len(ids_a), len(ids_b)))
    overlap = shared / float(denom)

    return {
        "baseline": baseline,
        "baseline_norm": baseline_norm,
        "view_angle_degrees": angle,
        "angle_norm": angle_norm,
        "overlap": overlap,
        "shared_points": shared,
    }


def choose_smart_dense_views(
    poses: list[dict],
    observations: dict[str, dict],
    target_frames: int,
    min_frames: int,
    coverage_target: float,
) -> dict:
    """
    Greedy sparse-geometry-aware reference selector.

    It favors frames that:
      1. add previously unseen reconstructed scene points,
      2. are geometrically distinct from already chosen frames,
      3. still overlap enough with another chosen view for stereo,
      4. have broadly distributed useful feature support.
    """
    names = [p["name"] for p in poses if p["name"] in observations]
    if len(names) < 4:
        return {
            "selected_names": names,
            "coverage": 1.0,
            "shot": characterize_camera_motion(poses),
            "reason": "too few registered observations for smart reduction",
        }

    target_frames = min(max(4, int(target_frames)), len(names))
    min_frames = min(max(4, int(min_frames)), target_frames)

    centers, forwards, frame_order = _pose_maps(poses)

    all_points = set()
    for name in names:
        all_points |= observations[name]["point_ids"]

    if not all_points:
        # Fallback to temporal spacing if sparse IDs are unexpectedly absent.
        indices = np.linspace(0, len(names) - 1, target_frames).round().astype(int)
        selected = [names[i] for i in sorted(set(indices.tolist()))]
        return {
            "selected_names": selected,
            "coverage": 0.0,
            "shot": characterize_camera_motion(poses),
            "reason": "no sparse 3D observations; evenly spaced fallback",
        }

    point_counts = np.asarray([observations[n]["point_count"] for n in names], dtype=np.float64)
    max_count = max(float(np.max(point_counts)), 1.0)

    center_array = np.asarray([centers[n] for n in names])
    max_baseline = 0.0
    # Exact pairwise maximum is cheap for typical movie-shot frame counts.
    for i in range(len(center_array)):
        distances = np.linalg.norm(center_array[i + 1:] - center_array[i], axis=1)
        if len(distances):
            max_baseline = max(max_baseline, float(np.max(distances)))
    max_baseline = max(max_baseline, 1.0e-12)

    quality = {}
    for name in names:
        support = math.sqrt(observations[name]["point_count"] / max_count)
        spread = observations[name]["grid_coverage"]
        quality[name] = 0.45 * support + 0.55 * spread

    # Strong first reference: good support + good image-wide feature spread.
    first = max(names, key=lambda n: quality[n])
    selected = [first]
    covered = set(observations[first]["point_ids"])

    history = [{
        "name": first,
        "coverage": len(covered) / float(len(all_points)),
        "new_points": len(covered),
        "score": quality[first],
    }]

    while len(selected) < target_frames:
        unselected = [n for n in names if n not in selected]
        if not unselected:
            break

        gains = {
            n: len(observations[n]["point_ids"] - covered)
            for n in unselected
        }
        max_gain = max(gains.values()) if gains else 0

        current_coverage = len(covered) / float(len(all_points))
        if (
            len(selected) >= min_frames
            and current_coverage >= coverage_target
            and max_gain / float(len(all_points)) < 0.01
        ):
            break

        best_name = None
        best_score = -1.0
        best_metrics = None

        for name in unselected:
            gain_norm = gains[name] / float(max(max_gain, 1))

            pair_metrics = [
                _pair_geometry(name, chosen, centers, forwards, observations, max_baseline)
                for chosen in selected
            ]

            # For stereo we want at least one strongly overlapping selected view.
            best_overlap = max((m["overlap"] for m in pair_metrics), default=0.0)

            # Geometric novelty uses distance to the nearest selected camera,
            # normalized to the shot's maximum camera baseline.
            nearest_baseline = min(
                (np.linalg.norm(centers[name] - centers[c]) for c in selected),
                default=0.0,
            )
            baseline_novelty = min(1.0, float(nearest_baseline) / max_baseline)

            # A little orientation novelty helps orbit/handheld shots.
            min_forward_dot = max(
                (float(np.dot(forwards[name], forwards[c])) for c in selected),
                default=1.0,
            )
            orientation_novelty = min(
                1.0,
                float(np.degrees(np.arccos(np.clip(min_forward_dot, -1.0, 1.0)))) / 45.0,
            )

            # Reward overlap, but don't let near-duplicate cameras dominate.
            stereo_support = min(1.0, best_overlap / 0.65)

            score = (
                0.42 * gain_norm
                + 0.20 * baseline_novelty
                + 0.18 * stereo_support
                + 0.12 * quality[name]
                + 0.08 * orientation_novelty
            )

            # A candidate with essentially no shared reconstructed geometry is
            # risky for MVS even if it adds unique coverage.
            if best_overlap < 0.05 and len(selected) >= 2:
                score *= 0.35

            if score > best_score:
                best_score = score
                best_name = name
                best_metrics = {
                    "best_overlap": best_overlap,
                    "baseline_novelty": baseline_novelty,
                    "orientation_novelty": orientation_novelty,
                }

        if best_name is None:
            break

        new_ids = observations[best_name]["point_ids"] - covered
        covered |= observations[best_name]["point_ids"]
        selected.append(best_name)

        history.append({
            "name": best_name,
            "coverage": len(covered) / float(len(all_points)),
            "new_points": len(new_ids),
            "score": best_score,
            **(best_metrics or {}),
        })

    # If the greedy stop happened before a safe MVS minimum, fill with the most
    # temporally separated remaining frames.
    if len(selected) < min_frames:
        remaining = [n for n in names if n not in selected]
        while remaining and len(selected) < min_frames:
            candidate = max(
                remaining,
                key=lambda n: min(
                    abs(frame_order[n] - frame_order[s])
                    for s in selected
                ),
            )
            selected.append(candidate)
            covered |= observations[candidate]["point_ids"]
            remaining.remove(candidate)

    selected.sort(key=lambda n: frame_order[n])

    return {
        "selected_names": selected,
        "coverage": len(covered) / float(len(all_points)),
        "all_sparse_points": len(all_points),
        "shot": characterize_camera_motion(poses),
        "history": history,
        "reason": "smart sparse-geometry selection",
    }


def choose_dense_views(
    poses: list[dict],
    observations: dict[str, dict],
    settings: dict,
) -> dict:
    ordered = [p["name"] for p in poses]
    if not ordered:
        raise RuntimeError("No registered images available for dense reconstruction.")

    mode = settings["selection"]

    if mode == "all":
        selected = ordered
        return {
            "selected_names": selected,
            "coverage": 1.0,
            "shot": characterize_camera_motion(poses),
            "reason": "all registered views requested",
        }

    if mode == "step":
        step = max(1, int(settings["frame_step"]))
        selected = ordered[::step]
        if ordered[-1] not in selected:
            selected.append(ordered[-1])
        if len(selected) < 4 and len(ordered) >= 4:
            selected = ordered[:]
        return {
            "selected_names": selected,
            "coverage": None,
            "shot": characterize_camera_motion(poses),
            "reason": f"manual frame-step selection ({step})",
        }

    return choose_smart_dense_views(
        poses=poses,
        observations=observations,
        target_frames=settings["target_frames"],
        min_frames=settings["min_frames"],
        coverage_target=settings["coverage_target"],
    )


def rank_source_views(
    ref_name: str,
    selected_names: list[str],
    poses: list[dict],
    observations: dict[str, dict],
    source_views: int,
) -> list[str]:
    centers, forwards, frame_order = _pose_maps(poses)
    center_array = np.asarray([centers[n] for n in selected_names])
    max_baseline = 0.0
    for i in range(len(center_array)):
        d = np.linalg.norm(center_array[i + 1:] - center_array[i], axis=1)
        if len(d):
            max_baseline = max(max_baseline, float(np.max(d)))
    max_baseline = max(max_baseline, 1.0e-12)

    scored = []
    for name in selected_names:
        if name == ref_name:
            continue

        metrics = _pair_geometry(
            ref_name, name, centers, forwards, observations, max_baseline
        )

        # Strong overlap is the most important source-view property. Baseline
        # and viewpoint change provide useful depth discrimination.
        baseline_useful = min(1.0, metrics["baseline_norm"] * 2.5)
        angle_useful = min(1.0, metrics["angle_norm"] * 2.0)

        score = (
            0.62 * metrics["overlap"]
            + 0.28 * baseline_useful
            + 0.10 * angle_useful
        )

        # Near-zero overlap is usually unusable no matter the baseline.
        if metrics["overlap"] < 0.03:
            score *= 0.2

        scored.append((score, frame_order[name], name))

    scored.sort(key=lambda item: (-item[0], item[1]))
    count = min(max(2, int(source_views)), len(scored))
    return [item[2] for item in scored[:count]]


def write_dense_configs(
    dense: Path,
    poses: list[dict],
    observations: dict[str, dict],
    selection: dict,
    source_views: int,
) -> list[str]:
    selected = selection["selected_names"]

    stereo = dense / "stereo"
    stereo.mkdir(parents=True, exist_ok=True)

    patch_cfg = stereo / "patch-match.cfg"
    fusion_cfg = stereo / "fusion.cfg"

    lines = []
    for ref_name in selected:
        sources = rank_source_views(
            ref_name,
            selected,
            poses,
            observations,
            source_views,
        )

        lines.append(ref_name)
        lines.append(", ".join(sources))

    patch_cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fusion_cfg.write_text("\n".join(selected) + "\n", encoding="utf-8")
    return selected

def dense_settings_signature(settings: dict, selected_names: list[str]) -> dict:
    keys = (
        "quality",
        "selection",
        "target_frames",
        "min_frames",
        "coverage_target",
        "frame_step",
        "max_image_size",
        "source_views",
        "geom_consistency",
        "window_step",
        "window_radius",
        "num_iterations",
        "num_samples",
        "proxy_face_ratio",
        "fusion_min_num_pixels",
        "gpu_index",
        "patchmatch_cache_gb",
        "fusion_cache_gb",
    )
    sig = {key: settings[key] for key in keys}
    sig["selected_images"] = selected_names
    return sig


def print_dense_config(
    settings: dict,
    selection: dict,
    total_registered: int,
) -> None:
    selected_count = len(selection["selected_names"])
    shot = selection.get("shot", {})
    coverage = selection.get("coverage")

    print("\n" + "=" * 72)
    print("AUTOLENS SMART DENSE CONFIG")
    print("=" * 72)
    print(f"Quality:                 {settings['quality']}")
    print(f"Selection mode:          {settings['selection']}")
    print(f"Shot classification:     {shot.get('classification', 'UNKNOWN')}")
    print(f"Dense reference frames:  {selected_count} / {total_registered}")
    if coverage is not None:
        print(f"Sparse scene coverage:   {coverage * 100.0:.2f}%")
    print(f"Dense resolution:        {settings['max_image_size']} px")
    print(f"Source views/reference:  {min(settings['source_views'], max(0, selected_count - 1))}")
    print(f"Geometric consistency:   {'ON' if settings['geom_consistency'] else 'OFF'}")
    print(f"PatchMatch GPUs:          {settings['gpu_index']}")
    print(f"PatchMatch cache:         {settings['patchmatch_cache_gb']} GB")
    print(f"Fusion cache:             {settings['fusion_cache_gb']} GB")
    print(f"Proxy face ratio:         {settings['proxy_face_ratio']}")

    if shot:
        print(f"Camera path linearity:    {shot.get('linearity', 0.0) * 100.0:.1f}%")
        print(f"Optical-axis alignment:   {shot.get('optical_axis_alignment', 0.0) * 100.0:.1f}%")
        print(f"Camera rotation:          {shot.get('cumulative_rotation_degrees', 0.0):.2f} deg")

    print("-" * 72)
    print("Selected dense references:")
    selected = selection["selected_names"]
    for i in range(0, len(selected), 6):
        print("  " + "  |  ".join(Path(n).name for n in selected[i:i+6]))
    print("=" * 72 + "\n")


def run_dense_reconstruction(
    colmap,
    images_dir,
    best_model,
    output,
    poses,
    observations,
    settings,
    force_dense=False,
):
    dense = output / "dense"
    settings_path = dense / "autolens_dense_settings.json"

    # Analyze the solved shot and choose a compact, geometrically useful set
    # of dense reference views before deciding cache compatibility.
    selection = choose_dense_views(poses, observations, settings)
    selected_names = selection["selected_names"]
    requested_signature = dense_settings_signature(settings, selected_names)

    existing_signature = load_json_if_exists(settings_path)

    if force_dense and dense.exists():
        print("\n[AUTOLENS CACHE] --force-dense requested: rebuilding dense stage.\n")
        shutil.rmtree(dense)

    if dense.exists() and existing_signature is not None and existing_signature != requested_signature:
        print("\n[AUTOLENS CACHE] Dense settings changed.")
        print("Rebuilding DENSE stage only; sparse camera/lens solve will be kept.\n")
        shutil.rmtree(dense)

    dense.mkdir(parents=True, exist_ok=True)

    prepared = (
        (dense / "images").exists()
        and (dense / "sparse").exists()
        and (dense / "stereo").exists()
    )

    if not prepared:
        show_progress(
            7,
            "Preparing dense reconstruction",
            f"Undistorting registered views at max {settings['max_image_size']} px."
        )
        run([
            colmap, "image_undistorter",
            "--image_path", str(images_dir),
            "--input_path", str(best_model),
            "--output_path", str(dense),
            "--output_type", "COLMAP",
            "--max_image_size", str(settings["max_image_size"]),
        ], label="image_undistorter")
    else:
        show_progress(
            7,
            "Preparing dense reconstruction [CACHED]",
            "Existing undistorted dense workspace found; reusing it."
        )

    selected_names = write_dense_configs(
        dense,
        poses,
        observations,
        selection,
        settings["source_views"],
    )
    requested_signature = dense_settings_signature(settings, selected_names)
    settings_path.write_text(json.dumps(requested_signature, indent=2), encoding="utf-8")

    print_dense_config(settings, selection, len(poses))

    proxy = dense / "meshed-proxy.ply"
    clean_proxy = dense / "meshed-proxy-clean.ply"
    cleanup_report_path = dense / "autolens_proxy_cleanup.json"
    fused = dense / "fused.ply"
    raw_delaunay = dense / "meshed-delaunay.ply"
    raw_poisson = dense / "meshed-poisson.ply"

    # If a compatible simplified proxy already exists, the expensive dense
    # chain is done. v0.10.16 can still add/refresh the cleanup pass without
    # recomputing PatchMatch, fusion, or meshing.
    if proxy.exists():
        show_progress(
            8,
            "Computing dense depth maps [CACHED]",
            "Compatible proxy mesh already exists; skipping PatchMatch."
        )
        show_progress(
            9,
            "Fusing dense point cloud [CACHED]",
            "Compatible fused reconstruction already exists."
        )
        show_progress(
            10,
            "Cleaning projection mesh",
            "Removing spanning triangles from the cached proxy."
        )

        clean_proxy, cleanup_report = ensure_clean_proxy_mesh(
            proxy,
            clean_proxy,
            cleanup_report_path,
        )

        raw = raw_delaunay if raw_delaunay.exists() else raw_poisson
        method = "delaunay" if raw_delaunay.exists() else "poisson"

        return {
            "workspace": dense,
            "fused": fused,
            "raw_mesh": raw,
            "raw_proxy_ply": proxy,
            "proxy_ply": clean_proxy,
            "proxy_cleanup": cleanup_report,
            "proxy_cleanup_report": cleanup_report_path,
            "mesh_method": method,
            "selected_dense_images": selected_names,
            "selection_report": selection,
            "settings": settings,
            "resumed": True,
        }

    show_progress(
        8,
        "Computing dense depth maps",
        "PatchMatch resumes automatically and skips complete depth/normal maps."
    )

    patch_cmd = [
        colmap, "patch_match_stereo",
        "--workspace_path", str(dense),
        "--workspace_format", "COLMAP",
        "--PatchMatchStereo.geom_consistency",
        "true" if settings["geom_consistency"] else "false",
        "--PatchMatchStereo.filter", "true",
        "--PatchMatchStereo.max_image_size", str(settings["max_image_size"]),
        "--PatchMatchStereo.gpu_index", str(settings["gpu_index"]),
        "--PatchMatchStereo.cache_size", str(settings["patchmatch_cache_gb"]),
        "--PatchMatchStereo.window_step", str(settings["window_step"]),
        "--PatchMatchStereo.window_radius", str(settings["window_radius"]),
        "--PatchMatchStereo.num_iterations", str(settings["num_iterations"]),
        "--PatchMatchStereo.num_samples", str(settings["num_samples"]),
    ]
    run(patch_cmd, label="patch_match_stereo")

    input_type = "geometric" if settings["geom_consistency"] else "photometric"

    if not fused.exists():
        show_progress(
            9,
            "Fusing dense point cloud",
            f"Fusing {input_type} depth maps using {settings['fusion_cache_gb']} GB cache."
        )
        run([
            colmap, "stereo_fusion",
            "--workspace_path", str(dense),
            "--workspace_format", "COLMAP",
            "--input_type", input_type,
            "--output_path", str(fused),
            "--StereoFusion.max_image_size", str(settings["max_image_size"]),
            "--StereoFusion.cache_size", str(settings["fusion_cache_gb"]),
            "--StereoFusion.min_num_pixels", str(settings["fusion_min_num_pixels"]),
        ], label="stereo_fusion")
    else:
        show_progress(
            9,
            "Fusing dense point cloud [CACHED]",
            "Existing fused.ply found; reusing it."
        )

    show_progress(
        10,
        "Building projection mesh",
        "Meshing fused reconstruction and simplifying for Nuke."
    )

    method = "delaunay"
    raw = raw_delaunay

    if not raw_delaunay.exists() and not raw_poisson.exists():
        if not run_try([
            colmap, "delaunay_mesher",
            "--input_path", str(dense),
            "--output_path", str(raw_delaunay),
        ], label="delaunay_mesher"):
            method = "poisson"
            raw = raw_poisson
            print("\nDelaunay failed; using Poisson fallback.\n")
            run([
                colmap, "poisson_mesher",
                "--input_path", str(fused),
                "--output_path", str(raw_poisson),
            ], label="poisson_mesher")
    elif raw_delaunay.exists():
        method = "delaunay"
        raw = raw_delaunay
        print("\n[AUTOLENS CACHE] Reusing existing Delaunay mesh.\n")
    else:
        method = "poisson"
        raw = raw_poisson
        print("\n[AUTOLENS CACHE] Reusing existing Poisson mesh.\n")

    if not proxy.exists():
        if not run_try([
            colmap, "mesh_simplifier",
            "--input_path", str(raw),
            "--output_path", str(proxy),
            "--MeshSimplification.target_face_ratio",
            str(settings["proxy_face_ratio"]),
        ], label="mesh_simplifier"):
            print("\nMesh simplification failed; using unsimplified mesh.\n")
            proxy = raw

    clean_proxy, cleanup_report = ensure_clean_proxy_mesh(
        proxy,
        clean_proxy,
        cleanup_report_path,
    )

    return {
        "workspace": dense,
        "fused": fused,
        "raw_mesh": raw,
        "raw_proxy_ply": proxy,
        "proxy_ply": clean_proxy,
        "proxy_cleanup": cleanup_report,
        "proxy_cleanup_report": cleanup_report_path,
        "mesh_method": method,
        "selected_dense_images": selected_names,
        "selection_report": selection,
        "settings": settings,
        "resumed": False,
    }



# -------------------------------------------------------------------------
# Connected mesh-fit planar surface extraction (V0.6)
# -------------------------------------------------------------------------
def mesh_triangle_geometry(vertices: np.ndarray, triangles: np.ndarray):
    a=vertices[triangles[:,0]]; b=vertices[triangles[:,1]]; c=vertices[triangles[:,2]]
    cross=np.cross(b-a,c-a); lengths=np.linalg.norm(cross,axis=1); valid=lengths>1.0e-12
    triangles=triangles[valid]; a=a[valid]; b=b[valid]; c=c[valid]; cross=cross[valid]; lengths=lengths[valid]
    normals=cross/lengths[:,None]; areas=lengths*0.5; centroids=(a+b+c)/3.0
    return triangles,normals,areas,centroids


def triangulate_faces(faces: list[list[int]]) -> np.ndarray:
    triangles=[]
    for face in faces:
        for i in range(1,len(face)-1):
            triangles.append([face[0],face[i],face[i+1]])
    if not triangles:
        raise RuntimeError('Proxy mesh contains no triangulatable faces.')
    return np.asarray(triangles,dtype=np.int64)


def build_triangle_adjacency(triangles: np.ndarray):
    edge_faces=defaultdict(list)
    for fi,tri in enumerate(triangles):
        for edge in (tuple(sorted((int(tri[0]),int(tri[1])))),tuple(sorted((int(tri[1]),int(tri[2])))),tuple(sorted((int(tri[2]),int(tri[0]))))):
            edge_faces[edge].append(fi)
    adjacency=[[] for _ in range(len(triangles))]
    for ids in edge_faces.values():
        if len(ids)<2: continue
        for i in range(len(ids)):
            for j in range(i+1,len(ids)):
                a,b=ids[i],ids[j]; adjacency[a].append(b); adjacency[b].append(a)
    return adjacency


def fit_mesh_plane(points: np.ndarray):
    centroid=np.mean(points,axis=0); q=points-centroid; cov=q.T@q/max(len(points),1)
    vals,vecs=np.linalg.eigh(cov); normal=vecs[:,np.argmin(vals)]; normal/=np.linalg.norm(normal)
    distances=q@normal; rms=float(np.sqrt(np.mean(distances*distances))); max_abs=float(np.max(np.abs(distances)))
    return normal,centroid,rms,max_abs


def mesh_plane_basis(normal: np.ndarray):
    normal=normal/np.linalg.norm(normal); axes=np.eye(3); helper=axes[np.argmin(np.abs(axes@normal))]
    u=np.cross(normal,helper); u/=np.linalg.norm(u); v=np.cross(normal,u); v/=np.linalg.norm(v)
    return u,v


def build_meshfit_quad(points: np.ndarray, normal: np.ndarray, centroid: np.ndarray, padding: float):
    u,v=mesh_plane_basis(normal); pu=(points-centroid)@u; pv=(points-centroid)@v
    u0,u1=np.percentile(pu,[1.0,99.0]); v0,v1=np.percentile(pv,[1.0,99.0])
    du=(u1-u0)*padding; dv=(v1-v0)*padding; u0-=du; u1+=du; v0-=dv; v1+=dv
    return np.array([centroid+u*u0+v*v0,centroid+u*u1+v*v0,centroid+u*u1+v*v1,centroid+u*u0+v*v1])


def extract_meshfit_surfaces(vertices, faces, poses, angle_degrees=10.0, distance_ratio=0.0015, min_faces=25, min_area_fraction=0.002, max_surfaces=20, padding=0.04):
    triangles=triangulate_faces(faces); triangles,normals,areas,centroids=mesh_triangle_geometry(vertices,triangles); adjacency=build_triangle_adjacency(triangles)
    low=np.percentile(vertices,1.0,axis=0); high=np.percentile(vertices,99.0,axis=0); scene_diag=float(np.linalg.norm(high-low))
    tol=max(scene_diag*distance_ratio,1.0e-8); total_area=float(np.sum(areas)); min_area=total_area*min_area_fraction; cos_thr=math.cos(math.radians(angle_degrees))
    seed_order=np.argsort(-areas); claimed=np.zeros(len(triangles),dtype=bool); surfaces=[]
    for seed in seed_order:
        if claimed[seed]: continue
        seed_normal=normals[seed].copy(); seed_point=centroids[seed].copy(); queue=deque([int(seed)]); region=[]; claimed[seed]=True
        while queue:
            cur=queue.popleft(); region.append(cur)
            for nb in adjacency[cur]:
                if claimed[nb]: continue
                if abs(float(np.dot(normals[nb],seed_normal)))<cos_thr: continue
                if abs(float(np.dot(centroids[nb]-seed_point,seed_normal)))>tol: continue
                claimed[nb]=True; queue.append(nb)
        if len(region)<min_faces: continue
        region=np.asarray(region,dtype=np.int64); area=float(np.sum(areas[region]))
        if area<min_area: continue
        vids=np.unique(triangles[region].reshape(-1)); region_points=vertices[vids]
        normal,centroid,rms,max_abs=fit_mesh_plane(region_points)
        if rms>tol*1.5: continue
        if np.dot(normal,seed_normal)<0: normal=-normal
        corners=build_meshfit_quad(region_points,normal,centroid,padding)
        surfaces.append({'surface':len(surfaces)+1,'face_count':int(len(region)),'vertex_count':int(len(vids)),'area':area,'area_fraction':area/max(total_area,1e-12),'normal':normal.tolist(),'centroid':centroid.tolist(),'rms_planarity':rms,'max_planarity_error':max_abs,'corners':corners.tolist()})
        if len(surfaces)>=max_surfaces: break
    surfaces.sort(key=lambda x:x['area'],reverse=True)
    up=estimate_scene_up(poses); centers=np.asarray([p['center'] for p in poses],dtype=np.float64); camera_height=float(np.median(centers@up))
    for i,s in enumerate(surfaces,1):
        s['surface']=i; n=np.asarray(s['normal']); c=np.asarray(s['centroid']); align=abs(float(np.dot(n,up))); below=camera_height-float(np.dot(c,up))
        s['classification']='horizontal' if align>=0.72 else ('vertical' if align<=0.35 else 'angled'); s['camera_up_alignment']=align; s['below_camera_by']=below
    return surfaces,{'scene_diagonal':scene_diag,'distance_tolerance':tol,'total_mesh_area':total_area,'triangle_count':int(len(triangles))}


def write_meshfit_planes_obj(path: Path, surfaces: list[dict]):
    with path.open('w',encoding='utf-8') as f:
        f.write('# AutoLens V0.8 connected mesh-fit planar proxies\n'); base=1
        for s in surfaces:
            name=f"{s.get('classification','surface').upper()}_{s['surface']:02d}"; f.write(f'\ng {name}\n')
            for x,y,z in np.asarray(s['corners'],dtype=np.float64): f.write(f'v {x:.10g} {y:.10g} {z:.10g}\n')
            f.write('f '+' '.join(str(base+i) for i in range(4))+'\n'); base+=4

def radial_forward(x: np.ndarray, y: np.ndarray, k1: float, k2: float):
    r2 = x * x + y * y
    scale = 1.0 + k1 * r2 + k2 * r2 * r2
    return x * scale, y * scale


def radial_inverse(
    xd: np.ndarray,
    yd: np.ndarray,
    k1: float,
    k2: float,
    iterations: int = 12,
):
    xu = xd.copy()
    yu = yd.copy()

    for _ in range(iterations):
        r2 = xu * xu + yu * yu
        scale = 1.0 + k1 * r2 + k2 * r2 * r2
        safe = np.where(np.abs(scale) < 1.0e-8, 1.0e-8, scale)
        xu = xd / safe
        yu = yd / safe

    return xu, yu



# -------------------------------------------------------------------------
# User-drawn 2D surface guides -> solved 3D planar geometry (V0.7)
# -------------------------------------------------------------------------

def parse_image_xy_point_observations(images_txt: Path) -> dict[str, list[tuple[float, float, int]]]:
    with images_txt.open("r", encoding="utf-8", errors="replace") as f:
        lines = [line.rstrip("\n") for line in f if line.strip() and not line.startswith("#")]

    result = {}
    i = 0
    while i < len(lines):
        meta = lines[i].strip()
        pts = lines[i + 1].strip() if i + 1 < len(lines) else ""
        i += 2
        parts = meta.split()
        if len(parts) < 10:
            continue
        try:
            int(parts[0]); int(parts[8]); [float(v) for v in parts[1:8]]
        except ValueError:
            continue
        name = " ".join(parts[9:])
        obs = []
        vals = pts.split()
        for j in range(0, len(vals) - 2, 3):
            try:
                x = float(vals[j]); y = float(vals[j + 1]); pid = int(vals[j + 2])
            except ValueError:
                continue
            if pid >= 0:
                obs.append((x, y, pid))
        result[name] = obs
    return result


def point_in_polygon_2d(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) if abs(yj - yi) > 1.0e-12 else 1.0e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def find_pose_for_guide(guide: dict, poses: list[dict]) -> dict | None:
    """
    Match a guide to a solved pose.

    Exact source image / exact frame are preferred. If neither exists, choose
    the nearest registered frame. This safely clamps frames before/after the
    shot and handles occasional unregistered frames inside the range.
    """
    if not poses:
        return None

    source_image = guide.get("source_image")
    if source_image:
        target = Path(source_image).name.lower()
        for pose in poses:
            if Path(pose["name"]).name.lower() == target:
                return pose

    frame = guide.get("frame")
    if frame is not None:
        try:
            requested = int(frame)
        except Exception:
            requested = None

        if requested is not None:
            exact = [
                p for p in poses
                if int(p["frame"]) == requested
            ]
            if exact:
                return exact[0]

            return min(
                poses,
                key=lambda p: abs(int(p["frame"]) - requested),
            )

    return None


def guide_pixel_to_colmap(point, source_width, source_height, target_width, target_height, coordinate_system):
    x,y=float(point[0]),float(point[1])
    if coordinate_system == "nuke_bottom_left_pixels": y=(float(source_height)-1.0)-y
    x*=float(target_width)/max(float(source_width),1.0); y*=float(target_height)/max(float(source_height),1.0)
    return [x,y]


def pixel_to_world_ray(u: float, v: float, cam: dict, pose: dict):
    f = float(cam["f_px"])
    cx = float(cam["cx"])
    cy = float(cam["cy"])

    xd = np.asarray([(u - cx) / f], dtype=np.float64)
    yd = np.asarray([(v - cy) / f], dtype=np.float64)

    xu, yu = radial_inverse(
        xd,
        yd,
        float(cam["k1"]),
        float(cam["k2"]),
    )

    direction_cam = np.asarray(
        [xu[0], yu[0], 1.0],
        dtype=np.float64,
    )
    direction_cam /= np.linalg.norm(direction_cam)

    # Derive the COLMAP-camera-to-world rotation from the already exported
    # Nuke matrix. This keeps guide rays consistent after Auto Upright rotates
    # the world coordinate system.
    nuke_matrix = np.asarray(
        pose["nuke_matrix"],
        dtype=np.float64,
    )

    basis_flip = np.diag(
        [1.0, -1.0, -1.0]
    )

    r_cw = (
        nuke_matrix[:3, :3]
        @ basis_flip
    )

    direction_world = r_cw @ direction_cam
    direction_world /= np.linalg.norm(direction_world)

    origin = np.asarray(
        pose["center"],
        dtype=np.float64,
    )

    return origin, direction_world


def raycast_triangles(origin, direction, vertices, triangles, chunk_size=50000):
    """Nearest positive Moller-Trumbore intersection, chunked for large proxy meshes."""
    nearest_t = np.inf
    nearest = None
    eps = 1.0e-9
    for start in range(0, len(triangles), chunk_size):
        tri = triangles[start:start + chunk_size]
        v0 = vertices[tri[:, 0]]
        v1 = vertices[tri[:, 1]]
        v2 = vertices[tri[:, 2]]
        e1 = v1 - v0
        e2 = v2 - v0
        d = np.broadcast_to(direction, e2.shape)
        pvec = np.cross(d, e2)
        det = np.einsum("ij,ij->i", e1, pvec)
        valid = np.abs(det) > eps
        if not np.any(valid):
            continue
        inv_det = np.zeros_like(det)
        inv_det[valid] = 1.0 / det[valid]
        tvec = origin - v0
        uu = np.einsum("ij,ij->i", tvec, pvec) * inv_det
        valid &= (uu >= 0.0) & (uu <= 1.0)
        qvec = np.cross(tvec, e1)
        vv = np.einsum("j,ij->i", direction, qvec) * inv_det
        valid &= (vv >= 0.0) & ((uu + vv) <= 1.0)
        tt = np.einsum("ij,ij->i", e2, qvec) * inv_det
        valid &= tt > eps
        if not np.any(valid):
            continue
        candidate_ts = np.where(valid, tt, np.inf)
        idx = int(np.argmin(candidate_ts))
        if candidate_ts[idx] < nearest_t:
            nearest_t = float(candidate_ts[idx])
            nearest = origin + direction * nearest_t
    return nearest


def fit_plane_ransac(points: np.ndarray, threshold: float, iterations: int = 400):
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 3:
        return None
    if len(points) == 3:
        normal, centroid, rms, max_abs = fit_mesh_plane(points)
        return normal, centroid, np.arange(3), rms, max_abs

    rng = np.random.default_rng(1707)
    best = None
    best_count = 0
    best_error = np.inf
    iterations = min(iterations, max(80, len(points) * 20))

    for _ in range(iterations):
        ids = rng.choice(len(points), 3, replace=False)
        a, b, c = points[ids]
        normal = np.cross(b - a, c - a)
        norm = np.linalg.norm(normal)
        if norm < 1.0e-10:
            continue
        normal /= norm
        dist = np.abs((points - a) @ normal)
        inliers = np.flatnonzero(dist <= threshold)
        if len(inliers) < 3:
            continue
        error = float(np.median(dist[inliers]))
        if len(inliers) > best_count or (len(inliers) == best_count and error < best_error):
            best = inliers
            best_count = len(inliers)
            best_error = error

    if best is None:
        return None
    normal, centroid, rms, max_abs = fit_mesh_plane(points[best])
    return normal, centroid, best, rms, max_abs


def intersect_ray_plane(origin, direction, plane_point, plane_normal):
    denom = float(np.dot(direction, plane_normal))
    if abs(denom) < 1.0e-8:
        return None
    t = float(np.dot(plane_point - origin, plane_normal) / denom)
    if t <= 1.0e-7:
        return None
    return origin + direction * t


def sample_polygon_pixels(polygon: list[list[float]], grid=4):
    samples = [list(p) for p in polygon]
    arr = np.asarray(polygon, dtype=np.float64)
    centroid = np.mean(arr, axis=0)
    samples.append(centroid.tolist())
    lo = np.min(arr, axis=0); hi = np.max(arr, axis=0)
    for iy in range(grid):
        for ix in range(grid):
            x = lo[0] + (ix + 0.5) / grid * (hi[0] - lo[0])
            y = lo[1] + (iy + 0.5) / grid * (hi[1] - lo[1])
            if point_in_polygon_2d(float(x), float(y), polygon):
                samples.append([float(x), float(y)])
    return samples


def sanitize_obj_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(name)).strip("_")
    return cleaned or "Surface"


def build_guided_surfaces(
    guide_json: Path,
    cam: dict,
    poses: list[dict],
    points3d: list[dict],
    images_txt: Path,
    proxy_vertices: np.ndarray,
    proxy_faces: list[list[int]],
    output: Path,
):
    guide = json.loads(
        guide_json.read_text(
            encoding="utf-8"
        )
    )
    coordinate_system = guide.get(
        "coordinate_system",
        "nuke_bottom_left_pixels",
    )

    # Parse image observations once, then choose a DIFFERENT solved camera pose
    # for each guide surface below.
    obs_by_image = parse_image_xy_point_observations(
        images_txt
    )

    xyz_by_id = {
        int(p["id"]): np.asarray(
            p["xyz"],
            dtype=np.float64,
        )
        for p in points3d
    }
    triangles = triangulate_faces(proxy_faces)

    low = np.percentile(proxy_vertices, 1.0, axis=0)
    high = np.percentile(proxy_vertices, 99.0, axis=0)
    scene_diag = max(float(np.linalg.norm(high - low)), 1.0e-6)
    threshold = scene_diag * 0.004

    surfaces_out = []
    report = []
    guide_width = float(guide.get("width") or cam["width"])
    guide_height = float(guide.get("height") or cam["height"])

    matched_frames = []
    matched_images = []

    for index, surface in enumerate(
        guide.get("surfaces", []),
        1,
    ):
        name = str(
            surface.get("name")
            or f"Surface_{index:02d}"
        )

        raw_poly = surface.get(
            "points",
            [],
        )
        if len(raw_poly) < 3:
            continue

        # New v0.10.30 behavior: each surface may carry its own frame/image.
        # Legacy guide files still work because missing per-surface values fall
        # back to the old top-level guide frame/image.
        surface_guide = dict(guide)

        if surface.get("guide_frame") is not None:
            surface_guide["frame"] = surface.get(
                "guide_frame"
            )
        elif surface.get("frame") is not None:
            surface_guide["frame"] = surface.get(
                "frame"
            )

        if surface.get("source_image"):
            surface_guide["source_image"] = surface.get(
                "source_image"
            )

        requested_frame = surface_guide.get(
            "frame"
        )
        requested_source_image = surface_guide.get(
            "source_image"
        )

        pose = find_pose_for_guide(
            surface_guide,
            poses,
        )

        if pose is None:
            report.append({
                "name": name,
                "status": "camera_pose_not_found",
                "guide_frame": requested_frame,
            })
            continue

        used_frame = int(
            pose["frame"]
        )
        used_source_image = Path(
            pose["name"]
        ).name

        matched_frames.append(
            used_frame
        )
        matched_images.append(
            used_source_image
        )

        try:
            frame_adjusted = (
                requested_frame is not None
                and int(requested_frame)
                != used_frame
            )
        except Exception:
            frame_adjusted = False

        image_adjusted = (
            bool(requested_source_image)
            and Path(
                requested_source_image
            ).name.lower()
            != used_source_image.lower()
        )

        if frame_adjusted or image_adjusted:
            registered_frames = sorted(
                int(p["frame"])
                for p in poses
            )
            print(
                "[AUTOLENS GUIDE] "
                f"{name}: guide camera auto-corrected "
                f"{requested_frame} -> {used_frame} "
                f"(registered range "
                f"{registered_frames[0]}–"
                f"{registered_frames[-1]})."
            )

        pose_obs = None
        for image_name, obs in obs_by_image.items():
            if (
                Path(image_name).name.lower()
                == used_source_image.lower()
            ):
                pose_obs = obs
                break

        pose_obs = pose_obs or []

        polygon = [
            guide_pixel_to_colmap(
                p,
                guide_width,
                guide_height,
                cam["width"],
                cam["height"],
                coordinate_system,
            )
            for p in raw_poly
        ]

        sparse_candidates = []
        for x, y, pid in pose_obs:
            if point_in_polygon_2d(x, y, polygon) and pid in xyz_by_id:
                sparse_candidates.append(xyz_by_id[pid])

        # Cast a compact set of rays through the guide. Keep the actual ray
        # alongside each proxy hit so we can compare hit depth against sparse
        # scene evidence instead of blindly accepting the nearest mesh sheet.
        mesh_hit_records = []

        for u, v in sample_polygon_pixels(
            polygon,
            grid=4,
        ):
            origin, direction = pixel_to_world_ray(
                u,
                v,
                cam,
                pose,
            )
            hit = raycast_triangles(
                origin,
                direction,
                proxy_vertices,
                triangles,
            )

            if hit is not None:
                mesh_hit_records.append({
                    "uv": (float(u), float(v)),
                    "origin": np.asarray(
                        origin,
                        dtype=np.float64,
                    ),
                    "direction": np.asarray(
                        direction,
                        dtype=np.float64,
                    ),
                    "hit": np.asarray(
                        hit,
                        dtype=np.float64,
                    ),
                })

        fit_source = None
        fit = None

        sparse_arr = (
            np.asarray(
                sparse_candidates,
                dtype=np.float64,
            )
            if sparse_candidates
            else np.empty((0, 3))
        )

        raw_proxy_hit_count = len(
            mesh_hit_records
        )
        rejected_proxy_too_close = 0
        rejected_proxy_plane_depth = 0

        # Even one or two sparse points can serve as a conservative depth
        # sanity check. We only reject proxy hits that are dramatically closer
        # than the lower end of the sparse depth distribution.
        if len(sparse_arr) and mesh_hit_records:
            camera_origin = np.asarray(
                pose["center"],
                dtype=np.float64,
            )

            sparse_depths = np.linalg.norm(
                sparse_arr - camera_origin,
                axis=1,
            )
            sparse_depths = sparse_depths[
                np.isfinite(sparse_depths)
                & (sparse_depths > 1.0e-9)
            ]

            if len(sparse_depths):
                sparse_low_depth = float(
                    np.percentile(
                        sparse_depths,
                        20.0,
                    )
                )

                # "Dramatically closer" is intentionally conservative. A real
                # sloped floor/wall can vary in depth; a phantom foreground
                # bridge is commonly much closer than all sparse evidence.
                minimum_proxy_depth = max(
                    scene_diag * 0.002,
                    sparse_low_depth * 0.70,
                )

                depth_filtered_records = []

                for record in mesh_hit_records:
                    hit_depth = float(
                        np.linalg.norm(
                            record["hit"]
                            - record["origin"]
                        )
                    )

                    if (
                        np.isfinite(hit_depth)
                        and hit_depth >= minimum_proxy_depth
                    ):
                        depth_filtered_records.append(
                            record
                        )
                    else:
                        rejected_proxy_too_close += 1

                mesh_hit_records = (
                    depth_filtered_records
                )

        # Without enough sparse support for a plane, also suppress extreme
        # near-depth outliers relative to the rest of the guide's proxy hits.
        # This catches an isolated foreground bridge covering only a few rays.
        if (
            len(sparse_arr) < 3
            and len(mesh_hit_records) >= 5
        ):
            hit_depths = np.asarray([
                np.linalg.norm(
                    record["hit"]
                    - record["origin"]
                )
                for record in mesh_hit_records
            ], dtype=np.float64)

            finite_depths = hit_depths[
                np.isfinite(hit_depths)
                & (hit_depths > 1.0e-9)
            ]

            if len(finite_depths) >= 5:
                median_depth = float(
                    np.median(finite_depths)
                )
                mad_depth = float(
                    np.median(
                        np.abs(
                            finite_depths
                            - median_depth
                        )
                    )
                )

                robust_floor = min(
                    median_depth * 0.70,
                    median_depth
                    - 3.0 * mad_depth,
                )
                robust_floor = max(
                    robust_floor,
                    median_depth * 0.45,
                    scene_diag * 0.002,
                )

                depth_filtered_records = []

                for record in mesh_hit_records:
                    hit_depth = float(
                        np.linalg.norm(
                            record["hit"]
                            - record["origin"]
                        )
                    )

                    if hit_depth >= robust_floor:
                        depth_filtered_records.append(
                            record
                        )
                    else:
                        rejected_proxy_too_close += 1

                mesh_hit_records = (
                    depth_filtered_records
                )

        mesh_hits_arr = (
            np.asarray(
                [
                    record["hit"]
                    for record in mesh_hit_records
                ],
                dtype=np.float64,
            )
            if mesh_hit_records
            else np.empty((0, 3))
        )

        # Prefer actual sparse observations that COLMAP matched inside the
        # artist's guide. They correspond to visible image evidence and are
        # much less likely than a triangulated proxy to contain a phantom
        # foreground sheet bridging unrelated objects.
        if len(sparse_arr) >= 3:
            sparse_fit = fit_plane_ransac(
                sparse_arr,
                threshold * 1.5,
                iterations=500,
            )

            if sparse_fit is not None:
                fit = sparse_fit
                fit_source = "sparse_points"

                # Dense proxy hits are allowed to refine the plane ONLY when
                # they agree with the sparse plane. This prevents a bad
                # nearest-hit wall from pulling a floor guide forward.
                n0, c0, _, _, _ = sparse_fit

                if mesh_hit_records:
                    compatible_mesh = []

                    for record in mesh_hit_records:
                        hit = record["hit"]

                        # First gate: hit must geometrically agree with the
                        # sparse plane.
                        plane_distance = abs(
                            float(
                                np.dot(
                                    hit - c0,
                                    n0,
                                )
                            )
                        )

                        if plane_distance > threshold * 2.0:
                            continue

                        # Second gate: a proxy sheet substantially in front of
                        # where this exact camera ray reaches the sparse plane
                        # is treated as an occluding/phantom bridge, even if its
                        # broad plane happens to look numerically similar.
                        expected = intersect_ray_plane(
                            record["origin"],
                            record["direction"],
                            c0,
                            n0,
                        )

                        if expected is None:
                            rejected_proxy_plane_depth += 1
                            continue

                        hit_depth = float(
                            np.linalg.norm(
                                hit
                                - record["origin"]
                            )
                        )
                        expected_depth = float(
                            np.linalg.norm(
                                expected
                                - record["origin"]
                            )
                        )

                        dramatic_foreground = (
                            hit_depth
                            < expected_depth * 0.75
                            and (
                                expected_depth - hit_depth
                            )
                            > scene_diag * 0.008
                        )

                        if dramatic_foreground:
                            rejected_proxy_plane_depth += 1
                            continue

                        compatible_mesh.append(
                            hit
                        )

                    compatible_mesh = np.asarray(
                        compatible_mesh,
                        dtype=np.float64,
                    )

                    if len(compatible_mesh) >= 3:
                        combined = np.vstack(
                            [
                                sparse_arr,
                                compatible_mesh,
                            ]
                        )

                        refined = fit_plane_ransac(
                            combined,
                            threshold * 1.5,
                            iterations=350,
                        )

                        if refined is not None:
                            fit = refined
                            fit_source = (
                                "sparse_points+depth_gated_proxy"
                            )

        # Proxy mesh is now a fallback, not the first authority.
        if fit is None and len(mesh_hits_arr) >= 3:
            fit = fit_plane_ransac(
                mesh_hits_arr,
                threshold,
                iterations=300,
            )
            fit_source = "proxy_mesh"


        if fit is None:
            report.append({
                "name": name,
                "status": "insufficient_3d_support",
                "sparse_points_inside": int(len(sparse_arr)),
                "proxy_ray_hits_raw": int(raw_proxy_hit_count),
                "proxy_ray_hits_accepted": int(len(mesh_hits_arr)),
                "proxy_hits_rejected_too_close": int(
                    rejected_proxy_too_close
                ),
                "proxy_hits_rejected_plane_depth": int(
                    rejected_proxy_plane_depth
                ),
            })
            continue

        normal, centroid, inlier_ids, rms, max_abs = fit
        world_polygon = []
        for u, v in polygon:
            origin, direction = pixel_to_world_ray(u, v, cam, pose)
            pt = intersect_ray_plane(origin, direction, centroid, normal)
            if pt is not None and np.all(np.isfinite(pt)):
                world_polygon.append(pt)

        if len(world_polygon) < 3:
            report.append({
                "name": name,
                "status": "plane_ray_intersection_failed",
                "fit_source": fit_source,
            })
            continue

        world_polygon = np.asarray(world_polygon, dtype=np.float64)
        surfaces_out.append({
            "name": name,
            "vertices": world_polygon,
            "normal": normal,
            "centroid": centroid,
            "guide_frame": (
                int(requested_frame)
                if requested_frame is not None
                else used_frame
            ),
            "matched_frame": used_frame,
            "matched_image": used_source_image,
        })
        report.append({
            "name": name,
            "status": "ok",
            "guide_frame": (
                int(requested_frame)
                if requested_frame is not None
                else used_frame
            ),
            "matched_frame": used_frame,
            "matched_image": used_source_image,
            "fit_source": fit_source,
            "sparse_points_inside": int(len(sparse_arr)),
            "proxy_ray_hits_raw": int(raw_proxy_hit_count),
            "proxy_ray_hits_accepted": int(len(mesh_hits_arr)),
            "proxy_hits_rejected_too_close": int(
                rejected_proxy_too_close
            ),
            "proxy_hits_rejected_plane_depth": int(
                rejected_proxy_plane_depth
            ),
            "fit_inliers": int(len(inlier_ids)),
            "rms_planarity": float(rms),
            "max_planarity_error": float(max_abs),
            "normal": normal.tolist(),
            "centroid": centroid.tolist(),
            "vertex_count": int(len(world_polygon)),
        })

    obj_path = output / "autolens_guided_surfaces.obj"
    report_path = output / "autolens_guided_surfaces.json"
    individual_dir = output / "guided_surfaces"

    def planar_uvs_for_surface(vertices, normal):
        vertices = np.asarray(vertices, dtype=np.float64)
        normal = np.asarray(normal, dtype=np.float64)
        n = np.linalg.norm(normal)
        if n <= 1.0e-12:
            normal = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        else:
            normal = normal / n

        axes = np.eye(3, dtype=np.float64)
        helper = axes[np.argmin(np.abs(axes @ normal))]
        u_axis = np.cross(normal, helper)
        un = np.linalg.norm(u_axis)
        if un <= 1.0e-12:
            u_axis = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        else:
            u_axis = u_axis / un

        v_axis = np.cross(normal, u_axis)
        vn = np.linalg.norm(v_axis)
        if vn > 1.0e-12:
            v_axis = v_axis / vn

        center = np.mean(vertices, axis=0)
        local = vertices - center
        u = local @ u_axis
        v = local @ v_axis

        u_min, u_max = float(np.min(u)), float(np.max(u))
        v_min, v_max = float(np.min(v)), float(np.max(v))
        u_span = max(u_max - u_min, 1.0e-12)
        v_span = max(v_max - v_min, 1.0e-12)

        return np.column_stack([
            (u - u_min) / u_span,
            (v - v_min) / v_span,
        ])

    def write_one_surface_obj(path, surface, object_name):
        vertices = np.asarray(surface["vertices"], dtype=np.float64)
        uv = planar_uvs_for_surface(vertices, surface["normal"])
        with Path(path).open("w", encoding="utf-8") as f:
            f.write("# AutoLens guided surface with planar 0-1 UVs\n")
            f.write(f"o {sanitize_obj_name(object_name)}\n")
            f.write(f"g {sanitize_obj_name(object_name)}\n")
            for x, y, z in vertices:
                f.write(f"v {x:.10g} {y:.10g} {z:.10g}\n")
            for u, v in uv:
                f.write(f"vt {u:.10g} {v:.10g}\n")
            face = " ".join(
                f"{i + 1}/{i + 1}"
                for i in range(len(vertices))
            )
            f.write(f"f {face}\n")

    if individual_dir.exists():
        for stale in individual_dir.glob("*.obj"):
            try:
                stale.unlink()
            except OSError:
                pass

    if surfaces_out:
        individual_dir.mkdir(parents=True, exist_ok=True)

        with obj_path.open("w", encoding="utf-8") as f:
            f.write("# AutoLens guided surfaces with planar 0-1 UVs\n")
            vertex_base = 1
            uv_base = 1
            used_names = set()

            for index, surface in enumerate(surfaces_out, start=1):
                base_name = sanitize_obj_name(surface["name"]) or f"Surface_{index:02d}"
                unique_name = base_name
                suffix = 2
                while unique_name.lower() in used_names:
                    unique_name = f"{base_name}_{suffix:02d}"
                    suffix += 1
                used_names.add(unique_name.lower())

                vertices = np.asarray(surface["vertices"], dtype=np.float64)
                uv = planar_uvs_for_surface(vertices, surface["normal"])

                f.write(f"\no GUIDE_{unique_name}\n")
                f.write(f"g GUIDE_{unique_name}\n")
                for x, y, z in vertices:
                    f.write(f"v {x:.10g} {y:.10g} {z:.10g}\n")
                for u, v in uv:
                    f.write(f"vt {u:.10g} {v:.10g}\n")

                face = " ".join(
                    f"{vertex_base + i}/{uv_base + i}"
                    for i in range(len(vertices))
                )
                f.write(f"f {face}\n")

                individual_path = individual_dir / f"{unique_name}.obj"
                write_one_surface_obj(
                    individual_path,
                    surface,
                    f"GUIDE_{unique_name}",
                )
                surface["obj_file"] = str(individual_path)
                surface["obj_name"] = unique_name

                vertex_base += len(vertices)
                uv_base += len(vertices)
    elif obj_path.exists():
        obj_path.unlink()

    successful_index = 0
    for entry in report:
        if entry.get("status") != "ok":
            continue
        if successful_index < len(surfaces_out):
            meta = surfaces_out[successful_index]
            entry["obj_file"] = meta.get("obj_file")
            entry["obj_name"] = meta.get("obj_name")
            entry["uv_projection"] = "planar_0_1"
        successful_index += 1

    report_payload = {
        "version": "0.10.30",
        "guide_file": str(guide_json),
        "matched_images": sorted(
            set(matched_images)
        ),
        "matched_frames": sorted(
            set(matched_frames)
        ),
        "multi_frame_guides": (
            len(
                set(matched_frames)
            )
            > 1
        ),
        "scene_plane_threshold": threshold,
        "surfaces": report,
        "successful_surface_count": len(surfaces_out),
    }
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
    return (obj_path if surfaces_out else None), report_path, report_payload

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_obj_mesh(path: Path):
    """Minimal OBJ mesh reader used for guides-only fallback."""
    vertices = []
    faces = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    vertices.append(
                        [float(parts[1]), float(parts[2]), float(parts[3])]
                    )

            elif line.startswith("f "):
                face = []
                for token in line.split()[1:]:
                    token = token.split("/")[0]
                    if not token:
                        continue
                    index = int(token)
                    if index < 0:
                        index = len(vertices) + index
                    else:
                        index -= 1
                    face.append(index)

                if len(face) >= 3:
                    faces.append(face)

    if not vertices or not faces:
        raise RuntimeError(f"Could not parse proxy OBJ mesh: {path}")

    return np.asarray(vertices, dtype=np.float64), faces


def _load_guides_only_state(output: Path):
    result_path = output / "autolens_result.json"
    camera_track_path = output / "autolens_camera_track.json"

    if not result_path.exists():
        raise RuntimeError(
            "Guides-only refresh requires an existing completed AutoLens result."
        )
    if not camera_track_path.exists():
        raise RuntimeError(
            "Guides-only refresh requires cached camera-track data."
        )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    camera_track = json.loads(
        camera_track_path.read_text(encoding="utf-8")
    )

    cam = result.get("camera")
    if not isinstance(cam, dict):
        raise RuntimeError("Cached result does not contain solved camera data.")

    poses = camera_track.get("frames") or camera_track.get("poses") or []
    if not poses:
        raise RuntimeError("Cached camera track contains no solved poses.")

    best_model_value = result.get("best_model_path")
    if not best_model_value:
        raise RuntimeError("Cached result does not contain best_model_path.")

    best_model = Path(best_model_value)
    best_txt = best_model / "txt"
    images_txt = best_txt / "images.txt"
    points_txt = best_txt / "points3D.txt"

    if not images_txt.exists() or not points_txt.exists():
        raise RuntimeError(
            "Cached sparse TXT model is missing images.txt or points3D.txt."
        )

    points = parse_points3d(points_txt)

    world_orientation = (
        result.get("world_orientation")
        or {}
    )

    world_rotation = np.asarray(
        world_orientation.get("rotation_3x3")
        or np.eye(3),
        dtype=np.float64,
    )

    points = apply_world_rotation_to_points(
        points,
        world_rotation,
    )

    geometry = result.get("geometry_outputs") or {}
    proxy_obj_value = geometry.get("proxy_mesh_obj")
    proxy_obj = (
        Path(proxy_obj_value)
        if proxy_obj_value
        else output / "autolens_proxy_mesh.obj"
    )

    dense = result.get("dense") or {}
    dense_workspace = dense.get("workspace")

    clean_proxy_value = dense.get("clean_proxy_ply")
    if clean_proxy_value:
        proxy_ply = Path(clean_proxy_value)
    elif dense_workspace:
        clean_candidate = (
            Path(dense_workspace)
            / "meshed-proxy-clean.ply"
        )
        raw_candidate = (
            Path(dense_workspace)
            / "meshed-proxy.ply"
        )
        proxy_ply = (
            clean_candidate
            if clean_candidate.exists()
            else raw_candidate
        )
    else:
        proxy_ply = None

    if proxy_ply is not None and proxy_ply.exists():
        proxy_vertices, proxy_faces = read_ply_mesh(
            proxy_ply
        )
        proxy_vertices = apply_world_rotation_to_vertices(
            proxy_vertices,
            world_rotation,
        )
    elif proxy_obj.exists():
        proxy_vertices, proxy_faces = read_obj_mesh(proxy_obj)
    else:
        raise RuntimeError(
            "Guides-only refresh requires the cached proxy mesh."
        )

    meshfit_obj_value = geometry.get("meshfit_planes_obj")
    meshfit_obj = (
        Path(meshfit_obj_value)
        if meshfit_obj_value
        else output / "autolens_meshfit_planes.obj"
    )
    if not meshfit_obj.exists():
        meshfit_obj = None

    meshfit_surfaces = (
        (result.get("meshfit_surface_detection") or {}).get("surfaces")
        or []
    )

    source_format = result.get("source_format") or {"width": int(cam["width"]), "height": int(cam["height"]), "pixel_aspect": 1.0, "display_width": float(cam["width"]), "display_height": float(cam["height"])}
    nuke_cam = result.get("nuke_camera")
    if not isinstance(nuke_cam, dict): nuke_cam = camera_to_nuke_display_space(cam, source_format)
    lens_model = fit_nuke_lens_model(nuke_cam)
    lens_model_json = output / "autolens_lens_model.json"
    lens_model_json.write_text(
        json.dumps(lens_model, indent=2),
        encoding="utf-8",
    )

    projection = result.get("nuke_projection") or {}
    aperture_mm = float(projection.get("haperture_mm") or 36.0)

    return {
        "result": result,
        "result_path": result_path,
        "camera_track_path": camera_track_path,
        "cam": cam,
        "nuke_cam": nuke_cam,
        "source_format": source_format,
        "poses": poses,
        "points": points,
        "images_txt": images_txt,
        "proxy_vertices": proxy_vertices,
        "proxy_faces": proxy_faces,
        "proxy_obj": proxy_obj if proxy_obj.exists() else None,
        "meshfit_obj": meshfit_obj,
        "meshfit_surfaces": meshfit_surfaces,
        "lens_model": lens_model,
        "lens_model_json": lens_model_json,
        "aperture_mm": aperture_mm,
    }


def refresh_guided_surfaces_only(
    args,
    images_dir: Path,
    output: Path,
    images: list[Path],
):
    if not args.surface_guides:
        raise RuntimeError("--guides-only requires --surface-guides.")

    guide_path = Path(args.surface_guides).resolve()
    if not guide_path.exists():
        raise RuntimeError(f"Surface guide JSON does not exist: {guide_path}")

    print("\n============================================================")
    print("AUTOLENS GUIDED-SURFACES REFRESH")
    print("============================================================")
    print("Reusing completed camera / sparse solve / proxy mesh.")
    print("No tracking or dense reconstruction stages will run.\n")

    state = _load_guides_only_state(output)
    guide_hash = file_sha256(guide_path)

    previous = state["result"].get("guided_surfaces") or {}
    previous_hash = previous.get("guide_sha256")
    previous_obj_value = (
        (state["result"].get("geometry_outputs") or {}).get(
            "guided_surfaces_obj"
        )
    )
    previous_obj = (
        Path(previous_obj_value)
        if previous_obj_value
        else output / "autolens_guided_surfaces.obj"
    )

    if previous_hash == guide_hash and previous_obj.exists():
        print(
            "[AUTOLENS GUIDE] Guides are unchanged from the last fitted "
            "result; guided geometry is already current."
        )
        return 0

    print(f"[AUTOLENS GUIDE] Re-fitting current guides:\n  {guide_path}")

    guided_obj, guided_report, guided_metadata = build_guided_surfaces(
        guide_path,
        state["cam"],
        state["poses"],
        state["points"],
        state["images_txt"],
        state["proxy_vertices"],
        state["proxy_faces"],
        output,
    )

    guided_metadata = guided_metadata or {}
    guided_metadata["guide_sha256"] = guide_hash
    guided_metadata["refresh_mode"] = "guides_only"
    guided_metadata["refreshed_at_unix"] = time.time()

    if guided_report and Path(guided_report).exists():
        try:
            payload = json.loads(
                Path(guided_report).read_text(encoding="utf-8")
            )
            payload["guide_sha256"] = guide_hash
            payload["refresh_mode"] = "guides_only"
            Path(guided_report).write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    result = state["result"]
    result["version"] = "0.10.7"
    result["lens_distortion"] = result.get("lens_distortion") or {}
    result["lens_distortion"]["native_nuke_model"] = state["lens_model"]
    result["lens_distortion"]["model_json"] = str(
        state["lens_model_json"]
    )

    geometry = result.get("geometry_outputs") or {}
    geometry["guided_surfaces_obj"] = (
        str(guided_obj) if guided_obj else None
    )
    result["geometry_outputs"] = geometry
    result["guided_surfaces"] = guided_metadata

    state["result_path"].write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    nk_path = output / "AutoLens_v08.nk"
    write_nuke_script(
        nk_path,
        images,
        state["lens_model"],
        state["nuke_cam"],
        state["result_path"],
        state["poses"],
        state["camera_track_path"],
        state["aperture_mm"],
        proxy_obj=state["proxy_obj"],
        meshfit_planes_obj=state["meshfit_obj"],
        meshfit_surfaces=state["meshfit_surfaces"],
        guided_surfaces_obj=guided_obj,
        guided_surface_count=int(
            guided_metadata.get("successful_surface_count", 0)
        ),
        source_format=state["source_format"],
    )

    print("\n[AUTOLENS GUIDE] Guided-surface refresh complete.")
    print(
        f"  Successful guided surfaces: "
        f"{guided_metadata.get('successful_surface_count', 0)}"
    )
    print(f"  Guided OBJ: {guided_obj}")
    print(f"  Updated result: {nk_path}")
    print("============================================================\n")

    return 0



def generate_stmap(
    cam: dict,
    mode: str,
    tile_rows: int = 192,
) -> np.ndarray:
    """
    Generate one STMap at a time in row tiles.

    mode:
      "undistort" -> ideal output samples distorted plate
      "redistort" -> distorted output samples undistorted comp

    This keeps peak working memory low even for 3K/4K plates and provides a
    natural place for progress logging.
    """
    if mode not in ("undistort", "redistort"):
        raise ValueError(f"Unknown STMap mode: {mode}")

    w = int(cam["width"])
    h = int(cam["height"])
    f = float(cam["f_px"])
    cx = float(cam["cx"])
    cy = float(cam["cy"])
    k1 = float(cam["k1"])
    k2 = float(cam["k2"])

    result = np.zeros((h, w, 3), dtype=np.float32)
    cols = np.arange(w, dtype=np.float64)[None, :]

    tile_rows = max(16, int(tile_rows))
    tile_count = int(math.ceil(h / float(tile_rows)))

    print(
        f"[AUTOLENS STMAP] Generating {mode} map "
        f"{w}x{h} in {tile_count} tile(s)..."
    )

    for tile_index, y0 in enumerate(range(0, h, tile_rows), start=1):
        y1 = min(h, y0 + tile_rows)
        rows = np.arange(y0, y1, dtype=np.float64)[:, None]

        x_norm = (cols - cx) / f
        y_norm = (rows - cy) / f

        # Broadcasting avoids a full-resolution px/py meshgrid.
        if mode == "undistort":
            r2 = x_norm * x_norm + y_norm * y_norm
            scale = 1.0 + k1 * r2 + k2 * r2 * r2

            source_x = f * (x_norm * scale) + cx
            source_y_top = f * (y_norm * scale) + cy

        else:
            # REDISTORT: invert the radial model only for this row tile.
            xd = np.broadcast_to(x_norm, (y1 - y0, w))
            yd = np.broadcast_to(y_norm, (y1 - y0, w))

            xu, yu = radial_inverse(
                xd,
                yd,
                k1,
                k2,
            )

            source_x = f * xu + cx
            source_y_top = f * yu + cy

        result[y0:y1, :, 0] = (
            source_x / max(w - 1, 1)
        ).astype(np.float32)

        result[y0:y1, :, 1] = (
            1.0 - source_y_top / max(h - 1, 1)
        ).astype(np.float32)

        if (
            tile_index == 1
            or tile_index == tile_count
            or tile_index % max(1, tile_count // 4) == 0
        ):
            percent = int(round(tile_index / tile_count * 100.0))
            print(
                f"[AUTOLENS STMAP] {mode}: "
                f"{tile_index}/{tile_count} tile(s) ({percent}%)"
            )

    return result


def stmap_arrays(cam: dict):
    """
    Compatibility helper. The main pipeline now generates/maps/writes each
    STMap independently to reduce peak memory.
    """
    return (
        generate_stmap(cam, "undistort"),
        generate_stmap(cam, "redistort"),
    )



def read_colmap_dense_array(path: Path) -> np.ndarray:
    """Read COLMAP dense stereo .bin array format."""
    path = Path(path)
    with path.open("rb") as f:
        values = []
        while len(values) < 3:
            token = bytearray()
            while True:
                char = f.read(1)
                if not char:
                    raise RuntimeError(f"Unexpected EOF in dense map header: {path}")
                if char == b"&":
                    break
                token.extend(char)
            values.append(int(token.decode("ascii")))
        width, height, channels = values
        data = np.fromfile(f, dtype=np.float32)

    expected = width * height * channels
    if data.size != expected:
        raise RuntimeError(
            f"Dense map size mismatch for {path}: expected {expected}, got {data.size}"
        )

    data = data.reshape((width, height, channels), order="F")
    data = np.transpose(data, (1, 0, 2))
    if channels == 1:
        data = data[:, :, 0]
    return np.asarray(data, dtype=np.float32)


def parse_pinhole_camera_txt(path: Path) -> dict:
    """Parse the first undistorted dense camera."""
    with Path(path).open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue

            camera_id = int(parts[0])
            model = parts[1]
            width = int(parts[2])
            height = int(parts[3])
            params = [float(v) for v in parts[4:]]

            if model == "PINHOLE":
                fx, fy, cx, cy = params[:4]
            elif model == "SIMPLE_PINHOLE":
                fpx, cx, cy = params[:3]
                fx = fy = fpx
            elif model in ("SIMPLE_RADIAL", "RADIAL") and len(params) >= 3:
                fpx, cx, cy = params[:3]
                fx = fy = fpx
            else:
                raise RuntimeError(
                    f"Unsupported dense camera model for depth propagation: {model}"
                )

            return {
                "camera_id": camera_id,
                "model": model,
                "width": width,
                "height": height,
                "fx": float(fx),
                "fy": float(fy),
                "cx": float(cx),
                "cy": float(cy),
            }

    raise RuntimeError(f"No dense camera found in {path}")


def ensure_dense_model_txt(colmap, dense_workspace: Path) -> Path:
    dense_workspace = Path(dense_workspace)
    dense_sparse = dense_workspace / "sparse"
    dense_txt = dense_workspace / "sparse_txt"

    if (
        (dense_txt / "cameras.txt").exists()
        and (dense_txt / "images.txt").exists()
    ):
        return dense_txt

    if dense_txt.exists():
        shutil.rmtree(dense_txt, ignore_errors=True)
    dense_txt.mkdir(parents=True, exist_ok=True)

    run([
        colmap,
        "model_converter",
        "--input_path", str(dense_sparse),
        "--output_path", str(dense_txt),
        "--output_type", "TXT",
    ], label="dense_model_converter")

    return dense_txt


def find_dense_depth_map(dense_workspace: Path, image_name: str) -> Path | None:
    root = Path(dense_workspace) / "stereo" / "depth_maps"
    normalized = str(image_name).replace("\\", "/")

    for suffix in (".geometric.bin", ".photometric.bin"):
        candidate = root / (normalized + suffix)
        if candidate.exists():
            return candidate

    basename = Path(normalized).name
    for suffix in (".geometric.bin", ".photometric.bin"):
        candidate = root / (basename + suffix)
        if candidate.exists():
            return candidate
        matches = list(root.rglob(basename + suffix))
        if matches:
            return matches[0]
    return None


def pose_camera_to_world_rotation(pose: dict) -> np.ndarray:
    nuke_matrix = np.asarray(pose["nuke_matrix"], dtype=np.float64)
    basis_flip = np.diag([1.0, -1.0, -1.0])
    return nuke_matrix[:3, :3] @ basis_flip


def sampled_depth_camera_points(
    depth_map: np.ndarray,
    dense_camera: dict,
    max_points: int = 450000,
) -> dict:
    depth = np.asarray(depth_map, dtype=np.float32)
    if depth.ndim != 2:
        raise RuntimeError(f"Expected scalar depth map; got {depth.shape}")

    h, w = depth.shape
    stride = max(
        1,
        int(math.ceil(math.sqrt((h * w) / max(int(max_points), 1)))),
    )

    ys = np.arange(0, h, stride, dtype=np.int32)
    xs = np.arange(0, w, stride, dtype=np.int32)
    sample = depth[np.ix_(ys, xs)]
    valid = np.isfinite(sample) & (sample > 1.0e-8)

    if not np.any(valid):
        return {
            "points": np.empty((0, 3), dtype=np.float32),
            "stride": stride,
            "depth_width": int(w),
            "depth_height": int(h),
        }

    gx, gy = np.meshgrid(xs, ys)
    z = sample[valid].astype(np.float64, copy=False)
    u = gx[valid].astype(np.float64, copy=False)
    v = gy[valid].astype(np.float64, copy=False)

    sx = float(w) / max(float(dense_camera["width"]), 1.0)
    sy = float(h) / max(float(dense_camera["height"]), 1.0)
    fx = float(dense_camera["fx"]) * sx
    fy = float(dense_camera["fy"]) * sy
    cx = float(dense_camera["cx"]) * sx
    cy = float(dense_camera["cy"]) * sy

    x = ((u - cx) / max(fx, 1.0e-12)) * z
    y = ((v - cy) / max(fy, 1.0e-12)) * z

    return {
        "points": np.column_stack([x, y, z]).astype(np.float32, copy=False),
        "stride": int(stride),
        "depth_width": int(w),
        "depth_height": int(h),
    }


def output_depth_camera(nuke_cam: dict, source_format: dict) -> dict:
    pa = float(source_format.get("pixel_aspect", 1.0))
    return {
        "width": int(source_format["width"]),
        "height": int(source_format["height"]),
        "fx": float(nuke_cam["f_px"]) / max(pa, 1.0e-12),
        "fy": float(nuke_cam["f_px"]),
        "cx": float(nuke_cam["cx"]) / max(pa, 1.0e-12),
        "cy": float(nuke_cam["cy"]),
    }


def reproject_depth_points(
    source_points_cam: np.ndarray,
    source_pose: dict,
    target_pose: dict,
    target_camera: dict,
    output_depth: np.ndarray,
) -> int:
    points = np.asarray(source_points_cam, dtype=np.float64)
    if len(points) == 0:
        return 0

    source_center = np.asarray(source_pose["center"], dtype=np.float64)
    target_center = np.asarray(target_pose["center"], dtype=np.float64)
    source_r_cw = pose_camera_to_world_rotation(source_pose)
    target_r_cw = pose_camera_to_world_rotation(target_pose)

    relative_r = target_r_cw.T @ source_r_cw
    relative_t = target_r_cw.T @ (source_center - target_center)
    target_points = points @ relative_r.T + relative_t

    z = target_points[:, 2]
    valid = np.isfinite(z) & (z > 1.0e-8)
    if not np.any(valid):
        return 0

    target_points = target_points[valid]
    z = z[valid]

    u = target_camera["fx"] * target_points[:, 0] / z + target_camera["cx"]
    v = target_camera["fy"] * target_points[:, 1] / z + target_camera["cy"]

    ui = np.rint(u).astype(np.int64, copy=False)
    vi = np.rint(v).astype(np.int64, copy=False)
    width = int(target_camera["width"])
    height = int(target_camera["height"])

    inside = (
        np.isfinite(u)
        & np.isfinite(v)
        & (ui >= 0)
        & (ui < width)
        & (vi >= 0)
        & (vi < height)
    )
    if not np.any(inside):
        return 0

    ui = ui[inside]
    vi = vi[inside]
    z = z[inside].astype(np.float32, copy=False)

    flat_indices = vi * width + ui
    flat = output_depth.reshape(-1)
    np.minimum.at(flat, flat_indices, z)
    return int(len(z))


def expand_sparse_depth(depth: np.ndarray, iterations: int = 2) -> np.ndarray:
    result = np.asarray(depth, dtype=np.float32).copy()

    for _ in range(max(0, int(iterations))):
        invalid = (~np.isfinite(result)) | (result <= 0.0)
        if not np.any(invalid):
            break

        padded = np.pad(
            result,
            1,
            mode="constant",
            constant_values=np.inf,
        )
        neighbors = np.stack([
            padded[:-2, 1:-1],
            padded[2:, 1:-1],
            padded[1:-1, :-2],
            padded[1:-1, 2:],
        ])
        neighbors[(~np.isfinite(neighbors)) | (neighbors <= 0.0)] = np.inf
        nearest = np.min(neighbors, axis=0)
        fill = invalid & np.isfinite(nearest)
        result[fill] = nearest[fill]

    return result


def write_depth_exr(path: Path, depth: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    z = np.asarray(depth, dtype=np.float32)
    z = np.where(
        np.isfinite(z)
        & (z > 0.0)
        & (z < np.finfo(np.float32).max * 0.5),
        z,
        0.0,
    ).astype(np.float32, copy=False)
    alpha = (z > 0.0).astype(np.float32)

    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass

    if hasattr(OpenEXR, "File"):
        try:
            OpenEXR.File(
                {"compression": OpenEXR.ZIP_COMPRESSION},
                {
                    "depth.Z": np.ascontiguousarray(z),
                    "alpha": np.ascontiguousarray(alpha),
                },
            ).write(str(path))
            return
        except Exception as modern_error:
            print(
                "[AUTOLENS DEPTH] Modern EXR writer failed; "
                f"using legacy writer: {modern_error}"
            )

    h, w = z.shape
    header = OpenEXR.Header(int(w), int(h))
    pixel_type = Imath.PixelType(Imath.PixelType.FLOAT)
    header["compression"] = Imath.Compression(Imath.Compression.ZIP_COMPRESSION)
    header["channels"] = {
        "depth.Z": Imath.Channel(pixel_type),
        "alpha": Imath.Channel(pixel_type),
    }

    exr = OpenEXR.OutputFile(str(path), header)
    try:
        exr.writePixels({
            "depth.Z": np.ascontiguousarray(z).tobytes(),
            "alpha": np.ascontiguousarray(alpha).tobytes(),
        })
    finally:
        exr.close()


def choose_depth_keyframes_for_target(
    target_frame: int,
    keyframes: list[dict],
) -> list[dict]:
    if not keyframes:
        return []

    exact = [k for k in keyframes if int(k["frame"]) == int(target_frame)]
    if exact:
        return exact[:1]

    before = [k for k in keyframes if int(k["frame"]) < int(target_frame)]
    after = [k for k in keyframes if int(k["frame"]) > int(target_frame)]
    selected = []

    if before:
        selected.append(max(before, key=lambda k: int(k["frame"])))
    if after:
        selected.append(min(after, key=lambda k: int(k["frame"])))

    if len(selected) < 2:
        remaining = [k for k in keyframes if k not in selected]
        remaining.sort(key=lambda k: abs(int(k["frame"]) - int(target_frame)))
        selected.extend(remaining[: 2 - len(selected)])

    return selected[:2]


def build_propagated_depth_sequence(
    colmap,
    dense_info: dict,
    poses: list[dict],
    nuke_cam: dict,
    source_format: dict,
    output: Path,
) -> dict | None:
    dense_workspace = Path(dense_info["workspace"])
    selected_names = list(dense_info.get("selected_dense_images", []))

    if not selected_names:
        print("[AUTOLENS DEPTH] No selected dense references; skipping depth.")
        return None

    depth_dir = Path(output) / "depth"
    metadata_path = depth_dir / "autolens_depth_metadata.json"

    pose_by_name = {
        str(p["name"]).replace("\\", "/"): p
        for p in poses
    }
    pose_by_basename = {
        Path(str(p["name"])).name: p
        for p in poses
    }

    target_camera = output_depth_camera(nuke_cam, source_format)
    target_frames = [int(p["frame"]) for p in poses]

    signature = {
        "version": 1,
        "selected_dense_images": selected_names,
        "target_frames": target_frames,
        "width": int(target_camera["width"]),
        "height": int(target_camera["height"]),
        "fx": float(target_camera["fx"]),
        "fy": float(target_camera["fy"]),
        "cx": float(target_camera["cx"]),
        "cy": float(target_camera["cy"]),
    }

    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            complete = all(
                (depth_dir / f"autolens_depth.{frame:04d}.exr").exists()
                for frame in target_frames
            )
            if existing.get("signature") == signature and complete:
                print("[AUTOLENS DEPTH] Reusing complete propagated depth sequence.")
                return existing
        except Exception:
            pass

    dense_txt = ensure_dense_model_txt(colmap, dense_workspace)
    dense_camera = parse_pinhole_camera_txt(dense_txt / "cameras.txt")

    keyframes = []
    print("\n[AUTOLENS DEPTH] Preparing native PatchMatch depth keyframes...")

    for image_name in selected_names:
        normalized = str(image_name).replace("\\", "/")
        pose = (
            pose_by_name.get(normalized)
            or pose_by_basename.get(Path(normalized).name)
        )
        if pose is None:
            print(f"[AUTOLENS DEPTH WARNING] No solved pose for {image_name}")
            continue

        depth_path = find_dense_depth_map(dense_workspace, image_name)
        if depth_path is None:
            print(f"[AUTOLENS DEPTH WARNING] Missing native depth for {image_name}")
            continue

        depth_map = read_colmap_dense_array(depth_path)
        sample = sampled_depth_camera_points(depth_map, dense_camera)
        del depth_map

        if len(sample["points"]) < 100:
            print(f"[AUTOLENS DEPTH WARNING] Too few depth samples for {image_name}")
            continue

        keyframes.append({
            "name": image_name,
            "frame": int(pose["frame"]),
            "pose": pose,
            "depth_path": str(depth_path),
            "points": sample["points"],
            "sample_stride": int(sample["stride"]),
            "depth_width": int(sample["depth_width"]),
            "depth_height": int(sample["depth_height"]),
        })

        print(
            f"  frame {int(pose['frame'])}: "
            f"{len(sample['points']):,} sampled point(s), "
            f"stride {sample['stride']}"
        )

    keyframes.sort(key=lambda k: int(k["frame"]))

    if not keyframes:
        print("[AUTOLENS DEPTH WARNING] No usable native depth maps.")
        return None

    depth_dir.mkdir(parents=True, exist_ok=True)

    width = int(target_camera["width"])
    height = int(target_camera["height"])
    per_frame = []
    total = len(poses)

    print(
        "\n[AUTOLENS DEPTH] Reprojecting dense depth across "
        f"{total} solved timeline frame(s)..."
    )

    for index, target_pose in enumerate(poses, start=1):
        frame = int(target_pose["frame"])
        chosen = choose_depth_keyframes_for_target(frame, keyframes)

        zbuffer = np.full((height, width), np.inf, dtype=np.float32)
        projected_points = 0

        for key in chosen:
            projected_points += reproject_depth_points(
                key["points"],
                key["pose"],
                target_pose,
                target_camera,
                zbuffer,
            )

        valid_before = int(np.count_nonzero(np.isfinite(zbuffer)))
        zbuffer[~np.isfinite(zbuffer)] = 0.0
        zbuffer = expand_sparse_depth(zbuffer, iterations=2)
        valid_after = int(np.count_nonzero(zbuffer > 0.0))

        out_path = depth_dir / f"autolens_depth.{frame:04d}.exr"
        write_depth_exr(out_path, zbuffer)

        key_frames = [int(k["frame"]) for k in chosen]
        native = any(int(k["frame"]) == frame for k in chosen)
        coverage = valid_after / max(float(width * height), 1.0)

        per_frame.append({
            "frame": frame,
            "mode": "native_patchmatch" if native else "camera_reprojected",
            "source_depth_frames": key_frames,
            "projected_points": int(projected_points),
            "valid_pixels_before_fill": valid_before,
            "valid_pixels_after_fill": valid_after,
            "coverage": float(coverage),
            "file": str(out_path),
        })

        if (
            index == 1
            or index == total
            or index % max(1, total // 10) == 0
        ):
            print(
                "[AUTOLENS DEPTH] "
                f"{index}/{total} frame {frame} "
                f"keys={key_frames} coverage={coverage * 100.0:.1f}%"
            )

    first_frame = min(target_frames)
    last_frame = max(target_frames)
    frame_padding = max(4, max(len(str(abs(f))) for f in target_frames))
    pattern = str(
        depth_dir / ("autolens_depth." + f"%0{frame_padding}d.exr")
    )

    metadata = {
        "version": "0.10.30",
        "type": "smart_dense_camera_reprojected_depth",
        "signature": signature,
        "coordinate_space": "undistorted AutoLens VFX raster",
        "depth_semantics": (
            "positive target-camera +Z depth in AutoLens/COLMAP scene units"
        ),
        "native_dense_reference_count": len(keyframes),
        "native_dense_frames": [int(k["frame"]) for k in keyframes],
        "first_frame": int(first_frame),
        "last_frame": int(last_frame),
        "width": width,
        "height": height,
        "channels": ["depth.Z", "alpha"],
        "sequence_pattern": pattern,
        "hole_fallback": (
            "Imported Nuke graph uses rendered AutoLens geometry depth only "
            "where propagated alpha is zero."
        ),
        "frames": per_frame,
    }

    metadata_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    print("[AUTOLENS DEPTH] Full timeline depth sequence complete:")
    print(f"  {pattern}")
    return metadata


def write_exr_rgb(path: Path, rgb: np.ndarray) -> None:
    """
    Write a 32-bit floating-point RGB STMap.

    OpenEXR 3.x's legacy OutputFile/writePixels API requires splitting the
    packed RGB image into channel buffers and serializing each one to Python
    bytes. At 3K/4K resolution that creates several large temporary copies and
    can spend a long time in the deprecated writer.

    Prefer the supported OpenEXR.File API, which accepts the packed NumPy RGB
    array directly.

    STMaps are deterministic utility data, so NO_COMPRESSION is intentional:
      - fastest write/read path;
      - no compression CPU stall;
      - preserves full float precision;
      - avoids making Stage 6 look hung.

    The legacy writer remains only as a compatibility fallback.
    """
    started = time.perf_counter()

    rgb = np.ascontiguousarray(rgb, dtype=np.float32)
    h, w, c = rgb.shape

    if c != 3:
        raise RuntimeError(
            f"STMap EXR writer expected RGB data; got shape {rgb.shape}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    # Remove a partial file from an interrupted previous attempt.
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass

    print(
        f"[AUTOLENS EXR] Writing {path.name}: "
        f"{w}x{h}, RGB float32, NO_COMPRESSION"
    )

    modern_error = None

    if hasattr(OpenEXR, "File"):
        try:
            # Configure OpenEXR's worker pool before constructing the File.
            # Even though NO_COMPRESSION is lightweight, this also benefits
            # scanline/chunk processing in current OpenEXR versions.
            thread_count = max(
                1,
                min(int(os.cpu_count() or 1), 16),
            )

            if hasattr(OpenEXR, "set_global_thread_count"):
                try:
                    OpenEXR.set_global_thread_count(thread_count)
                except Exception:
                    pass

            header = {
                "compression": OpenEXR.NO_COMPRESSION,
                "type": OpenEXR.scanlineimage,
            }
            channels = {
                "RGB": rgb,
            }

            with OpenEXR.File(header, channels) as outfile:
                outfile.write(str(path))

            elapsed = time.perf_counter() - started
            size_mb = (
                path.stat().st_size / (1024.0 * 1024.0)
                if path.exists()
                else 0.0
            )

            if not path.exists() or path.stat().st_size <= 0:
                raise RuntimeError(
                    "OpenEXR.File returned without creating a valid file."
                )

            print(
                f"[AUTOLENS EXR] Finished {path.name}: "
                f"{size_mb:.1f} MB in {format_duration(elapsed)} "
                f"(OpenEXR.File)"
            )
            return

        except Exception as exc:
            modern_error = exc
            print(
                "[AUTOLENS EXR] Modern OpenEXR.File writer failed; "
                f"falling back to legacy writer: {exc}"
            )

            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    # Compatibility fallback for an older OpenEXR Python binding.
    try:
        header = OpenEXR.Header(w, h)
        pixel_type = Imath.PixelType(Imath.PixelType.FLOAT)

        header["channels"] = {
            "R": Imath.Channel(pixel_type),
            "G": Imath.Channel(pixel_type),
            "B": Imath.Channel(pixel_type),
        }

        # Explicitly disable EXR compression here as well.
        try:
            header["compression"] = Imath.Compression(
                Imath.Compression.NO_COMPRESSION
            )
        except Exception:
            pass

        out = OpenEXR.OutputFile(str(path), header)
        try:
            out.writePixels({
                "R": np.ascontiguousarray(rgb[..., 0]).tobytes(),
                "G": np.ascontiguousarray(rgb[..., 1]).tobytes(),
                "B": np.ascontiguousarray(rgb[..., 2]).tobytes(),
            })
        finally:
            out.close()

    except Exception as legacy_exc:
        if modern_error is not None:
            raise RuntimeError(
                "Both OpenEXR writers failed. "
                f"Modern File API: {modern_error}; "
                f"legacy OutputFile API: {legacy_exc}"
            ) from legacy_exc
        raise

    elapsed = time.perf_counter() - started
    size_mb = (
        path.stat().st_size / (1024.0 * 1024.0)
        if path.exists()
        else 0.0
    )

    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(
            "Legacy OpenEXR writer returned without creating a valid file."
        )

    print(
        f"[AUTOLENS EXR] Finished {path.name}: "
        f"{size_mb:.1f} MB in {format_duration(elapsed)} "
        f"(legacy fallback)"
    )


def escape_nuke_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def nuke_escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace('"', '\\"')
            .replace("\n", "\\n")
    )



def fit_nuke_lens_model(cam: dict, sample_count: int = 4096) -> dict:
    '''
    Fit COLMAP RADIAL to Nuke's native Custom Radial Standard 2/2 model.

    COLMAP:
        rd = ru * (1 + k1*ru^2 + k2*ru^4)

    Nuke custom rational model:
        ru = rd * (1 + n0*r^2 + n1*r^4)
                  / (1 + d0*r^2 + d1*r^4)

    The fit is performed over the actual usable image radius and uses Nuke's
    Maximum normalization. Outer-image samples receive greater weight because
    that is where lens-model mismatch is most visible.
    '''
    if str(cam.get("model", "")).upper() != "RADIAL":
        raise RuntimeError(
            "Native Nuke LensDistortion export currently supports "
            "COLMAP RADIAL solves only."
        )

    w = float(cam["width"])
    h = float(cam["height"])
    f = float(cam["f_px"])
    cx = float(cam["cx"])
    cy = float(cam["cy"])
    k1 = float(cam["k1"])
    k2 = float(cam["k2"])

    if f <= 0.0:
        raise RuntimeError("Solved focal length is invalid.")

    # Nuke Maximum radial coordinates are normalized by half of the
    # largest image dimension, not the full dimension.
    norm_px = max(w, h) * 0.5

    corners = (
        (0.0, 0.0),
        (w, 0.0),
        (0.0, h),
        (w, h),
    )
    max_radius_px = max(
        math.hypot(x - cx, y - cy)
        for x, y in corners
    )
    max_ru_colmap = max_radius_px / f

    count = max(256, int(sample_count))
    ru_c = np.linspace(
        max_ru_colmap / float(count),
        max_ru_colmap,
        count,
        dtype=np.float64,
    )

    ru2 = ru_c * ru_c
    radial_scale = 1.0 + k1 * ru2 + k2 * ru2 * ru2
    rd_c = ru_c * radial_scale

    # Convert to the Nuke "Maximum" normalized coordinate system.
    ru = ru_c * f / norm_px
    rd = rd_c * f / norm_px

    r2 = rd * rd
    r4 = r2 * r2

    # Cross-multiplied rational fit:
    #
    # ru*(1+d0*r2+d1*r4) = rd*(1+n0*r2+n1*r4)
    design = np.column_stack((
        ru * r2,
        ru * r4,
        -rd * r2,
        -rd * r4,
    ))
    target = rd - ru

    # Prioritize the outside of frame while retaining center stability.
    radius_fraction = np.clip(
        ru / max(float(ru[-1]), 1e-12),
        0.0,
        1.0,
    )
    weights = np.maximum(radius_fraction * radius_fraction, 1e-4)
    sw = np.sqrt(weights)

    params, *_ = np.linalg.lstsq(
        design * sw[:, None],
        target * sw,
        rcond=None,
    )

    d0, d1, n0, n1 = [float(v) for v in params]

    denominator = 1.0 + d0 * r2 + d1 * r4
    numerator = 1.0 + n0 * r2 + n1 * r4

    if np.any(np.abs(denominator) < 1e-8):
        raise RuntimeError(
            "Fitted Nuke LensDistortion model became singular in-frame."
        )

    ru_fit = rd * numerator / denominator
    error_px = (ru_fit - ru) * norm_px

    # Nuke center is expressed relative to image center in the chosen
    # normalization. COLMAP's image Y axis points down, so flip Y here.
    centre_x = (cx - w * 0.5) / norm_px
    centre_y = (h * 0.5 - cy) / norm_px

    return {
        "node_class": "LensDistortion2",
        "preset": "Custom",
        "lens_type": "Spherical",
        "projection": "Rectilinear",
        "model_type": "Radial Standard",
        "direction": "Forward",
        "normalisation": "Maximum",
        "normalisation_convention": "max(width,height)/2",
        "order": [2, 2],
        "exponent": [2, 2],
        "numerator": [n0, n1],
        "denominator": [d0, d1],
        "centre": [centre_x, centre_y],
        "normalisation_scale_px": norm_px,
        "source_model": "COLMAP RADIAL",
        "source_coefficients": {
            "k1": k1,
            "k2": k2,
            "f_px": f,
            "cx": cx,
            "cy": cy,
        },
        "fit": {
            "sample_count": count,
            "maximum_radius_px": max_radius_px,
            "rms_error_px": float(
                np.sqrt(np.mean(error_px * error_px))
            ),
            "max_abs_error_px": float(
                np.max(np.abs(error_px))
            ),
            "edge_error_px": float(error_px[-1]),
        },
    }


def nuke_lens_node_text(
    name: str,
    output_mode: str,
    lens_model: dict,
    xpos: int,
    ypos: int,
    label: str,
) -> str:
    '''Serialize a configured native LensDistortion2 node.

    Deliberately omit the lens/projection enum knobs. Nuke versions changed
    their scripting names/enum values (for example lensType -> lens, and
    Rectilinear -> None/rectilinear semantics). Spherical + rectilinear are
    the native defaults, so omitting them is cross-version safe.
    '''
    n0, n1 = lens_model["numerator"]
    d0, d1 = lens_model["denominator"]
    centre_x, centre_y = lens_model["centre"]

    lines = [
        "LensDistortion2 {",
        " inputs 1",
        " distortionModelPreset Custom",
        ' distortionModelType "Radial Standard"',
        " distortionOrder {2 2}",
        " distortionExponent {2 2}",
        " distortionModelDirection Forward",
        " normalisationType Maximum",
        f" distortionNumerator0 {format_nuke_number(n0)}",
        f" distortionNumerator1 {format_nuke_number(n1)}",
        f" distortionDenominator0 {format_nuke_number(d0)}",
        f" distortionDenominator1 {format_nuke_number(d1)}",
        (
            " centre {"
            f"{format_nuke_number(centre_x)} "
            f"{format_nuke_number(centre_y)}"
            "}"
        ),
        f" output {output_mode}",
        " useGPUIfAvailable true",
        f" name {name}",
        f' label "{nuke_escape_text(label)}"',
        f" xpos {int(xpos)}",
        f" ypos {int(ypos)}",
        "}",
    ]
    return "\n".join(lines)



def write_nuke_script(
    path: Path,
    source_images: list[Path],
    lens_model: dict,
    cam: dict,
    result_json: Path,
    poses: list[dict],
    camera_track_json: Path,
    aperture_mm: float,
    proxy_obj: Path | None = None,
    meshfit_planes_obj: Path | None = None,
    meshfit_surfaces: list[dict] | None = None,
    guided_surfaces_obj: Path | None = None,
    guided_surface_count: int = 0,
    source_format: dict | None = None,
):
    first = source_images[0]
    plate_path = escape_nuke_path(first)

    projection = camera_projection_values(cam, aperture_mm)
    focal_mm = projection["focal_mm"]
    hap = projection["haperture_mm"]
    vap = projection["vaperture_mm"]
    win_x, win_y = projection["win_translate"]

    first_frame = min(p["frame"] for p in poses)
    last_frame = max(p["frame"] for p in poses)
    matrix_block = nuke_camera_matrix_block(poses)

    fit = lens_model.get("fit", {})
    source_format = source_format or {"width": int(round(float(cam["width"]))), "height": int(round(float(cam["height"]))), "pixel_aspect": 1.0}
    root_width=int(source_format["width"]); root_height=int(source_format["height"]); root_pa=float(source_format.get("pixel_aspect",1.0))
    info = (
        "AUTOLENS V0.10.30 SOLVE\n"
        f"Model: {cam['model']}\n"
        f"Resolution: {root_width} x {root_height}  PAR {root_pa:g}\n"
        f"Registered camera frames: {len(poses)}\n"
        f"Nuke range: {first_frame} - {last_frame}\n"
        f"f: {cam['f_px']:.8f} px\n"
        f"k1: {cam['k1']:.12g}\n"
        f"k2: {cam['k2']:.12g}\n"
        f"Native LensDistortion RMS: {fit.get('rms_error_px', 0.0):.4f} px\n"
        f"Native LensDistortion max: {fit.get('max_abs_error_px', 0.0):.4f} px\n"
        f"Mesh-fit planes: {len(meshfit_surfaces or [])}\n"
        f"User-guided surfaces: {guided_surface_count}\n"
        f"Nuke focal: {focal_mm:.6f} mm @ {hap:.6f} mm aperture\n"
        f"Result JSON: {escape_nuke_path(result_json)}"
    )

    geo = ""

    if proxy_obj and proxy_obj.exists():
        geo += (
            "\nReadGeo2 {\n"
            " inputs 0\n"
            f' file "{escape_nuke_path(proxy_obj)}"\n'
            " name AUTOLENS_PROXY_MESH\n"
            ' label "PROXY MESH\nALT GEO"\n'
            " xpos 120\n"
            " ypos -20\n"
            "}\n"
        )

    if meshfit_planes_obj and meshfit_planes_obj.exists():
        geo += (
            "\nReadGeo2 {\n"
            " inputs 0\n"
            f' file "{escape_nuke_path(meshfit_planes_obj)}"\n'
            " name AUTOLENS_MESHFIT_PLANES\n"
            ' label "MESHFIT PLANES\nALT GEO"\n'
            " xpos 120\n"
            " ypos 150\n"
            "}\n"
        )

    if guided_surfaces_obj and guided_surfaces_obj.exists():
        geo += (
            "\nReadGeo2 {\n"
            " inputs 0\n"
            f' file "{escape_nuke_path(guided_surfaces_obj)}"\n'
            " name AUTOLENS_GUIDED_SURFACES\n"
            ' label "GUIDED SURFACES\nDEFAULT GEO"\n'
            " xpos 120\n"
            " ypos 320\n"
            "}\n"
        )

    fit_max = float(fit.get("max_abs_error_px", 0.0))

    und_node = nuke_lens_node_text(
        "AUTOLENS_UNDISTORT",
        "Undistort",
        lens_model,
        -520,
        -70,
        (
            "AUTOLENS UNDISTORT -> TRACKING SPACE\n"
            "COLMAP RADIAL -> Nuke Custom 2/2\n"
            f"fit max {fit_max:.3f}px"
        ),
    )

    red_node = nuke_lens_node_text(
        "AUTOLENS_REDISTORT",
        "Redistort",
        lens_model,
        -520,
        270,
        (
            "AUTOLENS TRACKING SPACE -> REDISTORT\n"
            "COLMAP RADIAL -> Nuke Custom 2/2\n"
            f"fit max {fit_max:.3f}px"
        ),
    )

    nk = f'''Root {{
 inputs 0
 name AutoLens_v01030
 first_frame {first_frame}
 last_frame {last_frame}
 format "{root_width} {root_height} 0 0 {root_width} {root_height} {format_nuke_number(root_pa)} AutoLens_Format"
}}

Read {{
 file "{plate_path}"
 first {first_frame}
 last {last_frame}
 origfirst {first_frame}
 origlast {last_frame}
 name PLATE_REPOINT_TO_SEQUENCE
 xpos -520
 ypos -250
}}
set PLATE [stack 0]

push $PLATE
{und_node}

Dot {{
 name AUTOLENS_TRACKING_PLATE
 label "UNDISTORTED TRACKING PLATE\\nTRACK / PROJECT / COMP HERE"
 xpos -486
 ypos 80
}}
set TRACKING_PLATE [stack 0]

push $TRACKING_PLATE
Dot {{
 name AUTOLENS_REDISTORT_INPUT
 label "CONNECT FINAL UNDISTORTED COMP HERE\\nthen redistort"
 xpos -486
 ypos 210
}}
{red_node}

{geo}

Camera2 {{
 inputs 0
 useMatrix true
 matrix {{
{matrix_block}
 }}
 focal {format_nuke_number(focal_mm)}
 haperture {format_nuke_number(hap)}
 vaperture {format_nuke_number(vap)}
 win_translate {{{format_nuke_number(win_x)} {format_nuke_number(win_y)}}}
 near 0.001
 far 1000000
 name AUTOLENS_CAMERA
 label "UNDISTORTED TRACKING CAMERA\\nUse with AUTOLENS_TRACKING_PLATE"
 xpos 470
 ypos -80
}}

Scene {{
 inputs 0
 name AUTOLENS_SCENE
 label "3D SCENE"
 xpos -145
 ypos 500
}}

ScanlineRender {{
 inputs 0
 name AUTOLENS_SCANLINE
 label "SCANLINE RENDER"
 xpos -145
 ypos 655
}}

Merge2 {{
 inputs 0
 operation over
 name AUTOLENS_MERGE
 label "CG OVER UNDISTORTED PLATE"
 xpos 0
 ypos 760
}}

Dot {{
 inputs 0
 name AUTOLENS_FINAL
 label "AUTOLENS FINAL"
 xpos 34
 ypos 1035
}}
'''

    path.write_text(nk, encoding="utf-8")

def main():
    total_started = time.perf_counter()

    parser = argparse.ArgumentParser(
        description="Solve lens/camera and create optimized projection-ready proxy geometry."
    )
    parser.add_argument("--images", required=True, help="Folder containing the image sequence.")
    parser.add_argument("--output", required=True, help="Output/work folder.")
    parser.add_argument("--colmap", default=None, help="Path/name of COLMAP executable.")
    parser.add_argument(
        "--camera-model",
        default="RADIAL",
        choices=["RADIAL"],
        help="V0.7 currently supports RADIAL only."
    )
    parser.add_argument(
        "--max-image-size",
        type=int,
        default=2400,
        help="Max image dimension used for sparse SIFT extraction."
    )
    parser.add_argument(
        "--use-gpu",
        action="store_true",
        help="Use GPU for sparse feature extraction/matching."
    )
    parser.add_argument(
        "--nuke-aperture-mm",
        type=float,
        default=36.0,
        help="Horizontal filmback used for the generated Nuke camera."
    )
    parser.add_argument("--source-width", type=int, default=0, help="Original Nuke source width.")
    parser.add_argument("--source-height", type=int, default=0, help="Original Nuke source height.")
    parser.add_argument("--source-pixel-aspect", type=float, default=1.0, help="Original Nuke source pixel aspect ratio.")

    parser.add_argument(
        "--write-stmaps",
        action="store_true",
        help=(
            "Optional legacy/debug output: also write undistort/redistort "
            "32-bit EXR STMaps. Native LensDistortion2 is the default. Final output also carries depth.Z rendered from the solved geometry."
        ),
    )

    parser.add_argument(
        "--surface-guides",
        default=None,
        help="Optional JSON exported from the Nuke AutoLens Surface Guides Roto node."
    )

    parser.add_argument(
        "--mask-path",
        default=None,
        help=(
            "Optional COLMAP feature-mask folder. Black pixels are ignored "
            "during feature extraction."
        ),
    )
    parser.add_argument(
        "--no-upright-world",
        action="store_true",
        help="Keep COLMAP's arbitrary world orientation instead of auto-uprighting it.",
    )

    parser.add_argument(
        "--guides-only",
        action="store_true",
        help=(
            "Refresh only guided surfaces from an existing completed track. "
            "Reuses cached camera, sparse solve and proxy mesh."
        ),
    )

    # Performance presets.
    parser.add_argument(
        "--quality",
        choices=["preview", "normal", "high"],
        default="normal",
        help=(
            "Dense reconstruction preset. "
            "All presets use smart viewpoint selection by default; "
            "normal targets ~12 high-resolution references."
        )
    )

    # Optional overrides. None means "use quality preset".
    parser.add_argument("--dense-max-image-size", type=int, default=None)
    parser.add_argument(
        "--dense-selection",
        choices=["smart", "step", "all"],
        default=None,
        help="Dense reference selection. Default comes from quality preset (smart)."
    )
    parser.add_argument(
        "--dense-target-frames",
        type=int,
        default=None,
        help="Maximum smart-selected dense reference frames."
    )
    parser.add_argument(
        "--dense-min-frames",
        type=int,
        default=None,
        help="Minimum smart-selected dense reference frames."
    )
    parser.add_argument(
        "--dense-coverage-target",
        type=float,
        default=None,
        help="Sparse scene-coverage target for smart selection, e.g. 0.97."
    )
    parser.add_argument(
        "--dense-frame-step",
        type=int,
        default=None,
        help="Used only when --dense-selection step."
    )
    parser.add_argument("--dense-source-views", type=int, default=None)
    parser.add_argument("--dense-window-step", type=int, default=None)
    parser.add_argument("--dense-window-radius", type=int, default=None)
    parser.add_argument("--dense-iterations", type=int, default=None)
    parser.add_argument("--dense-samples", type=int, default=None)
    parser.add_argument("--proxy-face-ratio", type=float, default=None)
    parser.add_argument("--fusion-min-num-pixels", type=int, default=None)

    parser.add_argument(
        "--geom-consistency",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override preset geometric consistency."
    )
    parser.add_argument(
        "--dense-gpus",
        default="auto",
        help='PatchMatch GPU indices, e.g. "0,1". Default auto uses all NVIDIA GPUs found.'
    )
    parser.add_argument(
        "--patchmatch-cache-gb",
        default="auto",
        help="PatchMatch RAM cache in GB or 'auto'."
    )
    parser.add_argument(
        "--fusion-cache-gb",
        default="auto",
        help="Stereo fusion RAM cache in GB or 'auto'."
    )

    # Cache / resume controls.
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete cached sparse+dense work and rebuild everything."
    )
    parser.add_argument(
        "--force-dense",
        action="store_true",
        help="Keep the sparse lens/camera solve but rebuild dense reconstruction."
    )

    # Mesh-fit settings.
    parser.add_argument("--meshfit-angle", type=float, default=10.0)
    parser.add_argument("--meshfit-distance-ratio", type=float, default=0.0015)
    parser.add_argument("--meshfit-min-faces", type=int, default=25)
    parser.add_argument("--meshfit-min-area-fraction", type=float, default=0.002)
    parser.add_argument("--meshfit-max-surfaces", type=int, default=20)
    parser.add_argument("--meshfit-padding", type=float, default=0.04)
    parser.add_argument("--skip-dense", action="store_true")

    args = parser.parse_args()

    # Catch packaging/runtime symbol errors before starting expensive COLMAP work.
    engine_runtime_preflight()

    images_dir = Path(args.images).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    if not images_dir.is_dir():
        raise RuntimeError(f"Images folder does not exist: {images_dir}")

    images = find_images(images_dir)

    if args.guides_only:
        raise SystemExit(
            refresh_guided_surfaces_only(
                args,
                images_dir,
                output,
                images,
            )
        )

    colmap = locate_colmap(args.colmap)
    dense_settings = resolve_dense_settings(args)

    cache_path = output / "autolens_cache.json"
    cache = load_json_if_exists(cache_path)
    current_signature = image_signature(images)

    current_mask_signature = directory_signature(
        Path(args.mask_path)
        if args.mask_path
        else None
    )

    if (
        cache
        and cache.get("mask_signature")
        != current_mask_signature
    ):
        clear_reconstruction_cache_for_mask_change(
            output,
            cache_path,
        )
        cache = None

    if args.force:
        print("\n[AUTOLENS CACHE] --force requested: clearing previous reconstruction.\n")
        for path in (
            output / "database.db",
            output / "database_global.db",
            output / "sparse",
            output / "sparse_global",
            output / "dense",
            output / "autolens_proxy_mesh.obj",
            output / "autolens_meshfit_planes.obj",
            output / "autolens_guided_surfaces.obj",
            output / "autolens_guided_surfaces.json",
            output / "autolens_result.json",
            output / "autolens_camera_track.json",
            output / "autolens_lens_model.json",
            output / "autolens_undistort.exr",
            output / "autolens_redistort.exr",
            output / "AutoLens_v08.nk",
            cache_path,
        ):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        cache = None

    if cache and cache.get("image_signature") != current_signature:
        cached_root = find_cached_sparse_root(output, cache)
        if cached_root is not None:
            raise RuntimeError(
                "Input image sequence has changed since the cached solve. "
                "Run again with --force to rebuild safely."
            )

    show_progress(
        1,
        "Preparing solve",
        f"Found {len(images)} source images. Output: {output}"
    )

    database = output / "database.db"
    sparse = output / "sparse"
    cached_sparse = find_cached_sparse_root(output, cache)

    if cached_sparse is not None:
        sparse = cached_sparse
        matcher_name = (cache or {}).get("matching_method", "cached")
        solve_method = (cache or {}).get("sfm_method", "cached")

        show_progress(
            2,
            "Extracting image features [CACHED]",
            "Existing sparse reconstruction found; feature extraction is not repeated."
        )
        show_progress(
            3,
            "Matching frames [CACHED]",
            "Existing sparse reconstruction found; feature matching is not repeated."
        )
        show_progress(
            4,
            "Solving camera and lens [CACHED]",
            f"Reusing sparse reconstruction: {sparse}"
        )
    else:
        sparse.mkdir(parents=True, exist_ok=True)

        show_progress(
            2,
            "Extracting image features",
            "COLMAP resumes existing database features when possible."
        )
        colmap_image_list = write_colmap_image_list(
            output,
            images_dir,
            images,
        )

        print(
            f"[AUTOLENS INPUT] Restricting COLMAP to "
            f"{len(images)} source frame(s) via {colmap_image_list.name}"
        )

        feat_cmd = [
            colmap, "feature_extractor",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--image_list_path", str(colmap_image_list),
            "--ImageReader.camera_model", args.camera_model,
            "--ImageReader.single_camera", "1",
            "--FeatureExtraction.type", "SIFT",
            "--FeatureExtraction.max_image_size", str(args.max_image_size),
            "--FeatureExtraction.use_gpu", "1" if args.use_gpu else "0",
        ]

        if args.mask_path:
            mask_folder = Path(
                args.mask_path
            ).resolve()

            if not mask_folder.is_dir():
                raise RuntimeError(
                    f"Tracking mask folder does not exist: {mask_folder}"
                )

            feat_cmd += [
                "--ImageReader.mask_path",
                str(mask_folder),
            ]

            print(
                "[AUTOLENS MASK] COLMAP feature extraction will ignore "
                "black mask regions."
            )

        run(feat_cmd, label="feature_extractor")

        if len(images) <= 250:
            matcher_name = "exhaustive"
            show_progress(
                3,
                "Matching frames",
                f"Exhaustively matching {len(images)} frames; database matching resumes when possible."
            )
            match_cmd = [
                colmap, "exhaustive_matcher",
                "--database_path", str(database),
                "--FeatureMatching.type", "SIFT_BRUTEFORCE",
                "--FeatureMatching.use_gpu", "1" if args.use_gpu else "0",
            ]
        else:
            matcher_name = "sequential"
            show_progress(
                3,
                "Matching frames",
                f"Sequentially matching {len(images)} frames."
            )
            match_cmd = [
                colmap, "sequential_matcher",
                "--database_path", str(database),
                "--FeatureMatching.type", "SIFT_BRUTEFORCE",
                "--FeatureMatching.use_gpu", "1" if args.use_gpu else "0",
            ]
        run(match_cmd, label=f"{matcher_name}_matcher")

        show_progress(
            4,
            "Solving camera and lens",
            "Trying incremental SfM first; global SfM remains the fallback."
        )

        mapper_cmd = [
            colmap, "mapper",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--output_path", str(sparse),
        ]

        solve_method = "incremental"
        mapper_ok = run_try(mapper_cmd, label="incremental_mapper")

        if not mapper_ok:
            print("\n" + "!" * 72)
            print("AUTOLENS FALLBACK")
            print("Incremental SfM could not find a valid initial image pair.")
            print("Trying view-graph calibration + GLOBAL SfM instead.")
            print("!" * 72 + "\n")

            global_db = output / "database_global.db"
            if global_db.exists():
                global_db.unlink()
            shutil.copy2(database, global_db)

            sparse_global = output / "sparse_global"
            if sparse_global.exists():
                shutil.rmtree(sparse_global)
            sparse_global.mkdir(parents=True, exist_ok=True)

            calibrator_ok = run_try([
                colmap, "view_graph_calibrator",
                "--database_path", str(global_db),
            ], label="view_graph_calibrator")

            global_ok = False
            if calibrator_ok:
                global_ok = run_try([
                    colmap, "global_mapper",
                    "--database_path", str(global_db),
                    "--image_path", str(images_dir),
                    "--output_path", str(sparse_global),
                ], label="global_mapper")

            if global_ok:
                sparse = sparse_global
                database = global_db
                solve_method = "global"
                print("\nAUTOLENS FALLBACK SUCCEEDED: using GLOBAL SfM reconstruction.\n")
            else:
                raise RuntimeError(
                    "Neither incremental nor global SfM could reconstruct this shot."
                )

    show_progress(
        5,
        "Evaluating reconstruction",
        "Choosing the reconstruction with the most successfully registered frames."
    )

    model_scores = []
    for model_dir, txt_dir in iter_models(sparse):
        # Only treat actual model directories as models.
        if not model_dir_is_valid(model_dir):
            continue

        cameras_txt = txt_dir / "cameras.txt"
        images_txt = txt_dir / "images.txt"
        points_txt = txt_dir / "points3D.txt"

        if not (cameras_txt.exists() and images_txt.exists() and points_txt.exists()):
            run([
                colmap, "model_converter",
                "--input_path", str(model_dir),
                "--output_path", str(txt_dir),
                "--output_type", "TXT",
            ], label="model_converter")

        count = count_registered_images(images_txt)
        model_scores.append((count, model_dir, txt_dir))

    if not model_scores:
        raise RuntimeError(f"No valid sparse model found under {sparse}")

    model_scores.sort(key=lambda x: x[0], reverse=True)
    reg_count, best_model, best_txt = model_scores[0]

    if reg_count < 3:
        raise RuntimeError(
            f"Best reconstruction registered only {reg_count} images."
        )

    cam = parse_camera(best_txt / "cameras.txt")
    solve_cam = cam
    source_format = source_format_from_args(args, solve_cam)
    nuke_cam = camera_to_nuke_display_space(solve_cam, source_format)
    poses = parse_colmap_images(
        best_txt / "images.txt",
        images,
    )
    points = parse_points3d(
        best_txt / "points3D.txt"
    )

    world_rotation = np.eye(
        3,
        dtype=np.float64,
    )

    if not args.no_upright_world:
        world_rotation = compute_upright_world_rotation(
            poses
        )

        poses = apply_world_rotation_to_poses(
            poses,
            world_rotation,
        )

        points = apply_world_rotation_to_points(
            points,
            world_rotation,
        )

        print(
            "[AUTOLENS WORLD] Auto-upright applied. "
            "Average camera up is now +Y."
        )
    else:
        print(
            "[AUTOLENS WORLD] Auto-upright disabled; "
            "preserving raw COLMAP world orientation."
        )

    observations = parse_image_observations(
        best_txt / "images.txt",
        cam["width"],
        cam["height"],
    )

    point_errors = np.array([p["error"] for p in points], dtype=np.float64)
    mean_point_error = float(np.mean(point_errors))
    median_point_error = float(np.median(point_errors))

    print("\nSolved camera:")
    print(json.dumps(cam, indent=2))
    print(f"Registered images: {reg_count} / {len(images)}")
    print(f"Camera poses parsed: {len(poses)}")
    print(f"Sparse 3D points: {len(points)}")
    print(f"Mean sparse reprojection error: {mean_point_error:.4f} px")

    # Save sparse cache as soon as the important solve exists.
    cache_payload = {
        "version": "0.10.30",
        "image_signature": current_signature,
        "sparse_root": str(sparse),
        "matching_method": matcher_name,
        "sfm_method": solve_method,
        "best_model": str(best_model),
        "mask_signature": current_mask_signature,
    }
    cache_path.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")

    show_progress(
        6,
        "Generating native Nuke lens and camera outputs",
        "Fitting COLMAP RADIAL to LensDistortion2; no STMap render is required."
    )

    stage6_started = time.perf_counter()

    print("\n[AUTOLENS LENS] 1/3 Fitting native LensDistortion2 model")
    lens_model = fit_nuke_lens_model(nuke_cam)

    print(
        "[AUTOLENS LENS] Fit quality: "
        f"RMS {lens_model['fit']['rms_error_px']:.4f}px, "
        f"max {lens_model['fit']['max_abs_error_px']:.4f}px"
    )
    print(
        "[AUTOLENS LENS] Numerator: "
        f"{lens_model['numerator'][0]:.12g}, "
        f"{lens_model['numerator'][1]:.12g}"
    )
    print(
        "[AUTOLENS LENS] Denominator: "
        f"{lens_model['denominator'][0]:.12g}, "
        f"{lens_model['denominator'][1]:.12g}"
    )

    lens_model_json = output / "autolens_lens_model.json"
    lens_model_json.write_text(
        json.dumps(lens_model, indent=2),
        encoding="utf-8",
    )
    print(f"[AUTOLENS LENS] Wrote {lens_model_json.name}")

    print("\n[AUTOLENS LENS] 2/3 Building animated camera data")
    projection = camera_projection_values(
        nuke_cam,
        args.nuke_aperture_mm,
    )

    camera_track = {
        "version": "0.10.30",
        "coordinate_notes": {
            "colmap_camera_axes": "+X right, +Y down, +Z forward",
            "nuke_camera_axes": "+X right, +Y up, -Z forward",
            "world_space": "COLMAP world coordinates are preserved",
            "matrix": (
                "Nuke local-to-world matrix, row-major, "
                "translation in final column"
            ),
        },
        "solve_camera": solve_cam,
        "nuke_camera": nuke_cam,
        "source_format": source_format,
        "projection": projection,
        "frames": poses,
    }

    camera_track_json = output / "autolens_camera_track.json"
    camera_track_json.write_text(
        json.dumps(camera_track, indent=2),
        encoding="utf-8",
    )
    print(f"[AUTOLENS LENS] Wrote {camera_track_json.name}")

    legacy_stmaps = None

    if args.write_stmaps:
        print(
            "\n[AUTOLENS LENS] Optional legacy STMap export requested."
        )

        und_path = output / "autolens_undistort.exr"
        red_path = output / "autolens_redistort.exr"

        und = generate_stmap(cam, "undistort")
        write_exr_rgb(und_path, und)
        del und

        red = generate_stmap(cam, "redistort")
        write_exr_rgb(red_path, red)
        del red

        legacy_stmaps = {
            "undistort": str(und_path),
            "redistort": str(red_path),
        }

    print("[AUTOLENS LENS] 3/3 Native lens + camera outputs complete")
    print(
        f"[AUTOLENS TIMING] lens_camera_outputs: "
        f"{format_duration(time.perf_counter() - stage6_started)}"
    )

    dense_info = None
    proxy_obj = None
    meshfit_planes_obj = None
    meshfit_surfaces = []
    meshfit_diagnostics = None
    guided_surfaces_obj = None
    guided_surfaces_report = None
    guided_surfaces_metadata = None
    propagated_depth = None

    if not args.skip_dense:
        dense_info = run_dense_reconstruction(
            colmap=colmap,
            images_dir=images_dir,
            best_model=best_model,
            output=output,
            poses=poses,
            observations=observations,
            settings=dense_settings,
            force_dense=args.force_dense,
        )

        selection_report_path = output / "autolens_dense_selection.json"
        selection_report_path.write_text(
            json.dumps(dense_info["selection_report"], indent=2),
            encoding="utf-8",
        )

        show_progress(
            11,
            "Building projection geometry",
            "Creating proxy OBJ and connected mesh-fit planar proxies."
        )

        proxy_vertices, proxy_faces = read_ply_mesh(
            dense_info["proxy_ply"]
        )

        proxy_vertices = apply_world_rotation_to_vertices(
            proxy_vertices,
            world_rotation,
        )

        proxy_obj = output / "autolens_proxy_mesh.obj"
        write_mesh_obj(proxy_obj, proxy_vertices, proxy_faces)

        meshfit_surfaces, meshfit_diagnostics = extract_meshfit_surfaces(
            proxy_vertices,
            proxy_faces,
            poses,
            angle_degrees=args.meshfit_angle,
            distance_ratio=args.meshfit_distance_ratio,
            min_faces=args.meshfit_min_faces,
            min_area_fraction=args.meshfit_min_area_fraction,
            max_surfaces=args.meshfit_max_surfaces,
            padding=args.meshfit_padding,
        )

        if meshfit_surfaces:
            meshfit_planes_obj = output / "autolens_meshfit_planes.obj"
            write_meshfit_planes_obj(meshfit_planes_obj, meshfit_surfaces)

        if args.surface_guides:
            guide_path = Path(args.surface_guides).resolve()
            if guide_path.exists():
                print(
                    "\n[AUTOLENS] Fitting user-drawn 2D surface guides "
                    "into the solved 3D scene..."
                )
                try:
                    (
                        guided_surfaces_obj,
                        guided_surfaces_report,
                        guided_surfaces_metadata,
                    ) = build_guided_surfaces(
                        guide_path,
                        cam,
                        poses,
                        points,
                        best_txt / "images.txt",
                        proxy_vertices,
                        proxy_faces,
                        output,
                    )

                    if guided_surfaces_metadata is not None:
                        try:
                            guided_surfaces_metadata["guide_sha256"] = file_sha256(
                                guide_path
                            )
                        except Exception:
                            pass

                    print(
                        f"  Guided surfaces:   {guided_surfaces_obj}"
                    )
                    if guided_surfaces_metadata is not None:
                        print(
                            "  Guided count:      "
                            f"{guided_surfaces_metadata.get('successful_surface_count', 0)}"
                        )

                except Exception as exc:
                    print(
                        "\n[AUTOLENS WARNING] Surface-guide fitting "
                        f"was skipped: {exc}"
                    )
                    print(
                        "[AUTOLENS WARNING] The camera, lens, proxy mesh "
                        "and mesh-fit planes are still valid."
                    )

                    guided_surfaces_obj = None
                    guided_surfaces_report = None
                    guided_surfaces_metadata = {
                        "status": "skipped",
                        "error": str(exc),
                        "successful_surface_count": 0,
                    }

        print("\nGeometry summary:")
        print(f"  Proxy mesh:       {proxy_obj}")
        print(f"  Mesh-fit planes:  {meshfit_planes_obj}")
        print(f"  Plane patches:    {len(meshfit_surfaces)}")

        propagated_depth = None

    result = {
        "version": "0.10.30",
        "solver": "COLMAP shared-intrinsics + optimized dense MVS + connected mesh-fit planes",
        "matching_method": matcher_name,
        "sfm_method": solve_method,
        "camera": solve_cam,
        "nuke_camera": nuke_cam,
        "source_format": source_format,
        "world_orientation": {
            "mode": (
                "auto_upright"
                if not args.no_upright_world
                else "raw_colmap"
            ),
            "rotation_3x3": world_rotation.tolist(),
        },
        "tracking_mask_path": (
            str(Path(args.mask_path).resolve())
            if args.mask_path
            else None
        ),
        "registered_images": reg_count,
        "total_images": len(images),
        "camera_pose_count": len(poses),
        "sparse_point_count_internal": len(points),
        "mean_sparse_reprojection_error_px": mean_point_error,
        "median_sparse_reprojection_error_px": median_point_error,
        "nuke_projection": projection,
        "best_model_path": str(best_model),
        "lens_distortion": {
            "native_nuke_model": lens_model,
            "model_json": str(lens_model_json),
            "legacy_stmaps": legacy_stmaps,
        },
        "camera_track": str(camera_track_json),
        "geometry_outputs": {
            "proxy_mesh_obj": str(proxy_obj) if proxy_obj else None,
            "meshfit_planes_obj": str(meshfit_planes_obj) if meshfit_planes_obj else None,
            "guided_surfaces_obj": str(guided_surfaces_obj) if guided_surfaces_obj else None,
        },
        "guided_surfaces": guided_surfaces_metadata,
        "meshfit_surface_detection": {
            "diagnostics": meshfit_diagnostics,
            "surfaces": meshfit_surfaces,
        },
        "dense": None if dense_info is None else {
            "workspace": str(dense_info["workspace"]),
            "quality": dense_settings["quality"],
            "settings": dense_settings,
            "selected_dense_images": dense_info["selected_dense_images"],
            "selected_dense_image_count": len(dense_info["selected_dense_images"]),
            "selection_report": dense_info["selection_report"],
            "selection_report_path": str(output / "autolens_dense_selection.json"),
            "mesh_method": dense_info["mesh_method"],
            "proxy_cleanup": dense_info.get("proxy_cleanup"),
            "proxy_cleanup_report": (
                str(dense_info.get("proxy_cleanup_report"))
                if dense_info.get("proxy_cleanup_report")
                else None
            ),
            "raw_proxy_ply": (
                str(dense_info.get("raw_proxy_ply"))
                if dense_info.get("raw_proxy_ply")
                else None
            ),
            "clean_proxy_ply": str(dense_info["proxy_ply"]),
            "resumed": dense_info["resumed"],
        },
    }

    result_json = output / "autolens_result.json"
    result_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    nk_path = output / "AutoLens_v08.nk"
    write_nuke_script(
        nk_path,
        images,
        lens_model,
        nuke_cam,
        result_json,
        poses,
        camera_track_json,
        args.nuke_aperture_mm,
        proxy_obj=proxy_obj,
        meshfit_planes_obj=meshfit_planes_obj,
        meshfit_surfaces=meshfit_surfaces,
        guided_surfaces_obj=guided_surfaces_obj,
        guided_surface_count=(guided_surfaces_metadata or {}).get("successful_surface_count", 0),
        source_format=source_format,
    )

    total_elapsed = time.perf_counter() - total_started

    print("\n============================================================")
    print("AUTOLENS V0.10.30 COMPLETE")
    print("============================================================")
    print(f"Registered:       {reg_count}/{len(images)} images")
    print(f"Matcher:          {matcher_name}")
    print(f"SfM method:       {solve_method}")
    print(f"Model:            {cam['model']}")
    print(f"Mean point error: {mean_point_error:.4f} px")
    if dense_info is not None:
        selection_report = dense_info["selection_report"]
        print(f"Quality:          {dense_settings['quality']}")
        print(f"Selection:        {dense_settings['selection']}")
        print(f"Shot type:        {selection_report.get('shot', {}).get('classification', 'UNKNOWN')}")
        print(f"Dense refs:       {len(dense_info['selected_dense_images'])}/{len(poses)}")
        if selection_report.get("coverage") is not None:
            print(f"Scene coverage:   {selection_report['coverage'] * 100.0:.2f}%")
        print(f"Dense resolution: {dense_settings['max_image_size']} px")
        print(f"Dense GPUs:       {dense_settings['gpu_index']}")
        print(f"Patch cache:      {dense_settings['patchmatch_cache_gb']} GB")
        print(f"Fusion cache:     {dense_settings['fusion_cache_gb']} GB")
    print(f"Proxy OBJ:        {proxy_obj}")
    print(f"Mesh-fit planes:  {meshfit_planes_obj}")
    print(f"Plane patches:    {len(meshfit_surfaces)}")
    print(f"Guided surfaces:  {guided_surfaces_obj}")
    print(f"Nuke script:      {nk_path}")
    print(f"JSON:             {result_json}")
    print(f"TOTAL TIME:       {format_duration(total_elapsed)}")
    print("============================================================")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        print("\nAUTOLENS FAILED:")
        print(exc)
        sys.exit(1)
