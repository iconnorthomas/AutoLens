# AutoLens UX review

Audit of the path from a plate to a solved camera, lens and surfaces in AutoLens
v0.10.30, the pain points found, a proposed simpler flow, and what this branch
changes. Sources: `docs/WORKFLOW.md`, `docs/TROUBLESHOOTING.md`, `README.md`, the
controller knobs in `autolens/node.py`, the Solve panel in `autolens/panel.py`,
and the Surfaces / Masks / Solve sidebar in `autolens/dialog.py`.

## 1. Current flow (v0.10.30)

### Steps for a first solve

| # | Where | Action |
|---|-------|--------|
| 1 | Node Graph | Select the plate Read. |
| 2 | Nuke menu | Create an AutoLens node. Its properties open. |
| 3 | Properties | Scroll past Quality, Run mode, Use surface guides, Import result, Output root and Solve version to **Setup / Run...** and click it. |
| 4 | Workspace | The workspace opens on the **Surfaces** tab. Optional: Add Plane / Auto Suggest, drag corners, Rename, Set Guide Frame, per surface. |
| 5 | Workspace | Optional: **Masks** tab. Connect a matte to input 1, check the red overlay, maybe Invert. |
| 6 | Workspace | Click the **Solve** tab. |
| 7 | Solve tab | Check sections 1, 3, Advanced, 4 and 5 (see the settings list below). |
| 8 | Solve tab | Click **Run AutoLens**. |
| 9 | Solve tab | Wait. The label shows only the stage name, e.g. "Computing dense depth maps", and the bar jumps between 11 coarse stages. No elapsed time or ETA. |
| 10 | Solve tab | When the solve finishes, the result is imported automatically if *Import AutoLens nodes...* is ticked. Otherwise click **Import Result**. |

A minimal solve (no surfaces, no mask, defaults kept) takes **5 clicks**: select
Read, create node, Setup / Run..., Solve tab, Run AutoLens. On the way the artist
sees **7 editable settings** on the Solve tab, **10 more** under Advanced, and an
always-open Installation Status box with 2 read-only paths and 2 buttons.

### Every setting an artist can touch

**Controller node, AutoLens tab** (in order): Status (read-only), Quality,
Run mode, Use surface guides, Import result when complete, Output root +
Browse..., Solve version, result indicator, **Setup / Run...**, Import Result,
Create from solve + Create.

**Controller node, AutoLens Advanced tab**: External Python, COLMAP executable,
help text. Hidden: Resolved output, Last result, Invert tracking mask, Surface
guide node, Guide source, Tracking mask node.

**Workspace, Surfaces tab**: surface list, name field + Rename, Auto Suggest,
Add Plane, Delete, Set Guide Frame. Viewer: drag corners, F to fit, wheel zoom,
middle-drag pan, timeline scrub.

**Workspace, Masks tab**: Invert (alpha means Avoid vs Keep).

**Workspace, Solve tab**:

- *1. Input*: Image folder + Browse…, Use Selected Read, Output root + Browse…, Solve version.
- *3. Reconstruction*: Quality, Run mode, GPU accelerate feature extraction / matching, Import AutoLens nodes into current comp when complete.
- *Advanced* (collapsed): Dense selection, Dense max dimension, Max smart references, Minimum references, Sparse coverage target, Source views / reference, Nuke horizontal aperture, Auto upright world, Dense GPU indices, PatchMatch cache GB, Fusion cache GB.
- *4. Installation Status*: Python runtime (read-only), Managed COLMAP (read-only), Refresh Installation Status, Open Install Report.
- *5. Run*: Completed Result Available (read-only), stage label, progress bar, Run AutoLens, Background, Cancel.
- *Log*: log view, Import Result, Reveal Output.

The section numbers skip 2 because the dialog hides the old *2. Surface Guides*
box (surfaces moved to the viewer).

**Engine-only options** that the UI never shows: `--write-stmaps` (legacy STMap
export) and the cache/debug flags. STMaps are already hidden, so nothing needed
doing there.

### Already automatic

- Output root defaults to `<plate folder>/AutoLens`, with `v1`, `v2`, … version folders.
- GPU feature extraction is on and the dense GPU indices default to `auto`.
- Non-square-pixel plates get a square-pixel solve proxy without the artist doing anything.
- The guide frame is clamped to the Read's frame range.

## 2. Pain points

1. **The main action is buried.** On the node, *Setup / Run...* comes after six
   settings. In the Solve tab, *Run AutoLens* comes after the Installation box.
2. **Too many visible decisions.** Run mode and GPU acceleration are expert
   settings that sit next to Quality, and the default (Resume, GPU on) is right
   almost every time.
3. **Quality presets are opaque.** The summary lists numbers ("12 dense
   references • 3200 px") but doesn't say which preset to pick or what it costs.
   Editing an Advanced value silently overrides the preset.
4. **There's no sense of progress.** The engine reports 11 stages of very
   different length. The panel shows the stage name and a percentage, but no step
   count, elapsed time or estimate. Long dense stages look frozen.
5. **Errors don't say what to do.** Any failure shows "AutoLens did not complete
   successfully. Check the log for the last COLMAP message." The artist has to read
   raw COLMAP output to tell *install broken* from *plate can't be solved* from
   *GPU out of memory*.
6. **Re-solving into a new version is a detour.** When the selected version
   already has a result, the only choices are *Reuse Selected Version* or
   *Cancel*. To keep the old solve, the artist must cancel, open the Solve version
   dropdown, pick *Create New*, and click Run again.
7. **"Import Result" vs "Create from solve" is unclear.** Both create nodes, and
   the names don't say that Import Result creates everything (camera, lens and
   all geometry).
8. **There's no "where am I" indicator.** Nothing says whether the next step is
   setup, solve, review or create.
9. **Installation Status is always open**, even when everything is fine.
10. **Few tooltips.** Most knobs and Solve-tab fields have none.
11. **Bug: Create from solve fails for geometry.** `node.py` calls `json.loads`
    without importing `json`, so creating *Proxy Mesh*, *Mesh-fit Planes* or
    *Guided Surfaces (Combined)* fails with `name 'json' is not defined`. Guided
    surfaces also lose their friendly names.

## 3. Proposed simplified flow

```text
Setup  ›  Solve  ›  Review  ›  Create
```

1. **Setup**: select the Read and create AutoLens. Output, GPU and version are
   chosen automatically. The node and panel say what's missing (no plate, install
   not ready) and how to fix it.
2. **Solve**: one Quality choice (Preview / Normal / High, each with a
   plain-language description) and one primary **Solve** button. Everything else
   lives under Advanced, which stays collapsed.
3. **Review**: progress reads *Step 8 of 11: Computing dense depth maps · 12m 03s
   elapsed · about 6 min left*. If the solve fails, the panel says in plain
   language what went wrong and what to do next.
4. **Create**: one **Create All** button builds the camera, lens and geometry.
   *Create from solve* stays for picking single assets.

Surfaces and the tracking mask stay optional side-steps before Solve.

## 4. What this branch implements

| Pain point | Change | Where |
|---|---|---|
| 1 | The node's primary buttons, **Open AutoLens...** and **Create All**, are now at the top of the AutoLens tab. The Solve tab puts Quality, **Solve** and **Create All** in one *2. Solve* box, right after the shot. | `node.py` `_add_knobs`, `panel.py` `_build_ui` |
| 2 | Run mode and GPU acceleration move into the collapsed Advanced section. On new nodes, Run mode, Use surface guides and Create All on completion move to the AutoLens Advanced tab. | `panel.py`, `node.py` |
| 3 | Each preset gets a one-line description (Fastest… / Recommended… / Slowest…). The summary warns when Advanced values override the preset. | `ux.preset_summary`, `ux.settings_match_preset` |
| 4 | Progress shows step *n of 11*, elapsed time, and a rough ETA once 10% is done and 20 s have passed. It refreshes every second. | `ux.ProgressEstimator`, `runner.py`, `panel.py` |
| 5 | Failures are translated from the engine's `AUTOLENS FAILED:` line and the log tail into a title, a reason and a *What to do* line: install broken, COLMAP missing, Windows DLL error, out of memory, disk full, folder not writable, plate missing, fewer than 3 frames, mask export failed, plate changed since cache, camera unsolvable, a generic COLMAP step failure, and cancel. Cancelling no longer shows an error box. | `ux.explain_failure`, `panel._process_finished` |
| 6 | When the version already has a result, the dialog offers **Solve as New Version (vN)**, **Re-run vX** or Cancel. Re-run stays the default, as before. | `panel._ask_existing_result_action` |
| 7 | *Import Result* is renamed **Create All** on the node and the panel, and the tooltip says what it creates. The knob name `autolens_import_result` is unchanged. | `node._KNOB_LABELS`, `panel.py` |
| 8 | A *1. Setup › 2. Solve › 3. Review › 4. Create* indicator with a next-step hint sits at the top of the Solve tab. The node's result indicator adds "Next: …". | `ux.current_step`, `panel._update_step_indicator`, `node.refresh_result_status` |
| 9 | When the install is ready, Installation shows one green line (paths in its tooltip). The paths and repair instructions appear only when something is wrong. | `panel._refresh_install_status` |
| 10 | Tooltips on every visible node knob, Solve-tab control and Surfaces / Masks control. Old controllers get the tooltips and new labels when they're upgraded. | `node._KNOB_TOOLTIPS`, `panel.py`, `dialog.py` |
| 11 | `import json` added to `node.py`. | `node.py` |

### Step counts, before and after

| Task | Before | After |
|---|---|---|
| First solve with defaults (clicks) | 5 | 5 (Solve is now the first thing in its box, and the node button is at the top) |
| Settings shown on the Solve tab before Advanced | 7 editable + Installation box (2 paths, 2 buttons) | 5 editable (image folder and output are prefilled) + 1 status line |
| Settings on the node's main tab | 6 before the Run button | 0 before the Open / Create All buttons |
| Re-solve into a new version, keeping the old one | 5 actions (Run, Cancel, open version menu, pick Create New, Run) | 2 (Solve, *Solve as New Version*) |
| Find out why a solve failed | Read raw COLMAP log | Read the message box and status line |
| Create everything after a manual review | Import Result (unclear name) | Create All |

### Kept as before

- Every option is still reachable. Run mode, GPU and the dense settings sit under
  Advanced, and *Create from solve* still creates single assets.
- Knob names and types are unchanged, so saved scripts load and
  `upgrade_existing_nodes()` still finds controllers by `autolens_output` +
  `autolens_open`. Old controllers keep their knob layout. They get the new
  tooltips and labels, and each is written only when it differs, so opening an
  old script doesn't mark it modified.
- Preset values are unchanged. A test pins them.
- No engine changes.

## 5. Not done, suggested follow-ups

- **Open the workspace on the Solve tab when there are no surfaces yet.** That
  would save one click for camera-only solves. Dialog construction and tab order
  belong to the `ui-startup` work, so it was left alone here.
- **Filmback from metadata.** *Nuke horizontal aperture* defaults to 36 mm. Read
  metadata keys for sensor width differ between cameras (ARRI, RED, Sony) and
  guessing wrong would quietly change the exported focal length. This needs real
  sample plates first.
- **Old controllers keep their old knob order.** Nuke can't reorder existing knobs
  without rebuilding them, which is riskier than it's worth.
- **The Create from solve dropdown can jump tabs.** On a version change it is
  removed and re-added (`_refresh_create_item_knob(force_rebuild=True)`), which
  appends it at the end of the knob list, on the AutoLens Advanced tab. This looks
  pre-existing but needs checking in NukeX.
- **Better ETA.** Weighting stages by their typical share of run time (dense depth
  maps dominate) would beat the current linear estimate. That needs timing data
  from real solves.

## 6. Second pass: Match-style workspace (Track › Draw › Output)

The artist's real order is: open AutoLens, track, draw shapes, output. The
first pass kept the old Surfaces / Masks / Solve tabs, so the workspace still
opened on drawing, buried the Track button in a long settings tab, and showed
about 20 controls at once. This pass rebuilds the workspace in the style of
the Match tool. One theme (`autolens/theme.py`) and one painted icon set
(`autolens/icons.py`) are shared by every button, and nothing ships as image files.

### New layout

| Area | What's there |
|---|---|
| Top bar | AUTOLENS wordmark · File / Track / Draw / Output / View menus · `Plate: Read1 · 1920×1080 · 1001–1087 · v2` · **Track › Draw › Output** pills (current one cyan, done ones green, a failed track red). |
| Viewer | The plate, a status chip (*NOT SOLVED*, *TRACKING 42%*, *SOLVED · 0.29 px*, *FAILED*) and the frame, plus Fit. |
| Side panel | Only the current step. **Track:** Track button, quality popover, inline progress and Stop, tracking-mask status and Invert. **Draw:** four icon tools (add plane, auto suggest, guide frame, delete), the surface list and a name field. **Output:** Create All, Create one ▾, open folder, geometry status and Build geometry. *Solve settings…* sits at the bottom. |
| Transport | first / prev / frame field / next / last, play back / play, Loop, fps. In the centre, **Track** and **Stop**. On the right, the cache count. |
| Timeline | Lanes with labels: a frame ruler, *Guides* (one blue mark per surface guide frame) and *Cache* (frames in memory or on disk). |
| Status bar | The next-step hint on the left, the last message (frame info, progress, errors) on the right. |

The node's main tab now holds only the title, status, **Open AutoLens...**,
**Create All** and the result line. Quality, output root, solve version and
Create from solve moved to the AutoLens Advanced tab on new nodes.

### What moved where (nothing removed)

| Was | Now |
|---|---|
| Surfaces / Masks / Solve tabs | Track / Draw / Output pages, switched by the pills or keys 1–3 |
| Solve tab: plate, output, version, Advanced, install box | **File › Solve settings…** (the same panel, in its own window) |
| Quality combo, *Create when finished* | The quality popover next to Track (also in Solve settings) |
| Background button | **Track › Continue in background**. Closing the window while tracking still offers it. |
| Log box | **View › Log** |
| Rename button | Type in the name field and press Enter |
| Reveal Output | The folder button on Output, or **File › Open output folder** |
| Clear (frame cache) | **View › Clear frame cache** |
| Create from solve + Create (node only) | **Create one ▾** on Output and **Output › Create one** (still on the node's Advanced tab) |
| Install status / report | **File › Installation status…** / **Open install report** |
| Use surface guides (checkbox) | **Draw › Use surfaces in the track** (also in Solve settings) |
| — | New: *Build geometry during the track* (off = camera and lens only, `--skip-dense`) and **Build geometry** on Output (`--geometry-only`: dense stages only, from the cached solve). Output polls the engine's `autolens_geometry_status.json` for *building* (step n of 11), *ready* and *failed*. A file stuck at *building* with no live process is shown as an interrupted build. |

### Step counts, before (first pass) and after

| Task | First pass | This pass |
|---|---|---|
| Camera + lens + geometry, no surfaces (clicks after selecting the Read) | 4 (create node, Open AutoLens..., Solve tab, Solve) | 3 (create node, Open AutoLens..., Track) |
| Draw surfaces after a track, then create | 6 (Surfaces tab, add, drag, set guide frame, Solve tab, Create All) | 5 (the workspace moves to Draw itself: add, drag, set guide frame, Output, Create All) |
| Controls on the side panel at once | ≈ 20 on the Solve tab | 3 on Track (Track, quality, Invert), 6 on Draw, 4 on Output |
| Controls on the node's main tab | 8 | 2 (Open AutoLens..., Create All) |
| Geometry for a camera-only track | Re-run the whole track with different settings | One click: Build geometry |

### Mask overlay that plays with the shot

Before, an overlay existed only for frames that had settled, so playback
blinked the red mask on and off. Now (`autolens/mask_cache.py`):

- **Filled in idle time.** While the Track step is showing, a 15 ms timer samples a few rows of the next missing frame within a 10 ms budget per tick. It works outward from the playhead (+1, −1, +2, −2…) and pauses during playback.
- **No blinking.** A frame that isn't ready yet shows the nearest ready overlay. The timeline's new red *Mask* lane shows which frames are ready.
- **Nuke's frame never moves.** Each pixel is one `sample(channel, x, y, 1, 1, frame)` call with the frame passed explicitly, on a 160 × 90 grid (14,400 samples per frame). Rows go into a premultiplied byte buffer, not `setPixelColor`. Builds that can't take a frame argument fall back to filling only the settled frame.
- **Why sampling rather than a render.** At an estimated 10–20 µs per sample, a frame costs roughly 0.15–0.3 s of idle time, spread over ticks, so a 100-frame shot fills in under half a minute without blocking. A Write render needs a temporary node and a disk round trip for each frame. The measured cost per frame is written to **View › Log** after 10 frames and when the cache is full.
- **Invalidation.** A signature of the mask's upstream graph (adapted from Match's NukeSource) covers node classes, wiring and non-default knob values including animation keys, plus Invert and the mask size. It is re-checked when the Track step opens and whenever the window becomes active again, and the cache clears only if the signature changed. Renaming or moving nodes doesn't clear it; re-keying a Roto does. **Track › Refresh mask preview** forces a refill.
- The cache is kept in memory only, about 56 KB per frame.

### Track time

The engine already saves per-stage timings in `autolens_result.json`. The
workspace now also times the Nuke-side prep before the engine starts (plate
proxy, mask and guide export) and the engine's wall time. It saves both in
`autolens_ui_timing.json` beside the result, under `track` and `geometry`, so
the total matches what the artist waited.

- **Chip:** *SOLVED · 0.30 px · 9m 06s*; its tooltip has the full breakdown.
- **Status bar** when a run finishes: *Track complete in 9m 06s (Nuke prep 12s).*
- **Output › Timing:** stages longest first, plus the last geometry build time.
- **View › Log:** the same breakdown, written when the run finishes.

Solves made before this build, such as the user's v1 (574 s) and v2 (546 s), show the engine's own total.

### Testing

The step logic, chip, hints, geometry status, tick spacing and theme are pure
Python in `ux.py` and `theme.py`, covered by `tests/test_workspace.py`. The Qt
layout itself can only be checked in NukeX (PySide2 in Nuke 14, PySide6 in
Nuke 16+).
