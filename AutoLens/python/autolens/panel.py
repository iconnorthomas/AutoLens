from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import nuke
from nukescripts import panels

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal

from . import PANEL_ID
from .paths import engine_path, find_result_nk, next_version_folder
from .pixel_aspect import source_format_info, ensure_square_pixel_proxy
from .tracking_masks import (
    mask_input_connected,
    export_input_alpha_masks,
)
from .environment import normalize_colmap_path, build_colmap_invocation, windows_status_hex
from .runner import AutoLensRunner
from .importer import import_result
from .surface_guides import (
    ensure_guide_node, auto_suggest_surfaces, export_surface_guides,
    find_guide_node, shape_count, set_guide_frame_to_current,
    clamp_guide_frame, source_frame_range,
    guide_source_read_fullname,
)

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".exr"}

QUALITY = {
    "Preview": {"quality": "preview", "resolution": 1800, "target": 8, "minimum": 5, "coverage": 0.90, "sources": 6},
    "Normal":  {"quality": "normal",  "resolution": 3200, "target": 12, "minimum": 6, "coverage": 0.97, "sources": 8},
    "High":    {"quality": "high",    "resolution": 4000, "target": 18, "minimum": 8, "coverage": 0.99, "sources": 12},
}


def _install_config():
    path = Path.home() / ".nuke" / "AutoLens" / "autolens_install.json"
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _install_report_path():
    return Path.home() / ".nuke" / "AutoLens" / "AutoLens_Install_Report.txt"

def _installed_python():
    cfg = _install_config()
    value = cfg.get("python_runtime")

    # v0.9+ installer stores:
    #   {"ok": true, "path": "C:/.../python.exe"}
    # Older builds stored the path directly as a string.
    if isinstance(value, dict):
        value = value.get("path")

    return str(value) if value and Path(str(value)).exists() else ""

def _installed_colmap():
    cfg = _install_config()
    data = cfg.get("colmap") or {}
    p = data.get("path")
    return str(Path(p)) if p and Path(p).exists() else ""


def _installed_backend_label():
    cfg = _install_config()
    data = cfg.get("colmap") or {}
    if data.get("backend") in ("managed-conda-forge", "managed-miniforge-conda-forge") and data.get("ok"):
        return f"Managed COLMAP {data.get('colmap_version','')} / CUDA {data.get('cuda_version','')}"
    return "Managed COLMAP"


def _open_folder(path: str):
    p = Path(path)
    if not p.exists():
        return
    if sys.platform.startswith("win"):
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _selected_read():
    selected = nuke.selectedNodes("Read")
    return selected[0] if len(selected) == 1 else None


def _safe_node_filename(node):
    try:
        return nuke.filename(node)
    except Exception:
        try:
            return node["file"].evaluate()
        except Exception:
            return ""


class AutoLensPanel(QtWidgets.QWidget):
    background_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QtCore.QSettings("AutoLens", "AutoLensNukeX")
        self.runner = AutoLensRunner(self)
        self.source_read_fullname = None
        self.last_result_nk = None
        self._build_ui()
        self._connect()
        self._restore_settings()
        self._quality_changed(self.quality_combo.currentText())
        read = _selected_read()
        if read is not None:
            self.use_selected_read(read)
        else:
            stored = guide_source_read_fullname(getattr(self, "controller_node", None))
            stored_node = nuke.toNode(stored) if stored else None
            if stored_node is not None and stored_node.Class() == "Read":
                self.use_selected_read(stored_node)
        self._update_guide_status()
        self._update_run_button_text()

    def _build_ui(self):
        self.setObjectName("AutoLensPanel")
        self.setMinimumWidth(480)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QtWidgets.QLabel("AutoLens")
        font = title.font(); font.setPointSize(font.pointSize() + 5); font.setBold(True); title.setFont(font)
        subtitle = QtWidgets.QLabel("Automatic lens solve • camera track • dense projection mesh • planar proxies")
        subtitle.setWordWrap(True); subtitle.setStyleSheet("color: #9a9a9a;")
        root.addWidget(title); root.addWidget(subtitle)

        input_group = QtWidgets.QGroupBox("1. Input")
        gl = QtWidgets.QGridLayout(input_group)
        self.source_edit = QtWidgets.QLineEdit()
        self.source_browse = QtWidgets.QPushButton("Browse…")
        self.selected_read_button = QtWidgets.QPushButton("Use Selected Read")
        self.input_info = QtWidgets.QLabel("No input selected."); self.input_info.setStyleSheet("color: #9a9a9a;")
        self.output_edit = QtWidgets.QLineEdit(); self.output_browse = QtWidgets.QPushButton("Browse…")
        self.version_combo = QtWidgets.QComboBox()
        gl.addWidget(QtWidgets.QLabel("Image folder"), 0, 0); gl.addWidget(self.source_edit, 0, 1); gl.addWidget(self.source_browse, 0, 2)
        gl.addWidget(self.selected_read_button, 1, 1)
        gl.addWidget(self.input_info, 2, 1, 1, 2)
        gl.addWidget(QtWidgets.QLabel("Output root"), 3, 0); gl.addWidget(self.output_edit, 3, 1); gl.addWidget(self.output_browse, 3, 2)
        gl.addWidget(QtWidgets.QLabel("Solve version"), 4, 0); gl.addWidget(self.version_combo, 4, 1, 1, 2)
        root.addWidget(input_group)

        self.guide_group = QtWidgets.QGroupBox("2. Surface Guides (optional)")
        sg = QtWidgets.QGridLayout(self.guide_group)
        self.guide_create_button = QtWidgets.QPushButton("Create / Edit Surface Guides")
        self.guide_auto_button = QtWidgets.QPushButton("Auto Suggest from Current Frame")
        self.guide_frame_button = QtWidgets.QPushButton("Set Guide Frame = Current")
        self.use_guides_checkbox = QtWidgets.QCheckBox("Use drawn surfaces when building 3D geometry")
        self.use_guides_checkbox.setChecked(True)
        self.guide_status = QtWidgets.QLabel("No surface guide Roto node yet.")
        self.guide_status.setWordWrap(True)
        self.guide_status.setStyleSheet("color: #9a9a9a;")
        sg.addWidget(self.guide_create_button, 0, 0, 1, 2)
        sg.addWidget(self.guide_auto_button, 1, 0, 1, 2)
        sg.addWidget(self.guide_frame_button, 2, 0, 1, 2)
        sg.addWidget(self.use_guides_checkbox, 3, 0, 1, 2)
        sg.addWidget(self.guide_status, 4, 0, 1, 2)
        root.addWidget(self.guide_group)

        quality_group = QtWidgets.QGroupBox("3. Reconstruction")
        ql = QtWidgets.QGridLayout(quality_group)
        self.quality_combo = QtWidgets.QComboBox(); self.quality_combo.addItems(["Preview", "Normal", "High"])
        self.quality_summary = QtWidgets.QLabel(); self.quality_summary.setWordWrap(True); self.quality_summary.setStyleSheet("color: #9a9a9a;")
        self.run_mode = QtWidgets.QComboBox(); self.run_mode.addItems(["Resume / reuse cached stages", "Rebuild dense only", "Rebuild everything"])
        self.sparse_gpu = QtWidgets.QCheckBox("GPU accelerate feature extraction / matching"); self.sparse_gpu.setChecked(True)
        self.import_when_done = QtWidgets.QCheckBox("Import AutoLens nodes into current comp when complete"); self.import_when_done.setChecked(True)
        ql.addWidget(QtWidgets.QLabel("Quality"), 0, 0); ql.addWidget(self.quality_combo, 0, 1)
        ql.addWidget(self.quality_summary, 1, 1, 1, 2)
        ql.addWidget(QtWidgets.QLabel("Run mode"), 2, 0); ql.addWidget(self.run_mode, 2, 1, 1, 2)
        ql.addWidget(self.sparse_gpu, 3, 1, 1, 2); ql.addWidget(self.import_when_done, 4, 1, 1, 2)
        root.addWidget(quality_group)

        self.advanced_group = QtWidgets.QGroupBox("Advanced")
        self.advanced_group.setCheckable(True); self.advanced_group.setChecked(False)
        adv = QtWidgets.QFormLayout(self.advanced_group)
        self.selection_combo = QtWidgets.QComboBox(); self.selection_combo.addItems(["smart", "step", "all"])
        self.resolution_spin = QtWidgets.QSpinBox(); self.resolution_spin.setRange(640, 8192); self.resolution_spin.setSingleStep(100)
        self.target_spin = QtWidgets.QSpinBox(); self.target_spin.setRange(4, 100)
        self.minimum_spin = QtWidgets.QSpinBox(); self.minimum_spin.setRange(4, 100)
        self.coverage_spin = QtWidgets.QDoubleSpinBox(); self.coverage_spin.setRange(0.50, 1.00); self.coverage_spin.setDecimals(3); self.coverage_spin.setSingleStep(0.01)
        self.sources_spin = QtWidgets.QSpinBox(); self.sources_spin.setRange(2, 50)
        self.aperture_spin = QtWidgets.QDoubleSpinBox(); self.aperture_spin.setRange(1.0, 100.0); self.aperture_spin.setDecimals(3); self.aperture_spin.setValue(36.0); self.aperture_spin.setSuffix(" mm")
        self.upright_world_checkbox = QtWidgets.QCheckBox("Auto upright world")
        self.upright_world_checkbox.setChecked(True)
        self.gpus_edit = QtWidgets.QLineEdit("auto"); self.patch_cache_edit = QtWidgets.QLineEdit("auto"); self.fusion_cache_edit = QtWidgets.QLineEdit("auto")
        adv.addRow("Dense selection", self.selection_combo); adv.addRow("Dense max dimension", self.resolution_spin)
        adv.addRow("Max smart references", self.target_spin); adv.addRow("Minimum references", self.minimum_spin)
        adv.addRow("Sparse coverage target", self.coverage_spin); adv.addRow("Source views / reference", self.sources_spin)
        adv.addRow("Nuke horizontal aperture", self.aperture_spin)
        adv.addRow("", self.upright_world_checkbox)
        adv.addRow("Dense GPU indices", self.gpus_edit)
        adv.addRow("PatchMatch cache GB", self.patch_cache_edit); adv.addRow("Fusion cache GB", self.fusion_cache_edit)
        root.addWidget(self.advanced_group)
        self._set_advanced_contents_visible(False)

        env_group = QtWidgets.QGroupBox("4. Installation Status")
        env = QtWidgets.QGridLayout(env_group)
        self.python_edit = QtWidgets.QLineEdit(); self.python_edit.setReadOnly(True)
        self.colmap_edit = QtWidgets.QLineEdit(); self.colmap_edit.setReadOnly(True)
        self.install_status = QtWidgets.QLabel("Installation status has not been checked.")
        self.install_status.setWordWrap(True)
        self.check_button = QtWidgets.QPushButton("Refresh Installation Status")
        self.install_deps_button = QtWidgets.QPushButton("Open Install Report")
        env.addWidget(QtWidgets.QLabel("Python runtime"),0,0); env.addWidget(self.python_edit,0,1)
        env.addWidget(QtWidgets.QLabel("Managed COLMAP"),1,0); env.addWidget(self.colmap_edit,1,1)
        env.addWidget(self.install_status,2,0,1,2)
        env_buttons=QtWidgets.QHBoxLayout(); env_buttons.addWidget(self.check_button); env_buttons.addWidget(self.install_deps_button)
        env.addLayout(env_buttons,3,0,1,2)
        root.addWidget(env_group)

        run_group = QtWidgets.QGroupBox("5. Run")
        rl = QtWidgets.QVBoxLayout(run_group)

        self.completed_result_checkbox = QtWidgets.QCheckBox(
            "Completed Result Available"
        )
        self.completed_result_checkbox.setEnabled(False)
        self.completed_result_checkbox.setChecked(False)
        self.completed_result_checkbox.setStyleSheet(
            "QCheckBox:disabled { color: #858585; font-weight: 600; }"
        )

        self.stage_label = QtWidgets.QLabel("Ready")
        self.progress = QtWidgets.QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0)
        buttons = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run AutoLens"); self.run_button.setMinimumHeight(34)
        self.background_button = QtWidgets.QPushButton("Background")
        self.background_button.setEnabled(False)
        self.background_button.setToolTip(
            "Hide the full AutoLens workspace and keep the solve running "
            "with a compact progress window."
        )
        self.cancel_button = QtWidgets.QPushButton("Cancel"); self.cancel_button.setEnabled(False)

        buttons.addWidget(self.run_button, 1)
        buttons.addWidget(self.background_button)
        buttons.addWidget(self.cancel_button)
        rl.addWidget(self.completed_result_checkbox)
        rl.addWidget(self.stage_label)
        rl.addWidget(self.progress)
        rl.addLayout(buttons)
        root.addWidget(run_group)

        log_group = QtWidgets.QGroupBox("Log")
        ll = QtWidgets.QVBoxLayout(log_group)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(12000)
        self.log.setMinimumHeight(170)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        ll.addWidget(self.log)
        rb = QtWidgets.QHBoxLayout()
        self.import_button = QtWidgets.QPushButton("Import Result"); self.import_button.setEnabled(False)
        self.reveal_button = QtWidgets.QPushButton("Reveal Output")
        rb.addWidget(self.import_button); rb.addWidget(self.reveal_button); rb.addStretch(1)
        ll.addLayout(rb)
        root.addWidget(log_group, 1)

    def _connect(self):
        self.source_browse.clicked.connect(self._browse_source)
        self.output_browse.clicked.connect(self._browse_output)
        self.selected_read_button.clicked.connect(lambda: self.use_selected_read())
        self.source_edit.textChanged.connect(self._update_input_info)
        self.output_edit.textChanged.connect(
            lambda _text: self._refresh_version_combo(
                prefer_latest=True,
            )
        )
        self.output_edit.textChanged.connect(
            lambda _text: self.restore_result_state()
        )
        self.output_edit.textChanged.connect(
            lambda _text: self._update_run_button_text()
        )
        self.version_combo.currentTextChanged.connect(
            lambda _text: self._version_changed()
        )
        self.run_mode.currentIndexChanged.connect(
            lambda _index: self._update_run_button_text()
        )
        self.quality_combo.currentTextChanged.connect(self._quality_changed)
        self.advanced_group.toggled.connect(self._set_advanced_contents_visible)
        self.guide_create_button.clicked.connect(self._create_or_edit_guides)
        self.guide_auto_button.clicked.connect(self._auto_suggest_guides)
        self.guide_frame_button.clicked.connect(self._set_guide_frame)
        self.check_button.clicked.connect(self.check_environment)
        self.install_deps_button.clicked.connect(self._open_install_report)
        self.run_button.clicked.connect(self.run_autolens)
        self.background_button.clicked.connect(
            self.background_requested.emit
        )
        self.cancel_button.clicked.connect(self.runner.cancel)
        self.import_button.clicked.connect(self.import_last_result)
        self.reveal_button.clicked.connect(
            lambda: _open_folder(
                self._resolved_output_folder()
            )
        )
        self.runner.output_text.connect(self._append_log)
        self.runner.progress_changed.connect(self._progress_changed)
        self.runner.started.connect(self._process_started)
        self.runner.finished.connect(self._process_finished)

    def _set_advanced_contents_visible(self, visible):
        # QGroupBox checkable normally only disables its children. For a compact
        # dialog we actually hide them so the section collapses vertically.
        for child in self.advanced_group.findChildren(QtWidgets.QWidget):
            child.setVisible(bool(visible))
        self.advanced_group.updateGeometry()

    def _open_install_report(self):
        path=_install_report_path()
        if path.exists():
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            else:
                _open_folder(str(path.parent))
        else:
            QtWidgets.QMessageBox.information(self,"AutoLens Installation","No install report exists yet. Run install_autolens.bat from the AutoLens package first.")

    def _refresh_install_status(self):
        cfg=_install_config()
        ready=bool(cfg.get("ready"))
        py=_installed_python(); cm=_installed_colmap()
        self.python_edit.setText(py)
        self.colmap_edit.setText(cm)
        if ready and py and cm:
            self.install_status.setText("✓ " + _installed_backend_label() + " is ready")
            self.install_status.setStyleSheet("color: #7fd27f;")
        else:
            stage = str(cfg.get("stage") or "unknown")
            error = str(cfg.get("error") or "").strip()
            message = f"✗ AutoLens runtime not ready — installer stage: {stage}"
            if error:
                short_error = error.splitlines()[0]
                if len(short_error) > 100:
                    short_error = short_error[:97] + "..."
                message += f" — {short_error}"
            self.install_status.setText(message)
            self.install_status.setToolTip(error)
            self.install_status.setStyleSheet("color: #e08080;")
        return ready and bool(py) and bool(cm)

    def _python_dependency_check(self, python_exe=None):
        python_exe = (python_exe or self.python_edit.text().strip() or "python")
        try:
            proc = subprocess.run(
                [python_exe, "-c", "import sys,numpy,OpenEXR; print(sys.executable); print(sys.version.split()[0]); print(numpy.__version__)"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            return proc.returncode == 0, (proc.stdout.strip() or proc.stderr.strip())
        except Exception as exc:
            return False, str(exc)

    def _autolens_venv_python(self):
        root = Path.home() / ".nuke" / "AutoLens" / ".venv"
        if sys.platform.startswith("win"):
            return root / "Scripts" / "python.exe"
        return root / "bin" / "python"

    def setup_python_environment(self):
        """Create an isolated runtime and install the two engine dependencies."""
        base_python = self.python_edit.text().strip() or "python"
        venv_python = self._autolens_venv_python()
        venv_root = venv_python.parents[1] if sys.platform.startswith("win") else venv_python.parents[1]

        self._append_log("\n[AUTOLENS] Setting up isolated Python environment...\n")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            if not venv_python.exists():
                create = subprocess.run(
                    [base_python, "-m", "venv", str(venv_root)],
                    capture_output=True,
                    text=True,
                )
                if create.returncode != 0:
                    raise RuntimeError(create.stderr.strip() or create.stdout.strip() or "Could not create Python virtual environment.")

            # Keep pip current enough to resolve modern NumPy/OpenEXR wheels.
            subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
                capture_output=True,
                text=True,
            )
            install = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--upgrade", "numpy", "OpenEXR"],
                capture_output=True,
                text=True,
            )
            if install.returncode != 0:
                raise RuntimeError(install.stderr.strip() or install.stdout.strip() or "pip install failed.")

            ok, detail = self._python_dependency_check(str(venv_python))
            if not ok:
                raise RuntimeError(detail)

            self.python_edit.setText(str(venv_python))
            self._save_settings()
            self._append_log(f"[AUTOLENS] Python environment ready: {venv_python}\n{detail}\n")
            QtWidgets.QMessageBox.information(
                self,
                "AutoLens Python Ready",
                "AutoLens created its own Python environment and installed NumPy + OpenEXR.\n\n"
                f"{venv_python}"
            )
            return True
        except Exception as exc:
            self._append_log(f"[AUTOLENS] Python setup failed: {exc}\n")
            QtWidgets.QMessageBox.warning(
                self,
                "AutoLens Python Setup",
                "AutoLens could not set up its Python environment.\n\n"
                f"{exc}\n\n"
                "You can also browse to another Python installation that already has NumPy and OpenEXR."
            )
            return False
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _find_colmap(self):
        return _installed_colmap()

    def _colmap_smoke_test(self, colmap_path):
        try:
            if not colmap_path or not Path(colmap_path).exists():
                return False, -1, "Managed COLMAP launcher is missing."

            path = Path(colmap_path)

            if path.suffix.lower() == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))

                if data.get("type") == "autolens-managed-colmap":
                    conda = data.get("conda")
                    env_prefix = data.get("env_prefix")

                    proc = subprocess.run(
                        [
                            str(conda),
                            "run",
                            "-p", str(env_prefix),
                            "--no-capture-output",
                            "colmap",
                            "-h",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )

                    detail = proc.stdout.strip() or proc.stderr.strip()
                    return proc.returncode == 0, int(proc.returncode), detail

            # Legacy fallback.
            comspec = os.environ.get(
                "COMSPEC",
                r"C:\Windows\System32\cmd.exe"
            )

            cmdline = "call " + subprocess.list2cmdline(
                [str(colmap_path), "-h"]
            )

            proc = subprocess.run(
                [comspec, "/d", "/s", "/c", cmdline],
                cwd=str(Path(colmap_path).parent),
                capture_output=True,
                text=True,
                timeout=60,
            )

            detail = proc.stdout.strip() or proc.stderr.strip()
            return proc.returncode == 0, int(proc.returncode), detail

        except Exception as exc:
            return False, -1, str(exc)

    def _preflight_before_run(self):
        if not self._refresh_install_status():
            QtWidgets.QMessageBox.warning(
                self,
                "AutoLens Installation Not Ready",
                "AutoLens will not start reconstruction because the installer has not validated this machine.\n\n"
                "Run install_autolens.bat outside Nuke. It will create the Python runtime and AutoLens private dependency-resolved CUDA COLMAP backend.\n\n"
                f"Report:\n{_install_report_path()}"
            )
            return False
        return True


    def _restore_settings(self):
        self.python_edit.setText(_installed_python())
        self.colmap_edit.setText(_installed_colmap())
        quality = str(self.settings.value("quality", "Normal"))
        idx = self.quality_combo.findText(quality)
        if idx >= 0: self.quality_combo.setCurrentIndex(idx)
        self.import_when_done.setChecked(str(self.settings.value("import_when_done", "true")).lower() != "false")
        self._refresh_install_status()


    def _save_settings(self):
        self.settings.setValue("quality", self.quality_combo.currentText())
        self.settings.setValue("import_when_done", self.import_when_done.isChecked())


    def _quality_changed(self, label):
        p = QUALITY[label]
        self.resolution_spin.setValue(p["resolution"]); self.target_spin.setValue(p["target"]); self.minimum_spin.setValue(p["minimum"])
        self.coverage_spin.setValue(p["coverage"]); self.sources_spin.setValue(p["sources"]); self.selection_combo.setCurrentText("smart")
        self.quality_summary.setText(f"Smart viewpoint selection • up to {p['target']} dense references • {p['resolution']} px • {p['coverage']*100:.0f}% sparse scene coverage")

    def use_selected_read(self, node=None):
        node = node or _selected_read()
        if node is None:
            QtWidgets.QMessageBox.information(self, "AutoLens", "Select exactly one Read node, then click Use Selected Read.")
            return
        filename = _safe_node_filename(node)
        if not filename:
            QtWidgets.QMessageBox.warning(self, "AutoLens", "Could not resolve the selected Read node's file path.")
            return
        source_folder = str(Path(filename).parent)
        self.source_edit.setText(source_folder)
        self.source_read_fullname = node.fullName()

        # Output defaults follow the actual plate/source Read, never the
        # location of the current Nuke script.
        root = Path(source_folder)
        self.output_edit.setText(
            str(root / "AutoLens")
        )
        self._refresh_version_combo(
            prefer_latest=True,
        )

        self._update_input_info()
        self._update_guide_status()

    def _source_read_node(self):
        node = nuke.toNode(self.source_read_fullname) if self.source_read_fullname else None
        return node if node is not None and node.Class() == "Read" else None

    def _update_guide_status(self):
        node = find_guide_node(getattr(self, "controller_node", None))
        if node is None:
            self.guide_status.setText(
                "No guides yet. Draw them manually, or let Auto Suggest create editable rough floor/wall shapes."
            )
            return
        controller = getattr(self, "controller_node", None)
        count = shape_count(controller)
        frame = (
            int(node["autolens_guide_frame"].value())
            if node.knob("autolens_guide_frame")
            else int(nuke.frame())
        )

        source = self._source_read_node()
        range_text = ""

        if source is not None:
            info = clamp_guide_frame(
                controller=controller,
                source_read=source,
                requested_frame=frame,
                move_timeline=False,
            )
            frame = int(info["frame"])
            range_text = (
                f" • source range {info['first']}–{info['last']}"
                if info["first"] is not None
                else ""
            )

        self.guide_status.setText(
            f"{count} guide shape(s) • evaluated at frame {frame}{range_text}. "
            "Guides are stored privately inside the AutoLens node."
        )

    def _create_or_edit_guides(self):
        source = self._source_read_node() or _selected_read()
        if source is None:
            QtWidgets.QMessageBox.information(
                self, "AutoLens Surface Guides", "Select or load a source Read node first."
            )
            return
        self.source_read_fullname = source.fullName()
        ensure_guide_node(source, controller=getattr(self, "controller_node", None))
        self._update_guide_status()
        QtWidgets.QMessageBox.information(
            self,
            "AutoLens Surface Guides",
            "A native Roto node named AUTOLENS_SURFACE_GUIDES is ready.\n\n"
            "View/select that node and draw closed Bezier shapes around rough surfaces such as Floor or BackWall. "
            "The shapes can be loose; AutoLens uses the solved 3D evidence under each shape to place the plane."
        )

    def _auto_suggest_guides(self):
        source = self._source_read_node() or _selected_read()
        if source is None:
            QtWidgets.QMessageBox.information(
                self, "AutoLens Surface Guides", "Select or load a source Read node first."
            )
            return
        self.source_read_fullname = source.fullName()
        self.stage_label.setText("Analyzing current frame for rough surfaces…")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            result = auto_suggest_surfaces(source, controller=getattr(self, "controller_node", None))
            room_count = len(result.get("room_created", []))
            object_count = len(result.get("object_created", []))
            confidence = float(result.get("confidence", 0.0))
            self._append_log(
                "\n[AUTOLENS] Auto Suggest complete: "
                f"{room_count} room surface(s), {object_count} local object plane(s), "
                f"room-layout confidence {confidence:.2f}.\n"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Auto Suggest Surfaces", str(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.stage_label.setText("Ready")
            self._update_guide_status()

    def _set_guide_frame(self):
        try:
            controller = getattr(self, "controller_node", None)
            source = self._source_read_node()
            requested = int(nuke.frame())

            frame = set_guide_frame_to_current(
                controller=controller
            )

            if frame != requested and source is not None:
                frame_range = source_frame_range(source)
                self._append_log(
                    "\n[AUTOLENS] Guide frame auto-corrected: "
                    f"{requested} -> {frame} "
                    f"(valid {frame_range[0]}–{frame_range[1]}).\n"
                )
            else:
                self._append_log(
                    f"\n[AUTOLENS] Surface guide frame set to {frame}.\n"
                )

            self._update_guide_status()

        except Exception as exc:
            QtWidgets.QMessageBox.information(
                self,
                "AutoLens Surface Guides",
                str(exc)
            )

    def _update_input_info(self):
        folder = Path(self.source_edit.text().strip())
        if not folder.is_dir():
            self.input_info.setText("Input folder does not exist."); return
        count = sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS)
        read_text = f" • Read: {self.source_read_fullname}" if self.source_read_fullname else ""
        self.input_info.setText(f"{count} image files found{read_text}")

    def _discover_versions(self):
        root_text = self.output_edit.text().strip()

        if not root_text:
            return []

        root = Path(root_text)

        if not root.is_dir():
            return []

        found = []

        try:
            children = list(root.iterdir())
        except Exception:
            return []

        for child in children:
            if not child.is_dir():
                continue

            match = re.match(
                r"^v(\d+)$",
                child.name,
                re.IGNORECASE,
            )

            if match:
                found.append(
                    (
                        int(match.group(1)),
                        f"v{int(match.group(1))}",
                    )
                )

        found.sort(key=lambda item: item[0])
        return [name for _, name in found]


    def _refresh_version_combo(self, prefer_latest=False):
        versions = self._discover_versions()
        latest = versions[-1] if versions else ""

        if versions:
            next_version = (
                f"v{max(int(v[1:]) for v in versions) + 1}"
            )
        else:
            next_version = "v1"

        values = []

        if latest:
            values.append(
                f"Latest ({latest})"
            )

        values.extend(versions)
        values.append(
            f"Create New ({next_version})"
        )

        previous = self.version_combo.currentText()

        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItems(values)

        if prefer_latest or previous not in values:
            target = (
                f"Latest ({latest})"
                if latest
                else f"Create New ({next_version})"
            )
        else:
            target = previous

        index = self.version_combo.findText(target)

        if index >= 0:
            self.version_combo.setCurrentIndex(index)

        self.version_combo.blockSignals(False)
        self._sync_controller_output()


    def _selected_version(self):
        value = self.version_combo.currentText().strip()

        match = re.match(
            r"^(?:Latest|Create New)\s+\((v\d+)\)$",
            value,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

        if re.match(r"^v\d+$", value, re.IGNORECASE):
            return value

        return "v1"


    def _resolved_output_folder(self):
        root = self.output_edit.text().strip()

        if not root:
            return ""

        return str(
            Path(root)
            / self._selected_version()
        )


    def _sync_controller_output(self):
        controller = getattr(
            self,
            "controller_node",
            None,
        )

        if controller is None:
            return

        try:
            from .node import refresh_version_menu

            if controller.knob("autolens_output_root"):
                controller["autolens_output_root"].setValue(
                    self.output_edit.text().strip()
                )

            refresh_version_menu(
                controller,
                prefer_latest=False,
            )

            if controller.knob("autolens_version"):
                desired = self.version_combo.currentText()
                values = list(
                    controller["autolens_version"].values()
                )

                if desired in values:
                    controller["autolens_version"].setValue(
                        desired
                    )

            if controller.knob("autolens_output"):
                controller["autolens_output"].setValue(
                    self._resolved_output_folder()
                )
        except Exception:
            pass


    def _version_changed(self):
        self._sync_controller_output()
        self.restore_result_state()
        self._update_run_button_text()

        controller = getattr(
            self,
            "controller_node",
            None,
        )

        if controller is not None:
            try:
                from .node import (
                    refresh_result_status,
                    refresh_create_items,
                )

                refresh_result_status(
                    controller
                )

                # The controller's version knobChanged callback schedules a
                # deferred Create-menu rebuild. Do not force-rebuild the enum
                # inside this QComboBox signal itself.
            except Exception:
                pass


    def _browse_source(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "AutoLens Input Image Folder", self.source_edit.text())
        if path: self.source_edit.setText(path); self.source_read_fullname = None

    def _browse_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "AutoLens Output Root",
            self.output_edit.text(),
        )
        if path:
            self.output_edit.setText(path)
            self._refresh_version_combo(
                prefer_latest=True,
            )

    def _browse_python(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "External Python Executable", self.python_edit.text(), "Executables (*.exe);;All Files (*)")
        if path: self.python_edit.setText(path)

    def _browse_colmap(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "COLMAP Executable",
            self.colmap_edit.text(),
            "COLMAP (colmap.exe *.exe *.bat);;All Files (*)"
        )
        if path:
            self.colmap_edit.setText(normalize_colmap_path(path))

    def check_environment(self):
        ok=self._refresh_install_status()
        report=_install_report_path()
        cfg = _install_config()
        if ok:
            message = "AutoLens installation is READY."
        else:
            stage = cfg.get("stage", "unknown")
            error = cfg.get("error") or "No installer error was recorded."
            message = (
                "AutoLens managed runtime is NOT READY.\n\n"
                f"Installer stage: {stage}\n"
                f"Error: {error}\n\n"
                "Run install_autolens.bat again."
            )
        QtWidgets.QMessageBox.information(
            self,
            "AutoLens Installation",
            message + f"\n\nInstall report:\n{report}"
        )


    def _build_arguments(self):
        source = self.source_edit.text().strip()
        output = self._resolved_output_folder()
        if not Path(source).is_dir():
            raise RuntimeError("Choose a valid input image folder.")
        if not output:
            raise RuntimeError("Choose an output folder.")
        Path(output).mkdir(parents=True, exist_ok=True)
        quality_key = QUALITY[self.quality_combo.currentText()]["quality"]
        solve_source = source
        source_node = self._source_read_node()
        source_format = {"width": 0, "height": 0, "pixel_aspect": 1.0}
        if source_node is not None:
            source_format = source_format_info(source_node)
            if abs(float(source_format["pixel_aspect"]) - 1.0) > 1e-6:
                proxy_info = ensure_square_pixel_proxy(
                    getattr(self, "controller_node", None), source_node, output,
                    self.resolution_spin.value(), log=self._append_log,
                )
                if proxy_info.get("images_dir"):
                    solve_source = proxy_info["images_dir"]
        args = [
            "--images", solve_source, "--output", output, "--quality", quality_key,
            "--dense-selection", self.selection_combo.currentText(),
            "--dense-max-image-size", str(self.resolution_spin.value()),
            "--dense-target-frames", str(self.target_spin.value()),
            "--dense-min-frames", str(self.minimum_spin.value()),
            "--dense-coverage-target", str(self.coverage_spin.value()),
            "--dense-source-views", str(self.sources_spin.value()),
            "--nuke-aperture-mm", str(self.aperture_spin.value()),
            "--source-width", str(int(source_format.get("width") or 0)),
            "--source-height", str(int(source_format.get("height") or 0)),
            "--source-pixel-aspect", str(float(source_format.get("pixel_aspect") or 1.0)),
            "--dense-gpus", self.gpus_edit.text().strip() or "auto",
            "--patchmatch-cache-gb", self.patch_cache_edit.text().strip() or "auto",
            "--fusion-cache-gb", self.fusion_cache_edit.text().strip() or "auto",
        ]
        if self.sparse_gpu.isChecked(): args.append("--use-gpu")
        colmap = self.colmap_edit.text().strip()
        if colmap: args += ["--colmap", colmap]
        if self.run_mode.currentIndex() == 1:
            args.append("--force-dense")
        elif self.run_mode.currentIndex() == 2:
            args.append("--force")

        if not self.upright_world_checkbox.isChecked():
            args.append("--no-upright-world")

        controller = getattr(
            self,
            "controller_node",
            None,
        )

        if (
            controller is not None
            and source_node is not None
            and mask_input_connected(controller)
        ):
            mask_dir = export_input_alpha_masks(
                output,
                solve_source,
                source_node,
                controller,
                log=self._append_log,
            )

            if mask_dir is not None:
                args += [
                    "--mask-path",
                    str(mask_dir),
                ]
                self._append_log(
                    "\n[AUTOLENS MASK] Using connected input alpha mask.\n"
                )

        if self.use_guides_checkbox.isChecked():
            controller = getattr(self, "controller_node", None)
            source_node = self._source_read_node()
            guide_node = find_guide_node(controller)

            if guide_node is not None and source_node is not None:
                requested = (
                    int(guide_node["autolens_guide_frame"].value())
                    if guide_node.knob("autolens_guide_frame")
                    else int(nuke.frame())
                )
                info = clamp_guide_frame(
                    controller=controller,
                    source_read=source_node,
                    requested_frame=requested,
                    move_timeline=False,
                )
                if info["clamped"]:
                    self._append_log(
                        "\n[AUTOLENS] Guide frame was outside the source "
                        "range and was auto-corrected: "
                        f"{info['requested']} -> {info['frame']} "
                        f"(valid {info['first']}–{info['last']}).\n"
                    )

            guide_path = export_surface_guides(
                output,
                self.source_read_fullname,
                controller=controller,
            )
            if guide_path is not None:
                args += ["--surface-guides", str(guide_path)]
                self._append_log(
                    f"\n[AUTOLENS] Using {shape_count(controller)} "
                    f"surface guide shape(s): {guide_path}\n"
                )
        return args


    def _sparse_root_has_reusable_model(self, root):
        """
        Lightweight UI-side check matching the engine's resume behavior.

        We deliberately do not parse/revalidate the reconstruction here; the
        engine remains the authority. This only decides whether the button
        should visually say Resume.
        """
        root = Path(root)

        if not root.is_dir():
            return False

        try:
            children = list(root.iterdir())
        except Exception:
            return False

        for model_dir in children:
            if not model_dir.is_dir():
                continue

            # A normal COLMAP binary sparse model.
            binary_model = (
                (model_dir / "cameras.bin").exists()
                and (model_dir / "images.bin").exists()
                and (model_dir / "points3D.bin").exists()
            )

            # Or a model that has already been converted to TXT.
            txt_dir = model_dir / "txt"
            text_model = (
                (txt_dir / "cameras.txt").exists()
                and (txt_dir / "images.txt").exists()
                and (txt_dir / "points3D.txt").exists()
            )

            if binary_model or text_model:
                return True

        return False


    def _has_reusable_cache(self):
        """
        Return True when the current output folder appears to contain stages
        the engine can resume.

        The UI checks both:
          1. autolens_cache.json -> sparse_root
          2. sparse / sparse_global reconstruction folders

        The second path is important when a prior run successfully solved the
        camera but crashed before autolens_cache.json was written.
        """
        output_text = self._resolved_output_folder()

        if not output_text:
            return False

        output = Path(output_text)

        if not output.is_dir():
            return False

        cache_path = output / "autolens_cache.json"

        if cache_path.exists():
            try:
                payload = json.loads(
                    cache_path.read_text(
                        encoding="utf-8"
                    )
                )
                sparse_root = payload.get("sparse_root")

                if (
                    sparse_root
                    and self._sparse_root_has_reusable_model(
                        sparse_root
                    )
                ):
                    return True
            except Exception:
                # An unreadable JSON cache should not prevent fallback
                # discovery of the sparse directories.
                pass

        for name in ("sparse", "sparse_global"):
            if self._sparse_root_has_reusable_model(
                output / name
            ):
                return True

        return False


    def _update_run_button_text(self):
        if not hasattr(self, "run_button"):
            return

        resume_mode = (
            hasattr(self, "run_mode")
            and self.run_mode.currentIndex() == 0
        )

        reusable = (
            resume_mode
            and self._has_reusable_cache()
        )

        if reusable:
            self.run_button.setText(
                "Resume AutoLens"
            )
            self.run_button.setToolTip(
                "Reusable AutoLens cache/reconstruction detected in the "
                "current output folder. Cached stages will be reused where "
                "valid."
            )
        else:
            self.run_button.setText(
                "Run AutoLens"
            )

            if resume_mode:
                self.run_button.setToolTip(
                    "Resume mode is selected, but no reusable AutoLens cache "
                    "was detected in the current output folder. A new solve "
                    "will start."
                )
            else:
                self.run_button.setToolTip(
                    "Run AutoLens using the selected rebuild mode."
                )


    def _discover_result_path(self):
        """Find a completed result in exactly the currently-selected output folder."""
        output_text = self._resolved_output_folder()
        result = find_result_nk(output_text) if output_text else None
        return str(result) if result is not None else None


    def _remember_result_path(self, path):
        if not path:
            return

        self.last_result_nk = str(path)

        controller = getattr(self, "controller_node", None)
        if controller is not None:
            try:
                from .node import ensure_runtime_knobs
                ensure_runtime_knobs(controller)
                controller["autolens_result_nk"].setValue(str(path))
                from .node import refresh_result_status
                refresh_result_status(controller)
            except Exception:
                pass

    def _set_completed_result_progress(self, completed):
        """
        Idle Solve-page status:
          completed selected version -> 100%
          no completed result        -> 0%

        Active solves continue to own live progress.
        """
        try:
            if self.runner.is_running():
                return
        except Exception:
            pass

        try:
            self.progress.setRange(
                0,
                100,
            )

            if completed:
                self.progress.setValue(
                    100
                )
                self.progress.setFormat(
                    "%p%"
                )
            else:
                self.progress.setValue(
                    0
                )
                self.progress.setFormat(
                    "%p%"
                )

            self.progress.update()

        except Exception:
            pass


    def restore_result_state(self):
        """Update the obvious completed-result status for the current output folder."""
        path = self._discover_result_path()
        self.last_result_nk = path

        self.import_button.setEnabled(bool(path))
        self.completed_result_checkbox.setChecked(bool(path))

        # Keep the progress bar tied to the SAME completed-result truth used by
        # the green status UI. This is intentionally authoritative rather than
        # relying on later branch-specific bookkeeping.
        self._set_completed_result_progress(
            bool(path)
        )

        controller = getattr(self, "controller_node", None)
        if controller is not None:
            try:
                from .node import refresh_result_status
                refresh_result_status(controller)
            except Exception:
                pass

        if path:
            guide_restore = None
            controller = getattr(self, "controller_node", None)

            if controller is not None:
                try:
                    from .node import restore_result_guides
                    guide_restore = restore_result_guides(
                        controller,
                        output=self._resolved_output_folder(),
                    )
                except Exception:
                    guide_restore = None

            self.completed_result_checkbox.setStyleSheet(
                "QCheckBox:disabled { "
                "color: #63d471; "
                "font-weight: 700; "
                "}"
            )

            if guide_restore and guide_restore.get("restored"):
                count = int(guide_restore.get("count", 0))
                self.stage_label.setText(
                    f"Completed Result Available • {count} guide(s) restored"
                )
                self._append_log(
                    "\n[AUTOLENS] Restored surface guides from completed track:\n"
                    f"  {guide_restore.get('path')}\n"
                    f"  {count} shape(s) at frame {guide_restore.get('frame')}\n"
                )
                self._update_guide_status()

                parent = self.parent()
                if parent is not None and hasattr(parent, "_reload_surfaces"):
                    try:
                        QtCore.QTimer.singleShot(
                            0,
                            parent._reload_surfaces,
                        )
                    except Exception:
                        pass
            else:
                self.stage_label.setText("Completed Result Available")
            self.stage_label.setStyleSheet(
                "color: #63d471; font-weight: 700;"
            )
            self.import_button.setToolTip(
                "Import/re-import completed AutoLens result:\n" + path
            )
        else:
            self.completed_result_checkbox.setStyleSheet(
                "QCheckBox:disabled { "
                "color: #858585; "
                "font-weight: 600; "
                "}"
            )
            if not self.runner.is_running():
                self.stage_label.setText("Ready")
                self.stage_label.setStyleSheet("")
            self.import_button.setToolTip(
                "Import becomes available when the current output folder "
                "contains a completed AutoLens result."
            )

        self._update_run_button_text()
        return path

        self._set_completed_result_progress(
            bool(path)
        )

    def _ask_existing_result_action(self):
        """
        If the selected solve version already contains a result, confirm reuse.
        Creating a new version is now explicit in the Solve version dropdown.
        """
        existing = self._discover_result_path()

        if not existing:
            return "overwrite"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("AutoLens Result Already Exists")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(
            "The selected solve version already contains a completed AutoLens track."
        )
        box.setInformativeText(
            "Continue using this version with the currently-selected Run mode?\n\n"
            "To start a clean new version, choose Create New in the Solve version dropdown."
        )

        reuse_button = box.addButton(
            "Reuse Selected Version",
            QtWidgets.QMessageBox.AcceptRole,
        )
        cancel_button = box.addButton(
            QtWidgets.QMessageBox.Cancel,
        )

        box.setDefaultButton(reuse_button)
        box.exec()

        return (
            "overwrite"
            if box.clickedButton() is reuse_button
            else None
        )


    def run_autolens(self):
        if self.runner.is_running():
            return

        if not self._preflight_before_run():
            return

        action = self._ask_existing_result_action()
        if action is None:
            return

        output_folder = Path(
            self._resolved_output_folder()
        )
        output_folder.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._sync_controller_output()

        try:
            args = self._build_arguments()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "AutoLens",
                str(exc)
            )
            return

        self._save_settings()
        # Keep the previous completed result remembered until a new successful
        # result replaces it. This allows re-import even if a later run fails.
        self.import_button.setEnabled(False)
        self.progress.setValue(0)
        self.stage_label.setText("Starting AutoLens…")
        self.stage_label.setStyleSheet("")
        self.runner.start(self.python_edit.text().strip() or "python", str(engine_path()), args)

    def _process_started(self):
        self.run_button.setEnabled(False)
        self.background_button.setEnabled(True)
        self.cancel_button.setEnabled(True)

    def _process_finished(self, exit_code):
        self.run_button.setEnabled(True)
        self.background_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._update_run_button_text()
        output = Path(self._resolved_output_folder()); nk_path = output / "AutoLens_v08.nk"
        if exit_code == 0 and nk_path.exists():
            self._remember_result_path(str(nk_path))
            self.restore_result_state()
            self._set_completed_result_progress(True)

            if self.import_when_done.isChecked():
                try: self.import_last_result()
                except Exception as exc: QtWidgets.QMessageBox.warning(self, "AutoLens Import", f"Solve completed, but result import failed:\n\n{exc}")
        elif exit_code != 0:
            self.stage_label.setText("AutoLens stopped or failed")
            self.restore_result_state()
            QtWidgets.QMessageBox.warning(
                self,
                "AutoLens",
                "AutoLens did not complete successfully.\n\n"
                "Check the log for the last COLMAP message."
            )

    def import_last_result(self):
        path = self._discover_result_path() or self.last_result_nk

        if not path:
            QtWidgets.QMessageBox.information(
                self,
                "AutoLens",
                "No completed AutoLens result is available yet."
            )
            return

        controller = getattr(self, "controller_node", None)
        controller_name = (
            controller.fullName()
            if controller is not None
            else None
        )

        if controller is not None:
            try:
                from .node import refresh_guided_surfaces_for_node

                refresh_info = refresh_guided_surfaces_for_node(
                    controller,
                    quiet_if_no_guides=True,
                )

                if refresh_info.get("ran"):
                    self._append_log(
                        "\n[AUTOLENS] Guided-surfaces-only refresh:\n"
                    )
                    if refresh_info.get("output"):
                        self._append_log(
                            refresh_info["output"] + "\n"
                        )

                    path = self._discover_result_path() or path

            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self,
                    "AutoLens Guided Surface Refresh",
                    "The completed track is still intact, but the current "
                    "guide shapes could not be refreshed:\n\n"
                    f"{exc}"
                )
                return

        pasted = import_result(
            path,
            self.source_read_fullname,
            controller_name,
        )

        self._remember_result_path(path)
        self.import_button.setEnabled(True)

        self._append_log(
            f"\n[AUTOLENS] Imported {len(pasted)} result nodes into current comp.\n"
            f"[AUTOLENS] Result remains available for re-import: {path}\n"
        )

    def _append_log(self, text):
        cursor = self.log.textCursor(); cursor.movePosition(QtGui.QTextCursor.End); cursor.insertText(text); self.log.setTextCursor(cursor); self.log.ensureCursorVisible()

    def _progress_changed(self, percent, stage):
        self.progress.setValue(percent)
        self.stage_label.setStyleSheet("")
        self.stage_label.setText(stage)

_REGISTERED = False

def register_panel():
    global _REGISTERED
    if _REGISTERED: return
    panels.registerWidgetAsPanel("autolens.panel.AutoLensPanel", "AutoLens", PANEL_ID, create=False)
    _REGISTERED = True

def show_panel():
    register_panel()
    return panels.registerWidgetAsPanel("autolens.panel.AutoLensPanel", "AutoLens", PANEL_ID, create=True)
