"""Pool of frame-server worker processes (autolens/frame_worker.py).

Plain Python (subprocess + threads), no Qt and no Nuke. Each worker is a
separate process running AutoLens's managed Python, so EXR/DPX decoding
neither needs numpy/OpenEXR inside Nuke nor competes with Nuke's GIL, and it
works the same on Nuke 14 to 17. Decoded RGB comes back over the worker's
stdout pipe; nothing touches the disk.

``decode()`` is called from the viewer's decode threads: it borrows an idle
worker (blocking until one is free), runs one request and returns the worker.
A worker that dies is restarted on its next use.
"""
from __future__ import annotations

import itertools
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from . import frame_protocol as fp

WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frame_worker.py")


class FrameServerError(RuntimeError):
    pass


def managed_python(configured=""):
    """The managed runtime's python: the node's External Python if it exists,
    else ~/.nuke/AutoLens/.venv; None when neither is there."""
    configured = (configured or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    root = Path.home() / ".nuke" / "AutoLens" / ".venv"
    candidate = root / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    return str(candidate) if candidate.is_file() else None


class FrameServerPool:
    def __init__(self, python, size=3, script=WORKER_SCRIPT, popen=None):
        self.python = python
        self.script = script
        self.size = max(1, int(size))
        self._popen = popen or subprocess.Popen
        self._slots = queue.Queue()
        for _ in range(self.size):
            self._slots.put(None)         # workers start on first use
        self._procs = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._closed = False

    # ------------------------------------------------------------------
    def decode(self, path, max_w=0, pixel_aspect=1.0, transfer="linear"):
        """(width, height, rgb bytes, (src_w, src_h)); raises FrameServerError."""
        header, payload = self.request({
            "cmd": "decode",
            "path": str(path),
            "max_w": int(max_w or 0),
            "pixel_aspect": float(pixel_aspect or 1.0),
            "transfer": transfer,
        })
        width, height = int(header["width"]), int(header["height"])
        if len(payload) != width * height * 3:
            raise FrameServerError(
                "frame server sent %d bytes for %dx%d" % (len(payload), width, height)
            )
        return width, height, payload, (header.get("src_width"), header.get("src_height"))

    def hello(self):
        header, _ = self.request({"cmd": "hello"})
        return header

    def request(self, message):
        if self._closed:
            raise FrameServerError("frame server is shut down")
        proc = self._slots.get()
        try:
            if proc is None or proc.poll() is not None:
                proc = self._spawn()
            message = dict(message, id=next(self._ids))
            try:
                fp.send(proc.stdin, message)
                header, payload = fp.recv(proc.stdout)
            except (EOFError, OSError, fp.ProtocolError) as exc:
                self._kill(proc)
                proc = None
                if self._closed:
                    raise FrameServerError("frame server is shut down")
                raise FrameServerError("frame server stopped: %s" % exc)
            if header.get("id") != message["id"]:
                self._kill(proc)
                proc = None
                raise FrameServerError("frame server replied out of order")
            if not header.get("ok"):
                raise FrameServerError(header.get("error") or "decode failed")
            return header, payload
        finally:
            self._slots.put(proc)

    def close(self):
        self._closed = True
        with self._lock:
            procs, self._procs = self._procs, []
        for proc in procs:
            self._kill(proc)

    # ------------------------------------------------------------------
    def _spawn(self):
        if not self.python:
            raise FrameServerError("AutoLens's Python runtime isn't installed")
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            # -I: ignore PYTHONPATH/user site, so only the managed runtime is used.
            proc = self._popen(
                [self.python, "-I", self.script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
        except OSError as exc:
            raise FrameServerError("couldn't start the frame server: %s" % exc)
        with self._lock:
            self._procs.append(proc)
        return proc

    def _kill(self, proc):
        if proc is None:
            return
        for step in (
            lambda: proc.stdin.close(),
            lambda: proc.kill(),
            lambda: proc.wait(timeout=2),
            lambda: proc.stdout.close(),
        ):
            try:
                step()
            except Exception:
                pass
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)
