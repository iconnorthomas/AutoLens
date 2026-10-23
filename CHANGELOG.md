# Changelog

## Unreleased

## 0.11.0 - 2026-10-23

AutoLens 0.11.0 reworks the workspace around how artists work: **Track › Draw › Output**. Plates
play straight from disk like Mocha, the camera arrives before the geometry, and a long list of
Nuke 14–17 fixes makes it start, open and run faster. AutoLens is now open source under Apache 2.0.

### Workspace: Track › Draw › Output
- **New Match-style layout** (see `docs/UX_REVIEW.md`).
  - A dark, flat theme and painted icons. The top bar has File / Track / Draw / Output / View
    menus, the plate breadcrumb and step pills.
  - The side panel shows only the current step.
    - **Track:** the Track button, a quality popover, inline progress and the tracking mask.
    - **Draw:** surface tools and the surface list.
    - **Output:** Create All, Create one, geometry status and Build geometry.
  - A chip over the viewer reads *NOT SOLVED*, *TRACKING n%*, then *SOLVED · 0.29 px · 9m 06s*.
  - The transport has Track and Stop in the centre, and the timeline has *Guides*, *Mask* and
    *Cache* lanes.
  - The workspace moves to Draw (or Output) when a track finishes.
  - Shortcuts: T track, P add plane, G guide frame, Del delete, Ctrl+E Create All, 1–3 steps,
    Space play/pause, ←/→ or ,/. step (Shift = 10 frames), Home/End.
  - Plate, output, version, run mode, GPU and dense settings are in **File › Solve settings…**;
    the log is in **View › Log**. Every earlier control is still reachable.
- **Plates are read straight from disk, like Mocha: open, press Play, and it plays.** Nothing is
  rendered or re-encoded and no image files are written.
  - JPEG/PNG/TIFF frames are decoded by Qt on background threads. EXR and DPX frames are decoded
    by a pool of frame-server processes in AutoLens's managed Python (numpy + OpenEXR), which read
    only R/G/B, scale to the viewer and send 8-bit sRGB back over a pipe.
  - Colour follows the Read's colorspace knob (linear, sRGB, Rec.709, gamma, Cineon) through
    Nuke's default sRGB view; other OCIO spaces are approximated and say so. If nodes sit between
    the Read and AutoLens, a note says the viewer shows the Read's source.
  - Decoded frames stay in a memory-bounded cache (1 GB by default; `AUTOLENS_PREVIEW_CACHE_MB`),
    neighbouring frames are prefetched, and a timeline lane shows what's in memory.
  - Scrubbing shows the nearest cached frame at once; Nuke's frame, the surfaces and the mask
    follow when the drag settles. Playback runs in real time with Loop and an fps box.
  - Zoom and pan are kept while stepping or playing; zooming in loads full resolution.
- **The tracking-mask overlay follows animated masks and plays with the shot.** The mask is
  sampled at each frame (it used to show whatever frame Nuke's viewer had last evaluated).
  Overlays fill in idle time outward from the playhead, a frame that isn't ready shows the
  nearest ready one, and the cache clears only when the mask's graph or keys change.
- **Track time.** The SOLVED chip and status bar show how long the track took, including the
  Nuke-side prep. Output › Timing breaks it down by stage; it's saved in `autolens_ui_timing.json`.
- **Simpler controls.** One Track action with Quality presets that describe themselves, readable
  progress (*Step n of 11*, elapsed, time left), plain-language errors with a *What to do* line,
  no error box on cancel, and *Solve as New Version* when a version already holds a solve.
  Tooltips on every control.

### Tracking and speed
- **Camera first, geometry later.** Switch off *Build geometry during the track* to get the camera
  and lens in about 2 minutes instead of about 9 on an 87-frame shot (`--skip-dense`). **Build
  geometry** on Output runs only the geometry stages from the cached solve (`--geometry-only`) and
  reports progress, success, failure or interruption through `autolens_geometry_status.json`.
- **Faster matching.** Shots over 60 frames are matched sequentially (each frame against its next
  20 plus frames 32, 64, 128… ahead): about 6x fewer pairs at 250 frames. If that registers under
  90% of the frames, the exhaustive matches are added and the shot is solved again.
- The mapper runs global bundle adjustment half as often during the solve (the final one is
  unchanged), dense reconstruction only undistorts the views it uses, and the engine's Python
  hot loops (model parsing, view selection, mesh reading, plane growing) are faster.
- **Hard shots: sparse-solve recovery ladder.** When the mapper fails or registers under 80% of
  the frames, the engine retries in stages before the global fallback: relaxed initial-pair
  limits, low-parallax limits (nodal and push-in shots), merging partial models, denser covariant
  SIFT with guided matching, and the same on contrast-normalised (CLAHE) tracking copies of the
  plate. A retry is kept only if it registers more frames, with mean error under 2 px. Flags:
  `--sparse-recovery off|auto|max` and `--min-registered-fraction`.
- **Registration coverage and timing.** The log and `autolens_result.json` record unregistered
  frames and ranges (`registration_coverage`), each recovery rung (`sparse_recovery`) and a
  per-stage timing table (`timings`).
- COLMAP options are passed only if the installed COLMAP lists them. `AUTOLENS_SPEED_PROFILE=legacy`
  restores the previous pipeline for A/B comparisons.

### Node, startup and Nuke integration
- **Smaller node panel.** New nodes show Open AutoLens..., the status and Create All; everything
  else is on the AutoLens Advanced tab. Existing nodes keep their knobs and get the new labels.
- **Create All** replaces *Import Result* and works the same way.
- **Faster Nuke launch and script open.** `menu.py` no longer imports the controller module at
  launch, and opening a script skips nodes this version already upgraded and no longer scans every
  node's output folder (`AUTOLENS_FULL_REFRESH_ON_LOAD=1` restores the old behaviour).
- **The workspace window opens immediately**, and the plate, surfaces, masks and timeline load
  right after (`AUTOLENS_DEFER_PREVIEW=0` to load them first).
- **Startup profiling:** launch Nuke with `AUTOLENS_PROFILE=1`, then run
  `import autolens.startup; autolens.startup.report()` in the Script Editor.

### Fixes
- **Nuke 14:** the workspace couldn't open (`QtGui.QShortcut` only exists in PySide6), and
  AutoLens failed to load on Python 3.9 (`Path | None` annotation in `autolens/paths.py`).
- **SyntaxError on every AutoLens knob change.** The knobChanged callback was stored with a literal
  backslash-n, so version-menu sync never ran. Nodes saved with it are repaired when the script
  opens. Node labels, Surface Guides labels and some messages had the same problem.
- **Nuke slowed down after adding an AutoLens node** once that callback worked: AutoLens' own knob
  updates retriggered it on every UI tick. It now ignores its own updates and writes only changed
  knobs.
- **Create from solve** failed for Proxy Mesh, Mesh-fit Planes and Guided Surfaces
  (`name 'json' is not defined`), and the **mesh-fit planes OBJ** loaded as nothing.
- **Import Result could leave Redistort unwired** (a node-name prefix match picked the input Dot).
- **Installer:** it could delete the plugin it was installing when unzipped into `~/.nuke`; an
  interrupted upgrade lost the managed runtimes; non-ASCII tool output could fail a step; step
  timeouts never fired.
- **Stale frames in the square-pixel solve proxy** when a different or shorter plate reused it.
- Degenerate Roto shapes are skipped instead of aborting the guide export, and the guide JSON is
  written atomically.

### Project
- **AutoLens is now licensed under Apache 2.0** (`LICENSE`, `NOTICE`); both ship in the release zip.
- **Version guard and release workflow** (`scripts/release.py`, README *Version numbers and
  releases*). Pull requests run the tests on Python 3.9–3.11 and a *Version check*; a `vX.Y.Z` tag
  on `main` builds the release zip from that commit and posts the GitHub Release with these notes.
  The engine's own version strings now follow `__version__` too.
- **The release zip is no longer committed**; download it from the GitHub Release.
- **Build label for test installs:** installing from a git checkout shows the branch or PR and
  commit next to the version, e.g. `AutoLens 0.11.0 (PR #8 · d9bf93a)`.
- **Code audit** (`docs/AUDIT.md`): ranked findings and a roadmap of follow-up PRs.
- `RELEASE_NOTES.md` is now this CHANGELOG; the `GITHUB_PUBLISHING.md` checklist is gone.

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
