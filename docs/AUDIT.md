# AutoLens code audit

October 2026, against `chore/repo-rules` (0.10.30 plus CI). This is a read-through of every
Python module, the installer, the docs and the engine. Nothing here was run in Nuke or against
COLMAP: this machine has no Python, Nuke or GPU. Findings marked **(verify)** follow from the
code but depend on Nuke or Qt behaviour that should be checked in a real session before fixing.

Each finding gives the location, a concrete failure, a suggested fix, and the area it belongs to:

| Area tag | Covers |
|---|---|
| `perf-solve-speed` | engine matching, mapping, dense |
| `perf-sparse-tracking` | feature extraction, retries, tracking masks |
| `ui-playback` | workspace viewer and timeline |
| `ui-startup` | menu, startup, dialog construction and lifetime |
| `ux-simplify` | node knobs, properties panel, sidebar |
| `unassigned` | everything else |

Items marked **Fixed on this branch** have a commit and a unit test here. The rest are open.

Contents: [Critical](#critical) · [High](#high) · [Medium](#medium) · [Low](#low) ·
[Docs](#stale-or-incorrect-docs) · [License](#license) · [Roadmap](#roadmap-of-follow-up-prs) ·
[Checked and fine](#checked-and-fine)

---

## Critical

### C1. The workspace can't open on Nuke 14 (PySide2): `QtGui.QShortcut` · `ui-startup`
- **Where:** `AutoLens/python/autolens/dialog.py:1349` (`_install_shortcuts`).
- **Failure:** in Qt5/PySide2, `QShortcut` lives in `QtWidgets`; `QtGui.QShortcut` exists only in
  Qt6. On Nuke 14, `AutoLensDialog.__init__` raises AttributeError, and `show_autolens_dialog`
  shows "AutoLens could not open the workspace". Nuke 15+ (PySide6) is unaffected, which is
  probably why it went unnoticed.
- **Fix:** `QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut`.
  `QAction` has the same split, so check any future use of it too.
- **Status:** fixed (uncommitted at the time of writing) on `claude/startup-time-1df6d0`.

### C2. Mesh-fit planes OBJ is written with literal `\n` and loads as nothing · `unassigned`
- **Where:** `AutoLens/engine/autolens_engine.py:2756-2758` (`write_meshfit_planes_obj`).
- **Failure:** `f'\\ng {name}\\n'`, `'v ...\\n'` and `'...\\n'` write a backslash followed by `n`.
  Everything after the header comment ends up on one line, so `autolens_meshfit_planes.obj`
  holds no `v`, `g` or `f` records. `AUTOLENS_MESHFIT_PLANES` (ReadGeo2) is empty on every run,
  and the importer's "Guided > Mesh-fit > Proxy" fallback then picks an empty mesh whenever
  there are no guides.
- **Fix:** use `'\n'` in all three writes. Add a test that writes two surfaces and re-reads them
  with `read_obj_mesh` (Nuke-free; `tests/test_engine_math.py` already loads the engine).

### C3. `node.py` uses `json` without importing it · `ux-simplify`
- **Where:** `AutoLens/python/autolens/node.py:1-12` (imports); used at `1126`, `1202`, `1531`.
- **Failure:** Create → "Proxy Mesh", "Mesh-fit Planes" and "Guided Surfaces (Combined)" always
  fail with `NameError: name 'json' is not defined`. At 1126 and 1202 the error is swallowed, so
  friendly guide names and OBJs listed only in the report are silently never found.
- **Fix:** `import json`. A smoke test that imports every `autolens.*` module with a stub `nuke`
  would catch this class of bug (see [Roadmap](#roadmap-of-follow-up-prs) PR 12).

---

## High

### H1. `box.exec()` on PySide2 (verify) · `ux-simplify`
- **Where:** `panel.py:1367`.
- **Failure:** older PySide2 builds expose only `exec_()`. On Nuke 14, Run on a version that
  already has a result raises AttributeError, so Resume and Rebuild Dense can't be chosen.
- **Fix:** `(getattr(box, "exec", None) or box.exec_)()`. The same applies to any
  `QDialog.exec` or `QMenu.exec` call.

### H2. The knobChanged callback is stored with literal `\n` (verify) · `ux-simplify`
- **Where:** `node.py:793-803` (`_install_knob_changed_callback`).
- **Failure:** the script is built from `"import nuke\\n" ...`, so the knob holds a backslash-n
  rather than a line break. Python reports "unexpected character after line continuation
  character" on every knob change, and version-menu sync and showPanel refresh never run. The
  `marker in existing` check means fixed code will never repair nodes that already hold the
  bad text.
- **Fix:** use real newlines. If the existing value contains the marker but not a valid block,
  replace the AutoLens block. To check: `repr(n['knobChanged'].value())`.

### H3. Esc closes the workspace without `closeEvent`, killing a running solve · `ui-startup`
- **Where:** `dialog.py:1080` (no `reject` override), `2378-2411`.
- **Failure:** Esc calls `QDialog.reject()`, which hides and deletes the dialog
  (`WA_DeleteOnClose`) without calling `closeEvent`. The "move solve to background?" prompt is
  skipped. The panel, runner and QProcess are destroyed, so the engine is killed and COLMAP is
  orphaned (see M8). `_DIALOGS` keeps the dead wrapper, so every later Open for that node fails
  with "Internal C++ object already deleted" until Nuke restarts.
- **Fix:** override `reject()` to call `self.close()`, and pop `_DIALOGS` on `destroyed`.

### H4. `_DIALOGS` is keyed by node name and never checked for liveness · `ui-startup`
- **Where:** `dialog.py:2408-2427`.
- **Failure 1:** rename the controller and the old entry stays, so reopening finds a deleted
  dialog.
- **Failure 2:** delete the controller, or use File > New. `fullName()` raises in `closeEvent`,
  so the entry is never removed. A new `AutoLens1` in the next script then gets the stale dialog
  bound to a dead node.
- **Fix:** key by a stable id (for example a hidden UUID knob). On lookup, check validity with
  `shiboken2`/`shiboken6` `isValid` and `existing.controller_node.name()` inside a try. Wrap the
  pop in a try.

### H5. Timeline scrubbing renders full-res frames on the GUI thread, re-entrantly · `ui-playback`
- **Where:** `dialog.py:861-885`, `1429-1450`, `1834`, `1855-1886`.
- **Failure:** every mouse-move emits `frameChanged`, even when the frame hasn't changed. Each
  emit runs `_load_preview`, which creates a Write and runs `nuke.execute` for a full-res PNG,
  then calls `QApplication.processEvents()`. On a 4K EXR plate the UI freezes. Nested events
  re-enter the render, so you get stacked `nuke.execute` calls ("already executing"), extra
  Write nodes, and the frame jumping as nested `finally` blocks restore `old_frame` out of
  order.
- **Fix:**
  - Emit only when the frame changes.
  - Debounce with a single-shot QTimer, or render on release and show a cached or low-res
    frame while dragging.
  - Add a re-entrancy guard and drop `processEvents`.
  - Keep an LRU pixmap cache, and render through a Reformat to viewer size.

### H6. The Masks tab samples about 32k pixels in Python per frame · `ui-playback`
- **Where:** `tracking_masks.py:667-857` (`render_mask_overlay_pixmap`), called from
  `dialog.py:1450`, with `processEvents` at `dialog.py:1555`.
- **Failure:** each frame change makes about 240×135 `node.sample()` calls on the GUI thread.
  Each pixel can be tried with two call signatures, and `nuke.frame()` round-trips fire viewer
  callbacks. Scrubbing in the Masks tab effectively hangs Nuke.
- **Fix:**
  - Render the alpha once through a Write via the existing "preview" mode of
    `_temporary_alpha_expression`, and load it with QImage.
  - Cache by (mask node, frame, invert) and debounce.
  - Remember which `sample()` signature works.

### H7. Editing one surface deletes hidden, degenerate or layered guide shapes · `unassigned`
- **Where:** `surface_guides.py:1064-1113` (`replace_surface_guides`), driven by
  `dialog.py:2009-2019` (`_commit_surfaces`).
- **Failure:** every corner drag, rename, add or delete rebuilds the whole Roto from
  `canvas.surfaces`. `surface_guides_data` leaves out shapes that are invisible at their guide
  frame or under 3 points, so those are permanently deleted. Shapes inside Roto layers are not
  removed (only top-level shapes are), so they are duplicated at root on every edit. Animated
  or tracked shapes are flattened to static polygons. Duplicate names (the default is
  "Surface") share one guide-frame entry.
- **Fix:** update only the edited shape in place. At minimum, remove only shapes whose names the
  canvas returned (plus renamed ones), recurse into layers, and key the frame map by the name
  rotopaint actually assigned (`shape.name` after append).

### H8. The lens fit never samples the frame corners on barrel lenses · `unassigned`
- **Where:** `autolens_engine.py:4960-4986` (`fit_nuke_lens_model`).
- **Failure:** `max_radius_px` is the *distorted* corner radius, but it is used as the upper
  limit of the *undistorted* samples `ru_c`. With k1 < 0, the largest sampled distorted radius
  falls short of the real corner (about 5-10% of radius at k1 = -0.1). The corners are
  extrapolated by the rational curve, and the reported `rms`, `max` and edge errors never measure
  them, so the fit looks sub-pixel while the corners drift.
- **Fix:** sample distorted radius from 0 to the corner radius and get `ru` with
  `radial_inverse` (or invert `max_ru_colmap` first). Check `np.any(denominator <= 0)` instead
  of a sign change between samples. Extend `test_nuke_lens_fit_is_subpixel` to measure the
  error at the corners.

### H9. External COLMAP `.bat` launch breaks on any path with a space · `unassigned`
- **Where:** `autolens_engine.py:225-232` (`prepare_colmap_command`, legacy fallback).
- **Failure:** `command_line = "call " + list2cmdline([...])` is passed as one list element, so
  `subprocess` quotes it again and turns inner quotes into `\"`. `cmd /s /c` doesn't understand
  `\"`, so `call \"C:\Program Files\COLMAP\COLMAP.bat\" ...` fails. Any space in the plate
  folder, user profile or COLMAP path triggers it. The managed conda launcher is not affected.
  `& ^ % |` in paths are also unescaped.
- **Fix:** build the whole command line as a string, for example
  `f'"{comspec}" /d /s /c "call {list2cmdline(argv)}"'`, pass it as a string with
  `shell=False`, and escape cmd metacharacters. Note that `environment.py:94-150` holds a
  separate, unused copy of this logic (see Low › Installer).

### H10. Feathered tracking-mask edges are not masked for COLMAP · `perf-sparse-tracking`
- **Where:** `tracking_masks.py:511` (`_temporary_alpha_expression`), the Write at `543-563`,
  and the scale at `999`.
- **Failure:** COLMAP ignores features only where the mask is exactly 0. The mask is written as
  continuous `1-a` through the Write's default sRGB transform, then smooth-scaled, so feathered
  or motion-blurred actor edges come out non-zero. Features are still extracted in exactly
  the unstable band the mask is meant to remove.
- **Fix:** threshold in the expression (`a>0.001?0:1`, inverted as needed), optionally with a
  small dilate. Write raw/linear, and scale with `Qt.FastTransformation`.

### H11. The dense cache isn't tied to the sparse model it came from · `perf-solve-speed`
- **Where:** `autolens_engine.py:2363-2386`, `2445-2464`.
- **Failure:** the dense signature covers only dense settings and the selected image names. If
  the sparse model is re-solved (sparse deleted, global-SfM fallback, or a mask-change cleanup
  that failed, see M4) and the selector picks the same names, the old undistorted workspace and
  `meshed-proxy.ply` are reused. COLMAP's world frame and scale are arbitrary, so the proxy no
  longer lines up with the new cameras.
- **Fix:** hash `best_model/{cameras,images,points3D}.bin` (or record their path and mtime) into
  the signature.

### H12. Import Result could leave Redistort unwired · `unassigned` · **Fixed on this branch**
- **Where:** `importer.py:162-168` (`_find_pasted`).
- **Failure:** prefix matching meant `AUTOLENS_REDISTORT` could resolve to the
  `AUTOLENS_REDISTORT_INPUT` Dot, which the engine writes first. The importer then deletes that
  Dot and wires "Redistort" (the deleted Dot) to the Merge, so the real Redistort hangs loose.
- **Fix:** match the exact base name plus Nuke's numeric suffix (`_is_named`).
  Test: `tests/test_importer.py`.

### H13. The installer could delete the plugin it was installing · `unassigned` · **Fixed on this branch**
- **Where:** `install_autolens.py:145-172` (`install_plugin`).
- **Failure:** if the release zip was unzipped into `~/.nuke`, then `source == target`, and
  `rmtree(target)` deleted the package before `copytree`. If it was unzipped inside
  `~/.nuke/AutoLens`, the same thing happened from inside.
- **Fix:** install in place when source is target. Refuse with a clear message when source is
  inside target. Test: `tests/test_installer.py`.

### H14. Version switches move the Create controls to the Advanced tab · `ux-simplify`
- **Where:** `node.py:1311-1361` (`force_rebuild`).
- **Failure:** `autolens_create_item` and `autolens_create_selected` are removed and re-added
  with `addKnob`, which appends them after the "AutoLens Advanced" tab. This runs on every
  version change (deferred refresh at `51-54`) and is saved with the script.
- **Fix:** update the enumeration in place (`setValues` plus a panel refresh) instead of
  removing and re-adding the knobs.

---

## Medium

### Installer and runtime
- **M1. An interrupted upgrade deleted the managed runtimes** · `unassigned` · **Fixed on this branch.**
  - **Where:** `install_autolens.py:156-157`.
  - **Failure:** the run parks `.venv`, `.miniforge3` and `.colmap_env` in
    `~/.nuke/.autolens_upgrade_preserve`. If it crashed after that (for example `rmtree` on a
    file Nuke had locked), the next run started with `rmtree(preserve_root)`, deleting several
    GB of environments.
  - **Fix:** the parked items are now moved back first, and also when the copy fails.
- **M2. Installer steps failed on UTF-8 output, and timeouts never fired** · `unassigned` · **Fixed on this branch.**
  - **Where:** `install_autolens.py:92-127`, `130-142`.
  - **Failure:** `text=True` decoded with the locale codec (cp1252). A pip or conda progress
    glyph raised UnicodeDecodeError, returned -1 and abandoned the still-running child.
    `proc.wait(timeout)` ran only after stdout closed, so a hung conda never timed out. `log()`
    could also raise on a cp1252 stdout.
  - **Fix:** decode as UTF-8 with `errors="replace"`, kill on a watchdog timer, and fall back
    when printing.
- **M3. The managed environment lives under the user's home** · `unassigned`.
  - **Where:** `install_autolens.py:24`, `390`, `496`.
  - **Failure:** for a user like `C:\Users\Jane Smith`, the Miniforge prefix contains a space.
    Miniforge and conda warn against this, and some packages' activation scripts break.
    Non-ASCII user names have the same problem.
  - **Fix:** detect a space or non-ASCII character and install the runtimes under a safe root
    (for example `%LOCALAPPDATA%\AutoLens` or `C:\AutoLens`), recorded in
    `autolens_install.json`.
- **M4. The engine's venv uses whatever Python launched the installer** · `unassigned`.
  - **Where:** `install_autolens.py:208-249`.
  - **Failure:** `py -3` picks the newest Python installed. On a version that OpenEXR has no
    wheel for yet, pip tries to build from source and the install fails at step 2.
  - **Fix:** create the engine env from the conda `python=3.11` that step 5 already installs, or
    use `py -3.11`, falling back with a clear message.
- **M5. Miniforge is downloaded from `latest` and run without a checksum** · `unassigned`.
  - **Where:** `install_autolens.py:17-20`, `386-433`.
  - **Failure:** installs aren't reproducible, and the executable isn't verified.
  - **Fix:** pin a Miniforge version and check its published SHA-256 before running it.
- **M6. Installer requires CUDA 12.9 or later, with no CPU path** · `unassigned`.
  - **Where:** `install_autolens.py:297-310`, `724-742`.
  - **Failure:** machines without an NVIDIA GPU, or with a driver older than 12.9, can't install
    at all, although COLMAP can run on CPU.
  - **Fix:** offer a CPU-only conda environment. At minimum, state the driver requirement in the
    README.

### Process lifetime and environment
- **M7. Nothing stops a solve when Nuke quits or the script closes** · `unassigned` (runner) /
  `perf-solve-speed` (engine `run`).
  - **Where:** `runner.py` (whole file), `autolens_engine.py:241-278`.
  - **Failure:** the QProcess destructor terminates only the Python engine. COLMAP (under
    `conda run`, or under PowerShell and cmd) keeps the GPU and `database.db`. On Linux,
    `cancel()` also kills only Python. After File > Close, `_process_finished` →
    `import_last_result` calls `controller.fullName()` on a deleted node (`panel.py:1457`).
  - **Fix:**
    - Put the engine in a Windows Job Object with KILL_ON_JOB_CLOSE (or start a new process
      group and `taskkill /T`). On Linux, use `start_new_session` and `os.killpg`.
    - Hook `QApplication.aboutToQuit` and `nuke.addOnScriptClose` to `runner.cancel()`.
    - Guard deleted nodes.
- **M8. The engine inherits Nuke's Python environment and a cp1252 stdout** · `unassigned`.
  - **Where:** `runner.py:53-55`, `97`; `node.py:1865`.
  - **Failure:**
    - PYTHONPATH and PYTHONHOME from Nuke can pull Nuke's site-packages into the venv
      interpreter.
    - With a piped stdout on Windows, the engine prints in cp1252. A plate path with characters
      outside cp1252 raises UnicodeEncodeError and kills the solve.
    - The runner decodes each chunk as UTF-8 on its own, so a multi-byte character split across
      reads is corrupted.
  - **Fix:**
    - Set `PYTHONUTF8=1` and `PYTHONNOUSERSITE=1`, and remove PYTHONPATH, PYTHONHOME and
      PYTHONSTARTUP.
    - Decode with `codecs.getincrementaldecoder("utf-8")(errors="replace")`.
- **M9. The guides-only refresh blocks Nuke and aborts Import on failure** · `ux-simplify` /
  `unassigned` (engine).
  - **Where:** `node.py:1798-1894`, `panel.py:1466-1490`, `autolens_engine.py:5514`.
  - **Failure:**
    - It runs a blocking `subprocess.run(timeout=300)` on the UI thread, with a console window
      flashing on Windows.
    - It runs on every Import, even when guides haven't changed.
    - A failure aborts the import of an otherwise good track.
    - The engine runs `find_images` on the plate folder before the guides-only branch, so a DPX
      plate fails with "Need at least 3 images". For anamorphic shots the rewritten `.nk` points
      at the plate instead of the proxy.
  - **Fix:**
    - Skip the refresh when the guide JSON hash is unchanged.
    - Run it via QProcess.
    - On failure, import the existing result.
    - Skip `find_images` in guides-only mode.

### Engine cache correctness
- **M10. An interrupted `image_undistorter` is treated as a complete workspace** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:2460-2466`.
  - **Failure:** `prepared` only checks that `images/`, `sparse/` and `stereo/` exist, and COLMAP
    creates them first. After a cancel, PatchMatch runs on partial images.
  - **Fix:** check `dense/sparse/images.bin`, write a completion marker, and treat `dense/` with
    no signature file as invalid.
- **M11. Mask-change cache clearing swallows every error** · `perf-sparse-tracking`.
  - **Where:** `autolens_engine.py:1779-1810`.
  - **Failure:** a `database.db` locked by an orphaned COLMAP (M7) or OneDrive isn't deleted, but
    the cache JSON is. The next run reuses features extracted without the mask while the user
    believes the mask applied. The banner also prints a literal `\n`.
  - **Fix:** collect the failures and stop with a message naming the locked files.
- **M12. The sparse model or database can be reused without a cache file** · `perf-sparse-tracking`.
  - **Where:** `autolens_engine.py:5576-5595`, `5633-5665`, `1838`.
  - **Failure:** the image-signature and mask checks run only `if cache`. With
    `autolens_cache.json` missing or corrupt, a replaced plate reuses the old model, or the old
    `database.db` features.
  - **Fix:** when a model or database exists with no readable cache, rebuild (or require
    `--force`).
- **M13. Any dense-signature change wipes PatchMatch depth maps** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:2363-2386`, `2453-2456`.
  - **Failure:** the signature includes `gpu_index` and cache sizes. `--dense-gpus auto` gives
    "0" when `nvidia-smi` is on PATH and "-1" when it isn't (Nuke's environment versus a
    terminal), and each flip re-runs undistort, PatchMatch and fusion. Changing only the mesh
    face ratio also re-runs PatchMatch.
  - **Fix:** use staged signatures that invalidate only downstream stages, and leave out GPU
    index and cache sizes.
- **M14. `ensure_clean_proxy_mesh` raises KeyError on degenerate meshes** · `unassigned`.
  - **Where:** `autolens_engine.py:1130`, `1158`, `1191`, `1239` (early returns) → `1447`, `1485`.
  - **Failure:** the early returns never set `removed_total_triangles` or `removed_fraction`, so
    the safety-valve check or the summary print raises KeyError after hours of dense work.
  - **Fix:** set both keys on every path, or use `.get(..., 0)`.

### Engine performance
- **M15. `image_undistorter` writes every registered frame** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:2472`.
  - **Failure:** only the 8-18 selected reference views are used, so a 300-frame plate does
    about 20-40× the necessary I/O.
  - **Fix:** undistort a reduced model holding only the selected images.
- **M16. Exhaustive matching up to 250 frames** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:5667`.
  - **Failure:** about 31k pairs at 250 frames. Sequential matching with overlap and loop
    detection costs roughly 10× less for video.
  - **Fix:** lower the threshold (about 80).
- **M17. Mapper partial success is accepted** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:5709-5711`, `5785`.
  - **Failure:** 12 of 300 frames registered passes, because the only check is `reg_count < 3`.
  - **Fix:** also fall back to global SfM, or retry, when the registered fraction is low.
- **M18. PLY face reading and triangle adjacency are pure-Python loops** · `perf-solve-speed`.
  - **Where:** `autolens_engine.py:1002-1019`, `2675-2696`.
  - **Failure:** when `mesh_simplifier` fails, the raw Delaunay mesh (millions of faces) goes
    through them, which takes minutes and a lot of memory.
  - **Fix:** use a structured `np.fromfile` for triangle faces, and vectorised adjacency.

### Geometry and Nuke output
- **M19. Vertical `win_translate` is normalised by height (verify)** · `unassigned`.
  - **Where:** `autolens_engine.py:899-900`.
  - **Failure:** Nuke's window translate uses horizontal-aperture units on both axes. If so,
    `win_y` should be `-2*(cy-h/2)/w`, and today any off-centre `cy` is scaled by w/h (about
    1.78× on 16:9).
  - **Fix:** check against a known off-centre solve before changing it.
- **M20. Paths in the generated `.nk` aren't Tcl-escaped** · `unassigned`.
  - **Where:** `autolens_engine.py:4909` (`escape_nuke_path`), used at `5139-5201`.
  - **Failure:** `Shot_010 [v2]/...` is evaluated as a Tcl command in the `file` knob. A `"` in
    a Linux path breaks parsing.
  - **Fix:** backslash-escape `[ ] $ " { }`.
- **M21. Each guided surface is one n-gon** · `unassigned`.
  - **Where:** `autolens_engine.py:3658-3662`, `3699-3703`; dropped vertices at `3549-3553`.
  - **Failure:** Nuke fan-triangulates a concave guide (an L-shaped floor), giving faces outside
    the outline. Vertices whose ray misses the plane are dropped silently, and the surface is
    still reported `ok`.
  - **Fix:** ear-clip in 2D guide space, and flag the surface when any vertex is dropped.
- **M22. A guided plane can come from three nearly collinear sparse points** · `unassigned`.
  - **Where:** `autolens_engine.py:3415-3424`, `2975-2981`.
  - **Failure:** three points inside a guide make the sparse plane authoritative with no
    degeneracy check. Points along a seam give an arbitrary orientation, and good proxy hits are
    then rejected for disagreeing with it.
  - **Fix:** require more inliers (about 6 or more) and a non-degenerate eigenvalue ratio, and
    otherwise use the proxy plane.

### Nuke-side data flow
- **M23. `source_frame_range` prefers file numbers over the timeline range (verify)** · `unassigned`.
  - **Where:** `surface_guides.py:423-455`.
  - **Failure:** a Read with a frame offset (files 1001-1100 shown at 1-100) clamps guide
    frames to 1001-1100. The square-pixel proxy renders the wrong frames, and masks
    (`tracking_masks.py:902-921`) are misaligned.
  - **Fix:** use `firstFrame()`/`lastFrame()` for timeline frames, and map file numbers
    explicitly.
- **M24. Setting one surface's guide frame moves every unmapped surface** · `unassigned`.
  - **Where:** `surface_guides.py:124-133`, `2022`.
  - **Failure:** both write the legacy fallback knob that every surface without a map entry
    uses. That includes any shape drawn directly in the Roto.
  - **Fix:** pin current effective frames (`surface_guide_frames(controller)`) into the map
    before changing the fallback.
- **M25. Legacy-guide migration can take another controller's guide** · `unassigned`.
  - **Where:** `surface_guides.py:194-211`, `355`.
  - **Failure:** after copy and paste, controller B's link knob can point at
    `AutoLens1.AUTOLENS_SURFACE_GUIDES_INTERNAL`. That node isn't under B's prefix, so it is
    treated as legacy: copied into B, then deleted from A.
  - **Fix:** treat only nodes outside every AutoLens controller as legacy.
- **M26. `nuke.filename()` without `nuke.REPLACE` returns the pattern** · `unassigned` /
  `ui-playback`.
  - **Where:** `surface_guides.py:826` (`_evaluated_source_filename`), `dialog.py:1844`.
  - **Failure:** the guide JSON's `source_image` is `shot.%04d.exr`, so the engine never matches
    a pose by name. The preview fast path never fires for sequences, so every frame takes the
    slow Write render.
  - **Fix:** `nuke.filename(node, nuke.REPLACE)`.
- **M27. Group context for buttons and temp nodes (verify)** · `ux-simplify` / `ui-playback`.
  - **Where:** `node.py:493/553/571`; `dialog.py:1578-1593`, `1862`; `tracking_masks.py:543`;
    `pixel_aspect.py:56-65`.
  - **Failure:** PyScript buttons on a Group run in the Group's context, so `nodePaste`, temp
    Writes and selection handling can act inside the AutoLens Group.
  - **Fix:** wrap in `with nuke.root():`, and pass node objects to `nuke.execute`.
- **M28. Tracking-mask export has no cache, and solves every image in the folder** · `perf-sparse-tracking`.
  - **Where:** `tracking_masks.py:860-1047`, `887-896`; `panel.py:543`.
  - **Failure:**
    - Every Run re-renders all frames, with one `nuke.execute` per frame.
    - Stale PNGs are never cleared.
    - The solve folder is the Read's directory, so other versions or slates in it get solved
      and masked too.
    - A missing image folder silently drops the mask.
  - **Fix:** one ranged execute, a manifest cache, clearing old PNGs, filtering to the Read's
    pattern and range, and a log line when masking is skipped.
- **M29. Stale square-pixel proxy frames** · `unassigned` · **Fixed on this branch.**
  - **Where:** `pixel_aspect.py` (`ensure_square_pixel_proxy`).
  - **Failure:** the cache folder name holds only size and PAR. A different or shorter plate
    re-rendered into it kept the old extra frames, and the engine solves every image in the
    folder.
  - **Fix:** clear frames and the manifest before re-rendering. Test:
    `tests/test_pixel_aspect.py`.

### Panel, node and dialog behaviour
- **M30. The legacy upgrade hides old results** · `ux-simplify`.
  - **Where:** `node.py:889-945`.
  - **Failure:** a pre-versioning script whose result sits directly in `…/AutoLens` resolves to
    `…/AutoLens/v1`. Import is greyed out, and Run starts from scratch.
  - **Fix:** keep the root as a "(root)" version when it holds `AutoLens_v*.nk`.
- **M31. Script load overwrites the saved version, and dirties the script** · `ui-startup`.
  - **Where:** `node.py:282-300`, `1716-1733`.
  - **Failure:** with the output drive unmounted, or another machine, the saved "v2" isn't
    discovered and is reset to "Create New (v1)". The upgrade adds knobs and inputs and may
    restore guides on every load.
  - **Fix:** leave the version alone when the root is unreachable, and only add what is missing.
- **M32. Inputs stay editable during a run** · `ux-simplify`.
  - **Where:** `panel.py:1415-1442`.
  - **Failure:** switching version mid-run imports the wrong folder, or nothing. Line 1425
    hard-codes `AutoLens_v08.nk` instead of `find_result_nk`.
  - **Fix:** capture the output folder at start, disable inputs while running, and use
    `find_result_nk`.
- **M33. Heavy repeated callbacks** · `ux-simplify` / `ui-startup`.
  - **Where:** `node.py:1707-1713`, `panel.py:284-294`.
  - **Failure:**
    - `ensure_runtime_knobs` runs about 4 times per panel open, each with directory scans and
      legacy migration.
    - Each keystroke in Output root writes the knob, which triggers a deferred knob rebuild and
      more scans.
  - **Fix:** stamp the node once it is upgraded, use `editingFinished`, and debounce.
- **M34. `self.parent()` is never the dialog** · `ux-simplify`.
  - **Where:** `panel.py:1298`.
  - **Failure:** the scroll area reparents the panel, so the Surfaces sidebar never reloads after
    guides are restored.
  - **Fix:** use `self.window()`.
- **M35. Deleting all guides on purpose brings them back** · `ux-simplify`.
  - **Where:** `surface_guides.py:906-907`, `node.py:1076`.
  - **Failure:** with no exportable surfaces, the old JSON stays, and
    `restore_result_guides` restores whenever the Roto has 0 shapes.
  - **Fix:** restore only when the guide node doesn't exist, or record "intentionally empty".
- **M36. Dialog robustness issues** · `ui-playback` unless noted.
  - Every frame resets zoom and pan (`dialog.py:158-159`).
  - In the Masks tab, clicks still drag the invisible surface handles and commit them
    (`506-530`).
  - Every edit calls `nuke.show(roto)` and pops the Roto panel: `show_node=True` and `show=True`
    defaults at `2014` and `2214-2217` (`ux-simplify`).
  - `self.read_node` is cached and never revalidated (`1092`).
  - No slot handles a deleted controller (`1778-1798`, `1471-1489`).
  - Temp preview Writes are created with undo on, which marks the script modified and runs
    studio Write callbacks (`1862-1881`).
  - The panel is constructed before `controller_node` is set (`1585` vs `1595`, `ui-startup`).

---

## Low

**Engine parsing and maths** (`unassigned` unless noted)
- `images.txt` parsers drop blank lines and then pair lines, so an image with an empty POINTS2D
  line shifts every later image (`autolens_engine.py:368-370`, `1868-1880`, `2792-2800`).
  `parse_colmap_images` also accepts any 10-token line as a header (`477-489`). Parse header and
  points lines strictly in alternation.
- `parse_frame_number` takes the last digit group, so `plate_1001_v2.exr` gives frame 2
  (`autolens_engine.py:425`, and also `tracking_masks.py:158-163`, `perf-sparse-tracking`).
  Prefer the padded group before the extension, and fall back to sequence order on duplicates.
- `guide_pixel_to_colmap` flips with `(H-1)-y`; continuous coordinates need `H-y`
  (`autolens_engine.py:2883`). Tracking masks already use `H-y`.
- STMaps use integer pixel indices and normalise by `w-1` and `h-1` (`4106-4152`), giving up to
  ±0.5 px error even with zero distortion.
- `radial_inverse` runs 12 fixed iterations with no convergence check (`2766-2783`).
- `apply_world_rotation_to_poses` leaves `qvec` and `tvec` in raw COLMAP space while
  `nuke_matrix` is rotated, so the camera-track JSON disagrees with itself (`609-631`).
- `_rotation_align_vectors` divides its input arrays in place (`515-516`).
- `choose_smart_dense_views` can return 0-1 views, giving an unclear COLMAP error
  (`2062-2069`, `perf-solve-speed`).
- `int(requested_frame)` is unguarded in guide fitting (`3570`, `3581`).

**Engine flow and messages** (`unassigned` unless noted)
- `--surface-guides` is silently ignored with `--skip-dense` or a missing file (`5965`,
  `6017-6019`).
- Guides-only exits 0 even when no surface fits, and stamps `result["version"] = "0.10.7"`
  (`3992`, `4027`, `4075`).
- `--force` cleanup has no error handling (a Nuke-locked OBJ aborts the run) and leaves
  `guided_surfaces/`, `depth/` and the selection files behind (`5550-5573`).
- The top-level handler prints only `str(exc)`, so a KeyError shows as `'frame'`. Add
  `traceback.print_exc()` (`6211-6214`).
- A launcher `.json` with an unknown `type` is executed as a program (`190-234`).
- The mask signature uses a case-sensitive `*.png` glob (`1764`, `perf-sparse-tracking`).
- The depth pipeline (`4538-4744`) is never called, yet the `--write-stmaps` help says the
  output carries depth.Z. Its cache signature and frame padding are also wrong if it gets wired
  in.
- The `info` block in `write_nuke_script` is built and never written (`5154-5169`). The `.nk`
  Root name is hard-coded as `AutoLens_v01030`.

**Plugin** (area as noted)
- Literal `"\\n"` in user-facing strings shows a backslash-n: `dialog.py:2435-2438`
  (`ui-startup`); `node.py:663` and `704-720` (`ux-simplify`); and the engine mask banner.
- Enter in a text field triggers the dialog's default button (`dialog.py:1080`, `ui-startup`).
- `processEvents()` during `__init__` lets a double-click build two dialogs, and a dialog
  half-built by a failed `__init__` is never deleted (`dialog.py:1834`, `2430-2443`,
  `ui-startup`).
- On the timeline:
  - Right and middle clicks scrub (`dialog.py:861-871`).
  - Out-of-range guide markers are clamped to the ends (`664-675`).
  - It never follows Nuke's frame after startup (`1827-1831`, `ui-playback`).
- Duplicate surface names are allowed (`dialog.py:2136-2148`, `ux-simplify`).
- The Invert status message always says "inverted" (`dialog.py:1539-1542`, `ux-simplify`).
- The output folder is created before validation, outside a try, and an empty root becomes `.`
  (`panel.py:1387-1393`, `ux-simplify`).
- Browse source drops the mask input and the PAR proxy. A guide export failure then aborts the
  run (`panel.py:875`, `1001-1036`, `ux-simplify`).
- `_paste_single_named_result_node` skips the importer's sanitisers (`node.py:1426-1462`,
  `ux-simplify`).
- Relative Read paths aren't resolved against `project_directory` (`panel.py:539-551`,
  `ux-simplify`).
- Solve parameters (`perf-sparse-tracking`):
  - `--max-image-size` is never passed.
  - The PAR proxy is sized from the *dense* max dimension (`panel.py:942`).
  - The min-reference spin box can exceed max.
- `create_autolens_node` calls `node.begin()` without try/finally, and
  `upgrade_existing_nodes` doesn't recurse into groups (`node.py:617-638`, `1719`,
  `ui-startup`).
- Runner issues (`runner.py:49-72`, `unassigned`):
  - Cancel is reported as a failure.
  - A new QProcess per run is never deleted.
  - `waitForStarted` blocks the UI.
  - `started` is emitted before start succeeds.
- `_copy_legacy_guide_into_group` leaves a pasted copy behind on error
  (`surface_guides.py:299-360`).
- Auto-suggested shapes may be shifted half a grid cell (`surface_guides.py:2037-2041`,
  verify).
- A tracking mask with no alpha channel silently masks nothing (`tracking_masks.py:511`).
- `importer.py:488`: when the source isn't a Read, the `PLATE_REPOINT_TO_SEQUENCE` placeholder
  Read is kept, even though Undistort is rewired to the source.

**Installer, environment and dead code** (`unassigned` unless noted)
- `environment.colmap_root()` given a COLMAP *folder* returns its parent, so
  `normalize_colmap_path` looks for `COLMAP.bat` one level too high (`environment.py:9-25`).
- `environment.build_colmap_invocation` and `build_fresh_windows_invocation` are imported by
  the panel but never called. The engine has its own launcher logic.
- `Start-Process -UseNewEnvironment` in Windows PowerShell 5.1 is known to drop
  `USERPROFILE`, `APPDATA` and `LOCALAPPDATA` (speculative impact: CUDA's JIT cache).
- Dead code in the panel (`setup_python_environment`, `_colmap_smoke_test`, `_find_colmap`,
  the browse helpers, and unreachable lines `1333-1335`) and in tracking_masks (the internal
  mask-Roto pipeline, which writes a second `autolens_tracking_masks/` folder). Tagged
  `ux-simplify` / `perf-sparse-tracking`.
- The installer prompts "Press Enter" and then the `.bat` `pause`s again.
- Unicode output from `log()` could raise on a cp1252 stdout. **Fixed on this branch** (part
  of M2).
- Degenerate Roto shapes made `_sample_roto_shape` raise, aborting the guide export or the
  dialog reload, or else return a zero-area guide. **Fixed on this branch**
  (`surface_guides.py:792-812`, tested). The guide JSON is now written atomically.

---

## Stale or incorrect docs

- **README › Installation** offers `python install_autolens.py` with no platform note, but the
  installer exits on anything other than Windows. It also doesn't mention the NVIDIA driver
  requirement (CUDA 12.9+, see M6). Add a "Requirements" block: Windows 10/11, NukeX 14-17,
  NVIDIA driver with CUDA 12.9 or later, about 10 GB disk, internet for the first install.
- **README › Development** says "`main` is protected". Confirm branch protection is actually
  enabled on GitHub. This audit can't see repository settings.
- **`AutoLens/README.md`** (2,057 lines) is mostly per-version history for v0.8-v0.10, and
  overlaps the CHANGELOG. It ships inside the plugin folder. Trim it to a short user guide and
  link to the CHANGELOG.
- **Version strings that never move:**
  - `AutoLens/menu.py:1` says v0.10.3.
  - The engine docstring says "AutoLens V0.8.3".
  - The guides-only result stamps `0.10.7`.
  - The mesh-fit OBJ header says "V0.8".
  - `tracking_masks.py:1023` stamps `0.10.26`.

  Either drop them or read `autolens.__version__`. `scripts/release.py` only bumps
  `__init__.py` and the installer.
- **`--write-stmaps` help text** promises depth.Z, which is never produced.
- **docs/TROUBLESHOOTING › Non-square pixel footage** says "use a build with PAR-aware solve
  proxies". Every current build has them, so this should say what to check instead.
- **docs/ARCHITECTURE › Caching** lists dense workspace and fused geometry reuse with no
  caveats. See H11, M10 and M12 for when that reuse is wrong.
- **`docs/LINKEDIN_ARTICLE.md`** is marketing copy inside the technical docs folder. Consider
  moving it out of the repo or into a `press/` folder.

---

## License

The repository is **public** but has no license (`LICENSE_NOT_SPECIFIED.md`). By default that
means all rights reserved. Nobody may legally copy, modify or redistribute AutoLens, including
studios that want to install it and contributors who want to send pull requests. A public repo
with no license also tends to put off users and contributors.

Points for the decision:

- **Dependencies don't constrain the choice.** AutoLens doesn't vendor COLMAP; the installer
  downloads it from conda-forge. COLMAP and pycolmap are BSD-3-Clause, NumPy is BSD, OpenEXR is
  BSD-3-Clause, and Miniforge is BSD-3-Clause. PySide is LGPL, but it is used, not
  redistributed. Nuke's Python API carries no license obligation for plugin code. Any license
  below is compatible.
- **Options:**
  - **MIT or Apache-2.0.** The most adoption-friendly for studios. Apache-2.0 adds an explicit
    patent grant and contribution terms, which suits a tool with novel methods (guided surfaces,
    smart dense view selection).
  - **GPL-3.0 or MPL-2.0.** Modifications that are distributed must stay open. MPL is per-file
    and friendlier to studio pipelines.
  - **Source-available, non-commercial** (for example PolyForm Noncommercial 1.0.0). Keeps
    commercial rights with DeadCow VFX, while still letting people read and try the code.
- **Recommendation:** if AutoLens is meant as an open tool, use **Apache-2.0**. If commercial
  licensing is planned, use **PolyForm Noncommercial**, plus a line saying commercial licenses
  are available.
- **Either way:**
  - Replace `LICENSE_NOT_SPECIFIED.md` with `LICENSE`.
  - Add the license to the README and to the release zip (`scripts/release.py`
    `PACKAGE_FILES`).
  - If outside contributions are expected, add a short CONTRIBUTING note.
- **This is the owner's decision.** No license has been added on this branch.

---

## Roadmap of follow-up PRs

Ordered by user impact against effort. PRs 1-3 are small and should ship first, as one patch
release, once the parallel sessions' branches have merged, so there are no conflicts in
dialog, panel, node or the engine.

1. **Nuke 14 and broken-output hotfix** (S).
   - Covers C1 `QShortcut`, H1 `exec`, C3 `import json`, C2 meshfit OBJ newlines, H2
     knobChanged newlines, M14 proxy-cleanup KeyError, and literal `\n` in messages.
   - Add the module-import smoke test (PR 12) in the same PR.
   - Touches `dialog`, `panel`, `node` and the engine.
2. **Workspace lifetime** (S-M, `ui-startup`).
   - Covers H3 Esc/`reject`, H4 `_DIALOGS` liveness, dialogs half-built by a failed `__init__`,
     the panel constructed before the controller is set, and deleted-controller guards (M36).
3. **Process lifetime and environment** (M, `unassigned`).
   - Covers M7 (Job Object / killpg, quit and script-close hooks) and M8 (clean env, UTF-8,
     incremental decoder).
   - This also prevents the locked-database cascades behind M11.
4. **Scrub and mask-preview performance** (M, `ui-playback`).
   - Covers H5 and H6: debounce, cache, no `processEvents`, downscaled renders, and the
     `nuke.REPLACE` fast path (M26).
   - Also the zoom reset and mask-tab clicks (M36).
5. **Surface-guide data safety** (M, `unassigned`).
   - Covers H7 in-place shape updates, M24 frame-map pinning, M25 legacy migration, and M35
     intentional-empty.
   - Needs Nuke testing with layered and animated shapes.
6. **Cache correctness** (M, `perf-solve-speed` / `perf-sparse-tracking`).
   - Covers H11 dense tied to sparse, M10 undistort completion marker, M11 and M12 cache errors,
     and M13 staged signatures.
   - Add Nuke-free tests that build fake caches in a temp dir.
7. **Tracking-mask quality and speed** (S-M, `perf-sparse-tracking`).
   - Covers H10 binarised raw masks, M28 cache and range filtering, and the mask-signature case.
8. **Lens and camera accuracy** (M, `unassigned`).
   - Covers H8 corner sampling, M19 `win_translate` (verify first), the guide `H-y` flip, and
     STMap pixel centres.
   - Needs one reference shot with a known grid to confirm before and after.
9. **Engine speed** (M, `perf-solve-speed`).
   - Covers M15 undistort only selected views, M16 sequential matching threshold, M17
     partial-registration fallback, and M18 numpy PLY and adjacency.
10. **Node and panel UX correctness** (M, `ux-simplify`).
    - Covers H14 Create knobs moving tab, M30 legacy results, M31 version on load, M32 inputs
      during run, M33 callback cost, M34 `self.window()`, M9 non-blocking guides refresh, and
      the dead code.
11. **Installer hardening** (M, `unassigned`).
    - Covers M3 safe env root, M4 Python 3.11 runtime, M5 pinned and verified Miniforge, M6
      CPU-only option, the `colmap_root` folder case, and the double pause.
12. **Test net** (S, any).
    - Import every `autolens.*` module with stub `nuke`/`nukescripts` and both PySide versions
      mocked (this would have caught C1 and C3).
    - Add engine writer round-trip tests (OBJ, PLY, `.nk` parse), and the `images.txt` parser
      with an empty POINTS2D line.
13. **Nuke-script and geometry robustness** (M, `unassigned`).
    - Covers M20 Tcl escaping, M21 triangulated guided surfaces, and M22 degenerate sparse
      planes.
14. **Docs and license** (S).
    - Covers the [Docs](#stale-or-incorrect-docs) list, the trimmed plugin README, the
      requirements block, and the license decision above.

---

## Checked and fine

- **Python 3.9.**
  - Every plugin module and the engine use `from __future__ import annotations`.
  - There are no `match` statements, `zip(strict=)`, runtime `X | None` or
    `typing.get_type_hints` calls.
  - `argparse.BooleanOptionalAction` exists in 3.9.
- **Engine flags.** Every flag `panel.py` and `node.py` pass exists in the engine's argparse,
  with compatible types and choices. The panel's quality table matches the engine presets.
- **Temp nodes and files.** Write and Reformat nodes in `pixel_aspect.py` and
  `tracking_masks.py` are deleted in `finally`. Temp `.nk` and PNG files are removed.
- **Maths.** Quaternion and pose conversion, Rodrigues at θ=π, Möller-Trumbore ray casting, and
  binary PLY vertex reads look correct.
- **COLMAP output.** The engine lets it inherit the engine's console streams instead of
  capturing them, so its output is never decoded.
- **Frame padding.** It is consistent (`%06d` / `:06d`) between the proxy, the temp masks and
  the engine, including negative frames.
- **Qt.**
  - Enum shorthand and `event.position()`/`pos()` are guarded and work on both PySide versions.
  - Zoom, pan and timeline mappings are clamped against division by zero.
  - No QTimer is left running.
