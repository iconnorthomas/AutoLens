# Production Workflow

## 1. Connect the plate
Create an AutoLens node and connect the source Read.

## 2. Optional tracking mask
Connect a matte to AutoLens input 1.

Default:
```text
alpha = avoid
```

Invert:
```text
alpha = keep
```

## 3. Open AutoLens...
Click **Open AutoLens...** at the top of the node's properties. The Solve tab shows
where you are: *1. Setup › 2. Solve › 3. Review › 4. Create*, plus a hint for the
next step.

These are filled in for you, so you can usually leave them alone:
- image folder (from the connected Read)
- output root (`<plate folder>/AutoLens`)
- solve version (Latest, or v1 for a new shot)

Choose a **Quality**:
- Preview: fastest, for checking that a shot tracks
- Normal: recommended
- High: densest geometry, slowest

Run mode (Resume / Rebuild dense only / Rebuild everything), GPU feature
extraction and the dense settings are under **Advanced**.

## 4. Place surfaces
In the Surfaces tab:
- move to a useful frame
- Add Plane
- drag corners
- rename
- Set Guide Frame

Each surface can be authored on a different frame.

## 5. Solve
Click **Solve**. Progress reads like *Step 8 of 11: Computing dense depth maps ·
12m 03s elapsed · about 6 min left*. If the solve fails, the panel says what went
wrong and what to do next. If the selected version already holds a solve,
AutoLens offers to solve into a new version instead.

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

## 6. Create
**Create All** (formerly Import Result) builds the camera, the undistort/redistort
lens nodes and all geometry. It runs automatically when *Create camera, lens and
geometry in the comp when the solve finishes* is ticked. Use **Create from solve**
to create single assets.

## 7. Composite
Work in undistorted space.

## 8. Deliver
Redistort after VFX.
