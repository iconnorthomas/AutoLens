"""Direct plate reading: Read snapshots, colour transforms, worker framing and decoding.

No Qt and no Nuke. Pixel tests use numpy/OpenEXR (installed in CI) and skip
without them; the end-to-end test starts a real frame-server worker.
"""
import io
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "AutoLens", "python"))

from autolens import frame_decode as fd  # noqa: E402
from autolens import frame_protocol as fp  # noqa: E402
from autolens import plate_reader as pr  # noqa: E402

try:
    import numpy as np
except ImportError:          # pragma: no cover
    np = None
try:
    import OpenEXR
except ImportError:          # pragma: no cover
    OpenEXR = None


class PatternTests(unittest.TestCase):
    def test_format_pattern(self):
        self.assertEqual(pr.format_pattern("/a/p.%04d.exr", 7), "/a/p.0007.exr")
        self.assertEqual(pr.format_pattern("/a/p.####.exr", 1001), "/a/p.1001.exr")
        self.assertEqual(pr.format_pattern("/a/p.%d.dpx", 7), "/a/p.7.dpx")
        self.assertEqual(pr.format_pattern("/a/p.%04d.exr", -3), "/a/p.-003.exr")
        self.assertEqual(pr.format_pattern("/a/still.jpg", 5), "/a/still.jpg")

    def test_choose_pattern(self):
        absolute = os.path.abspath("plates/p.%04d.exr").replace("\\", "/")
        project = os.path.abspath("proj")
        self.assertEqual(pr.choose_pattern(["[python x]", absolute]), absolute)
        self.assertEqual(
            pr.choose_pattern(["plates/p.%04d.exr"], project),
            os.path.join(project, "plates/p.%04d.exr").replace("\\", "/"),
        )
        self.assertIsNone(pr.choose_pattern(["plates/p.%04d.exr"]))
        self.assertIsNone(pr.choose_pattern(["", None, "/a/%V/p.exr" if os.name != "nt" else "C:/a/%V/p.exr"]))
        self.assertEqual(pr.file_extension("/a/p.%04d.EXR"), "exr")

    def snap(self, **kw):
        kw.setdefault("pattern", "/a/p.%04d.exr")
        kw.setdefault("first", 1)
        kw.setdefault("last", 10)
        return pr.ReadSnapshot(**kw)

    def test_file_frames(self):
        s = self.snap(first=1001, last=1010)
        self.assertEqual(s.file_frame(1005), 1005)
        self.assertEqual(s.file_frame(990), 1001)                  # hold
        self.assertEqual(s.path(1012), "/a/p.1010.exr")
        s = self.snap(before="black", after="loop")
        self.assertIsNone(s.file_frame(0))
        self.assertIsNone(s.path(0))
        self.assertEqual(s.file_frame(12), 2)
        s = self.snap(last=4, after="bounce")
        self.assertEqual([s.file_frame(t) for t in (5, 6, 7, 8)], [3, 2, 1, 2])
        self.assertEqual(self.snap(frame_offset="100", frame_mode="start at").file_frame(103), 4)
        self.assertEqual(self.snap(frame_offset="2", frame_mode="offset").file_frame(5), 3)
        expr = self.snap(frame_offset="frame*2", frame_mode="expression")
        self.assertIs(expr.file_frame(5), False)
        self.assertIsNone(expr.path(5))

    def test_snapshot_key_tracks_knobs(self):
        self.assertEqual(self.snap().key(), self.snap().key())
        self.assertNotEqual(self.snap().key(), self.snap(colorspace="sRGB").key())
        self.assertEqual(self.snap(pixel_aspect=0).pixel_aspect, 1.0)

    def test_upstream_note(self):
        self.assertEqual(pr.upstream_note([]), "")
        self.assertEqual(pr.upstream_note([("Dot", False), ("Grade", True)]), "")
        self.assertIn("upstream nodes not applied", pr.upstream_note([("Dot", False), ("Grade", False)]))


class ColourTests(unittest.TestCase):
    def test_transfer_for(self):
        t = fd.transfer_for
        self.assertEqual(t("default", ext="exr"), ("linear", True))
        self.assertEqual(t("default (linear)", ext="exr"), ("linear", True))
        self.assertEqual(t("default", ext="dpx"), ("cineon", True))
        self.assertEqual(t("default (Cineon)", ext="dpx"), ("cineon", True))
        self.assertEqual(t("default", ext="jpg"), ("srgb", True))
        self.assertEqual(t("sRGB", ext="exr"), ("srgb", True))
        self.assertEqual(t("sRGB - Texture", ext="exr"), ("srgb", True))
        self.assertEqual(t("Linear Rec.709 (sRGB)", ext="exr"), ("linear", True))
        self.assertEqual(t("ACES - ACEScg", ext="exr"), ("linear", True))
        self.assertEqual(t("scene_linear", ext="exr"), ("linear", True))
        self.assertEqual(t("rec709", ext="dpx"), ("rec709", True))
        self.assertEqual(t("Gamma2.2", ext="exr"), ("gamma2.2", True))
        self.assertEqual(t("Rec.1886", ext="exr"), ("gamma2.4", True))
        self.assertEqual(t("Cineon", ext="exr"), ("cineon", True))
        self.assertEqual(t("Input - ARRI - LogC", ext="exr"), ("linear", False))
        self.assertEqual(t("Input - ARRI - LogC", ext="dpx"), ("srgb", False))
        self.assertEqual(t("sRGB", raw=True, ext="exr"), ("linear", True))

    def test_reference_curves(self):
        self.assertAlmostEqual(fd.srgb_encode(0.18), 0.4613, places=3)
        self.assertAlmostEqual(fd.srgb_encode(2.0), 1.0)
        self.assertEqual(fd.srgb_encode(-1.0), 0.0)
        # sRGB decode then encode is the identity.
        for x in (0.0, 0.02, 0.2, 0.5, 1.0):
            self.assertAlmostEqual(fd.srgb_encode(fd.to_linear(x, "srgb")), x, places=6)
        # Cineon: 95 is black, 685 is white (1.0 linear).
        self.assertAlmostEqual(fd.to_linear(95 / 1023.0, "cineon"), 0.0, places=6)
        self.assertAlmostEqual(fd.to_linear(685 / 1023.0, "cineon"), 1.0, places=6)
        self.assertAlmostEqual(fd.to_linear(0.5, "gamma2.2"), 0.5 ** 2.2)
        self.assertEqual(fd.display_value(1.0, "linear"), 255)
        self.assertEqual(fd.display_value(0.0, "linear"), 0)

    def test_box_factor(self):
        self.assertEqual(fd.box_factor(4096, 1280), 3)
        self.assertEqual(fd.box_factor(1920, 1920), 1)
        self.assertEqual(fd.box_factor(1920, 0), 1)
        self.assertEqual(fd.box_factor(1000, 4000), 1)
        self.assertEqual(fd.box_factor(2048, 1024, pixel_aspect=2.0), 4)

    def test_window_slices(self):
        self.assertEqual(
            fd.window_slices((0, 0, 9, 4), (0, 0, 9, 4)),
            (((0, 5), (0, 10)), ((0, 5), (0, 10))),
        )
        # Overscan: data window larger than the display window.
        self.assertEqual(
            fd.window_slices((-2, -1, 11, 5), (0, 0, 9, 4)),
            (((1, 6), (2, 12)), ((0, 5), (0, 10))),
        )
        # Data inside the display window (a crop).
        self.assertEqual(
            fd.window_slices((3, 1, 5, 2), (0, 0, 9, 4)),
            (((0, 2), (0, 3)), ((1, 3), (3, 6))),
        )
        self.assertIsNone(fd.window_slices((20, 20, 30, 30), (0, 0, 9, 4)))

    def test_choose_channels(self):
        self.assertEqual(fd.choose_channels(["A", "B", "G", "R", "depth.Z"]), ["R", "G", "B"])
        self.assertEqual(fd.choose_channels(["Y", "A"]), ["Y", "Y", "Y"])
        self.assertEqual(
            fd.choose_channels(["beauty.R", "beauty.G", "beauty.B", "N.x"]),
            ["beauty.R", "beauty.G", "beauty.B"],
        )
        self.assertEqual(fd.choose_channels(["Z"]), ["Z", "Z", "Z"])
        with self.assertRaises(ValueError):
            fd.choose_channels([])

    @unittest.skipIf(np is None, "numpy not installed")
    def test_lut_matches_reference(self):
        for transfer in fd.TRANSFERS:
            values = [i / 64.0 for i in range(65)]
            got = fd.apply_display(np.array(values, dtype=np.float32).reshape(1, -1, 1).repeat(3, 2), transfer)
            want = [fd.display_value(v, transfer) for v in values]
            diffs = [abs(int(g) - w) for g, w in zip(got[0, :, 0], want)]
            self.assertLessEqual(max(diffs), 1, transfer)
        weird = np.array([[[np.nan, np.inf, -np.inf]]], dtype=np.float32)
        self.assertEqual(fd.apply_display(weird, "linear").tolist(), [[[0, 255, 0]]])

    @unittest.skipIf(np is None, "numpy not installed")
    def test_downsample(self):
        a = np.arange(4 * 6 * 3, dtype=np.float32).reshape(4, 6, 3)
        d = fd.downsample(a, 2)
        self.assertEqual(d.shape, (2, 3, 3))
        self.assertAlmostEqual(float(d[0, 0, 0]), float(a[:2, :2, 0].mean()))
        self.assertIs(fd.downsample(a, 1), a)


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self):
        buf = io.BytesIO()
        fp.send(buf, {"id": 3, "ok": True}, b"\x01\x02\x03")
        fp.send(buf, {"cmd": "hello"})
        buf.seek(0)
        header, payload = fp.recv(buf)
        self.assertEqual((header["id"], header["nbytes"], payload), (3, 3, b"\x01\x02\x03"))
        header, payload = fp.recv(buf)
        self.assertEqual((header["cmd"], payload), ("hello", b""))
        with self.assertRaises(EOFError):
            fp.recv(buf)

    def test_truncated_and_bad_messages(self):
        whole = fp.pack({"id": 1}, b"abcdef")
        with self.assertRaises(EOFError):
            fp.recv(io.BytesIO(whole[:-2]))
        with self.assertRaises(fp.ProtocolError):
            fp.recv(io.BytesIO(struct.pack(">I", fp.MAX_HEADER + 1)))
        with self.assertRaises(fp.ProtocolError):
            fp.recv(io.BytesIO(struct.pack(">I", 3) + b"[1]"))
        with self.assertRaises(fp.ProtocolError):
            fp.recv(io.BytesIO(struct.pack(">I", 4) + b"nope"))

    def test_reads_across_short_chunks(self):
        class Trickle(io.RawIOBase):
            def __init__(self, data):
                self.data = data

            def readable(self):
                return True

            def read(self, n=-1):
                chunk, self.data = self.data[:1], self.data[1:]
                return chunk

        header, payload = fp.recv(Trickle(fp.pack({"x": 1}, b"pixels")))
        self.assertEqual((header["x"], payload), (1, b"pixels"))


def write_dpx(path, rgb10, big_endian=True, packing=1):
    """A minimal 10-bit RGB DPX (method A or B) for tests."""
    h, w, _ = rgb10.shape
    e = ">" if big_endian else "<"
    head = bytearray(2048)
    head[0:4] = b"SDPX" if big_endian else b"XPDS"
    struct.pack_into(e + "I", head, 4, 2048)
    struct.pack_into(e + "I", head, 772, w)
    struct.pack_into(e + "I", head, 776, h)
    head[800] = 50          # RGB
    head[801] = 2           # linear
    head[803] = 10
    struct.pack_into(e + "H", head, 804, packing)
    struct.pack_into(e + "H", head, 806, 0)
    struct.pack_into(e + "I", head, 808, 2048)
    shift = 2 if packing == 1 else 0
    words = (
        (rgb10[..., 0].astype(np.uint32) << (20 + shift))
        | (rgb10[..., 1].astype(np.uint32) << (10 + shift))
        | (rgb10[..., 2].astype(np.uint32) << shift)
    )
    with open(path, "wb") as fh:
        fh.write(bytes(head))
        fh.write(words.astype(e + "u4").tobytes())


@unittest.skipIf(np is None, "numpy not installed")
class DecodeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_dpx_header_and_pixels(self):
        rgb = np.zeros((4, 6, 3), dtype=np.uint16)
        rgb[..., 0] = 1023
        rgb[1, 2] = (0, 512, 100)
        for big, packing in ((True, 1), (False, 1), (True, 2)):
            path = os.path.join(self.dir, "f_%d_%d.dpx" % (big, packing))
            write_dpx(path, rgb, big, packing)
            with open(path, "rb") as fh:
                hdr = fd.parse_dpx_header(fh.read(2048))
            self.assertEqual((hdr["width"], hdr["height"], hdr["bits"]), (6, 4, 10))
            data, size = fd.read_dpx(path)
            self.assertEqual(size, (6, 4))
            self.assertAlmostEqual(float(data[0, 0, 0]), 1.0)
            self.assertAlmostEqual(float(data[1, 2, 1]), 512 / 1023.0, places=5)
            self.assertAlmostEqual(float(data[1, 2, 2]), 100 / 1023.0, places=5)
        with self.assertRaises(ValueError):
            fd.parse_dpx_header(b"JUNK" + bytes(2044))

    def test_dpx_decode_file(self):
        rgb = np.full((8, 8, 3), 685, dtype=np.uint16)      # Cineon white
        path = os.path.join(self.dir, "white.dpx")
        write_dpx(path, rgb)
        pixels, size = fd.decode_file(path, 0, 1.0, "cineon")
        self.assertEqual((pixels.shape, pixels.dtype.name, size), ((8, 8, 3), "uint8", (8, 8)))
        self.assertTrue((pixels == 255).all())
        with self.assertRaises(ValueError):
            fd.decode_file(os.path.join(self.dir, "x.mov"))

    @unittest.skipIf(OpenEXR is None, "OpenEXR not installed")
    def test_exr_rgb_only_display_window_and_downsample(self):
        h, w = 40, 64
        r = np.full((h, w), 0.18, dtype=np.float32)
        g = np.zeros((h, w), dtype=np.float32)
        b = np.ones((h, w), dtype=np.float32) * 4.0      # HDR clips to white
        z = np.full((h, w), 1000.0, dtype=np.float32)
        path = os.path.join(self.dir, "f.exr")
        OpenEXR.File(
            {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage},
            {"R": r, "G": g, "B": b, "depth.Z": z},
        ).write(path)
        pixels, size = fd.decode_file(path, max_w=32, transfer="linear")
        self.assertEqual(size, (64, 40))
        self.assertEqual(pixels.shape, (20, 32, 3))
        r8, g8, b8 = pixels[5, 5].tolist()
        self.assertLessEqual(abs(r8 - fd.display_value(0.18, "linear")), 1)
        self.assertEqual((g8, b8), (0, 255))
        data, _ = fd.read_exr(path)
        self.assertEqual(data.shape, (40, 64, 3))


@unittest.skipIf(np is None or OpenEXR is None, "numpy/OpenEXR not installed")
class FrameServerTests(unittest.TestCase):
    """Starts real worker processes with this interpreter."""

    def test_decode_through_worker_processes(self):
        from autolens.frame_server import FrameServerError, FrameServerPool

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.exr")
            ones = np.ones((10, 16), dtype=np.float32)
            OpenEXR.File(
                {"type": OpenEXR.scanlineimage},
                {"R": ones, "G": ones * 0.0, "B": ones * 0.5},
            ).write(path)
            pool = FrameServerPool(sys.executable, size=2)
            try:
                self.assertTrue(pool.hello()["ok"])
                w, h, rgb, src = pool.decode(path, 0, 1.0, "linear")
                self.assertEqual((w, h, len(rgb), tuple(src)), (16, 10, 16 * 10 * 3, (16, 10)))
                self.assertEqual(list(rgb[:2]), [255, 0])
                self.assertLessEqual(abs(rgb[2] - fd.display_value(0.5, "linear")), 1)
                with self.assertRaises(FrameServerError) as err:
                    pool.decode(os.path.join(tmp, "missing.exr"))
                self.assertIn("missing.exr", str(err.exception))
                # The worker survives a failed request.
                self.assertEqual(pool.decode(path, 8)[0], 8)
            finally:
                pool.close()
            with self.assertRaises(FrameServerError):
                pool.decode(path)

    def test_missing_python(self):
        from autolens.frame_server import FrameServerError, FrameServerPool

        pool = FrameServerPool(None)
        with self.assertRaises(FrameServerError):
            pool.hello()


if __name__ == "__main__":
    unittest.main()
