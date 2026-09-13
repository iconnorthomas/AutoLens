# I Built a Nuke Tool That Solves the Lens, Camera, and Rough 3D Scene From the Shot Itself

One of the recurring problems I run into in visual effects is that a “camera solve” often gives you only part of what you actually need.

You may get a camera. You may get a point cloud. You may even get a decent reprojection error. But then the real work starts: estimating lens distortion, building rough set geometry, deciding where projection cards should live, figuring out how to undistort and redistort cleanly, and turning that solve into something another compositor can actually use.

That is the problem I have been trying to solve with **AutoLens**, an experimental NukeX tool I have been developing for DeadCow VFX.

The goal is simple:

> Start with the photographed image sequence and generate as much useful VFX scene information as possible — lens distortion, camera motion, sparse 3D structure, dense proxy geometry, projection surfaces, and a clean Nuke handoff.

## Why I Wanted a Different Workflow

Traditional camera tracking is usually treated as one isolated task.

You solve the camera, export it, and then manually build everything around it.

But in production, these problems are tightly connected.

Lens distortion affects feature tracks.

Feature tracks affect camera motion.

Camera motion affects the 3D reconstruction.

The reconstruction can tell us where walls, floors, counters, and other surfaces exist.

Those surfaces are exactly what we need for paint projection, cleanup, CG integration, set extension, and object replacement.

So instead of treating those as separate jobs, AutoLens tries to solve them as one connected workflow.

## The Full AutoLens Pipeline

The current process looks roughly like this:

**1. Feature extraction and matching**

AutoLens uses COLMAP to find repeatable image features across the sequence and establish correspondence between frames.

**2. Sparse Structure-from-Motion**

Those tracks are used to estimate the moving camera and a sparse 3D point cloud.

The camera intrinsics and radial lens distortion are shared across the shot so the solve can use the motion itself to help estimate the lens.

**3. Bundle adjustment**

The camera poses, focal length, distortion parameters, and 3D points are jointly refined.

**4. Smart dense-frame selection**

Dense stereo is expensive, so AutoLens does not blindly reconstruct every frame.

Instead, it looks at the solved camera motion and chooses a smaller group of useful viewpoints — for example, around 12 dense reference frames in a 120-frame shot.

**5. PatchMatch stereo**

Those selected frames are processed through dense stereo to estimate much more detailed scene depth.

This is generally the most expensive part of the process.

**6. Dense fusion and proxy geometry**

The dense results are fused into a rough 3D reconstruction.

The goal is not to create a production-ready model.

The goal is to create useful spatial context: where are the walls, floor, counters, objects, and major depth transitions?

**7. Proxy cleanup**

Photogrammetry sometimes creates large triangles bridging unrelated pieces of geometry.

AutoLens automatically tries to remove those spanning triangles because even one incorrect polygon can cause a projection surface to land in completely the wrong place.

**8. Automatic plane extraction**

Large planar areas can be simplified into useful scene surfaces.

**9. Artist-guided surfaces**

This is the part I find especially useful.

The artist can draw a simple quadrilateral or polygon over a surface in the Nuke viewer — for example, a wall or countertop.

AutoLens looks at the sparse 3D tracks inside that shape, fits a robust plane, checks it against the dense reconstruction, and turns the 2D drawing into a real 3D surface.

## Using Different Frames for Different Surfaces

A major addition in the latest build is that every guided surface can have its own guide frame.

That matters because a camera move may reveal different parts of the environment at different times.

For example:

- a wall may be clearly visible on frame 523
- a counter may only be fully visible on frame 579
- another side of the room may not appear until frame 612

Instead of forcing every guide to come from the same frame, AutoLens now stores the appropriate frame for each surface and uses the solved camera from that specific viewpoint when fitting it into the 3D world.

The timeline shows blue markers for every guide frame.

When you are on a surface’s actual guide frame, it appears green.

On other frames it fades to gray/white so you can still see it as reference without mistaking the current view for the frame where it was authored.

## Why Sparse Evidence Matters

One of the more interesting problems I ran into was dense geometry that looked plausible at first glance but contained large incorrect surfaces.

A bad triangle could bridge the refrigerator and counter, for example.

If a guide ray hit that triangle first, the resulting surface would appear floating in front of the actual set.

The fix was to make guided-surface fitting more conservative.

AutoLens now prefers tracked sparse 3D observations inside the guide first.

Those sparse points come from features that were actually observed across multiple frames.

The dense mesh is then used as supporting evidence rather than absolute truth.

This has been much more stable.

## Lens Distortion Without a Grid

AutoLens also estimates radial lens distortion directly from the moving shot.

A real lens grid is still the ideal source when one exists, but that is not always the reality of production.

Sometimes:

- no grid was shot
- metadata is missing
- the VFX team receives material late
- the exact lens is unknown

In those situations, solving shared intrinsics from the photography can provide a very useful fallback.

The resulting distortion is converted into Nuke’s native LensDistortion setup so the artist can work in the familiar:

**Distorted source → Undistort → VFX work → Redistort → Delivery**

workflow.

## Tracking Masks

Moving actors and objects are another common problem.

AutoLens now accepts a normal alpha input directly on the node, similar to Nuke’s CameraTracker.

By default, the alpha tells AutoLens which region to avoid during tracking.

It can also be inverted so the matte defines the only region that should be tracked.

Inside the AutoLens workspace the exclusion region is shown as a translucent red overlay.

## What This Is Useful For

The system is most useful when the scene is mostly static and the VFX problem depends on understanding the photographed environment.

Examples include:

- cleanup and object removal
- paint projection
- set extensions
- signage replacement
- CG integration
- screen replacement
- adding atmospheric elements at correct depth
- rebuilding enough environment geometry for occlusion and projection
- shots where the lens grid is unavailable

## What It Does Not Solve

This is still photogrammetry.

It can struggle with:

- reflections
- transparent surfaces
- heavy motion blur
- textureless walls
- repeating patterns
- foliage
- moving foreground subjects
- extreme shallow depth of field

The intent is not to remove artists from the process.

The intent is to give the artist a much better starting point.

## The Bigger Idea

What interests me most about this project is the idea that camera tracking can become something broader than just estimating a transform.

A moving camera contains information about the scene.

The lens, camera, sparse tracks, dense depth, proxy geometry, and artist input can all reinforce each other.

For a compositor, that means the end result can be more than:

> “Here is your solved camera.”

It can become:

> “Here is the camera, lens, rough set, useful projection geometry, and the Nuke setup you need to start the shot.”

That is the direction I am continuing to push AutoLens.

— **Connor Thomas / DeadCow VFX**
