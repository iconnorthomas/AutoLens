"""autolens.build: which build of AutoLens is installed (no Nuke needed).

When AutoLens is installed from a git checkout, the installer writes
``autolens_build.json`` next to the plugin's ``init.py`` with the branch,
commit, pull request number and whether the tree had uncommitted changes.
A release zip has no such file, so the label is empty and only the version
shows.
"""
import json
import os

BUILD_FILE = "autolens_build.json"

_cached = None


def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_build_info(root=None):
    """The installer's build info dict, or {} for a release install."""
    path = os.path.join(root or plugin_root(), BUILD_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def format_build_label(info):
    """'PR #8 · d9bf93a', 'my-branch · d9bf93a', '' for a release; '+dirty' when uncommitted."""
    if not info:
        return ""
    parts = []
    pr = info.get("pr")
    if pr:
        parts.append("PR #%s" % pr)
    elif info.get("branch"):
        parts.append(str(info["branch"]))
    commit = info.get("commit")
    if commit:
        parts.append(str(commit) + ("+dirty" if info.get("dirty") else ""))
    return " · ".join(parts)


def build_label():
    """Cached label for the installed build ('' for a release install)."""
    global _cached
    if _cached is None:
        _cached = format_build_label(load_build_info())
    return _cached


def version_text(version):
    """'0.10.30 (PR #8 · d9bf93a)', or just the version for a release install."""
    label = build_label()
    return "%s (%s)" % (version, label) if label else str(version)
