"""AutoLens frame server: decodes EXR/DPX plate frames for the workspace viewer.

Started by autolens/frame_server.py with AutoLens's managed Python (numpy +
OpenEXR), several at once. Speaks autolens/frame_protocol.py on stdin/stdout:

    {"cmd": "hello"}                          -> {"ok", "error", "python"}
    {"cmd": "decode", "path", "max_w",        -> {"ok", "width", "height",
     "pixel_aspect", "transfer"}                  "src_width", "src_height"}
                                                 + width*height*3 bytes of RGB
    {"cmd": "quit"}                           -> exits

Errors come back as {"ok": false, "error": "..."}; the worker keeps serving.
Nothing is written to disk.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import frame_protocol as fp  # noqa: E402


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    # Anything a library prints must not corrupt the protocol stream.
    sys.stdout = sys.stderr

    try:
        import numpy  # noqa: F401
        import OpenEXR  # noqa: F401
        import frame_decode as fd
        missing = None
    except Exception as exc:          # managed Python without numpy/OpenEXR
        fd = None
        missing = "%s: %s" % (type(exc).__name__, exc)

    while True:
        try:
            request, _ = fp.recv(stdin)
        except EOFError:
            return 0
        rid = request.get("id")
        cmd = request.get("cmd", "decode")
        if cmd == "quit":
            return 0
        if cmd == "hello":
            fp.send(stdout, {
                "id": rid,
                "ok": missing is None,
                "error": missing,
                "python": sys.version.split()[0],
            })
            continue
        if missing:
            fp.send(stdout, {
                "id": rid,
                "ok": False,
                "error": "the frame server needs numpy and OpenEXR (%s)" % missing,
            })
            continue
        try:
            pixels, (src_w, src_h) = fd.decode_file(
                request["path"],
                int(request.get("max_w") or 0),
                float(request.get("pixel_aspect") or 1.0),
                request.get("transfer") or "linear",
            )
            height, width = pixels.shape[:2]
            fp.send(stdout, {
                "id": rid,
                "ok": True,
                "width": int(width),
                "height": int(height),
                "src_width": int(src_w),
                "src_height": int(src_h),
            }, pixels.tobytes())
        except Exception as exc:
            fp.send(stdout, {
                "id": rid,
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
            })


if __name__ == "__main__":
    sys.exit(main())
