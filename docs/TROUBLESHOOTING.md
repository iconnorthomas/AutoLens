# Troubleshooting

## A solve failed
When a solve fails, the Solve tab names the problem and says what to do next, for
example *The camera could not be solved* or *Ran out of GPU or system memory*. The
engine's own error line appears under *Engine message*, and the full COLMAP output
is still in the log. Run mode Resume reuses every stage that already finished.

## Managed COLMAP shows CUDA BA warning
If COLMAP reports Ceres was compiled without cuDSS, bundle adjustment may fall back to CPU. This does not necessarily indicate failure.

## Solve restarts instead of resuming
Verify:
- selected solve version
- output root
- cached folder exists
- Run Mode = Resume / reuse cached stages (under Advanced on the Solve tab)

## Guided surface floats in front of scene
Possible causes:
- bad proxy triangle
- insufficient sparse points inside guide
- incorrect guide frame
- moving foreground object contaminating the solve

Try:
- tracking mask
- different guide frame
- tighter guide shape
- newer proxy cleanup
- more useful camera baseline

## Surface list differs between v1 and v2
Create From Solve is version-specific. Verify the selected Solve version.

## Non-square pixel footage looks squeezed
Use a build with PAR-aware solve proxies. AutoLens should create a square-pixel solve representation while maintaining original Nuke display space.

## Nuke UI feels slow during dense solve
PatchMatch can saturate the GPU even though the solver runs in the background.
