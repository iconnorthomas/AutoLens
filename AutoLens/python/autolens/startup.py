"""
autolens.startup: the cheap code that runs at Nuke launch and script open.

Nothing here imports `nuke` at module level, so menu.py can register AutoLens
without loading the heavy controller module (autolens.node), and the pure
helpers can be unit tested without Nuke.

Profiling: set AUTOLENS_PROFILE=1 before launching Nuke. Each timed step is
printed as "[AutoLens profile] <step>: <ms> ms" and kept in TIMINGS, so in the
Script Editor you can run:

    import autolens.startup; autolens.startup.report()
"""
import os
import sys
import time
from contextlib import contextmanager

from . import __version__

PROFILE_ENV = "AUTOLENS_PROFILE"

# Set to 1 to restore the old behaviour of rescanning every AutoLens
# node's output folder (versions, results, guided surfaces) on script open.
FULL_REFRESH_ENV = "AUTOLENS_FULL_REFRESH_ON_LOAD"

# Set to 0 to load the workspace's plate preview before its window appears.
DEFER_PREVIEW_ENV = "AUTOLENS_DEFER_PREVIEW"

# Hidden knob holding the plugin version that last upgraded a controller node.
VERSION_KNOB = "autolens_plugin_version"

# Knobs ensure_runtime_knobs() adds. A node missing any of them is upgraded
# again even if its version knob says it is current.
RUNTIME_KNOBS = (
    "autolens_output_root",
    "autolens_version",
    "autolens_mask_invert",
    "autolens_result_status",
    "autolens_import_result",
    "autolens_create_sep",
    "autolens_create_item",
    "autolens_create_selected",
    "autolens_result_nk",
    "autolens_guides_source",
    "autolens_mask_node",
)

# Knobs that only ever existed on old controllers and are removed on upgrade.
LEGACY_KNOBS = (
    "autolens_guides",
    "autolens_auto_surfaces",
    "autolens_set_guide_frame",
)

TIMINGS = []


def _env_flag(name, environ=None):
    value = (environ if environ is not None else os.environ).get(name, "")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def profiling_enabled(environ=None):
    return _env_flag(PROFILE_ENV, environ)


def full_refresh_on_load(environ=None):
    return _env_flag(FULL_REFRESH_ENV, environ)


def defer_initial_preview(environ=None):
    """Load the workspace's plate preview after the window shows (default on)."""
    value = (environ if environ is not None else os.environ).get(DEFER_PREVIEW_ENV, "")
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def record(label, seconds):
    """Keep a timing and print it when AUTOLENS_PROFILE is on."""
    ms = float(seconds) * 1000.0
    TIMINGS.append((str(label), ms))
    if profiling_enabled():
        try:
            sys.stdout.write(f"[AutoLens profile] {label}: {ms:.1f} ms\n")
            sys.stdout.flush()
        except Exception:
            pass
    return ms


@contextmanager
def timed(label):
    """Time a block. Costs one perf_counter() pair when profiling is off."""
    start = time.perf_counter()
    try:
        yield
    finally:
        if profiling_enabled():
            record(label, time.perf_counter() - start)


def report():
    """Print every timing recorded this session (for the Script Editor)."""
    if not TIMINGS:
        print(
            "[AutoLens profile] nothing recorded. Set AUTOLENS_PROFILE=1 "
            "before launching Nuke."
        )
        return []
    for label, ms in TIMINGS:
        print(f"[AutoLens profile] {label}: {ms:.1f} ms")
    return list(TIMINGS)


def _knob(node, name):
    try:
        return node.knob(name)
    except Exception:
        return None


def is_controller(node):
    """True for an AutoLens controller Group (same test the plugin always used)."""
    return bool(_knob(node, "autolens_output") and _knob(node, "autolens_open"))


def stored_version(node):
    knob = _knob(node, VERSION_KNOB)
    if knob is None:
        return ""
    try:
        return str(knob.value() or "").strip()
    except Exception:
        return ""


def needs_upgrade(node, current=__version__):
    """
    Does this controller need ensure_runtime_knobs()' structural upgrade?

    A node is current when it was last upgraded by this exact plugin version,
    has every runtime knob, and has none of the legacy ones. Any other version
    (older or newer) runs the upgrade again; it is idempotent.
    """
    if stored_version(node) != str(current):
        return True
    for name in RUNTIME_KNOBS:
        if _knob(node, name) is None:
            return True
    for name in LEGACY_KNOBS:
        if _knob(node, name) is not None:
            return True
    return False


def controllers_needing_upgrade(nodes, current=__version__):
    found = []
    for node in nodes or ():
        try:
            if is_controller(node) and needs_upgrade(node, current):
                found.append(node)
        except Exception:
            pass
    return found


def upgrade_on_script_load():
    """
    nuke.addOnScriptLoad callback registered by menu.py.

    Only imports autolens.node when the script holds an AutoLens controller
    that an older plugin saved. Output-folder scans (solve versions, results,
    guided surfaces) wait until the node's properties panel or the workspace
    opens, where they already run.
    """
    with timed("script load: AutoLens upgrade check"):
        import nuke

        try:
            groups = nuke.allNodes("Group")
        except Exception:
            return

        full = full_refresh_on_load()
        if full:
            pending = [node for node in groups if is_controller(node)]
        else:
            pending = controllers_needing_upgrade(groups)

        if not pending:
            return

        from . import node as autolens_node

        autolens_node.upgrade_nodes(pending, refresh=full)
