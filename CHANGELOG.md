# Changelog

## Unreleased

- **Faster solves.** Shots over 60 frames are matched sequentially (each frame against its
  next 20 frames plus frames 32, 64, 128… ahead) instead of exhaustively. That is about 6x fewer
  image pairs at 250 frames. If sequential matching registers under 90% of the frames, AutoLens
  adds the full exhaustive matches and solves again, so shots the old matcher could solve still
  solve. Frames whose names don't sort in frame order (unpadded numbers) stay exhaustive.
- The incremental mapper runs global bundle adjustment half as often during the solve
  (`ba_global_*_ratio` 1.2). The final global adjustment is unchanged.
- Dense reconstruction only undistorts the views it uses (typically 8–18) instead of every
  registered frame, and falls back to all frames if COLMAP needs them.
- Faster engine-side Python: sparse-model parsing, dense view selection (same picks),
  proxy-mesh reading, triangle adjacency and mesh-fit plane growing.
- **Timing summary.** The log ends with a per-stage wall-clock table, and
  `autolens_result.json` stores it under `timings`.
- Optional speed settings are passed to COLMAP only if `colmap <command> -h` lists them.
  `AUTOLENS_SPEED_PROFILE=legacy` (or `--speed-profile legacy`) restores the previous pipeline
  for A/B comparisons.
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
