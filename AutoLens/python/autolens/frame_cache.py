"""Frame cache, prefetch ordering and playback timing for the workspace viewer.

Everything here is plain Python (no Qt, no Nuke) so the unit tests can cover it.
autolens/preview_loader.py does the Qt decoding and dialog.py the Nuke rendering.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import os
import re
import shutil
import time
from collections import OrderedDict

DEFAULT_CACHE_MB = 1024
CACHE_MB_ENV = "AUTOLENS_PREVIEW_CACHE_MB"

# Viewer-sized decodes are snapped to these widths so a small window resize
# doesn't throw the whole cache away.
WIDTH_BUCKETS = (480, 640, 960, 1280, 1600, 1920, 2560, 3200, 3840)

_PRINTF_RE = re.compile(r"%(0?)(\d*)d")
_HASH_RE = re.compile(r"#+")
_AT_RE = re.compile(r"@+")


def cache_budget_bytes(environ=None):
    """Memory budget for decoded preview frames (env AUTOLENS_PREVIEW_CACHE_MB)."""
    environ = os.environ if environ is None else environ
    try:
        mb = float(environ.get(CACHE_MB_ENV, DEFAULT_CACHE_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_CACHE_MB
    return int(max(64.0, mb) * 1024 * 1024)


class FrameCache:
    """Least-recently-used cache bounded by total bytes, not entry count."""

    def __init__(self, max_bytes):
        self.max_bytes = max(1, int(max_bytes))
        self.total_bytes = 0
        self._items = OrderedDict()   # key -> (value, nbytes)

    def __len__(self):
        return len(self._items)

    def __contains__(self, key):
        return key in self._items

    def get(self, key, default=None):
        item = self._items.get(key)
        if item is None:
            return default
        self._items.move_to_end(key)
        return item[0]

    def peek(self, key, default=None):
        """Like get() but doesn't mark the entry as recently used."""
        item = self._items.get(key)
        return default if item is None else item[0]

    def put(self, key, value, nbytes, protect=()):
        """Store value; evicts least-recently-used entries outside `protect`.

        Returns the evicted keys. An entry bigger than the whole budget is not
        stored.
        """
        nbytes = max(0, int(nbytes))
        self.discard(key)
        if nbytes > self.max_bytes:
            return []
        self._items[key] = (value, nbytes)
        self.total_bytes += nbytes
        return self._evict(protect=set(protect) | {key})

    def discard(self, key):
        item = self._items.pop(key, None)
        if item is not None:
            self.total_bytes -= item[1]

    def clear(self):
        self._items.clear()
        self.total_bytes = 0

    def keys(self):
        return list(self._items.keys())

    def _evict(self, protect):
        evicted = []
        if self.total_bytes <= self.max_bytes:
            return evicted
        for key in list(self._items.keys()):
            if self.total_bytes <= self.max_bytes:
                break
            if key in protect:
                continue
            evicted.append(key)
            self.discard(key)
        return evicted


def bucket_width(width, buckets=WIDTH_BUCKETS):
    """Smallest bucket at least `width` wide (the largest if none is)."""
    width = int(math.ceil(max(1.0, float(width))))
    for bucket in buckets:
        if bucket >= width:
            return bucket
    return buckets[-1]


def fit_size(src_w, src_h, max_w, max_h=0, pixel_aspect=1.0):
    """Decode size that fits (max_w, max_h) on screen, keeping the aspect.

    Never upscales. `pixel_aspect` widens the displayed image, so anamorphic
    plates are decoded narrower. Returns (w, h) in image pixels.
    """
    src_w = max(1, int(src_w))
    src_h = max(1, int(src_h))
    pa = float(pixel_aspect) if pixel_aspect and pixel_aspect > 0 else 1.0
    scale = 1.0
    if max_w and max_w > 0:
        scale = min(scale, float(max_w) / (src_w * pa))
    if max_h and max_h > 0:
        scale = min(scale, float(max_h) / src_h)
    return (
        max(1, int(round(src_w * scale))),
        max(1, int(round(src_h * scale))),
    )


def clamp_frame(frame, first, last):
    return max(int(first), min(int(last), int(frame)))


def step_frame(frame, delta, first, last, loop=False):
    """Frame `delta` steps away, wrapping when loop is on, else clamped."""
    first, last = int(first), int(last)
    target = int(frame) + int(delta)
    if loop:
        span = last - first + 1
        return first + (target - first) % span
    return clamp_frame(target, first, last)


def prefetch_order(current, first, last, direction=0, ahead=24, behind=8, loop=False):
    """Frames to decode next, most urgent first, excluding `current`.

    direction > 0 or < 0 (playing): frames ahead in that direction, with a few
    behind interleaved late. direction == 0 (scrubbing/paused): alternate
    either side of current so both neighbours are ready.
    """
    first, last = int(first), int(last)
    current = clamp_frame(current, first, last)
    span = last - first + 1
    order = []
    seen = {current}

    def add(frame):
        if loop:
            frame = first + (frame - first) % span
        elif frame < first or frame > last:
            return
        if frame not in seen:
            seen.add(frame)
            order.append(frame)

    if direction == 0:
        for offset in range(1, max(ahead, behind) + 1):
            if offset <= ahead:
                add(current + offset)
            if offset <= behind:
                add(current - offset)
        return order

    step = 1 if direction > 0 else -1
    # Every 4th slot (after the first few ahead) goes to a frame behind.
    behind_frames = [current - step * i for i in range(1, behind + 1)]
    for i in range(1, ahead + 1):
        add(current + step * i)
        if i >= 4 and i % 4 == 0 and behind_frames:
            add(behind_frames.pop(0))
    for frame in behind_frames:
        add(frame)
    return order


def nearest_cached(frame, cached_frames, direction=0):
    """Closest frame in `cached_frames` (any iterable), or None.

    Ties go to the frame on the side we came from (opposite `direction`),
    which is what a drag just showed.
    """
    frames = sorted(set(int(f) for f in cached_frames))
    if not frames:
        return None
    frame = int(frame)
    i = bisect.bisect_left(frames, frame)
    if i < len(frames) and frames[i] == frame:
        return frame
    below = frames[i - 1] if i > 0 else None
    above = frames[i] if i < len(frames) else None
    if below is None:
        return above
    if above is None:
        return below
    d_below, d_above = frame - below, above - frame
    if d_below == d_above:
        return above if direction < 0 else below
    return below if d_below < d_above else above


def contiguous_runs(frames):
    """Sorted (start, end) inclusive runs covering the given frames."""
    runs = []
    for frame in sorted(set(int(f) for f in frames)):
        if runs and frame == runs[-1][1] + 1:
            runs[-1][1] = frame
        else:
            runs.append([frame, frame])
    return [(a, b) for a, b in runs]


def split_runs(runs, max_len):
    """Split (start, end) runs so none is longer than max_len frames."""
    max_len = max(1, int(max_len))
    out = []
    for start, end in runs:
        while start <= end:
            stop = min(end, start + max_len - 1)
            out.append((start, stop))
            start = stop + 1
    return out


def merge_runs(runs, max_gap):
    """Join sorted (start, end) runs separated by at most max_gap frames."""
    merged = []
    for start, end in runs:
        if merged and start - merged[-1][1] - 1 <= max_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


class PlaybackClock:
    """Real-time playback: which frame should be on screen now.

    Frames are dropped rather than slowing playback down: the target frame is
    derived from wall-clock time since play started, not from a frame count.
    """

    def __init__(self, fps, first, last, loop=True, direction=1, clock=None):
        self.fps = max(1.0, float(fps))
        self.first = int(first)
        self.last = max(self.first, int(last))
        self.loop = bool(loop)
        self.direction = 1 if direction >= 0 else -1
        self._clock = clock or time.monotonic
        self.start_frame = self.first
        self.start_time = 0.0

    def start(self, frame):
        self.start_frame = clamp_frame(frame, self.first, self.last)
        # Starting on the last frame (or first, backwards) restarts the range.
        if not self.loop:
            if self.direction > 0 and self.start_frame >= self.last:
                self.start_frame = self.first
            elif self.direction < 0 and self.start_frame <= self.first:
                self.start_frame = self.last
        self.start_time = self._clock()
        return self.start_frame

    def target(self, now=None):
        """(frame, finished) for time `now`; finished only when not looping."""
        now = self._clock() if now is None else now
        steps = int(math.floor(max(0.0, now - self.start_time) * self.fps + 1e-6))
        raw = self.start_frame + self.direction * steps
        if self.loop:
            return step_frame(raw, 0, self.first, self.last, loop=True), False
        if raw > self.last:
            return self.last, True
        if raw < self.first:
            return self.first, True
        return raw, False


def expand_frame_pattern(pattern, frame):
    """Replace %04d / #### / @@@ frame tokens in a sequence path."""
    frame = int(frame)

    def printf(match):
        width = int(match.group(2) or 0)
        if match.group(1) or width:
            return "%0*d" % (width, frame)
        return "%d" % frame

    def padded(match):
        return "%0*d" % (len(match.group(0)), frame)

    out = _PRINTF_RE.sub(printf, pattern)
    out = _HASH_RE.sub(padded, out)
    out = _AT_RE.sub(padded, out)
    return out


def has_frame_token(pattern):
    return bool(
        _PRINTF_RE.search(pattern)
        or _HASH_RE.search(pattern)
        or _AT_RE.search(pattern)
    )


def is_directly_readable(path, supported_extensions):
    """True when the file extension is one Qt can decode itself."""
    ext = os.path.splitext(str(path))[1].lower().lstrip(".")
    return bool(ext) and ext in {str(e).lower().lstrip(".") for e in supported_extensions}


def proxy_key(*parts):
    """Stable short folder name for a proxy sequence (path, mtime, size...)."""
    text = "\n".join(str(p) for p in parts)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def proxy_frame_name(frame):
    return "proxy.%06d.jpg" % int(frame)


def prune_old_dirs(root, max_age_days=7.0, now=None, keep=()):
    """Delete proxy folders under root not touched for max_age_days.

    Returns the removed folder names. Errors are ignored.
    """
    removed = []
    now = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    for name in names:
        if name in keep:
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < max_age_days * 86400.0:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
        except OSError:
            pass
    return removed
