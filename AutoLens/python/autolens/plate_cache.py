"""Persistent, lazily filled plate cache for the workspace viewer.

The same design as the Match tool's frame cache. No Qt and no Nuke here: a
*source* object (``plate_source.NukePlateSource`` in Nuke, a fake in the tests)
says where pixels come from. Building a ``PlateCacheManager`` renders nothing.
The dialog then

* asks ``path(frame)`` for an image to show (None while it isn't cached yet),
* calls ``request(frame)`` when the viewer's frame changes, so caching works
  outward from where the user is looking,
* calls ``step(budget)`` from a short QTimer in idle time: each call renders a
  few frames within the time budget and returns them, so nothing is modal and
  the viewer stays responsive.

Where the cache lives
---------------------
``<cache root>/<hash of source.key() and the cache size>``. The root is
``$AUTOLENS_CACHE_DIR``, else a per-user cache folder (not the temp folder),
and nothing in it is ever deleted automatically.

What makes a frame valid
------------------------
``frame_cache.json`` records, for every rendered frame, the file's size and
mtime and ``source.signature(frame)`` (the upstream graph's settings plus the
source files' paths, sizes and dates). A frame re-renders only when one of
those changes, so reopening a shot renders nothing. Frames the source can
serve directly (``source.direct_path``, e.g. a plain JPEG sequence) are never
copied.

The manifest is written atomically (temp file + ``os.replace``) with the
previous one kept as ``.bak``; a damaged manifest falls back to the backup,
then to an empty cache. Frames render into ``_render/`` and are moved into
place, so a half-written file is never treated as valid.

Source protocol (duck-typed)::

    frames() -> [int]            cache_size() -> (w, h)
    key() -> json-able           signature(frame) -> json-able
    direct_path(frame) -> path or None
    renderer(directory) -> context manager yielding render(frames) -> {frame: path}
    refresh()                    re-read the graph and file stats
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time

MANIFEST = "frame_cache.json"
VERSION = 1
CACHE_DIR_ENV = "AUTOLENS_CACHE_DIR"


def default_cache_root(environ=None, platform=None, home=None):
    """``$AUTOLENS_CACHE_DIR``, else the user's local cache folder + AutoLens/cache.

    Windows: %LOCALAPPDATA% (local, not synced by OneDrive). macOS:
    ~/Library/Caches. Linux: $XDG_CACHE_HOME or ~/.cache.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser("~") if home is None else home
    override = environ.get(CACHE_DIR_ENV)
    if override:
        return override
    if platform.startswith("win"):
        base = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "AutoLens", "cache")
    if platform == "darwin":
        return os.path.join(home, "Library", "Caches", "AutoLens")
    base = environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return os.path.join(base, "autolens")


def read_json(path):
    """The parsed JSON object at ``path``, or None if missing, unreadable or damaged."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path, data, backup=False):
    """Write JSON to a temp file and ``os.replace`` it over ``path``.

    A reader never sees a half-written file; a crash mid-write leaves the old
    file in place. ``backup`` keeps the previous version as ``path + '.bak'``.
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    if backup and os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)


def _norm(value):
    """JSON round trip, so tuples and lists compare equal to what the manifest holds."""
    return json.loads(json.dumps(value))


def _listing(directory):
    """{normcased name: (mtime_ns, size)} for one directory (one scandir)."""
    out = {}
    try:
        for entry in os.scandir(directory):
            try:
                st = entry.stat()
                out[os.path.normcase(entry.name)] = (int(st.st_mtime_ns), int(st.st_size))
            except OSError:
                pass
    except OSError:
        pass
    return out


def cache_directory(source, cache_root=None):
    """Content-addressed folder for a source: same plate and size, same folder."""
    blob = json.dumps(
        [_norm(source.key()), list(source.cache_size()), VERSION],
        sort_keys=True,
    ).encode("utf-8")
    root = cache_root or default_cache_root()
    return os.path.abspath(os.path.join(root, hashlib.sha1(blob).hexdigest()[:16]))


class PlateCacheManager:
    """Per-frame plate cache that renders lazily. See the module docstring.

    ``directory`` overrides the content-addressed folder under ``cache_root``;
    ``clock`` is injectable for tests.
    """

    def __init__(self, source, directory=None, cache_root=None, clock=None):
        self.source = source
        self._clock = clock or time.perf_counter
        self._lock = threading.RLock()
        self._fixed_directory = directory is not None
        self._cache_root = cache_root
        self.warnings = []
        self.failed = {}            # {frame: error text}
        self.frames = sorted(set(int(f) for f in source.frames()))
        self._center = self.frames[0] if self.frames else 0
        self._avg = None            # seconds per rendered frame
        self.directory = os.path.abspath(
            directory if directory is not None else cache_directory(source, cache_root)
        )
        self._entries = self._load()
        self._validate()

    # -- public ---------------------------------------------------------
    @property
    def cache_size(self):
        return tuple(self.source.cache_size())

    @property
    def done(self):
        return not self._pending

    @property
    def direct(self):
        """True when every valid frame so far is the source's own file."""
        with self._lock:
            return bool(self._direct) and len(self._direct) == len(self._valid)

    def path(self, frame):
        """Image path for ``frame``, or None while it isn't cached."""
        return self._valid.get(int(frame))

    def cached_frames(self):
        with self._lock:
            return sorted(f for f in self._valid if f in self._frame_set)

    def pending_frames(self):
        """Frames still to render, in the order step() will render them."""
        with self._lock:
            return sorted(self._pending, key=self._priority)

    def request(self, frame):
        """Render ``frame`` next, then outward from it (forward first)."""
        self._center = int(frame)

    def step(self, budget_seconds=0.06):
        """Render pending frames, nearest the requested frame first, within the budget.

        Always renders at least one frame when any is pending, and stops early
        when the next frame would likely overrun (based on the average so far).
        Returns the frames rendered; a frame that fails moves to ``failed``.
        """
        if not self._pending:
            return []
        start = self._clock()
        rendered = []
        with self.source.renderer(self._stage()) as render:
            while True:
                with self._lock:
                    if not self._pending:
                        break
                    frame = min(self._pending, key=self._priority)
                t0 = self._clock()
                try:
                    self._render_run(render, [frame])
                    rendered.append(frame)
                    dt = self._clock() - t0
                    self._avg = dt if self._avg is None else 0.7 * self._avg + 0.3 * dt
                except Exception as exc:
                    with self._lock:
                        self._pending.discard(frame)
                        self.failed[frame] = str(exc)
                if self._clock() - start + (self._avg or 0.0) > budget_seconds:
                    break
        if rendered:
            self._save()
        return rendered

    def invalidate(self, frames=None):
        """Forget rendered frames (all when None) so they render again.

        Direct frames stay valid: they are the source files themselves.
        """
        with self._lock:
            targets = list(self._entries) if frames is None else [int(f) for f in frames]
            changed = False
            for frame in targets:
                if self._entries.pop(frame, None) is not None:
                    changed = True
                if frame in self._valid and frame not in self._direct:
                    del self._valid[frame]
                if frame in self._frame_set and frame not in self._valid:
                    self._pending.add(frame)
                self.failed.pop(frame, None)
        if changed:
            self._save()

    def refresh(self):
        """Re-read the source (graph, file stats) and revalidate.

        Returns the frames that were valid and no longer are. When the
        source's key changed (different plate graph), switches to that key's
        folder, which may already hold its frames.
        """
        with self._lock:
            before = dict(self._valid)
            self.source.refresh()
            self.failed.clear()         # whatever broke them may be fixed now
            if not self._fixed_directory:
                directory = cache_directory(self.source, self._cache_root)
                if directory != self.directory:
                    self.directory = directory
                    self._entries = self._load()
            self._validate()
            return sorted(
                f for f, path in before.items() if self._valid.get(f) != path
            )

    # -- validity -------------------------------------------------------
    def _params(self):
        return {"cache": list(self.cache_size), "version": VERSION}

    def _validate(self):
        listing = _listing(self.directory)
        self._frame_set = set(self.frames)
        self._valid, self._direct = {}, {}
        for frame in self.frames:
            path = self.source.direct_path(frame)
            if path is not None:
                self._direct[frame] = self._valid[frame] = path
        for frame, entry in list(self._entries.items()):
            if frame in self._valid:
                continue
            name = entry.get("file", "")
            st = listing.get(os.path.normcase(name))
            if st is None or [entry.get("mtime"), entry.get("size")] != list(st):
                continue
            if entry.get("sig") != _norm(self.source.signature(frame)):
                continue
            self._valid[frame] = os.path.join(self.directory, name)
        self._pending = set(f for f in self.frames if f not in self._valid)
        for frame in list(self.failed):
            if frame in self._valid:
                del self.failed[frame]
            else:
                # Failed renders aren't retried until something changes.
                self._pending.discard(frame)

    def _priority(self, frame):
        d = frame - self._center
        return (abs(d), 0 if d >= 0 else 1)

    # -- rendering ------------------------------------------------------
    def _stage(self):
        stage = os.path.join(self.directory, "_render")
        os.makedirs(stage, exist_ok=True)
        return stage

    def _render_run(self, render, frames):
        """Render ``frames`` into the stage folder, check each, move it into place."""
        staged = render(list(frames))
        for frame in frames:
            path = staged.get(frame)
            if path is None or not os.path.exists(path):
                raise RuntimeError("render produced no file for frame %d" % frame)
            final = os.path.join(self.directory, os.path.basename(path))
            os.replace(path, final)
            st = os.stat(final)
            entry = {
                "file": os.path.basename(final),
                "mtime": int(st.st_mtime_ns),
                "size": int(st.st_size),
                "sig": _norm(self.source.signature(frame)),
            }
            with self._lock:
                self._entries[frame] = entry
                self._valid[frame] = final
                self._pending.discard(frame)
                self.failed.pop(frame, None)

    # -- manifest -------------------------------------------------------
    def _manifest_path(self):
        return os.path.join(self.directory, MANIFEST)

    def _load(self):
        path = self._manifest_path()
        for candidate in (path, path + ".bak"):
            data = read_json(candidate)
            if data is None:
                if os.path.exists(candidate):
                    self.warnings.append(
                        "ignored a damaged frame cache manifest: %s" % candidate
                    )
                continue
            if data.get("params") != _norm(self._params()):
                return {}
            entries = {}
            for key, entry in (data.get("frames") or {}).items():
                try:
                    entries[int(key)] = dict(entry)
                except (TypeError, ValueError):
                    continue
            return entries
        return {}

    def _save(self):
        with self._lock:
            data = {
                "params": self._params(),
                "key": _norm(self.source.key()),
                "frames": {str(f): e for f, e in sorted(self._entries.items())},
            }
            os.makedirs(self.directory, exist_ok=True)
            write_json_atomic(self._manifest_path(), data, backup=True)
