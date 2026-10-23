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
- **Hard shots: sparse-solve recovery ladder.** When the incremental mapper fails or registers
  under 80% of the frames, the engine now retries in stages before the global-SfM fallback:
  relaxed initial-pair and registration limits, low-parallax limits (nodal and push-in shots),
  merging overlapping partial models, denser covariant SIFT with guided matching, and the same
  on contrast-normalised (CLAHE) tracking copies of the plate. A retry is used only when it
  registers more frames than the plain solve, and relaxed retries must keep a mean reprojection
  error under 2 px. The log says which rung won and why. Shots that already solve well run
  exactly as before. Engine flags: `--sparse-recovery off|auto|max` (`max` adds ALIKED +
  LightGlue features where the COLMAP build supports them) and `--min-registered-fraction`.
  Options the installed COLMAP doesn't list in its `-h` output are skipped instead of failing.
- Tracking copies are used for feature extraction only; the mapper, dense stages and every
  output still read the original plate. They're written for EXR plates; for JPEG, PNG and
  TIFF plates they need Pillow or OpenCV in the engine runtime, and the rung is skipped
  (logged) otherwise.
- **Registration coverage** in the log and in `autolens_result.json`
  (`registration_coverage`: unregistered frames, their frame ranges and the longest
  unregistered run), plus a `sparse_recovery` report of every retry rung.
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
- **Fast scrubbing and real-time playback in the workspace viewer.** Frames are decoded on
  background threads at viewer size into a memory-bounded cache (1 GB by default; set
  `AUTOLENS_PREVIEW_CACHE_MB` to change it), and neighbouring frames are prefetched. Dragging
  the timeline shows the nearest cached frame at once; Nuke's frame, the surfaces and the mask
  overlay follow when the drag settles. New transport bar: first/previous/play backwards/play/
  next/last, Loop and an fps box (defaults to the script's fps). Playback runs in real time and
  waits for frames when decoding falls behind. Keys: Space play/pause, ←/→ or ,/. step
  (Shift = 10 frames), Home/End. A green bar under the timeline shows the frames in memory.
- **Plates are read straight from disk, like Mocha: open, press Play, and it plays.** Nothing is
  rendered or re-encoded and no image files are written. JPEG/PNG/TIFF frames are decoded by Qt
  on background threads; EXR and DPX frames by a pool of frame-server processes in AutoLens's
  managed Python (numpy + OpenEXR), which read only the R/G/B channels, scale to the viewer and
  send 8-bit sRGB back over a pipe. Colour follows the Read's colorspace knob (linear, sRGB,
  Rec.709, gamma, Cineon) through Nuke's default sRGB view; other OCIO spaces are approximated
  and say so. Decoded frames stay in a memory-bounded cache; when decoding falls behind, playback
  waits for the next frame rather than skipping. If nodes sit between the Read and AutoLens, a
  note says the viewer shows the Read's source. Without the managed Python, EXR/DPX plates show
  a clear message instead of an image.
- Zoom and pan are kept while stepping or playing; zooming in loads the settled frame at full
  resolution.
- Fix: the workspace's keyboard shortcuts used `QtGui.QShortcut`, which only exists in Qt 6;
  PySide2 (Nuke 14/15) has it in `QtWidgets`.

- **Build label for test installs.** Installing from a git checkout records the branch, commit,
  pull request number and uncommitted-changes flag in `autolens_build.json`, and the workspace
  shows it next to the version, e.g. `AutoLens 0.10.30 (PR #8 · d9bf93a)`. Release installs show
  only the version.
- **Fix: SyntaxError on every AutoLens knob change.** The node's knobChanged callback was stored
  with a literal backslash-n instead of line breaks, so Nuke printed "unexpected character after
  line continuation character" and version-menu sync never ran. Nodes saved with the broken
  callback are repaired when the script opens. Node labels, the Surface Guides labels, a few
  messages and the mask-change log line had the same problem.
- **Fix: mesh-fit planes OBJ loaded as nothing.** It was written with literal backslash-n.
- **AutoLens is now licensed under Apache 2.0.** `LICENSE` and `NOTICE` replace
  `LICENSE_NOT_SPECIFIED.md`, and both ship in the release zip.
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
- **Code audit** (`docs/AUDIT.md`): ranked findings across the plugin, engine and installer, with
  a roadmap of follow-up PRs. Small fixes from it:
  - **Fix: Import Result could leave the Redistort node unwired.** The importer matched node
    names by prefix, so `AUTOLENS_REDISTORT` could resolve to the `AUTOLENS_REDISTORT_INPUT` Dot,
    which is then deleted.
  - **Fix: the installer could delete the plugin it was installing** when the package was
    unzipped into `~/.nuke`, or inside `~/.nuke/AutoLens`. It now installs in place or stops
    with a clear message.
  - **Fix: an interrupted upgrade no longer loses the managed runtimes.** The installer used to
    delete `.autolens_upgrade_preserve` (the parked `.venv`, Miniforge and COLMAP environments)
    at the start of the next run. It now moves them back, including when the copy fails part-way.
  - **Fix: installer steps could fail on non-ASCII tool output, and timeouts never fired.** pip
    and conda output is decoded as UTF-8 with replacement, and a watchdog enforces the step
    timeout while a child process holds its output open.
  - **Fix: stale frames in the square-pixel solve proxy.** The proxy cache folder is keyed only
    by size and pixel aspect, so a different or shorter plate re-rendered into it kept the old
    extra frames, and the solve used them.
  - Degenerate Roto shapes (no curve, or a line) are skipped instead of aborting the guide export
    or exporting a zero-area guide. The surface-guide JSON is written atomically.

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
