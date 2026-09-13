from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def colmap_root(value: str) -> Path | None:
    if not value:
        return None

    p = Path(value)
    if not p.exists():
        return None

    p = p.resolve()

    if p.suffix.lower() in (".bat", ".cmd"):
        return p.parent

    if p.name.lower() == "colmap.exe" and p.parent.name.lower() == "bin":
        return p.parent.parent

    return p.parent


def normalize_colmap_path(value: str) -> str:
    """
    Prefer the official Windows COLMAP.bat wrapper when available.
    """
    if not value:
        return value

    p = Path(value)
    if not p.exists():
        return value

    p = p.resolve()

    if sys.platform.startswith("win"):
        root = colmap_root(str(p))
        if root is not None:
            for candidate in (
                root / "COLMAP.bat",
                root / "colmap.bat",
                root / "COLMAP.cmd",
                root / "colmap.cmd",
            ):
                if candidate.exists():
                    return str(candidate.resolve())

    return str(p)


def powershell_path() -> str:
    """
    Prefer Windows PowerShell 5.1 because it is present on supported Windows
    installs and supports Start-Process -UseNewEnvironment.
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidates = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        Path("powershell.exe"),
        Path("pwsh.exe"),
    )

    for candidate in candidates:
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil_which(name)
        if found:
            return found

    return "powershell.exe"


def shutil_which(name: str):
    # Local tiny which to keep this module dependency-free.
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for folder in paths:
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    return None


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_fresh_windows_invocation(
    colmap_path: str,
    args: list[str],
):
    """
    Launch COLMAP through a brand-new Windows environment.

    The outer PowerShell process is inherited from Nuke, but Start-Process
    -UseNewEnvironment creates the actual cmd/COLMAP process using Windows'
    Machine/User environment instead of Nuke's environment.
    """
    launcher = normalize_colmap_path(colmap_path)
    root = colmap_root(launcher)
    working_dir = str(root) if root is not None else str(Path(launcher).parent)

    comspec = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "cmd.exe",
    )
    if not Path(comspec).exists():
        comspec = os.environ.get("COMSPEC", "cmd.exe")

    # Build one cmd.exe command string using Windows' own quoting rules.
    logical = [launcher] + [str(a) for a in args]
    cmdline = "call " + subprocess.list2cmdline(logical)

    ps = (
        "$ErrorActionPreference='Stop'; "
        "$p = Start-Process "
        f"-FilePath {_ps_quote(comspec)} "
        f"-ArgumentList @('/d','/s','/c',{_ps_quote(cmdline)}) "
        f"-WorkingDirectory {_ps_quote(working_dir)} "
        "-UseNewEnvironment -NoNewWindow -Wait -PassThru; "
        "exit $p.ExitCode"
    )

    return [
        powershell_path(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps,
    ]


def build_colmap_invocation(colmap_path: str, args: list[str]):
    normalized = normalize_colmap_path(colmap_path)

    if sys.platform.startswith("win"):
        return build_fresh_windows_invocation(normalized, args), None, None

    return [normalized] + [str(a) for a in args], None, None


def windows_status_hex(returncode: int) -> str:
    return f"0x{int(returncode) & 0xFFFFFFFF:08X}"
