# Changelog

## Unreleased

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
