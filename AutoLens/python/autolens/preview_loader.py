"""Background decoding of viewer frames into a memory-bounded cache.

Qt only, no Nuke: frames are decoded on worker threads (concurrent.futures),
scaled to the viewer while decoding, and turned into QPixmaps on the main
thread by a polling QTimer. No Qt signal ever crosses a thread, so the dialog
can be closed at any time without a dangling emitter.

JPEG/PNG/TIFF are decoded with QImageReader (``decode_image``). EXR/DPX are
decoded by the frame-server processes (``worker_decoder`` over
autolens/frame_server.py), whose 8-bit RGB is wrapped in a QImage here.
Files are only ever read, never written.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

try:
    from PySide6 import QtCore, QtGui
except ImportError:
    from PySide2 import QtCore, QtGui

from .frame_cache import FrameCache, fit_size

FULL = "full"
VIEW = "view"


def supported_extensions():
    """Image file extensions this Qt build can decode."""
    out = set()
    for fmt in QtGui.QImageReader.supportedImageFormats():
        try:
            out.add(bytes(fmt).decode("ascii", "ignore").lower())
        except Exception:
            out.add(str(fmt).lower())
    return out


def worker_decoder(pool, transfer):
    """A decoder (same signature as decode_image) that asks the frame server.

    Raises with the server's message on failure, so the viewer can show why.
    """
    def decode(path, max_w=0, pixel_aspect=1.0):
        width, height, rgb, src = pool.decode(path, max_w, pixel_aspect, transfer)
        image = QtGui.QImage(rgb, width, height, width * 3, QtGui.QImage.Format_RGB888)
        # copy(): the QImage must own its pixels once `rgb` goes away.
        return image.copy(), src
    return decode


def decode_image(path, max_w=0, pixel_aspect=1.0):
    """Decode `path` to a QImage, scaled to fit max_w display pixels.

    Safe on any thread. Returns (image or None, (src_w, src_h)).
    """
    if not path or not os.path.isfile(path):
        return None, (0, 0)
    reader = QtGui.QImageReader(path)
    size = reader.size()
    src = (size.width(), size.height()) if size.isValid() else (0, 0)
    if max_w and src[0] > 0 and src[1] > 0:
        w, h = fit_size(src[0], src[1], max_w, 0, pixel_aspect)
        if w < src[0]:
            # JPEG scales during decode (much faster); other formats are
            # scaled by Qt after decoding, still off the main thread.
            reader.setScaledSize(QtCore.QSize(w, h))
    image = reader.read()
    if image.isNull():
        return None, src
    return image, src


class PreviewLoader:
    """Decode frames ahead of the playhead and keep them in an LRU cache.

    path_for_frame(frame) -> file path or None (None: not available yet).
    on_frames_ready(list of frames) and on_frame_failed(frame) are called on
    the main thread.
    """

    def __init__(self, parent, budget_bytes, workers=3):
        self.cache = FrameCache(budget_bytes)
        self.workers = max(1, int(workers))
        self._executor = ThreadPoolExecutor(max_workers=self.workers)
        self._pending = {}        # (kind, frame) -> Future
        self._wanted = []         # frames in priority order
        self._generation = 0
        self._path_for_frame = None
        self._decoder = decode_image
        self._max_w = 0
        self._pixel_aspect = 1.0
        self.source = None
        self.failed = set()
        self.errors = {}          # frame -> why its decode failed
        self.full = None          # (frame, QPixmap) at full resolution
        self.protect = set()
        self.closed = False
        self.on_frames_ready = None
        self.on_frame_failed = None
        self.on_full_ready = None

        self._timer = QtCore.QTimer(parent)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._collect)

    # ------------------------------------------------------------------
    def set_source(self, source, path_for_frame, max_w, pixel_aspect=1.0, decoder=None):
        """Point at a new sequence; clears the cache when anything changed.

        ``decoder(path, max_w, pixel_aspect) -> (QImage or None, src size)``
        runs on worker threads; decode_image (Qt) when None.
        """
        decoder = decoder or decode_image
        if (
            source == self.source
            and int(max_w) <= self._max_w
            and path_for_frame is not None
        ):
            self._path_for_frame = path_for_frame
            self._decoder = decoder
            return False
        self._generation += 1
        self.cancel_all()
        self.cache.clear()
        self.failed.clear()
        self.errors.clear()
        self.full = None
        self.source = source
        self._path_for_frame = path_for_frame
        self._decoder = decoder
        self._max_w = int(max_w)
        self._pixel_aspect = float(pixel_aspect or 1.0)
        return True

    def capacity_frames(self, src_w, src_h):
        """Roughly how many viewer-sized frames fit in the budget."""
        w, h = fit_size(src_w, src_h, self._max_w, 0, self._pixel_aspect)
        return max(1, int(self.cache.max_bytes // max(1, w * h * 4)))

    def pixmap(self, frame):
        return self.cache.get(int(frame))

    def frames(self):
        return self.cache.keys()

    def forget(self, frames):
        for frame in frames:
            self.cache.discard(int(frame))
            self.failed.discard(int(frame))
            self.errors.pop(int(frame), None)

    def is_pending(self, frame):
        return (VIEW, int(frame)) in self._pending

    # ------------------------------------------------------------------
    def request(self, frames):
        """Decode these frames (priority order); drop queued work for others."""
        wanted = []
        seen = set()
        for frame in frames:
            frame = int(frame)
            if frame in seen:
                continue
            seen.add(frame)
            if frame in self.cache or frame in self.failed:
                continue
            wanted.append(frame)
        self._wanted = wanted
        for key, future in list(self._pending.items()):
            if key[0] == VIEW and key[1] not in seen and future.cancel():
                del self._pending[key]
        self._pump()

    def request_full(self, frame):
        """Decode one frame at full resolution (for zoomed-in inspection)."""
        frame = int(frame)
        if self.full is not None and self.full[0] == frame:
            return
        for key, future in list(self._pending.items()):
            if key[0] == FULL and key[1] != frame and future.cancel():
                del self._pending[key]
        self._submit(FULL, frame, 0)

    def load_now(self, frame):
        """Decode one frame on the calling (main) thread; returns the pixmap."""
        frame = int(frame)
        path = self._path(frame)
        try:
            image, _src = self._decoder(path, self._max_w, self._pixel_aspect)
        except Exception as exc:
            image = None
            self.errors[frame] = str(exc)
        if image is None:
            self.failed.add(frame)
            return None
        self.failed.discard(frame)
        self.errors.pop(frame, None)
        return self._store(frame, image)

    def cancel_all(self):
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._wanted = []

    def shutdown(self):
        self.closed = True
        self._timer.stop()
        self.cancel_all()
        self._generation += 1
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:          # Python < 3.9
            self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    def _path(self, frame):
        if self._path_for_frame is None:
            return None
        try:
            return self._path_for_frame(frame)
        except Exception:
            return None

    def _pump(self):
        # A short queue keeps the most urgent frames at the front: requests
        # change every frame while scrubbing or playing.
        limit = self.workers * 2
        while self._wanted and self._view_pending() < limit:
            frame = self._wanted.pop(0)
            if (VIEW, frame) in self._pending or frame in self.cache:
                continue
            self._submit(VIEW, frame, self._max_w)

    def _view_pending(self):
        return sum(1 for key in self._pending if key[0] == VIEW)

    def _submit(self, kind, frame, max_w):
        key = (kind, frame)
        if key in self._pending or self.closed:
            return
        path = self._path(frame)
        if not path:
            return
        self._pending[key] = self._executor.submit(
            self._decoder, path, max_w, self._pixel_aspect
        )
        if not self._timer.isActive():
            self._timer.start()

    def _store(self, frame, image):
        pixmap = QtGui.QPixmap.fromImage(image)
        nbytes = pixmap.width() * pixmap.height() * 4
        self.cache.put(frame, pixmap, nbytes, protect=self.protect)
        return pixmap

    def _collect(self):
        ready = []
        failed = []
        full = None
        for key, future in list(self._pending.items()):
            if not future.done():
                continue
            self._pending.pop(key, None)
            if future.cancelled():
                continue
            kind, frame = key
            try:
                image, _src = future.result()
            except Exception as exc:
                image = None
                self.errors[frame] = str(exc)
            if kind == FULL:
                if image is not None:
                    self.full = (frame, QtGui.QPixmap.fromImage(image))
                    full = frame
            elif image is None:
                self.failed.add(frame)
                failed.append(frame)
            else:
                self._store(frame, image)
                ready.append(frame)

        self._pump()
        if not self._pending:
            self._timer.stop()

        # Callbacks last: they may change the source and reset this loader.
        generation = self._generation
        if ready and self.on_frames_ready:
            self.on_frames_ready(ready)
        if full is not None and self.on_full_ready and generation == self._generation:
            self.on_full_ready(full)
        for frame in failed:
            if generation != self._generation or not self.on_frame_failed:
                break
            self.on_frame_failed(frame)
