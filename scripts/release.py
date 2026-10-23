"""AutoLens version guard and release helper.

A version number isn't a version until it's posted as a GitHub Release, and
several branches can be in flight at once, so the number is checked at both
ends:

* ``check-pr``      (CI, every pull request) A branch that changes
  ``__version__`` must raise it above ``main`` and above every posted release,
  and must have moved its CHANGELOG notes from "Unreleased" to that version.
  The installer's version must always match ``__version__``.
  Branches that don't touch the version always pass.
* ``check-release`` (release workflow, on a ``vX.Y.Z`` tag) The tag must equal
  ``__version__`` in the tagged commit, that commit must be on ``main``, the
  version must be newer than every earlier ``v*`` tag, and the CHANGELOG must
  have its section. Writes that section as the release notes.
* ``package``       (release workflow) Builds ``dist/AutoLens_NukeX_vX.Y.Z.zip``
  from the working tree, laid out like every earlier release zip.
* ``check-package`` (release workflow) The zip holds the plugin, the installer
  and exactly this version.
* ``bump X.Y.Z``    (you, on a release branch) Sets ``__version__`` and the
  installer's version, and turns "## Unreleased" into "## X.Y.Z - <today>".

Run ``python scripts/release.py --help``. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = "AutoLens/python/autolens/__init__.py"
CHANGELOG = "CHANGELOG.md"
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
STRICT_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Other files that carry the version and must agree with VERSION_FILE:
# (path, regex whose group 1 is the version, replacement template).
SYNCED = [
    ("install_autolens.py", re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.M),
     'VERSION = "%s"'),
    ("install_autolens.bat", re.compile(r"^title AutoLens NukeX v(\S+) Installer", re.M),
     "title AutoLens NukeX v%s Installer"),
    ("install_autolens.bat", re.compile(r"^echo AutoLens NukeX v(\S+) Installer", re.M),
     "echo AutoLens NukeX v%s Installer"),
]

# What goes in the release zip, relative to ROOT.
PACKAGE_FILES = ["install_autolens.bat", "install_autolens.py", "README.md", "LICENSE", "NOTICE"]
PACKAGE_DIRS = ["AutoLens"]


class CheckError(Exception):
    pass


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def parse(version):
    """'0.10.30' / 'v0.10.30' -> (0, 10, 30). Raises CheckError for anything else."""
    v = (version or "").strip()
    v = v[1:] if v[:1] in ("v", "V") else v
    if not STRICT_RE.match(v):
        raise CheckError("%r is not a version like 1.2.3" % version)
    return tuple(int(p) for p in v.split("."))


def version_in(text):
    m = VERSION_RE.search(text or "")
    if not m:
        raise CheckError("no __version__ in %s" % VERSION_FILE)
    return m.group(1)


def changelog_sections(text):
    """[(heading title, body)] for every '## ' heading, in order."""
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def changelog_section(text, version):
    """Body of '## X.Y.Z - date' (or '## X.Y.Z'), or None."""
    for title, body in changelog_sections(text):
        if re.match(r"^%s(\s|$)" % re.escape(version), title):
            return body
    return None


def unreleased_notes(text):
    for title, body in changelog_sections(text):
        if title.lower() == "unreleased":
            return body
    return ""


def release_tags(tags):
    """Versions of the 'vX.Y.Z' tags in ``tags``, newest first (others ignored)."""
    out = []
    for t in tags:
        try:
            out.append((parse(t), t))
        except CheckError:
            pass
    return [t for _v, t in sorted(out, reverse=True)]


def sync_problems(version, files):
    """Problems with the copies of the version in ``files`` ({path: text})."""
    problems = []
    for path, regex, _template in SYNCED:
        m = regex.search(files.get(path) or "")
        if not m:
            problems.append("%s has no version line matching %s" % (path, regex.pattern))
        elif m.group(1) != version:
            problems.append("%s says %s but __version__ is %s (run: python scripts/release.py "
                            "bump, or fix it by hand)" % (path, m.group(1), version))
    return problems


def check_pr(head_version, base_version, posted_tags, changelog, files=None):
    """Problems with a pull request's version (empty list = fine)."""
    problems = sync_problems(head_version, files) if files is not None else []
    if head_version == base_version:
        return problems
    try:
        new = parse(head_version)
    except CheckError as exc:
        return problems + [str(exc)]
    if new <= parse(base_version):
        problems.append("__version__ %s must be higher than main's %s (another branch may have "
                        "posted it first; merge main and pick the next number)"
                        % (head_version, base_version))
    tags = release_tags(posted_tags)
    if tags and new <= parse(tags[0]):
        problems.append("__version__ %s must be higher than the latest posted release %s"
                        % (head_version, tags[0]))
    if "v%s" % head_version in posted_tags:
        problems.append("v%s is already posted" % head_version)
    if changelog_section(changelog, head_version) is None:
        problems.append('CHANGELOG.md has no "## %s - <date>" section (run: python '
                        "scripts/release.py bump %s)" % (head_version, head_version))
    if unreleased_notes(changelog):
        problems.append('CHANGELOG.md still has notes under "## Unreleased"; a version bump '
                        "moves them into the new version's section")
    return problems


def check_release(tag, version, earlier_tags, changelog, on_main):
    """Problems with posting ``tag`` (empty list = fine)."""
    problems = []
    if tag != "v%s" % version:
        problems.append("tag %s doesn't match __version__ %s in that commit (expected v%s)"
                        % (tag, version, version))
    try:
        new = parse(version)
    except CheckError as exc:
        return problems + [str(exc)]
    if not on_main:
        problems.append("the tagged commit isn't on main; tag the merge commit on main")
    older = [t for t in release_tags(earlier_tags) if t != tag]
    if older and new <= parse(older[0]):
        problems.append("%s isn't newer than the latest earlier release %s" % (version, older[0]))
    notes = changelog_section(changelog, version)
    if not notes:
        problems.append('CHANGELOG.md has no notes under "## %s - <date>"' % version)
    return problems


def package_name(version):
    return "AutoLens_NukeX_v%s.zip" % version


def package_prefix(version):
    return "AutoLens_NukeX_Plugin_v%s/" % version


def check_package(names, init_text, version):
    """Problems with a release zip, given its member names and its __init__.py text."""
    problems = []
    prefix = package_prefix(version)
    required = [prefix + p for p in PACKAGE_FILES] + [
        prefix + VERSION_FILE,
        prefix + "AutoLens/init.py",
        prefix + "AutoLens/menu.py",
        prefix + "AutoLens/engine/autolens_engine.py",
    ]
    for path in required:
        if path not in names:
            problems.append("the zip is missing %s" % path)
    stray = [n for n in names if not n.startswith(prefix)]
    if stray:
        problems.append("the zip has files outside %s: %s" % (prefix, ", ".join(stray[:5])))
    junk = [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    if junk:
        problems.append("the zip has compiled Python files: %s" % ", ".join(junk[:5]))
    if init_text is not None:
        try:
            found = version_in(init_text)
        except CheckError as exc:
            problems.append(str(exc))
        else:
            if found != version:
                problems.append("the zip carries AutoLens %s, not %s" % (found, version))
    return problems


def bump(init_text, changelog, version, today=None, files=None):
    """(new __init__ text, new CHANGELOG text, {path: new text}) for releasing ``version``."""
    parse(version)
    if changelog_section(changelog, version) is not None:
        raise CheckError("CHANGELOG.md already has a %s section" % version)
    if not unreleased_notes(changelog):
        raise CheckError('there are no notes under "## Unreleased" to release')
    current = version_in(init_text)
    if parse(version) <= parse(current):
        raise CheckError("%s isn't higher than the current %s" % (version, current))
    today = today or datetime.date.today().isoformat()
    init_text = VERSION_RE.sub('__version__ = "%s"' % version, init_text, count=1)
    changelog = re.sub(r"^## +Unreleased[ \t]*$", "## Unreleased\n\n## %s - %s" % (version, today),
                       changelog, count=1, flags=re.M | re.I)
    files = dict(files or {})
    for path, regex, template in SYNCED:
        if path in files:
            files[path] = regex.sub(template % version, files[path], count=1)
    return init_text, changelog, files


# --------------------------------------------------------------------------
# git / files
# --------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    if path.endswith(".bat"):       # .gitattributes keeps batch files CRLF
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def synced_files():
    return {path: read(path) for path in sorted({p for p, _r, _t in SYNCED})}


def build_package(version, out_dir):
    """Write the release zip into ``out_dir`` and return its path."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, package_name(version))
    prefix = package_prefix(version)
    entries = list(PACKAGE_FILES)
    for top in PACKAGE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith((".pyc", ".pyo")):
                    continue
                entries.append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in entries:
            zf.write(os.path.join(ROOT, rel), prefix + rel.replace(os.sep, "/"))
    return path


def _report(problems, ok_message):
    if problems:
        for p in problems:
            print("ERROR: %s" % p)
        return 1
    print(ok_message)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-pr", help="CI: check a pull request's version against main")
    p.add_argument("--base", default="origin/main", help="git ref of the base branch")
    p = sub.add_parser("check-release", help="release workflow: check a vX.Y.Z tag")
    p.add_argument("--tag", required=True)
    p.add_argument("--main", default="origin/main", help="git ref of main")
    p.add_argument("--notes", help="write the release notes (CHANGELOG section) here")
    p = sub.add_parser("package", help="build dist/AutoLens_NukeX_vX.Y.Z.zip")
    p.add_argument("--out", default=os.path.join(ROOT, "dist"))
    p = sub.add_parser("check-package", help="release workflow: check a built release zip")
    p.add_argument("zip")
    p.add_argument("--version", required=True)
    sub.add_parser("version", help="print __version__")
    p = sub.add_parser("bump", help="set __version__ and date the Unreleased CHANGELOG notes")
    p.add_argument("version")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "check-pr":
            head = version_in(read(VERSION_FILE))
            base = version_in(git("show", "%s:%s" % (args.base, VERSION_FILE)))
            tags = git("tag", "--list", "v*").split()
            problems = check_pr(head, base, tags, read(CHANGELOG), synced_files())
            msg = ("version unchanged (%s)" % head if head == base
                   else "version %s -> %s is ready to post" % (base, head))
            return _report(problems, msg)
        if args.cmd == "check-release":
            version = version_in(read(VERSION_FILE))
            tags = git("tag", "--list", "v*").split()
            on_main = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", args.main],
                                     cwd=ROOT).returncode == 0
            changelog = read(CHANGELOG)
            problems = check_release(args.tag, version, tags, changelog, on_main)
            problems += sync_problems(version, synced_files())
            if not problems and args.notes:
                with open(args.notes, "w", encoding="utf-8") as fh:
                    fh.write(changelog_section(changelog, version) + "\n")
            return _report(problems, "%s is ready to post" % args.tag)
        if args.cmd == "version":
            print(version_in(read(VERSION_FILE)))
            return 0
        if args.cmd == "package":
            path = build_package(version_in(read(VERSION_FILE)), args.out)
            print(path)
            return 0
        if args.cmd == "check-package":
            version = args.version.lstrip("vV")
            try:
                with zipfile.ZipFile(args.zip) as zf:
                    names = zf.namelist()
                    member = package_prefix(version) + VERSION_FILE
                    init_text = (zf.read(member).decode("utf-8") if member in names else None)
            except (OSError, zipfile.BadZipFile) as exc:
                raise CheckError("couldn't read the release zip %s: %s" % (args.zip, exc))
            problems = check_package(names, init_text, version)
            return _report(problems, "%s holds AutoLens %s" % (os.path.basename(args.zip),
                                                               version))
        if args.cmd == "bump":
            init_text, changelog, files = bump(read(VERSION_FILE), read(CHANGELOG),
                                               args.version, files=synced_files())
            write(VERSION_FILE, init_text)
            write(CHANGELOG, changelog)
            for path, text in files.items():
                write(path, text)
            print("__version__ = %s (and the installer); CHANGELOG notes moved under '## %s'. "
                  "Commit this on a release branch, merge the PR, then tag the merge commit v%s "
                  "and push the tag." % (args.version, args.version, args.version))
            return 0
    except (CheckError, subprocess.CalledProcessError) as exc:
        print("ERROR: %s" % (getattr(exc, "stderr", "") or exc))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
