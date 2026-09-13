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

## 3. Open Setup / Run
Choose:
- output root
- solve version
- quality
- run mode
- GPU feature extraction/matching
- whether to import result automatically

## 4. Place surfaces
In the Surfaces tab:
- move to a useful frame
- Add Plane
- drag corners
- rename
- Set Guide Frame

Each surface can be authored on a different frame.

## 5. Run
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

## 6. Import
Import all nodes, or use Create From Solve for individual assets.

## 7. Composite
Work in undistorted space.

## 8. Deliver
Redistort after VFX.
