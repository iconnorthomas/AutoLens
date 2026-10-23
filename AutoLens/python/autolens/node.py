from __future__ import annotations

from pathlib import Path
import os
import re
import tempfile
import subprocess
import sys

import nuke

from . import __version__
from .paths import find_result_nk, engine_path
from .startup import (
    VERSION_KNOB,
    LEGACY_KNOBS,
    controllers_needing_upgrade,
    is_controller,
    needs_upgrade,
    timed,
)


def _selected_input():
    nodes = nuke.selectedNodes()
    return nodes[0] if len(nodes) == 1 else None


_PENDING_VERSION_UI_REFRESH = set()


def _run_deferred_version_ui_refresh(node_fullname):
    """
    Execute after the current Nuke knobChanged event has returned.

    Rebuilding Enumeration_Knobs while their own menu-selection event is still
    active can leave Nuke's visible properties panel stale. Doing the rebuild
    on the next Qt event-loop turn updates the menu immediately.
    """
    _PENDING_VERSION_UI_REFRESH.discard(
        str(node_fullname)
    )

    try:
        node = nuke.toNode(
            str(node_fullname)
        )
    except Exception:
        node = None

    if node is None:
        return

    try:
        refresh_version_menu(
            node,
            prefer_latest=False,
        )
        refresh_result_status(node)
        refresh_create_items(
            node,
            force_rebuild=True,
        )
    except Exception:
        pass


def _schedule_version_ui_refresh(node):
    if node is None:
        return

    try:
        node_fullname = node.fullName()
    except Exception:
        return

    key = str(node_fullname)

    if key in _PENDING_VERSION_UI_REFRESH:
        return

    _PENDING_VERSION_UI_REFRESH.add(key)

    try:
        try:
            from PySide6 import QtCore
        except ImportError:
            from PySide2 import QtCore

        QtCore.QTimer.singleShot(
            0,
            lambda name=key:
                _run_deferred_version_ui_refresh(
                    name
                ),
        )
        return
    except Exception:
        pass

    # Fallback for unusual Nuke/Qt environments.
    try:
        nuke.executeInMainThread(
            _run_deferred_version_ui_refresh,
            args=(key,),
        )
    except Exception:
        _run_deferred_version_ui_refresh(
            key
        )


def source_read_for_node(node):
    """Walk input 0 upstream until a Read is found."""
    current = node.input(0) if node is not None and node.inputs() else None
    seen = set()
    while current is not None and current not in seen:
        seen.add(current)
        if current.Class() == "Read":
            return current
        current = current.input(0) if current.inputs() else None
    return None



_VERSION_RE = re.compile(r"^v(\d+)$", re.IGNORECASE)


def _split_output_root_and_version(value):
    if not value:
        return "", ""

    path = Path(str(value))
    match = _VERSION_RE.match(path.name)

    if match:
        return str(path.parent), f"v{int(match.group(1))}"

    return str(path), ""


def discover_solve_versions(root_value):
    if not root_value:
        return []

    root = Path(root_value)

    if not root.is_dir():
        return []

    found = []

    try:
        children = list(root.iterdir())
    except Exception:
        return []

    for child in children:
        if not child.is_dir():
            continue

        match = _VERSION_RE.match(child.name)

        if match:
            found.append(
                (
                    int(match.group(1)),
                    f"v{int(match.group(1))}",
                )
            )

    found.sort(key=lambda item: item[0])
    return [name for _, name in found]


def _latest_solve_version(root_value):
    versions = discover_solve_versions(root_value)
    return versions[-1] if versions else ""


def _next_solve_version(root_value):
    versions = discover_solve_versions(root_value)

    if not versions:
        return "v1"

    numbers = [
        int(_VERSION_RE.match(version).group(1))
        for version in versions
    ]

    return f"v{max(numbers) + 1}"


def _version_from_menu_value(value, root_value=""):
    value = str(value or "").strip()

    match = re.match(
        r"^(?:Latest|Create New)\s+\((v\d+)\)$",
        value,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    if _VERSION_RE.match(value):
        return f"v{int(_VERSION_RE.match(value).group(1))}"

    return _latest_solve_version(root_value) or "v1"


def resolved_output_folder(node):
    if node is None:
        return ""

    root = ""

    if node.knob("autolens_output_root"):
        try:
            root = node["autolens_output_root"].value().strip()
        except Exception:
            root = ""

    if not root:
        try:
            legacy = node["autolens_output"].value().strip()
        except Exception:
            legacy = ""

        root, legacy_version = _split_output_root_and_version(legacy)

        if legacy_version:
            return str(Path(root) / legacy_version)

        return legacy

    version_value = ""

    if node.knob("autolens_version"):
        try:
            version_value = node["autolens_version"].value()
        except Exception:
            version_value = ""

    version = _version_from_menu_value(
        version_value,
        root,
    )

    return str(Path(root) / version)


def refresh_version_menu(node, prefer_latest=False):
    if node is None:
        return

    root_knob = node.knob("autolens_output_root")
    version_knob = node.knob("autolens_version")

    if root_knob is None or version_knob is None:
        return

    try:
        root = root_knob.value().strip()
    except Exception:
        root = ""

    versions = discover_solve_versions(root)
    latest = versions[-1] if versions else ""
    next_version = _next_solve_version(root)

    values = []

    if latest:
        values.append(f"Latest ({latest})")

    values.extend(versions)
    values.append(f"Create New ({next_version})")

    try:
        previous = version_knob.value()
    except Exception:
        previous = ""

    try:
        version_knob.setValues(values)
    except Exception:
        pass

    if prefer_latest or previous not in values:
        target = (
            f"Latest ({latest})"
            if latest
            else f"Create New ({next_version})"
        )
    else:
        target = previous

    try:
        current_value = version_knob.value()
    except Exception:
        current_value = ""

    if current_value != target:
        try:
            version_knob.setValue(target)
        except Exception:
            pass

    resolved = str(
        Path(root)
        / _version_from_menu_value(
            target,
            root,
        )
    ) if root else ""

    legacy = node.knob("autolens_output")

    if legacy is not None:
        try:
            legacy.setValue(resolved)
        except Exception:
            pass


def _default_output_root(node, source_read=None):
    root = Path.home()

    if source_read is not None:
        try:
            filename = nuke.filename(source_read)
            if filename:
                root = Path(filename).parent
        except Exception:
            pass

    return str(root / "AutoLens")


def _default_output(node, source_read=None):
    """
    Default AutoLens output location is always derived from the upstream Read.

    The Nuke script location is intentionally ignored. This keeps a shot's
    AutoLens cache/results beside that shot's source media even when the .nk
    script is saved somewhere else.
    """
    root = Path.home()

    if source_read is not None:
        try:
            filename = nuke.filename(source_read)
            if filename:
                root = Path(filename).parent
        except Exception:
            pass

    # Keep version folders consistent from the very first solve.
    return str(root / "AutoLens" / "v1")



def browse_output_folder(node):
    """Choose the AutoLens root folder; version selection is handled separately."""
    if node is None:
        return

    ensure_runtime_knobs(node)

    try:
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets

        current = ""

        if node.knob("autolens_output_root"):
            current = node["autolens_output_root"].value().strip()

        start = Path(current).expanduser() if current else Path.home()

        while not start.exists() and start.parent != start:
            start = start.parent

        app = QtWidgets.QApplication.instance()
        parent = app.activeWindow() if app else None

        selected = QtWidgets.QFileDialog.getExistingDirectory(
            parent,
            "Choose AutoLens Output Root",
            str(start),
            QtWidgets.QFileDialog.ShowDirsOnly,
        )

        if selected:
            node["autolens_output_root"].setValue(
                str(Path(selected).resolve())
            )
            refresh_version_menu(
                node,
                prefer_latest=True,
            )
            refresh_result_status(node)
            set_status(node, "Output root selected")

    except Exception as exc:
        nuke.message(
            f"Could not open the output folder browser:\n\n{exc}"
        )


def _button(name, label, command, startline=True, tooltip=None):
    k = nuke.PyScript_Knob(name, label)
    k.setCommand(command)
    try:
        if startline:
            k.setFlag(nuke.STARTLINE)
        else:
            k.clearFlag(nuke.STARTLINE)
    except Exception:
        pass
    if tooltip:
        try:
            k.setTooltip(tooltip)
        except Exception:
            pass
    return k


def _set_knob_enabled(knob, enabled: bool):
    if knob is None:
        return

    try:
        knob.setEnabled(bool(enabled))
        return
    except Exception:
        pass

    # Older Nuke builds sometimes prefer the DISABLED flag instead.
    try:
        disabled_flag = getattr(nuke, "DISABLED", None)
        if disabled_flag is not None:
            if enabled:
                knob.clearFlag(disabled_flag)
            else:
                knob.setFlag(disabled_flag)
    except Exception:
        pass


def _add_knobs(node, source_read=None):
    node.addKnob(nuke.Tab_Knob("autolens_tab", "AutoLens"))

    title = nuke.Text_Knob(
        "autolens_title", "",
        "<b>AutoLens v0.10.30</b><br>Lens solve • camera track • proxy geometry • guided surfaces"
    )
    node.addKnob(title)

    status = nuke.Text_Knob("autolens_status", "Status", "Ready")
    node.addKnob(status)

    quality = nuke.Enumeration_Knob(
        "autolens_quality", "Quality", ["Preview", "Normal", "High"]
    )
    quality.setValue("Normal")
    node.addKnob(quality)

    mode = nuke.Enumeration_Knob(
        "autolens_run_mode", "Run mode",
        ["Resume / reuse cached stages", "Rebuild dense only", "Rebuild everything"]
    )
    node.addKnob(mode)

    use_guides = nuke.Boolean_Knob("autolens_use_guides", "Use surface guides")
    use_guides.setValue(True)
    node.addKnob(use_guides)

    auto_import = nuke.Boolean_Knob("autolens_auto_import", "Import result when complete")
    auto_import.setValue(True)
    node.addKnob(auto_import)

    output_root = nuke.String_Knob(
        "autolens_output_root",
        "Output root",
    )
    output_root.setValue(
        _default_output_root(
            node,
            source_read,
        )
    )
    node.addKnob(output_root)

    output_browse = _button(
        "autolens_output_browse",
        "Browse...",
        "import autolens.node as _al; _al.browse_output_folder(nuke.thisNode())",
        startline=False,
        tooltip="Choose the AutoLens output root."
    )
    node.addKnob(output_browse)

    version = nuke.Enumeration_Knob(
        "autolens_version",
        "Solve version",
        ["Create New (v1)"],
    )
    node.addKnob(version)

    # Hidden resolved folder retained for engine compatibility.
    output = nuke.String_Knob(
        "autolens_output",
        "Resolved output",
    )
    output.setVisible(False)
    output.setValue(
        _default_output(
            node,
            source_read,
        )
    )
    node.addKnob(output)

    refresh_version_menu(
        node,
        prefer_latest=True,
    )

    result_status = nuke.Text_Knob(
        "autolens_result_status",
        "",
        "",
    )
    node.addKnob(result_status)

    result_path = nuke.String_Knob(
        "autolens_result_nk",
        "Last result",
    )
    result_path.setValue("")
    node.addKnob(result_path)
    try:
        result_path.setVisible(False)
    except Exception:
        pass

    node.addKnob(nuke.Text_Knob("autolens_sep1", "", ""))

    node.addKnob(_button(
        "autolens_open", "Setup / Run...",
        "import autolens.node as _al; _al.open_dialog(nuke.thisNode())",
        tooltip="Open the AutoLens setup, environment, progress and log window."
    ))

    import_button = _button(
        "autolens_import_result", "Import Result",
        "import autolens.node as _al; _al.import_result_for_node(nuke.thisNode())",
        startline=False,
        tooltip="Import or re-import the completed AutoLens result from the current output folder."
    )
    node.addKnob(import_button)

    node.addKnob(nuke.Text_Knob("autolens_create_sep", "", ""))

    create_item = nuke.Enumeration_Knob(
        "autolens_create_item",
        "Create from solve",
        ["Camera"],
    )
    node.addKnob(create_item)

    create_button = _button(
        "autolens_create_selected",
        "Create",
        "import autolens.node as _al; _al.create_selected_solve_item(nuke.thisNode())",
        startline=False,
        tooltip=(
            "Create only the selected solved asset. Individual guided surfaces "
            "are created as separate ReadGeo nodes."
        ),
    )
    node.addKnob(create_button)

    mask_invert = nuke.Boolean_Knob(
        "autolens_mask_invert",
        "Invert tracking mask",
    )
    mask_invert.setValue(False)
    try:
        mask_invert.setVisible(False)
    except Exception:
        pass
    node.addKnob(mask_invert)

    node.addKnob(nuke.Tab_Knob("autolens_advanced_tab", "AutoLens Advanced"))
    python_k = nuke.String_Knob("autolens_python", "External Python")
    python_k.setValue("python")
    node.addKnob(python_k)
    node.addKnob(nuke.String_Knob("autolens_colmap", "COLMAP executable"))
    node.addKnob(nuke.String_Knob("autolens_guide_node", "Surface guide node"))
    try:
        node["autolens_guide_node"].setVisible(False)
    except Exception:
        pass

    help_k = nuke.Text_Knob(
        "autolens_help", "",
        "Connect AutoLens directly after the plate (or an upstream chain containing the plate Read). "
        "The image passes through unchanged. Open Setup / Run for surface guides and solving, or use Import Result when a completed track already exists."
    )
    node.addKnob(help_k)


def create_autolens_node():
    source = _selected_input()

    node = nuke.nodes.Group(name="AutoLens")
    node["label"].setValue("AutoLens\\nReady")
    node["tile_color"].setValue(0x356B8FFF)

    node.begin()
    inp = nuke.nodes.Input(name="Source")
    try:
        if inp.knob("number"):
            inp["number"].setValue(0)
        inp["label"].setValue("Source")
    except Exception:
        pass

    mask_inp = nuke.nodes.Input(name="TrackingMask")
    try:
        if mask_inp.knob("number"):
            mask_inp["number"].setValue(1)
        mask_inp["label"].setValue("Tracking Mask Alpha")
        mask_inp.setXpos(170)
        mask_inp.setYpos(0)
    except Exception:
        pass

    out = nuke.nodes.Output(name="Output1")
    out.setInput(0, inp)
    node.end()

    if source is not None:
        node.setInput(0, source)
        node.setXpos(source.xpos())
        node.setYpos(source.ypos() + 100)

    read = source if source is not None and source.Class() == "Read" else source_read_for_node(node)
    _add_knobs(node, read)
    ensure_runtime_knobs(node)
    refresh_result_status(node)

    for n in nuke.selectedNodes():
        n.setSelected(False)
    node.setSelected(True)
    nuke.show(node)
    return node


def set_status(node, text, percent=None):
    if node is None:
        return
    try:
        if node.knob("autolens_status"):
            node["autolens_status"].setValue(str(text))
        label = "AutoLens\\n" + str(text)
        if percent is not None and 0 < int(percent) < 100:
            label += f" {int(percent)}%"
        node["label"].setValue(label)
    except Exception:
        pass


def edit_surface_guides(node):
    from .surface_guides import ensure_guide_node
    read = source_read_for_node(node)
    if read is None:
        nuke.message("AutoLens could not find an upstream Read node. Connect AutoLens to the plate first.")
        return
    guide = ensure_guide_node(read, controller=node)
    set_status(node, f"Surface guides: {guide.name()}")


def auto_suggest_surfaces_for_node(node):
    from .surface_guides import auto_suggest_surfaces, shape_count
    read = source_read_for_node(node)
    if read is None:
        nuke.message("AutoLens could not find an upstream Read node. Connect AutoLens to the plate first.")
        return
    try:
        result = auto_suggest_surfaces(read, controller=node)
        created = result.get("created", [])
        room_created = result.get("room_created", [])
        object_created = result.get("object_created", [])
        confidence = float(result.get("confidence", 0.0))

        if created:
            set_status(
                node,
                f"{len(room_created)} room + {len(object_created)} object planes"
            )

            room_text = ", ".join(room_created) if room_created else "none"
            object_text = ", ".join(object_created) if object_created else "none"

            nuke.message(
                f"AutoLens created {len(created)} editable surface guide shape(s) at frame {int(nuke.frame())}.\\n\\n"
                f"Room-layout confidence: {confidence:.2f}\\n"
                f"Room surfaces: {room_text}\\n"
                f"Local object planes: {object_text}\\n\\n"
                "ObjectPlane shapes are general planar-face guesses — for example a fridge face, "
                "counter front/top, cabinet face, tabletop, door, or other bounded flat surface.\\n\\n"
                "Adjust, rename, or delete the Roto shapes before solving."
            )
        else:
            set_status(node, "No reliable auto surfaces")
            nuke.message(
                "AutoLens did not find enough coherent perspective or bounded planar-edge evidence "
                "to suggest useful surfaces on this frame.\\n\\n"
                "Try another frame or draw the surfaces manually."
            )
    except Exception as exc:
        nuke.message(f"Auto Suggest Surfaces failed:\\n\\n{exc}")


def set_surface_guide_frame(node):
    from .surface_guides import (
        set_guide_frame_to_current,
        source_frame_range,
    )
    try:
        requested = int(nuke.frame())
        frame = set_guide_frame_to_current(controller=node)

        if frame != requested:
            source = source_read_for_node(node)
            frame_range = (
                source_frame_range(source)
                if source is not None
                else None
            )
            if frame_range:
                set_status(
                    node,
                    f"Guide frame {requested} -> {frame} "
                    f"(valid {frame_range[0]}-{frame_range[1]})"
                )
            else:
                set_status(node, f"Guide frame {frame}")
        else:
            set_status(node, f"Guide frame {frame}")

    except Exception as exc:
        nuke.message(str(exc))



def _ensure_source_input_label(node):
    """Label the Group's external input connection as Source."""
    if node is None or node.Class() != "Group":
        return

    try:
        node.begin()
        inputs = nuke.allNodes("Input")
        if inputs:
            inp = sorted(
                inputs,
                key=lambda n: int(n["number"].value())
                if n.knob("number")
                else 0,
            )[0]
            try:
                inp.setName("Source")
            except Exception:
                pass
            try:
                if inp.knob("label"):
                    inp["label"].setValue("Source")
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            node.end()
        except Exception:
            pass


def _install_knob_changed_callback(node):
    if node is None or not node.knob("knobChanged"):
        return

    marker = "_al.autolens_knob_changed"
    command = (
        "import nuke\\n"
        "import autolens.node as _al\\n"
        "_al.autolens_knob_changed(nuke.thisNode(), nuke.thisKnob())"
    )

    try:
        existing = node["knobChanged"].value() or ""
        if marker not in existing:
            if existing.strip():
                existing = existing.rstrip() + "\\n"
            node["knobChanged"].setValue(existing + command)
    except Exception:
        pass


def ensure_tracking_mask_input(node):
    """
    Ensure AutoLens exposes a second external Group input for a tracking-mask
    alpha, similar to Nuke CameraTracker's mask input.

    Input 0 = Source plate
    Input 1 = Tracking mask alpha
    """
    if node is None or node.Class() != "Group":
        return None

    mask_input = None

    try:
        node.begin()

        mask_input = nuke.toNode("TrackingMask")

        if mask_input is None:
            for candidate in nuke.allNodes("Input"):
                try:
                    number = int(
                        candidate["number"].value()
                    ) if candidate.knob("number") else 0
                except Exception:
                    number = 0

                if number == 1:
                    mask_input = candidate
                    break

        if mask_input is None:
            mask_input = nuke.nodes.Input(
                name="TrackingMask"
            )

        try:
            if mask_input.knob("number"):
                mask_input["number"].setValue(1)
            mask_input["label"].setValue(
                "Tracking Mask Alpha"
            )
            mask_input.setXpos(170)
            mask_input.setYpos(0)
        except Exception:
            pass

    except Exception:
        mask_input = None

    finally:
        try:
            node.end()
        except Exception:
            pass

    return mask_input


def _mark_upgraded(node):
    """Record the plugin version that upgraded this node (hidden knob)."""
    knob = node.knob(VERSION_KNOB)

    if knob is None:
        knob = nuke.String_Knob(
            VERSION_KNOB,
            "AutoLens plugin version",
        )
        node.addKnob(knob)
        try:
            knob.setVisible(False)
        except Exception:
            pass

    try:
        if knob.value() != __version__:
            knob.setValue(__version__)
    except Exception:
        pass


def ensure_runtime_knobs(node):
    """Upgrade existing AutoLens controller nodes to the current lightweight UI."""
    if node is None:
        return

    # Already upgraded by this plugin version: skip the structural work
    # (Group edits, legacy guide migration) and only keep the solve-version
    # menu in step with the output folder, as the full upgrade does.
    if not needs_upgrade(node):
        refresh_version_menu(
            node,
            prefer_latest=False,
        )
        return

    ensure_tracking_mask_input(node)

    # Remove legacy property-panel actions that no longer belong on the
    # lightweight controller. Import Result is intentionally kept.
    for knob_name in LEGACY_KNOBS:
        try:
            knob = node.knob(knob_name)
            if knob is not None:
                node.removeKnob(knob)
        except Exception:
            pass

    if not node.knob("autolens_output_root"):
        legacy = ""

        try:
            if node.knob("autolens_output"):
                legacy = node["autolens_output"].value().strip()
        except Exception:
            legacy = ""

        root, legacy_version = _split_output_root_and_version(
            legacy
        )

        knob = nuke.String_Knob(
            "autolens_output_root",
            "Output root",
        )
        knob.setValue(root)
        node.addKnob(knob)

    if not node.knob("autolens_version"):
        legacy = ""

        try:
            if node.knob("autolens_output"):
                legacy = node["autolens_output"].value().strip()
        except Exception:
            legacy = ""

        root, legacy_version = _split_output_root_and_version(
            legacy
        )

        knob = nuke.Enumeration_Knob(
            "autolens_version",
            "Solve version",
            ["Create New (v1)"],
        )
        node.addKnob(knob)

        refresh_version_menu(
            node,
            prefer_latest=False,
        )

        if legacy_version:
            try:
                values = list(knob.values())
                if legacy_version in values:
                    knob.setValue(legacy_version)
            except Exception:
                pass

    refresh_version_menu(
        node,
        prefer_latest=False,
    )

    if not node.knob("autolens_mask_invert"):
        knob = nuke.Boolean_Knob(
            "autolens_mask_invert",
            "Invert tracking mask",
        )
        knob.setValue(False)
        try:
            knob.setVisible(False)
        except Exception:
            pass
        node.addKnob(knob)

    if not node.knob("autolens_result_status"):
        knob = nuke.Text_Knob(
            "autolens_result_status",
            "",
            "",
        )
        node.addKnob(knob)

    if not node.knob("autolens_import_result"):
        knob = _button(
            "autolens_import_result",
            "Import Result",
            "import autolens.node as _al; _al.import_result_for_node(nuke.thisNode())",
            startline=False,
            tooltip="Import or re-import the completed AutoLens result from the current output folder."
        )
        node.addKnob(knob)

    if not node.knob("autolens_create_sep"):
        node.addKnob(nuke.Text_Knob("autolens_create_sep", "", ""))

    if not node.knob("autolens_create_item"):
        node.addKnob(
            nuke.Enumeration_Knob(
                "autolens_create_item",
                "Create from solve",
                ["Camera"],
            )
        )

    if not node.knob("autolens_create_selected"):
        node.addKnob(_button(
            "autolens_create_selected",
            "Create",
            "import autolens.node as _al; _al.create_selected_solve_item(nuke.thisNode())",
            startline=False,
            tooltip=(
                "Create only the selected solved asset. Individual guided "
                "surfaces are created as separate ReadGeo nodes."
            ),
        ))

    if not node.knob("autolens_result_nk"):
        knob = nuke.String_Knob(
            "autolens_result_nk",
            "Last result",
        )
        knob.setValue("")
        node.addKnob(knob)
        try:
            knob.setVisible(False)
        except Exception:
            pass

    if not node.knob("autolens_guides_source"):
        knob = nuke.String_Knob(
            "autolens_guides_source",
            "Guide source",
        )
        knob.setValue("")
        node.addKnob(knob)
        try:
            knob.setVisible(False)
        except Exception:
            pass

    if not node.knob("autolens_mask_node"):
        knob = nuke.String_Knob(
            "autolens_mask_node",
            "Tracking mask node",
        )
        knob.setValue("")
        node.addKnob(knob)
        try:
            knob.setVisible(False)
        except Exception:
            pass

    _ensure_source_input_label(node)
    _install_knob_changed_callback(node)

    # v0.10.5: absorb the old visible root-level surface-guide Roto into the
    # AutoLens Group so the user sees a single controller node.
    try:
        from .surface_guides import migrate_legacy_guide_into_controller

        source = source_read_for_node(node)
        migrate_legacy_guide_into_controller(
            node,
            source_read=source,
        )
    except Exception:
        pass

    try:
        _mark_upgraded(node)
    except Exception:
        pass



def restore_result_guides(node, output=None):
    """
    Restore a completed track's guide shapes into this controller when the
    controller does not already have live guide shapes.
    """
    if node is None:
        return None

    if output is None:
        try:
            output = resolved_output_folder(node)
        except Exception:
            output = ""

    if not output:
        return None

    try:
        from .surface_guides import restore_surface_guides_from_output

        source = source_read_for_node(node)
        return restore_surface_guides_from_output(
            output,
            source_read=source,
            controller=node,
            replace_existing=False,
        )
    except Exception:
        # Result discovery must never make the controller unusable.
        return None



def _result_json_for_output(output):
    if not output:
        return None
    path = Path(output) / "autolens_result.json"
    return path if path.exists() else None


def _guided_report_for_output(output):
    if not output:
        return None
    path = Path(output) / "autolens_guided_surfaces.json"
    return path if path.exists() else None


def _discover_guided_surface_assets(output):
    """
    Discover all individually creatable guided surface OBJs.

    Search order:
      1. <output>/guided_surfaces/*.obj
      2. legacy/alternate individual Surface_*.obj files directly under output
      3. obj_file references in autolens_guided_surfaces.json

    The JSON report is metadata only; physical OBJ files are authoritative.
    """
    assets = []

    if not output:
        return assets

    output_path = Path(output)
    report_path = _guided_report_for_output(output)

    friendly_by_path = {}
    friendly_by_stem = {}

    if report_path is not None:
        try:
            payload = json.loads(
                report_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )

            for surface in payload.get("surfaces") or []:
                if surface.get("status") != "ok":
                    continue

                friendly = str(
                    surface.get("name")
                    or surface.get("obj_name")
                    or ""
                ).strip()

                obj_file = surface.get("obj_file")
                obj_name = str(
                    surface.get("obj_name")
                    or ""
                ).strip()

                if friendly and obj_file:
                    try:
                        key = str(
                            Path(obj_file).resolve()
                        ).lower()
                    except Exception:
                        key = str(
                            Path(obj_file)
                        ).lower()

                    friendly_by_path[key] = friendly

                if friendly and obj_name:
                    friendly_by_stem[
                        obj_name.lower()
                    ] = friendly

        except Exception:
            pass

    discovered = []

    individual_dir = (
        output_path
        / "guided_surfaces"
    )

    if individual_dir.is_dir():
        discovered.extend(
            sorted(
                individual_dir.glob("*.obj"),
                key=lambda path: path.name.lower(),
            )
        )

    # Legacy/alternate cache layouts may leave individual guide exports beside
    # the combined OBJ rather than inside guided_surfaces/.
    for pattern in (
        "Surface_*.obj",
        "surface_*.obj",
        "Guide_*.obj",
        "GUIDE_*.obj",
    ):
        discovered.extend(
            sorted(
                output_path.glob(pattern),
                key=lambda path: path.name.lower(),
            )
        )

    # Explicit report references are also valid even when outside both folders.
    if report_path is not None:
        try:
            payload = json.loads(
                report_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )

            for surface in payload.get("surfaces") or []:
                if surface.get("status") != "ok":
                    continue

                obj_file = surface.get("obj_file")

                if obj_file:
                    obj_path = Path(obj_file)

                    if obj_path.exists():
                        discovered.append(
                            obj_path
                        )
        except Exception:
            pass

    seen = set()

    for obj_path in discovered:
        try:
            key = str(
                obj_path.resolve()
            ).lower()
        except Exception:
            key = str(
                obj_path
            ).lower()

        if key in seen:
            continue

        seen.add(key)

        stem = obj_path.stem
        friendly = (
            friendly_by_path.get(key)
            or friendly_by_stem.get(
                stem.lower()
            )
            or stem
        )

        assets.append({
            "label": friendly,
            "path": str(obj_path),
            "stem": stem,
        })

    assets.sort(
        key=lambda asset: (
            str(asset["label"]).lower(),
            str(asset["path"]).lower(),
        )
    )

    return assets


def _create_item_options(output):
    options = [
        "Camera",
        "Undistort",
        "Redistort",
        "Proxy Mesh",
        "Mesh-fit Planes",
        "Guided Surfaces (Combined)",
    ]
    for asset in _discover_guided_surface_assets(output):
        options.append("Guided: " + asset["label"])
    return options


def _refresh_create_item_knob(
    node,
    output,
    enabled,
    force_rebuild=False,
):
    """
    Refresh Create-from-solve choices for the currently selected solve version.

    Nuke can visually cache Enumeration_Knob entries. A solve-version change
    therefore uses force_rebuild=True so v1 and v2 can expose different guided
    surfaces immediately.
    """
    knob = node.knob("autolens_create_item")
    button = node.knob("autolens_create_selected")

    values = (
        _create_item_options(output)
        if enabled
        else ["Camera"]
    )

    previous = ""

    if knob is not None:
        try:
            previous = knob.value()
        except Exception:
            previous = ""

    if (
        force_rebuild
        and knob is not None
    ):
        try:
            node.removeKnob(knob)
            knob = None
        except Exception:
            pass

        # Rebuild Create too so the dropdown remains directly before it.
        if button is not None:
            try:
                node.removeKnob(button)
                button = None
            except Exception:
                pass

    if knob is None:
        try:
            knob = nuke.Enumeration_Knob(
                "autolens_create_item",
                "Create from solve",
                list(values),
            )
            node.addKnob(knob)
        except Exception:
            knob = node.knob(
                "autolens_create_item"
            )
    else:
        try:
            knob.setValues(
                list(values)
            )
        except Exception:
            pass

    if button is None:
        try:
            button = _button(
                "autolens_create_selected",
                "Create",
                "import autolens.node as _al; _al.create_selected_solve_item(nuke.thisNode())",
                startline=False,
                tooltip=(
                    "Create only the selected solved asset. Individual guided "
                    "surfaces are created as separate ReadGeo nodes."
                ),
            )
            node.addKnob(button)
        except Exception:
            button = node.knob(
                "autolens_create_selected"
            )

    if (
        knob is not None
        and previous in values
    ):
        try:
            knob.setValue(
                previous
            )
        except Exception:
            pass

    _set_knob_enabled(
        button,
        bool(enabled),
    )


def _extract_named_nuke_node_block(nk_text, node_name):
    """Extract a complete top-level Nuke node block by scripted name."""
    text = str(nk_text)
    line_pattern = re.compile(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)\s*\{\s*$")

    # Precompute brace depth per character so nested blocks such as Camera2
    # matrix { ... } are never mistaken for top-level nodes.
    depth = 0
    depth_at = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        depth_at[i] = depth
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    depth_at[len(text)] = depth

    for match in line_pattern.finditer(text):
        open_brace = text.find("{", match.start(), match.end())
        if open_brace < 0 or depth_at[open_brace] != 0:
            continue

        block_depth = 0
        end_index = None
        for index in range(open_brace, len(text)):
            ch = text[index]
            if ch == "{":
                block_depth += 1
            elif ch == "}":
                block_depth -= 1
                if block_depth == 0:
                    end_index = index + 1
                    break
        if end_index is None:
            continue

        block = text[match.start():end_index]
        if re.search(rf"(?m)^\s*name\s+{re.escape(node_name)}\s*$", block):
            return block + "\n"
    return None


def _paste_single_named_result_node(result_nk, scripted_name):
    text = Path(result_nk).read_text(
        encoding="utf-8",
        errors="replace",
    )
    block = _extract_named_nuke_node_block(text, scripted_name)
    if not block:
        raise RuntimeError(
            f"Could not find {scripted_name} in AutoLens result."
        )

    for existing in nuke.selectedNodes():
        existing.setSelected(False)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".nk",
            prefix="autolens_create_",
            delete=False,
            encoding="utf-8",
        ) as temp:
            temp.write(block)
            temp_path = temp.name

        nuke.nodePaste(temp_path)
        created = nuke.selectedNodes()
        if not created:
            raise RuntimeError(f"Nuke did not create {scripted_name}.")
        return created[0]
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _create_readgeo(path, name, label=""):
    if not path:
        raise RuntimeError("No geometry path is available for this item.")

    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"Geometry file not found:\n{path}")

    geo = nuke.nodes.ReadGeo2(name=name)
    geo["file"].setValue(str(path).replace("\\", "/"))
    if geo.knob("label"):
        geo["label"].setValue(label)
    return geo


def create_selected_solve_item(node):
    ensure_runtime_knobs(node)

    # Always rescan solved assets at click time too.
    refresh_create_items(node)

    try:
        output = resolved_output_folder(node)
    except Exception:
        output = ""

    result_nk = result_path_for_node(node)
    if not result_nk:
        nuke.message(
            "No completed AutoLens result was found in the selected output folder."
        )
        return None

    try:
        selection = node["autolens_create_item"].value()
    except Exception:
        selection = "Camera"

    try:
        if selection == "Camera":
            created = _paste_single_named_result_node(
                result_nk,
                "AUTOLENS_CAMERA",
            )

        elif selection == "Undistort":
            created = _paste_single_named_result_node(
                result_nk,
                "AUTOLENS_UNDISTORT",
            )

        elif selection == "Redistort":
            created = _paste_single_named_result_node(
                result_nk,
                "AUTOLENS_REDISTORT",
            )

        elif selection in (
            "Proxy Mesh",
            "Mesh-fit Planes",
            "Guided Surfaces (Combined)",
        ):
            result_json = _result_json_for_output(output)
            if result_json is None:
                raise RuntimeError("autolens_result.json was not found.")

            payload = json.loads(
                result_json.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )
            geometry = payload.get("geometry_outputs") or {}

            if selection == "Proxy Mesh":
                created = _create_readgeo(
                    geometry.get("proxy_mesh_obj"),
                    "AUTOLENS_PROXY_MESH",
                )
            elif selection == "Mesh-fit Planes":
                created = _create_readgeo(
                    geometry.get("meshfit_planes_obj"),
                    "AUTOLENS_MESHFIT_PLANES",
                )
            else:
                created = _create_readgeo(
                    geometry.get("guided_surfaces_obj"),
                    "AUTOLENS_GUIDED_SURFACES",
                )

        elif selection.startswith("Guided: "):
            target = selection[len("Guided: "):]
            match = None
            for asset in _discover_guided_surface_assets(output):
                if str(asset["label"]) == target:
                    match = asset
                    break

            if match is None:
                raise RuntimeError(f"Guided surface '{target}' was not found.")

            safe = re.sub(r"[^A-Za-z0-9_]+", "_", target).strip("_") or "Surface"
            created = _create_readgeo(
                match["path"],
                "AUTOLENS_GUIDE_" + safe,
                target,
            )

        else:
            raise RuntimeError(
                f"Unknown create item: {selection}"
            )

        try:
            created.setXpos(node.xpos() + 180)
            created.setYpos(node.ypos() + 80)
        except Exception:
            pass

        return created

    except Exception as exc:
        nuke.message(
            "AutoLens create failed:\n\n"
            + str(exc)
        )
        raise


def refresh_create_items(
    node,
    force_rebuild=False,
):
    ensure_runtime_knobs(node)

    try:
        output = resolved_output_folder(node)
    except Exception:
        output = ""

    _refresh_create_item_knob(
        node,
        output,
        bool(result_path_for_node(node)),
        force_rebuild=force_rebuild,
    )


def refresh_result_status(node):
    """
    Update the AutoLens properties-panel result indicator based strictly on the
    folder currently shown in Output folder.
    """
    if node is None:
        return None

    ensure_runtime_knobs(node)

    output = ""
    try:
        if node.knob("autolens_output"):
            output = resolved_output_folder(node)
    except Exception:
        pass

    result = find_result_nk(output) if output else None
    status_knob = node.knob("autolens_result_status")

    import_knob = node.knob("autolens_import_result")

    if result is not None:
        if status_knob is not None:
            status_knob.setValue(
                '<font color="#63d471"><b>✓ Completed Result Available</b></font>'
            )
        try:
            node["autolens_result_nk"].setValue(str(result))
        except Exception:
            pass

        _set_knob_enabled(import_knob, True)
        _refresh_create_item_knob(
            node,
            output,
            True,
        )

        restore_result_guides(
            node,
            output=output,
        )
    else:
        if status_knob is not None:
            status_knob.setValue(
                '<font color="#888888">○ No completed result in this folder</font>'
            )
        try:
            node["autolens_result_nk"].setValue("")
        except Exception:
            pass

        _set_knob_enabled(import_knob, False)
        _refresh_create_item_knob(
            node,
            output,
            False,
        )

    return str(result) if result is not None else None


def autolens_knob_changed(node, knob):
    if node is None or knob is None:
        return

    try:
        name = knob.name()
    except Exception:
        return

    if name == "autolens_output_root":
        refresh_version_menu(
            node,
            prefer_latest=True,
        )
        _schedule_version_ui_refresh(
            node
        )

    elif name == "autolens_version":
        # Keep the hidden resolved output path synchronized immediately, then
        # rebuild result/create UI on the next event-loop turn. Nuke otherwise
        # caches the old Enumeration_Knob entries until the properties panel is
        # rebuilt by another action.
        refresh_version_menu(
            node,
            prefer_latest=False,
        )
        _schedule_version_ui_refresh(
            node
        )

    elif name == "showPanel":
        refresh_version_menu(
            node,
            prefer_latest=False,
        )
        refresh_result_status(node)
        refresh_create_items(node)


def upgrade_nodes(nodes, refresh=False):
    """
    Upgrade the given AutoLens controllers' knobs.

    refresh=True also rescans each node's output folder (solve versions,
    completed result, guided surfaces). Script open skips that by default:
    the properties panel (showPanel) and the workspace run the same refresh
    when they open.
    """
    for node in nodes:
        try:
            with timed(f"upgrade {node.fullName()}"):
                ensure_runtime_knobs(node)
                if refresh:
                    refresh_version_menu(
                        node,
                        prefer_latest=False,
                    )
                    refresh_result_status(node)
        except Exception:
            pass


def upgrade_existing_nodes(refresh=True):
    """Upgrade AutoLens Group controllers already present in the current script."""
    try:
        groups = nuke.allNodes("Group")
    except Exception:
        return

    if refresh:
        nodes = [node for node in groups if is_controller(node)]
    else:
        nodes = controllers_needing_upgrade(groups)

    upgrade_nodes(nodes, refresh=refresh)


def result_path_for_node(node):
    """Return a completed result from the controller's CURRENT output folder."""
    if node is None:
        return None

    ensure_runtime_knobs(node)

    output = ""
    try:
        output = resolved_output_folder(node)
    except Exception:
        pass

    result = find_result_nk(output) if output else None
    return str(result) if result is not None else None


def remember_result_path(node, path):
    if node is None or not path:
        return

    ensure_runtime_knobs(node)

    try:
        node["autolens_result_nk"].setValue(str(path))
    except Exception:
        pass

    refresh_result_status(node)



def _managed_python_for_refresh(node):
    try:
        value = node["autolens_python"].value().strip()
        if value and Path(value).exists():
            return value
    except Exception:
        pass

    if sys.platform.startswith("win"):
        candidate = (
            Path.home()
            / ".nuke"
            / "AutoLens"
            / ".venv"
            / "Scripts"
            / "python.exe"
        )
    else:
        candidate = (
            Path.home()
            / ".nuke"
            / "AutoLens"
            / ".venv"
            / "bin"
            / "python"
        )

    return str(candidate) if candidate.exists() else "python"


def refresh_guided_surfaces_for_node(
    node,
    quiet_if_no_guides=True,
):
    """
    Export current internal Roto guides and re-run ONLY guided-surface fitting
    for a completed track.
    """
    if node is None:
        return {"ran": False, "updated": False, "output": ""}

    path = result_path_for_node(node)
    if not path:
        return {"ran": False, "updated": False, "output": ""}

    source = source_read_for_node(node)
    if source is None:
        raise RuntimeError(
            "AutoLens could not find the upstream Read for guide refresh."
        )

    from .surface_guides import (
        export_surface_guides,
        shape_count,
    )

    if shape_count(node) <= 0:
        if quiet_if_no_guides:
            return {"ran": False, "updated": False, "output": ""}
        raise RuntimeError("No current AutoLens guide shapes exist.")

    output = Path(path).parent

    guide_path = export_surface_guides(
        output,
        source.fullName(),
        controller=node,
    )

    if guide_path is None:
        return {"ran": False, "updated": False, "output": ""}

    try:
        source_filename = nuke.filename(source)
    except Exception:
        source_filename = ""

    if not source_filename:
        raise RuntimeError(
            "AutoLens could not resolve the source Read filename."
        )

    cmd = [
        _managed_python_for_refresh(node),
        "-u",
        str(engine_path()),
        "--images",
        str(Path(source_filename).parent),
        "--output",
        str(output),
        "--surface-guides",
        str(guide_path),
        "--guides-only",
    ]

    set_status(node, "Refreshing guided surfaces")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    text = "\n".join(
        value
        for value in (
            proc.stdout.strip(),
            proc.stderr.strip(),
        )
        if value
    )

    if proc.returncode != 0:
        set_status(node, "Guide refresh failed")
        raise RuntimeError(
            "The guided-surfaces-only refresh failed.\n\n"
            + (text[-6000:] if text else f"Exit code {proc.returncode}")
        )

    set_status(node, "Guided surfaces updated")

    return {
        "ran": True,
        "updated": "Guided-surface refresh complete" in text,
        "output": text,
    }



def import_result_for_node(node):
    from .importer import import_result

    path = result_path_for_node(node)

    if not path:
        nuke.message(
            "No completed AutoLens result was found.\n\n"
            "Run AutoLens first, or point Output Folder at an existing solve."
        )
        return []

    source = source_read_for_node(node)
    source_name = source.fullName() if source is not None else None

    try:
        refresh_guided_surfaces_for_node(
            node,
            quiet_if_no_guides=True,
        )

        path = result_path_for_node(node) or path

        pasted = import_result(
            path,
            source_name,
            node.fullName(),
        )

        remember_result_path(node, path)
        set_status(node, "Result imported")
        return pasted

    except Exception as exc:
        nuke.message(
            "AutoLens result import failed:\n\n"
            f"{exc}\n\n"
            f"Result:\n{path}"
        )
        raise


def open_dialog(node):
    # The workspace's Solve panel refreshes the result status itself once it
    # knows the selected solve version (restore_result_state), so only the
    # knob upgrade runs here.
    with timed("open_dialog: ensure_runtime_knobs"):
        ensure_runtime_knobs(node)
    with timed("open_dialog: import workspace"):
        from .dialog import show_autolens_dialog
    return show_autolens_dialog(node)
