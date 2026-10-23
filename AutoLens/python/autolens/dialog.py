from __future__ import annotations

import os
import time
from pathlib import Path
import re

import nuke

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal

from . import __version__, icons, theme, ux
from .frame_cache import (
    PlaybackClock,
    bucket_width,
    cache_budget_bytes,
    clamp_frame,
    contiguous_runs,
    nearest_cached,
    playback_step,
    prefetch_order,
    step_frame,
)
from .frame_decode import WORKER_EXTS, transfer_for
from .frame_server import FrameServerPool, managed_python
from .plate_reader import ReadSnapshot, choose_pattern, upstream_note
from .preview_loader import (
    PreviewLoader,
    decode_image,
    supported_extensions,
    worker_decoder,
)
from .build import version_text
from .startup import defer_initial_preview, timed
from .ux import timeline_tick_step as tick_step
from .panel import AutoLensPanel
from .node import source_read_for_node, set_status
from .surface_guides import (
    auto_suggest_surfaces,
    ensure_guide_node,
    find_guide_node,
    replace_surface_guides,
    surface_guides_data,
    surface_guide_frames,
    set_surface_guide_frame,
    clamp_frame_to_source,
    source_frame_range,
)
from .tracking_masks import (
    mask_input_connected,
    mask_inverted,
    set_mask_inverted,
    render_mask_overlay_pixmap,
)

_DIALOGS = {}


LEFT_BUTTON = getattr(
    QtCore.Qt,
    "LeftButton",
    getattr(getattr(QtCore.Qt, "MouseButton", object()), "LeftButton", 1),
)
MIDDLE_BUTTON = getattr(
    QtCore.Qt,
    "MiddleButton",
    getattr(getattr(QtCore.Qt, "MouseButton", object()), "MiddleButton", 4),
)
NO_BRUSH = getattr(
    QtCore.Qt,
    "NoBrush",
    getattr(getattr(QtCore.Qt, "BrushStyle", object()), "NoBrush", 0),
)
ALIGN_CENTER = getattr(
    QtCore.Qt,
    "AlignCenter",
    getattr(getattr(QtCore.Qt, "AlignmentFlag", object()), "AlignCenter", 0),
)


NO_FOCUS = getattr(
    QtCore.Qt,
    "NoFocus",
    getattr(getattr(QtCore.Qt, "FocusPolicy", object()), "NoFocus", 0),
)
CLICK_FOCUS = getattr(
    QtCore.Qt,
    "ClickFocus",
    getattr(getattr(QtCore.Qt, "FocusPolicy", object()), "ClickFocus", 2),
)
PRECISE_TIMER = getattr(
    QtCore.Qt,
    "PreciseTimer",
    getattr(getattr(QtCore.Qt, "TimerType", object()), "PreciseTimer", 0),
)

# Qt 5 (PySide2, Nuke 14/15) has QShortcut in QtWidgets; Qt 6 moved it to QtGui.
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut


WIDGET_WITH_CHILDREN_SHORTCUT = getattr(
    QtCore.Qt,
    "WidgetWithChildrenShortcut",
    getattr(
        getattr(QtCore.Qt, "ShortcutContext", object()),
        "WidgetWithChildrenShortcut",
        3,
    ),
)


_HORIZONTAL = getattr(
    QtCore.Qt,
    "Horizontal",
    getattr(getattr(QtCore.Qt, "Orientation", object()), "Horizontal", 1),
)
_ALIGN_RIGHT = getattr(
    QtCore.Qt,
    "AlignRight",
    getattr(getattr(QtCore.Qt, "AlignmentFlag", object()), "AlignRight", 2),
) | getattr(
    QtCore.Qt,
    "AlignVCenter",
    getattr(getattr(QtCore.Qt, "AlignmentFlag", object()), "AlignVCenter", 0x80),
)
_NO_BUTTONS = getattr(
    QtWidgets.QAbstractSpinBox,
    "NoButtons",
    getattr(getattr(QtWidgets.QAbstractSpinBox, "ButtonSymbols", object()), "NoButtons", 2),
)
_SIZE_IGNORED = getattr(
    QtWidgets.QSizePolicy,
    "Ignored",
    getattr(getattr(QtWidgets.QSizePolicy, "Policy", object()), "Ignored", 13),
)
_SIZE_PREFERRED = getattr(
    QtWidgets.QSizePolicy,
    "Preferred",
    getattr(getattr(QtWidgets.QSizePolicy, "Policy", object()), "Preferred", 5),
)

_STEP_TIPS = {
    "track": "Track: solve the camera and lens from the plate (mask moving objects first)",
    "draw": "Draw: optional planes over flat surfaces, each with its own guide frame",
    "output": "Output: Create All, single assets, and geometry status",
}


# Small builders for the Match-style workspace.
def _frame(name):
    f = QtWidgets.QFrame()
    f.setObjectName(name)
    return f


def _hbox(parent, margins=(12, 0, 12, 0), spacing=6):
    lay = QtWidgets.QHBoxLayout(parent)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def _vbox(parent, margins=(12, 10, 12, 10), spacing=8):
    lay = QtWidgets.QVBoxLayout(parent)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


def _label(text, name=None):
    lab = QtWidgets.QLabel(text)
    if name:
        lab.setObjectName(name)
    return lab


def _wrap(label):
    label.setWordWrap(True)
    return label


def _elide(label):
    # Long text is cut off instead of widening the window.
    label.setSizePolicy(_SIZE_IGNORED, _SIZE_PREFERRED)
    return label


def _separator():
    s = _frame("Separator")
    s.setFixedSize(1, 22)
    return s


def _icon_button(icon_name, tip, name="Tool", colour=None):
    b = QtWidgets.QPushButton()
    b.setObjectName(name)
    b.setAutoDefault(False)
    b.setIcon(icons.icon(icon_name, colour) if colour else icons.icon(icon_name))
    b.setIconSize(icons.icon_size())
    b.setToolTip(tip)
    return b


def _menu_button(text):
    b = QtWidgets.QPushButton(text)
    b.setObjectName("MenuBarButton")
    b.setAutoDefault(False)
    b.setFocusPolicy(NO_FOCUS)
    m = QtWidgets.QMenu(b)
    b.setMenu(m)
    return b, m


def _action(menu, text, slot, key=None, checkable=False, tip=""):
    """Menu entry. `key` is only shown; the window's shortcuts do the work."""
    a = menu.addAction(text + ("\t" + key if key else ""))
    a.setCheckable(checkable)
    if tip:
        a.setToolTip(tip)
        a.setStatusTip(tip)
    a.triggered.connect(lambda *_: slot())
    return a


def _action_group(parent):
    cls = getattr(QtWidgets, "QActionGroup", None) or QtGui.QActionGroup
    group = cls(parent)
    group.setExclusive(True)
    return group


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _active_window():
    app = QtWidgets.QApplication.instance()
    return app.activeWindow() if app else None


def _surface_color(
    name,
    selected=False,
    on_guide_frame=True,
):
    """
    Guide-frame state is the primary visual cue:
      green      = this surface's actual guide frame
      gray/white = viewing the surface from another frame
    """
    if on_guide_frame:
        base = QtGui.QColor(
            105,
            225,
            120,
        )
        if selected:
            return base.lighter(125)
        return base

    base = QtGui.QColor(
        205,
        205,
        205,
    )
    if selected:
        return QtGui.QColor(
            235,
            235,
            235,
        )
    return base


class SurfaceCanvas(QtWidgets.QWidget):
    """Small planar-guide editor embedded directly in the AutoLens window."""

    surfacesChanged = Signal(list)
    selectionChanged = Signal(int)
    zoomChanged = Signal(float)
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setMouseTracking(True)
        # Click focus so arrow keys reach the dialog's frame stepping.
        self.setFocusPolicy(CLICK_FOCUS)
        self.setMinimumSize(600, 420)

        self.pixmap = QtGui.QPixmap()
        self.image_width = 1
        self.image_height = 1
        self.pixel_aspect = 1.0

        self.surfaces = []
        self.selected_surface = -1
        self.drag_point = -1
        self.current_frame = int(nuke.frame())

        self.mask_overlay = QtGui.QPixmap()
        self.show_mask_overlay = False
        self.show_surfaces = True

        self.zoom = 1.0
        self.pan = QtCore.QPointF(0.0, 0.0)
        self._panning = False
        self._pan_anchor = None

        self.setStyleSheet("background: #151515;")

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def set_image(self, pixmap, width, height, pixel_aspect=1.0):
        # `pixmap` may be a viewer-sized proxy; width/height are the plate's
        # real size, which surface coordinates use.
        self.pixmap = pixmap or QtGui.QPixmap()
        width = max(1, int(width))
        height = max(1, int(height))
        try:
            pa = float(pixel_aspect)
        except Exception:
            pa = 1.0
        pa = pa if pa > 0.0 else 1.0

        # Keep zoom/pan while stepping or playing through the same plate.
        if (width, height, pa) != (
            self.image_width,
            self.image_height,
            self.pixel_aspect,
        ):
            self.zoom = 1.0
            self.pan = QtCore.QPointF(0.0, 0.0)

        self.image_width = width
        self.image_height = height
        self.pixel_aspect = pa
        self.update()

    def set_surfaces(
        self,
        surfaces,
        selected=None,
        current_frame=None,
    ):
        if current_frame is not None:
            self.current_frame = int(
                current_frame
            )

        copied = []
        for surface in surfaces or []:
            guide_frame = surface.get(
                "guide_frame",
                self.current_frame,
            )
            try:
                guide_frame = int(
                    guide_frame
                )
            except Exception:
                guide_frame = int(
                    self.current_frame
                )

            copied.append({
                "name": str(
                    surface.get("name")
                    or "Surface"
                ),
                "points": [
                    (
                        float(p[0]),
                        float(p[1]),
                    )
                    for p in surface.get(
                        "points",
                        [],
                    )
                ],
                "guide_frame": guide_frame,
                "is_guide_frame": (
                    int(self.current_frame)
                    == guide_frame
                ),
            })

        self.surfaces = copied

        if selected is None:
            if self.selected_surface >= len(self.surfaces):
                self.selected_surface = -1
        else:
            self.selected_surface = int(selected)

        self.update()

    def set_current_frame(self, frame):
        self.current_frame = int(frame)

        for surface in self.surfaces:
            try:
                surface["is_guide_frame"] = (
                    int(
                        surface.get(
                            "guide_frame",
                            self.current_frame,
                        )
                    )
                    == self.current_frame
                )
            except Exception:
                surface["is_guide_frame"] = True

        self.update()

    def set_mask_overlay(self, pixmap, visible=True):
        self.mask_overlay = pixmap or QtGui.QPixmap()
        self.show_mask_overlay = bool(visible)
        self.update()

    def clear_mask_overlay(self):
        self.mask_overlay = QtGui.QPixmap()
        self.show_mask_overlay = False
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def fit_view(self):
        self.zoom = 1.0
        self.pan = QtCore.QPointF(0.0, 0.0)
        self.update()

    # ------------------------------------------------------------------
    # Coordinate transforms
    # ------------------------------------------------------------------
    def _base_scale(self):
        margin = 20.0
        available_w = max(1.0, self.width() - margin * 2.0)
        available_h = max(1.0, self.height() - margin * 2.0)

        display_width = float(self.image_width) * self.pixel_aspect
        return min(
            available_w / max(display_width, 1.0),
            available_h / float(self.image_height),
        )

    def _image_rect(self):
        scale = self._base_scale() * self.zoom
        w = self.image_width * self.pixel_aspect * scale
        h = self.image_height * scale

        left = (self.width() - w) * 0.5 + self.pan.x()
        top = (self.height() - h) * 0.5 + self.pan.y()

        return QtCore.QRectF(left, top, w, h), scale

    def _to_widget(self, point):
        rect, scale = self._image_rect()
        x, y = point

        # Nuke surface coordinates are bottom-left.
        return QtCore.QPointF(
            rect.left() + x * self.pixel_aspect * scale,
            rect.top() + (self.image_height - y) * scale,
        )

    def _to_image(self, point):
        rect, scale = self._image_rect()

        x = (point.x() - rect.left()) / max(scale * self.pixel_aspect, 1e-8)
        y_top = (point.y() - rect.top()) / max(scale, 1e-8)
        y = self.image_height - y_top

        x = min(max(x, 0.0), float(self.image_width))
        y = min(max(y, 0.0), float(self.image_height))
        return (x, y)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QtGui.QColor(theme.BG_0))

        image_rect, _ = self._image_rect()

        if not self.pixmap.isNull():
            painter.drawPixmap(image_rect, self.pixmap, self.pixmap.rect())
        else:
            painter.setPen(QtGui.QColor(145, 145, 145))
            painter.drawText(self.rect(), ALIGN_CENTER, "No preview frame")

        if (
            self.show_mask_overlay
            and not self.mask_overlay.isNull()
        ):
            painter.drawPixmap(
                image_rect,
                self.mask_overlay,
                self.mask_overlay.rect(),
            )

        painter.save()
        painter.setClipRect(image_rect)

        if self.show_surfaces:
            for index, surface in enumerate(self.surfaces):
                points = surface.get("points") or []
                if len(points) < 3:
                    continue

                selected = index == self.selected_surface
                on_guide_frame = bool(
                    surface.get(
                        "is_guide_frame",
                        int(
                            surface.get(
                                "guide_frame",
                                self.current_frame,
                            )
                        )
                        == int(self.current_frame),
                    )
                )

                color = _surface_color(
                    surface.get("name"),
                    selected,
                    on_guide_frame,
                )

                polygon = QtGui.QPolygonF([
                    self._to_widget(point)
                    for point in points
                ])

                # Keep the plate readable. Active guide frames are slightly
                # stronger; off-guide frames become a subtle gray reference.
                fill = QtGui.QColor(color)
                if on_guide_frame:
                    fill.setAlpha(
                        26 if selected else 18
                    )
                else:
                    fill.setAlpha(
                        15 if selected else 9
                    )

                painter.setBrush(
                    QtGui.QBrush(fill)
                )

                pen_color = QtGui.QColor(color)
                pen_color.setAlpha(
                    235
                    if on_guide_frame
                    else 155
                )

                pen = QtGui.QPen(
                    pen_color
                )
                pen.setWidth(
                    2 if not selected else 3
                )
                painter.setPen(pen)
                painter.drawPolygon(polygon)

                # Surface name + its own guide frame.
                bounds = polygon.boundingRect()
                text_rect = QtCore.QRectF(
                    bounds.left() + 6,
                    bounds.top() + 4,
                    max(
                        90.0,
                        bounds.width() - 12,
                    ),
                    22,
                )

                text_color = QtGui.QColor(
                    color
                )
                text_color.setAlpha(
                    235
                    if on_guide_frame
                    else 175
                )
                painter.setPen(
                    text_color
                )

                name = str(
                    surface.get("name")
                )
                guide_frame = surface.get(
                    "guide_frame"
                )
                if guide_frame is not None:
                    name = (
                        f"{name}  F"
                        f"{int(guide_frame)}"
                    )

                painter.drawText(
                    text_rect,
                    name,
                )

                if selected:
                    for point_index, point in enumerate(points):
                        q = self._to_widget(point)
                        radius = 6.0
                        painter.setBrush(
                            QtGui.QBrush(
                                QtGui.QColor(
                                    18,
                                    18,
                                    18,
                                    185,
                                )
                            )
                        )
                        handle_pen = QtGui.QPen(
                            pen_color
                        )
                        handle_pen.setWidth(2)
                        painter.setPen(
                            handle_pen
                        )
                        painter.drawEllipse(
                            q,
                            radius,
                            radius,
                        )

        painter.restore()

        # Viewer border.
        border = QtGui.QPen(QtGui.QColor(75, 75, 75))
        border.setWidth(1)
        painter.setPen(border)
        painter.setBrush(NO_BRUSH)
        painter.drawRect(image_rect)

    # ------------------------------------------------------------------
    # Selection / editing
    # ------------------------------------------------------------------
    def _nearest_handle(self, pos):
        best = None
        best_distance = 12.0

        indices = range(len(self.surfaces))
        if self.selected_surface >= 0:
            # Prefer handles of the currently selected surface.
            indices = [self.selected_surface] + [
                i for i in indices if i != self.selected_surface
            ]

        for surface_index in indices:
            points = self.surfaces[surface_index].get("points") or []
            for point_index, point in enumerate(points):
                q = self._to_widget(point)
                distance = ((q.x() - pos.x()) ** 2 + (q.y() - pos.y()) ** 2) ** 0.5
                if distance < best_distance:
                    best_distance = distance
                    best = (surface_index, point_index)

        return best

    def _surface_at(self, pos):
        for index in reversed(range(len(self.surfaces))):
            points = self.surfaces[index].get("points") or []
            if len(points) < 3:
                continue
            polygon = QtGui.QPolygonF([
                self._to_widget(point)
                for point in points
            ])
            if polygon.containsPoint(pos, QtCore.Qt.OddEvenFill):
                return index
        return -1

    def mousePressEvent(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()

        if event.button() == MIDDLE_BUTTON:
            self._panning = True
            self._pan_anchor = QtCore.QPointF(pos)
            return

        if event.button() != LEFT_BUTTON:
            return

        nearest = self._nearest_handle(pos)
        if nearest is not None:
            surface_index, point_index = nearest
            self.selected_surface = surface_index
            self.drag_point = point_index
            self.selectionChanged.emit(surface_index)
            self.update()
            return

        index = self._surface_at(pos)
        self.selected_surface = index
        self.drag_point = -1
        self.selectionChanged.emit(index)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()

        if self._panning and self._pan_anchor is not None:
            delta = QtCore.QPointF(pos) - self._pan_anchor
            self.pan += delta
            self._pan_anchor = QtCore.QPointF(pos)
            self.update()
            return

        if self.selected_surface < 0 or self.drag_point < 0:
            return

        point = self._to_image(pos)
        self.surfaces[self.selected_surface]["points"][self.drag_point] = point
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == MIDDLE_BUTTON:
            self._panning = False
            self._pan_anchor = None
            return

        if event.button() == LEFT_BUTTON and self.drag_point >= 0:
            self.drag_point = -1
            self.surfacesChanged.emit(self.surfaces)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return

        pos = (
            event.position()
            if hasattr(event, "position")
            else QtCore.QPointF(event.pos())
        )

        old_zoom = float(self.zoom)

        # 120 is one traditional mouse-wheel notch. Using the fractional
        # exponent also makes high-resolution wheels/trackpads feel smooth.
        factor = 1.15 ** (float(delta) / 120.0)
        new_zoom = min(
            max(old_zoom * factor, 0.5),
            8.0,
        )

        if abs(new_zoom - old_zoom) < 1.0e-10:
            try:
                event.accept()
            except Exception:
                pass
            return

        # Keep the pixel currently under the cursor pinned under the cursor.
        #
        # widget_point =
        #     canvas_center + pan
        #     + zoom * base_scale * image_offset
        #
        # Solving that expression for the new pan after changing zoom avoids
        # the old behavior where every wheel event zoomed toward image center.
        canvas_center = QtCore.QPointF(
            self.width() * 0.5,
            self.height() * 0.5,
        )

        relative = (
            QtCore.QPointF(pos)
            - canvas_center
            - self.pan
        )

        ratio = new_zoom / max(old_zoom, 1.0e-12)

        self.pan = (
            QtCore.QPointF(pos)
            - canvas_center
            - relative * ratio
        )

        self.zoom = new_zoom
        self.update()
        self.zoomChanged.emit(float(new_zoom))

        try:
            event.accept()
        except Exception:
            pass


class GuideTimeline(QtWidgets.QWidget):
    """Compact Mocha-style shot timeline with guide-frame markers."""

    frameChanged = Signal(int)
    scrubStarted = Signal()
    scrubFinished = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.first_frame = 1
        self.last_frame = 100
        self.current_frame = 1
        self.markers = []
        # (start, end) runs: decoded in memory / rendered proxies on disk.
        self.cached_ranges = []
        self.disk_ranges = []
        self.scrubbing = False
        self.setMinimumHeight(58)
        self.setMaximumHeight(62)
        self.setMouseTracking(True)
        self.setFocusPolicy(CLICK_FOCUS)

    def set_cached_ranges(self, memory_ranges, disk_ranges=()):
        memory_ranges = list(memory_ranges or [])
        disk_ranges = list(disk_ranges or [])
        if (
            memory_ranges == self.cached_ranges
            and disk_ranges == self.disk_ranges
        ):
            return
        self.cached_ranges = memory_ranges
        self.disk_ranges = disk_ranges
        self.update()

    def set_range(self, first_frame, last_frame):
        self.first_frame = int(first_frame)
        self.last_frame = max(
            self.first_frame,
            int(last_frame),
        )
        self.current_frame = max(
            self.first_frame,
            min(
                self.last_frame,
                self.current_frame,
            ),
        )
        self.update()

    def set_current_frame(self, frame):
        frame = max(
            self.first_frame,
            min(
                self.last_frame,
                int(frame),
            ),
        )
        self.current_frame = frame
        self.update()

    def set_markers(self, frames):
        self.markers = sorted({
            max(
                self.first_frame,
                min(
                    self.last_frame,
                    int(frame),
                ),
            )
            for frame in (frames or [])
        })
        self.update()

    # Lane layout: labels in a left gutter, as in Match's timeline.
    GUTTER = 64
    RIGHT_PAD = 14
    RULER_Y = 16
    LANES = (("Guides", 30), ("Cache", 48))

    def _x_for_frame(self, frame):
        left = float(self.GUTTER)
        right = max(
            left + 1.0,
            float(self.width()) - self.RIGHT_PAD,
        )
        span = max(
            1,
            self.last_frame - self.first_frame,
        )
        t = (
            float(frame - self.first_frame)
            / float(span)
        )
        return left + t * (right - left)

    def _frame_for_x(self, x):
        left = float(self.GUTTER)
        right = max(
            left + 1.0,
            float(self.width()) - self.RIGHT_PAD,
        )
        t = (
            float(x) - left
        ) / max(
            right - left,
            1.0,
        )
        t = max(0.0, min(1.0, t))
        return int(
            round(
                self.first_frame
                + t
                * (
                    self.last_frame
                    - self.first_frame
                )
            )
        )

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(
            QtGui.QPainter.Antialiasing,
            False,
        )
        painter.fillRect(self.rect(), QtGui.QColor(theme.BG_1))

        left = self.GUTTER
        right = self.width() - self.RIGHT_PAD
        height = self.height()
        line = QtGui.QColor(theme.LINE)
        faint = QtGui.QColor(theme.TEXT_FAINT)
        dim = QtGui.QColor(theme.TEXT_DIM)
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        painter.setFont(font)

        # Ruler: a thin line with labelled ticks at a readable spacing.
        frame_count = max(1, self.last_frame - self.first_frame)
        step = tick_step(frame_count, max(1, right - left))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.CACHE_DISK), 1))
        painter.drawLine(left, self.RULER_Y + 4, right, self.RULER_Y + 4)
        first_tick = ((self.first_frame + step - 1) // step) * step
        for frame in range(first_tick, self.last_frame + 1, step):
            x = int(round(self._x_for_frame(frame)))
            painter.setPen(line)
            painter.drawLine(x, self.RULER_Y + 6, x, height)
            painter.setPen(faint)
            painter.drawText(x + 3, self.RULER_Y, str(frame))

        # Lane labels and separators.
        for text, y in self.LANES:
            painter.setPen(dim)
            painter.drawText(10, y + 4, text)

        # Guides lane: one mark per surface guide frame.
        guide_y = self.LANES[0][1]
        guide = QtGui.QColor(theme.GUIDE)
        for frame in self.markers:
            x = int(round(self._x_for_frame(frame)))
            painter.fillRect(x - 2, guide_y - 6, 5, 12, guide)

        # Cache lane: dark = proxy rendered to disk, bright = decoded in memory.
        cache_y = self.LANES[1][1]
        painter.fillRect(left, cache_y - 1, max(1, right - left), 3, QtGui.QColor("#1a1d21"))
        for ranges, color in (
            (self.disk_ranges, QtGui.QColor(theme.CACHE_DISK)),
            (self.cached_ranges, QtGui.QColor(theme.CACHE)),
        ):
            for start, end in ranges:
                x0 = max(left, int(round(self._x_for_frame(start - 0.5))))
                x1 = min(right, int(round(self._x_for_frame(end + 0.5))))
                if x1 >= x0:
                    painter.fillRect(x0, cache_y - 1, max(1, x1 - x0), 3, color)

        # Playhead.
        current_x = int(round(self._x_for_frame(self.current_frame)))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.PLAYHEAD), 2))
        painter.drawLine(current_x, self.RULER_Y + 2, current_x, height - 2)
        painter.setBrush(QtGui.QColor(theme.PLAYHEAD))
        painter.setPen(QtCore.Qt.NoPen if hasattr(QtCore.Qt, "NoPen") else QtCore.Qt.PenStyle.NoPen)
        painter.drawPolygon(QtGui.QPolygon([
            QtCore.QPoint(current_x - 5, self.RULER_Y - 4),
            QtCore.QPoint(current_x + 5, self.RULER_Y - 4),
            QtCore.QPoint(current_x, self.RULER_Y + 3),
        ]))
        painter.end()

    def mousePressEvent(self, event):
        pos = (
            event.position()
            if hasattr(event, "position")
            else event.pos()
        )
        frame = self._frame_for_x(
            pos.x()
        )
        self.scrubbing = True
        self.scrubStarted.emit()
        self.set_current_frame(frame)
        self.frameChanged.emit(frame)

    def mouseMoveEvent(self, event):
        buttons = event.buttons()
        if buttons & LEFT_BUTTON:
            pos = (
                event.position()
                if hasattr(event, "position")
                else event.pos()
            )
            frame = self._frame_for_x(
                pos.x()
            )
            # Many mouse moves land on the same frame; only report changes.
            if frame == self.current_frame:
                return
            self.set_current_frame(frame)
            self.frameChanged.emit(frame)

    def mouseReleaseEvent(self, event):
        if self.scrubbing:
            self.scrubbing = False
            self.scrubFinished.emit(int(self.current_frame))


class AutoLensBackgroundMonitor(QtWidgets.QDialog):
    """
    Compact non-modal progress palette used while the main AutoLens workspace
    is hidden. The actual QProcess remains owned by the live AutoLens dialog.
    """

    def __init__(self, owner, parent=None):
        super().__init__(parent)

        self.owner = owner

        self.setWindowTitle(
            f"AutoLens {version_text(__version__)} - DeadCow VFX"
        )

        try:
            tool_flag = getattr(
                QtCore.Qt,
                "Tool",
                getattr(
                    getattr(QtCore.Qt, "WindowType", object()),
                    "Tool",
                    0,
                ),
            )
            self.setWindowFlags(
                self.windowFlags() | tool_flag
            )
        except Exception:
            pass

        self.setAttribute(
            QtCore.Qt.WA_DeleteOnClose,
            False,
        )
        self.setMinimumWidth(410)
        self.setMaximumWidth(560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        title_row = QtWidgets.QHBoxLayout()

        self.title_label = QtWidgets.QLabel(
            f"<b>AutoLens {version_text(__version__)}</b>"
        )
        self.node_label = QtWidgets.QLabel(
            owner.controller_node.name()
        )
        self.node_label.setStyleSheet(
            "color: #909090;"
        )

        title_row.addWidget(self.title_label)
        title_row.addSpacing(8)
        title_row.addWidget(self.node_label)
        title_row.addStretch(1)

        layout.addLayout(title_row)

        self.stage_label = QtWidgets.QLabel(
            "Starting AutoLens…"
        )
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.detail_label = QtWidgets.QLabel(
            "The solve is running outside the main Nuke UI. "
            "You can continue working in the comp."
        )
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(
            "color: #8f8f8f;"
        )
        layout.addWidget(self.detail_label)

        button_row = QtWidgets.QHBoxLayout()

        self.open_button = QtWidgets.QPushButton(
            "Open AutoLens"
        )
        self.cancel_button = QtWidgets.QPushButton(
            "Cancel Solve"
        )
        self.dismiss_button = QtWidgets.QPushButton(
            "Hide"
        )

        button_row.addWidget(
            self.open_button,
            1,
        )
        button_row.addWidget(
            self.cancel_button,
        )
        button_row.addWidget(
            self.dismiss_button,
        )

        layout.addLayout(button_row)

        self.open_button.clicked.connect(
            self._open_main
        )
        self.cancel_button.clicked.connect(
            owner.panel.runner.cancel
        )
        self.dismiss_button.clicked.connect(
            self.hide
        )

        owner.panel.runner.progress_changed.connect(
            self._progress_changed
        )
        owner.panel.runner.started.connect(
            self._started
        )
        owner.panel.runner.finished.connect(
            self._finished
        )

    def _started(self):
        self.progress.setValue(0)
        self.stage_label.setStyleSheet("")
        self.stage_label.setText(
            "Starting AutoLens…"
        )
        self.cancel_button.setEnabled(True)
        self.detail_label.setText(
            "The solve is running in the background. "
            "You can continue working in Nuke."
        )

    def _progress_changed(self, percent, stage):
        self.progress.setValue(
            int(percent)
        )
        self.stage_label.setStyleSheet("")
        self.stage_label.setText(
            f"{stage} — {int(percent)}%"
        )

    def _finished(self, exit_code):
        self.cancel_button.setEnabled(False)

        if int(exit_code) == 0:
            self.progress.setValue(100)
            self.stage_label.setText(
                "✓ AutoLens complete"
            )
            self.stage_label.setStyleSheet(
                "color: #63d471; font-weight: 700;"
            )
            self.detail_label.setText(
                "The solve finished successfully. "
                "Open AutoLens to review/import the result."
            )
        else:
            self.stage_label.setText(
                "✗ AutoLens stopped or failed"
            )
            self.stage_label.setStyleSheet(
                "color: #e08080; font-weight: 700;"
            )
            self.detail_label.setText(
                "Open AutoLens and check the Solve log for details."
            )

        # Bring the compact notification forward when a background job ends.
        if not self.owner.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def _open_main(self):
        self.owner.showMaximized()
        self.owner.raise_()
        self.owner.activateWindow()
        self.hide()

    def closeEvent(self, event):
        # Closing the notification must never terminate the solve.
        self.hide()
        event.ignore()


class AutoLensDialog(QtWidgets.QDialog):
    """
    Mocha-inspired AutoLens workspace:
      - persistent image viewport
      - editable planar surface guides over the plate
      - compact Surfaces and Solve tabs
    """

    def __init__(self, controller_node, parent=None):
        super().__init__(parent or _active_window())

        self.controller_node = controller_node
        self.read_node = source_read_for_node(controller_node)
        self._syncing_list = False

        self.setWindowTitle(
            f"AutoLens {version_text(__version__)} - DeadCow VFX"
        )
        self.background_monitor = None

        self.setAttribute(
            QtCore.Qt.WA_DeleteOnClose,
            True,
        )
        self.setMinimumSize(900, 620)

        with timed("dialog: build UI"):
            self._build_ui()
            self._install_shortcuts()
        with timed("dialog: build Solve panel"):
            self._construct_panel()
        with timed("dialog: load node settings"):
            self._load_node_settings(self.read_node)
        self._wire_node_sync()
        self._set_step("track")

        # The plate preview can mean a Nuke render (EXR, TIFF), so by default
        # it loads on the first event-loop turn after the window is shown.
        # AUTOLENS_DEFER_PREVIEW=0 loads it here, before the window appears.
        self._initial_load_done = False
        if not defer_initial_preview():
            self._initial_load()

        # AutoLens is designed as a full workspace, not a utility popup.
        # Give it a sensible fallback size here; show_autolens_dialog() opens it
        # maximized after construction.
        screen = self.screen() or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(1600, int(available.width() * 0.96)),
                min(980, int(available.height() * 0.94)),
            )
        else:
            self.resize(1440, 900)

    def _initial_load(self):
        """Plate preview, surfaces, masks and timeline. Runs once."""
        if self._initial_load_done:
            return
        self._initial_load_done = True

        with timed("dialog: preview, surfaces, masks, timeline"):
            self._load_preview()
            self._reload_surfaces()
            self._reload_masks()
            self._refresh_timeline()
            # The workspace opens on Track: show the mask overlay there.
            self._sidebar_tab_changed(self.sidebar.currentIndex())
            self._refresh_workflow()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        """
        Match-style workspace:

          top bar      brand · menus · shot breadcrumb · Track › Draw › Output
          middle       viewer (status chip, frame) | panel for the current step
          bottom       transport + Track / Stop, then the timeline lanes
          status bar   what to do next | last message

        The panel only shows the current step. Everything else is in a menu,
        the quality popover or Solve settings.
        """
        self.setObjectName("AutoLensWorkspace")
        self.setStyleSheet(theme.STYLESHEET)
        self._step = "track"
        self._result_cache = (None, None, {})
        self._last_percent = 0

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- top bar: brand, menus, shot, workflow --------------------------
        top = _frame("TopBar")
        top.setFixedHeight(46)
        tl = _hbox(top, (14, 0, 12, 0), 4)
        tl.addWidget(_label("AUTOLENS", "Brand"))
        tl.addSpacing(12)
        file_btn, self.file_menu = _menu_button("File")
        track_btn, self.track_menu = _menu_button("Track")
        draw_btn, self.draw_menu = _menu_button("Draw")
        output_btn, self.output_menu = _menu_button("Output")
        view_btn, self.view_menu = _menu_button("View")
        for b in (file_btn, track_btn, draw_btn, output_btn, view_btn):
            tl.addWidget(b)
        tl.addSpacing(10)
        tl.addWidget(_separator())
        tl.addSpacing(10)
        self.shot_label = _elide(_label("No plate", "ShotInfo"))
        tl.addWidget(self.shot_label, 1)

        self.step_buttons = {}
        for index, (key, text) in enumerate(ux.WORKSPACE_STEPS):
            if index:
                tl.addWidget(_label("›", "StepArrow"))
            b = QtWidgets.QPushButton(text)
            b.setObjectName("Step")
            b.setAutoDefault(False)
            b.setFocusPolicy(NO_FOCUS)
            b.setToolTip(_STEP_TIPS[key])
            b.clicked.connect(lambda _c=False, k=key: self._set_step(k))
            self.step_buttons[key] = b
            tl.addWidget(b)
        root.addWidget(top)

        # -- viewer: status chip and frame over the plate --------------------
        viewer_frame = QtWidgets.QWidget()
        viewer_frame.setStyleSheet(f"background: {theme.BG_0};")
        viewer_layout = QtWidgets.QVBoxLayout(viewer_frame)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        header = QtWidgets.QWidget()
        hl = _hbox(header, (12, 6, 8, 4), 8)
        self.chip = QtWidgets.QLabel("NOT SOLVED")
        self.chip.setObjectName("Chip")
        self.chip.setStyleSheet(theme.chip_style("none"))
        self.chip.setToolTip(
            "Track status. After a track it shows the mean reprojection error: "
            "under about 1 px is a good solve."
        )
        hl.addWidget(self.chip)
        hl.addStretch(1)
        self.frame_label = _label(f"Frame {int(nuke.frame())}", "Faint")
        hl.addWidget(self.frame_label)
        self.fit_button = _icon_button("fit", "Fit the plate to the viewer (F)", name="Tool")
        hl.addWidget(self.fit_button)
        viewer_layout.addWidget(header)
        self.canvas = SurfaceCanvas()
        viewer_layout.addWidget(self.canvas, 1)

        # -- side panel: one page per step -----------------------------------
        side = _frame("SidePanel")
        side_layout = _vbox(side, (14, 12, 14, 12), 10)
        self.sidebar = QtWidgets.QStackedWidget()
        side_layout.addWidget(self.sidebar, 1)

        # Track ---------------------------------------------------------------
        track_page = QtWidgets.QWidget()
        tp = _vbox(track_page, (0, 0, 0, 0), 8)
        tp.addWidget(_label("TRACK", "Section"))
        self.track_info = _wrap(_label("", "Dim"))
        tp.addWidget(self.track_info)
        self.track_row = QtWidgets.QHBoxLayout()
        self.track_row.setSpacing(6)
        tp.addLayout(self.track_row)   # Track button + quality popover, see _construct_panel
        self.track_status_layout = QtWidgets.QVBoxLayout()
        self.track_status_layout.setSpacing(6)
        tp.addLayout(self.track_status_layout)  # progress, stage and Cancel
        tp.addSpacing(10)
        tp.addWidget(_label("TRACKING MASK", "Section"))
        self.mask_connection_status = _wrap(_label("No mask connected.", "Dim"))
        tp.addWidget(self.mask_connection_status)
        mask_row = QtWidgets.QHBoxLayout()
        self.mask_mode_label = _label("Alpha meaning: AVOID", "Dim")
        mask_row.addWidget(self.mask_mode_label, 1)
        self.mask_invert_button = QtWidgets.QPushButton("Invert")
        self.mask_invert_button.setObjectName("Toggle")
        self.mask_invert_button.setCheckable(True)
        self.mask_invert_button.setAutoDefault(False)
        self.mask_invert_button.setToolTip(
            "Swap whether the mask's alpha means Avoid (default) or Keep. "
            "The red overlay always shows what tracking will avoid."
        )
        mask_row.addWidget(self.mask_invert_button)
        tp.addLayout(mask_row)
        tp.addWidget(_wrap(_label(
            "Optional: connect a matte to the TrackingMask input to keep moving "
            "people, cars or reflections out of the track.",
            "Faint",
        )))
        tp.addStretch(1)
        self.masks_tab_index = self.sidebar.addWidget(track_page)

        # Draw ----------------------------------------------------------------
        draw_page = QtWidgets.QWidget()
        dp = _vbox(draw_page, (0, 0, 0, 0), 8)
        dp.addWidget(_label("SURFACES", "Section"))
        self.draw_info = _wrap(_label("", "Dim"))
        dp.addWidget(self.draw_info)
        tools = QtWidgets.QHBoxLayout()
        tools.setSpacing(4)
        self.surface_add_button = _icon_button(
            "plane", "Add plane (P): drag its four corners onto a flat surface", name="Tool")
        self.surface_auto_button = _icon_button(
            "wand", "Auto suggest: guess floor, wall and object planes on this frame", name="Tool")
        self.guide_frame_button = _icon_button(
            "flag", "Set guide frame (G): the selected surface was drawn on this frame", name="Tool")
        self.surface_delete_button = _icon_button(
            "trash", "Delete the selected surface (Del)", name="Tool")
        for b in (self.surface_add_button, self.surface_auto_button, self.guide_frame_button):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self.surface_delete_button)
        dp.addLayout(tools)
        self.surface_list = QtWidgets.QListWidget()
        self.surface_list.setToolTip("Surfaces sent to the track. Select one to edit it in the viewer.")
        dp.addWidget(self.surface_list, 1)
        self.surface_name_edit = QtWidgets.QLineEdit()
        self.surface_name_edit.setPlaceholderText("Surface name")
        self.surface_name_edit.setToolTip(
            "Name of the selected surface; press Enter to rename. Its ReadGeo uses this name."
        )
        dp.addWidget(self.surface_name_edit)
        # Renaming happens on Enter; the button stays for code that clicks it.
        self.rename_button = QtWidgets.QPushButton("Rename")
        self.rename_button.hide()
        self.surface_status = _wrap(_label(
            "Green: you're on the surface's guide frame. Grey: another frame.", "Faint"))
        dp.addWidget(self.surface_status)
        self.sidebar.addWidget(draw_page)

        # Output --------------------------------------------------------------
        output_page = QtWidgets.QWidget()
        op = _vbox(output_page, (0, 0, 0, 0), 8)
        op.addWidget(_label("OUTPUT", "Section"))
        self.output_info = _wrap(_label("", "Dim"))
        op.addWidget(self.output_info)
        self.output_create_layout = QtWidgets.QVBoxLayout()
        self.output_create_layout.setSpacing(6)
        op.addLayout(self.output_create_layout)   # Create All, see _construct_panel
        create_row = QtWidgets.QHBoxLayout()
        self.create_one_button = QtWidgets.QPushButton("Create one")
        self.create_one_button.setObjectName("MenuButton")
        self.create_one_button.setAutoDefault(False)
        self.create_one_button.setToolTip(
            "Create a single asset from the solve: the camera, undistort, redistort, "
            "a mesh, or one guided surface"
        )
        self.create_one_menu = QtWidgets.QMenu(self.create_one_button)
        self.create_one_menu.aboutToShow.connect(
            lambda: self._fill_create_menu(self.create_one_menu))
        self.create_one_button.setMenu(self.create_one_menu)
        create_row.addWidget(self.create_one_button, 1)
        self.reveal_output_button = _icon_button(
            "folder", "Open the solve version's folder", name="Tool")
        create_row.addWidget(self.reveal_output_button)
        op.addLayout(create_row)
        op.addSpacing(10)
        op.addWidget(_label("GEOMETRY", "Section"))
        card = _frame("Card")
        cl = _vbox(card, (10, 8, 10, 8), 4)
        self.geometry_rows = []
        for _key, text in ux.GEOMETRY_ITEMS:
            row = QtWidgets.QHBoxLayout()
            name = _label(text, "Dim")
            state = _label("—", "Faint")
            row.addWidget(name, 1)
            row.addWidget(state)
            cl.addLayout(row)
            self.geometry_rows.append(state)
        op.addWidget(card)
        self.geometry_detail = _wrap(_label("", "Faint"))
        self.geometry_detail.hide()
        op.addWidget(self.geometry_detail)
        self.build_geometry_button = QtWidgets.QPushButton("  Build geometry")
        self.build_geometry_button.setIcon(icons.icon("cube"))
        self.build_geometry_button.setIconSize(icons.icon_size())
        self.build_geometry_button.setAutoDefault(False)
        self.build_geometry_button.setToolTip(
            "Build the dense mesh, mesh-fit planes and guided surfaces for this solve. "
            "Reuses the tracked camera, so only the geometry stages run."
        )
        op.addWidget(self.build_geometry_button)
        op.addStretch(1)
        self.sidebar.addWidget(output_page)

        self.settings_toggle = QtWidgets.QPushButton("Solve settings…")
        self.settings_toggle.setObjectName("Disclosure")
        self.settings_toggle.setAutoDefault(False)
        self.settings_toggle.setToolTip(
            "Plate and output folders, solve version, quality, run mode, GPU and "
            "dense settings, and the installation status"
        )
        side_layout.addWidget(self.settings_toggle)

        side.setMinimumWidth(260)
        side.setMaximumWidth(440)
        self.main_splitter = QtWidgets.QSplitter(_HORIZONTAL)
        self.main_splitter.setHandleWidth(3)
        self.main_splitter.addWidget(viewer_frame)
        self.main_splitter.addWidget(side)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setSizes([1300, 300])
        root.addWidget(self.main_splitter, 1)

        # -- bottom: transport with Track / Stop, then the timeline ----------
        bottom = _frame("BottomBar")
        bottom_layout = _vbox(bottom, (0, 0, 0, 0), 0)
        self.timeline = GuideTimeline()
        self._build_playback_bar(bottom_layout)
        timeline_row = QtWidgets.QHBoxLayout()
        timeline_row.setContentsMargins(0, 0, 10, 6)
        timeline_row.addWidget(self.timeline, 1)
        bottom_layout.addLayout(timeline_row)
        root.addWidget(bottom)

        # -- status bar: what to do next | last message ---------------------
        status = _frame("StatusBar")
        status.setFixedHeight(26)
        sb = _hbox(status, (12, 0, 12, 0), 18)
        self.hint_label = _elide(_label("", "Hint"))
        sb.addWidget(self.hint_label, 3)
        self.workspace_status = _elide(_label("Ready", "Faint"))
        self.workspace_status.setAlignment(_ALIGN_RIGHT)
        sb.addWidget(self.workspace_status, 2)
        root.addWidget(status)

        self._build_menus()

        # Wiring for workspace.
        self.fit_button.clicked.connect(self.canvas.fit_view)
        self.surface_auto_button.clicked.connect(self._auto_suggest)
        self.surface_add_button.clicked.connect(self._add_surface)
        self.surface_delete_button.clicked.connect(self._delete_surface)
        self.guide_frame_button.clicked.connect(self._set_guide_frame)
        self.rename_button.clicked.connect(self._rename_surface)
        self.surface_name_edit.returnPressed.connect(self._rename_surface)
        self.surface_list.currentRowChanged.connect(self._surface_list_changed)
        self.canvas.selectionChanged.connect(self._canvas_selection_changed)
        self.canvas.surfacesChanged.connect(self._canvas_surfaces_changed)
        self.reveal_output_button.clicked.connect(self._reveal_output)
        self.build_geometry_button.clicked.connect(lambda: self.panel.build_geometry())

        self.timeline.frameChanged.connect(
            self._timeline_frame_changed
        )
        self.sidebar.currentChanged.connect(
            self._sidebar_tab_changed
        )
        self.mask_invert_button.toggled.connect(
            self._mask_invert_toggled
        )

    def _build_menus(self):
        """Every action, including the ones without a button on screen."""
        m = self.file_menu
        _action(m, "Solve settings…", lambda: self._show_settings())
        _action(m, "Open output folder", self._reveal_output)
        m.addSeparator()
        _action(m, "Installation status…", lambda: self.panel.check_environment())
        _action(m, "Open install report", lambda: self.panel._open_install_report())
        m.addSeparator()
        _action(m, "Close", self.close)

        m = self.track_menu
        self.track_action = _action(m, "Track", lambda: self._track(), "T",
                                    tip="Solve the camera and lens from the plate")
        self.cancel_action = _action(m, "Stop tracking", lambda: self.panel.runner.cancel())
        self.background_action = _action(
            m, "Continue in background", lambda: self.panel.background_requested.emit(),
            tip="Hide the workspace and keep tracking with a small progress window")
        m.addSeparator()
        self.invert_action = _action(m, "Invert tracking mask", lambda: self.mask_invert_button.toggle(),
                                     checkable=True)
        _action(m, "Refresh mask preview", self._refresh_mask_preview)
        m.aboutToShow.connect(self._sync_track_menu)

        m = self.draw_menu
        _action(m, "Add plane", self._add_surface, "P")
        _action(m, "Auto suggest planes", self._auto_suggest)
        _action(m, "Set guide frame", self._set_guide_frame, "G")
        _action(m, "Delete surface", self._delete_surface, "Del")
        m.addSeparator()
        self.use_guides_action = _action(
            m, "Use surfaces in the track", lambda: self.panel.use_guides_checkbox.toggle(),
            checkable=True, tip="Fit a projection plane to each surface when tracking")
        _action(m, "Show guide Roto node", self._open_roto)
        m.aboutToShow.connect(
            lambda: self.use_guides_action.setChecked(self.panel.use_guides_checkbox.isChecked()))

        m = self.output_menu
        _action(m, "Create All", lambda: self.panel.import_last_result(), "Ctrl+E",
                tip="Camera, lens nodes and all geometry, in the comp")
        self.create_one_submenu = m.addMenu("Create one")
        self.create_one_submenu.aboutToShow.connect(
            lambda: self._fill_create_menu(self.create_one_submenu))
        _action(m, "Build geometry", lambda: self.panel.build_geometry())
        m.addSeparator()
        _action(m, "Open output folder", self._reveal_output)

        m = self.view_menu
        _action(m, "Fit", self.canvas.fit_view, "F")
        m.addSeparator()
        for key, text in ux.WORKSPACE_STEPS:
            index = ux.step_index(key) + 1
            _action(m, text, lambda k=key: self._set_step(k), str(index))
        m.addSeparator()
        _action(m, "Log", self._show_log)
        _action(m, "Clear frame cache", lambda: self._clear_frame_cache(),
                tip="Forget the frames held in memory and read them from disk again")

    def _sync_track_menu(self):
        running = self.panel.runner.is_running()
        self.track_action.setEnabled(not running)
        self.cancel_action.setEnabled(running)
        self.background_action.setEnabled(running)
        self.invert_action.setChecked(self.mask_invert_button.isChecked())

    def _install_shortcuts(self):
        self.fit_shortcut = QShortcut(
            QtGui.QKeySequence("F"),
            self,
        )
        self.fit_shortcut.setContext(
            WIDGET_WITH_CHILDREN_SHORTCUT
        )
        self.fit_shortcut.activated.connect(
            self._fit_shortcut_triggered
        )
        self._install_playback_shortcuts()
        for keys, slot in (
            ("T", lambda: self._track()),
            ("P", lambda: (self._set_step("draw"), self._add_surface())),
            ("G", self._set_guide_frame),
            ("Delete", self._delete_surface),
            ("Ctrl+E", lambda: self.panel.import_last_result()),
            ("1", lambda: self._set_step("track")),
            ("2", lambda: self._set_step("draw")),
            ("3", lambda: self._set_step("output")),
        ):
            shortcut = QShortcut(QtGui.QKeySequence(keys), self)
            shortcut.setContext(WIDGET_WITH_CHILDREN_SHORTCUT)
            shortcut.activated.connect(
                lambda slot=slot: None if self._typing_in_field() else slot()
            )
            self.playback_shortcuts.append(shortcut)

    def _fit_shortcut_triggered(self):
        # Don't steal F while the artist is actively typing text/numbers in
        # the Solve panel or renaming a surface.
        focus = QtWidgets.QApplication.focusWidget()

        text_entry_types = (
            QtWidgets.QLineEdit,
            QtWidgets.QTextEdit,
            QtWidgets.QPlainTextEdit,
            QtWidgets.QSpinBox,
            QtWidgets.QDoubleSpinBox,
        )

        if isinstance(focus, text_entry_types):
            return

        self.canvas.fit_view()
        self.workspace_status.setText(
            "Viewer fit to image."
        )

    def _guide_marker_frames(self):
        frames = []

        try:
            mapping = surface_guide_frames(
                self.controller_node
            )
            frames.extend(
                int(value)
                for value in mapping.values()
            )
        except Exception:
            pass

        return sorted(
            set(frames)
        )


    def _refresh_timeline(self):
        read = (
            self.read_node
            or source_read_for_node(
                self.controller_node
            )
        )

        if read is None:
            return

        frame_range = source_frame_range(
            read
        )

        if frame_range:
            self.timeline.set_range(
                frame_range[0],
                frame_range[1],
            )

        self.timeline.set_current_frame(
            int(nuke.frame())
        )
        self.timeline.set_markers(
            self._guide_marker_frames()
        )


    def _timeline_frame_changed(self, frame):
        # While dragging, show whatever is cached right away; Nuke's frame,
        # surfaces, mask overlay and any proxy render follow once the drag
        # settles (mouse release or a short pause).
        self._stop_playback(settle=False)
        frame = int(frame)
        previous = self._view_frame
        direction = 0
        if previous is not None and frame != previous:
            direction = 1 if frame > previous else -1
        self._show_frame(
            frame,
            settle=not self.timeline.scrubbing,
            direction=direction,
        )


    def _sidebar_tab_changed(self, index):
        is_masks = (
            int(index)
            == int(self.masks_tab_index)
        )

        self.canvas.show_mask_overlay = is_masks
        self.canvas.show_surfaces = not is_masks

        if is_masks:
            self._reload_masks()
            self._refresh_mask_preview()
        else:
            self.canvas.clear_mask_overlay()
            self.canvas.update()


    def _reload_masks(self):
        connected = mask_input_connected(
            self.controller_node
        )
        inverted = mask_inverted(
            self.controller_node
        )

        self.mask_invert_button.blockSignals(
            True
        )
        self.mask_invert_button.setChecked(
            bool(inverted)
        )
        self.mask_invert_button.blockSignals(
            False
        )

        if connected:
            source = self.controller_node.input(1)
            try:
                source_name = source.fullName()
            except Exception:
                source_name = "Connected alpha"

            self.mask_connection_status.setText(
                "Connected: "
                + source_name
            )
            self.mask_connection_status.setStyleSheet(
                "color: #72c991; font-weight: 600;"
            )
        else:
            self.mask_connection_status.setText(
                "No alpha is connected to AutoLens input 2."
            )
            self.mask_connection_status.setStyleSheet(
                "color: #888;"
            )

        if inverted:
            self.mask_mode_label.setText(
                "Alpha meaning: KEEP / TRACK"
            )
            self.mask_mode_label.setStyleSheet(
                "color: #72c991; font-weight: 600;"
            )
        else:
            self.mask_mode_label.setText(
                "Alpha meaning: AVOID / EXCLUDE"
            )
            self.mask_mode_label.setStyleSheet(
                "color: #e18aa9; font-weight: 600;"
            )


    def _mask_invert_toggled(self, checked):
        set_mask_inverted(
            self.controller_node,
            bool(checked),
        )
        self._reload_masks()

        if (
            self.sidebar.currentIndex()
            == self.masks_tab_index
        ):
            self._refresh_mask_preview()

        self.workspace_status.setText(
            "Mask interpretation inverted. "
            "Red now shows the area AutoLens will avoid."
        )


    def _refresh_mask_preview(self):
        if not mask_input_connected(
            self.controller_node
        ):
            self.canvas.clear_mask_overlay()
            return

        self.workspace_status.setText(
            "Rendering mask preview…"
        )
        QtWidgets.QApplication.processEvents()

        try:
            overlay = render_mask_overlay_pixmap(
                self.controller_node,
                int(nuke.frame()),
            )
            self.canvas.set_mask_overlay(
                overlay,
                visible=True,
            )
            self.workspace_status.setText(
                "20% red = area AutoLens will avoid during tracking."
            )
        except Exception as exc:
            self.canvas.clear_mask_overlay()
            self.workspace_status.setText(
                f"Mask preview failed: {exc}"
            )


    def _construct_panel(self):
        # Clear the selection while the solve panel builds so it can't pick up
        # an unrelated selected Read. The upstream Read is applied once, by
        # _load_node_settings(); selecting it here made the panel scan the
        # plate folder and solve versions twice.
        old_selection = list(nuke.selectedNodes())
        try:
            for node in old_selection:
                node.setSelected(False)

            self.panel = AutoLensPanel(self)
        finally:
            try:
                for node in nuke.selectedNodes():
                    node.setSelected(False)
                for node in old_selection:
                    node.setSelected(True)
            except Exception:
                pass

        p = self.panel
        p.controller_node = self.controller_node
        p.background_requested.connect(
            self.enter_background_mode
        )

        # The panel keeps all the solve logic. Its run controls move into the
        # Track and Output pages; the rest becomes the Solve settings window.
        # Surface editing lives in the viewer, and the step pills replace the
        # panel's own step indicator.
        for widget in (p.guide_group, p.step_label, p.step_hint_label,
                       p.completed_result_checkbox, p.background_button):
            widget.hide()

        p.run_button.setObjectName("Primary")
        p.run_button.setStyleSheet("")
        p.run_button.setIcon(icons.icon("track", "#071317"))
        p.run_button.setIconSize(icons.icon_size())
        p.run_button.setMinimumHeight(32)
        self.track_row.addWidget(p.run_button, 1)
        self.quality_button = QtWidgets.QPushButton()
        self.quality_button.setObjectName("MenuButton")
        self.quality_button.setAutoDefault(False)
        self.quality_button.setMinimumHeight(32)
        self.quality_button.setToolTip("Quality and what happens after the track")
        self.quality_menu = QtWidgets.QMenu(self.quality_button)
        self.quality_menu.aboutToShow.connect(self._fill_quality_menu)
        self.quality_button.setMenu(self.quality_menu)
        self.track_row.addWidget(self.quality_button)
        p.quality_combo.currentTextChanged.connect(lambda _t: self._sync_quality_button())
        self._sync_quality_button()

        self.track_status_layout.addWidget(p.stage_label)
        self.track_status_layout.addWidget(p.progress)
        p.cancel_button.setText("  Stop")
        p.cancel_button.setIcon(icons.icon("stop", "#ff8a9a"))
        p.cancel_button.setIconSize(icons.icon_size())
        self.track_status_layout.addWidget(p.cancel_button)

        # What's left of the panel's "2. Solve" box is the quality settings.
        quality_box = p.quality_combo.parentWidget()
        if isinstance(quality_box, QtWidgets.QGroupBox):
            quality_box.setTitle("Quality")
        for widget in (p.run_button, p.cancel_button):
            _repolish(widget)

        p.import_button.setObjectName("Primary")
        p.import_button.setText("  Create All")
        p.import_button.setIcon(icons.icon("create", "#071317"))
        p.import_button.setIconSize(icons.icon_size())
        p.import_button.setMinimumHeight(32)
        self.output_create_layout.addWidget(p.import_button)
        _repolish(p.import_button)
        p.reveal_button.hide()

        # View > Log.
        self.log_dialog = QtWidgets.QDialog(self)
        self.log_dialog.setWindowTitle("AutoLens log")
        self.log_dialog.resize(820, 460)
        old_log_box = p.log.parentWidget()
        _vbox(self.log_dialog, (10, 10, 10, 10), 0).addWidget(p.log)
        if old_log_box is not None:
            old_log_box.hide()

        # File > Solve settings.
        self.settings_dialog = QtWidgets.QDialog(self)
        self.settings_dialog.setWindowTitle("AutoLens solve settings")
        self.settings_dialog.resize(560, 760)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(p)
        _vbox(self.settings_dialog, (0, 0, 0, 0), 0).addWidget(scroll)
        self.settings_toggle.clicked.connect(self._show_settings)

        p.runner.started.connect(self._refresh_workflow)
        p.import_button.clicked.connect(
            lambda: QtCore.QTimer.singleShot(0, self._refresh_workflow))

    # ------------------------------------------------------------------
    # Workflow: Track -> Draw -> Output
    # ------------------------------------------------------------------
    def _set_step(self, key):
        self._step = ux.WORKSPACE_KEYS[ux.step_index(key)]
        index = ux.step_index(self._step)
        if self.sidebar.currentIndex() != index:
            self.sidebar.setCurrentIndex(index)
        self._refresh_workflow()

    def _track(self):
        self._set_step("track")
        self.panel.run_autolens()

    def _show_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _show_log(self):
        self.log_dialog.show()
        self.log_dialog.raise_()

    def _reveal_output(self):
        try:
            from .panel import _open_folder
            _open_folder(self.panel._resolved_output_folder())
        except Exception as exc:
            self.workspace_status.setText(f"Couldn't open the output folder: {exc}")

    def _sync_quality_button(self):
        self.quality_button.setText(self.panel.quality_combo.currentText())

    def _fill_quality_menu(self):
        p = self.panel
        menu = self.quality_menu
        menu.clear()
        group = _action_group(self)
        for name in ux.QUALITY_ORDER:
            a = _action(menu, name, lambda n=name: p.quality_combo.setCurrentText(n),
                        checkable=True, tip=ux.preset_summary(name))
            a.setChecked(p.quality_combo.currentText() == name)
            group.addAction(a)
        menu.addSeparator()
        a = _action(menu, "Build geometry during the track",
                    lambda: p.build_geometry_checkbox.toggle(), checkable=True,
                    tip=p.build_geometry_checkbox.toolTip())
        a.setChecked(p.build_geometry_checkbox.isChecked())
        a = _action(menu, "Create All when the track finishes",
                    lambda: p.import_when_done.toggle(), checkable=True,
                    tip=p.import_when_done.toolTip())
        a.setChecked(p.import_when_done.isChecked())
        menu.addSeparator()
        _action(menu, "More settings…", self._show_settings)

    def _fill_create_menu(self, menu):
        from .node import solve_item_options

        menu.clear()
        solved = bool(self.panel.last_result_nk)
        try:
            items = solve_item_options(self.controller_node) if solved else []
        except Exception:
            items = []
        if not items:
            a = menu.addAction("Track first")
            a.setEnabled(False)
            return
        for item in items:
            _action(menu, item, lambda i=item: self._create_one(i))

    def _create_one(self, item):
        from .node import create_selected_solve_item

        try:
            created = create_selected_solve_item(self.controller_node, item)
        except Exception:
            # create_selected_solve_item already told the artist why.
            return
        if created is not None:
            self.workspace_status.setText(f"Created {created.name()}.")

    def _solve_result(self):
        """autolens_result.json of the selected version, re-read only when it changes."""
        folder = self.panel._resolved_output_folder()
        path = os.path.join(folder, "autolens_result.json") if folder else ""
        try:
            stamp = os.path.getmtime(path) if path else None
        except OSError:
            stamp = None
        cached_path, cached_stamp, cached = self._result_cache
        if cached_path == path and cached_stamp == stamp:
            return cached
        result = ux.load_result(folder) if stamp is not None else {}
        self._result_cache = (path, stamp, result)
        return result

    def _refresh_workflow(self, *_args):
        """Step pills, viewer chip, next-step hint and the Output page."""
        if not hasattr(self, "panel"):
            return
        p = self.panel
        running = p.runner.is_running()
        solved = bool(p.last_result_nk)
        failed = bool(p.last_failure) and not solved
        result = self._solve_result() if solved else {}
        folder = p._resolved_output_folder()
        building = running and bool(getattr(p, "geometry_build_running", False))
        geometry, items, geometry_detail = ux.geometry_overview(
            result, ux.load_geometry_status(folder) if solved else {}, running=running,
        )
        surfaces = len(getattr(self.canvas, "surfaces", []) or [])
        has_plate = self.read_node is not None
        installed = bool(getattr(p, "_install_ready", True))

        states = ux.workspace_step_states(
            self._step, solved=solved, failed=failed,
            surfaces=surfaces, created=bool(getattr(p, "_imported", False)),
        )
        for key, button in self.step_buttons.items():
            if button.property("state") != states[key]:
                button.setProperty("state", states[key])
                button.style().unpolish(button)
                button.style().polish(button)

        text, chip_state = ux.solve_chip(
            running=running, percent=self._last_percent, solved=solved and not running,
            failed=failed, error_px=ux.result_error_px(result), building=building,
        )
        self.chip.setText(text)
        self.chip.setStyleSheet(theme.chip_style(chip_state))

        hint_args = dict(
            has_plate=has_plate, installed=installed, running=running, solved=solved,
            surfaces=surfaces, geometry_ready=geometry == "ready",
            created=bool(getattr(p, "_imported", False)),
            geometry=geometry, geometry_detail=geometry_detail,
        )
        self.hint_label.setText(ux.workspace_hint(self._step, **hint_args))
        self.track_info.setText(ux.workspace_hint("track", **hint_args))
        self.draw_info.setText(ux.workspace_hint("draw", **hint_args))
        self.output_info.setText(ux.workspace_hint("output", **hint_args))

        for state_label, (_name, ready) in zip(self.geometry_rows, items):
            if not solved:
                state_label.setText("—")
                state_label.setStyleSheet("")
            elif ready:
                state_label.setText("✓ ready")
                state_label.setStyleSheet(f"color: {theme.CACHE};")
            elif geometry == "building":
                state_label.setText("building…")
                state_label.setStyleSheet(f"color: {theme.ACCENT};")
            else:
                state_label.setText("not built")
                state_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        if geometry == "building":
            self.geometry_detail.setText(geometry_detail or "Building…")
            self.geometry_detail.setStyleSheet(f"color: {theme.ACCENT};")
        elif geometry == "failed":
            self.geometry_detail.setText(
                "Last build failed" + (f": {geometry_detail}" if geometry_detail else "."))
            self.geometry_detail.setStyleSheet(f"color: {theme.STATUS_FAILED};")
        elif geometry == "missing":
            self.geometry_detail.setText("Camera-only track: geometry not built yet.")
            self.geometry_detail.setStyleSheet("")
        else:
            self.geometry_detail.setText("")
        self.geometry_detail.setVisible(bool(self.geometry_detail.text()))
        self.build_geometry_button.setEnabled(solved and not running)
        self.build_geometry_button.setText(
            "  Rebuild geometry" if geometry == "ready" else "  Build geometry")
        self.create_one_button.setEnabled(solved)
        self.track_transport_button.setEnabled(not running)
        self.stop_transport_button.setEnabled(running)
        p.cancel_button.setVisible(running)
        p.progress.setVisible(running or p.progress.value() > 0)

        read = self.read_node
        width = height = 0
        first = last = None
        info = getattr(self, "_preview_info", {}) or {}
        width, height = info.get("width", 0), info.get("height", 0)
        if read is not None:
            try:
                frame_range = source_frame_range(read)
                if frame_range:
                    first, last = frame_range
            except Exception:
                pass
        self.shot_label.setText(ux.shot_summary(
            read.name() if read is not None else "",
            width if width > 1 else 0, height if height > 1 else 0,
            first, last, p._selected_version() if p.output_edit.text().strip() else "",
        ))

    # ------------------------------------------------------------------
    # Existing node setting sync
    # ------------------------------------------------------------------
    def _load_node_settings(self, read):
        node = self.controller_node

        # Capture the user's current solve selection BEFORE use_selected_read().
        # That helper is also used for new-shot initialization and intentionally
        # prefers Latest. On an existing controller, however, Setup / Run must
        # honor an explicit v1/v2/etc choice from the properties panel.
        desired_root = ""
        desired_version = ""

        try:
            if (
                node.knob("autolens_output_root")
                and node[
                    "autolens_output_root"
                ].value()
            ):
                desired_root = node[
                    "autolens_output_root"
                ].value()
        except Exception:
            pass

        try:
            if node.knob("autolens_version"):
                desired_version = node[
                    "autolens_version"
                ].value()
        except Exception:
            pass

        legacy_output = ""

        try:
            if (
                node.knob("autolens_output")
                and node[
                    "autolens_output"
                ].value()
            ):
                legacy_output = node[
                    "autolens_output"
                ].value()
        except Exception:
            pass

        # Each setText() on these fields fires a version rescan, a result
        # rescan and a plate-folder count through the panel's signals, on top
        # of the explicit calls below. Hold the signals while the fields are
        # filled; the explicit sync and restore_result_state() below bring the
        # panel and controller to the same final state.
        blocked = [
            widget
            for widget in (
                getattr(self.panel, "source_edit", None),
                getattr(self.panel, "output_edit", None),
                getattr(self.panel, "version_combo", None),
            )
            if widget is not None
        ]
        was_blocked = [widget.blockSignals(True) for widget in blocked]

        if read is not None:
            self.panel.use_selected_read(read)

        # Restore the exact controller root/version captured above rather than
        # accepting the temporary Latest selection made while initializing the
        # panel from the Read.
        if desired_root:
            self.panel.output_edit.setText(
                desired_root
            )
            self.panel._refresh_version_combo(
                prefer_latest=False,
            )

            if desired_version:
                index = (
                    self.panel
                    .version_combo
                    .findText(
                        desired_version
                    )
                )

                if index >= 0:
                    self.panel.version_combo.setCurrentIndex(
                        index
                    )

        elif legacy_output:
            legacy = Path(
                legacy_output
            )

            if re.match(
                r"^v\d+$",
                legacy.name,
                re.IGNORECASE,
            ):
                self.panel.output_edit.setText(
                    str(
                        legacy.parent
                    )
                )
                legacy_version = (
                    legacy.name
                )
            else:
                self.panel.output_edit.setText(
                    str(legacy)
                )
                legacy_version = ""

            self.panel._refresh_version_combo(
                prefer_latest=False,
            )

            if legacy_version:
                index = (
                    self.panel
                    .version_combo
                    .findText(
                        legacy_version
                    )
                )

                if index >= 0:
                    self.panel.version_combo.setCurrentIndex(
                        index
                    )

        for widget, previous in zip(blocked, was_blocked):
            widget.blockSignals(previous)

        # One final sync ensures the Setup / Run panel and controller agree on
        # the user's selected version before any result/cache state is restored.
        # restore_result_state() runs once, at the end, after the remaining
        # settings are loaded; it rescans the output folder each time.
        self.panel._sync_controller_output()

        if node.knob("autolens_quality"):
            idx = self.panel.quality_combo.findText(
                node["autolens_quality"].value()
            )
            if idx >= 0:
                self.panel.quality_combo.setCurrentIndex(idx)

        if node.knob("autolens_run_mode"):
            idx = self.panel.run_mode.findText(
                node["autolens_run_mode"].value()
            )
            if idx >= 0:
                self.panel.run_mode.setCurrentIndex(idx)

        if node.knob("autolens_use_guides"):
            self.panel.use_guides_checkbox.setChecked(
                bool(node["autolens_use_guides"].value())
            )

        if node.knob("autolens_auto_import"):
            self.panel.import_when_done.setChecked(
                bool(node["autolens_auto_import"].value())
            )

        # Managed v0.9 installer data is already loaded by AutoLensPanel.
        # Only overwrite from node knobs if they contain actual string paths.
        if node.knob("autolens_python"):
            value = node["autolens_python"].value()
            if isinstance(value, str) and value:
                self.panel.python_edit.setText(value)

        if node.knob("autolens_colmap"):
            value = node["autolens_colmap"].value()
            if isinstance(value, str) and value:
                self.panel.colmap_edit.setText(value)

        self.panel._update_guide_status()
        self.panel.restore_result_state()

    def _wire_node_sync(self):
        p = self.panel
        n = self.controller_node

        p.output_edit.textChanged.connect(
            lambda value: n["autolens_output"].setValue(value)
        )
        p.quality_combo.currentTextChanged.connect(
            lambda value: n["autolens_quality"].setValue(value)
        )
        p.run_mode.currentTextChanged.connect(
            lambda value: n["autolens_run_mode"].setValue(value)
        )
        p.use_guides_checkbox.toggled.connect(
            lambda value: n["autolens_use_guides"].setValue(bool(value))
        )
        p.import_when_done.toggled.connect(
            lambda value: n["autolens_auto_import"].setValue(bool(value))
        )
        p.python_edit.textChanged.connect(
            lambda value: n["autolens_python"].setValue(str(value))
        )
        p.colmap_edit.textChanged.connect(
            lambda value: n["autolens_colmap"].setValue(str(value))
        )

        p.runner.progress_changed.connect(self._progress)
        p.runner.finished.connect(self._finished)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def _load_preview(self):
        """Point the viewer at the source Read and show Nuke's current frame."""
        self.frame_label.setText(f"Frame {int(nuke.frame())}")

        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            self.workspace_status.setText("No upstream Read node.")
            self.canvas.set_image(QtGui.QPixmap(), 1, 1)
            self._preview_mode = None
            return

        self.read_node = read

        requested_frame = int(nuke.frame())
        frame_info = clamp_frame_to_source(
            requested_frame,
            read,
        )
        frame = int(frame_info["frame"])

        if frame_info["clamped"]:
            try:
                nuke.frame(frame)
            except Exception:
                pass

        # The timeline range bounds stepping and playback.
        frame_range = source_frame_range(read)
        if frame_range:
            self.timeline.set_range(frame_range[0], frame_range[1])
        else:
            self.timeline.set_range(
                min(frame, self.timeline.first_frame),
                max(frame, self.timeline.last_frame),
            )

        self._setup_preview_source(read, force=True)
        decode_failed = False
        if self._preview_mode == "qt" and self._preview_path_for_frame(frame):
            # One quick Qt decode so a JPEG/PNG plate is there at once; EXR
            # frames arrive from the frame server a moment later.
            decode_failed = self._preview.load_now(frame) is None
        self._show_frame(frame, settle=False)
        self._settle_frame(frame, refresh_surfaces=False)
        if decode_failed:
            self._preview_frame_failed(frame)

        if frame_info["clamped"] and self._view_exact:
            self.workspace_status.setText(
                self.workspace_status.text()
                + f" • auto-clamped from {requested_frame} "
                f"to source range {frame_info['first']}–{frame_info['last']}"
            )

    # ------------------------------------------------------------------
    # Preview source, frame cache and playback
    # ------------------------------------------------------------------
    def _build_playback_bar(self, layout):
        """Transport controls under the timeline, plus the frame cache."""
        self._view_frame = None
        self._view_exact = False
        self._displayed_frame = None
        self._preview_mode = None          # "qt", "server" or None
        self._preview_info = {}
        self._preview_signature = None
        self._preview_decoder = None
        self._preview_note = ""            # e.g. upstream nodes not shown
        self._preview_problem = ""         # why no frames can be shown
        self._read_snapshot = None         # plate_reader.ReadSnapshot
        self._frame_server = None          # frame_server.FrameServerPool
        self._play_clock = None
        self._play_shown = 0
        self._play_held = 0
        self._play_stats_time = 0.0
        self._qt_formats = supported_extensions()

        self._preview = PreviewLoader(
            self,
            cache_budget_bytes(),
            workers=min(4, max(2, (os.cpu_count() or 4) // 2)),
        )
        self._preview.on_frames_ready = self._preview_frames_ready
        self._preview.on_frame_failed = self._preview_frame_failed
        self._preview.on_full_ready = self._preview_full_ready

        self._play_timer = QtCore.QTimer(self)
        self._play_timer.setTimerType(PRECISE_TIMER)
        self._play_timer.timeout.connect(self._playback_tick)

        self._settle_timer = QtCore.QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(150)
        self._settle_timer.timeout.connect(
            lambda: self._settle_frame(self._view_frame)
        )

        self._sharp_timer = QtCore.QTimer(self)
        self._sharp_timer.setSingleShot(True)
        self._sharp_timer.setInterval(250)
        self._sharp_timer.timeout.connect(self._request_sharp)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        bar.setSpacing(4)

        def button(icon_name, tip, slot, name="Transport"):
            b = _icon_button(icon_name, tip, name=name)
            # Keep keyboard focus off the transport so Space always plays.
            b.setFocusPolicy(NO_FOCUS)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        button("first", "First frame (Home)", lambda: self._go_to_end(-1))
        button("prev", "Previous frame (← or ,)", lambda: self._step(-1))
        self.frame_field = QtWidgets.QSpinBox()
        self.frame_field.setObjectName("FrameField")
        self.frame_field.setProperty("bare", True)
        self.frame_field.setButtonSymbols(_NO_BUTTONS)
        self.frame_field.setRange(-10000000, 10000000)
        self.frame_field.setFixedWidth(72)
        self.frame_field.setKeyboardTracking(False)
        self.frame_field.setToolTip("Current frame: type a frame and press Enter")
        self.frame_field.valueChanged.connect(self._frame_field_changed)
        bar.addWidget(self.frame_field)
        button("next", "Next frame (→ or .)", lambda: self._step(1))
        button("last", "Last frame (End)", lambda: self._go_to_end(1))
        bar.addSpacing(4)
        self.play_back_button = button(
            "play_back", "Play backwards", lambda: self._toggle_playback(-1)
        )
        self.play_button = button(
            "play", "Play / pause (Space)", lambda: self._toggle_playback(1)
        )
        bar.addSpacing(6)
        self.loop_button = QtWidgets.QPushButton("Loop")
        self.loop_button.setObjectName("Toggle")
        self.loop_button.setCheckable(True)
        self.loop_button.setChecked(True)
        self.loop_button.setFocusPolicy(NO_FOCUS)
        self.loop_button.setToolTip("Loop playback over the source range")
        bar.addWidget(self.loop_button)

        self.fps_spin = QtWidgets.QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 120.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setProperty("bare", True)
        self.fps_spin.setButtonSymbols(_NO_BUTTONS)
        self.fps_spin.setFixedWidth(78)
        self.fps_spin.setToolTip(
            "Playback speed. Frames that aren't decoded in time are skipped "
            "so playback stays in real time."
        )
        try:
            fps = float(nuke.root()["fps"].value())
        except Exception:
            fps = 24.0
        self.fps_spin.setValue(fps if fps > 0 else 24.0)
        bar.addWidget(self.fps_spin)

        # Centre: the one action of the Track step, as in Match.
        bar.addStretch(1)
        bar.addWidget(_label("Track", "Dim"))
        bar.addSpacing(4)
        self.track_transport_button = button(
            "track", "Track the camera and lens (T)", lambda: self._track(), name="TrackButton"
        )
        self.stop_transport_button = button(
            "stop", "Stop tracking", lambda: self.panel.runner.cancel(), name="Stop"
        )
        self.stop_transport_button.setEnabled(False)
        bar.addStretch(1)

        self.cache_label = _label("", "Faint")
        self.cache_label.setToolTip(
            "Frames decoded in memory (green in the timeline). View > Clear frame cache forgets them."
        )
        bar.addWidget(self.cache_label)

        layout.addLayout(bar)

        self.timeline.scrubStarted.connect(
            lambda: self._stop_playback(settle=False)
        )
        self.timeline.scrubFinished.connect(self._settle_frame)
        self.canvas.zoomChanged.connect(
            lambda _zoom: self._sharp_timer.start()
        )
        # A bigger viewer (e.g. the window maximising after it opens) gets
        # sharper frames once the resize settles, not mid-playback.
        self._resize_timer = QtCore.QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(300)
        self._resize_timer.timeout.connect(self._refresh_decode_width)
        self.canvas.resized.connect(self._resize_timer.start)
        self.finished.connect(self._shutdown_preview)

    def _install_playback_shortcuts(self):
        self.playback_shortcuts = []
        for keys, slot in (
            ("Space", lambda: self._toggle_playback(0)),
            (",", lambda: self._step(-1)),
            (".", lambda: self._step(1)),
        ):
            shortcut = QShortcut(QtGui.QKeySequence(keys), self)
            shortcut.setContext(WIDGET_WITH_CHILDREN_SHORTCUT)
            shortcut.activated.connect(
                lambda slot=slot: None if self._typing_in_field() else slot()
            )
            self.playback_shortcuts.append(shortcut)

    def _typing_in_field(self):
        focus = QtWidgets.QApplication.focusWidget()
        return isinstance(
            focus,
            (
                QtWidgets.QLineEdit,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractSpinBox,
                QtWidgets.QComboBox,
            ),
        )

    def keyPressEvent(self, event):
        # Arrow keys reach here when the viewer/timeline (or anything that
        # doesn't use them itself) has focus. Shift steps 10 frames.
        key = event.key()
        try:
            shift = bool(event.modifiers() & QtCore.Qt.ShiftModifier)
        except Exception:
            shift = False
        if key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
            delta = 10 if shift else 1
            self._step(delta if key == QtCore.Qt.Key_Right else -delta)
            event.accept()
            return
        if key in (QtCore.Qt.Key_Home, QtCore.Qt.Key_End):
            self._go_to_end(1 if key == QtCore.Qt.Key_End else -1)
            event.accept()
            return
        super().keyPressEvent(event)

    # Source -----------------------------------------------------------
    def _refresh_decode_width(self):
        if not self._preview_mode or self._playing():
            return
        if self._viewer_decode_width() > self._preview._max_w:
            self._preview.set_source(
                self._preview.source,
                self._preview_path_for_frame,
                self._viewer_decode_width(),
                self._preview_info.get("pa", 1.0),
                decoder=self._preview_decoder,
            )
            if self._view_frame is not None:
                self._request_around(self._view_frame, 0)

    def _viewer_decode_width(self):
        try:
            ratio = float(self.canvas.devicePixelRatioF())
        except Exception:
            ratio = 1.0
        return bucket_width(max(320, self.canvas.width()) * ratio)

    def _snapshot_read(self, read):
        """Copy the Read knobs the viewer needs (main thread, once per settle)."""
        def value(name, default=""):
            k = read.knob(name)
            if k is None:
                return default
            try:
                return k.value()
            except Exception:
                return default

        candidates = [value("file")]
        try:
            candidates.append(nuke.filename(read))
        except Exception:
            pass
        try:
            project = nuke.root()["project_directory"].value()
        except Exception:
            project = ""
        frame_range = source_frame_range(read)
        if not frame_range:
            frame = self._view_frame if self._view_frame is not None else int(nuke.frame())
            frame_range = (frame, frame)
        try:
            pa = float(read.format().pixelAspect())
        except Exception:
            pa = 1.0
        return ReadSnapshot(
            choose_pattern(candidates, project) or "",
            frame_range[0],
            frame_range[1],
            frame_offset=value("frame"),
            frame_mode=value("frame_mode"),
            before=value("before", "hold"),
            after=value("after", "hold"),
            colorspace=value("colorspace", "default"),
            raw=value("raw", False),
            width=read.width(),
            height=read.height(),
            pixel_aspect=pa,
        )

    def _upstream_chain(self, read):
        """[(class, disabled)] for the nodes between AutoLens and its Read."""
        def name(node):
            try:
                return node.fullName()
            except Exception:
                return id(node)

        chain = []
        node = self.controller_node.input(0) if self.controller_node.inputs() else None
        target = name(read)
        while node is not None and name(node) != target and len(chain) < 500:
            try:
                cls = node.Class()
            except Exception:
                cls = "?"
            disabled = False
            try:
                k = node.knob("disable")
                disabled = bool(k.value()) if k is not None else False
            except Exception:
                pass
            chain.append((cls, disabled))
            node = node.input(0) if node.inputs() else None
        return chain

    def _ensure_frame_server(self, python):
        pool = self._frame_server
        if pool is not None and pool.python == python:
            return pool
        if pool is not None:
            pool.close()
        self._frame_server = FrameServerPool(python, size=self._preview.workers)
        return self._frame_server

    def _setup_preview_source(self, read, force=False):
        """Point the viewer at the Read's own files. Renders nothing.

        Like Mocha, frames are read straight from the files on disk: JPEG,
        PNG and TIFF by Qt on background threads, EXR and DPX by frame-server
        processes in AutoLens's managed Python, which send back viewer-sized
        8-bit sRGB. Nothing is re-encoded or written.
        """
        snap = self._snapshot_read(read)
        note = upstream_note(self._upstream_chain(read))
        note = f" • {note}" if note else ""
        signature = snap.key()
        max_w = self._viewer_decode_width()
        if not force and signature == self._preview_signature and self._preview_mode:
            self._preview_note = note + self._preview_info.get("colour_note", "")
            self._preview.set_source(
                self._preview.source,
                self._preview_path_for_frame,
                max_w,
                snap.pixel_aspect,
                decoder=self._preview_decoder,
            )
            return

        self._read_snapshot = snap
        self._preview_signature = signature
        info = {
            "width": snap.width,
            "height": snap.height,
            "pa": snap.pixel_aspect,
            "colour_note": "",
            "source": "",
        }
        ext = snap.extension
        mode, decoder, problem = None, None, ""
        if not snap.pattern:
            problem = (
                "The viewer can't resolve this Read's file path (an expression, "
                "or a relative path without a project directory)."
            )
        elif ext in self._qt_formats:
            mode, decoder = "qt", decode_image
            info["source"] = "read directly"
        elif ext in WORKER_EXTS:
            configured = ""
            try:
                configured = self.controller_node["autolens_python"].value()
            except Exception:
                pass
            python = managed_python(configured)
            if python is None:
                problem = (
                    f"AutoLens's Python runtime isn't installed, so .{ext} plates "
                    "can't be shown in the viewer (JPEG/PNG/TIFF can). "
                    "Set it up from the Solve tab."
                )
            else:
                transfer, exact = transfer_for(snap.colorspace, snap.raw, ext)
                mode = "server"
                decoder = worker_decoder(self._ensure_frame_server(python), transfer)
                info["source"] = f"read directly, {transfer} → sRGB"
                if not exact:
                    info["colour_note"] = (
                        f" • colour approximated for '{snap.colorspace}'"
                    )
        else:
            problem = f"The viewer can't read .{ext} files directly."

        self._preview_mode = mode
        self._preview_decoder = decoder
        self._preview_problem = problem
        self._preview_note = note + info["colour_note"]
        self._preview_info = info
        self._preview.set_source(
            ("plate", signature, mode),
            self._preview_path_for_frame,
            max_w,
            snap.pixel_aspect,
            decoder=decoder,
        )
        if problem:
            self.workspace_status.setText(problem)
        self._update_cache_bar()

    def _preview_path_for_frame(self, frame):
        snap = self._read_snapshot
        if snap is None or not self._preview_mode:
            return None
        return snap.path(frame)

    # Frames -----------------------------------------------------------
    def _show_frame(self, frame, settle=False, direction=0, playing=False):
        """Put `frame` on screen as fast as possible.

        In memory: shown now. Otherwise the nearest frame in memory stands in
        (held while playing) and the exact frame is swapped in as soon as it
        has been read from disk. Never renders anything.
        """
        frame = clamp_frame(
            frame,
            self.timeline.first_frame,
            self.timeline.last_frame,
        )
        self._view_frame = frame
        self.timeline.set_current_frame(frame)
        if self.frame_field.value() != frame:
            self.frame_field.blockSignals(True)
            self.frame_field.setValue(frame)
            self.frame_field.blockSignals(False)
        self.canvas.set_current_frame(frame)
        self._preview.protect = {frame}

        pixmap = self._preview.pixmap(frame)
        if pixmap is not None:
            self._display_pixmap(pixmap, frame)
        else:
            self._view_exact = False
            stand_in = None
            if playing:
                stand_in = self._displayed_frame
            elif self._preview_mode:
                stand_in = nearest_cached(
                    frame, self._preview.frames(), direction
                )
                if stand_in is not None:
                    self._display_pixmap(self._preview.pixmap(stand_in), stand_in)
            label = f"Frame {frame}"
            if stand_in is not None and stand_in != frame:
                label += f"  (showing {stand_in})"
            self.frame_label.setText(label)

        self._request_around(frame, direction)

        if playing:
            return
        if settle:
            self._settle_frame(frame)
        else:
            self._settle_timer.start()

    def _display_pixmap(self, pixmap, frame):
        info = self._preview_info
        self.canvas.set_image(
            pixmap,
            info.get("width", 1),
            info.get("height", 1),
            pixel_aspect=info.get("pa", 1.0),
        )
        self._displayed_frame = frame
        self._view_exact = frame == self._view_frame
        if self._view_exact:
            self.frame_label.setText(f"Frame {frame}")
            if self._playing():
                self._play_shown += 1

    def _request_around(self, frame, direction):
        if not self._preview_mode:
            return
        capacity = self._preview.capacity_frames(
            self._preview_info.get("width", 1),
            self._preview_info.get("height", 1),
        )
        if self._playing():
            ahead = max(2, min(96, int(capacity * 0.6)))
            behind = max(1, min(16, int(capacity * 0.15)))
        else:
            ahead = max(1, min(48, int(capacity * 0.4)))
            behind = max(1, min(16, int(capacity * 0.15)))
        loop = self._playing() and self.loop_button.isChecked()
        order = [frame] + prefetch_order(
            frame,
            self.timeline.first_frame,
            self.timeline.last_frame,
            direction,
            ahead,
            behind,
            loop=loop,
        )
        self._preview.request(order)

    def _settle_frame(self, frame, refresh_surfaces=True):
        """Bring Nuke and the rest of the workspace onto the shown frame."""
        self._settle_timer.stop()
        if frame is None or self._playing():
            return
        frame = int(frame)
        if frame != self._view_frame:
            self._show_frame(frame, settle=False)
            self._settle_timer.stop()

        try:
            if int(nuke.frame()) != frame:
                nuke.frame(frame)
        except Exception:
            pass

        # Pick up a newly connected Read or edited Read knobs.
        read = source_read_for_node(self.controller_node) or self.read_node
        if read is not None:
            changed = self.read_node is None or self.read_node.fullName() != read.fullName()
            self.read_node = read
            self._setup_preview_source(read, force=changed)

        if refresh_surfaces:
            self._reload_surfaces()
            if self.sidebar.currentIndex() == self.masks_tab_index:
                self._refresh_mask_preview()

        pixmap = self._preview.pixmap(frame)
        if pixmap is not None:
            self._display_pixmap(pixmap, frame)
            self._sharp_timer.start()
        self._request_around(frame, 0)
        self._update_cache_bar()
        self._update_frame_status()

    def _update_frame_status(self):
        if self._preview_problem:
            self.workspace_status.setText(self._preview_problem)
            return
        if not self._preview_mode:
            return
        info = self._preview_info
        frame = self._view_frame
        if not self._view_exact:
            path = self._preview_path_for_frame(frame)
            error = self._preview.errors.get(frame)
            if path is None:
                text = f"Frame {frame} is outside the Read's frames (black)."
            elif error:
                text = f"Frame {frame} couldn't be read: {error}"
            elif frame in self._preview.failed:
                text = f"Frame {frame} couldn't be read: {path}"
            else:
                text = f"Reading frame {frame}…"
            self.workspace_status.setText(text + self._preview_note)
            return
        self.workspace_status.setText(
            f"Frame {frame} • {info['width']} × {info['height']} "
            f"• PAR {info['pa']:g} • {info['source']}{self._preview_note}"
        )

    def _request_sharp(self):
        # Viewer-sized frames look soft when zoomed in; load the settled frame
        # at full size.
        if (
            self._preview_mode
            and not self._playing()
            and self.canvas.zoom > 1.25
            and self._view_frame is not None
        ):
            full = self._preview.full
            if full is not None and full[0] == self._view_frame:
                self._preview_full_ready(self._view_frame)
            else:
                self._preview.request_full(self._view_frame)

    def _preview_full_ready(self, frame):
        full = self._preview.full
        if (
            full is not None
            and frame == self._view_frame
            and not self._playing()
            and self._view_exact
        ):
            self._display_pixmap(full[1], frame)

    def _preview_frames_ready(self, frames):
        if self._view_frame in frames and not self._view_exact:
            self._display_pixmap(self._preview.pixmap(self._view_frame), self._view_frame)
            if not self._playing() and not self._settle_timer.isActive():
                self._update_frame_status()
        self._update_cache_bar()

    def _preview_frame_failed(self, frame):
        if frame == self._view_frame and not self._playing():
            self._update_frame_status()

    def _update_cache_bar(self):
        frames = self._preview.frames()
        self.timeline.set_cached_ranges(contiguous_runs(frames), ())
        used = self._preview.cache.total_bytes / (1024.0 * 1024.0)
        self.cache_label.setText(f"{len(frames)} in memory • {used:.0f} MB")

    # Transport --------------------------------------------------------
    def _playing(self):
        return self._play_timer.isActive()

    def _toggle_playback(self, direction=0):
        """direction 0 (Space): pause, or play forwards. A play button
        pauses its own direction and reverses the other."""
        if self._playing():
            same = direction == 0 or (
                self._play_clock is not None
                and self._play_clock.direction == (1 if direction > 0 else -1)
            )
            self._stop_playback(settle=same)
            if same:
                return
        self._start_playback(direction or 1)

    def _start_playback(self, direction=1):
        if self._view_frame is None or not self._preview_mode:
            return
        self._settle_timer.stop()
        fps = float(self.fps_spin.value())
        self._play_clock = PlaybackClock(
            fps,
            self.timeline.first_frame,
            self.timeline.last_frame,
            loop=self.loop_button.isChecked(),
            direction=direction,
        )
        frame = self._play_clock.start(self._view_frame)
        self._play_shown = 0
        self._play_held = 0
        self._play_stats_time = time.monotonic()
        # Tick at twice the frame rate so frames land close to on time.
        self._play_timer.start(max(1, int(500.0 / fps)))
        self._sync_play_buttons()
        self._show_frame(frame, direction=direction, playing=True)

    def _stop_playback(self, settle=True):
        if not self._playing():
            return
        self._play_timer.stop()
        self._sync_play_buttons()
        if settle:
            self._settle_frame(self._view_frame)

    def _playback_tick(self):
        if not self.isVisible() or self._play_clock is None:
            self._stop_playback(settle=False)
            return
        clock = self._play_clock
        target, finished = clock.target()
        frame, rebase = playback_step(
            self._view_frame,
            target,
            lambda f: self._preview.pixmap(f) is not None,
            clock.direction,
            self.timeline.first_frame,
            self.timeline.last_frame,
            loop=clock.loop,
        )
        if rebase:
            # Decoding is behind: play every frame at decode speed, don't skip.
            clock.rebase(frame if frame is not None else self._view_frame)
            self._play_held += 1
        if frame is not None:
            self._show_frame(frame, direction=clock.direction, playing=True)
        elif rebase:
            # Keep prefetch pointed at what playback needs next.
            self._request_around(self._view_frame, clock.direction)

        now = time.monotonic()
        if now - self._play_stats_time >= 1.0:
            shown = self._play_shown / (now - self._play_stats_time)
            self.workspace_status.setText(
                f"Playing {self._play_clock.fps:g} fps • showing {shown:.1f} fps"
                + (" • waiting for frames to load" if self._play_held else "")
            )
            self._play_shown = 0
            self._play_held = 0
            self._play_stats_time = now

        if finished and self._view_frame == target:
            self._stop_playback()

    def _sync_play_buttons(self):
        playing = self._playing()
        direction = self._play_clock.direction if self._play_clock else 1
        self.play_button.setIcon(icons.icon("pause" if playing and direction > 0 else "play"))
        self.play_back_button.setIcon(icons.icon("pause" if playing and direction < 0 else "play_back"))

    def _frame_field_changed(self, frame):
        if frame == self._view_frame:
            return
        self._stop_playback(settle=False)
        self._show_frame(int(frame), settle=True)

    def _step(self, delta):
        self._stop_playback(settle=False)
        if self._view_frame is None:
            return
        frame = step_frame(
            self._view_frame,
            delta,
            self.timeline.first_frame,
            self.timeline.last_frame,
        )
        # Settles after a short pause, so holding a key steps smoothly.
        self._show_frame(frame, direction=1 if delta > 0 else -1)

    def _go_to_end(self, which):
        self._stop_playback(settle=False)
        frame = self.timeline.last_frame if which > 0 else self.timeline.first_frame
        self._show_frame(frame, settle=True)

    def _clear_frame_cache(self):
        self._stop_playback(settle=False)
        self._preview.cancel_all()
        self._preview.cache.clear()
        self._preview.failed.clear()
        self._preview.errors.clear()
        self._preview.full = None
        self._update_cache_bar()
        self._settle_frame(self._view_frame)

    def _shutdown_preview(self, *_args):
        try:
            self._play_timer.stop()
            self._settle_timer.stop()
            self._sharp_timer.stop()
            self._preview.shutdown()
            if self._frame_server is not None:
                self._frame_server.close()
                self._frame_server = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Surface editor
    # ------------------------------------------------------------------
    def _reload_surfaces(self, keep_selection=True):
        selected_name = None

        if (
            keep_selection
            and 0
            <= self.canvas.selected_surface
            < len(self.canvas.surfaces)
        ):
            selected_name = self.canvas.surfaces[
                self.canvas.selected_surface
            ].get("name")

        current_frame = int(
            nuke.frame()
        )
        surfaces = surface_guides_data(
            self.controller_node,
            frame=current_frame,
        )

        selected = -1
        if selected_name:
            for index, surface in enumerate(
                surfaces
            ):
                if (
                    surface.get("name")
                    == selected_name
                ):
                    selected = index
                    break

        self.canvas.set_surfaces(
            surfaces,
            selected=selected,
            current_frame=current_frame,
        )
        self._populate_surface_list(
            selected
        )

    def _populate_surface_list(self, selected=-1):
        self._syncing_list = True
        try:
            self.surface_list.clear()

            for surface in self.canvas.surfaces:
                guide_frame = int(
                    surface.get(
                        "guide_frame",
                        self.canvas.current_frame,
                    )
                )
                on_guide_frame = (
                    guide_frame
                    == int(
                        self.canvas.current_frame
                    )
                )

                item = QtWidgets.QListWidgetItem(
                    f"{surface['name']}   •   F{guide_frame}"
                )
                color = _surface_color(
                    surface["name"],
                    False,
                    on_guide_frame,
                )
                item.setForeground(
                    QtGui.QBrush(color)
                )
                self.surface_list.addItem(
                    item
                )

            if 0 <= selected < self.surface_list.count():
                self.surface_list.setCurrentRow(
                    selected
                )
                self.surface_name_edit.setText(
                    self.canvas.surfaces[
                        selected
                    ]["name"]
                )
            else:
                self.surface_name_edit.clear()
        finally:
            self._syncing_list = False

    def _commit_surfaces(self):
        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            return

        replace_surface_guides(
            self.canvas.surfaces,
            source_read=read,
            controller=self.controller_node,
            frame=int(nuke.frame()),
        )

        self.panel._update_guide_status()
        self._refresh_timeline()

    def _canvas_surfaces_changed(self, surfaces):
        QtCore.QTimer.singleShot(0, self._refresh_workflow)
        self._commit_surfaces()

    def _canvas_selection_changed(self, index):
        if self._syncing_list:
            return

        self._syncing_list = True
        try:
            if 0 <= index < self.surface_list.count():
                self.surface_list.setCurrentRow(index)
                self.surface_name_edit.setText(
                    self.canvas.surfaces[index]["name"]
                )
            else:
                self.surface_list.setCurrentRow(-1)
                self.surface_name_edit.clear()
        finally:
            self._syncing_list = False

    def _surface_list_changed(self, row):
        if self._syncing_list:
            return

        self.canvas.selected_surface = row
        if 0 <= row < len(self.canvas.surfaces):
            self.surface_name_edit.setText(
                self.canvas.surfaces[row]["name"]
            )
        else:
            self.surface_name_edit.clear()

        if 0 <= row < len(self.canvas.surfaces):
            guide_frame = int(
                self.canvas.surfaces[
                    row
                ].get(
                    "guide_frame",
                    nuke.frame(),
                )
            )
            self.surface_status.setText(
                "Selected guide frame: "
                f"{guide_frame}.  "
                "Set Guide Frame changes only this surface."
            )
        else:
            self.surface_status.setText(
                "Select a surface to inspect or change its guide frame."
            )

        self.canvas.update()

    def _add_surface(self):
        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            return

        width = float(read.width())
        height = float(read.height())

        existing_names = {
            surface["name"]
            for surface in self.canvas.surfaces
        }

        index = 1
        while f"Surface_{index:02d}" in existing_names:
            index += 1

        name = f"Surface_{index:02d}"

        # A centered editable quadrilateral.
        points = [
            (width * 0.34, height * 0.34),
            (width * 0.66, height * 0.34),
            (width * 0.66, height * 0.66),
            (width * 0.34, height * 0.66),
        ]

        surfaces = list(self.canvas.surfaces)
        surfaces.append({
            "name": name,
            "points": points,
            "guide_frame": int(
                nuke.frame()
            ),
        })

        self.canvas.set_surfaces(
            surfaces,
            selected=len(surfaces) - 1,
        )
        self._populate_surface_list(len(surfaces) - 1)
        self._commit_surfaces()

    def _delete_surface(self):
        index = self.canvas.selected_surface
        if index < 0 or index >= len(self.canvas.surfaces):
            return

        surfaces = list(self.canvas.surfaces)
        del surfaces[index]

        new_index = min(index, len(surfaces) - 1)
        self.canvas.set_surfaces(
            surfaces,
            selected=new_index,
        )
        self._populate_surface_list(new_index)
        self._commit_surfaces()

    def _rename_surface(self):
        index = self.canvas.selected_surface
        if index < 0 or index >= len(self.canvas.surfaces):
            return

        name = self.surface_name_edit.text().strip()
        if not name:
            return

        self.canvas.surfaces[index]["name"] = name
        self._populate_surface_list(index)
        self.canvas.update()
        self._commit_surfaces()

    def _auto_suggest(self):
        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            QtWidgets.QMessageBox.information(
                self,
                "AutoLens",
                "No upstream Read node was found."
            )
            return

        self.workspace_status.setText(
            "Analyzing current frame for room and object planes…"
        )
        QtWidgets.QApplication.setOverrideCursor(
            QtCore.Qt.WaitCursor
        )

        try:
            result = auto_suggest_surfaces(
                read,
                controller=self.controller_node,
            )
            self._reload_surfaces(keep_selection=False)

            room_count = len(result.get("room_created", []))
            object_count = len(result.get("object_created", []))

            self.workspace_status.setText(
                f"Auto Suggest: {room_count} room surface(s), "
                f"{object_count} object plane(s). "
                "Drag the corners to correct them."
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "Auto Suggest Surfaces",
                str(exc),
            )
            self.workspace_status.setText("Auto Suggest failed.")
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _set_guide_frame(self):
        read = (
            self.read_node
            or source_read_for_node(
                self.controller_node
            )
        )
        if read is None:
            return

        index = self.canvas.selected_surface
        if (
            index < 0
            or index >= len(
                self.canvas.surfaces
            )
        ):
            self.workspace_status.setText(
                "Select a surface first, then choose Set Guide Frame."
            )
            return

        ensure_guide_node(
            read,
            self.controller_node,
        )

        surface = self.canvas.surfaces[
            index
        ]
        surface_name = str(
            surface.get("name")
            or f"Surface_{index + 1:02d}"
        )

        requested = int(
            nuke.frame()
        )
        frame = set_surface_guide_frame(
            surface_name,
            requested,
            controller=self.controller_node,
            source_read=read,
        )

        surface["guide_frame"] = int(
            frame
        )
        surface["is_guide_frame"] = (
            int(nuke.frame())
            == int(frame)
        )

        # Persist the selected surface's updated metadata alongside all other
        # guides without moving their frames.
        self._commit_surfaces()

        self.frame_label.setText(
            f"Frame {int(nuke.frame())}"
        )

        if frame != requested:
            frame_range = source_frame_range(
                read
            )
            self.workspace_status.setText(
                f"{surface_name} guide frame {requested} "
                f"was outside the Read range; AutoLens "
                f"moved it to {frame} "
                f"({frame_range[0]}–{frame_range[1]})."
            )
        else:
            self.workspace_status.setText(
                f"{surface_name} guide frame set to {frame}. "
                "Other surfaces were left unchanged."
            )

        self._reload_surfaces()
        self._refresh_timeline()


    def _open_roto(self):
        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            return

        guide = ensure_guide_node(
            read,
            controller=self.controller_node,
        )
        nuke.show(guide)

    # ------------------------------------------------------------------
    # Background solve mode
    # ------------------------------------------------------------------
    def _ensure_background_monitor(self):
        if self.background_monitor is None:
            # Deliberately make this a top-level tool window. Parenting it to
            # the hidden AutoLens dialog would hide it along with its parent.
            self.background_monitor = AutoLensBackgroundMonitor(
                self,
                parent=None,
            )
        return self.background_monitor

    def enter_background_mode(self):
        if not self.panel.runner.is_running():
            return

        monitor = self._ensure_background_monitor()

        # Synchronize with the full panel's current status immediately.
        monitor.progress.setValue(
            int(self.panel.progress.value())
        )
        monitor.stage_label.setText(
            self.panel.stage_label.text()
        )
        monitor.cancel_button.setEnabled(True)

        monitor.show()
        monitor.raise_()
        monitor.activateWindow()

        # Hide rather than destroy: the panel owns AutoLensRunner/QProcess.
        self.hide()

    def changeEvent(self, event):
        try:
            window_state_change = getattr(
                QtCore.QEvent,
                "WindowStateChange",
                getattr(
                    getattr(QtCore.QEvent, "Type", object()),
                    "WindowStateChange",
                    None,
                ),
            )

            if (
                window_state_change is not None
                and event.type() == window_state_change
                and self.isMinimized()
                and self.panel.runner.is_running()
            ):
                # Once Qt finishes the minimize event, switch to the compact
                # background notification automatically.
                QtCore.QTimer.singleShot(
                    0,
                    self.enter_background_mode,
                )
        except Exception:
            pass

        super().changeEvent(event)

    # ------------------------------------------------------------------
    # Run status
    # ------------------------------------------------------------------
    def _progress(self, percent, stage):
        set_status(
            self.controller_node,
            stage,
            percent,
        )
        self.workspace_status.setText(
            self.panel.runner.progress_text() or f"{stage} — {int(percent)}%"
        )
        self._last_percent = int(percent)
        self._refresh_workflow()

    def _finished(self, exit_code):
        if exit_code == 0:
            set_status(
                self.controller_node,
                "Complete",
                100,
            )
            self.workspace_status.setText("Track complete.")
            # Track -> Draw -> Output: move on once the track lands. A
            # geometry build or an auto Create All goes straight to Output.
            if self._step == "track":
                surfaces = len(self.canvas.surfaces or [])
                created = bool(getattr(self.panel, "_imported", False))
                self._set_step("output" if created else ux.next_step_after_solve(surfaces))
            elif self._step == "draw" and getattr(self.panel, "_imported", False):
                self._set_step("output")
        else:
            set_status(
                self.controller_node,
                "Stopped / failed",
            )
            failure = getattr(self.panel, "last_failure", None)
            if failure:
                self.workspace_status.setText(
                    f"{failure['title']}. {failure['next_step']}"
                )
            else:
                self.workspace_status.setText(
                    "AutoLens stopped or failed. See View > Log."
                )
        self._last_percent = 0
        self._set_step(self._step)

    def closeEvent(self, event):
        if self.panel.runner.is_running():
            choice = QtWidgets.QMessageBox.question(
                self,
                "AutoLens is running",
                "AutoLens is still computing. Move it to the background and "
                "keep working in Nuke?",
                QtWidgets.QMessageBox.Yes
                | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            )

            if choice == QtWidgets.QMessageBox.Yes:
                self.enter_background_mode()
                event.ignore()
                return

            # No means keep the full AutoLens window open. Cancelling the solve
            # remains an explicit action through the Cancel button.
            event.ignore()
            return

        if self.background_monitor is not None:
            try:
                self.background_monitor.hide()
                self.background_monitor.deleteLater()
            except Exception:
                pass
            self.background_monitor = None

        _DIALOGS.pop(
            self.controller_node.fullName(),
            None,
        )
        super().closeEvent(event)


def show_autolens_dialog(controller_node):
    if controller_node is None:
        nuke.message("AutoLens controller node is missing.")
        return None

    key = controller_node.fullName()
    existing = _DIALOGS.get(key)

    if existing is not None:
        existing.showMaximized()
        existing.raise_()
        existing.activateWindow()
        return existing

    try:
        with timed("dialog: construct"):
            dialog = AutoLensDialog(controller_node)
    except Exception as exc:
        # Include the exception type; much more useful than Nuke's generic popup.
        import traceback
        details = traceback.format_exc()
        nuke.tprint("[AutoLens] Failed to open workspace:\n" + details)
        nuke.message(
            "AutoLens could not open the workspace.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "The full traceback was printed to Nuke's Script Editor."
        )
        return None

    _DIALOGS[key] = dialog
    with timed("dialog: show window"):
        dialog.showMaximized()
        dialog.raise_()
        dialog.activateWindow()

    # Apply the preferred sidebar width after the maximize/layout pass. Qt can
    # otherwise preserve the constructor-time splitter ratio.
    try:
        QtCore.QTimer.singleShot(
            0,
            lambda d=dialog: d.main_splitter.setSizes([
                max(700, d.width() - 310),
                310,
            ]),
        )
    except Exception:
        pass

    # Plate preview, surfaces, masks and timeline, once the window is up.
    # A no-op when AUTOLENS_DEFER_PREVIEW=0 already loaded them.
    def _deferred_initial_load(d=dialog):
        try:
            QtWidgets.QApplication.processEvents()
            d._initial_load()
        except Exception as exc:
            if "already deleted" in str(exc):
                # The window was closed before the timer fired.
                return
            import traceback
            nuke.tprint(
                "[AutoLens] Workspace preview failed to load:\n"
                + traceback.format_exc()
            )

    QtCore.QTimer.singleShot(0, _deferred_initial_load)

    return dialog
