from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
import re

import nuke

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal

from . import __version__
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


WIDGET_WITH_CHILDREN_SHORTCUT = getattr(
    QtCore.Qt,
    "WidgetWithChildrenShortcut",
    getattr(
        getattr(QtCore.Qt, "ShortcutContext", object()),
        "WidgetWithChildrenShortcut",
        3,
    ),
)


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

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setMouseTracking(True)
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
        self.pixmap = pixmap or QtGui.QPixmap()
        self.image_width = max(1, int(width))
        self.image_height = max(1, int(height))
        try:
            pa = float(pixel_aspect)
        except Exception:
            pa = 1.0
        self.pixel_aspect = pa if pa > 0.0 else 1.0
        self.zoom = 1.0
        self.pan = QtCore.QPointF(0.0, 0.0)
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
        painter.fillRect(self.rect(), QtGui.QColor(18, 18, 18))

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

        try:
            event.accept()
        except Exception:
            pass


class GuideTimeline(QtWidgets.QWidget):
    """Compact Mocha-style shot timeline with guide-frame markers."""

    frameChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.first_frame = 1
        self.last_frame = 100
        self.current_frame = 1
        self.markers = []
        self.setMinimumHeight(52)
        self.setMaximumHeight(64)
        self.setMouseTracking(True)

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

    def _x_for_frame(self, frame):
        left = 12.0
        right = max(
            left + 1.0,
            float(self.width()) - 12.0,
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
        left = 12.0
        right = max(
            left + 1.0,
            float(self.width()) - 12.0,
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

        painter.fillRect(
            self.rect(),
            QtGui.QColor(31, 31, 31),
        )

        y = 18
        left = 12
        right = self.width() - 12

        painter.setPen(
            QtGui.QPen(
                QtGui.QColor(90, 90, 90),
                1,
            )
        )
        painter.drawLine(
            left,
            y,
            right,
            y,
        )

        frame_count = max(
            1,
            self.last_frame - self.first_frame + 1,
        )

        # Keep tick density readable while preserving the Mocha-like feel.
        tick_step = max(
            1,
            int(
                frame_count / max(
                    1,
                    self.width() // 10,
                )
            ),
        )

        painter.setPen(
            QtGui.QColor(150, 150, 150)
        )

        for frame in range(
            self.first_frame,
            self.last_frame + 1,
            tick_step,
        ):
            x = int(
                round(
                    self._x_for_frame(
                        frame
                    )
                )
            )
            painter.drawLine(
                x,
                y - 5,
                x,
                y + 5,
            )

        # Guided frames = blue marks.
        painter.setPen(
            QtGui.QPen(
                QtGui.QColor(55, 145, 255),
                3,
            )
        )

        for frame in self.markers:
            x = int(
                round(
                    self._x_for_frame(
                        frame
                    )
                )
            )
            painter.drawLine(
                x,
                y - 10,
                x,
                y + 11,
            )

        # Current frame = warm playhead.
        current_x = int(
            round(
                self._x_for_frame(
                    self.current_frame
                )
            )
        )

        painter.setPen(
            QtGui.QPen(
                QtGui.QColor(255, 145, 45),
                2,
            )
        )
        painter.drawLine(
            current_x,
            4,
            current_x,
            self.height() - 5,
        )

        painter.setPen(
            QtGui.QColor(80, 170, 255)
        )
        painter.drawText(
            QtCore.QRect(
                10,
                31,
                95,
                18,
            ),
            f"{self.current_frame}",
        )

        painter.setPen(
            QtGui.QColor(120, 120, 120)
        )
        painter.drawText(
            QtCore.QRect(
                self.width() - 180,
                31,
                168,
                18,
            ),
            getattr(
                QtCore.Qt,
                "AlignRight",
                0x0002,
            ),
            f"{self.first_frame} — {self.last_frame}",
        )

    def mousePressEvent(self, event):
        pos = (
            event.position()
            if hasattr(event, "position")
            else event.pos()
        )
        frame = self._frame_for_x(
            pos.x()
        )
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
            self.set_current_frame(frame)
            self.frameChanged.emit(frame)


class AutoLensBackgroundMonitor(QtWidgets.QDialog):
    """
    Compact non-modal progress palette used while the main AutoLens workspace
    is hidden. The actual QProcess remains owned by the live AutoLens dialog.
    """

    def __init__(self, owner, parent=None):
        super().__init__(parent)

        self.owner = owner

        self.setWindowTitle(
            f"AutoLens {__version__} - DeadCow VFX"
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
            f"<b>AutoLens {__version__}</b>"
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
            f"AutoLens {__version__} - DeadCow VFX"
        )
        self.background_monitor = None

        self.setAttribute(
            QtCore.Qt.WA_DeleteOnClose,
            True,
        )
        self.setMinimumSize(900, 620)

        self._build_ui()
        self._install_shortcuts()
        self._construct_panel()
        self._load_node_settings(self.read_node)
        self._wire_node_sync()

        self._load_preview()
        self._reload_surfaces()
        self._reload_masks()
        self._refresh_timeline()

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

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.setStyleSheet("""
            QDialog { background: #252525; color: #d7d7d7; }
            QGroupBox {
                border: 1px solid #454545;
                border-radius: 3px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QTabWidget::pane { border: 1px solid #454545; }
            QTabBar::tab {
                background: #303030;
                border: 1px solid #454545;
                padding: 7px 13px;
            }
            QTabBar::tab:selected { background: #454545; }
            QPushButton { min-height: 25px; }
            QListWidget { background: #1f1f1f; border: 1px solid #444; }
        """)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        # Toolbar
        toolbar = QtWidgets.QHBoxLayout()

        title = QtWidgets.QLabel(
            f"<b>AutoLens</b> "
            f"<span style='color:#8d8d8d;'>v{__version__}</span> "
            f"<span style='color:#666666;'>• DeadCow VFX</span>"
        )
        title.setStyleSheet("font-size: 14px;")
        toolbar.addWidget(title)

        toolbar.addSpacing(12)

        self.frame_label = QtWidgets.QLabel(f"Frame {int(nuke.frame())}")
        toolbar.addWidget(self.frame_label)

        toolbar.addStretch(1)

        self.fit_button = QtWidgets.QPushButton("Fit")
        toolbar.addWidget(self.fit_button)

        root.addLayout(toolbar)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(self.main_splitter, 1)

        # Left: viewer
        viewer_frame = QtWidgets.QFrame()
        viewer_layout = QtWidgets.QVBoxLayout(viewer_frame)
        viewer_layout.setContentsMargins(0, 0, 0, 0)

        self.canvas = SurfaceCanvas()
        viewer_layout.addWidget(self.canvas, 1)

        self.timeline = GuideTimeline()
        viewer_layout.addWidget(self.timeline)

        viewer_help = QtWidgets.QLabel(
            "Drag surface corners directly in this viewer.  "
            "Timeline: click/drag frame  •  Blue = surface guide frame  •  "
            "F: fit  •  Mouse wheel: zoom  •  Middle-drag: pan"
        )
        viewer_help.setStyleSheet("color: #888; padding: 3px;")
        viewer_layout.addWidget(viewer_help)

        self.main_splitter.addWidget(viewer_frame)

        # Right: tab sidebar
        sidebar = QtWidgets.QTabWidget()
        self.sidebar = sidebar
        sidebar.setMinimumWidth(390)
        sidebar.setMaximumWidth(560)

        # Surfaces tab
        surfaces_tab = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(surfaces_tab)
        sl.setContentsMargins(8, 8, 8, 8)

        self.surface_list = QtWidgets.QListWidget()
        sl.addWidget(self.surface_list, 1)

        name_row = QtWidgets.QHBoxLayout()
        self.surface_name_edit = QtWidgets.QLineEdit()
        self.rename_button = QtWidgets.QPushButton("Rename")
        name_row.addWidget(self.surface_name_edit, 1)
        name_row.addWidget(self.rename_button)
        sl.addLayout(name_row)

        row = QtWidgets.QHBoxLayout()
        self.surface_auto_button = QtWidgets.QPushButton("Auto Suggest")
        self.surface_add_button = QtWidgets.QPushButton("Add Plane")
        row.addWidget(self.surface_auto_button)
        row.addWidget(self.surface_add_button)
        sl.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        self.surface_delete_button = QtWidgets.QPushButton("Delete")
        self.guide_frame_button = QtWidgets.QPushButton("Set Guide Frame")
        row2.addWidget(self.surface_delete_button)
        row2.addWidget(self.guide_frame_button)
        sl.addLayout(row2)

        self.surface_status = QtWidgets.QLabel(
            "Each surface can have its own guide frame. Green = its guide frame; gray = another frame."
        )
        self.surface_status.setWordWrap(True)
        self.surface_status.setStyleSheet("color: #999;")
        sl.addWidget(self.surface_status)

        sidebar.addTab(surfaces_tab, "Surfaces")

        # Masks tab
        masks_tab = QtWidgets.QWidget()
        ml = QtWidgets.QVBoxLayout(masks_tab)
        ml.setContentsMargins(10, 10, 10, 10)

        mask_title = QtWidgets.QLabel(
            "<b>Tracking Mask Input</b>"
        )
        ml.addWidget(mask_title)

        mask_help = QtWidgets.QLabel(
            "Connect an alpha/matte to AutoLens input 2. "
            "The viewer shows the tracking exclusion region as 20% red. "
            "By default alpha marks areas AutoLens should AVOID while tracking."
        )
        mask_help.setWordWrap(True)
        mask_help.setStyleSheet("color: #aaa;")
        ml.addWidget(mask_help)

        self.mask_connection_status = QtWidgets.QLabel(
            "No mask connected."
        )
        self.mask_connection_status.setWordWrap(True)
        ml.addWidget(self.mask_connection_status)

        self.mask_mode_label = QtWidgets.QLabel(
            "Alpha meaning: AVOID / EXCLUDE"
        )
        self.mask_mode_label.setStyleSheet(
            "color: #e18aa9; font-weight: 600;"
        )
        ml.addWidget(self.mask_mode_label)

        self.mask_invert_button = QtWidgets.QPushButton(
            "Invert"
        )
        self.mask_invert_button.setCheckable(True)
        self.mask_invert_button.setToolTip(
            "Swap whether alpha means Avoid/Exclude or Keep/Track."
        )
        ml.addWidget(self.mask_invert_button)

        mask_preview_note = QtWidgets.QLabel(
            "Viewer overlay: 20% red always shows what AutoLens will AVOID."
        )
        mask_preview_note.setStyleSheet(
            "color: #888;"
        )
        mask_preview_note.setWordWrap(True)
        ml.addWidget(mask_preview_note)

        ml.addStretch(1)

        self.masks_tab_index = sidebar.addTab(
            masks_tab,
            "Masks",
        )
        # Solve tab placeholder; panel is installed after construction.
        self.solve_tab = QtWidgets.QWidget()
        self.solve_layout = QtWidgets.QVBoxLayout(self.solve_tab)
        self.solve_layout.setContentsMargins(0, 0, 0, 0)
        sidebar.addTab(self.solve_tab, "Solve")

        # The right properties area should remain comfortably readable at full
        # screen, especially on 1440p/4K displays.
        sidebar.setMinimumWidth(460)
        sidebar.setMaximumWidth(680)

        self.main_splitter.addWidget(sidebar)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setSizes([1100, 500])

        # Bottom status.
        self.workspace_status = QtWidgets.QLabel("Ready")
        self.workspace_status.setStyleSheet("color: #aaa; padding: 2px 4px;")
        root.addWidget(self.workspace_status)

        # Wiring for workspace.
        self.fit_button.clicked.connect(self.canvas.fit_view)
        self.surface_auto_button.clicked.connect(self._auto_suggest)
        self.surface_add_button.clicked.connect(self._add_surface)
        self.surface_delete_button.clicked.connect(self._delete_surface)
        self.guide_frame_button.clicked.connect(self._set_guide_frame)
        self.rename_button.clicked.connect(self._rename_surface)
        self.surface_list.currentRowChanged.connect(self._surface_list_changed)
        self.canvas.selectionChanged.connect(self._canvas_selection_changed)
        self.canvas.surfacesChanged.connect(self._canvas_surfaces_changed)

        self.timeline.frameChanged.connect(
            self._timeline_frame_changed
        )
        self.sidebar.currentChanged.connect(
            self._sidebar_tab_changed
        )
        self.mask_invert_button.toggled.connect(
            self._mask_invert_toggled
        )

    def _install_shortcuts(self):
        self.fit_shortcut = QtGui.QShortcut(
            QtGui.QKeySequence("F"),
            self,
        )
        self.fit_shortcut.setContext(
            WIDGET_WITH_CHILDREN_SHORTCUT
        )
        self.fit_shortcut.activated.connect(
            self._fit_shortcut_triggered
        )

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
        try:
            nuke.frame(
                int(frame)
            )
        except Exception:
            pass

        self.frame_label.setText(
            f"Frame {int(frame)}"
        )
        self.canvas.set_current_frame(
            int(frame)
        )
        self._load_preview()
        self._reload_surfaces()

        if (
            self.sidebar.currentIndex()
            == self.masks_tab_index
        ):
            self._refresh_mask_preview()


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
        # Make the upstream Read selected while the existing solve panel builds.
        old_selection = list(nuke.selectedNodes())
        try:
            for node in old_selection:
                node.setSelected(False)
            if self.read_node is not None:
                self.read_node.setSelected(True)

            self.panel = AutoLensPanel(self)
        finally:
            try:
                for node in nuke.selectedNodes():
                    node.setSelected(False)
                for node in old_selection:
                    node.setSelected(True)
            except Exception:
                pass

        self.panel.controller_node = self.controller_node
        self.panel.background_requested.connect(
            self.enter_background_mode
        )

        # Surface editing now lives in the permanent left viewport.
        if hasattr(self.panel, "guide_group"):
            self.panel.guide_group.hide()

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(self.panel)
        self.solve_layout.addWidget(scroll)

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

        # One final sync ensures the Setup / Run panel and controller agree on
        # the user's selected version before any result/cache state is restored.
        self.panel._sync_controller_output()
        self.panel.restore_result_state()
        self.panel._update_run_button_text()

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
        self.frame_label.setText(f"Frame {int(nuke.frame())}")

        read = self.read_node or source_read_for_node(self.controller_node)
        if read is None:
            self.workspace_status.setText("No upstream Read node.")
            self.canvas.set_image(QtGui.QPixmap(), 1, 1)
            return

        self.read_node = read

        requested_frame = int(nuke.frame())
        frame_info = clamp_frame_to_source(
            requested_frame,
            read,
        )
        frame = int(frame_info["frame"])
        if hasattr(self, "timeline"):
            self.timeline.set_current_frame(frame)


        if frame_info["clamped"]:
            try:
                nuke.frame(frame)
            except Exception:
                pass

        self.workspace_status.setText("Loading preview…")
        QtWidgets.QApplication.processEvents()

        pixmap = QtGui.QPixmap()

        # Fast path: let Qt load common image formats directly.
        old_frame = int(nuke.frame())
        filename = ""
        try:
            nuke.frame(frame)
            try:
                filename = nuke.filename(read) or read["file"].evaluate()
            except Exception:
                filename = read["file"].evaluate()
        finally:
            nuke.frame(old_frame)

        if filename and Path(filename).exists():
            pixmap = QtGui.QPixmap(filename)

        # Robust path: ask Nuke to render one temporary PNG. This also handles
        # EXR/TIFF formats Qt cannot decode itself.
        if pixmap.isNull():
            temp_path = Path(tempfile.gettempdir()) / (
                f"autolens_preview_{uuid.uuid4().hex}.png"
            )
            write = None
            try:
                input_node = self.controller_node.input(0) or read
                write = nuke.nodes.Write()
                write.setInput(0, input_node)
                write["file"].setValue(str(temp_path).replace("\\", "/"))
                if write.knob("file_type"):
                    write["file_type"].setValue("png")
                if write.knob("channels"):
                    write["channels"].setValue("rgb")

                nuke.execute(write.name(), frame, frame)
                pixmap = QtGui.QPixmap(str(temp_path))
            except Exception as exc:
                self.workspace_status.setText(
                    f"Preview render failed: {exc}"
                )
            finally:
                if write is not None:
                    try:
                        nuke.delete(write)
                    except Exception:
                        pass
                try:
                    temp_path.unlink()
                except Exception:
                    pass

        try:
            pixel_aspect = float(read.format().pixelAspect())
        except Exception:
            pixel_aspect = 1.0

        self.canvas.set_image(
            pixmap,
            int(read.width()),
            int(read.height()),
            pixel_aspect=pixel_aspect,
        )

        if pixmap.isNull():
            self.workspace_status.setText(
                "Preview unavailable; surface data is still editable."
            )
        else:
            suffix = ""
            if frame_info["clamped"]:
                suffix = (
                    f" • auto-clamped from {requested_frame} "
                    f"to source range {frame_info['first']}–{frame_info['last']}"
                )

            self.workspace_status.setText(
                f"Frame {frame} • {read.width()} × {read.height()} "
                f"• PAR {pixel_aspect:g}{suffix}"
            )

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
            f"{stage} — {int(percent)}%"
        )

    def _finished(self, exit_code):
        if exit_code == 0:
            set_status(
                self.controller_node,
                "Complete",
                100,
            )
            self.workspace_status.setText("AutoLens solve complete.")
        else:
            set_status(
                self.controller_node,
                "Stopped / failed",
            )
            self.workspace_status.setText(
                "AutoLens stopped or failed. See the Solve tab log."
            )

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
    dialog.showMaximized()
    dialog.raise_()
    dialog.activateWindow()

    # Apply the preferred sidebar width after the maximize/layout pass. Qt can
    # otherwise preserve the constructor-time splitter ratio.
    try:
        QtCore.QTimer.singleShot(
            0,
            lambda d=dialog: d.main_splitter.setSizes([
                max(700, d.width() - 500),
                500,
            ]),
        )
    except Exception:
        pass

    return dialog
