"""``NukePlateSource``: viewer frames of AutoLens's plate input, for ``plate_cache``.

Ported from the Match tool's ``NukeSource``. ``nuke`` is passed in, never
imported, so everything but the render itself is testable with fake nodes.

Renders the plate node (AutoLens's input 0) through
``Reformat (to box, distort) -> Crop -> Write`` to JPEG at the cache size, so
the viewer shows what Nuke shows (the real upstream graph and its colour
handling). ``nuke.execute`` must run on the main thread.

Per-frame signature: a hash of the upstream graph's non-default knob values
(names, DAG positions, labels and colours ignored; Root colour settings
included) plus the path, mtime and size of each upstream Read's file for that
frame. When the source frame can't be worked out (time-changing nodes,
groups/gizmos, an expression frame mode, TCL in the file path) every frame
depends on a fingerprint of the whole sequence instead.

Direct reads: a Read (directly, or through Dots, NoOps and disabled nodes) of
files Qt can decode, in its default (sRGB) colourspace, not raw or
premultiplied: the Read's files are shown as they are, nothing is rendered.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re

PASS_THROUGH = ("Dot", "NoOp")
# Nodes whose output frame t may come from another source frame.
TIME_CLASSES = frozenset((
    "TimeOffset", "FrameHold", "FrameRange", "Retime", "Kronos", "OFlow", "OFlow2",
    "TimeWarp", "AppendClip", "TimeBlur", "TimeEcho", "FrameBlend", "TimeClip",
    "TimeDissolve", "MotionBlur", "TemporalMedian", "Denoise2", "Group", "Gizmo",
    "LiveGroup", "Precomp", "Switch",
))
# Knobs that never change pixels.
IGNORED_KNOBS = frozenset((
    "name", "xpos", "ypos", "selected", "label", "note_font", "note_font_size",
    "note_font_color", "tile_color", "gl_color", "hide_input", "postage_stamp",
    "postage_stamp_frame", "dope_sheet", "bookmark", "icon", "indicators", "cached",
    "help", "autolabel", "knobChanged", "onCreate", "onDestroy", "updateUI", "panel",
    "inputs", "useLifetime", "lifetimeStart", "lifetimeEnd", "cacheLocal",
    "localizationPolicy", "proxy", "proxy_format", "proxy_type", "reload",
))
ROOT_KNOBS = (
    "format", "colorManagement", "OCIO_config", "customOCIOConfigPath",
    "workingSpaceLUT", "int8Lut", "int16Lut", "logLut", "floatLut",
)
CACHE_EXT = "jpg"


# ---------------------------------------------------------------------------
# Small knob helpers (tolerate missing knobs and odd nodes)


def knob(node, name):
    try:
        return node.knob(name)
    except Exception:
        return None


def knob_string(node, name, default=""):
    k = knob(node, name)
    if k is None:
        return default
    try:
        value = k.value()
    except Exception:
        return default
    return default if value is None else str(value)


def set_knob(node, name, value, required=True):
    k = knob(node, name)
    if k is None:
        if required:
            raise RuntimeError("%s has no knob %r" % (node_class(node), name))
        return
    try:
        k.setValue(value)
    except Exception:
        if required:
            raise


def node_class(node):
    try:
        return node.Class()
    except Exception:
        return "?"


def _input_count(node):
    try:
        return int(node.inputs())
    except Exception:
        return 0


def _disabled(node):
    k = knob(node, "disable")
    try:
        return bool(k.value()) if k is not None else False
    except Exception:
        return False


def contiguous_runs(frames):
    runs = []
    for frame in sorted(set(int(f) for f in frames)):
        if runs and frame == runs[-1][1] + 1:
            runs[-1][1] = frame
        else:
            runs.append([frame, frame])
    return [(a, b) for a, b in runs]


def cache_size(width, height, max_size):
    """Cache (w, h): the plate format scaled so its long edge is at most max_size."""
    w, h = float(max(1, width)), float(max(1, height))
    if max_size and max(w, h) > max_size:
        k = float(max_size) / max(w, h)
        w, h = w * k, h * k
    return max(1, int(round(w))), max(1, int(round(h)))


# ---------------------------------------------------------------------------
# Upstream graph


def upstream_nodes(node):
    """``[(node, [input ids])]`` breadth-first from ``node``; ids index this list."""
    order, ids = [], {id(node): 0}
    queue = [node]
    while queue:
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


def tcl_words(text):
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


def split_script(text):
    """``[(name, value)]`` from ``writeKnobs`` text, split by newlines or spaces."""
    words = tcl_words(text)
    if len(words) % 2 == 0:
        return list(zip(words[0::2], words[1::2]))
    pairs = []
    for line in text.splitlines():
        w = tcl_words(line)
        if w:
            pairs.append((w[0], " ".join(w[1:])))
    return pairs


def node_knob_text(node, nuke=None):
    """The node's non-default, pixel-relevant knob values as sorted text lines."""
    write_knobs = getattr(node, "writeKnobs", None)
    flag = getattr(nuke, "WRITE_NON_DEFAULT_ONLY", None) if nuke is not None else None
    if write_knobs is not None and flag is not None:
        try:
            flags = flag | getattr(nuke, "TO_SCRIPT", 0)
            pairs = split_script(write_knobs(flags) or "")
            return sorted("%s %s" % p for p in pairs if p[0] not in IGNORED_KNOBS)
        except Exception:
            pass
    lines = []
    try:
        items = sorted(node.knobs().items())
    except Exception:
        items = []
    for name, k in items:
        if name in IGNORED_KNOBS:
            continue
        lines.append("%s %s" % (name, _knob_repr(k)))
    return lines


def _knob_repr(k):
    to_script = getattr(k, "toScript", None)
    if to_script is not None:
        try:
            return to_script()
        except Exception:
            pass
    try:
        return repr(k.value())
    except Exception:
        return "?"


def graph_signature(node, nuke=None):
    """Hash of the upstream graph: classes, wiring and non-default knob values (no names)."""
    parts = []
    for n, links in upstream_nodes(node):
        parts.append([node_class(n), links, node_knob_text(n, nuke)])
    root = None
    try:
        root = nuke.root()
    except Exception:
        pass
    parts.append(["Root", [knob_string(root, k, "") for k in ROOT_KNOBS]])
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Read nodes: which file is frame t?


def _int_knob(node, name, default):
    k = knob(node, name)
    if k is None:
        return default
    try:
        return int(round(float(k.value())))
    except Exception:
        return default


def read_range(read):
    try:
        r = read.frameRange()
        default = (int(r.first()), int(r.last()))
    except Exception:
        default = (1, 1)
    return _int_knob(read, "first", default[0]), _int_knob(read, "last", default[1])


def read_pattern(read):
    """The Read's absolute file pattern, or None when it can't be resolved without Nuke."""
    pattern = knob_string(read, "file", "") or ""
    if (
        not pattern
        or "[" in pattern
        or "%v" in pattern.lower()
        or not os.path.isabs(pattern)
    ):
        return None
    return pattern


def format_pattern(pattern, frame):
    s = re.sub(r"#+", lambda m: "%0*d" % (len(m.group()), frame), pattern)
    return re.sub(r"%(0?\d*)d", lambda m: "%0*d" % (int(m.group(1) or 0), frame), s)


def pattern_regex(pattern):
    parts = re.split(r"(#+|%0?\d*d)", os.path.basename(pattern))
    rx = "".join("-?\\d+" if i % 2 else re.escape(p) for i, p in enumerate(parts))
    return re.compile("^%s$" % rx, re.IGNORECASE)


def read_file_frame(read, frame):
    """The file frame a Read shows at ``frame``: an int, None (black), or False (unknown)."""
    first, last = read_range(read)
    t = int(frame)
    offset = (knob_string(read, "frame", "") or "").strip()
    if offset:
        mode = (knob_string(read, "frame_mode", "start at") or "").lower()
        try:
            n = int(round(float(offset)))
        except ValueError:
            return False
        if mode.startswith("start"):
            t = t - n + first
        elif mode.startswith("offset"):
            t = t - n
        else:
            return False
    if first <= t <= last:
        return t
    policy = (knob_string(read, "before" if t < first else "after", "hold") or "hold").lower()
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


class _Stats:
    """File stats, one directory listing per refresh."""

    def __init__(self):
        self._dirs = {}

    def _listing(self, directory):
        key = os.path.normcase(os.path.abspath(directory))
        if key not in self._dirs:
            entries = {}
            try:
                for e in os.scandir(directory):
                    try:
                        st = e.stat()
                        entries[os.path.normcase(e.name)] = (
                            e.name, int(st.st_mtime_ns), int(st.st_size)
                        )
                    except OSError:
                        pass
            except OSError:
                pass
            self._dirs[key] = entries
        return self._dirs[key]

    def stat(self, path):
        """(mtime_ns, size) or None."""
        hit = self._listing(os.path.dirname(path)).get(
            os.path.normcase(os.path.basename(path))
        )
        return hit[1:] if hit else None

    def matching(self, directory, regex):
        return sorted(v for v in self._listing(directory).values() if regex.match(v[0]))


# ---------------------------------------------------------------------------
# Rendering


@contextlib.contextmanager
def render_graph(nuke, plate_node, pattern, cw, ch, warnings=None):
    """Build ``plate -> Reformat -> Crop -> Write(pattern)`` once; yields ``render(first, last)``.

    Proxy mode is off inside the block; the temporary nodes, proxy mode, the
    current frame, the undo stack and the script's modified flag are restored
    on exit.
    """
    warnings = warnings if warnings is not None else []
    root = nuke.root()
    proxy = knob(root, "proxy")
    try:
        proxy_was_on = bool(proxy.value()) if proxy is not None else False
    except Exception:
        proxy_was_on = False
    try:
        start_frame = nuke.frame()
    except Exception:
        start_frame = None
    try:
        was_modified = root.modified()
    except Exception:
        was_modified = None
    undo = getattr(nuke, "Undo", None)
    undo_off = False
    if undo is not None:
        try:
            undo.disable()
            undo_off = True
        except Exception:
            pass
    created = []
    context = root if hasattr(root, "__enter__") else contextlib.nullcontext()
    try:
        if proxy_was_on:
            proxy.setValue(False)
            warnings.append("proxy mode was switched off for the cache render")
        with context:
            ref = nuke.nodes.Reformat()
            created.append(ref)
            ref.setInput(0, plate_node)
            set_knob(ref, "type", "to box")
            set_knob(ref, "box_fixed", True)
            set_knob(ref, "box_width", cw)
            set_knob(ref, "box_height", ch)
            set_knob(ref, "box_pixel_aspect", 1.0, required=False)
            set_knob(ref, "resize", "distort")
            set_knob(ref, "center", True, required=False)
            set_knob(ref, "black_outside", False, required=False)
            set_knob(ref, "pbb", False, required=False)

            crop = nuke.nodes.Crop()
            created.append(crop)
            crop.setInput(0, ref)
            set_knob(crop, "box", [0, 0, cw, ch])
            set_knob(crop, "reformat", False, required=False)
            set_knob(crop, "crop", True, required=False)

            write = nuke.nodes.Write()
            created.append(write)
            write.setInput(0, crop)
            set_knob(write, "file", pattern)
            set_knob(write, "file_type", "jpeg")
            set_knob(write, "_jpeg_quality", 0.9, required=False)
            set_knob(write, "channels", "rgb", required=False)
            set_knob(write, "create_directories", True, required=False)

            def render(first, last):
                nuke.execute(write, int(first), int(last), 1)

            yield render
    finally:
        for n in reversed(created):
            try:
                nuke.delete(n)
            except Exception:
                pass
        if proxy_was_on:
            try:
                proxy.setValue(True)
            except Exception:
                pass
        if start_frame is not None:
            try:
                if nuke.frame() != start_frame:
                    nuke.frame(start_frame)
            except Exception:
                pass
        if undo_off:
            try:
                undo.enable()
            except Exception:
                pass
        if was_modified is not None:
            try:
                root.setModified(was_modified)
            except Exception:
                pass


def frame_file(directory, frame):
    return os.path.join(directory, "frame.%04d.%s" % (int(frame), CACHE_EXT))


class NukePlateSource:
    """AutoLens's plate input as a ``plate_cache`` source.

    ``readable_exts``: file extensions the viewer can decode itself (direct
    reads); empty disables direct reads.
    """

    def __init__(self, plate_node, frames, plate_width, plate_height, max_size=2048,
                 readable_exts=(), nuke=None):
        if plate_node is None:
            raise ValueError("no plate node to cache")
        if nuke is None:
            raise ValueError("NukePlateSource needs the nuke module")
        self.nuke = nuke
        self.plate_node = plate_node
        self._frames = sorted(set(int(f) for f in frames))
        self._cache_size = cache_size(plate_width, plate_height, max_size)
        self.readable_exts = set(str(e).lower().lstrip(".") for e in readable_exts)
        self.warnings = []
        self.refresh()

    # -- source protocol -------------------------------------------------
    def frames(self):
        return list(self._frames)

    def cache_size(self):
        return self._cache_size

    def key(self):
        return "nuke|%s|%s" % (self.graph, CACHE_EXT)

    @property
    def direct(self):
        return self._read is not None

    def signature(self, frame):
        return [self.graph, self._sources(int(frame))]

    def direct_path(self, frame):
        if self._read is None:
            return None
        ff = read_file_frame(self._read, frame)
        if ff is None or ff is False:
            return None
        path = format_pattern(read_pattern(self._read), ff)
        if self._stats.stat(path) is None:
            return None
        return path

    @contextlib.contextmanager
    def renderer(self, directory):
        cw, ch = self._cache_size
        pattern = os.path.join(directory, "frame.%%04d.%s" % CACHE_EXT).replace("\\", "/")
        warnings = []
        with render_graph(self.nuke, self.plate_node, pattern, cw, ch, warnings) as execute:
            def render(frames):
                for first, last in contiguous_runs(frames):
                    execute(first, last)
                for w in warnings:
                    if w not in self.warnings:
                        self.warnings.append(w)
                return {int(f): frame_file(directory, f) for f in frames}
            yield render

    def refresh(self):
        nodes = upstream_nodes(self.plate_node)
        self.graph = graph_signature(self.plate_node, self.nuke)
        self._reads = [n for n, _ in nodes if node_class(n) == "Read"]
        probe = self._frames[0] if self._frames else 1
        self._time_safe = not any(node_class(n) in TIME_CLASSES for n, _ in nodes) and all(
            read_pattern(r) is not None and read_file_frame(r, probe) is not False
            for r in self._reads
        )
        self._read = self._direct_read()
        self._stats, self._memo = _Stats(), {}

    # -- internals -------------------------------------------------------
    def _direct_read(self):
        n = self.plate_node
        while n is not None and node_class(n) != "Read":
            cls = node_class(n)
            if cls in PASS_THROUGH or (_disabled(n) and cls not in TIME_CLASSES):
                n = n.input(0) if _input_count(n) else None
            else:
                return None
        if n is None or _disabled(n) or not self._time_safe or not self.readable_exts:
            return None
        pattern = read_pattern(n)
        ext = os.path.splitext(pattern or "")[1].lower().lstrip(".")
        if ext not in self.readable_exts:
            return None
        ftype = knob_string(n, "file_type", "").lower()
        if ftype and ftype not in self.readable_exts and not (
            ftype == "jpeg" and "jpg" in self.readable_exts
        ):
            return None
        if knob_string(n, "raw", "False") in ("True", "1"):
            return None
        if knob_string(n, "premultiplied", "False") in ("True", "1"):
            return None
        cs = (knob_string(n, "colorspace", "default") or "default").lower()
        if not (cs.startswith("default") or "srgb" in cs):
            return None
        return n

    def _sources(self, frame):
        """What ``frame`` is made from: [[path, mtime_ns, size], ...]."""
        out = []
        for r in self._reads:
            ff = read_file_frame(r, frame) if self._time_safe else False
            if ff is None:
                out.append(["black"])
            elif ff is not False:
                path = format_pattern(read_pattern(r), ff)
                st = self._stats.stat(path)
                out.append([path] + (list(st) if st else [None, None]))
            else:
                key = id(r)
                if key not in self._memo:
                    pattern = read_pattern(r)
                    if pattern is None:
                        self._memo[key] = ["unknown", knob_string(r, "file", "")]
                    else:
                        files = self._stats.matching(
                            os.path.dirname(pattern), pattern_regex(pattern)
                        )
                        self._memo[key] = [
                            "sequence", pattern, len(files),
                            max([m for _, m, _ in files] or [0]),
                            sum(s for _, _, s in files),
                        ]
                out.append(self._memo[key])
        return out
