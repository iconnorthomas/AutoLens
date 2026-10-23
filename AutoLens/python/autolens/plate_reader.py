"""Which file the viewer reads for each frame, from a snapshot of the Read node.

The dialog copies the Read's knob values once on the main thread
(``ReadSnapshot``); everything after that is plain Python, so the viewer can
resolve file names on any thread and the tests need neither Qt nor Nuke.
Like Mocha, the viewer shows the Read's own files: nodes between the Read and
AutoLens aren't applied.
"""
from __future__ import annotations

import os
import re

# Nodes that don't change pixels between the Read and AutoLens.
PASS_THROUGH = ("Dot", "NoOp")

_HASHES = re.compile(r"#+")
_PRINTF = re.compile(r"%(0?\d*)d")


def format_pattern(pattern, frame):
    """Replace %04d / %d / #### frame tokens with ``frame``."""
    frame = int(frame)
    s = _HASHES.sub(lambda m: "%0*d" % (len(m.group()), frame), pattern)
    return _PRINTF.sub(lambda m: "%0*d" % (int(m.group(1) or 0), frame), s)


def choose_pattern(candidates, project_dir=""):
    """The first usable absolute file pattern among candidates, else None.

    Candidates are e.g. the file knob's text and nuke.filename(read). TCL
    expressions ("[...]") and %V views can't be resolved here; relative paths
    are taken relative to the script's project directory when there is one.
    """
    for value in candidates:
        value = (value or "").strip()
        if not value or "[" in value or "%v" in value.lower():
            continue
        if not os.path.isabs(value):
            if not project_dir or "[" in project_dir or not os.path.isabs(project_dir):
                continue
            value = os.path.join(project_dir, value)
        return value.replace("\\", "/")
    return None


def file_extension(pattern):
    return os.path.splitext(pattern or "")[1].lower().lstrip(".")


class ReadSnapshot:
    """The Read knob values that decide which file each frame shows."""

    def __init__(self, pattern, first, last, frame_offset="", frame_mode="",
                 before="hold", after="hold", colorspace="default", raw=False,
                 width=1, height=1, pixel_aspect=1.0):
        self.pattern = pattern
        self.first = int(first)
        self.last = int(last)
        self.frame_offset = str(frame_offset or "").strip()
        self.frame_mode = str(frame_mode or "").strip().lower()
        self.before = str(before or "hold").lower()
        self.after = str(after or "hold").lower()
        self.colorspace = str(colorspace or "default")
        self.raw = bool(raw)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.pixel_aspect = float(pixel_aspect) if pixel_aspect and pixel_aspect > 0 else 1.0

    def key(self):
        return (
            self.pattern, self.first, self.last, self.frame_offset, self.frame_mode,
            self.before, self.after, self.colorspace, self.raw,
            self.width, self.height, self.pixel_aspect,
        )

    @property
    def extension(self):
        return file_extension(self.pattern)

    def file_frame(self, frame):
        """The file frame shown at ``frame``: an int, None (black), or False (unknown)."""
        first, last = self.first, self.last
        t = int(frame)
        if self.frame_offset:
            try:
                n = int(round(float(self.frame_offset)))
            except ValueError:
                return False            # an expression: can't evaluate it here
            mode = self.frame_mode or "start at"
            if mode.startswith("start"):
                t = t - n + first
            elif mode.startswith("offset"):
                t = t - n
            else:
                return False
        if first <= t <= last:
            return t
        policy = self.before if t < first else self.after
        span = last - first + 1
        if policy.startswith("hold"):
            return first if t < first else last
        if policy.startswith("loop"):
            return first + (t - first) % span
        if policy.startswith("bounce"):
            if span == 1:
                return first
            p = (t - first) % (2 * span - 2)
            return first + (p if p < span else 2 * span - 2 - p)
        if policy.startswith("black"):
            return None
        return False

    def path(self, frame):
        """File for ``frame``, or None when it shows black or can't be worked out."""
        ff = self.file_frame(frame)
        if ff is None or ff is False:
            return None
        return format_pattern(self.pattern, ff)


def upstream_note(classes_and_disabled):
    """Viewer note for the nodes between AutoLens and its Read (Read excluded).

    ``classes_and_disabled``: [(node class, disabled)] walking up input 0.
    """
    for cls, disabled in classes_and_disabled:
        if cls in PASS_THROUGH or disabled:
            continue
        return "Viewer shows the Read's source (upstream nodes not applied)"
    return ""
