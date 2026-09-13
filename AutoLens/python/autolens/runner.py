from __future__ import annotations

import re
import subprocess
import sys


try:
    from PySide6 import QtCore
    Signal = QtCore.Signal
except ImportError:
    from PySide2 import QtCore
    Signal = QtCore.Signal

_STAGE_NAMES = {
    1: "Preparing solve",
    2: "Extracting features",
    3: "Matching frames",
    4: "Solving camera + lens",
    5: "Evaluating reconstruction",
    6: "Generating lens + camera outputs",
    7: "Preparing dense reconstruction",
    8: "Computing dense depth maps",
    9: "Fusing dense point cloud",
    10: "Building projection mesh",
    11: "Building projection geometry",
}


class AutoLensRunner(QtCore.QObject):
    output_text = Signal(str)
    progress_changed = Signal(int, str)
    finished = Signal(int)
    started = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self._buffer = ""

    def is_running(self):
        return self.process is not None and self.process.state() != QtCore.QProcess.NotRunning

    def start(self, python_exe: str, engine: str, arguments: list[str]):
        if self.is_running():
            raise RuntimeError("AutoLens is already running.")

        self._buffer = ""
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        # AutoLens' Python engine runs in its isolated venv. The engine itself
        # launches COLMAP through a fresh Windows Machine/User environment.
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)

        program = python_exe.strip() or "python"
        args = ["-u", engine] + list(arguments)
        self.output_text.emit(
            "\n[AUTOLENS] Starting:\n" + program + " "
            + " ".join(f'\"{a}\"' if " " in a else a for a in args)
            + "\n\n"
        )
        self.started.emit()
        self.process.start(program, args)
        if not self.process.waitForStarted(5000):
            self.output_text.emit(f"\n[AUTOLENS] Failed to start: {self.process.errorString()}\n")
            self.finished.emit(-1)

    def cancel(self):
        if not self.is_running():
            return
        pid = int(self.process.processId())
        self.output_text.emit("\n[AUTOLENS] Cancelling...\n")
        if sys.platform.startswith("win") and pid:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
            except Exception:
                self.process.kill()
        else:
            self.process.kill()

    def _read_output(self):
        if not self.process:
            return
        raw = bytes(self.process.readAllStandardOutput())
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace")
        self.output_text.emit(text)
        self._buffer += text
        lines = self._buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""

        status_re = re.compile(r"AUTOLENS STATUS\s+\[[^\]]+\]\s+(\d+)/(\d+)\s+\((\d+)%\)")
        for line in lines:
            match = status_re.search(line)
            if match:
                step = int(match.group(1))
                percent = int(match.group(3))
                self.progress_changed.emit(percent, _STAGE_NAMES.get(step, f"Stage {step}"))

    def _finished(self, exit_code, exit_status):
        self._read_output()
        if exit_code == 0:
            self.progress_changed.emit(100, "Complete")
        else:
            self.progress_changed.emit(0, "Stopped / failed")
        self.finished.emit(int(exit_code))
