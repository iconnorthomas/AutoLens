"""
autolens.mask_cache: the tracking-mask overlay, filled in idle time so it
plays with the shot like the plate does.

Pure Python: no Qt, and `nuke` is only ever passed in. The workspace turns
the finished bytes into a QPixmap.

How the overlay is made
-----------------------
The overlay is a small (about 160 px wide) red image: alpha 20% where tracking
will avoid the plate. Each pixel is one `Node.sample(channel, x, y, 1, 1,
frame)` call on the connected mask node, with the frame passed explicitly, so
Nuke's own frame (and its viewer) never moves while the cache fills. A frame
is sampled a few rows at a time (`OverlayJob.step`) under a small time budget
per idle tick, starting at the playhead and working outward (`fill_order`),
so the UI never blocks. A Nuke render of the mask through a Write node costs
a disk round trip and a node graph change per frame, which is slower at this
size than sampling 160 x 90 pixels.

When is it stale?
-----------------
`graph_signature` hashes the mask's upstream graph: node classes, wiring and
non-default knob values including animation keys (adapted from Match's
NukeSource). Names, positions, labels and colours are ignored, so tidying the
node graph doesn't refill the cache. Re-keying a Roto does.
"""
from __future__ import annotations

import hashlib
import json
import time

from .frame_cache import contiguous_runs, nearest_cached

OVERLAY_WIDTH = 160
OPACITY = 0.20


# ---------------------------------------------------------------------------
# Overlay geometry and pixels
# ---------------------------------------------------------------------------

def overlay_size(source_width, source_height, max_width=OVERLAY_WIDTH):
    """(width, height) of the overlay for a mask of the given size."""
    source_width = int(source_width) if source_width and source_width > 0 else 1920
    source_height = int(source_height) if source_height and source_height > 0 else 1080
    width = max(32, min(int(max_width), source_width))
    height = max(18, int(round(width * float(source_height) / float(source_width))))
    return width, height


def sample_points(out_width, out_height, source_width, source_height):
    """
    Pixel-centre sample positions in the mask's raster.

    Returns (xs, ys): xs for every column, ys for every overlay row from the
    top. Nuke's y axis points up, so the first row samples near the top.
    """
    xs = [(px + 0.5) / float(out_width) * float(source_width) for px in range(out_width)]
    ys = [
        (out_height - 1 - py + 0.5) / float(out_height) * float(source_height)
        for py in range(out_height)
    ]
    return xs, ys


def row_bytes(alphas, inverted=False, opacity=OPACITY):
    """
    One overlay row as premultiplied BGRA bytes (QImage ARGB32_Premultiplied
    on little-endian machines): red, `opacity` strong where tracking avoids.
    """
    out = bytearray(4 * len(alphas))
    scale = 255.0 * float(opacity)
    i = 0
    for alpha in alphas:
        a = float(alpha)
        a = 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)
        if inverted:
            a = 1.0 - a
        v = int(scale * a + 0.5)
        # B, G stay 0; premultiplied red equals alpha for pure red.
        out[i + 2] = v
        out[i + 3] = v
        i += 4
    return bytes(out)


class OverlayJob:
    """
    One frame's overlay, sampled a few rows per `step` call.

    `sampler(x, y)` returns the mask alpha at a raster position for this
    job's frame.
    """

    def __init__(self, frame, sampler, source_width, source_height,
                 out_width=None, out_height=None, inverted=False, clock=None):
        self.frame = int(frame)
        self.sampler = sampler
        if out_width is None or out_height is None:
            out_width, out_height = overlay_size(source_width, source_height)
        self.width = int(out_width)
        self.height = int(out_height)
        self.inverted = bool(inverted)
        self._xs, self._ys = sample_points(self.width, self.height, source_width, source_height)
        self._rows = []
        self._clock = clock or time.perf_counter
        self.seconds = 0.0
        self.error = None

    @property
    def done(self):
        return self.error is not None or len(self._rows) >= self.height

    @property
    def rows_done(self):
        return len(self._rows)

    def step(self, budget_seconds=0.012):
        """Sample rows until the budget is spent (at least one row). True when finished."""
        start = self._clock()
        deadline = start + max(0.0, float(budget_seconds))
        sample = self.sampler
        xs = self._xs
        try:
            while len(self._rows) < self.height:
                y = self._ys[len(self._rows)]
                self._rows.append(row_bytes([sample(x, y) for x in xs], self.inverted))
                if self._clock() >= deadline:
                    break
        except Exception as exc:
            self.error = exc
        self.seconds += self._clock() - start
        return self.done

    def data(self):
        """The finished overlay: width * height * 4 bytes, top row first."""
        if self.error is not None:
            raise RuntimeError(f"Mask sampling failed on frame {self.frame}: {self.error}")
        if len(self._rows) < self.height:
            raise RuntimeError(f"Frame {self.frame} overlay is not finished.")
        return b"".join(self._rows)


# ---------------------------------------------------------------------------
# Which frames, in which order
# ---------------------------------------------------------------------------

def fill_order(playhead, first, last, ready=(), limit=None):
    """
    Frames still to fill, nearest the playhead first: playhead, +1, -1, +2, -2...

    Frames after the playhead come first at each distance, since playback
    usually runs forward.
    """
    first, last = int(first), int(last)
    if last < first:
        return []
    playhead = max(first, min(last, int(playhead)))
    ready = set(int(f) for f in ready)
    order = []
    span = max(playhead - first, last - playhead)
    for d in range(span + 1):
        for frame in ((playhead,) if d == 0 else (playhead + d, playhead - d)):
            if first <= frame <= last and frame not in ready:
                order.append(frame)
                if limit is not None and len(order) >= int(limit):
                    return order
    return order


class OverlayCache:
    """
    Frame -> finished overlay (any object, e.g. a QPixmap), for one mask
    signature. Behaves like the dict the workspace used before.
    """

    def __init__(self):
        self._frames = {}
        self.signature = None
        self.seconds = 0.0      # sampling time spent on the frames held
        self.filled = 0         # frames sampled since the last clear

    def __len__(self):
        return len(self._frames)

    def __bool__(self):
        return bool(self._frames)

    def __contains__(self, frame):
        return int(frame) in self._frames

    def __getitem__(self, frame):
        return self._frames[int(frame)]

    def __setitem__(self, frame, overlay):
        self._frames[int(frame)] = overlay

    def get(self, frame, default=None):
        return self._frames.get(int(frame), default)

    def clear(self):
        self._frames.clear()
        self.seconds = 0.0
        self.filled = 0

    def frames(self):
        return sorted(self._frames)

    def ranges(self):
        return contiguous_runs(self._frames)

    def add(self, frame, overlay, seconds=0.0):
        self._frames[int(frame)] = overlay
        self.seconds += float(seconds)
        self.filled += 1

    def stand_in(self, frame, direction=0):
        """The overlay for `frame`, or the nearest ready one, or None."""
        frame = int(frame)
        if frame in self._frames:
            return self._frames[frame]
        near = nearest_cached(frame, self._frames, direction)
        return None if near is None else self._frames[near]

    def update_signature(self, signature):
        """Adopt `signature`; clear everything when it differs. True if cleared."""
        if signature == self.signature:
            return False
        changed = self.signature is not None or bool(self._frames)
        self.signature = signature
        self.clear()
        return changed

    def ms_per_frame(self):
        return 1000.0 * self.seconds / self.filled if self.filled else 0.0


# ---------------------------------------------------------------------------
# Signature of the mask's upstream graph (adapted from Match's NukeSource)
# ---------------------------------------------------------------------------

# Knobs that never change pixels.
IGNORED_KNOBS = frozenset((
    "name", "xpos", "ypos", "selected", "label", "note_font", "note_font_size", "note_font_color",
    "tile_color", "gl_color", "hide_input", "postage_stamp", "postage_stamp_frame", "dope_sheet",
    "bookmark", "icon", "indicators", "cached", "help", "autolabel", "knobChanged", "onCreate",
    "onDestroy", "updateUI", "panel", "inputs", "useLifetime", "lifetimeStart", "lifetimeEnd",
    "cacheLocal", "localizationPolicy", "proxy", "proxy_format", "proxy_type", "reload",
))
# Guards against a runaway walk through a huge comp.
MAX_UPSTREAM_NODES = 400


def _input_count(node):
    try:
        return int(node.inputs())
    except Exception:
        return 0


def upstream_nodes(node, limit=MAX_UPSTREAM_NODES):
    """[(node, [input ids])] breadth-first from `node`; ids index this list."""
    order, ids = [], {id(node): 0}
    queue = [node]
    while queue and len(order) < limit:
        n = queue.pop(0)
        links = []
        for i in range(_input_count(n)):
            try:
                up = n.input(i)
            except Exception:
                up = None
            if up is None:
                links.append(None)
                continue
            if id(up) not in ids:
                ids[id(up)] = len(ids)
                queue.append(up)
            links.append(ids[id(up)])
        order.append((n, links))
    return order


def _tcl_words(text):
    """Top-level Tcl words of a knob script (braced and quoted words kept whole)."""
    words, cur, depth, quote, esc = [], [], 0, False, False
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
        elif ch == '"' and depth == 0:
            quote = not quote
        elif not quote and ch == "{":
            depth += 1
        elif not quote and ch == "}":
            depth = max(0, depth - 1)
        elif ch.isspace() and depth == 0 and not quote:
            if cur:
                words.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        words.append("".join(cur))
    return words


def _split_script(text):
    """[(name, value)] from writeKnobs text, whether entries are split by newlines or spaces."""
    words = _tcl_words(text)
    if len(words) % 2 == 0:
        return list(zip(words[0::2], words[1::2]))
    pairs = []
    for line in text.splitlines():
        w = _tcl_words(line)
        if w:
            pairs.append((w[0], " ".join(w[1:])))
    return pairs


def _knob_repr(knob):
    to_script = getattr(knob, "toScript", None)
    if to_script is not None:
        try:
            return to_script()
        except Exception:
            pass
    try:
        text = repr(knob.value())
    except Exception:
        text = "?"
    anims = getattr(knob, "animations", None)
    if anims is not None:
        try:
            text += repr([[(key.x, key.y) for key in c.keys()] for c in anims()])
        except Exception:
            pass
    return text


def node_knob_text(node, nuke=None):
    """The node's non-default, pixel-relevant knob values as sorted text lines."""
    write_knobs = getattr(node, "writeKnobs", None)
    flag = getattr(nuke, "WRITE_NON_DEFAULT_ONLY", None) if nuke is not None else None
    if write_knobs is not None and flag is not None:
        try:
            flags = flag | getattr(nuke, "TO_SCRIPT", 0)
            pairs = _split_script(write_knobs(flags) or "")
            return sorted("%s %s" % p for p in pairs if p[0] not in IGNORED_KNOBS)
        except Exception:
            pass
    lines = []
    try:
        items = sorted(node.knobs().items())
    except Exception:
        items = []
    for name, knob in items:
        if name in IGNORED_KNOBS:
            continue
        lines.append("%s %s" % (name, _knob_repr(knob)))
    return lines


def _class(node):
    try:
        return node.Class()
    except Exception:
        return "?"


def graph_signature(node, nuke=None, extra=None):
    """Hash of the upstream graph: classes, wiring and non-default knob values (no names)."""
    if node is None:
        return None
    parts = [[_class(n), links, node_knob_text(n, nuke)] for n, links in upstream_nodes(node)]
    parts.append(["extra", extra])
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()
