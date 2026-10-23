from __future__ import annotations

import json
import math
import os
import random
import tempfile
from pathlib import Path

import nuke
import nuke.rotopaint as rp

from . import __version__

GUIDE_NODE_NAME = "AUTOLENS_SURFACE_GUIDES"
INTERNAL_GUIDE_NODE_NAME = "AUTOLENS_SURFACE_GUIDES_INTERNAL"
GUIDE_FRAME_KNOB = "autolens_guide_frame"
SOURCE_READ_KNOB = "autolens_source_read"
CONTROLLER_KNOB = "autolens_controller_node"
GUIDE_LINK_KNOB = "autolens_guide_node"
SURFACE_FRAMES_KNOB = "autolens_surface_frames"



def _surface_frame_map_from_node(node):
    """
    Read per-surface guide-frame metadata.

    Stored as compact JSON on the private AutoLens Roto node so the metadata
    travels with the Nuke script without exposing extra root-graph nodes.
    """
    if node is None or node.knob(SURFACE_FRAMES_KNOB) is None:
        return {}

    try:
        raw = node[SURFACE_FRAMES_KNOB].value() or ""
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    result = {}
    for key, value in (data or {}).items():
        try:
            result[str(key)] = int(value)
        except Exception:
            pass
    return result


def _write_surface_frame_map(node, mapping):
    if node is None or node.knob(SURFACE_FRAMES_KNOB) is None:
        return

    clean = {}
    for key, value in (mapping or {}).items():
        try:
            clean[str(key)] = int(value)
        except Exception:
            pass

    try:
        node[SURFACE_FRAMES_KNOB].setValue(
            json.dumps(
                clean,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        pass


def surface_guide_frames(controller=None):
    """
    Return {surface_name: guide_frame} for all current guide shapes.

    Legacy scripts with only the old single Guide frame transparently inherit
    that frame for every surface until an individual surface is reassigned.
    """
    node = find_guide_node(controller)
    if node is None:
        return {}

    mapping = _surface_frame_map_from_node(node)

    fallback = int(nuke.frame())
    if node.knob(GUIDE_FRAME_KNOB):
        try:
            fallback = int(node[GUIDE_FRAME_KNOB].value())
        except Exception:
            pass

    result = {}
    for shape in list_shapes(controller):
        name = str(shape.name)
        result[name] = int(mapping.get(name, fallback))

    return result


def set_surface_guide_frame(
    surface_name,
    frame,
    controller=None,
    source_read=None,
):
    """
    Assign one guide surface to one timeline frame without moving every other
    surface. Returns the clamped frame actually stored.
    """
    node = ensure_guide_node(
        source_read,
        controller,
        show=False,
    )

    requested = int(frame)

    if source_read is not None:
        requested = int(
            clamp_frame_to_source(
                requested,
                source_read,
            )["frame"]
        )

    mapping = _surface_frame_map_from_node(node)
    mapping[str(surface_name)] = int(requested)
    _write_surface_frame_map(node, mapping)

    # Keep the legacy single-frame knob useful as a fallback / inspection value,
    # but do not use it to overwrite other surfaces.
    if node.knob(GUIDE_FRAME_KNOB):
        try:
            node[GUIDE_FRAME_KNOB].setValue(int(requested))
        except Exception:
            pass

    return int(requested)


def _guide_name_for_controller(controller=None):
    # Legacy external-node name. Kept only for migration/fallback.
    if controller is None:
        return GUIDE_NODE_NAME
    try:
        return f"{GUIDE_NODE_NAME}_{controller.name()}"
    except Exception:
        return GUIDE_NODE_NAME


def _find_internal_guide_node(controller):
    if controller is None or controller.Class() != "Group":
        return None

    node = None

    try:
        controller.begin()

        # Prefer the canonical internal name.
        node = nuke.toNode(INTERNAL_GUIDE_NODE_NAME)

        # Upgrade path: if a prior internal Roto already exists under another
        # AutoLens-ish name, accept it.
        if node is None:
            for candidate in nuke.allNodes("Roto"):
                if (
                    candidate.knob(CONTROLLER_KNOB)
                    and candidate[CONTROLLER_KNOB].value() == controller.fullName()
                ):
                    node = candidate
                    break

    except Exception:
        node = None

    finally:
        try:
            controller.end()
        except Exception:
            pass

    return node


def _find_legacy_external_guide_node(controller=None):
    """
    Find the old root-level AutoLens Roto node without confusing it with the
    new internal guide node.
    """
    if controller is None:
        return nuke.toNode(GUIDE_NODE_NAME)

    # First use the serialized link if it still points at a root/external node.
    try:
        knob = controller.knob(GUIDE_LINK_KNOB)
        if knob and knob.value():
            linked = nuke.toNode(knob.value())
            if linked is not None:
                # Internal nodes have the controller fullName as a prefix.
                prefix = controller.fullName() + "."
                if not linked.fullName().startswith(prefix):
                    return linked
    except Exception:
        pass

    # Then try the legacy deterministic root name.
    try:
        legacy = nuke.toNode(_guide_name_for_controller(controller))
        if legacy is not None:
            prefix = controller.fullName() + "."
            if not legacy.fullName().startswith(prefix):
                return legacy
    except Exception:
        pass

    return None


def find_guide_node(controller=None):
    """
    Return the controller's private internal Roto guide node.

    Legacy root-level guide nodes are returned only when there is no internal
    node yet, so the migration path can still find old scripts.
    """
    if controller is not None:
        internal = _find_internal_guide_node(controller)
        if internal is not None:
            return internal

        legacy = _find_legacy_external_guide_node(controller)
        if legacy is not None:
            return legacy

        return None

    return nuke.toNode(GUIDE_NODE_NAME)


def _internal_source_input(controller):
    if controller is None or controller.Class() != "Group":
        return None

    result = None

    try:
        controller.begin()

        # v0.10.3+ uses Source, but support older Input1 scripts too.
        result = nuke.toNode("Source") or nuke.toNode("Input1")

        if result is None:
            inputs = nuke.allNodes("Input")
            if inputs:
                result = sorted(
                    inputs,
                    key=lambda n: int(n["number"].value())
                    if n.knob("number")
                    else 0,
                )[0]

    except Exception:
        result = None

    finally:
        try:
            controller.end()
        except Exception:
            pass

    return result


def _copy_legacy_guide_into_group(controller, legacy_node):
    """
    Copy the full legacy Roto node into the AutoLens Group.

    Using Nuke's node-copy serialization preserves the actual Roto curves,
    Bezier handles, animation and custom knobs better than rebuilding from
    sampled guide polygons.
    """
    if controller is None or legacy_node is None:
        return None

    temp_path = None
    copied = None
    old_selection = []

    try:
        old_selection = list(nuke.selectedNodes())

        for node in old_selection:
            try:
                node.setSelected(False)
            except Exception:
                pass

        legacy_node.setSelected(True)

        fd, temp_path = tempfile.mkstemp(
            prefix="autolens_guide_migrate_",
            suffix=".nk",
        )
        import os
        os.close(fd)

        nuke.nodeCopy(temp_path)

        # Paste the serialized Roto into the AutoLens Group.
        controller.begin()
        try:
            for node in nuke.selectedNodes():
                try:
                    node.setSelected(False)
                except Exception:
                    pass

            pasted = nuke.nodePaste(temp_path)
            copied = pasted

            if copied is None:
                selected = nuke.selectedNodes()
                copied = selected[0] if selected else None

            if copied is None:
                raise RuntimeError(
                    "Nuke did not return the migrated AutoLens guide node."
                )

            try:
                copied.setName(
                    INTERNAL_GUIDE_NODE_NAME,
                    uncollide=True,
                )
            except TypeError:
                copied.setName(INTERNAL_GUIDE_NODE_NAME)

            # The internal guide is storage/edit state only. Hook it to the
            # Group's internal Source input, never directly to the external Read.
            internal_input = nuke.toNode("Source") or nuke.toNode("Input1")
            if internal_input is None:
                inputs = nuke.allNodes("Input")
                internal_input = inputs[0] if inputs else None

            if internal_input is not None:
                copied.setInput(0, internal_input)

        finally:
            controller.end()

        # Update the controller link before deleting the old root-level node.
        if controller.knob(GUIDE_LINK_KNOB):
            controller[GUIDE_LINK_KNOB].setValue(copied.fullName())

        # Only delete the old node after successful copy/link.
        nuke.delete(legacy_node)

        return copied

    except Exception:
        return None

    finally:
        if temp_path:
            try:
                Path(temp_path).unlink()
            except Exception:
                pass

        # Restore selection of root-level nodes.
        try:
            for node in nuke.selectedNodes():
                node.setSelected(False)
        except Exception:
            pass

        for node in old_selection:
            try:
                node.setSelected(True)
            except Exception:
                pass


def migrate_legacy_guide_into_controller(controller, source_read=None):
    """
    Move an old visible root-level guide Roto into the AutoLens Group.
    Returns the internal Roto when migration succeeds.
    """
    if controller is None or controller.Class() != "Group":
        return None

    internal = _find_internal_guide_node(controller)
    if internal is not None:
        return internal

    legacy = _find_legacy_external_guide_node(controller)
    if legacy is None:
        return None

    migrated = _copy_legacy_guide_into_group(
        controller,
        legacy,
    )

    if migrated is not None:
        _add_autolens_knobs(
            migrated,
            source_read=source_read,
            controller=controller,
        )

    return migrated


def guide_source_read_fullname(controller=None):
    node = find_guide_node(controller)
    if node is None:
        return None
    knob = node.knob(SOURCE_READ_KNOB)
    return knob.value() if knob else None



def source_frame_range(source_read):
    """Return the effective first/last frame of a Nuke Read node."""
    if source_read is None or source_read.Class() != "Read":
        return None

    first = last = None

    try:
        if source_read.knob("first") is not None:
            first = int(round(source_read["first"].value()))
        if source_read.knob("last") is not None:
            last = int(round(source_read["last"].value()))
    except Exception:
        first = last = None

    if first is None or last is None:
        try:
            first = int(source_read.firstFrame())
            last = int(source_read.lastFrame())
        except Exception:
            first = last = None

    if first is None or last is None:
        try:
            first = int(round(source_read["origfirst"].value()))
            last = int(round(source_read["origlast"].value()))
        except Exception:
            return None

    if first > last:
        first, last = last, first

    return int(first), int(last)


def clamp_frame_to_source(frame, source_read):
    """
    Clamp a requested frame into the Read's valid range.

    Example: frame 1 with a 523-642 Read becomes 523.
    """
    requested = int(round(frame))
    frame_range = source_frame_range(source_read)

    if frame_range is None:
        return {
            "requested": requested,
            "frame": requested,
            "first": None,
            "last": None,
            "clamped": False,
            "reason": None,
        }

    first, last = frame_range
    used = min(max(requested, first), last)

    reason = None
    if requested < first:
        reason = "before_source_range"
    elif requested > last:
        reason = "after_source_range"

    return {
        "requested": requested,
        "frame": int(used),
        "first": int(first),
        "last": int(last),
        "clamped": int(used) != requested,
        "reason": reason,
    }


def clamp_guide_frame(
    controller=None,
    source_read=None,
    requested_frame=None,
    move_timeline=False,
):
    """Clamp and persist the AutoLens guide frame against the source Read."""
    node = find_guide_node(controller)

    if source_read is None:
        source_name = guide_source_read_fullname(controller)
        source_read = nuke.toNode(source_name) if source_name else None

    if requested_frame is None:
        if node is not None and node.knob(GUIDE_FRAME_KNOB):
            requested_frame = int(node[GUIDE_FRAME_KNOB].value())
        else:
            requested_frame = int(nuke.frame())

    info = clamp_frame_to_source(requested_frame, source_read)

    if node is not None and node.knob(GUIDE_FRAME_KNOB):
        node[GUIDE_FRAME_KNOB].setValue(int(info["frame"]))

    if move_timeline and int(nuke.frame()) != int(info["frame"]):
        try:
            nuke.frame(int(info["frame"]))
        except Exception:
            pass

    return info


def _add_autolens_knobs(node, source_read=None, controller=None):
    if node.knob("autolens_surface_tab") is None:
        node.addKnob(nuke.Tab_Knob("autolens_surface_tab", "AutoLens Surfaces"))
    if node.knob(GUIDE_FRAME_KNOB) is None:
        k = nuke.Int_Knob(GUIDE_FRAME_KNOB, "Guide frame")
        initial_frame = int(nuke.frame())
        if source_read is not None:
            initial_frame = clamp_frame_to_source(
                initial_frame,
                source_read,
            )["frame"]
        k.setValue(int(initial_frame))
        node.addKnob(k)
    if node.knob(SURFACE_FRAMES_KNOB) is None:
        k = nuke.String_Knob(
            SURFACE_FRAMES_KNOB,
            "Surface guide frames",
        )
        k.setValue("{}")
        try:
            k.setVisible(False)
        except Exception:
            pass
        node.addKnob(k)
    if node.knob(SOURCE_READ_KNOB) is None:
        k = nuke.String_Knob(SOURCE_READ_KNOB, "Source Read")
        node.addKnob(k)
    if node.knob(CONTROLLER_KNOB) is None:
        k = nuke.String_Knob(CONTROLLER_KNOB, "AutoLens Node")
        node.addKnob(k)
    if node.knob("autolens_surface_help") is None:
        help_knob = nuke.Text_Knob(
            "autolens_surface_help",
            "",
            "Draw closed Bezier/B-spline shapes around approximate planar surfaces. "
            "Examples: Floor, BackWall, FridgeFront, CounterTop. AutoLens evaluates all visible shapes "
            "at each surface\'s own Guide frame and places them into 3D after the camera solve."
        )
        node.addKnob(help_knob)
    if source_read is not None and node.knob(SOURCE_READ_KNOB):
        node[SOURCE_READ_KNOB].setValue(source_read.fullName())
    if controller is not None and node.knob(CONTROLLER_KNOB):
        node[CONTROLLER_KNOB].setValue(controller.fullName())


def ensure_guide_node(source_read=None, controller=None, show=True):
    """
    Ensure surface-guide storage exists.

    With an AutoLens Group controller the Roto node lives INSIDE the Group,
    so the root node graph contains only the single AutoLens controller.
    """
    # First migrate old root-level nodes when opening legacy scripts.
    if controller is not None and controller.Class() == "Group":
        migrated = migrate_legacy_guide_into_controller(
            controller,
            source_read=source_read,
        )
        if migrated is not None:
            node = migrated
            created = False
        else:
            node = _find_internal_guide_node(controller)
            created = node is None

        if node is None:
            try:
                controller.begin()

                node = nuke.nodes.Roto(
                    name=INTERNAL_GUIDE_NODE_NAME
                )
                node["label"].setValue(
                    "AUTOLENS SURFACE GUIDES\n"
                    "Private guide storage"
                )
                node["tile_color"].setValue(0x3B6E8FFF)

                internal_input = (
                    nuke.toNode("Source")
                    or nuke.toNode("Input1")
                )

                if internal_input is None:
                    inputs = nuke.allNodes("Input")
                    internal_input = inputs[0] if inputs else None

                if internal_input is not None:
                    node.setInput(0, internal_input)

                # Keep the internal node tucked away if the artist enters the
                # Group manually.
                try:
                    node.setXpos(160)
                    node.setYpos(120)
                except Exception:
                    pass

            finally:
                controller.end()

        # Source Read remains metadata from the root comp. The actual Roto
        # input is the Group's internal Source node.
        _add_autolens_knobs(
            node,
            source_read,
            controller,
        )

        try:
            if controller.knob(GUIDE_LINK_KNOB):
                controller[GUIDE_LINK_KNOB].setValue(
                    node.fullName()
                )
        except Exception:
            pass

        if created:
            clamp_guide_frame(
                controller=controller,
                source_read=source_read,
                requested_frame=int(nuke.frame()),
                move_timeline=True,
            )
        elif node.knob(GUIDE_FRAME_KNOB):
            clamp_guide_frame(
                controller=controller,
                source_read=source_read,
                requested_frame=int(
                    node[GUIDE_FRAME_KNOB].value()
                ),
                move_timeline=False,
            )

        try:
            if node.knob("toolbox"):
                node["toolbox"].setValue("bezier")
        except Exception:
            pass

        if show:
            # Show the internal Roto properties directly. The root DAG still
            # contains only the AutoLens Group.
            try:
                nuke.show(node)
            except Exception:
                pass

        return node

    # Controller-less fallback retained for isolated/test workflows.
    node = find_guide_node(controller)
    created = node is None

    if node is None:
        node = nuke.nodes.Roto(
            name=_guide_name_for_controller(controller)
        )
        node["label"].setValue(
            "AUTOLENS SURFACE GUIDES\n"
            "Draw rough Floor / Wall / Surface shapes here"
        )
        node["tile_color"].setValue(0x3B6E8FFF)

        if source_read is not None:
            node.setInput(0, source_read)

    elif source_read is not None:
        node.setInput(0, source_read)

    _add_autolens_knobs(
        node,
        source_read,
        controller,
    )

    if created:
        clamp_guide_frame(
            controller=controller,
            source_read=source_read,
            requested_frame=int(nuke.frame()),
            move_timeline=True,
        )

    if show:
        for selected in nuke.selectedNodes():
            selected.setSelected(False)
        node.setSelected(True)
        nuke.show(node)

    return node


def set_guide_frame_to_current(controller=None):
    node = find_guide_node(controller)
    if node is None:
        raise RuntimeError("Create Surface Guides first.")

    source_name = guide_source_read_fullname(controller)
    source = nuke.toNode(source_name) if source_name else None

    info = clamp_guide_frame(
        controller=controller,
        source_read=source,
        requested_frame=int(nuke.frame()),
        move_timeline=True,
    )
    return int(info["frame"])


def _iter_shapes(layer):
    for element in layer:
        if isinstance(element, rp.Shape):
            yield element
        elif isinstance(element, rp.Layer):
            yield from _iter_shapes(element)


def list_shapes(controller=None):
    node = find_guide_node(controller)
    if node is None:
        return []
    return list(_iter_shapes(node["curves"].rootLayer))


def shape_count(controller=None):
    return len(list_shapes(controller))


def _xy(point):
    if hasattr(point, "x") and hasattr(point, "y"):
        return float(point.x), float(point.y)
    return float(point[0]), float(point[1])


def _rdp(points, epsilon):
    """Ramer-Douglas-Peucker simplification for an open point sequence."""
    if len(points) <= 2:
        return points[:]

    x1, y1 = points[0]
    x2, y2 = points[-1]
    dx, dy = x2 - x1, y2 - y1
    denom = math.hypot(dx, dy)

    max_dist = -1.0
    index = 0
    for i, (x, y) in enumerate(points[1:-1], 1):
        if denom < 1.0e-8:
            dist = math.hypot(x - x1, y - y1)
        else:
            dist = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / denom
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist > epsilon:
        left = _rdp(points[:index + 1], epsilon)
        right = _rdp(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def _sample_roto_shape(shape, frame):
    """
    Bake the actual evaluated Roto outline, so layer/shape transforms and Bezier
    handles are reflected in the exported guide rather than only raw CV centers.
    """
    # A shape with no or one CV can raise here; callers skip anything under three
    # points, so an unusable shape must not abort the whole export or reload.
    try:
        count = max(12, min(48, max(4, len(shape)) * 4))
        curve = shape.evaluate(0, float(frame))
        points = [_xy(curve.getPoint(i / float(count))) for i in range(count)]
    except Exception:
        return []

    # Closed-curve simplification: temporarily duplicate the first point, run
    # RDP, then remove a duplicate endpoint if one remains.
    closed = points + [points[0]]
    simplified = _rdp(closed, 1.5)
    if len(simplified) > 1:
        if math.hypot(
            simplified[0][0] - simplified[-1][0],
            simplified[0][1] - simplified[-1][1],
        ) < 0.01:
            simplified = simplified[:-1]

    # Fewer than three points after simplification means a line or a dot, which
    # would export as a zero-area guide.
    return simplified if len(simplified) >= 3 else []


def _write_json_atomic(path, payload):
    """Write JSON through a temp file so a crash never leaves a half-written file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _evaluated_source_filename(read_node, frame):
    old = int(nuke.frame())
    try:
        nuke.frame(int(frame))
        try:
            return nuke.filename(read_node)
        except Exception:
            return read_node["file"].evaluate()
    finally:
        nuke.frame(old)



def export_surface_guides(output_folder, source_read_fullname=None, controller=None):
    node = find_guide_node(controller)
    if node is None:
        return None

    shapes = list_shapes(controller)
    if not shapes:
        return None

    source_name = source_read_fullname or guide_source_read_fullname(controller)
    source = nuke.toNode(source_name) if source_name else None
    if source is None or source.Class() != "Read":
        raise RuntimeError(
            "AutoLens Surface Guides exist, but their source Read node could not be found."
        )

    frame_map = surface_guide_frames(controller)

    fallback_requested = (
        int(node[GUIDE_FRAME_KNOB].value())
        if node.knob(GUIDE_FRAME_KNOB)
        else int(nuke.frame())
    )

    surfaces = []
    used_frames = []

    for shape in shapes:
        name = str(shape.name)
        requested_frame = int(
            frame_map.get(
                name,
                fallback_requested,
            )
        )

        frame_info = clamp_frame_to_source(
            requested_frame,
            source,
        )
        frame = int(frame_info["frame"])

        try:
            if not shape.getVisible(float(frame)):
                continue
        except Exception:
            pass

        points = _sample_roto_shape(
            shape,
            frame,
        )
        if len(points) < 3:
            continue

        source_filename = _evaluated_source_filename(
            source,
            frame,
        )

        surfaces.append({
            "name": name,
            "points": [
                [float(x), float(y)]
                for x, y in points
            ],
            "guide_frame": frame,
            "frame": frame,
            "frame_requested": int(requested_frame),
            "frame_was_clamped": bool(frame_info["clamped"]),
            "source_image": (
                Path(source_filename).name
                if source_filename
                else None
            ),
        })
        used_frames.append(frame)

    if not surfaces:
        return None

    # Keep top-level single-frame fields for backwards compatibility. New
    # engines use the per-surface fields above.
    primary = surfaces[0]
    unique_frames = sorted(set(used_frames))

    payload = {
        "version": __version__,
        "coordinate_system": "nuke_bottom_left_pixels",
        "frame": int(primary["guide_frame"]),
        "frame_requested": int(primary["frame_requested"]),
        "frame_was_clamped": bool(primary["frame_was_clamped"]),
        "source_first_frame": int(
            source_frame_range(source)[0]
        ),
        "source_last_frame": int(
            source_frame_range(source)[1]
        ),
        "source_image": primary.get("source_image"),
        "source_read": source.fullName(),
        "width": int(source.width()),
        "height": int(source.height()),
        "pixel_aspect": float(source.format().pixelAspect()),
        "guide_frames": unique_frames,
        "multi_frame_guides": len(unique_frames) > 1,
        "surfaces": surfaces,
    }

    output = Path(output_folder)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "autolens_surface_guides.json"
    _write_json_atomic(path, payload)
    return path



def surface_guides_data(controller=None, frame=None):
    """
    Return all current AutoLens surfaces as editable dictionaries.

    Each surface is sampled at its OWN guide frame. `frame` is the viewer's
    current frame and is only included so callers can style active/inactive
    guides; it no longer forces all surfaces onto one global guide frame.
    """
    node = find_guide_node(controller)
    if node is None:
        return []

    current_frame = (
        int(nuke.frame())
        if frame is None
        else int(frame)
    )

    source_name = guide_source_read_fullname(controller)
    source = nuke.toNode(source_name) if source_name else None

    frame_map = surface_guide_frames(controller)

    fallback = int(nuke.frame())
    if node.knob(GUIDE_FRAME_KNOB):
        try:
            fallback = int(node[GUIDE_FRAME_KNOB].value())
        except Exception:
            pass

    result = []

    for shape in list_shapes(controller):
        name = str(shape.name)
        guide_frame = int(
            frame_map.get(
                name,
                fallback,
            )
        )

        if source is not None and source.Class() == "Read":
            guide_frame = int(
                clamp_frame_to_source(
                    guide_frame,
                    source,
                )["frame"]
            )

        try:
            if not shape.getVisible(float(guide_frame)):
                continue
        except Exception:
            pass

        points = _sample_roto_shape(
            shape,
            guide_frame,
        )
        if len(points) < 3:
            continue

        result.append({
            "name": name,
            "points": [
                (float(x), float(y))
                for x, y in points
            ],
            "guide_frame": int(guide_frame),
            "is_guide_frame": (
                int(current_frame)
                == int(guide_frame)
            ),
        })

    return result



def replace_surface_guides(
    surfaces,
    source_read=None,
    controller=None,
    frame=None,
    show_node=True,
):
    """
    Replace all shapes in the dedicated AutoLens guide Roto node while
    preserving an independent guide frame for every surface.
    """
    node = ensure_guide_node(
        source_read,
        controller,
        show=show_node,
    )

    if frame is None:
        frame = int(nuke.frame())

    if source_read is not None:
        frame = int(
            clamp_frame_to_source(
                frame,
                source_read,
            )["frame"]
        )

    previous_frames = _surface_frame_map_from_node(
        node
    )

    curves = node["curves"]
    root = curves.rootLayer

    remove_indices = []
    for i, element in enumerate(root):
        if isinstance(element, rp.Shape):
            remove_indices.append(i)

    for i in reversed(remove_indices):
        root.remove(i)

    new_frames = {}

    for item in surfaces:
        name = str(
            item.get("name")
            or "Surface"
        )
        points = item.get("points") or []

        if len(points) < 3:
            continue

        guide_frame = item.get(
            "guide_frame",
            item.get(
                "frame",
                previous_frames.get(
                    name,
                    frame,
                ),
            ),
        )

        try:
            guide_frame = int(guide_frame)
        except Exception:
            guide_frame = int(frame)

        if source_read is not None:
            guide_frame = int(
                clamp_frame_to_source(
                    guide_frame,
                    source_read,
                )["frame"]
            )

        _create_shape(
            curves,
            name,
            points,
        )
        new_frames[name] = guide_frame

    _write_surface_frame_map(
        node,
        new_frames,
    )

    # Legacy fallback knob = first valid surface frame, not "whatever frame the
    # artist happened to be viewing when they edited a point".
    if (
        node.knob(GUIDE_FRAME_KNOB)
        and new_frames
    ):
        try:
            node[GUIDE_FRAME_KNOB].setValue(
                int(
                    next(
                        iter(
                            new_frames.values()
                        )
                    )
                )
            )
        except Exception:
            pass

    try:
        curves.changed()
    except Exception:
        pass

    return node


def result_surface_guides_path(output_folder):
    if not output_folder:
        return None

    path = Path(output_folder).expanduser() / "autolens_surface_guides.json"
    return path if path.exists() else None


def restore_surface_guides_from_output(
    output_folder,
    source_read=None,
    controller=None,
    replace_existing=False,
):
    """
    Rebuild this track's editable Nuke Roto guides from the guide JSON saved in
    the AutoLens output folder.

    Normal automatic restoration is conservative:
      - if no guide node exists, restore;
      - if a guide node exists but contains no shapes, restore;
      - if the controller already contains edited shapes, leave them alone.

    Returns a small status dictionary for UI/logging.
    """
    path = result_surface_guides_path(output_folder)

    if path is None:
        return {
            "available": False,
            "restored": False,
            "count": 0,
            "path": None,
            "reason": "no_saved_guides",
        }

    try:
        payload = json.loads(
            path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )
    except Exception as exc:
        return {
            "available": True,
            "restored": False,
            "count": 0,
            "path": str(path),
            "reason": "invalid_json",
            "error": str(exc),
        }

    surfaces = payload.get("surfaces") or []
    surfaces = [
        item for item in surfaces
        if len(item.get("points") or []) >= 3
    ]

    if not surfaces:
        return {
            "available": True,
            "restored": False,
            "count": 0,
            "path": str(path),
            "reason": "no_shapes",
        }

    existing = find_guide_node(controller)
    existing_count = shape_count(controller) if existing is not None else 0

    if existing_count > 0 and not replace_existing:
        return {
            "available": True,
            "restored": False,
            "count": existing_count,
            "path": str(path),
            "reason": "existing_guides_preserved",
        }

    if source_read is None and controller is not None:
        try:
            # Local import avoids a module-level cycle.
            from .node import source_read_for_node
            source_read = source_read_for_node(controller)
        except Exception:
            source_read = None

    if source_read is None or source_read.Class() != "Read":
        return {
            "available": True,
            "restored": False,
            "count": 0,
            "path": str(path),
            "reason": "source_read_missing",
        }

    requested_frame = payload.get("frame")
    if requested_frame is None:
        requested_frame = payload.get("frame_requested")
    if requested_frame is None:
        requested_frame = int(nuke.frame())

    frame_info = clamp_frame_to_source(
        requested_frame,
        source_read,
    )
    frame = int(frame_info["frame"])

    # New multi-frame files carry each surface's own frame. Legacy files inherit
    # the old top-level frame automatically.
    for item in surfaces:
        if item.get("guide_frame") is None:
            item["guide_frame"] = item.get(
                "frame",
                frame,
            )

    # Recreate the dedicated Roto node without opening its properties.
    node = ensure_guide_node(
        source_read,
        controller,
        show=False,
    )

    node = replace_surface_guides(
        surfaces,
        source_read=source_read,
        controller=controller,
        frame=frame,
        show_node=False,
    )

    try:
        node["label"].setValue(
            "AUTOLENS SURFACE GUIDES\n"
            f"Restored from {path.parent.name} • frame {frame}"
        )
    except Exception:
        pass

    # Remember where these were recovered from. This is intentionally only
    # bookkeeping; automatic restoration still refuses to overwrite live shapes.
    if controller is not None:
        try:
            knob = controller.knob("autolens_guides_source")
            if knob is not None:
                knob.setValue(str(path.resolve()))
        except Exception:
            pass

    return {
        "available": True,
        "restored": True,
        "count": len(surfaces),
        "frame": frame,
        "frame_requested": requested_frame,
        "frame_was_clamped": bool(frame_info["clamped"]),
        "path": str(path),
        "reason": "restored",
        "node": node.fullName(),
    }


def _create_shape(curves, name, points):
    try:
        shape = rp.Shape(curves, *[(float(x), float(y)) for x, y in points])
    except Exception:
        import _curveknob
        shape = _curveknob.Shape(curves, *[(float(x), float(y)) for x, y in points])
    shape.name = name
    curves.rootLayer.append(shape)
    return shape


def _remove_auto_shapes(curves):
    root = curves.rootLayer
    remove_indices = []
    for i, element in enumerate(root):
        if isinstance(element, rp.Shape) and str(element.name).startswith("AUTO_"):
            remove_indices.append(i)
    for i in reversed(remove_indices):
        root.remove(i)


def _choose_channels(node):
    channels = list(node.channels())
    preferred = []
    for c in ("rgba.red", "rgba.green", "rgba.blue", "rgb.red", "rgb.green", "rgb.blue"):
        if c in channels:
            preferred.append(c)
    if preferred:
        return preferred[:3]
    return channels[:3]


def _sample_frame_luma(node, width, height, nx=80, ny=45):
    channels = _choose_channels(node)
    if not channels:
        raise RuntimeError("The source Read has no sampleable image channels.")

    image = []
    for iy in range(ny):
        y = (iy + 0.5) * height / float(ny)
        row = []
        for ix in range(nx):
            x = (ix + 0.5) * width / float(nx)
            vals = []
            for c in channels:
                try:
                    vals.append(float(node.sample(c, x, y)))
                except Exception:
                    pass
            row.append(sum(vals) / len(vals) if vals else 0.0)
        image.append(row)
    return image


def _percentile(values, q):
    values = sorted(float(v) for v in values)
    if not values:
        return 0.0
    index = int(round((len(values) - 1) * float(q)))
    return values[max(0, min(len(values) - 1, index))]


def _line_value(model, independent):
    # dependent = slope * independent + intercept
    return model["slope"] * independent + model["intercept"]


def _fit_dominant_line(
    points,
    orientation,
    independent_size,
    dependent_size,
    max_abs_slope,
    tolerance,
    min_span_fraction,
    seed,
):
    """
    Weighted deterministic RANSAC.

    orientation "x_of_y": x = a*y+b  (wall edges)
    orientation "y_of_x": y = a*x+b  (horizontal architectural seams)
    """
    if len(points) < 8:
        return None

    points = sorted(points, key=lambda p: p[2], reverse=True)[:220]
    rng = random.Random(seed)

    pairs = []
    n = len(points)
    # Deterministic strong-point pairs plus seeded random pairs.
    top = min(n, 36)
    for i in range(top):
        for j in range(i + 1, min(top, i + 9)):
            pairs.append((i, j))
    for _ in range(min(550, n * 4)):
        i, j = rng.sample(range(n), 2)
        pairs.append((i, j))

    best = None
    total_weight = sum(max(0.0, p[2]) for p in points) or 1.0

    for i, j in pairs:
        p1 = points[i]
        p2 = points[j]

        if orientation == "x_of_y":
            independent1, dependent1 = p1[1], p1[0]
            independent2, dependent2 = p2[1], p2[0]
        else:
            independent1, dependent1 = p1[0], p1[1]
            independent2, dependent2 = p2[0], p2[1]

        delta = independent2 - independent1
        if abs(delta) < 1.0e-6:
            continue

        slope = (dependent2 - dependent1) / delta
        if abs(slope) > max_abs_slope:
            continue
        intercept = dependent1 - slope * independent1

        inliers = []
        score_weight = 0.0
        coords = []
        for x, y, w in points:
            independent = y if orientation == "x_of_y" else x
            dependent = x if orientation == "x_of_y" else y
            error = abs(dependent - (slope * independent + intercept))
            if error <= tolerance:
                inliers.append((x, y, w))
                score_weight += w * (1.0 - 0.45 * error / max(tolerance, 1.0e-6))
                coords.append(independent)

        if len(inliers) < 5:
            continue

        span = (max(coords) - min(coords)) / max(float(independent_size), 1.0)
        if span < min_span_fraction:
            continue

        support = score_weight / total_weight
        score = support * (0.35 + 0.65 * min(1.0, span / max(min_span_fraction, 1.0e-6)))

        if best is None or score > best["score"]:
            # Refit weighted least squares over inliers.
            sw = sum(max(p[2], 1.0e-8) for p in inliers)
            sx = sy = sxx = sxy = 0.0
            for x, y, w in inliers:
                independent = y if orientation == "x_of_y" else x
                dependent = x if orientation == "x_of_y" else y
                ww = max(w, 1.0e-8)
                sx += ww * independent
                sy += ww * dependent
                sxx += ww * independent * independent
                sxy += ww * independent * dependent

            denom = sw * sxx - sx * sx
            if abs(denom) > 1.0e-8:
                refined_slope = (sw * sxy - sx * sy) / denom
                refined_intercept = (sy - refined_slope * sx) / sw
                if abs(refined_slope) <= max_abs_slope:
                    slope = refined_slope
                    intercept = refined_intercept

            best = {
                "slope": slope,
                "intercept": intercept,
                "score": score,
                "support": support,
                "span": span,
                "inliers": len(inliers),
            }

    return best


def _perspective_surface_suggestion(img, nx, ny):
    """
    Detect long architectural edges instead of always forcing a four-sided room.

    Returns a confidence-aware room-layout hypothesis in sample-grid coordinates.
    """
    gradients = []
    gx_values = []
    gy_values = []

    for y in range(1, ny - 1):
        for x in range(1, nx - 1):
            gx = 0.5 * (img[y][x + 1] - img[y][x - 1])
            gy = 0.5 * (img[y + 1][x] - img[y - 1][x])
            strength = math.hypot(gx, gy)
            gradients.append((x, y, gx, gy, strength))
            gx_values.append(abs(gx))
            gy_values.append(abs(gy))

    gx_cut = _percentile(gx_values, 0.88)
    gy_cut = _percentile(gy_values, 0.88)

    left_points = []
    right_points = []
    horizontal_points = []

    for x, y, gx, gy, strength in gradients:
        # Vertical/sloping architectural edge: primarily horizontal gradient.
        if abs(gx) >= gx_cut and abs(gx) > abs(gy) * 0.75:
            if x <= nx * 0.46:
                left_points.append((x, y, abs(gx)))
            elif x >= nx * 0.54:
                right_points.append((x, y, abs(gx)))

        # Horizontal/sloping architectural seam: primarily vertical gradient.
        if abs(gy) >= gy_cut and abs(gy) > abs(gx) * 0.75:
            if ny * 0.20 <= y <= ny * 0.78:
                horizontal_points.append((x, y, abs(gy)))

    left = _fit_dominant_line(
        left_points, "x_of_y", ny, nx,
        max_abs_slope=1.15, tolerance=1.6,
        min_span_fraction=0.42, seed=17,
    )
    right = _fit_dominant_line(
        right_points, "x_of_y", ny, nx,
        max_abs_slope=1.15, tolerance=1.6,
        min_span_fraction=0.42, seed=29,
    )
    horizon = _fit_dominant_line(
        horizontal_points, "y_of_x", nx, ny,
        max_abs_slope=0.38, tolerance=1.4,
        min_span_fraction=0.48, seed=41,
    )

    # Confidence deliberately punishes missing long architectural evidence.
    components = [m["score"] for m in (left, right, horizon) if m is not None]
    overall = sum(components) / 3.0 if len(components) == 3 else (
        sum(components) / 3.0 if components else 0.0
    )

    reliable_left = left is not None and left["score"] >= 0.075
    reliable_right = right is not None and right["score"] >= 0.075
    reliable_horizon = horizon is not None and horizon["score"] >= 0.09

    return {
        "left": left if reliable_left else None,
        "right": right if reliable_right else None,
        "horizon": horizon if reliable_horizon else None,
        "confidence": overall,
        "reliable_count": sum((reliable_left, reliable_right, reliable_horizon)),
    }



# -------------------------------------------------------------------------
# General planar-face proposal helpers (v0.8.4)
# -------------------------------------------------------------------------

def _line_from_points(p1, p2):
    x1, y1 = p1
    x2, y2 = p2
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length < 1.0e-6:
        return None

    # ax + by + c = 0, normalized.
    a = dy / length
    b = -dx / length
    c = -(a * x1 + b * y1)

    angle = math.degrees(math.atan2(dy, dx))
    while angle < -90.0:
        angle += 180.0
    while angle >= 90.0:
        angle -= 180.0

    return {
        "a": a,
        "b": b,
        "c": c,
        "angle": angle,
    }


def _line_distance(line, x, y):
    return abs(line["a"] * x + line["b"] * y + line["c"])


def _line_intersection(l1, l2):
    det = l1["a"] * l2["b"] - l2["a"] * l1["b"]
    if abs(det) < 1.0e-8:
        return None
    x = (l1["b"] * l2["c"] - l2["b"] * l1["c"]) / det
    y = (l1["c"] * l2["a"] - l2["c"] * l1["a"]) / det
    return (x, y)


def _angle_difference(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _extract_general_lines(img, nx, ny, max_lines=18):
    """
    Lightweight weighted line-RANSAC over strong image gradients.

    This deliberately finds LOCAL object boundaries as well as architectural
    room edges. No semantic model is required.
    """
    samples = []
    strengths = []

    for y in range(1, ny - 1):
        for x in range(1, nx - 1):
            gx = 0.5 * (img[y][x + 1] - img[y][x - 1])
            gy = 0.5 * (img[y + 1][x] - img[y - 1][x])
            s = math.hypot(gx, gy)
            strengths.append(s)
            samples.append((x, y, s))

    threshold = _percentile(strengths, 0.90)
    points = [(x, y, s) for x, y, s in samples if s >= threshold]

    # Keep the strongest set so proposal generation remains interactive.
    points.sort(key=lambda p: p[2], reverse=True)
    points = points[:800]

    if len(points) < 20:
        return []

    rng = random.Random(8137)
    remaining = points[:]
    lines = []

    diag = math.hypot(nx, ny)
    tolerance = max(1.0, diag * 0.010)

    while len(lines) < max_lines and len(remaining) >= 12:
        best = None

        candidate_indices = list(range(min(len(remaining), 140)))
        trials = min(900, max(220, len(candidate_indices) * 5))

        for _ in range(trials):
            i, j = rng.sample(candidate_indices, 2)
            p1 = remaining[i]
            p2 = remaining[j]

            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < diag * 0.12:
                continue

            line = _line_from_points((p1[0], p1[1]), (p2[0], p2[1]))
            if line is None:
                continue

            inliers = []
            weight = 0.0
            coords = []

            # line-direction vector is (-b, a)
            tx, ty = -line["b"], line["a"]

            for idx, (x, y, s) in enumerate(remaining):
                d = _line_distance(line, x, y)
                if d <= tolerance:
                    inliers.append(idx)
                    weight += s * (1.0 - 0.55 * d / tolerance)
                    coords.append(x * tx + y * ty)

            if len(inliers) < 7:
                continue

            span = max(coords) - min(coords)
            span_fraction = span / diag
            if span_fraction < 0.16:
                continue

            score = weight * (0.35 + 0.65 * min(1.0, span_fraction / 0.42))

            if best is None or score > best["score"]:
                ordered_coords = sorted(coords)
                t_min = _percentile(ordered_coords, 0.05)
                t_max = _percentile(ordered_coords, 0.95)

                best = {
                    **line,
                    "score": score,
                    "span_fraction": span_fraction,
                    "inlier_indices": inliers,
                    "tx": tx,
                    "ty": ty,
                    "t_min": t_min,
                    "t_max": t_max,
                }

        if best is None:
            break

        # Convert score to something roughly normalized by total remaining edge energy.
        total = sum(p[2] for p in remaining) or 1.0
        best["support"] = best["score"] / total

        lines.append(best)

        inlier_set = set(best["inlier_indices"])
        remaining = [
            p for idx, p in enumerate(remaining)
            if idx not in inlier_set
        ]

    return lines


def _polygon_area(poly):
    area = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def _is_convex_quad(poly):
    if len(poly) != 4:
        return False
    signs = []
    for i in range(4):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % 4]
        x3, y3 = poly[(i + 2) % 4]
        cross = (x2 - x1) * (y3 - y2) - (y2 - y1) * (x3 - x2)
        if abs(cross) > 1.0e-6:
            signs.append(1 if cross > 0 else -1)
    return bool(signs) and all(s == signs[0] for s in signs)


def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 1.0e-8 else 0.0


def _order_quad(points):
    cx = sum(p[0] for p in points) / 4.0
    cy = sum(p[1] for p in points) / 4.0
    return sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))



def _point_line_t(line, point):
    return point[0] * line["tx"] + point[1] * line["ty"]


def _intersection_supported(line, point, extension_fraction=0.10):
    """
    Reject infinite-line intersections that lie far beyond the edge evidence
    actually present in the frame.
    """
    t = _point_line_t(line, point)
    span = max(line["t_max"] - line["t_min"], 1.0)
    pad = span * extension_fraction
    return (line["t_min"] - pad) <= t <= (line["t_max"] + pad)


def _object_orientation_class(line):
    angle = abs(line["angle"])
    # line angles are normalized into [-90, 90)
    if angle <= 28.0:
        return "horizontal"
    if angle >= 62.0:
        return "vertical"
    return "diagonal"


def _quad_edge_lengths(poly):
    result = []
    for i in range(4):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % 4]
        result.append(math.hypot(x2 - x1, y2 - y1))
    return result


def _generate_object_plane_candidates(lines, nx, ny, max_candidates=5):
    """
    Conservative LOCAL planar-face proposals.

    Unlike v0.8.4, intersections must sit on the line SEGMENTS actually
    supported by image edges. This prevents unrelated infinite lines from
    producing huge diagonal projection cards across the frame.

    For pre-solve suggestions we deliberately favor front/side object faces
    bounded by roughly horizontal + vertical lines. More arbitrary sloped
    planes are recovered later from the solved proxy mesh.
    """
    if len(lines) < 4:
        return []

    image_area = float(nx * ny)
    candidates = []

    horizontal = [
        l for l in lines
        if _object_orientation_class(l) == "horizontal"
        and l.get("support", 0.0) >= 0.015
        and l.get("span_fraction", 0.0) >= 0.14
    ]
    vertical = [
        l for l in lines
        if _object_orientation_class(l) == "vertical"
        and l.get("support", 0.0) >= 0.015
        and l.get("span_fraction", 0.0) >= 0.14
    ]

    # Cap combinatorics to strongest supported segments.
    horizontal = sorted(horizontal, key=lambda l: l["score"], reverse=True)[:10]
    vertical = sorted(vertical, key=lambda l: l["score"], reverse=True)[:10]

    for hi in range(len(horizontal)):
        for hj in range(hi + 1, len(horizontal)):
            h1, h2 = horizontal[hi], horizontal[hj]

            if _angle_difference(h1["angle"], h2["angle"]) > 12.0:
                continue

            for vi in range(len(vertical)):
                for vj in range(vi + 1, len(vertical)):
                    v1, v2 = vertical[vi], vertical[vj]

                    if _angle_difference(v1["angle"], v2["angle"]) > 12.0:
                        continue

                    corners = [
                        _line_intersection(h1, v1),
                        _line_intersection(h1, v2),
                        _line_intersection(h2, v2),
                        _line_intersection(h2, v1),
                    ]
                    if any(p is None for p in corners):
                        continue

                    # Every corner must be locally supported on BOTH edges.
                    supported = (
                        _intersection_supported(h1, corners[0])
                        and _intersection_supported(v1, corners[0])
                        and _intersection_supported(h1, corners[1])
                        and _intersection_supported(v2, corners[1])
                        and _intersection_supported(h2, corners[2])
                        and _intersection_supported(v2, corners[2])
                        and _intersection_supported(h2, corners[3])
                        and _intersection_supported(v1, corners[3])
                    )
                    if not supported:
                        continue

                    quad = _order_quad(corners)
                    if not _is_convex_quad(quad):
                        continue

                    # Object planes must actually live inside the visible frame.
                    if any(
                        x < 0.0 or x > nx
                        or y < 0.0 or y > ny
                        for x, y in quad
                    ):
                        continue

                    area = _polygon_area(quad)
                    area_fraction = area / image_area

                    # Useful localized faces, not tiny details or full-room boxes.
                    if area_fraction < 0.012 or area_fraction > 0.30:
                        continue

                    bb = _bbox(quad)
                    bw = bb[2] - bb[0]
                    bh = bb[3] - bb[1]

                    if bw < nx * 0.055 or bh < ny * 0.075:
                        continue

                    aspect = max(
                        bw / max(bh, 1.0e-6),
                        bh / max(bw, 1.0e-6),
                    )
                    if aspect > 5.5:
                        continue

                    edges = _quad_edge_lengths(quad)
                    if min(edges) < math.hypot(nx, ny) * 0.045:
                        continue

                    # Require the two horizontal sides and two vertical sides to
                    # be reasonably balanced. This suppresses triangular-looking
                    # perspective accidents.
                    horizontal_balance = min(edges[0], edges[2]) / max(edges[0], edges[2])
                    vertical_balance = min(edges[1], edges[3]) / max(edges[1], edges[3])

                    if horizontal_balance < 0.22 or vertical_balance < 0.22:
                        continue

                    edge_support = (
                        h1.get("support", 0.0)
                        + h2.get("support", 0.0)
                        + v1.get("support", 0.0)
                        + v2.get("support", 0.0)
                    )

                    size_score = min(1.0, area_fraction / 0.12)
                    balance_score = 0.5 * (horizontal_balance + vertical_balance)

                    score = (
                        0.60 * edge_support
                        + 0.23 * size_score
                        + 0.17 * balance_score
                    )

                    # Be intentionally conservative before the solve.
                    if score < 0.055:
                        continue

                    candidates.append({
                        "points": quad,
                        "score": score,
                        "area_fraction": area_fraction,
                        "bbox": bb,
                    })

    candidates.sort(key=lambda c: c["score"], reverse=True)

    selected = []
    for candidate in candidates:
        if any(_bbox_iou(candidate["bbox"], s["bbox"]) > 0.45 for s in selected):
            continue

        # Avoid nested duplicate cards.
        cx = 0.5 * (candidate["bbox"][0] + candidate["bbox"][2])
        cy = 0.5 * (candidate["bbox"][1] + candidate["bbox"][3])

        nested = False
        for prior in selected:
            px0, py0, px1, py1 = prior["bbox"]
            if px0 <= cx <= px1 and py0 <= cy <= py1:
                if candidate["area_fraction"] < prior["area_fraction"] * 0.55:
                    nested = True
                    break

        if nested:
            continue

        selected.append(candidate)
        if len(selected) >= max_candidates:
            break

    return selected

def _object_plane_suggestions(img, nx, ny, max_candidates=5):
    lines = _extract_general_lines(img, nx, ny, max_lines=18)
    candidates = _generate_object_plane_candidates(
        lines,
        nx,
        ny,
        max_candidates=max_candidates,
    )
    return {
        "lines": lines,
        "candidates": candidates,
    }


def auto_suggest_surfaces(source_read=None, controller=None):
    """
    Suggest BOTH room-layout surfaces and local planar object faces.

    Room surfaces are confidence-limited perspective hypotheses.
    Object surfaces are bounded quadrilateral proposals from coherent line
    families and can represent refrigerators, counters, cabinet fronts, table
    tops, doors, screens, or other visible planar faces.

    All results are native editable Nuke Roto shapes.
    """
    if source_read is None:
        selected = nuke.selectedNodes("Read")
        source_read = selected[0] if len(selected) == 1 else None
    if source_read is None:
        source_name = guide_source_read_fullname(controller)
        source_read = nuke.toNode(source_name) if source_name else None
    if source_read is None or source_read.Class() != "Read":
        raise RuntimeError("Select a Read node before using Auto Suggest Surfaces.")

    node = ensure_guide_node(source_read, controller)
    frame_info = clamp_guide_frame(
        controller=controller,
        source_read=source_read,
        requested_frame=int(nuke.frame()),
        move_timeline=True,
    )
    node[GUIDE_FRAME_KNOB].setValue(int(frame_info["frame"]))

    width = int(source_read.width())
    height = int(source_read.height())

    # Slightly denser sampling helps local object boundaries.
    nx, ny = 144, 81
    img = _sample_frame_luma(source_read, width, height, nx=nx, ny=ny)

    room = _perspective_surface_suggestion(img, nx, ny)
    objects = _object_plane_suggestions(img, nx, ny, max_candidates=5)

    curves = node["curves"]
    _remove_auto_shapes(curves)

    def px_x(grid_x):
        return float(grid_x) * width / float(nx)

    def px_y(grid_y):
        return float(grid_y) * height / float(ny)

    created = []
    room_created = []
    object_created = []

    left = room["left"]
    right = room["right"]
    horizon = room["horizon"]

    # ------------------------------------------------------------------
    # Room/layout surfaces
    # ------------------------------------------------------------------
    if horizon is not None and (left is not None or right is not None):
        y_left_grid = _line_value(horizon, nx * 0.12)
        y_right_grid = _line_value(horizon, nx * 0.88)

        lx_grid = (
            _line_value(left, 0.5 * (y_left_grid + y_right_grid))
            if left is not None else nx * 0.18
        )
        rx_grid = (
            _line_value(right, 0.5 * (y_left_grid + y_right_grid))
            if right is not None else nx * 0.82
        )

        lx = px_x(max(0.02 * nx, min(0.48 * nx, lx_grid)))
        rx = px_x(max(0.52 * nx, min(0.98 * nx, rx_grid)))
        hy_l = px_y(max(0.12 * ny, min(0.82 * ny, y_left_grid)))
        hy_r = px_y(max(0.12 * ny, min(0.82 * ny, y_right_grid)))

        _create_shape(curves, "AUTO_Floor", [
            (width * 0.02, height * 0.02),
            (width * 0.98, height * 0.02),
            (rx, hy_r),
            (lx, hy_l),
        ])
        created.append("AUTO_Floor")
        room_created.append("AUTO_Floor")

        if left is not None and right is not None:
            top_y = height * 0.97
            left_top_grid = _line_value(left, ny * 0.97)
            right_top_grid = _line_value(right, ny * 0.97)
            left_top = px_x(max(0.0, min(nx, left_top_grid)))
            right_top = px_x(max(0.0, min(nx, right_top_grid)))

            if right_top - left_top > width * 0.12 and rx - lx > width * 0.15:
                _create_shape(curves, "AUTO_BackWall", [
                    (lx, hy_l),
                    (rx, hy_r),
                    (right_top, top_y),
                    (left_top, top_y),
                ])
                created.append("AUTO_BackWall")
                room_created.append("AUTO_BackWall")

                if left["score"] >= 0.15:
                    _create_shape(curves, "AUTO_LeftWall", [
                        (width * 0.02, height * 0.02),
                        (lx, hy_l),
                        (left_top, top_y),
                        (width * 0.02, height * 0.98),
                    ])
                    created.append("AUTO_LeftWall")
                    room_created.append("AUTO_LeftWall")

                if right["score"] >= 0.15:
                    _create_shape(curves, "AUTO_RightWall", [
                        (rx, hy_r),
                        (width * 0.98, height * 0.02),
                        (width * 0.98, height * 0.98),
                        (right_top, top_y),
                    ])
                    created.append("AUTO_RightWall")
                    room_created.append("AUTO_RightWall")

    # ------------------------------------------------------------------
    # Local object/card surfaces
    # ------------------------------------------------------------------
    # Avoid object proposals that are almost identical to an already-created
    # room face. We approximate room boxes using their actual shape bboxes.
    existing_bboxes = []
    for shape in list_shapes(controller):
        try:
            if str(shape.name).startswith("AUTO_") and not str(shape.name).startswith("AUTO_ObjectPlane_"):
                pts = _sample_roto_shape(shape, int(nuke.frame()))
                if pts:
                    xs = [p[0] / width * nx for p in pts]
                    ys = [p[1] / height * ny for p in pts]
                    existing_bboxes.append((min(xs), min(ys), max(xs), max(ys)))
        except Exception:
            pass

    object_index = 1
    for candidate in objects["candidates"]:
        bb = candidate["bbox"]

        if any(_bbox_iou(bb, room_bb) > 0.68 for room_bb in existing_bboxes):
            continue

        points = [
            (px_x(max(0.0, min(nx, x))), px_y(max(0.0, min(ny, y))))
            for x, y in candidate["points"]
        ]

        name = f"AUTO_ObjectPlane_{object_index:02d}"
        _create_shape(curves, name, points)
        created.append(name)
        object_created.append(name)
        object_index += 1

        if object_index > 5:
            break

    try:
        curves.changed()
    except Exception:
        pass

    nuke.show(node)

    return {
        "shape_count": len(created),
        "created": created,
        "room_created": room_created,
        "object_created": object_created,
        "confidence": float(room["confidence"]),
        "reliable_line_count": int(room["reliable_count"]),
        "object_candidate_count": len(objects["candidates"]),
        "message": (
            "Surface proposals created."
            if created
            else "No reliable room or local planar surfaces were detected; draw guides manually or try another frame."
        ),
    }

