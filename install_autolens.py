#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

VERSION = "0.10.30"

MINIFORGE_URL = (
    "https://github.com/conda-forge/miniforge/releases/latest/download/"
    "Miniforge3-Windows-x86_64.exe"
)
COLMAP_VERSION = "4.2.0"
PYCOLMAP_VERSION = "4.2.0"

TARGET = Path.home() / ".nuke" / "AutoLens"
LIVE_LOG = TARGET / "AutoLens_Install_Live.log"
REPORT_JSON = TARGET / "autolens_install.json"
REPORT_TXT = TARGET / "AutoLens_Install_Report.txt"

_REPORT = {
    "version": VERSION,
    "platform": platform.platform(),
    "ready": False,
    "backend": "managed-miniforge-conda-forge",
    "stage": "starting",
    "error": None,
    "python_runtime": None,
    "nvidia": None,
    "miniforge": None,
    "colmap": None,
}


def log(message=""):
    text = str(message)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        # stdout redirected to a cp1252 file/pipe; never let logging stop the install.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding), flush=True)
    try:
        TARGET.mkdir(parents=True, exist_ok=True)
        with LIVE_LOG.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def heading(text):
    log()
    log("=" * 78)
    log(text)
    log("=" * 78)


def save_report(stage=None, error=None):
    if stage is not None:
        _REPORT["stage"] = stage
    if error is not None:
        _REPORT["error"] = error

    try:
        TARGET.mkdir(parents=True, exist_ok=True)
        REPORT_JSON.write_text(json.dumps(_REPORT, indent=2), encoding="utf-8")
        REPORT_TXT.write_text(
            f"AutoLens v{VERSION} Install Report\n"
            + "=" * 72 + "\n"
            + json.dumps(_REPORT, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"WARNING: Could not write install report: {exc}")


def fail(stage, message, exit_code=1):
    log()
    log("INSTALL FAILED")
    log(f"Stage: {stage}")
    log(str(message))
    save_report(stage=stage, error=str(message))
    log()
    log(f"Live log:\n  {LIVE_LOG}")
    log(f"Install report:\n  {REPORT_TXT}")
    return exit_code


def run_stream(cmd, cwd=None, env=None, timeout=None):
    cmd = [str(x) for x in cmd]
    log()
    log("> " + subprocess.list2cmdline(cmd))

    proc = None
    watchdog = None
    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    try:
        # Child tools (pip, conda, Miniforge) print UTF-8 progress glyphs; the
        # locale codec would raise mid-stream and abandon a still-running child.
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        # proc.wait(timeout) only runs after stdout closes, so a hung child that
        # keeps its pipe open would never time out. Kill it from a timer instead.
        if timeout:
            watchdog = threading.Timer(timeout, _kill_on_timeout)
            watchdog.daemon = True
            watchdog.start()

        captured = []
        assert proc.stdout is not None

        for line in proc.stdout:
            line = line.rstrip("\r\n")
            captured.append(line)
            log(line)

        proc.wait()
        if timed_out.is_set():
            return -1, "Process timed out.\n" + "\n".join(captured)
        return proc.returncode, "\n".join(captured)

    except Exception as exc:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return -1, str(exc)

    finally:
        if watchdog is not None:
            watchdog.cancel()
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()


def run_capture(cmd, cwd=None, env=None, timeout=60):
    try:
        proc = subprocess.run(
            [str(x) for x in cmd],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return -1, "", str(exc)


def _restore_preserved(preserve_root: Path, target: Path):
    """Move runtimes parked in preserve_root back into target (never overwrite)."""
    restored = []
    if not preserve_root.is_dir():
        return restored
    for item in preserve_root.iterdir():
        dest = target / item.name
        if dest.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(dest))
        restored.append(item.name)
    return restored


def install_plugin(source: Path, target: Path, nuke_dir: Path):
    save_report("installing-plugin")
    heading("1. Installing AutoLens Nuke Plugin")

    preserve_names = (
        ".venv",
        ".miniforge3",
        ".colmap_env",
        ".managed_colmap",
    )

    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if source_resolved == target_resolved:
        # The package was unzipped straight into ~/.nuke; deleting the target
        # would delete the plugin being installed.
        log(f"Plugin is already in place:\n  {target}")
        preserve_names = ()
    else:
        try:
            source_resolved.relative_to(target_resolved)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                "The installer is running from inside the folder it replaces:\n"
                f"  {source}\n"
                "Move the unzipped AutoLens package somewhere else (for example "
                "Downloads) and run the installer again."
            )

    preserve_root = nuke_dir / ".autolens_upgrade_preserve"
    # An earlier run that stopped mid-upgrade leaves the managed runtimes here.
    # Put them back first instead of deleting several GB of environments.
    restored = _restore_preserved(preserve_root, target)
    if restored:
        log("Recovered runtimes from an interrupted upgrade: " + ", ".join(restored))
    shutil.rmtree(preserve_root, ignore_errors=True)
    preserve_root.mkdir(parents=True, exist_ok=True)

    preserved = []

    try:
        if target.exists() and source_resolved != target_resolved:
            for name in preserve_names:
                item = target / name
                if item.exists():
                    dest = preserve_root / name
                    shutil.move(str(item), str(dest))
                    preserved.append(name)

            shutil.rmtree(target)

        if source_resolved != target_resolved:
            shutil.copytree(source, target)
    except Exception:
        # For example a file in the old plugin is locked by a running Nuke.
        _restore_preserved(preserve_root, target)
        raise

    for name in preserved:
        old = preserve_root / name
        if old.exists():
            new = target / name
            if new.exists():
                if new.is_dir():
                    shutil.rmtree(new)
                else:
                    new.unlink()
            shutil.move(str(old), str(new))

    shutil.rmtree(preserve_root, ignore_errors=True)

    init_py = nuke_dir / "init.py"
    content = (
        init_py.read_text(encoding="utf-8", errors="replace")
        if init_py.exists()
        else ""
    )

    if (
        "nuke.pluginAddPath('./AutoLens')" not in content
        and 'nuke.pluginAddPath("./AutoLens")' not in content
    ):
        with init_py.open("a", encoding="utf-8") as f:
            f.write(
                "\n# AutoLens NukeX plugin\n"
                "import nuke\n"
                "nuke.pluginAddPath('./AutoLens')\n"
            )

    log(f"Plugin installed to:\n  {target}")


def setup_python_runtime(target: Path):
    save_report("python-runtime")
    heading("2. AutoLens Python Runtime")

    root = target / ".venv"
    py = root / "Scripts" / "python.exe"

    if not py.exists():
        log(f"Creating isolated Python environment:\n  {root}")
        rc, _ = run_stream([sys.executable, "-m", "venv", str(root)])
        if rc != 0:
            return None
    else:
        log(f"Reusing isolated Python environment:\n  {root}")

    rc, _ = run_stream([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    if rc != 0:
        return None

    rc, _ = run_stream([
        str(py), "-m", "pip", "install", "--upgrade",
        "numpy", "OpenEXR",
    ])
    if rc != 0:
        return None

    rc, out, err = run_capture([
        str(py),
        "-c",
        "import sys,numpy,OpenEXR;"
        "print(sys.executable);"
        "print('Python',sys.version.split()[0]);"
        "print('NumPy',numpy.__version__);"
        "print('OpenEXR OK')",
    ], timeout=30)

    log(out.strip() or err.strip())

    if rc != 0:
        return None

    return py


def nvidia_status():
    exe = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")

    if not exe:
        candidate = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "NVIDIA Corporation"
            / "NVSMI"
            / "nvidia-smi.exe"
        )
        if candidate.exists():
            exe = str(candidate)

    if not exe:
        return {
            "available": False,
            "path": None,
            "detail": "nvidia-smi not found",
            "cuda_max": None,
        }

    rc, out, err = run_capture([exe], timeout=20)
    text = out.strip() or err.strip()

    cuda_max = None
    match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)?)", text)
    if match:
        try:
            cuda_max = float(match.group(1))
        except Exception:
            pass

    gpu_rc, gpu_out, gpu_err = run_capture(
        [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
        timeout=20,
    )

    return {
        "available": rc == 0 and gpu_rc == 0,
        "path": exe,
        "detail": gpu_out.strip() or text,
        "cuda_max": cuda_max,
    }


def choose_cuda_candidates(nvidia):
    maximum = nvidia.get("cuda_max")
    candidates = []

    if maximum is None:
        return ["12.9"]

    if maximum >= 13.0:
        candidates.append("13.0")

    if maximum >= 12.9:
        candidates.append("12.9")

    return candidates


def powershell_exe():
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if candidate.exists():
        return str(candidate)

    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def download_file(url: str, output: Path):
    """
    Try PowerShell, curl, then urllib. Keep the installer log explicit.
    """
    ps = powershell_exe()

    if ps:
        log("Trying download with PowerShell Invoke-WebRequest...")
        script = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' "
            f"-OutFile '{str(output).replace(chr(39), chr(39)*2)}'"
        )
        rc, out, err = run_capture(
            [
                ps,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=300,
        )
        if rc == 0 and output.exists() and output.stat().st_size > 0:
            return True
        log(out.strip() or err.strip())

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        log("Trying download with curl...")
        rc, out, err = run_capture(
            [curl, "-L", "--fail", "--retry", "3", "-o", str(output), url],
            timeout=300,
        )
        if rc == 0 and output.exists() and output.stat().st_size > 0:
            return True
        log(out.strip() or err.strip())

    log("Trying download with Python urllib...")
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"AutoLens/{VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            with output.open("wb") as f:
                shutil.copyfileobj(response, f)

        return output.exists() and output.stat().st_size > 0

    except Exception as exc:
        log(f"urllib failed: {exc}")
        return False


def install_miniforge(target: Path):
    save_report("miniforge")
    heading("4. Managed Package Runtime (Miniforge)")

    prefix = target / ".miniforge3"
    conda = prefix / "Scripts" / "conda.exe"

    if conda.exists():
        rc, out, err = run_capture([str(conda), "--version"], timeout=30)
        if rc == 0:
            version = out.strip() or err.strip()
            log(f"Existing Miniforge is healthy: {version}")
            return {
                "ok": True,
                "prefix": str(prefix),
                "conda": str(conda),
                "version": version,
            }, ""

        log("Existing Miniforge installation is unhealthy; rebuilding it.")
        shutil.rmtree(prefix, ignore_errors=True)

    installer = target / "Miniforge3-Windows-x86_64.exe"

    try:
        installer.unlink()
    except OSError:
        pass

    log("Downloading official Miniforge Windows installer:")
    log(f"  {MINIFORGE_URL}")

    if not download_file(MINIFORGE_URL, installer):
        return None, "Could not download the Miniforge Windows installer."

    log(f"Downloaded {installer.stat().st_size / 1024.0 / 1024.0:.1f} MB.")

    # Official Miniforge project documents silent Windows installation with
    # /InstallationType=JustMe /RegisterPython=0 /S /D=<path>.
    cmd = [
        str(installer),
        "/InstallationType=JustMe",
        "/RegisterPython=0",
        "/S",
        f"/D={prefix}",
    ]

    rc, output = run_stream(cmd, timeout=600)

    try:
        installer.unlink()
    except OSError:
        pass

    if rc != 0:
        return None, (
            f"Miniforge installer returned {rc}.\n{output[-4000:]}"
        )

    if not conda.exists():
        return None, (
            "Miniforge installer completed but conda.exe was not found at:\n"
            f"{conda}"
        )

    rc, out, err = run_capture([str(conda), "--version"], timeout=30)
    if rc != 0:
        return None, (
            "Miniforge installed but conda.exe did not start:\n"
            + (out.strip() or err.strip())
        )

    version = out.strip() or err.strip()
    log(f"Miniforge ready: {version}")

    return {
        "ok": True,
        "prefix": str(prefix),
        "conda": str(conda),
        "version": version,
    }, ""


def conda_env_ok(conda: Path, env_prefix: Path):
    if not env_prefix.exists():
        return False

    rc, out, err = run_capture(
        [
            str(conda),
            "run",
            "-p", str(env_prefix),
            "--no-capture-output",
            "colmap",
            "-h",
        ],
        timeout=90,
    )

    return rc == 0


def create_managed_colmap(
    target: Path,
    conda: Path,
    cuda_candidates,
):
    save_report("managed-colmap")
    heading("5. AutoLens Managed COLMAP")

    env_prefix = target / ".colmap_env"

    if conda_env_ok(conda, env_prefix):
        log(f"Existing managed COLMAP environment is healthy:\n  {env_prefix}")
        selected_cuda = "existing"

    else:
        if env_prefix.exists():
            log("Existing managed COLMAP environment is incomplete; rebuilding it.")
            shutil.rmtree(env_prefix, ignore_errors=True)

        selected_cuda = None
        last_output = ""

        for cuda_version in cuda_candidates:
            build_tag = "cuda_130*" if cuda_version == "13.0" else "cuda_129*"

            log()
            log(
                f"Attempting COLMAP {COLMAP_VERSION} CUDA {cuda_version}..."
            )

            specs = [
                "python=3.11",
                f"colmap={COLMAP_VERSION}={build_tag}",
                f"pycolmap={PYCOLMAP_VERSION}={build_tag}",
                f"cuda-version={cuda_version}",
            ]

            cmd = [
                str(conda),
                "create",
                "-y",
                "-p", str(env_prefix),
                "-c", "conda-forge",
                "--strict-channel-priority",
                "--solver", "libmamba",
                *specs,
            ]

            rc, output = run_stream(cmd, timeout=1800)
            last_output = output

            if rc == 0:
                selected_cuda = cuda_version
                break

            log(
                f"CUDA {cuda_version} environment solve/install failed; "
                "trying the next compatible CUDA runtime if available."
            )
            shutil.rmtree(env_prefix, ignore_errors=True)

        if selected_cuda is None:
            return None, (
                "Conda could not create the CUDA COLMAP environment.\n\n"
                + last_output[-6000:]
            )

    save_report("validating-colmap")

    tests = [
        (
            "COLMAP CLI",
            ["colmap", "-h"],
        ),
        (
            "COLMAP feature extractor",
            ["colmap", "feature_extractor", "-h"],
        ),
        (
            "COLMAP PatchMatch",
            ["colmap", "patch_match_stereo", "-h"],
        ),
        (
            "PyCOLMAP",
            [
                "python",
                "-c",
                "import pycolmap; print(pycolmap.__version__)",
            ],
        ),
    ]

    results = {}

    for label, inner in tests:
        command = [
            str(conda),
            "run",
            "-p", str(env_prefix),
            "--no-capture-output",
            *inner,
        ]

        rc, out, err = run_capture(command, timeout=120)
        detail = out.strip() or err.strip()

        results[label] = {
            "ok": rc == 0,
            "returncode": rc,
            "output": detail[-4000:],
        }

        log(f"{label}: {'PASS' if rc == 0 else 'FAIL'}")

        if rc != 0:
            log(detail)
            return None, f"{label} validation failed:\n{detail}"

    # The direct conda-run tests above are the authoritative validation.
    # Do NOT wrap conda run in another .cmd file: on some Windows systems the
    # nested cmd -> conda.bat/cmd lifecycle can remain open after COLMAP exits.
    launcher_dir = target / ".managed_colmap"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "launcher.json"

    launcher_payload = {
        "type": "autolens-managed-colmap",
        "version": 1,
        "conda": str(conda),
        "env_prefix": str(env_prefix),
        "command": "colmap",
    }
    launcher.write_text(
        json.dumps(launcher_payload, indent=2),
        encoding="utf-8",
    )

    log("Managed COLMAP launcher descriptor: PASS")


    return {
        "ok": True,
        "backend": "managed-miniforge-conda-forge",
        "path": str(launcher),
        "env_prefix": str(env_prefix),
        "conda": str(conda),
        "colmap_version": COLMAP_VERSION,
        "pycolmap_version": PYCOLMAP_VERSION,
        "cuda_version": selected_cuda,
        "test_results": results,
    }, ""



def unpack_result(value):
    """
    Installer helpers conventionally return (result, error).

    Accept a legacy single result object too so a successful cached-runtime
    path can never crash the installer with a tuple-unpacking error.
    """
    if isinstance(value, tuple):
        if len(value) == 2:
            return value[0], value[1]
        if len(value) == 1:
            return value[0], ""
    return value, ""


def main():
    TARGET.mkdir(parents=True, exist_ok=True)

    try:
        LIVE_LOG.unlink()
    except OSError:
        pass

    save_report("starting")

    heading(f"AutoLens NukeX Installer v{VERSION}")
    log(f"Platform: {platform.platform()}")
    log(f"Installer Python: {sys.executable}")
    log(f"Live log: {LIVE_LOG}")

    if os.name != "nt":
        raise SystemExit(
            fail(
                "platform-check",
                "The installer and its managed CUDA COLMAP backend "
                "currently support Windows only.",
                2,
            )
        )

    package = Path(__file__).resolve().parent
    source = package / "AutoLens"

    if not source.is_dir():
        raise SystemExit(
            fail(
                "package-check",
                f"AutoLens package folder is missing: {source}",
                2,
            )
        )

    nuke_dir = Path.home() / ".nuke"
    nuke_dir.mkdir(parents=True, exist_ok=True)

    # 1. Plugin.
    install_plugin(source, TARGET, nuke_dir)

    # 2. Python runtime.
    py = setup_python_runtime(TARGET)
    if py is None:
        raise SystemExit(
            fail(
                "python-runtime",
                "Could not create or validate the AutoLens Python runtime.",
                3,
            )
        )

    _REPORT["python_runtime"] = {
        "ok": True,
        "path": str(py),
    }
    save_report("nvidia-check")

    # 3. NVIDIA.
    heading("3. NVIDIA / CUDA Check")
    nvidia = nvidia_status()
    _REPORT["nvidia"] = nvidia
    save_report("nvidia-check")

    log(nvidia.get("detail") or "No NVIDIA information.")

    if not nvidia.get("available"):
        raise SystemExit(
            fail(
                "nvidia-check",
                "NVIDIA GPU/driver could not be validated with nvidia-smi.",
                4,
            )
        )

    cuda_candidates = choose_cuda_candidates(nvidia)

    if not cuda_candidates:
        raise SystemExit(
            fail(
                "nvidia-check",
                "The NVIDIA driver reports support below CUDA 12.9.",
                4,
            )
        )

    log("CUDA package candidates: " + ", ".join(cuda_candidates))

    # 4. Miniforge.
    miniforge, error = unpack_result(install_miniforge(TARGET))

    if miniforge is None:
        raise SystemExit(
            fail(
                "miniforge",
                error,
                5,
            )
        )

    _REPORT["miniforge"] = miniforge
    save_report("managed-colmap")

    conda = Path(miniforge["conda"])

    # 5. COLMAP.
    managed, error = unpack_result(create_managed_colmap(
        TARGET,
        conda,
        cuda_candidates,
    ))

    if managed is None:
        _REPORT["colmap"] = {
            "ok": False,
            "backend": "managed-miniforge-conda-forge",
        }
        raise SystemExit(
            fail(
                "managed-colmap",
                error,
                6,
            )
        )

    _REPORT["colmap"] = managed
    _REPORT["ready"] = True
    _REPORT["error"] = None
    save_report("complete")

    heading("AutoLens Installation Complete")
    log("AutoLens is READY.")
    log()
    log(f"Python runtime:\n  {py}")
    log(f"Miniforge:\n  {miniforge['prefix']}")
    log(f"Managed COLMAP environment:\n  {managed['env_prefix']}")
    log(f"Managed COLMAP launcher descriptor:\n  {managed['path']}")
    log()
    log(f"Install report:\n  {REPORT_TXT}")
    log(f"Live log:\n  {LIVE_LOG}")
    log()
    log("Restart NukeX, then open the AutoLens node.")

    return 0


if __name__ == "__main__":
    exit_code = 1

    try:
        exit_code = main()

    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    except Exception:
        details = traceback.format_exc()
        log(details)
        exit_code = fail(
            _REPORT.get("stage", "unexpected-error"),
            details,
            99,
        )

    log()
    log(f"Installer exit code: {exit_code}")

    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass

    raise SystemExit(exit_code)
