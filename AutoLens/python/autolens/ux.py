"""
autolens.ux: plain-language helpers for the Solve panel and controller node.

Pure Python (no nuke, no Qt), so the quality presets, step indicator, progress
/ETA text and failure explanations can be unit tested outside Nuke.
"""
from __future__ import annotations

import re
import time


# ---------------------------------------------------------------------------
# Quality presets
# ---------------------------------------------------------------------------

QUALITY_ORDER = ("Preview", "Normal", "High")
DEFAULT_QUALITY = "Normal"

# Engine settings behind each preset. The Advanced section shows the same
# values and lets the artist override any of them.
QUALITY_PRESETS = {
    "Preview": {"quality": "preview", "resolution": 1800, "target": 8, "minimum": 5, "coverage": 0.90, "sources": 6},
    "Normal":  {"quality": "normal",  "resolution": 3200, "target": 12, "minimum": 6, "coverage": 0.97, "sources": 8},
    "High":    {"quality": "high",    "resolution": 4000, "target": 18, "minimum": 8, "coverage": 0.99, "sources": 12},
}

_PRESET_PURPOSE = {
    "Preview": "Fastest. Good for checking that a shot tracks before a full solve.",
    "Normal": "Recommended for most shots.",
    "High": "Slowest. Densest geometry for close-up projection work.",
}


def preset_name(label):
    """Return a known preset name for `label`, falling back to Normal."""
    text = str(label or "").strip()
    for name in QUALITY_ORDER:
        if text.lower() == name.lower():
            return name
    return DEFAULT_QUALITY


def preset_settings(label):
    """Engine settings for a preset (a copy, so callers can't edit the table)."""
    return dict(QUALITY_PRESETS[preset_name(label)])


def preset_summary(label):
    """One-line description of a preset for the panel and tooltips."""
    name = preset_name(label)
    p = QUALITY_PRESETS[name]
    return (
        f"{_PRESET_PURPOSE[name]} Up to {p['target']} dense reference frames at "
        f"{p['resolution']} px, {p['coverage'] * 100:.0f}% scene coverage."
    )


def settings_match_preset(label, resolution, target, minimum, coverage, sources, selection="smart"):
    """True when the Advanced values are still exactly the preset's values."""
    p = QUALITY_PRESETS[preset_name(label)]
    return (
        int(resolution) == p["resolution"]
        and int(target) == p["target"]
        and int(minimum) == p["minimum"]
        and abs(float(coverage) - p["coverage"]) < 1e-6
        and int(sources) == p["sources"]
        and str(selection) == "smart"
    )


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

RUN_MODES = (
    "Resume / reuse cached stages",
    "Rebuild dense only",
    "Rebuild everything",
    # Camera-first: solve and deliver the camera and lens, plus guides fitted
    # to sparse points, without dense geometry. A later Resume run reuses the
    # cached solve and only builds the geometry.
    "Camera only (geometry later)",
)


def run_mode_args(index):
    """Engine flags for a Run mode combo index (0 = resume)."""
    index = int(index)
    if index == 1:
        return ["--force-dense"]
    if index == 2:
        return ["--force"]
    if index == 3:
        return ["--skip-dense"]
    return []


# ---------------------------------------------------------------------------
# Step indicator: Setup -> Solve -> Review -> Create
# ---------------------------------------------------------------------------

STEPS = ("Setup", "Solve", "Review", "Create")
STEP_SETUP, STEP_SOLVE, STEP_REVIEW, STEP_CREATE = range(4)

_STEP_HINTS = (
    "Connect a plate Read to AutoLens input 0 and finish the AutoLens install.",
    "Pick a quality and click Solve. Surfaces and a tracking mask are optional.",
    "Solve complete. Check the log, then click Create All to build the camera, lens and geometry.",
    "Result created in the comp. Re-run Solve or Create All whenever you change surfaces.",
)


def current_step(has_source, installed, running=False, has_result=False, imported=False):
    """Index into STEPS for the panel's current state."""
    if running:
        return STEP_SOLVE
    if has_result:
        return STEP_CREATE if imported else STEP_REVIEW
    if has_source and installed:
        return STEP_SOLVE
    return STEP_SETUP


def step_hint(step):
    return _STEP_HINTS[max(0, min(len(STEPS) - 1, int(step)))]


def step_indicator_html(step, active="#63d471", done="#9a9a9a", todo="#5a5a5a"):
    """`Setup › Solve › Review › Create` with the current step highlighted."""
    step = max(0, min(len(STEPS) - 1, int(step)))
    parts = []
    for index, name in enumerate(STEPS):
        label = f"{index + 1}. {name}"
        if index == step:
            parts.append(f'<span style="color:{active}; font-weight:700;">{label}</span>')
        elif index < step:
            parts.append(f'<span style="color:{done};">&#10003; {label}</span>')
        else:
            parts.append(f'<span style="color:{todo};">{label}</span>')
    return " &nbsp;&#8250;&nbsp; ".join(parts)


# ---------------------------------------------------------------------------
# Progress and ETA
# ---------------------------------------------------------------------------

# Matches the engine's show_progress() banner:
#   AUTOLENS STATUS  [######----]  8/11  (73%)
_STATUS_RE = re.compile(r"AUTOLENS STATUS\s+\[[^\]]+\]\s+(\d+)/(\d+)\s+\((\d+)%\)")


def parse_status_line(line):
    """(step, total, percent) from an engine status banner line, else None."""
    match = _STATUS_RE.search(str(line or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_duration(seconds):
    """Short human duration: 45s, 3m 05s, 1h 12m."""
    seconds = max(0, int(round(float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_eta(seconds):
    """Rounded, deliberately vague remaining-time text."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "less than a minute left"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"about {minutes} min left"
    hours = seconds / 3600.0
    return f"about {hours:.1f} h left"


class ProgressEstimator:
    """
    Turns the engine's coarse stage percentages into elapsed / ETA text.

    The engine reports 11 stages of very different length (dense depth maps
    dominate), so the ETA is a rough linear estimate. It stays hidden until
    there is enough evidence: at least `min_percent` done and `min_seconds`
    elapsed.
    """

    def __init__(self, clock=None, min_percent=10, min_seconds=20.0):
        self._clock = clock or time.monotonic
        self.min_percent = int(min_percent)
        self.min_seconds = float(min_seconds)
        self.reset()

    def reset(self):
        self.started_at = None
        self.percent = 0
        self.stage = ""
        self.step = 0
        self.total = 0

    def start(self):
        self.reset()
        self.started_at = self._clock()

    def update(self, percent, stage="", step=0, total=0):
        if self.started_at is None:
            self.started_at = self._clock()
        self.percent = max(0, min(100, int(percent)))
        if stage:
            self.stage = str(stage)
        self.step = int(step or 0)
        self.total = int(total or 0)

    def elapsed(self):
        if self.started_at is None:
            return 0.0
        return max(0.0, self._clock() - self.started_at)

    def remaining(self):
        """Estimated seconds left, or None while there isn't enough evidence."""
        elapsed = self.elapsed()
        if self.percent < self.min_percent or self.percent >= 100 or elapsed < self.min_seconds:
            return None
        return elapsed * (100.0 - self.percent) / float(self.percent)

    def text(self):
        """e.g. `Step 8 of 11: Computing dense depth maps · 12m 03s elapsed · about 6 min left`."""
        stage = self.stage or "Starting"
        if self.step and self.total:
            stage = f"Step {self.step} of {self.total}: {stage}"
        parts = [stage]
        if self.started_at is not None:
            parts.append(format_duration(self.elapsed()) + " elapsed")
        remaining = self.remaining()
        if remaining is not None:
            parts.append(format_eta(remaining))
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Failure explanations
# ---------------------------------------------------------------------------

# (pattern, title, what it means, what to do next). First match wins, so the
# more specific patterns come first. Patterns are matched case-insensitively
# against the tail of the solve log.
_FAILURE_RULES = (
    (
        r"\[AUTOLENS\] Failed to start",
        "The AutoLens engine could not start",
        "Nuke could not launch AutoLens' Python runtime.",
        "Run install_autolens.bat again (outside Nuke), then click Refresh Installation Status.",
    ),
    (
        r"requires the Python packages OpenEXR|No module named '?(numpy|OpenEXR|Imath)",
        "AutoLens' Python packages are missing",
        "The engine's Python environment is missing NumPy or OpenEXR.",
        "Run install_autolens.bat again (outside Nuke) to repair the environment.",
    ),
    (
        r"COLMAP was not found|Could not find COLMAP launcher|Managed COLMAP environment is missing|"
        r"Managed Conda executable is missing|Could not read AutoLens COLMAP launcher",
        "COLMAP is not installed correctly",
        "AutoLens could not find its private COLMAP install.",
        "Run install_autolens.bat again (outside Nuke), then click Refresh Installation Status.",
    ),
    (
        r"0xC0000142",
        "COLMAP failed to start (Windows DLL error)",
        "Windows could not initialise COLMAP's libraries (0xC0000142).",
        "Restart Nuke and try again. If it keeps happening, re-run install_autolens.bat.",
    ),
    (
        r"out of memory|cudaErrorMemoryAllocation|CUDA_ERROR_OUT_OF_MEMORY|std::bad_alloc|MemoryError",
        "Ran out of GPU or system memory",
        "The solve needed more memory than this machine had free.",
        "Close other GPU-heavy apps, then try the Preview or Normal quality (or lower "
        "Dense max dimension under Advanced). Run mode Resume keeps the stages already done.",
    ),
    (
        r"No space left on device|Errno 28",
        "The output disk is full",
        "AutoLens could not write to the output folder because the disk is full.",
        "Free some space or pick another Output root, then Solve again.",
    ),
    (
        r"Permission denied|Errno 13|Access is denied",
        "AutoLens can't write to the output folder",
        "The output folder is read-only or locked by another program.",
        "Choose a writable Output root, or close whatever has the folder open.",
    ),
    (
        r"Images folder does not exist",
        "The plate folder can't be found",
        "The image folder AutoLens was given does not exist.",
        "Check the Read node's file path, then click Use Selected Read and Solve again.",
    ),
    (
        r"Need at least 3 images",
        "Not enough frames",
        "AutoLens needs at least 3 image files in the plate folder.",
        "Point the Read at an image sequence (JPG, PNG, TIFF or EXR) with 3 or more frames.",
    ),
    (
        r"Tracking mask folder does not exist",
        "The tracking mask could not be exported",
        "The matte connected to AutoLens' TrackingMask input did not produce mask images.",
        "Check the mask input renders an alpha, or disconnect it, then Solve again.",
    ),
    (
        r"Input image sequence has changed since the cached solve",
        "The plate changed since the last solve",
        "The cached solve in this version folder was made from different frames.",
        "Pick Create New in Solve version (or Run mode Rebuild everything under Advanced), then Solve again.",
    ),
    (
        r"Neither incremental nor global SfM|COLMAP created no sparse reconstruction|"
        r"No valid sparse model found|registered only \d+ images|No registered camera poses|"
        r"No registered images available for dense",
        "The camera could not be solved",
        "COLMAP could not find enough matching detail between frames to work out the camera.",
        "Mask out moving objects (TrackingMask input), try High quality, or make sure the camera actually "
        "moves (a nodal pan or locked-off shot can't be solved this way).",
    ),
    (
        r"Command failed with exit code",
        "A COLMAP step failed",
        "One of COLMAP's processing steps exited with an error.",
        "Scroll the log to the last '>>>' line to see which step failed. Run mode Resume "
        "will restart from that step.",
    ),
)

_GENERIC_RULE = _FAILURE_RULES[-1]

_FAILED_RE = re.compile(r"AUTOLENS FAILED:\s*\n\s*(.+)", re.IGNORECASE)


def _first_rule(haystack, skip_generic=False):
    if not haystack:
        return None
    for rule in _FAILURE_RULES:
        if skip_generic and rule is _GENERIC_RULE:
            continue
        if re.search(rule[0], haystack, re.IGNORECASE):
            return rule
    return None


def last_engine_error(log_text):
    """The message printed after the engine's final `AUTOLENS FAILED:` line, if any."""
    matches = _FAILED_RE.findall(str(log_text or ""))
    return matches[-1].strip() if matches else ""


def explain_failure(log_text, exit_code=1, cancelled=False, tail_chars=20000):
    """
    Translate a failed run into plain language.

    Returns a dict with `title`, `reason`, `next_step` and `detail` (the raw
    engine error line, possibly empty).
    """
    text = str(log_text or "")[-int(tail_chars):]
    detail = last_engine_error(text)

    if cancelled or int(exit_code) == 130 or re.search(r"^Cancelled\.\s*$", text, re.MULTILINE):
        return {
            "title": "Solve cancelled",
            "reason": "The solve was stopped before it finished.",
            "next_step": "Click Solve with Run mode Resume to carry on from the last finished stage.",
            "detail": detail,
        }

    # Prefer the engine's own final error line. When that line is only the
    # generic "Command failed" wrapper, look through the log tail for the
    # specific cause COLMAP printed before it.
    rule = _first_rule(detail)
    if rule is None or rule is _GENERIC_RULE:
        rule = _first_rule(text, skip_generic=True) or rule or _first_rule(text)

    if rule is not None:
        _pattern, title, reason, next_step = rule
        return {
            "title": title,
            "reason": reason,
            "next_step": next_step,
            "detail": detail,
        }

    return {
        "title": "AutoLens did not finish",
        "reason": detail or f"The engine stopped with exit code {int(exit_code)}.",
        "next_step": "Check the log for the last COLMAP message. Run mode Resume will reuse the stages already done.",
        "detail": detail,
    }


def failure_message(info):
    """Multi-line message box text for an explain_failure() result."""
    lines = [info["reason"], "", "What to do: " + info["next_step"]]
    detail = info.get("detail") or ""
    if detail and detail not in info["reason"]:
        lines += ["", "Engine message: " + detail]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workspace: Track -> Draw -> Output
# ---------------------------------------------------------------------------

WORKSPACE_STEPS = (("track", "Track"), ("draw", "Draw"), ("output", "Output"))
WORKSPACE_KEYS = tuple(key for key, _label in WORKSPACE_STEPS)

# Engine flags for camera first, geometry later (the engine's final interface).
# A camera-only track solves the camera and lens, writes the .nk and fits any
# surface guides to sparse points. "Build geometry" then runs only the dense
# stages from that cached solve: never extraction, matching or mapping. The
# engine refuses --geometry-only together with --skip-dense or --force.
CAMERA_ONLY_ARGS = ("--skip-dense",)
BUILD_GEOMETRY_ARGS = ("--geometry-only",)
GEOMETRY_STATUS_FILE = "autolens_geometry_status.json"


def solve_args(build_geometry=True):
    """Extra engine flags for a track, depending on whether geometry is built now."""
    return [] if build_geometry else list(CAMERA_ONLY_ARGS)


def geometry_only_args():
    """Engine flags for Build geometry (no run-mode flags may go with them)."""
    return list(BUILD_GEOMETRY_ARGS)


def step_index(key):
    """Position of a workspace step, falling back to Track."""
    try:
        return WORKSPACE_KEYS.index(str(key))
    except ValueError:
        return 0


def workspace_step_states(current, solved=False, failed=False, surfaces=0, created=False):
    """
    State of each step pill: "current", "done", "warn" or "todo".

    Draw is optional, so it only counts as done once a surface exists.
    """
    current = WORKSPACE_KEYS[step_index(current)]
    done = {
        "track": bool(solved),
        "draw": int(surfaces) > 0,
        "output": bool(created),
    }
    states = {}
    for key in WORKSPACE_KEYS:
        if key == current:
            states[key] = "current"
        elif key == "track" and failed and not solved:
            states[key] = "warn"
        elif done[key]:
            states[key] = "done"
        else:
            states[key] = "todo"
    return states


def next_step_after_solve(surfaces):
    """Where the workspace goes when a track finishes."""
    return "output" if int(surfaces) > 0 else "draw"


def workspace_hint(step, has_plate=True, installed=True, running=False, solved=False,
                   surfaces=0, geometry_ready=False, created=False, geometry="",
                   geometry_detail=""):
    """One line telling the artist what to do next on `step`."""
    step = WORKSPACE_KEYS[step_index(step)]
    surfaces = int(surfaces)
    if not has_plate:
        return "Connect the plate's Read to AutoLens input 0, then reopen AutoLens."
    if not installed:
        return "AutoLens isn't installed yet: close Nuke, run install_autolens.bat, then reopen it."

    if step == "track":
        if running:
            return "Tracking. Keep drawing surfaces while it runs, or continue in the background."
        if solved:
            return "Tracked. Draw surfaces next, or go straight to Output."
        return "Play the plate, mask anything that moves, then click Track."

    if step == "draw":
        if surfaces == 0:
            return "Add a plane (P) and drag its corners onto a flat surface, or Auto Suggest. Optional."
        noun = "surface" if surfaces == 1 else "surfaces"
        tail = " Then go to Output." if solved else " Track to fit them."
        return f"{surfaces} {noun}. Set each one's guide frame (G) on the frame you drew it.{tail}"

    if not solved:
        return "Track the camera first: the camera and lens come from the track."
    if geometry == "building":
        return "Building geometry from the tracked camera. Create All when it finishes."
    if geometry == "failed":
        reason = f" ({geometry_detail})" if geometry_detail else ""
        return f"The geometry build didn't finish{reason}. Build geometry again; the camera is kept."
    if not geometry_ready:
        return "Camera and lens are ready. Build geometry for the mesh and surfaces, or Create All now."
    if created:
        return "Created in the comp. Create All again after editing surfaces."
    return "Create All builds the camera, lens and geometry in the comp."


def solve_chip(running=False, percent=0, solved=False, failed=False, error_px=None, building=False):
    """(text, state) for the viewer's status chip."""
    if running:
        verb = "BUILDING GEOMETRY" if building else "TRACKING"
        return f"{verb} {max(0, min(100, int(percent)))}%", "running"
    if solved:
        if error_px is not None:
            try:
                return f"SOLVED · {float(error_px):.2f} px", "solved"
            except (TypeError, ValueError):
                pass
        return "SOLVED", "solved"
    if failed:
        return "FAILED", "failed"
    return "NOT SOLVED", "none"


GEOMETRY_ITEMS = (
    ("proxy_mesh_obj", "Proxy mesh"),
    ("meshfit_planes_obj", "Mesh-fit planes"),
    ("guided_surfaces_obj", "Guided surfaces"),
)


def load_result(output_folder):
    """The solve's autolens_result.json as a dict, or {} when there isn't one."""
    import json
    from pathlib import Path

    if not output_folder:
        return {}
    path = Path(str(output_folder)) / "autolens_result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def result_error_px(result):
    """Mean sparse reprojection error from a result dict, or None."""
    value = (result or {}).get("mean_sparse_reprojection_error_px")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def geometry_status(result, exists=None):
    """
    Geometry items for the Output step.

    Returns (overall, items): overall is "none" (no solve), "missing" (no proxy
    mesh yet) or "ready"; items is a list of (label, ready) pairs. `exists` is
    a path predicate, injectable for tests.
    """
    if exists is None:
        from os.path import exists as _exists
        exists = _exists
    if not result:
        return "none", [(label, False) for _key, label in GEOMETRY_ITEMS]
    outputs = result.get("geometry_outputs") or {}
    items = []
    for key, label in GEOMETRY_ITEMS:
        path = outputs.get(key)
        items.append((label, bool(path) and bool(exists(path))))
    overall = "ready" if items[0][1] else "missing"
    return overall, items


def load_geometry_status(output_folder):
    """The engine's autolens_geometry_status.json as a dict, or {}."""
    import json
    from pathlib import Path

    if not output_folder:
        return {}
    path = Path(str(output_folder)) / GEOMETRY_STATUS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def geometry_overview(result, status=None, running=False, exists=None):
    """
    Everything the Output step shows about geometry.

    Returns (overall, items, detail). overall is one of:
      "none"      no solve yet
      "missing"   a camera-only solve, geometry not built
      "building"  the engine is building it right now
      "failed"    the last build failed or was interrupted
      "ready"     the proxy mesh exists
    `status` is the engine's geometry status file; `running` says whether this
    workspace's engine process is alive. A file stuck at "building" with no
    process means the build was killed.
    """
    status = status or {}
    state = str(status.get("status") or "")
    overall, items = geometry_status(result, exists=exists)

    # A finished build lists its own outputs; prefer them when they're newer.
    if state == "ready" and result:
        merged = dict(result)
        outputs = dict(result.get("geometry_outputs") or {})
        for key, _label in GEOMETRY_ITEMS:
            if status.get(key):
                outputs[key] = status[key]
        merged["geometry_outputs"] = outputs
        overall, items = geometry_status(merged, exists=exists)

    if not result:
        return "none", items, ""
    if state == "building":
        if not running:
            return "failed", items, "the last build was interrupted"
        step, total = status.get("step"), status.get("total")
        stage = str(status.get("stage") or "")
        if step and total:
            return "building", items, f"Step {int(step)} of {int(total)}: {stage}".rstrip(": ")
        return "building", items, stage
    if state == "failed" and overall != "ready":
        return "failed", items, str(status.get("error") or "")
    return overall, items, ""


_NICE_STEPS = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000)


def timeline_tick_step(frame_span, width_px, min_px=70):
    """Frames between labelled timeline ticks, so labels sit at least `min_px` apart."""
    frame_span = max(1, int(frame_span))
    width_px = max(1.0, float(width_px))
    for step in _NICE_STEPS:
        if step * width_px / frame_span >= min_px:
            return step
    return _NICE_STEPS[-1]


def shot_summary(read_name="", width=0, height=0, first=None, last=None, version=""):
    """Top-bar breadcrumb, e.g. `Plate: Read1 · 1920×1080 · 1001–1087 · v2`."""
    parts = [f"Plate: {read_name}" if read_name else "No plate"]
    if width and height:
        parts.append(f"{int(width)}×{int(height)}")
    if first is not None and last is not None:
        parts.append(f"{int(first)}–{int(last)}")
    if version:
        parts.append(str(version))
    return " · ".join(parts)
