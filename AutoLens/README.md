# AutoLens for NukeX — v0.10.30 Node Edition

AutoLens is now a **node-first NukeX tool**. You add an `AutoLens` node to the
plate like any other Nuke node. The image passes through unchanged, while the
node's Properties panel becomes the persistent controller for the solve.

## Why this version exists

The earlier dockable panel could fail to appear on some Nuke installs without a
useful error. v0.10.30 removes that dependency from the normal workflow. It uses a
regular Group node with native Nuke knobs and opens a floating setup/run dialog
only when needed — closer to the Mocha / Neat Video style of interaction.

## Install

Close Nuke and run:

```bat
install_autolens.bat
```

Restart NukeX.

## Add the node

Select your plate Read (or a simple upstream chain containing it), then use:

```text
Nodes > AutoLens > AutoLens
```

or:

```text
Nuke > AutoLens > Add AutoLens Node
```

Shortcut:

```text
Ctrl + Alt + L
```

The node is a pass-through — it does not alter the plate.

## AutoLens node Properties

The node contains:

- Status
- Quality: Preview / Normal / High
- Run mode: Resume / Rebuild Dense / Rebuild Everything
- Use Surface Guides
- Import Result When Complete
- Output Folder
- **Open AutoLens Setup / Run...**
- **Draw / Edit Surface Guides**
- **Auto Suggest Surfaces**
- **Use Current Frame for Surface Guides**

The Advanced tab stores the external Python and COLMAP paths.

## Surface Guides

**Draw / Edit Surface Guides** creates a native Roto helper associated with that
specific AutoLens node. Draw closed Bezier shapes around expected planar
surfaces such as:

- Floor
- BackWall
- LeftWall
- RightWall
- Counter
- TableTop

The shapes are artist hints, not tracks. AutoLens evaluates them at the guide
frame after solving the camera, then uses dense proxy intersections and sparse
3D observations to fit those 2D shapes into solved 3D space.

**Auto Suggest Surfaces** creates rough editable floor/wall shapes on the current
frame. Adjust them with normal Roto controls before running the solve.

Multiple AutoLens nodes can have separate guide Roto nodes.

## Open AutoLens Setup / Run

This opens a floating configuration window anchored to that AutoLens node. It
contains the detailed quality, smart-reference, GPU/cache, environment, log and
run controls from the previous UI, but no dockable-panel registration is needed.

The node label/status updates while the solve runs.

## Outputs

Geometry:

- `autolens_proxy_mesh.obj`
- `autolens_meshfit_planes.obj`
- `autolens_guided_surfaces.obj` when guides were drawn

Other outputs:

- lens STMaps
- solved camera track
- smart dense-selection report
- AutoLens result JSON
- generated Nuke setup (`AutoLens_v08.nk`)

## Dependencies

External Python requires:

```bat
python -m pip install numpy OpenEXR
```

A CUDA-enabled COLMAP build is recommended.

Use **Check Environment** in the AutoLens setup window to verify the configured
Python interpreter and COLMAP executable.

## Notes

AutoLens remains experimental R&D software. The node-centric UI is intended to
make the workflow feel native and persistent inside the comp while keeping the
heavy COLMAP/Python solve outside Nuke's own Python process.


### Output folder browser

The AutoLens node's **Output folder** field now has a **Browse...** button directly
in the Properties panel. Clicking it opens a folder-selection dialog and writes
the selected directory back to the node, so you do not need to type or paste
the path manually.



## v0.10.30 usability fixes

- The setup/run window is screen-aware and vertically scrollable.
- **Advanced** now truly collapses instead of leaving a tall disabled block.
- AutoLens checks Python dependencies before every run. If NumPy/OpenEXR are
  missing it offers to create `~/.nuke/AutoLens/.venv` and install them.
- **Set Up AutoLens Python** can also be run manually from Environment.
- COLMAP is preflighted before the solve begins rather than failing later.
- AutoLens action buttons in the node Properties panel use short labels and
  one button per row so Nuke does not clip them.


## v0.10.30 fixes

### COLMAP DLL isolation

When launched from Nuke, external programs inherit Nuke's Qt and DLL search
paths. COLMAP is also a Qt/CUDA application, so this can cause Windows error
`0xC0000142` even though COLMAP works from a normal Command Prompt.

AutoLens now:

- strips Nuke/Foundry Qt/Python paths from the external runtime;
- prefers the actual `bin\colmap.exe` over `COLMAP.bat`;
- prepends COLMAP's own `bin` / `lib` directories;
- performs a COLMAP startup test before beginning feature extraction;
- reports the Windows status code if startup still fails.

### Smarter Auto Suggest

The old Auto Suggest always constructed a room-like box from the strongest
horizontal and vertical transitions.

v0.10.30 instead fits long coherent perspective lines and scores their support.
It may create only a floor, a floor + back wall, or no surfaces at all. On
close-ups, shallow-depth-of-field shots, heavily occluded rooms, and images
without trustworthy architectural lines, AutoLens intentionally abstains rather
than drawing a misleading generic box.

Manual Roto Surface Guides remain the authoritative artist input.


## v0.10.30

### Correct Windows COLMAP launcher

AutoLens now prefers the official Windows `COLMAP.bat` wrapper rather than
launching `bin\colmap.exe` directly.

COLMAP's Windows documentation states that the wrapper is the supported command
line entry point because it sets the package's required library paths.

AutoLens launches the wrapper using:

- a fresh `cmd.exe`;
- COLMAP's installation folder as the working directory;
- Nuke / Foundry Qt and Python overrides removed from the child environment.

This specifically targets Windows startup error `0xC0000142`.

### Auto Suggest now includes object planes

Auto Suggest is no longer only a room-box/layout predictor.

It now performs two passes:

1. **Room/layout surfaces**
   - floor
   - back wall
   - side walls when supported

2. **General local planar-face proposals**
   - refrigerator fronts/sides
   - counter tops/fronts
   - cabinet faces
   - tabletops
   - doors
   - screens
   - other bounded flat-looking image regions

General planes are created as:

```text
AUTO_ObjectPlane_01
AUTO_ObjectPlane_02
...
```

These names are intentionally semantic-neutral. Rename or reshape them in the
native Roto node before running the solve.

After reconstruction, guided-surface fitting uses the same camera / proxy /
sparse-point logic for these object planes as for manually drawn walls/floors.


## v0.10.30

### Truly fresh Windows environment for COLMAP

AutoLens no longer attempts to reconstruct a clean COLMAP environment by
editing Nuke's inherited PATH.

On Windows, each COLMAP command is now launched using:

```text
PowerShell
  -> Start-Process -UseNewEnvironment
      -> cmd.exe
          -> COLMAP.bat
              -> COLMAP
```

`-UseNewEnvironment` asks Windows to build the actual child process environment
from the normal Machine/User environment variables rather than Nuke's process
environment.

This is intended specifically for applications such as COLMAP that also ship
Qt/CUDA/native DLLs and can fail with `0xC0000142` when embedded DCC
environments contaminate native library resolution.

### Local object planes are now bounded

The v0.8.4 object detector could combine unrelated infinite image lines and
produce long diagonal cards across the image.

v0.10.30 stores the observed extent of every detected edge segment. An automatic
object plane is only accepted when all four corners fall close to the actual
supported line segments.

Pre-solve object cards now favor:

- refrigerator / cabinet fronts and sides;
- counter fronts;
- doors and panels;
- screen-like faces;
- other localized surfaces with strong horizontal/vertical boundaries.

Arbitrary sloped planes are intentionally left primarily to the post-solve
proxy-mesh plane extraction, where real 3D evidence is available.


## v0.10.30 — Installer is authoritative

AutoLens no longer creates Python environments or smoke-tests COLMAP every time you press Run in Nuke.

Run `install_autolens.bat` outside Nuke. The installer validates Python/NumPy/OpenEXR, the Microsoft VC++ x64 runtime, NVIDIA, and COLMAP. It writes `autolens_install.json` and `AutoLens_Install_Report.txt`.

The Nuke plugin refuses to start reconstruction until that report says `ready: true`. This cleanly separates machine setup from compositing workflow.


## v0.10.30 — Managed COLMAP backend

AutoLens no longer depends on a manually extracted Windows COLMAP package for
its normal Windows workflow.

`install_autolens.bat` downloads the small standalone **micromamba** executable
and creates a private conda-forge environment in:

```text
~/.nuke/AutoLens/.colmap_env
```

It installs a matched dependency set containing:

```text
COLMAP 4.2.0
PyCOLMAP 4.2.0
Python 3.11
CUDA runtime selected for the installed NVIDIA driver
```

The plugin runs:

```text
~/.nuke/AutoLens/.managed_colmap/COLMAP.cmd
```

which invokes COLMAP through that resolved environment.

An older installation such as `D:\Tools\colmap-x64-windows-cuda` is no longer
used by default and can be left installed without affecting AutoLens.

The first v0.9 installation is larger and takes longer because it downloads the
actual COLMAP/CUDA runtime. Future AutoLens upgrades preserve the environment.


## v0.10.30 — Integrated Surface Workspace

### Setup / Run dialog fix

v0.9's installer stores the Python runtime as structured JSON:

```json
{
  "ok": true,
  "path": "C:/Users/.../.nuke/AutoLens/.venv/Scripts/python.exe"
}
```

The old Setup dialog incorrectly passed that dictionary to `Path()`, producing:

```text
expected str, bytes or os.PathLike object, not dict
```

v0.10.30 accepts both the new structured installer format and legacy string paths.

### Mocha-style workspace

**Setup / Run...** now opens a wide workspace rather than a tall settings-only
dialog.

The plate stays visible on the left while AutoLens surfaces are drawn directly
over it.

Features:

- floor / wall / object-plane overlays in the AutoLens window;
- direct draggable corner handles;
- surface selection list;
- rename / add / delete planar surfaces;
- Auto Suggest without leaving the window;
- Set Guide Frame;
- Open Roto for full native Nuke Bezier editing;
- mouse-wheel zoom;
- middle-mouse pan;
- persistent Solve tab containing quality, output, progress and log controls.

Edits in the integrated surface viewer are synchronized back to the dedicated
AutoLens Roto guide node, so the existing guided 3D fitting pipeline remains
unchanged.


## v0.10.30 — Installer diagnostics / Micromamba repair

The v0.9.1 package accidentally still identified its installer as v0.9.0.

v0.10.30 fixes that and makes every installer stage persistent. The install
report now contains:

```text
stage
error
micromamba
colmap
ready
```

A new live log is also written while installation is running:

```text
~/.nuke/AutoLens/AutoLens_Install_Live.log
```

Micromamba installation now follows the official Windows archive method and
tries three download paths in order:

1. PowerShell `Invoke-WebRequest`
2. Windows `curl`
3. Python `urllib`

The managed COLMAP environment explicitly requests the CUDA builds of COLMAP
4.2.0 and PyCOLMAP 4.2.0. If CUDA 13.0 solving fails on a compatible driver,
the installer automatically retries CUDA 12.9.

The installer window remains open and prints the exact stage and report paths
when anything fails.


## v0.10.30 — Miniforge backend

Micromamba was removed from the Windows installer after testing showed the
standalone micromamba executable itself could fail to launch on some systems.

AutoLens now installs the official **Miniforge3 Windows x86-64** distribution
silently into:

```text
~/.nuke/AutoLens/.miniforge3
```

and uses Miniforge's `conda` with the libmamba solver to create:

```text
~/.nuke/AutoLens/.colmap_env
```

The managed environment requests the Windows CUDA builds of:

```text
COLMAP 4.2.0
PyCOLMAP 4.2.0
Python 3.11
```

The installer selects CUDA 13.0 first when supported by the installed NVIDIA
driver and automatically retries CUDA 12.9 if that environment cannot be
resolved/installed.

The existing live log and stage/error reporting remain in:

```text
~/.nuke/AutoLens/AutoLens_Install_Live.log
~/.nuke/AutoLens/AutoLens_Install_Report.txt
```


## v0.10.30 — Direct managed-COLMAP launcher

On the tested system, all direct Miniforge/Conda COLMAP tests passed:

```text
COLMAP CLI: PASS
COLMAP feature extractor: PASS
COLMAP PatchMatch: PASS
PyCOLMAP: PASS
```

but the extra `COLMAP.cmd` wrapper could remain open and time out.

v0.10.30 removes that wrapper completely.

The installer now writes:

```text
~/.nuke/AutoLens/.managed_colmap/launcher.json
```

containing the managed `conda.exe` path and environment prefix.

The AutoLens engine invokes COLMAP directly as:

```text
conda.exe run -p <AutoLens .colmap_env> --no-capture-output colmap <command>
```

which is the exact execution route already validated during installation.

### Installer exit code

The Windows batch installer previously captured `%ERRORLEVEL%` inside a
parenthesized block, causing a failed Python installer to sometimes be printed
later as exit code `0`.

The batch launcher now captures the Python exit status outside parenthesized
blocks, so success/failure reporting reflects the real installer result.


## v0.10.30 — Cached Miniforge installer fix

When Miniforge was already installed and healthy, the installer returned the
Miniforge result dictionary directly instead of returning the standard
`(result, error)` pair. The caller then raised:

```text
ValueError: too many values to unpack (expected 2)
```

v0.10.30 fixes the healthy cached-Miniforge return path and also adds defensive
installer result normalization so legacy single-object helper returns cannot
crash installation.

Existing directories are preserved and reused:

```text
~/.nuke/AutoLens/.venv
~/.nuke/AutoLens/.miniforge3
~/.nuke/AutoLens/.colmap_env
```

A machine where the managed COLMAP environment already validates should only
need to re-run its quick validation tests and create:

```text
~/.nuke/AutoLens/.managed_colmap/launcher.json
```


## v0.10.30 — Clean source sequence + Stage 6 progress

### COLMAP input isolation

AutoLens can store its output folder inside the source plate directory. COLMAP
recursively scans `--image_path`, which previously caused AutoLens output files
to be encountered during feature extraction.

v0.10.30 writes:

```text
autolens_colmap_image_list.txt
```

containing only the source sequence and passes it through COLMAP's
`--image_list_path` option.

This prevents databases, guide JSON, STMaps and future dense output from being
mistaken for source images on reruns.

### Lower-memory STMap generation

Lens-map generation no longer allocates the two full-resolution STMaps plus
full-frame float64 mesh grids simultaneously.

The undistort and redistort maps are now generated independently in row tiles,
written immediately, and released before the next map is created.

Stage 6 now logs progress such as:

```text
[AUTOLENS STMAP] 1/4 Computing UNDISTORT STMap
[AUTOLENS STMAP] undistort: 3/12 tile(s) (25%)
...
[AUTOLENS STMAP] Wrote autolens_undistort.exr

[AUTOLENS STMAP] 2/4 Computing REDISTORT STMap
...
[AUTOLENS STMAP] 4/4 Lens + camera outputs complete
```

This makes memory usage more predictable and prevents a long silent period at
55% progress.


## v0.10.30 — Modern OpenEXR STMap writer

The solve could reach 100% STMap computation and then spend a long period in:

```text
Writing autolens_undistort.exr ...
```

The previous writer used OpenEXR's deprecated `OutputFile/writePixels` Python
API. It split the packed RGB NumPy array into three channel copies and then
created three additional Python byte buffers before writing.

v0.10.30 uses the supported OpenEXR 3.x `File` API:

```python
header = {
    "compression": OpenEXR.NO_COMPRESSION,
    "type": OpenEXR.scanlineimage,
}
channels = {"RGB": rgb}

with OpenEXR.File(header, channels) as outfile:
    outfile.write(path)
```

The STMaps remain RGB float32. Compression is intentionally disabled because
they are deterministic utility maps and fast I/O is preferable to spending CPU
compressing them.

The old OutputFile API is retained only as a compatibility fallback.

EXR writes now report backend, file size and timing:

```text
[AUTOLENS EXR] Writing autolens_undistort.exr:
3424x2202, RGB float32, NO_COMPRESSION

[AUTOLENS EXR] Finished autolens_undistort.exr:
86.3 MB in 1.8s (OpenEXR.File)
```


## v0.10.30 — Native LensDistortion2 output

STMap EXRs are no longer generated during a normal AutoLens solve.

Instead, the solved COLMAP `RADIAL` lens is fitted to Nuke's native Custom
`LensDistortion2` radial rational model:

```text
ru = rd * (1 + n0*r² + n1*r⁴)
          ---------------------
          (1 + d0*r² + d1*r⁴)
```

This is fit against the exact inverse of COLMAP's solved:

```text
rd = ru * (1 + k1*r² + k2*r⁴)
```

over the actual usable image radius.

The imported result now contains:

```text
AUTOLENS_UNDISTORT  LensDistortion2 / Undistort
AUTOLENS_REDISTORT  LensDistortion2 / Redistort
AUTOLENS_CAMERA     Camera2
```

The source Read is connected directly to `AUTOLENS_UNDISTORT`.

The fitted Nuke coefficients, original COLMAP values, center conversion, and
pixel residual statistics are saved to:

```text
autolens_lens_model.json
autolens_result.json
```

The engine still has an optional `--write-stmaps` diagnostic/interchange flag,
but it is off by default.


## v0.10.30 — Nuke 17 LensDistortion import + persistent re-import

### LensDistortion import compatibility

Recent Nuke versions changed scripting details around lens/projection controls.
A v0.9.8 result could fail to paste with:

```text
AUTOLENS_UNDISTORT.projection: Bad value for projection : Rectilinear
AUTOLENS_REDISTORT.projection: Bad value for projection : Rectilinear
```

AutoLens now omits the version-sensitive lens/projection enum fields entirely.
Spherical lens type and rectilinear projection are Nuke defaults.

The importer also sanitizes old v0.9.8 `.nk` results before pasting, removing:

```text
lensType Spherical
projection Rectilinear
```

This means an already-completed v0.9.8 solve can be imported after installing
v0.10.30 without rerunning COLMAP or dense reconstruction.

### Persistent result state

Completed results are now remembered on the AutoLens controller node using the
hidden knob:

```text
autolens_result_nk
```

When Setup / Run is closed and reopened, AutoLens searches:

1. the current Output Folder for `AutoLens_v08.nk`;
2. the remembered result path on the AutoLens node.

If either exists, **Import Result** is immediately enabled.

New AutoLens nodes also expose an **Import Result** button directly in the node
properties for importing/re-importing a completed solve without opening the
workspace.


## v0.10.30 — Explicit undistorted tracking/VFX space

The Nuke handoff now follows the standard VFX lens workflow:

```text
DISTORTED SOURCE
      ↓
AUTOLENS_UNDISTORT
      ↓
AUTOLENS_TRACKING_PLATE
      ↓
Camera / mesh / cards / Project3D / paint / CG / comp
      ↓
AUTOLENS_REDISTORT
      ↓
DISTORTED DELIVERY
```

Judge all camera and geometry alignment against `AUTOLENS_TRACKING_PLATE`,
not against the original distorted source.

### Correct Nuke Maximum normalization

v0.9.8/v0.9.9 used the full maximum image dimension when converting COLMAP
radial coefficients to Nuke's Custom radial model.

Nuke's Maximum convention uses half of the maximum image dimension:

```text
max(width,height) / 2
```

The old conversion therefore under-applied the distortion.

v0.10.30 records:

```json
"normalisation_convention": "max(width,height)/2"
```

### Existing solve migration

Completed v0.9.8/v0.9.9 solves do not need to rerun COLMAP.

At import time the old fitted coefficients are converted exactly:

```text
r² coefficients -> /4
r⁴ coefficients -> /16
centre offset   -> ×2
```

The existing camera poses, proxy mesh, mesh-fit planes, and guided surfaces are
still valid.


## v0.10.30 — Guide-frame range safety

AutoLens now automatically clamps surface-guide frames to the source Read
range.

For a Read with:

```text
523–642
```

a current/guide frame of:

```text
1
```

automatically becomes:

```text
523
```

and a frame after 642 automatically becomes 642.

This happens when creating guides, setting the guide frame, opening the
workspace outside the shot range, using Auto Suggest, exporting the guides, and
starting a solve.

### Existing guide frames are preserved

Previous builds could overwrite the stored guide frame with `nuke.frame()` each
time the guide Roto node was reopened. v0.10.30 no longer does this.

### Nearest registered camera fallback

The engine still checks the solved camera poses. If an in-range guide frame is
missing or failed to register, the nearest registered camera is used.

### Guide errors no longer fail a completed solve

Surface guides are optional. If guide fitting still fails for any reason,
AutoLens logs a warning and completes normally with the solved lens, camera,
proxy mesh, and mesh-fit planes.


## v0.10.30 — Clean result graph / ready-to-comp template

Import Result now builds a compact Nuke graph around the actual source Read.

```text
                         Read
                           |
                       Undistort
                           |
                    Tracking Plate
                           |
                 +---------+---------+
                 |                   |
             3D Scene          undistorted BG
                 |                   |
          ScanlineRender             |
                 |                   |
                 +-------> Merge <----+
                           |
                    Redistort Input
                           |
                      Redistort
                           |
                         Final
```

Three geometry choices are placed in a clean shelf on the left:

```text
Proxy Mesh
Mesh-fit Planes
Guided Surfaces
```

AutoLens connects one default geometry to the Scene using this preference:

```text
Guided Surfaces > Mesh-fit Planes > Proxy Mesh
```

The other geometry nodes remain immediately adjacent as alternatives.

The solved Camera is connected both to the Scene for 3D viewing and to the
ScanlineRender `cam` input.

The ScanlineRender output is merged over the undistorted tracking plate, then
fed into the native AutoLens Redistort node.

### Layout

The imported result is positioned relative to the existing source Read rather
than at a hard-coded Nuke graph origin.

The dedicated AutoLens controller and its surface-guide Roto node are also
placed neatly beside the source Read when available.

Older visual clutter is removed on import:

```text
AUTOLENS_TRACKING_SPACE_BACKDROP
AutoLens_Project3D_Help
```

A single `AutoLens_Info` backdrop contains the solve metrics and the concise
workflow reminder:

```text
UNDISTORT -> TRACK / GEO / COMP -> REDISTORT
```

Existing completed solves can simply be re-imported; no camera or dense solve
needs to be rerun to get the new graph layout.


## v0.10.30 — Streamlined controller + result/version status

### AutoLens node properties

The controller node is intentionally lightweight.

The external input is now labeled:

```text
Source
```

The following property-panel buttons were removed:

```text
Import Result
Edit Surface Guides
Auto Suggest Surfaces
Set Guide Frame
```

Those workflows remain inside **Setup / Run...**, where the plate viewer and
surface workspace provide the needed context.

Existing AutoLens nodes are upgraded automatically when a Nuke script loads.

### Completed-result status

The node properties now display:

```text
✓ Completed Result Available
```

in green whenever the exact folder in **Output folder** contains a completed
AutoLens NK result.

If not, the properties show a subdued:

```text
○ No completed result in this folder
```

Changing the Output folder updates the status automatically.

The Setup / Run workspace shows the same state as a prominent checked green
checkbox:

```text
☑ Completed Result Available
```

and the Run status becomes green.

### Versioned tracks

If **Run AutoLens** is pressed while the current output folder already contains
a completed result, AutoLens asks:

```text
Overwrite / Reuse Existing
Create v2
Cancel
```

Choosing the version option creates the next unused folder automatically:

```text
v2
v3
v4
...
```

If the controller already points to `v2`, the next version becomes a sibling
`v3`, not `v2/v2`.

The controller's Output folder is updated to the new version before the solve
starts.


## v0.10.30 — Completed tracks restore their surface guides

A completed AutoLens output folder is now treated as a complete track package.

When an AutoLens controller discovers a completed result in its current
**Output folder**, it also looks for:

```text
autolens_surface_guides.json
```

If that file contains saved guide surfaces and the controller does not already
have live guide shapes, AutoLens silently rebuilds the dedicated Roto guide node
and links it to the controller.

No Roto properties panel is opened during automatic restoration.

### Restoration behavior

Automatic restoration is deliberately conservative:

```text
No guide node             -> restore saved guides
Empty guide node          -> restore saved guides
Existing live guide shapes -> preserve them
```

This prevents result discovery from destroying guide edits already present in
the current Nuke script.

The restored guide uses the current shot's upstream Read node, not the original
Nuke node name stored in the old result JSON.

The saved guide frame is also clamped to the current Read's valid frame range.

### Versioned tracks

Each version folder carries its own guide JSON:

```text
AutoLens/
    autolens_surface_guides.json
    AutoLens_v08.nk

    v2/
        autolens_surface_guides.json
        AutoLens_v08.nk

    v3/
        ...
```

Pointing the controller at one of those completed folders restores the guide set
associated with that specific track.

Older completed tracks can also be restored because the existing guide JSON
already contains the evaluated surface names, outlines, guide frame, source
image name, and image dimensions.


## v0.10.30 — Surface guides live inside the AutoLens node

The dedicated Roto guide node is no longer a separate root-level node.

The visible Nuke graph is now simply:

```text
Read
  |
  | Source
  v
AutoLens
```

Inside the AutoLens Group, AutoLens maintains a private:

```text
AUTOLENS_SURFACE_GUIDES_INTERNAL
```

Roto node.

The Group's normal image path is still a direct pass-through:

```text
Source Input -> Output
```

The private Roto is only guide/editing state and does not alter the image
flow.

### Existing scripts

When an older script contains:

```text
AUTOLENS_SURFACE_GUIDES_<controller>
```

as a visible root-level Roto node, v0.10.30 automatically migrates it into the
AutoLens Group.

Migration uses Nuke's own node serialization so the full Roto node — including
Bezier curves, tangent handles, animation, frame metadata, and AutoLens custom
knobs — is preserved as accurately as Nuke itself can serialize it.

Only after a successful internal copy does AutoLens delete the legacy external
Roto node.

### Completed-track restoration

Restoring `autolens_surface_guides.json` from a completed track also creates the
Roto internally, so discovering an old track no longer adds a second node to
the root graph.

### Editing

The Mocha-style AutoLens workspace continues to edit these guide shapes
directly.

The optional **Open Roto** action can still show the internal Roto properties
for advanced native Nuke Bezier editing, but the guide node remains hidden from
the root DAG.


## v0.10.30 — v1 default folders + cleaner import + Import Result button

### Default output folder

A new AutoLens controller now defaults to:

```text
.../AutoLens/v1
```

instead of:

```text
.../AutoLens/AutoLens
```

This keeps version folders consistent from the first track onward:

```text
v1 -> v2 -> v3 -> ...
```

The existing versioning logic is unchanged, so a completed `v1` naturally offers
`v2` as the next sibling version.

### Cleaner imported result graph

The imported comp graph now removes the helper dots:

```text
AUTOLENS_REDISTORT_INPUT
AUTOLENS_FINAL
```

The final comp path is simply:

```text
Merge -> Redistort
```

The imported nodes also no longer add extra labels on:

```text
AUTOLENS_PROXY_MESH
AUTOLENS_MESHFIT_PLANES
AUTOLENS_GUIDED_SURFACES
AUTOLENS_SCENE
AUTOLENS_SCANLINE
AUTOLENS_MERGE
```

Their node names remain intact; only the extra label text is removed to reduce
visual clutter.

### Import Result button back on the controller

The AutoLens properties panel now includes:

```text
Import Result
```

again.

It is enabled only when the current **Output folder** already contains a
completed AutoLens result. If no completed result exists, the button is grayed
out and cannot be clicked.

The same result-discovery logic still restores the guide shapes and result path
status automatically.


## v0.10.30 — Incremental guided-surface refresh

A completed AutoLens track no longer needs a full rerun when guide shapes
change.

Workflow:

```text
Completed track
    ↓
Add / edit a guide shape
    ↓
Import Result
    ↓
AutoLens refreshes guided geometry only
    ↓
Updated guided surface imports
```

The lightweight refresh reuses the existing:

```text
Lens solve
Camera track
Sparse 3D points
Dense proxy mesh
Mesh-fit planes
```

and skips:

```text
Feature extraction
Feature matching
Sparse reconstruction
Bundle adjustment
Image undistortion
PatchMatch
Stereo fusion
Meshing
Mesh simplification
```

Both Import Result buttons automatically export the current internal AutoLens
Roto guides first.

The engine's new:

```text
--guides-only
```

mode then updates:

```text
autolens_guided_surfaces.obj
autolens_guided_surfaces.json
autolens_result.json
AutoLens_v08.nk
```

The guide JSON is SHA-256 hashed. If the guide set is unchanged, the engine
recognizes that the existing guided geometry is already current and skips the
refit.

This allows repeated artist iterations on guided planes without paying for
another camera or dense solve.


## v0.10.30 — Pixel aspect ratio support

The workspace now honors Nuke pixel aspect. Non-square-pixel sources are automatically rendered to a cached square-pixel/desqueezed solve proxy before COLMAP; solved intrinsics are then converted back into the original Nuke display space and the result NK preserves the original resolution and PAR.


## v0.10.30 — Tracking masks, safer guide depth, upright world

### Tracking exclusion masks

The AutoLens workspace now includes a **Masks** tab and a **Mask** toolbar
button.

The mask is stored privately inside the AutoLens Group as:

```text
AUTOLENS_TRACK_MASK_INTERNAL
```

Use **Open Mask Roto** to draw closed shapes around moving actors, cars,
reflections, screens, foliage, or anything else that should not contribute
camera-tracking features.

Masks may be animated or tracked with normal Nuke Roto tools.

Before a solve AutoLens renders a white/black mask sequence matching the exact
COLMAP solve images:

```text
white = use features
black = ignore features
```

and passes the folder to:

```text
--ImageReader.mask_path
```

If the mask shapes change, AutoLens automatically invalidates the sparse/dense
tracking cache because feature extraction must be rebuilt.

### Guided planes prefer real sparse evidence

Older guided-plane fitting preferred the nearest proxy-mesh ray hit. A bad
dense triangle bridging foreground objects could therefore behave like an
"invisible wall" and pull a floor guide toward the camera.

v0.10.30 reverses the priority:

```text
1. Sparse COLMAP points observed inside the guide
2. Compatible proxy-mesh hits that agree with that plane
3. Proxy mesh alone only as a fallback
```

This makes user-drawn floor/wall planes much less sensitive to phantom proxy
geometry.

### Auto Upright World

COLMAP's world coordinate system is arbitrary. v0.10.30 automatically measures
the solved cameras' average local up direction and rotates the exported world so:

```text
+Y = up
```

The same global rotation is applied to:

```text
Camera animation
Sparse 3D points
Dense proxy mesh
Mesh-fit planes
Guided surfaces
```

so reprojection remains unchanged while the Nuke 3D scene is much easier to
work in.

The behavior is enabled by default and can be disabled under **Advanced ->
Auto upright world**.


## v0.10.30 — Viewer navigation polish

### F = Fit

Press:

```text
F
```

anywhere in the AutoLens workspace to reset the plate viewer to Fit.

The shortcut deliberately does not fire while typing into a text or numeric
field.

### Cursor-centered mouse-wheel zoom

Mouse-wheel zoom now anchors to the point under the cursor.

Previously:

```text
wheel -> zoom toward image center
```

Now:

```text
place cursor over detail
        ↓
mouse wheel
        ↓
that detail stays under the cursor
```

The zoom calculation adjusts pan at the same time as zoom, so repeated wheel
events behave like a normal DCC/image viewer rather than pulling the image back
toward its center.

The viewer hint now reads:

```text
F: fit • Mouse wheel: zoom at cursor • Middle-drag: pan
```


## v0.10.30 — Engine runtime-preflight fix

v0.10.8–v0.10.10 referenced the pixel-aspect conversion helpers
`source_format_from_args()` and `camera_to_nuke_display_space()` but the helper
definitions were accidentally omitted from the packaged engine file.

This caused a solve to fail after sparse SfM at:

```text
name 'source_format_from_args' is not defined
```

v0.10.30 restores both helpers and adds an instantaneous engine runtime
preflight immediately after argument parsing. Missing packaged helpers will now
fail before feature extraction/matching/SfM begins instead of wasting minutes
of compute.

Existing `sparse` / `sparse_global` reconstructions remain reusable. Running
v0.10.30 in Resume mode will discover a valid sparse reconstruction even when
the previous run failed before writing `autolens_cache.json`, so feature
extraction, matching, and camera reconstruction do not need to be repeated.


## v0.10.30 — Visible version + background solve monitor

### Version always visible

The top-left AutoLens workspace header now shows:

```text
AutoLens v0.10.30
```

The full window title and compact background monitor also include the version.

### Background solving

The long AutoLens engine/COLMAP process already runs through Qt `QProcess`
rather than blocking Nuke's UI thread.

While a solve is active, the Solve tab now enables:

```text
Background
```

Pressing it hides the large AutoLens workspace and opens a compact non-modal
progress palette containing:

```text
Current stage
Progress %
Open AutoLens
Cancel Solve
Hide
```

The AutoLens workspace remains alive invisibly because it owns the running
`QProcess`.

### Minimize behavior

Minimizing the full AutoLens window while a solve is running automatically
switches to the compact background monitor.

Closing the full window during a solve now asks whether to move the job to the
background. It no longer destroys the dialog/QProcess while claiming the solve
will continue.

When a background solve finishes, the compact monitor comes forward and shows a
green Complete or red Failed status.

### Nuke responsiveness

The external Python/COLMAP solve is asynchronous, so the artist can continue
using Nuke while it runs.

Some setup work that must execute inside Nuke itself — for example rendering a
square-pixel solve proxy for non-1.0 PAR footage — can still occupy Nuke briefly
before the external solve starts.


## v0.10.30 — DeadCow VFX branding

The AutoLens workspace title now reads:

```text
AutoLens v0.10.30 - DeadCow VFX
```

The compact background-solve window uses the same title.

The top-left workspace header also includes a subtle `DeadCow VFX` brand mark
next to the AutoLens version.


## v0.10.30 — Context-aware Run / Resume button

The main solve button now communicates whether AutoLens has actually found a
reusable track cache.

When:

```text
Run mode = Resume / reuse cached stages
```

and the current output folder contains a reusable sparse reconstruction, the
button changes from:

```text
Run AutoLens
```

to:

```text
Resume AutoLens
```

If Resume mode is selected but no reusable cache is detected, the button stays:

```text
Run AutoLens
```

`Rebuild dense only` and `Rebuild everything` always display `Run AutoLens`.

The label refreshes immediately when:

```text
Output folder changes
Run mode changes
A solve finishes and creates cache
A completed-result state is refreshed
```

Cache detection checks both `autolens_cache.json` and the actual
`sparse`/`sparse_global` reconstruction folders. This also recognizes a
successful sparse solve from a previous run that crashed before its JSON cache
was written.


## v0.10.30 — Read-relative default output folders

New AutoLens tracks now derive their default output folder exclusively from the
upstream source Read.

Example:

```text
Read:
D:\Projects\Show\Shot020\plates\plate.%04d.exr

Default AutoLens output:
D:\Projects\Show\Shot020\plates\AutoLens\v1
```

The location of the current `.nk` script no longer affects this default.

This behavior applies when:

```text
Creating a new AutoLens controller
Selecting / changing the source Read in Setup / Run
```

Existing AutoLens nodes continue to preserve an output folder that is already
stored on the controller. AutoLens does not silently relocate an established
track when its dialog is reopened.


## v0.10.30 — Proxy bridge cleanup + conservative guide depth

### Automatic proxy bridge cleanup

AutoLens now runs a fast topology cleanup after COLMAP mesh simplification.

The cleanup removes triangles that are suspicious relative to the local mesh
scale, especially:

```text
very long edges connecting otherwise dense local neighborhoods
extremely skinny / needle-like spanning triangles
degenerate / invalid triangles
```

The cleaned proxy is written as:

```text
dense/meshed-proxy-clean.ply
```

and becomes the proxy used by Nuke, mesh-fit extraction, and guided surfaces.

The original simplified mesh is preserved as:

```text
dense/meshed-proxy.ply
```

Diagnostics are written to:

```text
dense/autolens_proxy_cleanup.json
```

The cleanup pass can be added to an existing cached dense reconstruction
without rerunning PatchMatch, stereo fusion, Delaunay meshing, or the camera
solve.

A safety valve falls back to the original simplified proxy if cleanup would
remove more than 45% of a substantial mesh.

### More conservative guided-surface depth

Proxy ray hits are no longer accepted only because they are the nearest mesh
intersection.

AutoLens now:

```text
1. Collects sparse 3D points observed inside the guide.
2. Uses sparse-point depth as a conservative foreground sanity check.
3. Rejects proxy hits dramatically closer than the sparse depth distribution.
4. Fits the sparse plane first when possible.
5. For each proxy ray, compares the hit depth to where that same ray should
   intersect the sparse plane.
6. Rejects a proxy sheet substantially in front of that expected plane.
7. Uses surviving proxy hits only as refinement evidence.
```

When there are fewer than three sparse points, AutoLens also rejects extreme
near-depth outliers relative to the other proxy hits inside the guide.

The guided-surface JSON report now records:

```text
proxy_ray_hits_raw
proxy_ray_hits_accepted
proxy_hits_rejected_too_close
proxy_hits_rejected_plane_depth
```

The intended failure mode is now conservative: when 3D support is ambiguous,
it is preferable for a guide to report `insufficient_3d_support` rather than
create a confidently wrong floating plane.


## v0.10.30 — depth.Z output channel

AutoLens now carries a depth pass on the imported/final image stream.

Implementation:

```text
ScanlineRender (solved geometry render)
        ↓
Copy depth.Z onto merged undistorted comp
        ↓
Redistort final image + depth together
```

This gives the artist a directly accessible channel downstream, for example:

```text
AutoLens final output
    ↓
Shuffle / Shuffle2
    layer: depth
    channel: Z
```

The depth pass is generated from the rendered solved geometry branch
(guided surfaces / mesh-fit planes / proxy, whichever is active in the Scene),
not from sparse-only metadata.

The compact import graph now includes:

```text
AUTOLENS_SCANLINE   # depth-producing geometry render
AUTOLENS_DEPTH_COPY # injects depth.Z into the main image stream
```

The final AutoLens stream is intended to expose `depth.Z` for ZDefocus, fog,
atmospherics, grading keys, and other depth-driven comp tasks.


## v0.10.30 — Smart dense full-sequence depth.Z

AutoLens now converts its smart-selected native COLMAP PatchMatch depth frames
into a complete timeline depth sequence.

Pipeline:

```text
smart dense reference selection
-> native PatchMatch depth maps
-> sample depth pixels into camera-space 3D
-> solved animated AutoLens camera
-> reproject previous/next dense keys into every timeline frame
-> z-buffer competing samples
-> small sampling-hole repair
-> depth/autolens_depth.####.exr
```

Each EXR contains `depth.Z` plus `alpha`. Alpha is 1 only where propagated
dense depth exists.

The Nuke import uses the propagated depth wherever alpha is valid and uses
ScanlineRender depth from the active AutoLens geometry only for remaining
holes/disocclusions.

Existing dense caches can generate this sequence without rerunning PatchMatch,
stereo fusion, meshing, or the camera solve, provided the cached native COLMAP
depth maps still exist.


## v0.10.30 — Clean imports + guided surface asset creation

- Removed the automatic depth.Z/shuffle branch and standard propagated-depth generation.
- Removed the AutoLens_Info backdrop. Import Result now imports nodes only.
- Guided surfaces now receive independent planar 0-1 UV projections.
- Every successful guided surface is also exported as its own OBJ under:
  `guided_surfaces/<SurfaceName>.obj`
- The AutoLens properties panel now has a CameraTracker-style:
  `Create from solve` dropdown and `Create` button.
- Create options include Camera, Undistort, Redistort, Proxy Mesh,
  Mesh-fit Planes, combined Guided Surfaces, and every individual guided surface.


## v0.10.30 — Create-from-solve fixes

- Fixed Camera creation. The v0.10.19 extractor could start inside Camera2's nested matrix block, which caused `win_translate: Unknown command`. v0.10.30 extracts the complete top-level Camera2 node.
- The Create-from-solve menu now discovers `<output>/guided_surfaces/*.obj` directly. Existing cached Surface_XX OBJ files therefore appear without re-solving.
- `autolens_guided_surfaces.json` is now optional metadata for friendlier display names rather than a requirement for listing individual surfaces.


## v0.10.30 — Guided asset menu refresh

The Create-from-solve menu now refreshes directly from disk every time the
AutoLens properties panel opens (`showPanel`), so existing AutoLens nodes no
longer keep a stale six-item dropdown after a plugin update.

Individual guided surface discovery now checks:

```text
<output>/guided_surfaces/*.obj
<output>/Surface_*.obj
<output>/Guide_*.obj
autolens_guided_surfaces.json obj_file references
```

The physical OBJ files are authoritative; the JSON report is used only for
friendlier display names.

The Create button also performs one final disk rescan before creating an item.


## v0.10.30 — Solve version manager

AutoLens now separates the solve destination into:

```text
Output root:    <Read folder>/AutoLens
Solve version:  Latest (v2)
```

Version folders matching `v1`, `v2`, `v3`, etc. are detected automatically.

The dropdown includes:

```text
Latest (vN)
v1
v2
...
Create New (vN+1)
```

The selected version now controls:

- result availability/status
- Resume / reuse cached stages
- Import Result
- Create from solve
- guided-surface restoration
- guides-only refresh
- the actual engine output folder

New AutoLens nodes default to the upstream Read folder's `AutoLens` root.
If versions already exist, Latest is selected. If none exist, Create New (v1)
is selected.

The legacy `autolens_output` knob is retained as a hidden resolved path for
engine compatibility.


## v0.10.30 — Version-selection state sync

### Setup / Run honors the selected solve version

Opening Setup / Run on an existing AutoLens node no longer resets an explicit
version choice to Latest.

The dialog now captures the controller's selected Output root + Solve version
before initializing itself from the upstream Read, then restores that exact
selection before result/cache state is loaded.

Example:

```text
Properties:
Solve version = v1

Setup / Run:
Solve version = v1
```

It will no longer silently jump to v2 merely because v2 is the latest folder.

### Solve-version changes refresh solved assets

Changing v1/v2/etc now immediately refreshes:

- Completed Result Available
- Resume / Run state
- Create from solve menu
- individual guided-surface entries
- result/import paths
- guide restoration context

The Create-from-solve enum is force-rebuilt on solve-version changes because
some Nuke versions visually cache Enumeration_Knob menu entries even after
`setValues()`.


## v0.10.30 — Immediate solve-version UI refresh

Changing the Solve version dropdown now schedules the version-dependent UI
refresh for the next Qt event-loop turn.

This specifically addresses Nuke's behavior where rebuilding an
`Enumeration_Knob` while another enum's `knobChanged` event is still active can
leave the visible menu cached until the properties panel is reopened.

After selecting v1 / v2 / Latest, AutoLens now immediately refreshes:

- Completed Result Available
- Import Result availability
- Create from solve choices
- individual guided surface entries
- resolved output/result context

No Setup / Run open-close cycle should be required.


## v0.10.30 — Mocha-style timeline + external alpha tracking mask

### Viewer timeline

The AutoLens workspace now includes a compact timeline below the image viewer.

- click/drag to change the current Nuke frame
- blue markers show AutoLens guide frames
- orange playhead shows the current frame
- frame range follows the upstream Read

The current guide system stores one active guide frame, so current tracks show
one blue marker. The timeline widget is structured to support additional guide
markers later.

### Tracking mask now uses AutoLens input 2

The old private Mask Roto workflow has been retired from the main UI.

AutoLens now exposes:

```text
Input 1: Source plate
Input 2: Tracking Mask Alpha
```

Connect any Nuke matte/alpha-producing node to input 2.

Default meaning:

```text
alpha = AVOID / EXCLUDE from feature tracking
```

Masks are converted automatically to COLMAP's convention:

```text
white = track
black = ignore
```

### Masks tab

The Masks tab keeps the same integrated AutoLens styling and now:

- reports the connected alpha source
- previews the current frame's alpha as a 20% red overlay in the main viewer
- includes an Invert button

Invert changes the meaning to:

```text
alpha = KEEP / TRACK
outside alpha = AVOID
```

The red preview always shows where the connected alpha exists, while the mode
label states how AutoLens will interpret it.

### Cleaner toolbar

Removed duplicate top-right workspace buttons:

```text
Refresh Frame
Auto Suggest
+ Plane
Mask
Delete
Open Roto
```

Surface editing controls remain in the Surfaces tab. Fit remains in the top
toolbar.


## v0.10.30 — silent mask preview + mask-only viewer mode

### No render-progress popup while scrubbing

The Masks viewer no longer creates a temporary Write and calls `nuke.execute()`
for every frame.

Instead, AutoLens samples the connected mask node's alpha directly at a modest
preview resolution and builds the red Qt overlay in memory. This keeps timeline
scrubbing interactive and avoids Nuke's one-frame Render Progress dialog.

The full-resolution mask used by COLMAP during an actual AutoLens solve is
unchanged and is still rendered at solve time.

### Invert now changes the red visualization

The red overlay now always means:

```text
RED = AutoLens will AVOID / EXCLUDE this region
```

Therefore:

```text
normal mode: alpha     -> red
invert mode: 1 - alpha -> red
```

This makes the tracking interpretation visually obvious rather than showing
the same red alpha regardless of Invert state.

### Masks tab hides Surface guides

While the Masks tab is active, AutoLens hides all surface polygons, handles,
and labels from the main viewer. The viewer contains only:

```text
plate
+ tracking mask exclusion overlay
```

Switching back to Surfaces restores the guide overlays.


## v0.10.30 — Completed solve = 100% progress

When the currently selected solve version has a completed AutoLens result,
the Solve-page progress bar now displays:

```text
100%
```

This is a persistent visual cue that the selected v1/v2/etc already contains
a finished solve.

When switching to a version without a completed result, the idle progress bar
returns to 0%. While an actual solve is running, live progress continues to
control the bar normally.


## v0.10.30 — Completed result progress fix

The Solve-page progress bar is now driven directly by the same result-state
condition that displays:

```text
Completed Result Available
```

This fixes the case where the green completed status was visible but the bar
still showed 0%.

When idle:

```text
Completed Result Available -> 100%
No completed result        -> 0%
```

An active AutoLens run still owns live progress normally.


## v0.10.30 — Full-screen workspace + wider properties + progress sync

### Opens maximized

AutoLens now opens as a maximized workspace by default.

This applies to:
- a newly opened AutoLens workspace
- reopening an already-open workspace
- returning from the compact Background monitor

### Wider right-side properties panel

The right sidebar now has:
- minimum width: 460 px
- preferred width: ~500 px
- maximum width: 680 px

The initial splitter is reapplied after maximize so Qt does not preserve the
older narrow constructor-time ratio.

### Completed progress bar fix

`restore_result_state()` now sets the progress bar directly from the same
`bool(completed_result_path)` used by the green completed-result status.

The sync is applied both:
- immediately after the result is discovered
- again after guide/status restoration completes

So:

```text
Completed Result Available -> 100%
No Completed Result        -> 0%
```

while idle.


## v0.10.30 — per-surface guide frames + guide-frame viewer state

### Each surface now has its own guide frame

Surface guides are no longer forced to share one global frame.

Examples:

```text
Surface_01 -> frame 523
Surface_02 -> frame 523
Surface_03 -> frame 579
Surface_04 -> frame 612
```

This is useful when the camera pans or reveals new parts of the set. An artist
can place a guide on one side of the room early in the shot, move to a later
frame, add another surface that only becomes visible there, and solve both into
the same reconstructed world.

`Add Plane` automatically assigns the newly created plane to the current frame.

`Set Guide Frame` now changes only the selected surface. Other surfaces retain
their existing guide frames.

The timeline displays blue marks for every distinct surface guide frame.

### Viewer state

A surface is now:

```text
green      = current frame is that surface's guide frame
gray/white = current frame is not that surface's guide frame
```

Surface fills are substantially more transparent so the plate remains readable.

The surface list also shows each stored frame:

```text
Surface_03   •   F579
```

### Solve/export behavior

The private AutoLens Roto node stores a small JSON map of surface-name to guide
frame. Legacy scripts without that map automatically inherit their existing
single Guide frame.

`autolens_surface_guides.json` now stores `guide_frame`, `frame`, and
`source_image` per surface.

The engine resolves a separate solved camera pose and sparse-observation set for
each surface before fitting it into 3D. Legacy single-frame guide JSON remains
supported through the top-level frame fields.
