# Changelog

## Unreleased

- **Faster Nuke launch and script open.** `menu.py` no longer imports the AutoLens controller
  module at launch; it loads when you add a node or use one. Opening a script now skips AutoLens
  nodes that this plugin version has already upgraded (new hidden `autolens_plugin_version` knob)
  and no longer scans every node's output folder for solve versions, results and guided
  surfaces. That refresh still runs when the node's properties or the workspace open. Set
  `AUTOLENS_FULL_REFRESH_ON_LOAD=1` to get the old behaviour back.
- **The workspace window opens before the plate preview loads.** The preview (which can be a
  Nuke render for EXR plates), surfaces, masks and timeline load right after the window
  appears. Opening also does less repeated work: the plate folder is counted once instead of
  up to three times, the result folder is scanned fewer times, and the
  installer config is read once. Set `AUTOLENS_DEFER_PREVIEW=0` to load the preview before the
  window shows, as before.
- **Fix: the workspace could not open on Nuke 14.** Its F (fit) shortcut used
  `QtGui.QShortcut`, which only exists in PySide6; PySide2 has it in `QtWidgets`.
- **Startup profiling.** Launch Nuke with `AUTOLENS_PROFILE=1` to print
  `[AutoLens profile] …` timings for launch, script open and workspace open. Run
  `import autolens.startup; autolens.startup.report()` in the Script Editor to list them again.
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
