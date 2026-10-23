# Changelog

## Unreleased

- **Simpler path from plate to solve** (see `docs/UX_REVIEW.md`).
  - The Solve tab shows *Setup › Solve › Review › Create* with a next-step hint.
  - The main action is now one **Solve** button under a Quality preset, and each
    preset has a plain-language description.
  - Run mode and GPU options are in the collapsed Advanced section.
  - The installation box shrinks to one line when everything is installed.
- **Readable progress.** The status shows *Step n of 11*, elapsed time and a rough
  time remaining.
- **Plain-language errors.** A failed solve says what went wrong and what to do next
  (install, COLMAP, GPU memory, disk, plate, mask, unsolvable camera) instead of
  "check the log". Cancelling no longer shows an error.
- **Create All.** *Import Result* is now **Create All** on the node and the Solve tab.
  It works the same way.
- **Solve as New Version.** When the selected version already holds a solve, the
  prompt offers *Solve as New Version* next to *Re-run*.
- **Node layout.** New AutoLens nodes put **Open AutoLens...** and **Create All** at
  the top, and move Run mode, Use surface guides and auto-create to the AutoLens
  Advanced tab. Existing nodes keep their layout and get the new labels.
- Tooltips on every node knob and Solve / Surfaces / Masks control.
- **Fix: Create from solve failed for Proxy Mesh, Mesh-fit Planes and Guided Surfaces
  (Combined)** with `name 'json' is not defined`. Guided surfaces now show their
  friendly names in the list again.

- **Build label for test installs.** Installing from a git checkout records the branch, commit,
  pull request number and uncommitted-changes flag in `autolens_build.json`, and the workspace
  shows it next to the version, e.g. `AutoLens 0.10.30 (PR #8 · d9bf93a)`. Release installs show
  only the version.
- **Version guard and release workflow** for maintainers (`scripts/release.py`, README
  *Version numbers and releases*). Pull requests run the unit tests and a *Version check*: a
  branch that changes `__version__` must raise it above `main` and every posted release, keep
  the installer's version in step, and move its CHANGELOG notes. Pushing a `vX.Y.Z` tag on
  `main` checks the number again, builds the release zip from that exact commit and posts the
  GitHub Release with its CHANGELOG notes.
- **The release zip is no longer committed.** `releases/` is git-ignored; download AutoLens from
  the latest GitHub Release, whose zip is always built from that release's code.
- **Fix: AutoLens failed to load on Nuke 14 (Python 3.9).** `autolens/paths.py` used a
  `Path | None` annotation, which Python 3.9 can't evaluate, so the AutoLens menu never
  appeared. The new CI runs the tests on Python 3.9, 3.10 and 3.11.
- `RELEASE_NOTES.md` is now this CHANGELOG. The one-off `GITHUB_PUBLISHING.md` checklist is gone.

## 0.10.30 - 2026-09-13

- Per-surface guide frames.
- Multiple blue guide-frame markers in the timeline.
- Surfaces are green on their own guide frame and gray/white on other frames.
- Lower overlay opacity.
- The engine resolves the camera pose per guide surface.
- Per-surface source image / frame exported to the guide JSON.

## 0.10.29

- Full-screen / maximized workspace.
- Wider right-side properties panel.
- Result-progress synchronization work.

## 0.10.28

- Completed-result progress fix.

## 0.10.27

- A completed solve shows 100% progress.

## 0.10.26

- Silent mask preview.
- Mask invert changes the visualization.
- The mask tab hides surfaces.

## 0.10.25

- Timeline.
- External alpha tracking-mask input.
- Simplified top toolbar.

## 0.10.24

- Immediate solve-version UI refresh.

## 0.10.23

- Solve-version state synchronization.

## 0.10.22

- Output root + solve version manager.

## 0.10.21

- Create From Solve improvements and guided surface asset discovery (0.10.20-0.10.21).

## 0.10.19

- Individual guided surface OBJ export.
- Planar UVs.
- Clean node-only result import.

## 0.10.10

- Viewer navigation improvements, mask workflow, auto upright world, guide-depth robustness,
  background solve and runtime preflight (0.10.10 and later).
