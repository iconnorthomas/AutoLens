from __future__ import annotations

import subprocess
import sys


try:
    from PySide6 import QtCore
    Signal = QtCore.Signal
except ImportError:
    from PySide2 import QtCore
    Signal = QtCore.Signal

from .ux import ProgressEstimator, parse_status_line

# Keep this much of the current run's output for plain-language failure
# messages (see autolens.ux.explain_failure).
_LOG_TAIL_CHARS = 40000

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
        self.log_tail = ""
        self.cancel_requested = False
        self.estimator = ProgressEstimator()

    def is_running(self):
        return self.process is not None and self.process.state() != QtCore.QProcess.NotRunning

    def start(self, python_exe: str, engine: str, arguments: list[str]):
        if self.is_running():
            raise RuntimeError("AutoLens is already running.")

        self._buffer = ""
        self.log_tail = ""
        self.cancel_requested = False
        self.estimator.start()
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
            message = f"\n[AUTOLENS] Failed to start: {self.process.errorString()}\n"
            self.log_tail += message
            self.output_text.emit(message)
            self.finished.emit(-1)

    def cancel(self):
        if not self.is_running():
            return
        pid = int(self.process.processId())
        self.cancel_requested = True
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

    def progress_text(self):
        """Stage, step count, elapsed time and a rough ETA for the current run."""
        return self.estimator.text()

    def _read_output(self):
        if not self.process:
            return
        raw = bytes(self.process.readAllStandardOutput())
        if not raw:
            return
        text = raw.decode("utf-8", errors="replace")
        self.output_text.emit(text)
        self.log_tail = (self.log_tail + text)[-_LOG_TAIL_CHARS:]
        self._buffer += text
        lines = self._buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""

        for line in lines:
            status = parse_status_line(line)
            if status:
                step, total, percent = status
                stage = _STAGE_NAMES.get(step, f"Stage {step}")
                self.estimator.update(percent, stage, step, total)
                self.progress_changed.emit(percent, stage)

    def _finished(self, exit_code, exit_status):
        self._read_output()
        if exit_code == 0:
            self.estimator.update(100, "Complete")
            self.progress_changed.emit(100, "Complete")
        elif self.cancel_requested:
            self.progress_changed.emit(0, "Cancelled")
        else:
            self.progress_changed.emit(0, "Stopped / failed")
        self.finished.emit(int(exit_code))
