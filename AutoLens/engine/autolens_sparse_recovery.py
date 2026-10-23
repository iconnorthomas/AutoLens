"""
AutoLens sparse-solve recovery for hard shots.

Low-texture plates, motion blur, small parallax, heavy masks and short
sequences often make COLMAP's incremental mapper fail outright or register
only part of the shot. This module adds a staged retry ladder that runs after
the normal incremental mapper and before the engine's global-SfM fallback:

    relaxed_init      mapper again, relaxed initial-pair / registration limits
    low_parallax      mapper again, tuned for nodal-ish and push-in shots
    merge_partials    merge overlapping partial models from the runs above
    dense_features    denser covariant SIFT + guided matching in a new database
    contrast_plate    the same on contrast-normalised tracking copies of the plate
    learned_features  ALIKED + LightGlue (only with --sparse-recovery max)

Each rung only runs while the best solve so far registers less than
--min-registered-fraction of the frames, and a rung's result is only used when
it registers strictly more frames than everything before it (relaxed rungs must
also keep a sane reprojection error). A shot that already solves well pays only
the cost of reading its model back.

Tracking copies are used for feature extraction only; the mapper, dense stages
and all outputs keep reading the original plate (the file names are identical).

The decision logic is plain Python so tests can run it without COLMAP or Nuke;
numpy/OpenEXR are imported lazily for the plate preprocessing.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path


RECOVERY_MODES = ("off", "auto", "max")
DEFAULT_RECOVERY_MODE = "auto"
DEFAULT_MIN_REGISTERED_FRACTION = 0.8

# Relaxed rungs can register frames with wrong poses. Reject a relaxed result
# whose mean sparse reprojection error is above this (px). The baseline solve
# is never rejected on this basis.
MAX_RELAXED_MEAN_REPROJ_ERROR_PX = 2.0

# model_merger needs shared registered frames to align two models.
MIN_COMMON_IMAGES_FOR_MERGE = 3

TRACKING_PLATE_DIR = "autolens_tracking_plate"
RECOVERY_SPARSE_DIR = "sparse_recovery"

LOG_PREFIX = "[AUTOLENS RECOVERY]"


# ---------------------------------------------------------------------------
# Option sets. Each entry is ((candidate option names...), value); the first
# candidate the installed COLMAP lists in its -h output is used, and options
# it doesn't know are dropped (logged) instead of failing the command.
# ---------------------------------------------------------------------------

MAPPER_OPTIONS = {
    # Defaults: init_min_num_inliers 100, abs_pose_min_num_inliers 30,
    # abs_pose_min_inlier_ratio 0.25, min_num_matches 15, init_min_tri_angle 16.
    "relaxed_init": [
        (("Mapper.init_min_num_inliers",), "50"),
        (("Mapper.abs_pose_min_num_inliers",), "15"),
        (("Mapper.abs_pose_min_inlier_ratio",), "0.15"),
        (("Mapper.min_num_matches",), "10"),
        (("Mapper.init_min_tri_angle",), "6"),
        (("Mapper.max_reg_trials",), "5"),
    ],
    # Small baselines: accept a thin initial triangulation angle and allow
    # mostly-forward motion for the initial pair.
    "low_parallax": [
        (("Mapper.init_min_num_inliers",), "30"),
        (("Mapper.abs_pose_min_num_inliers",), "12"),
        (("Mapper.abs_pose_min_inlier_ratio",), "0.1"),
        (("Mapper.min_num_matches",), "8"),
        (("Mapper.init_min_tri_angle",), "2"),
        (("Mapper.init_max_forward_motion",), "0.99"),
        (("Mapper.tri_min_angle",), "0.75"),
        (("Mapper.filter_min_tri_angle",), "0.75"),
        (("Mapper.max_reg_trials",), "8"),
    ],
}


def dense_feature_extraction_options(gpu_matching: bool) -> list:
    """Denser, more blur/viewpoint-tolerant SIFT. Affine shape and domain-size
    pooling run on COLMAP's CPU covariant extractor, so GPU is turned off."""
    # Brute-force matching cost grows with features squared; keep CPU matching
    # affordable.
    max_features = "16384" if gpu_matching else "10000"
    return [
        (("SiftExtraction.max_num_features", "FeatureExtraction.max_num_features"), max_features),
        (("SiftExtraction.peak_threshold",), "0.004"),
        (("SiftExtraction.edge_threshold",), "16"),
        (("SiftExtraction.estimate_affine_shape",), "1"),
        (("SiftExtraction.domain_size_pooling",), "1"),
        (("FeatureExtraction.use_gpu", "SiftExtraction.use_gpu"), "0"),
    ]


DENSE_FEATURE_MATCHING_OPTIONS = [
    (("FeatureMatching.guided_matching", "SiftMatching.guided_matching"), "1"),
    (("FeatureMatching.max_num_matches", "SiftMatching.max_num_matches"), "32768"),
    (("SiftMatching.max_ratio",), "0.85"),
]

LEARNED_FEATURE_EXTRACTION_OPTIONS = [
    (("FeatureExtraction.type",), "ALIKED_N16ROT"),
]

LEARNED_FEATURE_MATCHING_OPTIONS = [
    (("FeatureMatching.type",), "ALIKED_LIGHTGLUE"),
]

RUNG_REASONS = {
    "relaxed_init": "relaxed initial-pair and registration thresholds (same features)",
    "low_parallax": "low-parallax thresholds: thin triangulation angles, forward motion allowed",
    "merge_partials": "merged overlapping partial models into one",
    "dense_features": "denser covariant SIFT with guided matching",
    "contrast_plate": "dense features on contrast-normalised (CLAHE) tracking copies",
    "learned_features": "ALIKED features with LightGlue matching",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_arguments(parser) -> None:
    parser.add_argument(
        "--sparse-recovery",
        choices=list(RECOVERY_MODES),
        default=DEFAULT_RECOVERY_MODE,
        help=(
            "Retry ladder when SfM registers too few frames. auto (default) "
            "tries relaxed mapper settings, model merging, denser features and "
            "contrast-normalised tracking copies; max also tries learned "
            "ALIKED+LightGlue features; off keeps the plain solve."
        ),
    )
    parser.add_argument(
        "--min-registered-fraction",
        type=float,
        default=DEFAULT_MIN_REGISTERED_FRACTION,
        help=(
            "Run the recovery ladder while fewer than this fraction of frames "
            "are registered (default 0.8)."
        ),
    )


def work_paths(output: Path) -> list:
    """Everything recovery writes, for --force / mask-change cache clearing."""
    output = Path(output)
    paths = [
        output / RECOVERY_SPARSE_DIR,
        output / TRACKING_PLATE_DIR,
    ]
    for rung in ("dense_features", "contrast_plate", "learned_features"):
        paths.append(output / recovery_database_name(rung))
    return paths


def recovery_database_name(rung: str) -> str:
    return f"database_{rung}.db"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_OPTION_RE = re.compile(r"--([A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_.]+)")


def parse_help_options(text: str) -> set:
    """Option names (without --) that a COLMAP subcommand's -h output lists."""
    return set(_OPTION_RE.findall(text or ""))


def resolve_options(spec, supported) -> tuple:
    """
    Pick the option name COLMAP supports for each spec entry.

    supported=None means the -h probe failed: use each first candidate and let
    a failing command count as a failed rung. Returns (pairs, dropped) where
    pairs is [(name, value)] and dropped lists the options with no supported
    candidate.
    """
    pairs = []
    dropped = []
    for candidates, value in spec:
        if supported is None:
            pairs.append((candidates[0], str(value)))
            continue
        chosen = next((c for c in candidates if c in supported), None)
        if chosen is None:
            dropped.append(candidates[0])
        else:
            pairs.append((chosen, str(value)))
    return pairs, dropped


def with_options(cmd, pairs) -> list:
    """Copy a COLMAP command, replacing or appending ``--name value`` pairs."""
    out = [str(x) for x in cmd]
    for name, value in pairs:
        flag = "--" + str(name)
        if flag in out:
            index = out.index(flag)
            if index + 1 < len(out):
                out[index + 1] = str(value)
            else:
                out.append(str(value))
        else:
            out += [flag, str(value)]
    return out


def option_value(cmd, name: str):
    flag = "--" + name
    cmd = [str(x) for x in cmd]
    if flag in cmd:
        index = cmd.index(flag)
        if index + 1 < len(cmd):
            return cmd[index + 1]
    return None


def needs_recovery(registered: int, total: int, min_fraction: float) -> bool:
    if total <= 0:
        return False
    if registered < 3:
        return True
    return (float(registered) / float(total)) < float(min_fraction)


def plan_ladder(mode: str, can_preprocess: bool) -> list:
    """Rung order, cheapest first. Mapper-only rungs reuse the database."""
    if mode == "off":
        return []
    rungs = ["relaxed_init", "low_parallax", "merge_partials", "dense_features"]
    if can_preprocess:
        rungs.append("contrast_plate")
    if mode == "max":
        rungs.append("learned_features")
    return rungs


def attempt_is_acceptable(attempt: dict, max_relaxed_error: float) -> bool:
    if int(attempt.get("registered") or 0) <= 0:
        return False
    if attempt.get("rung") == "baseline":
        return True
    error = attempt.get("mean_error_px")
    if error is None:
        return True
    return float(error) <= float(max_relaxed_error)


def choose_best_attempt(attempts, max_relaxed_error=MAX_RELAXED_MEAN_REPROJ_ERROR_PX):
    """Most registered frames wins; ties go to the earlier (less relaxed) one."""
    best = None
    for attempt in attempts:
        if not attempt_is_acceptable(attempt, max_relaxed_error):
            continue
        if best is None or int(attempt["registered"]) > int(best["registered"]):
            best = attempt
    return best


def choose_mapper_rung(attempts) -> str:
    """Mapper settings for the feature rungs: the mapper rung that did best."""
    best_name = "relaxed_init"
    best_count = -1
    for attempt in attempts:
        rung = attempt.get("rung")
        if rung in MAPPER_OPTIONS and attempt_is_acceptable(attempt, MAX_RELAXED_MEAN_REPROJ_ERROR_PX):
            if int(attempt["registered"]) > best_count:
                best_name = rung
                best_count = int(attempt["registered"])
    return best_name


def plan_merge(models, min_common: int = MIN_COMMON_IMAGES_FOR_MERGE) -> list:
    """
    Greedy merge order for partial models built from the same database.

    models: [{"key": ..., "names": iterable of registered image names}].
    Starts from the largest model and repeatedly adds the model that shares at
    least ``min_common`` frames with the merged set and contributes the most
    new frames. Returns [start_key, key, key, ...], or [] when nothing can be
    merged.
    """
    pool = [
        {"key": m["key"], "names": set(m["names"])}
        for m in models
        if m.get("names")
    ]
    if len(pool) < 2:
        return []

    pool.sort(key=lambda m: len(m["names"]), reverse=True)
    start = pool.pop(0)
    merged = set(start["names"])
    order = [start["key"]]

    while pool:
        best_index = None
        best_gain = 0
        for index, model in enumerate(pool):
            common = len(model["names"] & merged)
            gain = len(model["names"] - merged)
            if common >= min_common and gain > best_gain:
                best_index = index
                best_gain = gain
        if best_index is None:
            break
        model = pool.pop(best_index)
        merged |= model["names"]
        order.append(model["key"])

    return order if len(order) > 1 else []


_FRAME_RE = re.compile(r"(\d+)(?!.*\d)")


def frame_number(name: str, fallback: int) -> int:
    stem = Path(str(name)).stem
    match = _FRAME_RE.search(stem)
    if match:
        return int(match.group(1))
    return int(fallback)


def frame_ranges(numbers) -> list:
    """[1,2,3,7,9,10] -> [[1,3],[7,7],[9,10]]"""
    ranges = []
    for number in sorted(set(int(n) for n in numbers)):
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def coverage_stats(all_names, registered_names) -> dict:
    """Which source frames the chosen sparse model registered."""
    all_names = [str(n) for n in all_names]
    registered = set(str(n) for n in registered_names)
    numbers = {name: frame_number(name, index + 1) for index, name in enumerate(all_names)}

    missing = [name for name in all_names if name not in registered]
    registered_count = sum(1 for name in all_names if name in registered)
    total = len(all_names)
    missing_ranges = frame_ranges(numbers[name] for name in missing)
    registered_ranges = frame_ranges(
        numbers[name] for name in all_names if name in registered
    )

    # Longest run of consecutive unregistered frames, in sequence order.
    longest_gap = 0
    run = 0
    for name in all_names:
        if name in registered:
            run = 0
        else:
            run += 1
            longest_gap = max(longest_gap, run)

    return {
        "total_frames": total,
        "registered_frames": registered_count,
        "registered_fraction": (registered_count / total) if total else 0.0,
        "missing_frame_count": len(missing),
        "missing_frames": missing,
        "missing_frame_ranges": missing_ranges,
        "registered_frame_ranges": registered_ranges,
        "longest_unregistered_run": longest_gap,
    }


def format_ranges(ranges, limit: int = 12) -> str:
    parts = [str(a) if a == b else f"{a}-{b}" for a, b in ranges[:limit]]
    if len(ranges) > limit:
        parts.append(f"... (+{len(ranges) - limit} more)")
    return ", ".join(parts) if parts else "none"


def coverage_log_lines(stats: dict) -> list:
    lines = [
        f"[AUTOLENS COVERAGE] Registered {stats['registered_frames']}/"
        f"{stats['total_frames']} frames "
        f"({stats['registered_fraction'] * 100.0:.1f}%)."
    ]
    if stats["missing_frame_count"]:
        lines.append(
            "[AUTOLENS COVERAGE] Unregistered frames: "
            + format_ranges(stats["missing_frame_ranges"])
        )
        lines.append(
            "[AUTOLENS COVERAGE] Longest unregistered run: "
            f"{stats['longest_unregistered_run']} frame(s)."
        )
    return lines


# ---------------------------------------------------------------------------
# COLMAP model reading (text format)
# ---------------------------------------------------------------------------

def read_registered_names(images_txt: Path) -> list:
    """Image names in a COLMAP images.txt (header line + points line each)."""
    images_txt = Path(images_txt)
    if not images_txt.exists():
        return []
    names = []
    with images_txt.open("r", encoding="utf-8", errors="replace") as f:
        rows = [ln.rstrip("\r\n") for ln in f if not ln.startswith("#")]
    for header in rows[0::2]:
        parts = header.strip().split(None, 9)
        if len(parts) == 10:
            names.append(parts[9])
    return names


def read_mean_point_error(points_txt: Path):
    points_txt = Path(points_txt)
    if not points_txt.exists():
        return None
    total = 0.0
    count = 0
    with points_txt.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 8:
                continue
            try:
                value = float(parts[7])
            except ValueError:
                continue
            if math.isfinite(value):
                total += value
                count += 1
    return (total / count) if count else None


def _model_files_exist(path: Path) -> bool:
    if not path.is_dir():
        return False
    return all(
        (path / f"{stem}.bin").exists() or (path / f"{stem}.txt").exists()
        for stem in ("cameras", "images", "points3D")
    )


# ---------------------------------------------------------------------------
# Plate preprocessing (tracking copies only)
# ---------------------------------------------------------------------------

def clahe(gray, tiles: int = 8, clip_limit: float = 3.0, bins: int = 256):
    """
    Contrast-limited adaptive histogram equalisation in numpy.

    gray: float array in 0..1 (H, W). Returns float32 in 0..1.
    """
    import numpy as np

    gray = np.clip(np.asarray(gray, dtype=np.float32), 0.0, 1.0)
    h, w = gray.shape
    ty = max(1, min(int(tiles), h))
    tx = max(1, min(int(tiles), w))
    q = np.minimum((gray * (bins - 1) + 0.5).astype(np.int32), bins - 1)

    ys = np.linspace(0, h, ty + 1).astype(int)
    xs = np.linspace(0, w, tx + 1).astype(int)
    luts = np.zeros((ty, tx, bins), dtype=np.float32)

    for iy in range(ty):
        for ix in range(tx):
            tile = q[ys[iy]:ys[iy + 1], xs[ix]:xs[ix + 1]]
            n = tile.size
            if n == 0:
                luts[iy, ix] = np.linspace(0.0, 1.0, bins)
                continue
            hist = np.bincount(tile.ravel(), minlength=bins).astype(np.float64)
            limit = max(1.0, clip_limit * n / bins)
            excess = np.maximum(hist - limit, 0.0).sum()
            hist = np.minimum(hist, limit) + excess / bins
            cdf = np.cumsum(hist)
            cdf = (cdf - cdf[0]) / max(cdf[-1] - cdf[0], 1e-12)
            luts[iy, ix] = cdf.astype(np.float32)

    # Bilinear blend of the four nearest tile mappings.
    tile_h = h / float(ty)
    tile_w = w / float(tx)
    fy = np.clip((np.arange(h) + 0.5) / tile_h - 0.5, 0.0, ty - 1)
    fx = np.clip((np.arange(w) + 0.5) / tile_w - 0.5, 0.0, tx - 1)
    y0 = np.floor(fy).astype(int)
    x0 = np.floor(fx).astype(int)
    y1 = np.minimum(y0 + 1, ty - 1)
    x1 = np.minimum(x0 + 1, tx - 1)
    wy = (fy - y0).astype(np.float32)[:, None]
    wx = (fx - x0).astype(np.float32)[None, :]

    out = np.empty((h, w), dtype=np.float32)
    # Row chunks bound the temporary memory on 4K+ plates.
    step = 256
    for r0 in range(0, h, step):
        r1 = min(h, r0 + step)
        qq = q[r0:r1]
        a = y0[r0:r1, None]
        b = y1[r0:r1, None]
        top = (1.0 - wx) * luts[a, x0[None, :], qq] + wx * luts[a, x1[None, :], qq]
        bottom = (1.0 - wx) * luts[b, x0[None, :], qq] + wx * luts[b, x1[None, :], qq]
        out[r0:r1] = (1.0 - wy[r0:r1]) * top + wy[r0:r1] * bottom
    return np.clip(out, 0.0, 1.0)


def tone_map_linear(luma):
    """Scene-linear EXR luminance -> display-ish 0..1 (robust white point + gamma)."""
    import numpy as np

    luma = np.asarray(luma, dtype=np.float32)
    finite = luma[np.isfinite(luma)]
    white = float(np.percentile(finite, 99.5)) if finite.size else 1.0
    if not math.isfinite(white) or white <= 1e-6:
        white = 1.0
    v = np.clip(np.nan_to_num(luma, nan=0.0, posinf=white, neginf=0.0) / white, 0.0, 1.0)
    return np.power(v, 1.0 / 2.2).astype(np.float32)


def tracking_image(gray01):
    """The tracking copy: CLAHE on a 0..1 grey image."""
    return clahe(gray01)


def _image_libraries():
    libs = {}
    try:
        import OpenEXR  # noqa: F401
        libs["exr"] = True
    except Exception:
        pass
    try:
        from PIL import Image  # noqa: F401
        libs["pil"] = True
    except Exception:
        pass
    try:
        import cv2  # noqa: F401
        libs["cv2"] = True
    except Exception:
        pass
    return libs


def can_preprocess(images) -> tuple:
    """(ok, reason). Tracking copies need a reader/writer for every plate format."""
    libs = _image_libraries()
    exts = sorted({Path(p).suffix.lower() for p in images})
    for ext in exts:
        if ext == ".exr":
            if not libs.get("exr"):
                return False, "OpenEXR is not importable"
        elif not (libs.get("pil") or libs.get("cv2")):
            return False, (
                f"no reader for {ext} plates in the engine runtime "
                "(install Pillow or opencv-python to enable)"
            )
    return True, ""


def _read_exr_luma(path: Path):
    import numpy as np
    import OpenEXR

    channels = None
    if hasattr(OpenEXR, "File"):
        try:
            f = OpenEXR.File(str(path), separate_channels=True)
            channels = {k: np.asarray(v.pixels, dtype=np.float32) for k, v in f.channels().items()}
        except Exception:
            channels = None
    if channels is None:
        import Imath

        exr = OpenEXR.InputFile(str(path))
        try:
            header = exr.header()
            dw = header["dataWindow"]
            w = dw.max.x - dw.min.x + 1
            h = dw.max.y - dw.min.y + 1
            pt = Imath.PixelType(Imath.PixelType.FLOAT)
            channels = {}
            for name in header["channels"]:
                raw = exr.channel(name, pt)
                channels[name] = np.frombuffer(raw, dtype=np.float32).reshape(h, w)
        finally:
            exr.close()

    def find(*names):
        for name in names:
            for key, value in channels.items():
                if key == name or key.endswith("." + name):
                    return value
        return None

    r, g, b = find("R", "r"), find("G", "g"), find("B", "b")
    if r is not None and g is not None and b is not None:
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    y = find("Y", "y")
    if y is not None:
        return y
    first = next(iter(channels.values()))
    return first


def _write_exr_grey(path: Path, gray):
    import numpy as np
    import OpenEXR

    gray = np.ascontiguousarray(gray, dtype=np.float32)
    if hasattr(OpenEXR, "File"):
        try:
            OpenEXR.File(
                {"compression": OpenEXR.ZIP_COMPRESSION},
                {"R": gray, "G": gray, "B": gray},
            ).write(str(path))
            return
        except Exception:
            pass
    import Imath

    h, w = gray.shape
    header = OpenEXR.Header(int(w), int(h))
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    header["compression"] = Imath.Compression(Imath.Compression.ZIP_COMPRESSION)
    header["channels"] = {c: Imath.Channel(pt) for c in ("R", "G", "B")}
    exr = OpenEXR.OutputFile(str(path), header)
    try:
        data = gray.tobytes()
        exr.writePixels({"R": data, "G": data, "B": data})
    finally:
        exr.close()


def _read_display_grey(path: Path):
    """8/16-bit plate -> 0..1 grey via Pillow or OpenCV."""
    import numpy as np

    try:
        from PIL import Image

        with Image.open(str(path)) as im:
            if im.mode in ("I;16", "I;16B", "I;16L", "I"):
                arr = np.asarray(im, dtype=np.float32)
                return np.clip(arr / 65535.0, 0.0, 1.0)
            arr = np.asarray(im.convert("L"), dtype=np.float32)
            return arr / 255.0
    except ImportError:
        pass
    import cv2

    arr = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_ANYDEPTH)
    if arr is None:
        raise RuntimeError(f"Could not read {path}")
    scale = 65535.0 if arr.dtype == np.uint16 else 255.0
    return arr.astype(np.float32) / scale


def _write_display_grey(path: Path, gray):
    import numpy as np

    data = (np.clip(gray, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    try:
        from PIL import Image

        kwargs = {"quality": 95} if path.suffix.lower() in (".jpg", ".jpeg") else {}
        Image.fromarray(data, mode="L").save(str(path), **kwargs)
        return
    except ImportError:
        pass
    import cv2

    if not cv2.imwrite(str(path), data):
        raise RuntimeError(f"Could not write {path}")


PREPROCESS_VERSION = 1


def write_tracking_plate(images, images_dir: Path, out_dir: Path, log=print):
    """
    Contrast-normalised copies of the plate with identical relative names, so
    COLMAP image names, masks and the image list still line up. Reused while
    the source files are unchanged. Returns out_dir or None.
    """
    images_dir = Path(images_dir)
    out_dir = Path(out_dir)
    signature = {
        "version": PREPROCESS_VERSION,
        "files": [
            [Path(p).name, Path(p).stat().st_size, int(Path(p).stat().st_mtime)]
            for p in images
        ],
    }
    manifest = out_dir / "autolens_tracking_plate.json"
    if manifest.exists():
        try:
            if json.loads(manifest.read_text(encoding="utf-8")) == signature:
                log(f"{LOG_PREFIX} Reusing contrast-normalised tracking copies in {out_dir}")
                return out_dir
        except Exception:
            pass

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    for index, image in enumerate(images):
        image = Path(image)
        try:
            relative = image.resolve().relative_to(images_dir.resolve())
        except Exception:
            relative = Path(image.name)
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if image.suffix.lower() == ".exr":
            grey = tone_map_linear(_read_exr_luma(image))
            _write_exr_grey(target, tracking_image(grey))
        else:
            _write_display_grey(target, tracking_image(_read_display_grey(image)))
        if index == 0 or (index + 1) % 25 == 0 or index + 1 == len(images):
            log(f"{LOG_PREFIX} Tracking copies: {index + 1}/{len(images)}")

    manifest.write_text(json.dumps(signature), encoding="utf-8")
    log(
        f"{LOG_PREFIX} Wrote {len(images)} tracking copies in "
        f"{time.perf_counter() - started:.1f}s (feature extraction only)."
    )
    return out_dir


# ---------------------------------------------------------------------------
# Ladder runner
# ---------------------------------------------------------------------------

class RecoveryLadder:
    """
    Runs the retry ladder. COLMAP access is injected so the engine keeps
    control of how commands are launched:

        run_try(cmd, label=...) -> bool
        prepare_command(cmd) -> (argv, cwd, env)   for capturing -h output
    """

    def __init__(
        self,
        colmap,
        run_try,
        prepare_command,
        output: Path,
        images_dir: Path,
        images,
        feat_cmd,
        match_cmd,
        mapper_cmd,
        mode: str = DEFAULT_RECOVERY_MODE,
        min_fraction: float = DEFAULT_MIN_REGISTERED_FRACTION,
        log=print,
    ):
        self.colmap = colmap
        self.run_try = run_try
        self.prepare_command = prepare_command
        self.output = Path(output)
        self.images_dir = Path(images_dir)
        self.images = [Path(p) for p in images]
        self.feat_cmd = list(feat_cmd)
        self.match_cmd = list(match_cmd)
        self.mapper_cmd = list(mapper_cmd)
        self.mode = mode
        self.min_fraction = float(min_fraction)
        self.log = log
        self._help = {}

    # -- COLMAP plumbing ----------------------------------------------------

    def supported_options(self, subcommand: str):
        if subcommand in self._help:
            return self._help[subcommand]
        supported = None
        try:
            argv, cwd, env = self.prepare_command([self.colmap, subcommand, "-h"])
            proc = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True,
                errors="replace", timeout=120,
            )
            found = parse_help_options((proc.stdout or "") + "\n" + (proc.stderr or ""))
            supported = found or None
        except Exception as exc:
            self.log(f"{LOG_PREFIX} Could not read `{subcommand} -h` ({exc}); using default option names.")
        self._help[subcommand] = supported
        return supported

    def _options(self, subcommand: str, spec):
        pairs, dropped = resolve_options(spec, self.supported_options(subcommand))
        if dropped:
            self.log(
                f"{LOG_PREFIX} This COLMAP has no {', '.join(dropped)} for "
                f"{subcommand}; skipping those settings."
            )
        return pairs

    def evaluate_root(self, root: Path) -> list:
        """[{dir, count, names, mean_error_px}] for each model under root."""
        models = []
        root = Path(root)
        if not root.is_dir():
            return models
        for model_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not _model_files_exist(model_dir):
                continue
            txt_dir = model_dir / "txt"
            txt_dir.mkdir(exist_ok=True)
            images_txt = txt_dir / "images.txt"
            points_txt = txt_dir / "points3D.txt"
            if not (images_txt.exists() and points_txt.exists() and (txt_dir / "cameras.txt").exists()):
                self.run_try([
                    self.colmap, "model_converter",
                    "--input_path", str(model_dir),
                    "--output_path", str(txt_dir),
                    "--output_type", "TXT",
                ], label="model_converter")
            names = read_registered_names(images_txt)
            models.append({
                "dir": model_dir,
                "count": len(names),
                "names": names,
                "mean_error_px": read_mean_point_error(points_txt),
            })
        return models

    def _attempt(self, rung, database, root, ok, started, note=""):
        models = self.evaluate_root(root) if ok else []
        best = max(models, key=lambda m: m["count"]) if models else None
        return {
            "rung": rung,
            "database": Path(database),
            "sparse_root": Path(root),
            "models": models,
            "registered": best["count"] if best else 0,
            "mean_error_px": best["mean_error_px"] if best else None,
            "model_count": len(models),
            "seconds": round(time.perf_counter() - started, 1),
            "status": "ok" if best else "failed",
            "note": note,
        }

    def _fresh_dir(self, path: Path) -> Path:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_mapper(self, rung_label, mapper_rung, database: Path):
        root = self._fresh_dir(self.output / RECOVERY_SPARSE_DIR / rung_label)
        pairs = [
            ("database_path", str(database)),
            ("image_path", str(self.images_dir)),
            ("output_path", str(root)),
        ] + self._options("mapper", MAPPER_OPTIONS[mapper_rung])
        ok = self.run_try(with_options(self.mapper_cmd, pairs), label=f"recovery_mapper_{rung_label}")
        return root, ok

    # -- rungs --------------------------------------------------------------

    def rung_mapper(self, rung, database):
        started = time.perf_counter()
        root, ok = self._run_mapper(rung, rung, database)
        return self._attempt(rung, database, root, ok, started)

    def rung_merge(self, attempts, database):
        started = time.perf_counter()
        candidates = []
        lookup = {}
        for attempt in attempts:
            if Path(attempt["database"]) != Path(database):
                continue
            if not attempt_is_acceptable(attempt, MAX_RELAXED_MEAN_REPROJ_ERROR_PX):
                continue
            for model in attempt["models"]:
                key = str(model["dir"])
                lookup[key] = model
                candidates.append({"key": key, "names": model["names"]})

        order = plan_merge(candidates)
        root = self.output / RECOVERY_SPARSE_DIR / "merge_partials"
        if not order:
            return None

        self._fresh_dir(root)
        current = Path(order[0])
        merged_any = False
        for step, key in enumerate(order[1:], start=1):
            out = root / f"step_{step}"
            out.mkdir(parents=True, exist_ok=True)
            ok = self.run_try([
                self.colmap, "model_merger",
                "--input_path1", str(current),
                "--input_path2", key,
                "--output_path", str(out),
            ], label=f"recovery_model_merger_{step}")
            if not ok or not _model_files_exist(out):
                shutil.rmtree(out, ignore_errors=True)
                continue
            self.run_try([
                self.colmap, "bundle_adjuster",
                "--input_path", str(out),
                "--output_path", str(out),
            ], label=f"recovery_bundle_adjuster_{step}")
            current = out
            merged_any = True

        if not merged_any:
            shutil.rmtree(root, ignore_errors=True)
            return self._attempt("merge_partials", database, root, False, started,
                                 note="model_merger found no alignable overlap")
        return self._attempt("merge_partials", database, root, True, started,
                             note=f"merged {len(order)} partial models")

    def rung_features(self, rung, attempts):
        started = time.perf_counter()
        database = self.output / recovery_database_name(rung)
        if database.exists():
            database.unlink()

        image_path = self.images_dir
        gpu_matching = option_value(self.match_cmd, "FeatureMatching.use_gpu") == "1"
        if rung == "learned_features":
            extract_spec = LEARNED_FEATURE_EXTRACTION_OPTIONS
            match_spec = LEARNED_FEATURE_MATCHING_OPTIONS
        else:
            extract_spec = dense_feature_extraction_options(gpu_matching)
            match_spec = DENSE_FEATURE_MATCHING_OPTIONS

        if rung == "contrast_plate":
            try:
                image_path = write_tracking_plate(
                    self.images, self.images_dir,
                    self.output / TRACKING_PLATE_DIR, log=self.log,
                )
            except Exception as exc:
                return self._attempt(rung, database, self.output, False, started,
                                     note=f"tracking copies failed: {exc}")

        feat = with_options(
            self.feat_cmd,
            [("database_path", str(database)), ("image_path", str(image_path))]
            + self._options("feature_extractor", extract_spec),
        )
        if not self.run_try(feat, label=f"recovery_feature_extractor_{rung}"):
            return self._attempt(rung, database, self.output, False, started,
                                 note="feature extraction failed")

        matcher = str(self.match_cmd[1]) if len(self.match_cmd) > 1 else "exhaustive_matcher"
        match = with_options(
            self.match_cmd,
            [("database_path", str(database))] + self._options(matcher, match_spec),
        )
        if not self.run_try(match, label=f"recovery_{matcher}_{rung}"):
            return self._attempt(rung, database, self.output, False, started,
                                 note="matching failed")

        mapper_rung = choose_mapper_rung(attempts)
        root, ok = self._run_mapper(rung, mapper_rung, database)
        return self._attempt(rung, database, root, ok, started,
                             note=f"mapper settings: {mapper_rung}")

    # -- driver -------------------------------------------------------------

    def run(self, database: Path, sparse_root: Path, mapper_ok: bool) -> dict:
        """
        Returns {"sparse", "database", "solve_method", "report"}; sparse is
        None when the baseline solve should be kept (or nothing worked).
        """
        total = len(self.images)
        started = time.perf_counter()
        baseline = self._attempt("baseline", database, sparse_root, mapper_ok, started)
        attempts = [baseline]
        report = {
            "mode": self.mode,
            "target_fraction": self.min_fraction,
            "baseline_registered": baseline["registered"],
            "total_frames": total,
            "triggered": False,
            "attempts": [],
            "selected_rung": "baseline",
            "reason": "",
        }

        if self.mode == "off":
            report["reason"] = "recovery disabled (--sparse-recovery off)"
            report["attempts"] = [_attempt_summary(baseline, total)]
            return {"sparse": None, "database": None, "solve_method": None, "report": report}

        if not needs_recovery(baseline["registered"], total, self.min_fraction):
            report["reason"] = "baseline solve met the registration target"
            report["attempts"] = [_attempt_summary(baseline, total)]
            return {"sparse": None, "database": None, "solve_method": None, "report": report}

        report["triggered"] = True
        preprocess_ok, preprocess_reason = can_preprocess(self.images)
        rungs = plan_ladder(self.mode, preprocess_ok)
        self.log("\n" + "=" * 72)
        self.log(
            f"{LOG_PREFIX} Baseline registered {baseline['registered']}/{total} frames "
            f"(target {self.min_fraction * 100.0:.0f}%). Trying: {', '.join(rungs)}"
        )
        if not preprocess_ok:
            self.log(f"{LOG_PREFIX} Contrast-normalised tracking copies unavailable: {preprocess_reason}")
            report["preprocess_unavailable"] = preprocess_reason
        self.log("=" * 72)

        for rung in rungs:
            self.log(f"\n{LOG_PREFIX} Rung {rung}: {RUNG_REASONS[rung]}")
            try:
                if rung in MAPPER_OPTIONS:
                    attempt = self.rung_mapper(rung, database)
                elif rung == "merge_partials":
                    attempt = self.rung_merge(attempts, database)
                    if attempt is None:
                        self.log(f"{LOG_PREFIX} Rung merge_partials: no overlapping partial models; skipped.")
                        continue
                else:
                    attempt = self.rung_features(rung, attempts)
            except Exception as exc:
                self.log(f"{LOG_PREFIX} Rung {rung} raised {exc}; continuing.")
                continue

            attempts.append(attempt)
            acceptable = attempt_is_acceptable(attempt, MAX_RELAXED_MEAN_REPROJ_ERROR_PX)
            error = attempt["mean_error_px"]
            self.log(
                f"{LOG_PREFIX} Rung {rung}: {attempt['status']}, "
                f"{attempt['registered']}/{total} registered"
                + (f", mean error {error:.3f}px" if error is not None else "")
                + (f" ({attempt['note']})" if attempt["note"] else "")
                + ("" if acceptable or not attempt["registered"] else
                   f"; rejected, error above {MAX_RELAXED_MEAN_REPROJ_ERROR_PX}px")
                + f" in {attempt['seconds']}s"
            )
            best = choose_best_attempt(attempts)
            if best is not None and not needs_recovery(best["registered"], total, self.min_fraction):
                self.log(f"{LOG_PREFIX} Target reached by {best['rung']}; stopping the ladder.")
                break

        best = choose_best_attempt(attempts)
        report["attempts"] = [_attempt_summary(a, total) for a in attempts]
        report["seconds"] = round(time.perf_counter() - started, 1)

        if best is None:
            report["selected_rung"] = None
            report["reason"] = "no rung produced a usable model"
            self.log(f"{LOG_PREFIX} No rung produced a usable model.")
            return {"sparse": None, "database": None, "solve_method": None, "report": report}

        report["selected_rung"] = best["rung"]
        if best["rung"] == "baseline":
            report["reason"] = "no rung registered more frames than the baseline"
            self.log(f"{LOG_PREFIX} Keeping the baseline solve: no rung registered more frames.")
            return {"sparse": None, "database": None, "solve_method": None, "report": report}

        report["reason"] = (
            f"{best['rung']} registered {best['registered']}/{total} frames vs "
            f"{baseline['registered']} for the baseline ({RUNG_REASONS[best['rung']]})"
        )
        self.log(f"{LOG_PREFIX} SELECTED {best['rung']}: {report['reason']}")
        return {
            "sparse": best["sparse_root"],
            "database": best["database"],
            "solve_method": f"incremental_recovery:{best['rung']}",
            "report": report,
        }


def _attempt_summary(attempt: dict, total: int) -> dict:
    return {
        "rung": attempt["rung"],
        "status": attempt["status"],
        "registered": attempt["registered"],
        "registered_fraction": (attempt["registered"] / total) if total else 0.0,
        "model_count": attempt["model_count"],
        "mean_error_px": attempt["mean_error_px"],
        "seconds": attempt["seconds"],
        "note": attempt["note"],
        "sparse_root": str(attempt["sparse_root"]),
    }
