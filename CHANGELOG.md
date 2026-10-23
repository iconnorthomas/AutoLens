# Changelog

## Unreleased

- **Fast scrubbing and real-time playback in the workspace viewer.** Frames are decoded on
  background threads at viewer size into a memory-bounded cache (1 GB by default; set
  `AUTOLENS_PREVIEW_CACHE_MB` to change it), and neighbouring frames are prefetched. Dragging
  the timeline shows the nearest cached frame at once; Nuke's frame, the surfaces and the mask
  overlay follow when the drag settles. New transport bar: first/previous/play backwards/play/
  next/last, Loop and an fps box (defaults to the script's fps). Playback stays in real time and
  skips frames that aren't ready. Keys: Space play/pause, ←/→ or ,/. step (Shift = 10 frames),
  Home/End. A green bar under the timeline shows cached frames.
- **EXR, DPX and other formats Qt can't read** are rendered by Nuke once into a 2K JPEG proxy
  sequence in the temp folder, keyed by the Read's path and file date, instead of one temporary
  PNG per frame change. **Cache** renders the missing proxies in one pass and decodes the whole
  range; **Clear** forgets them (use it after changing nodes between the Read and AutoLens).
  Proxy folders unused for 7 days are deleted.
- Zoom and pan are kept while stepping or playing; zooming in on a JPEG/PNG plate loads the
  settled frame at full resolution.
- Fix: the workspace's keyboard shortcuts used `QtGui.QShortcut`, which only exists in Qt 6;
  PySide2 (Nuke 14/15) has it in `QtWidgets`.

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
