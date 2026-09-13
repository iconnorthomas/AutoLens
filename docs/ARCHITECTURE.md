# AutoLens Technical Architecture

## Components

### Nuke Controller
The AutoLens root node is a Nuke Group used as a lightweight controller and pass-through image node.

Inputs:
- Input 0: source plate
- Input 1: tracking mask alpha

Responsibilities:
- store output root / solve version
- hold mask invert state
- launch Setup / Run workspace
- expose result import / create controls
- preserve private guide metadata

### Workspace UI
Qt/PySide dialog containing:
- large image viewer
- surfaces tab
- masks tab
- solve tab
- timeline
- background solve monitor

### Engine
Standalone Python process responsible for:
- COLMAP orchestration
- sparse/dense reconstruction
- lens conversion
- proxy cleanup
- guided-surface fitting
- output generation

### Managed Runtime
AutoLens can bootstrap its own Miniforge/Conda environment and managed COLMAP install under the Nuke plugin directory.

## Coordinate Systems

Nuke:
- +X right
- +Y up
- camera looks toward -Z

COLMAP camera:
- +X right
- +Y down
- +Z forward

Basis conversion:
```text
diag(1, -1, -1)
```

## Guide Pipeline

2D guide point:
```text
distorted Nuke raster coordinate
→ lens inverse
→ undistorted camera ray
→ surface evidence
→ robust plane fit
→ ray/plane intersection
→ world-space planar polygon
```

## Surface Evidence Priority

```text
sparse tracked 3D observations
        ↓
robust plane fit
        ↓
dense proxy consistency check
        ↓
proxy-only fallback if sparse evidence insufficient
```

This ordering intentionally avoids trusting noisy photogrammetry geometry more than tracked sparse evidence.

## Per-Surface Guide Frame

Each surface carries:
```text
name
guide_frame
source_image
2D polygon
```

The engine resolves:
```text
surface guide frame
→ registered solved camera pose
→ observations for that exact view
→ independent 3D plane fit
```

This allows a single shot to contain guide planes authored across multiple viewpoints.

## Lens Conversion

COLMAP RADIAL:
```text
rd = ru * (1 + k1*r² + k2*r⁴)
```

Nuke Custom Radial Standard rational fit:
```text
ru = rd * (1+n0*r²+n1*r⁴)/(1+d0*r²+d1*r⁴)
```

Nuke radial normalization:
```text
max(width, height) / 2
```

## Caching

AutoLens caches expensive outputs by solve version.

A resume run attempts to reuse:
- feature DB
- matcher output
- sparse solve
- dense workspace
- fused geometry
- generated meshes

Guide-only refresh can bypass camera / dense stages and rebuild only guided geometry.

## Failure Philosophy

AutoLens favors:
- warnings over hard failure for non-critical guide issues
- graceful CPU fallback when GPU BA is unavailable
- explicit result state
- persistent logs
- resumable stages
