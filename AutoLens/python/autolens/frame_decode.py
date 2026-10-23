"""Decode plate frames to viewer-sized 8-bit sRGB, outside Nuke.

Used by the frame-server worker (autolens/frame_worker.py), which runs in
AutoLens's managed Python (numpy + OpenEXR). No Qt, no Nuke, and no package
imports, so the worker can import it as a top-level module. numpy and OpenEXR
are imported only by the functions that touch pixels; everything else here is
plain Python that the tests cover directly.

Colour: values are converted to linear according to the Read's colorspace,
then encoded with the sRGB curve, matching Nuke's default sRGB viewer. Names
that aren't recognised (e.g. OCIO camera log spaces) are treated as linear for
float files and sRGB for integer files, and reported as approximate.
"""
from __future__ import annotations

import os
import re
import struct

WORKER_EXTS = ("exr", "dpx")

TRANSFERS = ("linear", "srgb", "rec709", "gamma1.8", "gamma2.2", "gamma2.4", "cineon")
FLOAT_EXTS = ("exr", "hdr", "pfm")
LOG_EXTS = ("dpx", "cin")

_CINEON_BLACK = 10.0 ** ((95.0 - 685.0) / 300.0)


# ---------------------------------------------------------------------------
# Colour


def transfer_for(colorspace, raw=False, ext=""):
    """(transfer, exact) for a Read's colorspace knob value and file extension.

    ``transfer`` is one of TRANSFERS: how file values become linear light.
    ``exact`` is False when the name wasn't recognised and we guessed.
    """
    ext = (ext or "").lower().lstrip(".")
    if raw:
        # Nuke passes raw data through untouched and the viewer shows it as linear.
        return "linear", True
    name = (colorspace or "").strip().lower()
    inner = re.match(r"^default\s*\((.*)\)\s*$", name)
    if inner:
        name = inner.group(1).strip()
    if name in ("", "default"):
        if ext in FLOAT_EXTS:
            return "linear", True
        if ext in LOG_EXTS:
            return "cineon", True
        return "srgb", True

    compact = name.replace(" ", "").replace("_", "").replace("-", "").replace(".", "")
    if "cineon" in compact or compact in ("log", "adx10", "adx16"):
        return "cineon", True
    if (
        compact.startswith("lin")
        or "linear" in compact
        or "scenelinear" in compact
        or "acescg" in compact
        or "aces2065" in compact
        or compact in ("raw", "data", "utilityraw", "rawdata")
    ):
        return "linear", True
    if "srgb" in compact:
        return "srgb", True
    if "gamma18" in compact:
        return "gamma1.8", True
    if "gamma22" in compact:
        return "gamma2.2", True
    if "gamma24" in compact or "rec1886" in compact or "bt1886" in compact:
        return "gamma2.4", True
    if "rec709" in compact or "bt709" in compact:
        return "rec709", True
    return ("linear" if ext in FLOAT_EXTS else "srgb"), False


def to_linear(x, transfer):
    """One code value (0..1) to linear light."""
    if transfer == "linear":
        return x
    if transfer == "srgb":
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    if transfer == "rec709":
        return x / 4.5 if x < 0.081 else ((x + 0.099) / 1.099) ** (1.0 / 0.45)
    if transfer.startswith("gamma"):
        return max(0.0, x) ** float(transfer[5:])
    if transfer == "cineon":
        return (10.0 ** ((1023.0 * x - 685.0) / 300.0) - _CINEON_BLACK) / (1.0 - _CINEON_BLACK)
    raise ValueError("unknown transfer %r" % transfer)


def srgb_encode(lin):
    """Linear light (clipped to 0..1) to an sRGB display value 0..1."""
    lin = min(1.0, max(0.0, lin))
    return lin * 12.92 if lin <= 0.0031308 else 1.055 * lin ** (1.0 / 2.4) - 0.055


def display_value(x, transfer):
    """Code value -> displayed 8-bit value (the reference the LUT must match)."""
    return int(round(srgb_encode(to_linear(x, transfer)) * 255.0))


_LUTS = {}
LUT_SIZE = 65536


def display_lut(transfer):
    """uint8 numpy LUT over input code values 0..1 (LUT_SIZE steps)."""
    lut = _LUTS.get(transfer)
    if lut is None:
        import numpy as np

        x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float64)
        if transfer == "linear":
            lin = x
        elif transfer == "srgb":
            lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
        elif transfer == "rec709":
            lin = np.where(x < 0.081, x / 4.5, ((x + 0.099) / 1.099) ** (1.0 / 0.45))
        elif transfer.startswith("gamma"):
            lin = x ** float(transfer[5:])
        elif transfer == "cineon":
            lin = (10.0 ** ((1023.0 * x - 685.0) / 300.0) - _CINEON_BLACK) / (1.0 - _CINEON_BLACK)
        else:
            raise ValueError("unknown transfer %r" % transfer)
        lin = np.clip(lin, 0.0, 1.0)
        disp = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1.0 / 2.4) - 0.055)
        lut = np.round(disp * 255.0).astype(np.uint8)
        _LUTS[transfer] = lut
    return lut


def apply_display(rgb, transfer):
    """Float HxWx3 code values -> contiguous uint8 HxWx3 display pixels."""
    import numpy as np

    a = np.nan_to_num(np.asarray(rgb, dtype=np.float32), nan=0.0, posinf=1.0, neginf=0.0)
    np.clip(a, 0.0, 1.0, out=a)
    idx = (a * (LUT_SIZE - 1) + 0.5).astype(np.uint16)
    return np.ascontiguousarray(display_lut(transfer)[idx])


# ---------------------------------------------------------------------------
# Geometry


def box_factor(src_w, max_w, pixel_aspect=1.0):
    """Integer downsample factor so the frame is still at least max_w wide on screen."""
    if not max_w or max_w <= 0:
        return 1
    pa = float(pixel_aspect) if pixel_aspect and pixel_aspect > 0 else 1.0
    target = float(max_w) / pa
    return max(1, int(float(src_w) // max(1.0, target)))


def window_slices(data_window, display_window):
    """Where an EXR data window lands in its display window.

    Windows are (x0, y0, x1, y1), inclusive, y down. Returns
    ((src_rows, src_cols), (dst_rows, dst_cols)) as (start, stop) pairs, or
    None when they don't overlap.
    """
    dx0, dy0, dx1, dy1 = data_window
    wx0, wy0, wx1, wy1 = display_window
    x0, y0 = max(dx0, wx0), max(dy0, wy0)
    x1, y1 = min(dx1, wx1), min(dy1, wy1)
    if x1 < x0 or y1 < y0:
        return None
    src = ((y0 - dy0, y1 - dy0 + 1), (x0 - dx0, x1 - dx0 + 1))
    dst = ((y0 - wy0, y1 - wy0 + 1), (x0 - wx0, x1 - wx0 + 1))
    return src, dst


def choose_channels(names):
    """Three channel names to show as R, G, B."""
    names = list(names)
    have = set(names)
    if {"R", "G", "B"} <= have:
        return ["R", "G", "B"]
    if "Y" in have:
        return ["Y", "Y", "Y"]
    for prefix in sorted({n.rsplit(".", 1)[0] for n in names if "." in n}):
        trio = [prefix + "." + c for c in ("R", "G", "B")]
        if set(trio) <= have:
            return trio
        trio = [prefix + "." + c for c in ("red", "green", "blue")]
        if set(trio) <= have:
            return trio
    if not names:
        raise ValueError("image has no channels")
    first = sorted(names)[0]
    return [first, first, first]


def downsample(a, k):
    """Box-filter a HxWxC float array by an integer factor."""
    if k <= 1:
        return a
    import numpy as np

    h, w = (a.shape[0] // k) * k, (a.shape[1] // k) * k
    if h == 0 or w == 0:
        return a
    a = a[:h, :w]
    return a.reshape(h // k, k, w // k, k, a.shape[2]).mean(axis=(1, 3), dtype=np.float32)


# ---------------------------------------------------------------------------
# EXR


def _box(value):
    """(x0, y0, x1, y1) from an Imath.Box2i or OpenEXR 3's (min, max) arrays."""
    if hasattr(value, "min"):
        return (int(value.min.x), int(value.min.y), int(value.max.x), int(value.max.y))
    lo, hi = value
    return (int(lo[0]), int(lo[1]), int(hi[0]), int(hi[1]))


def read_exr(path):
    """(float HxWx3 array over the display window, (width, height))."""
    import numpy as np
    import OpenEXR

    if hasattr(OpenEXR, "InputFile"):
        import Imath

        f = OpenEXR.InputFile(path)
        try:
            header = f.header()
            data_win = _box(header["dataWindow"])
            disp_win = _box(header["displayWindow"])
            names = choose_channels(header["channels"].keys())
            unique = list(dict.fromkeys(names))
            # Only these channels are read: AOVs and depth are skipped.
            raw = f.channels(unique, Imath.PixelType(Imath.PixelType.HALF))
        finally:
            try:
                f.close()
            except Exception:
                pass
        dh = data_win[3] - data_win[1] + 1
        dw = data_win[2] - data_win[0] + 1
        planes = {
            n: np.frombuffer(b, dtype=np.float16).reshape(dh, dw)
            for n, b in zip(unique, raw)
        }
    else:
        f = OpenEXR.File(path, separate_channels=True)
        header = f.header()
        data_win = _box(header["dataWindow"])
        disp_win = _box(header["displayWindow"])
        channels = f.channels()
        names = choose_channels(channels.keys())
        planes = {n: channels[n].pixels for n in set(names)}

    width = disp_win[2] - disp_win[0] + 1
    height = disp_win[3] - disp_win[1] + 1
    out = np.zeros((height, width, 3), dtype=np.float32)
    where = window_slices(data_win, disp_win)
    if where is not None:
        (sr, sc), (dr, dc) = where
        for i, n in enumerate(names):
            out[dr[0]:dr[1], dc[0]:dc[1], i] = planes[n][sr[0]:sr[1], sc[0]:sc[1]]
    return out, (width, height)


# ---------------------------------------------------------------------------
# DPX


def parse_dpx_header(head):
    """Fields needed to decode a DPX image from its first 2048 header bytes."""
    if len(head) < 820:
        raise ValueError("DPX header is truncated")
    magic = head[:4]
    if magic == b"SDPX":
        e = ">"
    elif magic == b"XPDS":
        e = "<"
    else:
        raise ValueError("not a DPX file")
    u32 = lambda o: struct.unpack(e + "I", head[o:o + 4])[0]  # noqa: E731
    u16 = lambda o: struct.unpack(e + "H", head[o:o + 2])[0]  # noqa: E731
    element_offset = u32(808)
    return {
        "endian": e,
        "offset": element_offset if element_offset not in (0, 0xFFFFFFFF) else u32(4),
        "width": u32(772),
        "height": u32(776),
        "descriptor": head[800],
        "transfer": head[801],
        "bits": head[803],
        "packing": u16(804),
        "encoding": u16(806),
    }


def dpx_components(descriptor):
    if descriptor == 50:
        return 3
    if descriptor == 51:
        return 4
    if descriptor in (6, 1, 2, 3, 4, 8):
        return 1
    raise ValueError("unsupported DPX descriptor %d" % descriptor)


def read_dpx(path):
    """(float HxWx3 code values 0..1, (width, height)) for common DPX layouts.

    Supports 8/16-bit, 10-bit packed into 32-bit words (methods A and B) and
    12-bit in 16-bit words, RGB, RGBA (8/16-bit) and luma.
    """
    import numpy as np

    with open(path, "rb") as fh:
        hdr = parse_dpx_header(fh.read(2048))
        if hdr["encoding"] != 0:
            raise ValueError("RLE-compressed DPX is not supported")
        fh.seek(hdr["offset"])
        data = fh.read()
    w, h, bits, e = hdr["width"], hdr["height"], hdr["bits"], hdr["endian"]
    comps = dpx_components(hdr["descriptor"])

    if bits == 10:
        if comps != 3 or hdr["packing"] not in (1, 2):
            raise ValueError(
                "10-bit DPX with %d components / packing %d is not supported"
                % (comps, hdr["packing"])
            )
        words = np.frombuffer(data, dtype=e + "u4", count=w * h)
        shift = 2 if hdr["packing"] == 1 else 0
        r = (words >> (20 + shift)) & 0x3FF
        g = (words >> (10 + shift)) & 0x3FF
        b = (words >> shift) & 0x3FF
        rgb = np.stack([r, g, b], axis=-1).reshape(h, w, 3).astype(np.float32) / 1023.0
    elif bits in (8, 12, 16):
        dtype = np.uint8 if bits == 8 else np.dtype(e + "u2")
        row = w * comps
        if bits == 8:
            row = (row + 3) // 4 * 4       # rows are padded to 32 bits
        vals = np.frombuffer(data, dtype=dtype, count=row * h).reshape(h, row)[:, :w * comps]
        vals = vals.reshape(h, w, comps).astype(np.float32)
        if bits == 12:
            # 12-bit in 16-bit words: method A keeps the data in the top bits.
            vals = vals / 16.0 if hdr["packing"] == 1 else vals
            vals /= 4095.0
        else:
            vals /= 255.0 if bits == 8 else 65535.0
        rgb = np.repeat(vals, 3, axis=2) if comps == 1 else vals[..., :3]
    else:
        raise ValueError("%d-bit DPX is not supported" % bits)
    return rgb, (w, h)


# ---------------------------------------------------------------------------


def decode_file(path, max_w=0, pixel_aspect=1.0, transfer="linear"):
    """(uint8 HxWx3 display pixels, (src_w, src_h)) for an EXR or DPX file."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "exr":
        rgb, size = read_exr(path)
    elif ext == "dpx":
        rgb, size = read_dpx(path)
    else:
        raise ValueError("the frame server can't read .%s files" % ext)
    rgb = downsample(rgb, box_factor(size[0], max_w, pixel_aspect))
    return apply_display(rgb, transfer), size
