from __future__ import annotations
import tempfile
import uuid
import shutil

import json
import re
from pathlib import Path

import nuke
import nuke.rotopaint as rp

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PySide2 import QtCore, QtGui

from .surface_guides import (
    _sample_roto_shape,
    source_frame_range,
)

INTERNAL_MASK_NODE_NAME = "AUTOLENS_TRACK_MASK_INTERNAL"
MASK_LINK_KNOB = "autolens_mask_node"


def _iter_shapes(layer):
    for element in layer:
        if isinstance(element, rp.Shape):
            yield element
        elif isinstance(element, rp.Layer):
            yield from _iter_shapes(element)


def find_mask_node(controller):
    if controller is None or controller.Class() != "Group":
        return None

    node = None
    try:
        controller.begin()
        node = nuke.toNode(INTERNAL_MASK_NODE_NAME)
    except Exception:
        node = None
    finally:
        try:
            controller.end()
        except Exception:
            pass

    return node


def ensure_mask_node(source_read=None, controller=None, show=True):
    if controller is None or controller.Class() != "Group":
        raise RuntimeError(
            "Tracking masks require an AutoLens controller Group."
        )

    node = find_mask_node(controller)

    if node is None:
        try:
            controller.begin()

            node = nuke.nodes.Roto(
                name=INTERNAL_MASK_NODE_NAME
            )
            node["label"].setValue(
                "AUTOLENS TRACK MASK\n"
                "Draw/animate masks around moving objects"
            )
            node["tile_color"].setValue(0x7A344FFF)

            internal_input = (
                nuke.toNode("Source")
                or nuke.toNode("Input1")
            )

            if internal_input is None:
                inputs = nuke.allNodes("Input")
                internal_input = inputs[0] if inputs else None

            if internal_input is not None:
                node.setInput(0, internal_input)

            try:
                node.setXpos(160)
                node.setYpos(260)
            except Exception:
                pass

            if node.knob("autolens_mask_help") is None:
                help_knob = nuke.Text_Knob(
                    "autolens_mask_help",
                    "",
                    "Draw closed Roto shapes around moving actors/objects. "
                    "Animate or track them if they move. Black regions are "
                    "ignored during COLMAP feature extraction."
                )
                node.addKnob(help_knob)

        finally:
            controller.end()

    try:
        if controller.knob(MASK_LINK_KNOB):
            controller[MASK_LINK_KNOB].setValue(
                node.fullName()
            )
    except Exception:
        pass

    if show:
        try:
            nuke.show(node)
        except Exception:
            pass

    return node


def list_mask_shapes(controller):
    node = find_mask_node(controller)
    if node is None:
        return []
    return list(_iter_shapes(node["curves"].rootLayer))


def mask_shape_count(controller):
    return len(list_mask_shapes(controller))


def clear_masks(controller):
    node = find_mask_node(controller)
    if node is None:
        return 0

    curves = node["curves"]
    root = curves.rootLayer

    remove_indices = []
    for i, element in enumerate(root):
        if isinstance(element, rp.Shape):
            remove_indices.append(i)

    for i in reversed(remove_indices):
        root.remove(i)

    try:
        curves.changed()
    except Exception:
        pass

    return len(remove_indices)


def _frame_number_from_name(name: str, fallback: int) -> int:
    stem = Path(name).stem
    match = re.search(r"(\d+)(?!.*\d)", stem)
    if match:
        return int(match.group(1))
    return int(fallback)


def _find_images(folder: Path):
    exts = {
        ".jpg", ".jpeg", ".png", ".tif", ".tiff",
        ".exr", ".bmp", ".webp",
    }
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )


def _image_size(path: Path):
    image = QtGui.QImage(str(path))
    if image.isNull():
        raise RuntimeError(
            f"AutoLens could not read solve image dimensions: {path}"
        )
    return int(image.width()), int(image.height())


def _mask_manifest_payload(
    controller,
    source_read,
    solve_images,
    target_width,
    target_height,
):
    frame_range = source_frame_range(source_read)
    first = frame_range[0] if frame_range else int(nuke.frame())

    frames = []

    for index, image_path in enumerate(solve_images):
        frame = _frame_number_from_name(
            image_path.name,
            first + index,
        )

        shapes = []

        for shape in list_mask_shapes(controller):
            try:
                if not shape.getVisible(float(frame)):
                    continue
            except Exception:
                pass

            points = _sample_roto_shape(
                shape,
                int(frame),
            )

            if len(points) < 3:
                continue

            shapes.append({
                "name": str(shape.name),
                "points": [
                    [
                        round(float(x), 3),
                        round(float(y), 3),
                    ]
                    for x, y in points
                ],
            })

        frames.append({
            "frame": int(frame),
            "image": image_path.name,
            "shapes": shapes,
        })

    return {
        "version": 1,
        "source_width": int(source_read.width()),
        "source_height": int(source_read.height()),
        "source_pixel_aspect": float(
            source_read.format().pixelAspect()
        ),
        "target_width": int(target_width),
        "target_height": int(target_height),
        "frames": frames,
    }


def export_colmap_masks(
    output_folder,
    solve_images_dir,
    source_read,
    controller,
    log=None,
):
    """
    Render white-keep / black-ignore masks matching the exact COLMAP input
    filenames. The resulting folder can be passed to ImageReader.mask_path.
    """
    shapes = list_mask_shapes(controller)

    if not shapes:
        return None

    solve_dir = Path(solve_images_dir)
    solve_images = _find_images(solve_dir)

    if not solve_images:
        raise RuntimeError(
            "AutoLens tracking masks exist, but no solve images were found."
        )

    target_width, target_height = _image_size(
        solve_images[0]
    )

    output = Path(output_folder)
    mask_dir = output / "autolens_tracking_masks"
    mask_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = mask_dir / "manifest.json"

    payload = _mask_manifest_payload(
        controller,
        source_read,
        solve_images,
        target_width,
        target_height,
    )

    old_payload = None
    if manifest_path.exists():
        try:
            old_payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            old_payload = None

    expected_first = mask_dir / (
        solve_images[0].name + ".png"
    )
    expected_last = mask_dir / (
        solve_images[-1].name + ".png"
    )

    if (
        old_payload == payload
        and expected_first.exists()
        and expected_last.exists()
    ):
        if log:
            log(
                "\n[AUTOLENS MASK] Reusing cached tracking masks:\n"
                f"  {mask_dir}\n"
            )
        return mask_dir

    # Remove old rendered masks, but keep directory.
    for old_file in mask_dir.glob("*.png"):
        try:
            old_file.unlink()
        except Exception:
            pass

    source_w = max(float(source_read.width()), 1.0)
    source_h = max(float(source_read.height()), 1.0)

    sx = float(target_width) / source_w
    sy = float(target_height) / source_h

    frame_range = source_frame_range(source_read)
    first_source_frame = (
        frame_range[0]
        if frame_range
        else int(nuke.frame())
    )

    if log:
        log(
            "\n[AUTOLENS MASK] Rendering COLMAP exclusion masks:\n"
            f"  {len(shapes)} mask shape(s)\n"
            f"  {len(solve_images)} frame(s)\n"
            f"  {target_width} x {target_height}\n"
        )

    for index, image_path in enumerate(solve_images):
        frame = _frame_number_from_name(
            image_path.name,
            first_source_frame + index,
        )

        image = QtGui.QImage(
            int(target_width),
            int(target_height),
            QtGui.QImage.Format_RGB32,
        )
        image.fill(QtGui.QColor(255, 255, 255))

        painter = QtGui.QPainter(image)
        painter.setRenderHint(
            QtGui.QPainter.Antialiasing,
            False,
        )
        painter.setBrush(
            QtGui.QBrush(
                QtGui.QColor(0, 0, 0)
            )
        )

        # A few pixels of black outline creates a tiny safety margin around
        # roto edges where actor motion/keypoints are most unstable.
        pen = QtGui.QPen(QtGui.QColor(0, 0, 0))
        pen.setWidth(5)
        painter.setPen(pen)

        for shape in shapes:
            try:
                if not shape.getVisible(float(frame)):
                    continue
            except Exception:
                pass

            points = _sample_roto_shape(
                shape,
                int(frame),
            )

            if len(points) < 3:
                continue

            polygon = QtGui.QPolygonF()

            for x, y in points:
                px = float(x) * sx
                py = (
                    float(target_height)
                    - float(y) * sy
                )
                polygon.append(
                    QtCore.QPointF(px, py)
                )

            painter.drawPolygon(polygon)

        painter.end()

        save_path = mask_dir / (
            image_path.name + ".png"
        )

        if not image.save(str(save_path), "PNG"):
            raise RuntimeError(
                f"AutoLens could not write COLMAP mask: {save_path}"
            )

    manifest_path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    return mask_dir


# =============================================================================
# v0.10.25 — external mask input workflow
# =============================================================================

def connected_mask_node(controller):
    """
    Return the node connected to AutoLens Group input 1.
    """
    if controller is None:
        return None

    try:
        return controller.input(1)
    except Exception:
        return None


def mask_input_connected(controller):
    return connected_mask_node(controller) is not None


def mask_inverted(controller):
    if controller is None:
        return False

    try:
        knob = controller.knob(
            "autolens_mask_invert"
        )
        return bool(
            knob.value()
        ) if knob else False
    except Exception:
        return False


def set_mask_inverted(controller, value):
    if controller is None:
        return

    try:
        knob = controller.knob(
            "autolens_mask_invert"
        )

        if knob is not None:
            knob.setValue(
                bool(value)
            )
    except Exception:
        pass


def _temporary_alpha_expression(
    mask_source,
    *,
    mode="preview",
    invert=False,
):
    """
    Create a temporary Expression node.

    preview:
        red RGBA overlay with 20% opacity wherever source alpha exists.

    colmap:
        grayscale white=track / black=ignore as required by COLMAP.
        Default meaning: alpha = avoid.
        Invert meaning: alpha = keep/track.
    """
    expr = nuke.nodes.Expression()
    expr.setInput(
        0,
        mask_source,
    )

    if mode == "preview":
        expressions = (
            "1",
            "0",
            "0",
            "a*0.2",
        )
    else:
        value = "a" if invert else "1-a"
        expressions = (
            value,
            value,
            value,
            "1",
        )

    for index, expression in enumerate(
        expressions
    ):
        knob = expr.knob(
            f"expr{index}"
        )

        if knob is not None:
            knob.setValue(
                expression
            )

    return expr


def _render_png_sequence(
    input_node,
    output_pattern,
    frames,
    channels="rgba",
):
    write = None

    try:
        write = nuke.nodes.Write()
        write.setInput(
            0,
            input_node,
        )
        write["file"].setValue(
            str(output_pattern).replace(
                "\\",
                "/",
            )
        )

        if write.knob("file_type"):
            write["file_type"].setValue(
                "png"
            )

        if write.knob("channels"):
            write["channels"].setValue(
                channels
            )

        for frame in frames:
            nuke.execute(
                write.name(),
                int(frame),
                int(frame),
            )

    finally:
        if write is not None:
            try:
                nuke.delete(write)
            except Exception:
                pass


def _mask_alpha_channel(node):
    """
    Prefer a conventional alpha channel but tolerate named layers.
    """
    try:
        channels = list(
            node.channels()
        )
    except Exception:
        channels = []

    preferred = [
        "alpha",
        "rgba.alpha",
    ]

    for name in preferred:
        if name in channels:
            return name

    for name in channels:
        if str(name).lower().endswith(
            ".alpha"
        ):
            return str(name)

    return "alpha"


def resolve_sampler(node, channel, frame=None, probe=(0.5, 0.5)):
    """A fast (x, y) -> value sampler using the first sample() form that works.

    Tries the frame-explicit forms first, once, at `probe`, so the per-pixel
    loop doesn't pay for failed calls on every pixel. The returned function's
    `frame_explicit` attribute says whether it samples `frame` itself (True)
    or whatever frame Nuke is on (False).
    """
    sample = getattr(node, "sample", None)
    if sample is None:
        raise RuntimeError(
            "Connected mask node does not expose the Nuke sample() API."
        )
    forms = []
    if frame is not None:
        f = int(frame)
        forms.append((lambda x, y: sample(channel, float(x), float(y), 1, 1, f), True))
        forms.append((lambda x, y: sample(channel, float(x), float(y), frame=f), True))
    forms.append((lambda x, y: sample(channel, float(x), float(y)), False))
    forms.append((lambda x, y: sample(channel, float(x), float(y), 1, 1), False))
    last_error = None
    for form, explicit in forms:
        try:
            float(form(*probe))
        except Exception as exc:
            last_error = exc
            continue
        sampler = lambda x, y, form=form: float(form(x, y))  # noqa: E731
        sampler.frame_explicit = explicit
        return sampler
    raise RuntimeError(
        "Could not sample connected mask alpha."
        + (f" {last_error}" if last_error is not None else "")
    )


def _mask_raster_size(mask_source, controller):
    try:
        width, height = int(mask_source.width()), int(mask_source.height())
    except Exception:
        width = height = 0
    if width <= 0 or height <= 0:
        try:
            width, height = int(controller.width()), int(controller.height())
        except Exception:
            width, height = 1920, 1080
    return width, height


def mask_overlay_job(controller, frame):
    """
    An OverlayJob for `frame` of the connected mask, or None without a mask.

    The job samples the frame explicitly when this Nuke build allows it
    (`job.frame_explicit`). Otherwise it samples Nuke's current frame, so the
    caller must put Nuke on `frame` while the job runs.
    """
    from .mask_cache import OverlayJob

    mask_source = connected_mask_node(controller)
    if mask_source is None:
        return None
    width, height = _mask_raster_size(mask_source, controller)
    sampler = resolve_sampler(
        mask_source,
        _mask_alpha_channel(mask_source),
        frame,
        probe=(width * 0.5, height * 0.5),
    )
    job = OverlayJob(frame, sampler, width, height, inverted=mask_inverted(controller))
    job.frame_explicit = bool(getattr(sampler, "frame_explicit", False))
    return job


def mask_signature(controller):
    """Hash of the mask's upstream graph and the invert setting, or None."""
    from .mask_cache import OVERLAY_WIDTH, graph_signature

    mask_source = connected_mask_node(controller)
    if mask_source is None:
        return None
    return graph_signature(
        mask_source,
        nuke,
        extra=[bool(mask_inverted(controller)), _mask_raster_size(mask_source, controller), OVERLAY_WIDTH],
    )


def overlay_pixmap(job):
    """A QPixmap from a finished OverlayJob."""
    data = job.data()
    image = QtGui.QImage(
        data,
        job.width,
        job.height,
        job.width * 4,
        getattr(QtGui.QImage, "Format_ARGB32_Premultiplied", None)
        or QtGui.QImage.Format.Format_ARGB32_Premultiplied,
    )
    # QImage doesn't own `data`; copy before it goes away.
    return QtGui.QPixmap.fromImage(image.copy())


def render_mask_overlay_pixmap(
    controller,
    frame,
    max_preview_width=None,
):
    """
    Build the viewer mask overlay for one frame now, WITHOUT a Nuke render.

    Red means the area AutoLens will exclude from tracking:
      normal: alpha       -> red
      invert: 1 - alpha   -> red
    The red overlay is 20% opacity at a fully masked pixel. The workspace
    normally fills overlays in idle time (see autolens.mask_cache); this is
    the one-shot version.
    """
    job = mask_overlay_job(controller, frame)
    if job is None:
        return QtGui.QPixmap()

    old_frame = int(nuke.frame())
    moved = False
    try:
        if not job.frame_explicit and old_frame != int(frame):
            nuke.frame(int(frame))
            moved = True
        while not job.step(1.0):
            pass
    finally:
        if moved:
            try:
                nuke.frame(old_frame)
            except Exception:
                pass

    return overlay_pixmap(job)


def export_input_alpha_masks(
    output_folder,
    solve_images_folder,
    source_read,
    controller,
    log=None,
):
    """
    Convert AutoLens input-1 alpha into COLMAP masks.

    COLMAP convention:
        white = allow features
        black = ignore features

    Default AutoLens convention:
        connected alpha = avoid / mask out

    Invert:
        connected alpha = keep / track
    """
    mask_source = connected_mask_node(
        controller
    )

    if mask_source is None:
        return None

    solve_folder = Path(
        solve_images_folder
    )

    solve_images = _find_images(
        solve_folder
    )

    if not solve_images:
        return None

    target_width, target_height = _image_size(
        solve_images[0]
    )

    frame_range = source_frame_range(
        source_read
    )
    first_source_frame = (
        frame_range[0]
        if frame_range
        else int(nuke.frame())
    )

    frames = []

    for index, image_path in enumerate(
        solve_images
    ):
        frames.append(
            _frame_number_from_name(
                image_path.name,
                first_source_frame + index,
            )
        )

    invert = mask_inverted(
        controller
    )

    mask_dir = (
        Path(output_folder)
        / "tracking_masks"
    )
    mask_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if log:
        meaning = (
            "alpha = KEEP/TRACK"
            if invert
            else "alpha = AVOID"
        )
        log(
            "\n[AUTOLENS MASK] Rendering connected alpha mask:\n"
            f"  Source: {mask_source.fullName()}\n"
            f"  Meaning: {meaning}\n"
            f"  {len(frames)} frame(s)\n"
        )

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="autolens_mask_input_"
        )
    )

    expr = None

    try:
        expr = _temporary_alpha_expression(
            mask_source,
            mode="colmap",
            invert=invert,
        )

        pattern = (
            temp_dir
            / "mask.%06d.png"
        )

        _render_png_sequence(
            expr,
            pattern,
            frames,
            channels="rgb",
        )

        for frame, solve_image in zip(
            frames,
            solve_images,
        ):
            rendered = (
                temp_dir
                / f"mask.{int(frame):06d}.png"
            )

            image = QtGui.QImage(
                str(rendered)
            )

            if image.isNull():
                raise RuntimeError(
                    "AutoLens could not read rendered input alpha "
                    f"for frame {frame}."
                )

            if (
                image.width() != target_width
                or image.height() != target_height
            ):
                image = image.scaled(
                    int(target_width),
                    int(target_height),
                    QtCore.Qt.IgnoreAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )

            save_path = (
                mask_dir
                / (
                    solve_image.name
                    + ".png"
                )
            )

            if not image.save(
                str(save_path),
                "PNG",
            ):
                raise RuntimeError(
                    f"Could not write COLMAP mask: {save_path}"
                )

        manifest = {
            "version": "0.10.26",
            "source_node": mask_source.fullName(),
            "invert": bool(invert),
            "meaning": (
                "alpha_keep"
                if invert
                else "alpha_avoid"
            ),
            "frame_count": len(frames),
            "width": int(target_width),
            "height": int(target_height),
        }

        (
            mask_dir
            / "manifest.json"
        ).write_text(
            json.dumps(
                manifest,
                indent=2,
            ),
            encoding="utf-8",
        )

        return mask_dir

    finally:
        if expr is not None:
            try:
                nuke.delete(expr)
            except Exception:
                pass

        shutil.rmtree(
            temp_dir,
            ignore_errors=True,
        )
