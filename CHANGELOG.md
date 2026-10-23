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
- **The plate loads like Match: no progress window, nothing blocks.** Opening the workspace
  renders nothing. Plain JPEG/PNG/TIFF Reads are shown straight from their files. Anything else
  (EXR, DPX, movies, nodes between the Read and AutoLens) is rendered by Nuke in idle time, a few
  frames every 15 ms within a 60 ms budget, nearest the playhead first; the timeline's cache line
  grows and a quiet "Caching plate n / total" hint shows progress. A frame that isn't cached yet
  shows the nearest one until it's ready. Idle caching pauses while a solve runs or Nuke shows a
  modal window, replacing the per-frame temporary PNG render.
- **The plate cache persists** in `%LOCALAPPDATA%\AutoLens\cache` (`~/.cache/autolens` on
  Linux, `~/Library/Caches/AutoLens` on macOS; set `AUTOLENS_CACHE_DIR` to move it), is never
  deleted automatically, and is keyed by the upstream graph: a frame re-renders only when a
  node's settings, the Root colour settings or that frame's source file change, so reopening a
  shot renders nothing. **Clear** re-caches the shot by hand.
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
