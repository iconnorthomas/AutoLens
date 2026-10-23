from __future__ import annotations

from pathlib import Path
import re

def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]

def engine_path() -> Path:
    return plugin_root() / "engine" / "autolens_engine.py"


def find_result_nk(output_folder) -> Path | None:
    """Return the completed AutoLens NK result in exactly this output folder."""
    if not output_folder:
        return None

    folder = Path(output_folder).expanduser()

    if not folder.is_dir():
        return None

    preferred = folder / "AutoLens_v08.nk"
    if preferred.exists():
        return preferred

    candidates = sorted(
        folder.glob("AutoLens_v*.nk"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def output_has_result(output_folder) -> bool:
    return find_result_nk(output_folder) is not None


def next_version_folder(output_folder) -> Path:
    """
    Return the next version folder for an output location.

    Base result:
        .../AutoLens -> .../AutoLens/v2

    If the controller already points at a version:
        .../AutoLens/v2 -> .../AutoLens/v3
    """
    current = Path(output_folder).expanduser()
    version_match = re.fullmatch(r"v(\d+)", current.name, re.IGNORECASE)

    if version_match:
        root = current.parent
        start = max(2, int(version_match.group(1)) + 1)
    else:
        root = current
        start = 2

    version = start
    while (root / f"v{version}").exists():
        version += 1

    return root / f"v{version}"
