from __future__ import annotations

import os
import tempfile
import json
import re
from pathlib import Path

import nuke


def _strip_root_block(nk_text: str) -> str:
    start = nk_text.find("Root {")
    if start < 0:
        return nk_text

    depth = 0
    in_block = False
    end = None
    for i in range(start, len(nk_text)):
        ch = nk_text[i]
        if ch == "{":
            depth += 1
            in_block = True
        elif ch == "}" and in_block:
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        return nk_text
    return nk_text[:start] + nk_text[end:]



def _sanitize_lensdistortion_text(nk_text: str) -> str:
    """
    Make AutoLens result NK files portable across recent Nuke versions.

    Nuke's LensDistortion node changed some enum scripting details:
      - older scripts used: lensType Spherical
      - current versions expose: lens
      - older docs/scripts accepted: projection Rectilinear
      - current versions use None as the rectilinear projection model

    Both spherical lens type and rectilinear projection are defaults, so the
    most robust import is to omit those version-sensitive settings entirely.
    """
    output_lines = []

    for line in nk_text.splitlines():
        stripped = line.strip()

        if stripped == "lensType Spherical":
            continue

        if stripped == "projection Rectilinear":
            continue

        output_lines.append(line)

    return "\n".join(output_lines) + ("\n" if nk_text.endswith("\n") else "")



def _replace_float_knob(nk_text: str, knob: str, value: float) -> str:
    pattern = re.compile(
        rf"(?m)^(\s*{re.escape(knob)}\s+)"
        r"[-+0-9.eE]+(\s*)$"
    )
    return pattern.sub(
        lambda m: f"{m.group(1)}{value:.12g}{m.group(2)}",
        nk_text,
    )


def _replace_centre_knob(nk_text: str, x: float, y: float) -> str:
    pattern = re.compile(
        r"(?m)^(\s*centre\s+\{)"
        r"[-+0-9.eE]+\s+[-+0-9.eE]+"
        r"(\}\s*)$"
    )
    return pattern.sub(
        lambda m: f"{m.group(1)}{x:.12g} {y:.12g}{m.group(2)}",
        nk_text,
    )


def _migrate_legacy_nuke_normalisation(
    nk_text: str,
    nk_file: Path,
) -> str:
    """
    Correct completed v0.9.8/v0.9.9 Nuke lens nodes without rerunning solve.

    Old normalization: max(width,height)
    Correct Nuke Maximum normalization: max(width,height)/2

    Therefore:
      r^2 coefficients -> /4
      r^4 coefficients -> /16
      center offsets   -> x2
    """
    model_path = nk_file.parent / "autolens_lens_model.json"
    if not model_path.exists():
        return nk_text

    try:
        model = json.loads(
            model_path.read_text(encoding="utf-8", errors="replace")
        )
    except Exception:
        return nk_text

    if model.get("normalisation_convention") == "max(width,height)/2":
        return nk_text

    if float(model.get("normalisation_scale_px") or 0.0) <= 0.0:
        return nk_text

    numerator = list(model.get("numerator") or [])
    denominator = list(model.get("denominator") or [])
    centre = list(model.get("centre") or [0.0, 0.0])

    if len(numerator) < 2 or len(denominator) < 2:
        return nk_text

    n0 = float(numerator[0]) / 4.0
    n1 = float(numerator[1]) / 16.0
    d0 = float(denominator[0]) / 4.0
    d1 = float(denominator[1]) / 16.0
    cx = float(centre[0]) * 2.0
    cy = float(centre[1]) * 2.0

    nk_text = _replace_float_knob(nk_text, "distortionNumerator0", n0)
    nk_text = _replace_float_knob(nk_text, "distortionNumerator1", n1)
    nk_text = _replace_float_knob(nk_text, "distortionDenominator0", d0)
    nk_text = _replace_float_knob(nk_text, "distortionDenominator1", d1)
    nk_text = _replace_centre_knob(nk_text, cx, cy)

    return nk_text


def _upgrade_tracking_space_labels(nk_text: str) -> str:
    nk_text = nk_text.replace(
        "PROJECT_THIS_IMAGE",
        "AUTOLENS_TRACKING_PLATE",
    )
    nk_text = nk_text.replace(
        "PROJECT THIS IMAGE\\\\nwith Project3D",
        "UNDISTORTED TRACKING PLATE\\\\nTRACK / PROJECT / COMP HERE",
    )
    nk_text = nk_text.replace(
        "COLMAP solved camera\\\\nUse this for Project3D",
        "UNDISTORTED TRACKING CAMERA\\\\nUse with AUTOLENS_TRACKING_PLATE",
    )
    return nk_text



def _is_named(node, base_name):
    """True when a node is named base_name, or base_name plus Nuke's numeric suffix.

    A plain prefix match is not enough: AUTOLENS_REDISTORT would also match the
    AUTOLENS_REDISTORT_INPUT Dot.
    """
    try:
        name = node.name()
    except Exception:
        return False
    return re.fullmatch(re.escape(base_name) + r"\d*", name) is not None


def _find_pasted(pasted, prefix):
    """Find a pasted node by AutoLens base-name, including Nuke numeric suffixes."""
    matches = [
        node for node in pasted
        if _is_named(node, prefix)
    ]
    return matches[0] if matches else None


def _delete_from_pasted(pasted, node):
    if node is None:
        return
    try:
        nuke.delete(node)
    except Exception:
        return
    try:
        pasted.remove(node)
    except ValueError:
        pass


def _set_xy(node, x, y):
    if node is None:
        return
    try:
        node.setXpos(int(x))
        node.setYpos(int(y))
    except Exception:
        pass


def _set_label(node, text):
    if node is None:
        return
    try:
        if node.knob("label"):
            node["label"].setValue(str(text))
    except Exception:
        pass


def _safe_set_input(node, index, source):
    if node is None:
        return False
    try:
        if source is None:
            node.setInput(index, None)
            return True
        if node.canSetInput(index, source):
            node.setInput(index, source)
            return True
    except Exception:
        pass
    return False


def _ensure_template_node(pasted, prefix, creator):
    node = _find_pasted(pasted, prefix)
    if node is not None:
        return node

    node = creator()
    pasted.append(node)
    return node


def _controller_and_guide(controller_fullname):
    controller = (
        nuke.toNode(controller_fullname)
        if controller_fullname
        else None
    )
    guide = None

    if controller is not None:
        try:
            knob = controller.knob("autolens_guide_node")
            if knob:
                guide_name = knob.value().strip()
                if guide_name:
                    guide = nuke.toNode(guide_name)
        except Exception:
            pass

    return controller, guide


def _ensure_clean_render_template(
    pasted,
    source,
    controller_fullname=None,
):
    """
    Build the compact result graph used by AutoLens imports.

    The template deliberately keeps the original distorted Read at the top and
    makes the UNDISTORTED plate the main VFX working pipe:

        Read
          |
        Undistort
          |
        Tracking Plate ------------.
          |                         |
          |                    Scene/Scanline
          |                         |
          '---------- Merge <-------'
                       |
                   Redistort
                       |
                     Final

    Only one geometry representation is connected to the Scene by default:
      Guided Surfaces > Mesh-fit Planes > Proxy Mesh.

    The other meshes stay immediately adjacent as alternates.
    """
    # Remove older visual-only clutter from prior exports.
    for prefix in (
        "AUTOLENS_TRACKING_SPACE_BACKDROP",
        "AutoLens_Project3D_Help",
        "AutoLens_Info",
        "AUTOLENS_PROPAGATED_DEPTH",
        "AUTOLENS_DEPTH_FALLBACK",
        "AUTOLENS_DEPTH_COPY",
    ):
        node = _find_pasted(pasted, prefix)
        _delete_from_pasted(pasted, node)

    undistort = _find_pasted(pasted, "AUTOLENS_UNDISTORT")
    tracking = _find_pasted(pasted, "AUTOLENS_TRACKING_PLATE")
    redistort_input = _find_pasted(pasted, "AUTOLENS_REDISTORT_INPUT")
    redistort = _find_pasted(pasted, "AUTOLENS_REDISTORT")

    proxy = _find_pasted(pasted, "AUTOLENS_PROXY_MESH")
    meshfit = _find_pasted(pasted, "AUTOLENS_MESHFIT_PLANES")
    guided = _find_pasted(pasted, "AUTOLENS_GUIDED_SURFACES")
    camera = _find_pasted(pasted, "AUTOLENS_CAMERA")

    # Complete render/composite template.
    scene = _ensure_template_node(
        pasted,
        "AUTOLENS_SCENE",
        lambda: nuke.nodes.Scene(
            name="AUTOLENS_SCENE",
        ),
    )

    scanline = _ensure_template_node(
        pasted,
        "AUTOLENS_SCANLINE",
        lambda: nuke.nodes.ScanlineRender(
            name="AUTOLENS_SCANLINE",
        ),
    )

    merge = _ensure_template_node(
        pasted,
        "AUTOLENS_MERGE",
        lambda: nuke.nodes.Merge2(
            name="AUTOLENS_MERGE",
            operation="over",
        ),
    )

    try:
        if merge.knob("operation"):
            merge["operation"].setValue("over")
    except Exception:
        pass

    # v0.10.6: keep the imported graph cleaner by removing the visual helper
    # dots. The actual pipe runs directly Merge -> Redistort.
    if redistort_input is not None:
        _delete_from_pasted(pasted, redistort_input)
        redistort_input = None

    final_dot = _find_pasted(pasted, "AUTOLENS_FINAL")
    if final_dot is not None:
        _delete_from_pasted(pasted, final_dot)
        final_dot = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    if source is not None and undistort is not None:
        _safe_set_input(undistort, 0, source)

    if tracking is not None and undistort is not None:
        _safe_set_input(tracking, 0, undistort)

    # Pick one sensible default geometry. The alternates remain nearby.
    preferred_geo = guided or meshfit or proxy

    # Clear a few Scene inputs before rebuilding the small template.
    for index in range(0, 6):
        try:
            scene.setInput(index, None)
        except Exception:
            pass

    if preferred_geo is not None:
        _safe_set_input(scene, 0, preferred_geo)

    # Put the render camera in the Scene as well so it is available in the
    # 3D Viewer, but ScanlineRender gets an explicit camera connection too.
    if camera is not None:
        _safe_set_input(scene, 1, camera)

    # Classic ScanlineRender indices:
    #   0 = bg, 1 = obj/scn, 2 = cam
    _safe_set_input(scanline, 0, None)
    _safe_set_input(scanline, 1, scene)
    _safe_set_input(scanline, 2, camera)

    # Merge2's B/main pipe is input 0; A/foreground is input 1.
    _safe_set_input(merge, 0, tracking)
    _safe_set_input(merge, 1, scanline)

    if redistort is not None:
        _safe_set_input(redistort, 0, merge)

    # ------------------------------------------------------------------
    # Compact labels
    # ------------------------------------------------------------------
    _set_label(
        undistort,
        "UNDISTORT\nDISTORTED -> VFX SPACE",
    )
    _set_label(
        tracking,
        "UNDISTORTED PLATE\nTRACK / PROJECT / COMP",
    )

    # Keep these working nodes visually clean. Their node names already
    # identify them, so extra labels just add clutter.
    _set_label(proxy, "")
    _set_label(meshfit, "")
    _set_label(guided, "")

    _set_label(scene, "")
    _set_label(camera, "SOLVED CAMERA")
    _set_label(scanline, "")
    _set_label(merge, "")
    _set_label(redistort, "REDISTORT\nVFX SPACE -> DELIVERY")

    # ------------------------------------------------------------------
    # Layout — modeled after the user's preferred compact graph.
    # Anchor everything to the existing source Read rather than to (0,0).
    # ------------------------------------------------------------------
    anchor = source or undistort or tracking
    if anchor is not None:
        sx = int(anchor.xpos())
        sy = int(anchor.ypos())
    else:
        sx = 0
        sy = 0

    controller, guide = _controller_and_guide(controller_fullname)

    # Tool-control nodes sit neatly beside the source.
    if controller is not None:
        _set_xy(controller, sx + 145, sy + 105)
    if guide is not None:
        _set_xy(guide, sx + 300, sy + 105)

    # Main image pipe.
    _set_xy(undistort, sx, sy + 235)
    _set_xy(tracking, sx + 34, sy + 350)

    # Geometry shelf on the left.
    geo_x = sx - 430
    geo_y = sy + 355
    geo_step = 105

    ordered_geos = [
        node for node in (proxy, meshfit, guided)
        if node is not None
    ]

    for index, node in enumerate(ordered_geos):
        _set_xy(node, geo_x, geo_y + index * geo_step)

    # 3D render branch.
    _set_xy(scene, sx - 145, sy + 500)
    _set_xy(camera, sx - 305, sy + 655)
    _set_xy(scanline, sx - 145, sy + 655)

    # Composite / delivery pipe.
    _set_xy(merge, sx, sy + 760)
    _set_xy(redistort, sx, sy + 860)

    return pasted


def import_result(
    nk_path: str,
    source_read_fullname: str | None = None,
    controller_fullname: str | None = None,
):
    nk_file = Path(nk_path)
    if not nk_file.exists():
        raise RuntimeError(f"AutoLens Nuke script not found:\n{nk_file}")

    content = nk_file.read_text(encoding="utf-8", errors="replace")
    content = _strip_root_block(content)
    content = _sanitize_lensdistortion_text(content)
    content = _migrate_legacy_nuke_normalisation(content, nk_file)
    content = _upgrade_tracking_space_labels(content)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".nk", prefix="autolens_import_",
            delete=False, encoding="utf-8"
        ) as temp:
            temp.write(content)
            temp_path = temp.name

        for node in nuke.selectedNodes():
            node.setSelected(False)

        nuke.nodePaste(temp_path)
        pasted = list(nuke.selectedNodes())

        source = nuke.toNode(source_read_fullname) if source_read_fullname else None
        if source is not None and source.Class() == "Read":
            for node in pasted:
                if (
                    _is_named(node, "AUTOLENS_UNDISTORT")
                    and node.Class() in ("LensDistortion2", "LensDistortion")
                ):
                    node.setInput(0, source)

            for node in list(pasted):
                if node.Class() == "Read" and _is_named(node, "PLATE_REPOINT_TO_SEQUENCE"):
                    try:
                        nuke.delete(node)
                        pasted.remove(node)
                    except Exception:
                        pass

        pasted = _ensure_clean_render_template(
            pasted,
            source,
            controller_fullname=controller_fullname,
        )

        # Leave the newly imported result selected as one clean group.
        try:
            for node in nuke.selectedNodes():
                node.setSelected(False)
            for node in pasted:
                try:
                    node.setSelected(True)
                except Exception:
                    pass
        except Exception:
            pass

        return pasted
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
