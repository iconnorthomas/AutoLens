"""
autolens.icons: painted icons for the workspace, so there are no image files
to ship. Same family as Match's transport icons: 20 px, drawn at 2x for
high-DPI screens, one flat colour, with a dimmed copy for the disabled state.

    icon("track")              # QIcon, normal + disabled states
    icon("track", "#27cfe8")   # tinted

Works with PySide2 (Nuke 14-15) and PySide6 (Nuke 16+).
"""
from __future__ import annotations

import math

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PySide2 import QtCore, QtGui

SIZE = 20       # logical px
FG = "#c9ced4"
OFF = "#4d535a"
_CACHE = {}


def _enum(owner, scoped, name):
    """Qt enum value that works for both Qt5 (flat) and Qt6 (scoped) names."""
    value = getattr(owner, name, None)
    if value is None:
        value = getattr(getattr(owner, scoped), name)
    return value


NO_PEN = _enum(QtCore.Qt, "PenStyle", "NoPen")
NO_BRUSH = _enum(QtCore.Qt, "BrushStyle", "NoBrush")
ROUND_CAP = _enum(QtCore.Qt, "PenCapStyle", "RoundCap")
ROUND_JOIN = _enum(QtCore.Qt, "PenJoinStyle", "RoundJoin")
ANTIALIAS = _enum(QtGui.QPainter, "RenderHint", "Antialiasing")
MODE_NORMAL = _enum(QtGui.QIcon, "Mode", "Normal")
MODE_DISABLED = _enum(QtGui.QIcon, "Mode", "Disabled")


def _pt(x, y):
    return QtCore.QPointF(x, y)


def _fill(p, colour):
    p.setPen(NO_PEN)
    p.setBrush(QtGui.QColor(colour))


def _stroke(p, colour, width=1.6):
    pen = QtGui.QPen(QtGui.QColor(colour), width)
    pen.setCapStyle(ROUND_CAP)
    pen.setJoinStyle(ROUND_JOIN)
    p.setPen(pen)
    p.setBrush(NO_BRUSH)


def _poly(p, points):
    p.drawPolygon(QtGui.QPolygonF([_pt(x, y) for x, y in points]))


def _line(p, x0, y0, x1, y1):
    p.drawLine(_pt(x0, y0), _pt(x1, y1))


def _letter_t(p, cx, cy, colour, h=7.0):
    """A bold T centred on (cx, cy), cut out of the track arrow."""
    _fill(p, colour)
    w, bar = h * 0.9, h * 0.26
    p.drawRect(QtCore.QRectF(cx - w / 2, cy - h / 2, w, bar))
    p.drawRect(QtCore.QRectF(cx - bar / 2, cy - h / 2, bar, h))


def _draw(p, name, fg):
    s = float(SIZE)
    c = s / 2
    hole = "#15171a"

    # Transport (filled, as in Match).
    if name in ("track", "track_back"):
        _fill(p, fg)
        if name == "track":
            _poly(p, [(2.5, 2.5), (s - 1.5, c), (2.5, s - 2.5)])
            tx = 7.6
        else:
            _poly(p, [(s - 2.5, 2.5), (1.5, c), (s - 2.5, s - 2.5)])
            tx = s - 7.6
        _letter_t(p, tx, c, hole, 7.4)
    elif name == "stop":
        _fill(p, fg)
        p.drawRoundedRect(QtCore.QRectF(4.5, 4.5, s - 9, s - 9), 1.5, 1.5)
    elif name == "play":
        _fill(p, fg)
        _poly(p, [(6, 4), (s - 4, c), (6, s - 4)])
    elif name == "play_back":
        _fill(p, fg)
        _poly(p, [(s - 6, 4), (4, c), (s - 6, s - 4)])
    elif name == "pause":
        _fill(p, fg)
        p.drawRect(QtCore.QRectF(5.5, 4.5, 3.2, s - 9))
        p.drawRect(QtCore.QRectF(s - 8.7, 4.5, 3.2, s - 9))
    elif name == "next":
        _fill(p, fg)
        _poly(p, [(7, 5), (s - 6, c), (7, s - 5)])
    elif name == "prev":
        _fill(p, fg)
        _poly(p, [(s - 7, 5), (6, c), (s - 7, s - 5)])
    elif name == "last":
        _fill(p, fg)
        _poly(p, [(4.5, 5), (s - 7.5, c), (4.5, s - 5)])
        p.drawRect(QtCore.QRectF(s - 6.5, 5, 2.2, s - 10))
    elif name == "first":
        _fill(p, fg)
        _poly(p, [(s - 4.5, 5), (7.5, c), (s - 4.5, s - 5)])
        p.drawRect(QtCore.QRectF(4.3, 5, 2.2, s - 10))

    # Tools (outlined).
    elif name == "plane":
        # A plane in perspective with a + : add a surface.
        _stroke(p, fg)
        _poly(p, [(2.5, 14.5), (6.5, 6.5), (14.5, 6.5), (12.5, 14.5)])
        _line(p, 15.5, 1.5, 15.5, 7.5)
        _line(p, 12.5, 4.5, 18.5, 4.5)
    elif name == "wand":
        # Auto suggest: a wand with a sparkle.
        _stroke(p, fg, 1.8)
        _line(p, 3, 17, 12, 8)
        _stroke(p, fg, 1.3)
        for dx, dy in ((0, -3), (0, 3), (-3, 0), (3, 0)):
            _line(p, 15, 5, 15 + dx, 5 + dy)
        _line(p, 7.5, 3, 7.5, 5.5)
        _line(p, 6.25, 4.25, 8.75, 4.25)
    elif name == "flag":
        # Guide frame: a flag on a pole.
        _stroke(p, fg)
        _line(p, 5, 2.5, 5, 17.5)
        _fill(p, fg)
        _poly(p, [(5.8, 3), (15.5, 5.8), (5.8, 9.5)])
    elif name == "trash":
        _stroke(p, fg)
        _line(p, 3.5, 5.5, 16.5, 5.5)
        _line(p, 8, 5.5, 8.6, 3)
        _line(p, 8.6, 3, 11.4, 3)
        _line(p, 11.4, 3, 12, 5.5)
        _poly(p, [(5, 5.5), (6, 17), (14, 17), (15, 5.5)])
        _line(p, 8.5, 8.5, 8.8, 14)
        _line(p, 11.5, 8.5, 11.2, 14)
    elif name == "gear":
        _stroke(p, fg, 1.5)
        p.drawEllipse(_pt(c, c), 2.6, 2.6)
        p.drawEllipse(_pt(c, c), 5.6, 5.6)
        _stroke(p, fg, 2.4)
        for i in range(8):
            a = i * math.pi / 4.0
            _line(p, c + 6.4 * math.cos(a), c + 6.4 * math.sin(a),
                  c + 8.2 * math.cos(a), c + 8.2 * math.sin(a))
    elif name == "mask":
        # A frame with a half-filled disc: what's tracked vs avoided.
        _stroke(p, fg, 1.4)
        p.drawRoundedRect(QtCore.QRectF(2.5, 3.5, s - 5, s - 7), 2, 2)
        p.drawEllipse(_pt(c, c), 4.2, 4.2)
        _fill(p, fg)
        path = QtGui.QPainterPath()
        path.moveTo(_pt(c, c - 4.2))
        path.arcTo(QtCore.QRectF(c - 4.2, c - 4.2, 8.4, 8.4), 90, 180)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "fit":
        _stroke(p, fg)
        for x, y, dx, dy in ((3, 3, 1, 1), (17, 3, -1, 1), (3, 17, 1, -1), (17, 17, -1, -1)):
            _line(p, x, y, x + 4 * dx, y)
            _line(p, x, y, x, y + 4 * dy)
    elif name == "loop":
        _stroke(p, fg)
        p.drawArc(QtCore.QRectF(3.5, 4.5, 13, 11), 30 * 16, 300 * 16)
        _fill(p, fg)
        _poly(p, [(13.5, 2.5), (18, 6), (12.6, 8.2)])
    elif name == "cube":
        # Geometry: an isometric cube.
        _stroke(p, fg, 1.4)
        _poly(p, [(c, 2.5), (17, 6.2), (17, 13.8), (c, 17.5), (3, 13.8), (3, 6.2)])
        _line(p, 3, 6.2, c, 10)
        _line(p, 17, 6.2, c, 10)
        _line(p, c, 10, c, 17.5)
    elif name == "create":
        # Create in the comp: a box with an arrow leaving it.
        _stroke(p, fg)
        p.drawRoundedRect(QtCore.QRectF(3, 6, 11, 11), 1.5, 1.5)
        _line(p, 9, 11, 17, 3)
        _line(p, 11.5, 3, 17, 3)
        _line(p, 17, 3, 17, 8.5)
    elif name == "folder":
        _stroke(p, fg, 1.4)
        _poly(p, [(2.5, 5), (7.5, 5), (9, 7), (17.5, 7), (17.5, 15.5), (2.5, 15.5)])
    elif name == "log":
        _stroke(p, fg)
        for y in (5.5, 10, 14.5):
            _line(p, 4, y, 16, y)
    elif name == "check":
        _stroke(p, fg, 2.0)
        _line(p, 4, 10.5, 8, 14.5)
        _line(p, 8, 14.5, 16, 5.5)
    else:
        raise KeyError(name)


NAMES = (
    "track", "track_back", "stop", "play", "play_back", "pause", "next", "prev",
    "last", "first", "plane", "wand", "flag", "trash", "gear", "mask", "fit",
    "loop", "cube", "create", "folder", "log", "check",
)


def pixmap(name, colour=FG, scale=2):
    pm = QtGui.QPixmap(SIZE * scale, SIZE * scale)
    pm.fill(QtGui.QColor(0, 0, 0, 0))
    p = QtGui.QPainter(pm)
    try:
        p.setRenderHint(ANTIALIAS)
        p.scale(scale, scale)
        _draw(p, name, colour)
    finally:
        p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name, colour=FG):
    key = (name, colour)
    if key not in _CACHE:
        ic = QtGui.QIcon()
        ic.addPixmap(pixmap(name, colour), MODE_NORMAL)
        ic.addPixmap(pixmap(name, OFF), MODE_DISABLED)
        _CACHE[key] = ic
    return _CACHE[key]


def icon_size():
    return QtCore.QSize(SIZE, SIZE)
