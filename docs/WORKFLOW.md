# Production Workflow

The workspace follows three steps, shown as pills in its top bar:

```text
Track  ›  Draw  ›  Output
```

The side panel only shows the current step. Every other action is in the top
menus (File, Track, Draw, Output, View), the quality popover next to **Track**,
or **Solve settings…**. Keys `1`, `2` and `3` switch steps.

## 1. Connect the plate
Create an AutoLens node and connect the source Read. Optional: connect a
tracking matte to the TrackingMask input (input 1). By default alpha means
*avoid*; **Invert** on the Track step makes it mean *keep*.

## 2. Open AutoLens...
Click **Open AutoLens...** in the node's properties. The node's main tab holds
only that, the status line and **Create All**. Quality, output folder, solve
version and single-asset creation are on the *AutoLens Advanced* tab.

The plate, output root (`<plate folder>/AutoLens`) and solve version are filled
in for you. Change them in **File › Solve settings…**.

## 3. Track
The plate plays right away (Space, or the transport under the viewer). With a
mask connected, the red overlay shows what tracking will avoid.

Click **Track** (or press `T`, or the track button in the transport). The
quality popover next to it sets:
- Preview: fastest, for checking that a shot tracks
- Normal: recommended
- High: densest geometry, slowest
- *Build geometry during the track*: off tracks only the camera and lens (much faster) and leaves geometry for Output
- *Create All when the track finishes*

Progress shows in the Track panel and the status bar, and the chip over the
viewer reads *TRACKING 42%*, then *SOLVED · 0.29 px* (the mean reprojection error).
If the track fails, the panel says what went wrong and what to do next.

## 4. Draw (optional)
When the track lands, AutoLens moves to Draw. Draw planes over the flat surfaces
you want as projection geometry:
- **Add plane** (`P`) and drag its corners
- **Auto suggest** guesses floor, wall and object planes on the current frame
- **Set guide frame** (`G`) marks the frame the selected surface was drawn on
- type a name and press Enter to rename; `Del` deletes

Each surface can have its own guide frame. They show as blue marks in the
timeline's *Guides* lane. Surfaces are green on their own guide frame and grey
on other frames. You can also draw before tracking: the track then fits the
surfaces directly.

## 5. Output
- **Create All** builds the camera, the undistort/redistort lens nodes and all geometry in the comp. Click it again after editing surfaces.
- **Create one** creates a single asset: Camera, Undistort, Redistort, Proxy Mesh, Mesh-fit Planes, the combined guided surfaces, or one guided surface.
- The *Geometry* card shows what's built. **Build geometry** runs only the geometry stages, reusing the tracked camera.

AutoLens performs:
- feature extraction
- matching
- camera solve
- bundle adjustment
- dense frame selection
- PatchMatch
- fusion
- mesh cleanup
- plane extraction
- guide fitting
- Nuke export

## 6. Composite
Work in undistorted space.

## 7. Deliver
Redistort after VFX.
