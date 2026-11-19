"""Cache naming and pruning for the Pi Zero client.

The device is hand-deployed and hard to reach, and both failures these cover
are silent from the couch — a wrong photo pinned in the cache forever, or an
SD card filling up over months — so they are pinned down here instead.

apps/pi-zero-client is a directory of plain scripts, not a package: the
modules are loaded by path so the tests run from the repo root like the rest.
"""
import importlib.util
import io
import sys
import unittest.mock
from PIL import Image, WebPImagePlugin
import tempfile
import unittest
from pathlib import Path

PI_ZERO = Path(__file__).resolve().parents[1] / "apps" / "pi-zero-client"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PI_ZERO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frame_cache = _load("frame_cache")


def tiny_image(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(output, format)
    return output.getvalue()


class TestImageValidation(unittest.TestCase):
    def test_verify_and_pixel_decode_reject_truncated_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.jpg"
            good = tiny_image(format="JPEG")
            path.write_bytes(good)
            self.assertTrue(frame_cache.valid_image(path))
            path.write_bytes(good[:-2])
            # JPEG verify alone accepts this; forced load must reject it.
            with Image.open(path) as image:
                image.verify()
            self.assertFalse(frame_cache.valid_image(path))

    def test_bad_png_crc_empty_and_missing_are_not_playable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            self.assertFalse(frame_cache.valid_image(path))
            good = tiny_image()
            damaged = bytearray(good)
            damaged[-17] ^= 1  # corrupt the IDAT CRC, not just the MIME/header
            for body in (b"", b"not image", good[:40], bytes(damaged)):
                path.write_bytes(body)
                self.assertFalse(frame_cache.valid_image(path))
            path.write_bytes(good)
            self.assertTrue(frame_cache.valid_image(path))


def noise_jpeg(size=(256, 192), **save):
    import random
    rng = random.Random(3)
    image = Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90, **save)
    return output.getvalue()


def recording_load(sizes):
    from PIL import ImageFile
    original = ImageFile.ImageFile.load

    def load(image):
        sizes.append(image.size)
        return original(image)
    return unittest.mock.patch.object(ImageFile.ImageFile, "load", load)


class TestBoundedValidation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "photo.jpg"

    def test_reduced_draft_still_rejects_truncated_baseline_and_progressive(self):
        for label, body in (("baseline", noise_jpeg()), ("progressive", noise_jpeg(progressive=True))):
            for keep in (0.7, 0.5, 0.95):
                with self.subTest(label=label, keep=keep):
                    self.path.write_bytes(body[:int(len(body) * keep)])
                    self.assertFalse(frame_cache.valid_image(self.path))
            self.path.write_bytes(body)
            self.assertTrue(frame_cache.valid_image(self.path))

    def test_validation_decodes_jpeg_at_one_eighth_scale(self):
        self.path.write_bytes(noise_jpeg((256, 192)))
        sizes = []
        with recording_load(sizes):
            self.assertTrue(frame_cache.valid_image(self.path))
        self.assertEqual(sizes[-1], (32, 24))

    def test_memory_error_is_not_reported_as_corruption(self):
        from PIL import ImageFile
        self.path.write_bytes(noise_jpeg())
        with unittest.mock.patch.object(ImageFile.ImageFile, "load", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                frame_cache.valid_image(self.path)
        with unittest.mock.patch.object(frame_cache.Image, "open", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                frame_cache.valid_image(self.path)

    def test_only_display_formats_are_accepted(self):
        for fmt, accepted in (("JPEG", True), ("PNG", True), ("WEBP", True), ("GIF", True),
                              ("BMP", False), ("TIFF", False), ("PPM", False)):
            with self.subTest(fmt):
                self.path.write_bytes(tiny_image(format=fmt))
                self.assertIs(frame_cache.valid_image(self.path), accepted)

    def test_decompression_bomb_is_invalid_not_a_crash(self):
        self.path.write_bytes(tiny_image(format="PNG"))
        for limit in (4, 2):  # warning band, then hard DecompressionBombError
            with unittest.mock.patch.object(Image, "MAX_IMAGE_PIXELS", limit):
                self.assertFalse(frame_cache.valid_image(self.path))


def _png_chunk(kind, data):
    import struct
    import zlib
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def large_png_header(width, height):
    """A tiny, structurally valid PNG whose IHDR claims huge dimensions.

    Image.open reports (width, height) from the header alone; nothing the
    size of the claimed pixels is ever allocated by the test.
    """
    import struct
    import zlib
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + _png_chunk(b"IEND", b""))


def large_webp_header(width, height, lossless=False):
    """A real Pillow-encoded 16x16 WebP with its bitstream dimensions patched.

    libwebp's demuxer reads only the frame header at open time, so Pillow
    reports the patched (huge) size for a file of a few dozen bytes.
    """
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, "WEBP", lossless=lossless)
    body = bytearray(output.getvalue())
    if lossless:
        assert body[12:16] == b"VP8L" and body[20] == 0x2F
        bits = int.from_bytes(body[21:25], "little") & ~((1 << 28) - 1)
        body[21:25] = (bits | (width - 1) | ((height - 1) << 14)).to_bytes(4, "little")
    else:
        assert body[12:16] == b"VP8 " and body[23:26] == b"\x9d\x01\x2a"
        body[26:28] = width.to_bytes(2, "little")
        body[28:30] = height.to_bytes(2, "little")
    return bytes(body)


def large_webp_vp8x(width, height):
    """Extended-format (VP8X) WebP header whose canvas claims huge dimensions."""
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    vp8x = b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" + canvas
    inner = large_webp_header(width, height, lossless=True)[12:]
    payload = b"WEBP" + vp8x + inner
    return b"RIFF" + len(payload).to_bytes(4, "little") + payload


class TestNonJpegPixelGuard(unittest.TestCase):
    """C1: PNG/WebP/GIF decode at full size; refuse them from the header."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def assert_rejected_without_decoding(self, name, body):
        from PIL import ImageFile
        path = self.dir / name
        path.write_bytes(body)
        with unittest.mock.patch.object(ImageFile.ImageFile, "load") as file_load, \
                unittest.mock.patch.object(Image.Image, "load") as image_load, \
                unittest.mock.patch.object(WebPImagePlugin.WebPImageFile, "load") as webp_load, \
                unittest.mock.patch.object(ImageFile.ImageFile, "verify") as verify:
            with self.assertRaises(ValueError) as caught:
                frame_cache.check_image(path)
            self.assertFalse(frame_cache.valid_image(path))
        self.assertIn("pixels", str(caught.exception))
        file_load.assert_not_called()
        image_load.assert_not_called()
        webp_load.assert_not_called()
        verify.assert_not_called()

    def test_fixtures_really_claim_large_dimensions(self):
        for body in (large_png_header(4000, 3000), large_webp_header(4000, 3000),
                     large_webp_header(4000, 3000, lossless=True)):
            with Image.open(io.BytesIO(body)) as image:
                self.assertEqual(image.size, (4000, 3000))

    def test_oversized_png_is_rejected_from_the_header(self):
        self.assertEqual(frame_cache.MAX_NON_JPEG_PIXELS, 8_000_000)
        self.assert_rejected_without_decoding("big.png", large_png_header(4000, 3000))
        self.assert_rejected_without_decoding("huge.png", large_png_header(8000, 6000))

    def test_oversized_webp_is_rejected_from_the_header(self):
        for label, body in (("lossy", large_webp_header(4000, 3000)),
                            ("lossless", large_webp_header(4000, 3000, lossless=True)),
                            ("vp8x", large_webp_vp8x(8000, 6000))):
            with self.subTest(label):
                self.assert_rejected_without_decoding(f"{label}.webp", body)

    def test_webp_is_refused_before_pillow_opens_it(self):
        """Pillow builds without WebP-anim decode the whole bitstream in open()."""
        path = self.dir / "big.webp"
        path.write_bytes(large_webp_header(4000, 3000))
        with unittest.mock.patch.object(frame_cache.Image, "open") as opened:
            with self.assertRaises(ValueError):
                frame_cache.check_image(path)
        opened.assert_not_called()

    def test_limit_boundary_applies_to_non_jpeg_only(self):
        path = self.dir / "image"
        for fmt in ("PNG", "WEBP", "GIF"):
            with self.subTest(fmt):
                path.write_bytes(tiny_image(format=fmt))  # 3x2 = 6 pixels
                with unittest.mock.patch.object(frame_cache, "MAX_NON_JPEG_PIXELS", 6):
                    self.assertTrue(frame_cache.valid_image(path))
                with unittest.mock.patch.object(frame_cache, "MAX_NON_JPEG_PIXELS", 5):
                    self.assertFalse(frame_cache.valid_image(path))
        path.write_bytes(tiny_image(format="JPEG"))
        with unittest.mock.patch.object(frame_cache, "MAX_NON_JPEG_PIXELS", 1):
            self.assertTrue(frame_cache.valid_image(path), "JPEG uses the 1/8 draft, not this cap")

    def test_jpeg_stays_bounded_by_the_bomb_limit(self):
        path = self.dir / "photo.jpg"
        path.write_bytes(noise_jpeg((64, 48)))
        with unittest.mock.patch.object(Image, "MAX_IMAGE_PIXELS", 64 * 48 - 1):
            self.assertFalse(frame_cache.valid_image(path))

    def test_small_png_webp_gif_still_validate(self):
        for fmt in ("PNG", "WEBP", "GIF"):
            with self.subTest(fmt):
                path = self.dir / f"ok.{fmt.lower()}"
                path.write_bytes(tiny_image(format=fmt))
                frame_cache.check_image(path)


def solid_jpeg(size, **save):
    """A real Pillow-encoded JPEG. Flat colour keeps a 24 MP file ~300 KB and
    encodes in well under a second; the SOF header still describes the real
    dimensions, sampling factors and (if requested) progressive coding."""
    output = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(output, "JPEG", quality=80, **save)
    return output.getvalue()


def assert_not_decoded():
    """Patch every decode entry point; the caller asserts none was called."""
    from PIL import ImageFile
    return (unittest.mock.patch.object(ImageFile.ImageFile, "load"),
            unittest.mock.patch.object(Image.Image, "load"),
            unittest.mock.patch.object(ImageFile.ImageFile, "verify"))


class TestProgressiveJpegBudget(unittest.TestCase):
    """I1: progressive JPEGs buffer every DCT coefficient at full resolution.

    draft()/DCT scaling only shrinks the OUTPUT of a baseline decode; libjpeg
    must keep the whole coefficient image of a progressive file in memory
    (2 bytes/coefficient), so a 48 MP 4:4:4 progressive JPEG needs ~290 MB
    no matter how small the draft is. Measured on macOS: 314 MB check peak.
    """

    LARGE = (6000, 4000)  # 24 MP: 4:4:4 progressive needs ~144 MB of coefficients

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, body):
        path = self.dir / name
        path.write_bytes(body)
        return path

    def test_budget_is_a_named_constant_between_64_and_96_mb(self):
        budget = frame_cache.MAX_BUFFERED_JPEG_COEF_BYTES
        self.assertGreaterEqual(budget, 64 * 1024 * 1024)
        self.assertLessEqual(budget, 96 * 1024 * 1024)

    def test_estimate_follows_component_sampling_factors(self):
        w, h = 640, 480
        cases = (("4:4:4", dict(subsampling=0), 3.0), ("4:2:0", dict(subsampling=2), 1.5))
        for label, save, factor in cases:
            with self.subTest(label), Image.open(io.BytesIO(solid_jpeg((w, h), progressive=True, **save))) as image:
                self.assertEqual(frame_cache.buffered_coefficient_bytes(image), int(w * h * 2 * factor))
        gray = io.BytesIO()
        Image.new("L", (w, h)).save(gray, "JPEG", progressive=True)
        with Image.open(gray) as image:
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), w * h * 2)
        with Image.open(io.BytesIO(solid_jpeg((w, h)))) as image:
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), 0, "baseline needs no buffer")

    def test_real_large_progressive_jpeg_is_refused_before_decoding(self):
        path = self.write("prog.jpg", solid_jpeg(self.LARGE, progressive=True, subsampling=0))
        with Image.open(path) as image:  # the fixture really is a large progressive JPEG
            self.assertEqual((image.format, image.size), ("JPEG", self.LARGE))
            self.assertTrue(image.info.get("progressive") or image.info.get("progression"))
        loads = assert_not_decoded()
        with loads[0] as file_load, loads[1] as image_load, loads[2] as verify:
            with self.assertRaises(ValueError) as caught:
                frame_cache.check_image(path)
            self.assertFalse(frame_cache.valid_image(path))
        self.assertIn("progressive", str(caught.exception))
        file_load.assert_not_called()
        image_load.assert_not_called()
        verify.assert_not_called()
        self.assertIsInstance(caught.exception, frame_cache.DECODE_ERRORS)

    def test_24mp_progressive_4_2_0_is_admitted(self):
        # ~72 MB of coefficients: the common web/camera-export shape plays.
        path = self.write("prog420.jpg", solid_jpeg(self.LARGE, progressive=True, subsampling=2))
        with Image.open(path) as image:
            need = frame_cache.buffered_coefficient_bytes(image)
        self.assertEqual(need, 6000 * 4000 * 3)
        frame_cache.check_image(path)

    def test_real_large_baseline_jpeg_still_validates(self):
        path = self.write("base.jpg", solid_jpeg(self.LARGE, subsampling=0))
        frame_cache.check_image(path)
        self.assertTrue(frame_cache.valid_image(path))

    def test_progressive_within_budget_still_validates_and_truncation_is_caught(self):
        body = solid_jpeg((4000, 3000), progressive=True, subsampling=2)  # 12 MP: ~36 MB
        path = self.write("ok.jpg", body)
        frame_cache.check_image(path)
        path.write_bytes(body[: len(body) // 2])
        self.assertFalse(frame_cache.valid_image(path))

    def test_budget_boundary(self):
        path = self.write("small.jpg", solid_jpeg((64, 48), progressive=True, subsampling=0))
        need = 64 * 48 * 2 * 3
        with unittest.mock.patch.object(frame_cache, "MAX_BUFFERED_JPEG_COEF_BYTES", need):
            self.assertTrue(frame_cache.valid_image(path))
        with unittest.mock.patch.object(frame_cache, "MAX_BUFFERED_JPEG_COEF_BYTES", need - 1):
            self.assertFalse(frame_cache.valid_image(path))

    def test_viewer_skips_large_progressive_without_decoding(self):
        sys.path.insert(0, str(PI_ZERO))
        self.addCleanup(sys.path.remove, str(PI_ZERO))
        viewer = _load("viewer")
        path = self.write("prog.jpg", solid_jpeg(self.LARGE, progressive=True, subsampling=0))
        fb = io.BytesIO(b"previous frame")
        loads = assert_not_decoded()
        with loads[0] as file_load, loads[1] as image_load, loads[2]:
            self.assertFalse(viewer.render_safely(path, {"xres": 80, "yres": 60, "bpp": 32}, fb, {}))
        file_load.assert_not_called()
        image_load.assert_not_called()
        self.assertEqual(fb.getvalue(), b"previous frame")


def _segment(marker, payload):
    import struct
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def sequential_jpeg(size, interleaved=False):
    """A real, decodable SOF0 (baseline) 4:4:4 JPEG built in pure Python.

    Mid-grey everywhere, so every 8x8 block is "DC difference 0, EOB": with
    one-code Huffman tables (code '0' for both) each block costs 2 zero bits
    and a 24 MP file is ~280 KB, built in microseconds. interleaved=False puts
    each component in its own full-spectrum scan (first SOS: 1 of 3
    components) - what `jpegtran -scans` produces, and what makes libjpeg
    buffer every coefficient even though the file is not progressive.
    """
    import struct
    width, height = size
    blocks = -(-width // 8) * -(-height // 8)
    one_code = bytes([1] + [0] * 15) + b"\x00"  # a single symbol 0, length 1
    parts = [b"\xff\xd8", _segment(0xDB, b"\x00" + b"\x01" * 64),
             _segment(0xC0, struct.pack(">BHHB", 8, height, width, 3) +
                      b"".join(bytes([c, 0x11, 0]) for c in (1, 2, 3))),
             _segment(0xC4, b"\x00" + one_code + b"\x10" + one_code)]
    for comps in ([(1, 2, 3)] if interleaved else [(1,), (2,), (3,)]):
        parts.append(_segment(0xDA, bytes([len(comps)]) + b"".join(bytes([c, 0]) for c in comps)
                              + b"\x00\x3f\x00"))
        bits = 2 * blocks * len(comps)
        parts.append(b"\x00" * (bits // 8) + (bytes([0xFF >> (bits % 8)]) if bits % 8 else b""))
    parts.append(b"\xff\xd9")
    return b"".join(parts)


class TestNonInterleavedJpegBudget(unittest.TestCase):
    """libjpeg buffers every coefficient whenever the FIRST scan carries fewer
    components than the frame (jdinput: has_multiple_scans), progressive or
    not. Pillow reports such SOF0 files as plain baseline (no 'progressive'
    info), so the estimator must read the first SOS itself. Reviewer repro:
    a jpegtran -scans 48 MP 4:4:4 file peaked at 314 MB check / 338 MB render.
    """

    LARGE = (6000, 4000)  # 24 MP 4:4:4: ~144 MB of coefficients

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, body):
        path = self.dir / name
        path.write_bytes(body)
        return path

    def assert_refused_without_decoding(self, path):
        loads = assert_not_decoded()
        with loads[0] as file_load, loads[1] as image_load, loads[2] as verify:
            with self.assertRaises(ValueError) as caught:
                frame_cache.check_image(path)
            self.assertFalse(frame_cache.valid_image(path))
        file_load.assert_not_called()
        image_load.assert_not_called()
        verify.assert_not_called()
        self.assertIn("coefficient", str(caught.exception))
        self.assertIsInstance(caught.exception, frame_cache.DECODE_ERRORS)

    def test_pure_python_fixture_is_a_real_non_interleaved_baseline(self):
        body = sequential_jpeg(self.LARGE)
        self.assertEqual(body[:2] + body[2 + 69:2 + 71], b"\xff\xd8\xff\xc0")  # SOF0, not progressive
        layout = frame_cache.jpeg_scan_layout(io.BytesIO(body))
        self.assertEqual(layout, (False, 3, 1))
        with Image.open(io.BytesIO(body)) as image:
            self.assertEqual((image.format, image.size), ("JPEG", self.LARGE))
            self.assertFalse(image.info.get("progressive") or image.info.get("progression"))
        with Image.open(io.BytesIO(sequential_jpeg((37, 21)))) as image:
            image.load()  # small one really decodes through libjpeg
            self.assertEqual(image.getextrema(), ((128, 128),) * 3)

    def test_estimate_charges_non_interleaved_but_not_interleaved_baseline(self):
        w, h = self.LARGE
        with Image.open(io.BytesIO(sequential_jpeg(self.LARGE))) as image:
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), w * h * 2 * 3)
        with Image.open(io.BytesIO(sequential_jpeg(self.LARGE, interleaved=True))) as image:
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), 0)
        with Image.open(io.BytesIO(solid_jpeg(self.LARGE, subsampling=0))) as image:
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), 0)

    def test_large_non_interleaved_baseline_is_refused_before_decoding(self):
        fixtures = [("pure-python", sequential_jpeg(self.LARGE))]
        # jpegtran is a test-time convenience only (never a runtime dependency);
        # when it is absent the pure-Python fixture above still runs.
        import shutil
        import subprocess
        jpegtran = shutil.which("jpegtran")
        if jpegtran:
            src = self.write("src.jpg", solid_jpeg(self.LARGE, progressive=True, subsampling=0))
            scans = self.write("scans.txt", b"0: 0 63 0 0;\n1: 0 63 0 0;\n2: 0 63 0 0;\n")
            out = self.dir / "seq.jpg"
            subprocess.run([jpegtran, "-scans", str(scans), "-outfile", str(out), str(src)],
                           check=True, timeout=60)
            self.assertEqual(frame_cache.jpeg_scan_layout(io.BytesIO(out.read_bytes())), (False, 3, 1))
            fixtures.append(("jpegtran -scans", out.read_bytes()))
        for label, body in fixtures:
            with self.subTest(label):
                self.assert_refused_without_decoding(self.write(f"{len(label)}.jpg", body))

    def test_small_non_interleaved_baseline_validates_and_boundary(self):
        path = self.write("small.jpg", sequential_jpeg((64, 48)))
        need = 64 * 48 * 2 * 3
        with unittest.mock.patch.object(frame_cache, "MAX_BUFFERED_JPEG_COEF_BYTES", need):
            self.assertTrue(frame_cache.valid_image(path))
        with unittest.mock.patch.object(frame_cache, "MAX_BUFFERED_JPEG_COEF_BYTES", need - 1):
            self.assertFalse(frame_cache.valid_image(path))

    def test_viewer_skips_large_non_interleaved_without_decoding(self):
        sys.path.insert(0, str(PI_ZERO))
        self.addCleanup(sys.path.remove, str(PI_ZERO))
        viewer = _load("viewer")
        path = self.write("seq.jpg", sequential_jpeg(self.LARGE))
        fb = io.BytesIO(b"previous frame")
        loads = assert_not_decoded()
        with loads[0] as file_load, loads[1] as image_load, loads[2]:
            self.assertFalse(viewer.render_safely(path, {"xres": 80, "yres": 60, "bpp": 32}, fb, {}))
        file_load.assert_not_called()
        image_load.assert_not_called()
        self.assertEqual(fb.getvalue(), b"previous frame")


class TestJpegScanLayoutParser(unittest.TestCase):
    """The header walk must never crash: malformed -> None (worst case)."""

    def layout(self, body):
        return frame_cache.jpeg_scan_layout(io.BytesIO(body))

    def test_fill_bytes_standalone_markers_and_app_segments(self):
        body = sequential_jpeg((64, 48))
        head, rest = body[:2], body[2:]
        app = _segment(0xE1, b"Exif\x00\x00" + b"\xff" * 4000)  # 0xFF inside a payload is data
        weird = head + b"\xff\xff\xff" + app + b"\xff\x01" + b"\xff\xd0" + b"\xff\xff" + rest
        self.assertEqual(self.layout(weird), (False, 3, 1))
        self.assertEqual(self.layout(sequential_jpeg((64, 48), interleaved=True)), (False, 3, 3))

    def test_progressive_sof_and_pillow_progressive_files(self):
        self.assertEqual(self.layout(solid_jpeg((64, 48), progressive=True, subsampling=0))[:2], (True, 3))
        self.assertEqual(self.layout(solid_jpeg((64, 48))), (False, 3, 3))
        body = bytearray(sequential_jpeg((64, 48), interleaved=True))
        sof = body.index(b"\xff\xc0")
        for marker in (0xC2, 0xC6, 0xCA, 0xCE):
            body[sof + 1] = marker
            self.assertEqual(self.layout(bytes(body)), (True, 3, 3))

    def test_malformed_or_truncated_headers_are_none(self):
        body = sequential_jpeg((64, 48))
        sos = body.index(b"\xff\xda")
        cases = {
            "empty": b"",
            "no SOI": b"\x00" + body,
            "cut before SOS": body[:sos],
            "cut inside SOS": body[:sos + 4],
            "cut inside a length": body[:3],
            "EOI before SOS": body[:2] + b"\xff\xd9" + body[2:],
            "length < 2": body[:2] + b"\xff\xe0\x00\x01" + body[2:],
            "garbage between segments": body[:2] + b"\x12" + body[2:],
            "SOS before SOF": body[:2] + body[sos:],
            "SOS with zero components": body[:sos] + b"\xff\xda\x00\x06\x00\x00\x3f\x00",
        }
        for label, bad in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.layout(bad))

    def test_header_walk_is_bounded(self):
        filler = _segment(0xE2, b"\x00" * 65000) * 4
        body = sequential_jpeg((64, 48))
        padded = body[:2] + filler + body[2:]
        with unittest.mock.patch.object(frame_cache, "MAX_JPEG_HEADER_BYTES", 100_000):
            self.assertIsNone(self.layout(padded))
        self.assertEqual(self.layout(padded), (False, 3, 1))

    def test_unreadable_layout_is_charged_as_worst_case(self):
        w, h = 640, 480
        with Image.open(io.BytesIO(solid_jpeg((w, h), subsampling=0))) as image:
            with unittest.mock.patch.object(frame_cache, "jpeg_scan_layout", return_value=None):
                self.assertEqual(frame_cache.buffered_coefficient_bytes(image), w * h * 2 * 3)

    def test_unreadable_sampling_factors_assume_four_full_res_components(self):
        class Fake:
            format = "JPEG"
            info = {"progressive": 1}
            size = (1000, 1000)
        self.assertEqual(frame_cache.buffered_coefficient_bytes(Fake()), 4 * 1000 * 1000 * 2)

    def test_estimator_leaves_the_file_position_alone(self):
        with Image.open(io.BytesIO(sequential_jpeg((64, 48)))) as image:
            image.fp.seek(17)
            frame_cache.buffered_coefficient_bytes(image)
            self.assertEqual(image.fp.tell(), 17)


def mpo_bytes(size, second=(300, 600), **save):
    """A real Pillow MPO (primary + one extra picture, as phones write for
    depth maps / previews). Image.open reports format 'MPO' for these."""
    output = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(
        output, "MPO", save_all=True, append_images=[Image.new("RGB", second, (200, 0, 0))],
        quality=80, **save)
    return output.getvalue()


class TestMpoPhotos(unittest.TestCase):
    """MpoImageFile subclasses JpegImageFile: draft() and the coefficient
    estimate apply to its primary picture exactly as to a JPEG. Treating MPO as
    a full-size non-JPEG decode refused ordinary >8 MP phone photos."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, body):
        path = self.dir / name
        path.write_bytes(body)
        return path

    def record_frame_seeks(self):
        from PIL import MpoImagePlugin
        seeks = []
        original = MpoImagePlugin.MpoImageFile.seek

        def seek(image, frame):
            seeks.append(frame)
            return original(image, frame)
        return seeks, unittest.mock.patch.object(MpoImagePlugin.MpoImageFile, "seek", seek)

    def test_large_mpo_validates_and_renders_bounded_primary_only(self):
        path = self.write("phone.jpg", mpo_bytes((4000, 3000)))
        with Image.open(path) as image:  # fixture really is a 12 MP multi-picture file
            self.assertEqual((image.format, image.size, image.n_frames), ("MPO", (4000, 3000), 2))
        with frame_cache.open_image(path) as image:
            self.assertEqual(image.format, "MPO")
            self.assertEqual(frame_cache.buffered_coefficient_bytes(image), 0)
        seeks, patch = self.record_frame_seeks()
        sizes = []
        with patch, recording_load(sizes):
            frame_cache.check_image(path)
            self.assertTrue(frame_cache.valid_image(path))
            sys.path.insert(0, str(PI_ZERO))
            self.addCleanup(sys.path.remove, str(PI_ZERO))
            viewer = _load("viewer")
            fb = io.BytesIO()
            self.assertTrue(viewer.render_safely(path, {"xres": 80, "yres": 60, "bpp": 32}, fb, None))
        self.assertTrue(all(frame == 0 for frame in seeks), seeks)
        self.assertTrue(sizes)
        for width, height in sizes:  # draft-reduced, primary's 4:3 aspect (extra picture is 1:2)
            self.assertLessEqual(width * height, 1000 * 750)
            self.assertEqual(width * 3, height * 4)
        self.assertEqual(len(fb.getvalue()), 80 * 60 * 4)

    def test_large_progressive_mpo_is_still_refused_by_the_coefficient_cap(self):
        path = self.write("prog.jpg", mpo_bytes((6000, 4000), progressive=True, subsampling=0))
        with Image.open(path) as image:
            self.assertEqual(image.format, "MPO")
            self.assertTrue(image.info.get("progressive") or image.info.get("progression"))
        loads = assert_not_decoded()
        with loads[0] as file_load, loads[1] as image_load, loads[2]:
            with self.assertRaises(ValueError) as caught:
                frame_cache.check_image(path)
        file_load.assert_not_called()
        image_load.assert_not_called()
        self.assertIn("coefficient", str(caught.exception))


class TestViewerRenderCache(unittest.TestCase):
    """I3: slides last ~23 min, so the render cache buys nothing but memory."""

    def setUp(self):
        sys.path.insert(0, str(PI_ZERO))
        self.addCleanup(sys.path.remove, str(PI_ZERO))
        self.viewer = _load("viewer")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def render_distinct(self, count, info, cache):
        for i in range(count):
            path = self.dir / f"p{i}.png"
            path.write_bytes(tiny_image(color=(i * 40 % 256, 0, 0)))
            self.assertTrue(self.viewer.render_safely(path, info, io.BytesIO(), cache))

    def test_1080p_cache_stays_within_byte_cap(self):
        cap = self.viewer.RENDER_CACHE_MAX_BYTES
        self.assertLessEqual(cap, 16 * 1024 * 1024)
        cache = {}
        self.render_distinct(5, {"xres": 1920, "yres": 1080, "bpp": 32}, cache)
        self.assertLessEqual(sum(len(v) for v in cache.values()), cap)
        self.assertLessEqual(len(cache), 2)

    def test_eviction_keeps_most_recent_and_oversized_frames_are_not_cached(self):
        info = {"xres": 8, "yres": 6, "bpp": 32}
        frame = 8 * 6 * 4
        with unittest.mock.patch.object(self.viewer, "RENDER_CACHE_MAX_BYTES", frame * 2):
            cache = {}
            self.render_distinct(5, info, cache)
            self.assertEqual(len(cache), 2)
            self.assertEqual([Path(k[0]).name for k in cache], ["p3.png", "p4.png"])
        with unittest.mock.patch.object(self.viewer, "RENDER_CACHE_MAX_BYTES", frame - 1):
            cache = {}
            self.render_distinct(2, info, cache)
            self.assertEqual(cache, {})

    def test_cache_hit_still_redraws(self):
        info = {"xres": 8, "yres": 6, "bpp": 32}
        path = self.dir / "a.png"
        path.write_bytes(tiny_image())
        cache = {}
        self.assertTrue(self.viewer.render_safely(path, info, io.BytesIO(), cache))
        fb = io.BytesIO()
        self.assertTrue(self.viewer.render_safely(path, info, fb, cache))
        self.assertEqual(len(fb.getvalue()), 8 * 6 * 4)


class TestViewerDraft(unittest.TestCase):
    def viewer(self):
        sys.path.insert(0, str(PI_ZERO))
        self.addCleanup(sys.path.remove, str(PI_ZERO))
        return _load("viewer")

    def test_oversized_non_jpeg_holds_previous_frame_without_decoding(self):
        from PIL import ImageFile
        viewer = self.viewer()
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("big.png", large_png_header(4000, 3000)),
                               ("big.webp", large_webp_header(4000, 3000))):
                with self.subTest(name):
                    path = Path(tmp) / name
                    path.write_bytes(body)
                    fb = io.BytesIO(b"previous frame")
                    with unittest.mock.patch.object(ImageFile.ImageFile, "load") as file_load, \
                            unittest.mock.patch.object(Image.Image, "load") as image_load, \
                            unittest.mock.patch.object(WebPImagePlugin.WebPImageFile, "load") as webp_load:
                        self.assertFalse(viewer.render_safely(path, {"xres": 8, "yres": 6, "bpp": 32}, fb, {}))
                    file_load.assert_not_called()
                    image_load.assert_not_called()
                    webp_load.assert_not_called()
                    self.assertEqual(fb.getvalue(), b"previous frame")

    def test_small_png_webp_gif_still_play(self):
        viewer = self.viewer()
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("PNG", "WEBP", "GIF"):
                with self.subTest(fmt):
                    path = Path(tmp) / f"ok.{fmt.lower()}"
                    path.write_bytes(tiny_image(format=fmt))
                    fb = io.BytesIO()
                    self.assertTrue(viewer.render_safely(path, {"xres": 8, "yres": 6, "bpp": 32}, fb, {}))
                    self.assertEqual(len(fb.getvalue()), 8 * 6 * 4)

    def test_render_uses_screen_sized_draft(self):
        viewer = self.viewer()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.jpg"
            path.write_bytes(noise_jpeg((640, 480)))
            sizes = []
            fb = io.BytesIO()
            with recording_load(sizes):
                self.assertTrue(viewer.render_safely(path, {"xres": 80, "yres": 60, "bpp": 32}, fb, {}))
            self.assertEqual(sizes[0], (80, 60))
            self.assertEqual(len(fb.getvalue()), 80 * 60 * 4)


class TestLocalName(unittest.TestCase):
    def test_changed_etag_gets_immutable_generation_but_url_refresh_does_not(self):
        photo = {"key": "photos/a.jpg", "etag": "one", "url": "https://x?sig=1"}
        name = frame_cache.local_name(photo)
        self.assertEqual(name, frame_cache.local_name({**photo, "url": "https://x?sig=2"}))
        self.assertNotEqual(name, frame_cache.local_name({**photo, "etag": "two"}))
        self.assertNotEqual(name, frame_cache.local_name({**photo, "key": "other/a.jpg"}))

    def test_same_basename_different_folders_get_different_names(self) -> None:
        """The bug this module exists for: basenames collide, keys do not."""
        a = frame_cache.local_name({"id": "IMG_0274.jpeg", "key": "photos/IMG_0274.jpeg"})
        b = frame_cache.local_name(
            {"id": "IMG_0274.jpeg", "key": "photos/04_phone_modern/IMG_0274.jpeg"}
        )
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("_IMG_0274.jpeg"))
        self.assertTrue(b.endswith("_IMG_0274.jpeg"))

    def test_name_is_stable_across_url_refreshes(self) -> None:
        """Presigned URLs are re-signed every publish; the cache must not churn."""
        key = "photos/01_scans/2008/SCAN0060.JPG"
        first = frame_cache.local_name({"id": "SCAN0060.JPG", "key": key, "url": "https://x/a?sig=1"})
        second = frame_cache.local_name({"id": "SCAN0060.JPG", "key": key, "url": "https://x/a?sig=2"})
        self.assertEqual(first, second)

    def test_schema_1_entry_falls_back_to_id(self) -> None:
        name = frame_cache.local_name({"id": "SCAN0060.JPG"})
        self.assertIsNotNone(name)
        self.assertEqual(name, frame_cache.local_name({"id": "SCAN0060.JPG"}))

    def test_key_wins_over_id(self) -> None:
        keyed = frame_cache.local_name({"id": "x.jpg", "key": "photos/a/x.jpg"})
        id_only = frame_cache.local_name({"id": "x.jpg"})
        self.assertNotEqual(keyed, id_only)

    def test_unusable_entries_return_none(self) -> None:
        for entry in ({}, {"id": ""}, {"id": "   ", "key": ""}, None, "nope"):
            self.assertIsNone(frame_cache.local_name(entry))

    def test_name_can_never_escape_the_images_directory(self) -> None:
        hostile = [
            {"key": "../../etc/passwd"},
            {"key": "photos/../../../root/.ssh/authorized_keys"},
            {"id": "/etc/shadow"},
            {"key": "photos/a/"},
            {"id": ".."},
        ]
        for entry in hostile:
            name = frame_cache.local_name(entry)
            self.assertIsNotNone(name, entry)
            self.assertNotIn("/", name, entry)
            self.assertNotIn("\\", name, entry)
            self.assertNotIn("..", name, entry)
            resolved = (Path("/opt/frame/images") / name).resolve()
            self.assertEqual(resolved.parent, Path("/opt/frame/images"))

    def test_extension_survives_a_very_long_basename(self) -> None:
        long_name = "x" * 300 + ".jpeg"
        name = frame_cache.local_name({"key": f"photos/{long_name}"})
        self.assertTrue(name.endswith(".jpeg"), name)
        self.assertLess(len(name), 80)

    def test_non_ascii_basename_is_transliterated_to_safe_chars(self) -> None:
        name = frame_cache.local_name({"key": "photos/café ☕.jpg"})
        self.assertTrue(name.isascii(), name)
        self.assertTrue(name.endswith(".jpg"), name)


class TestLegacyName(unittest.TestCase):
    def test_returns_id_unchanged(self) -> None:
        self.assertEqual(frame_cache.legacy_name({"id": "IMG_0274.jpeg"}), "IMG_0274.jpeg")

    def test_refuses_path_separators_and_dots(self) -> None:
        for pid in ("../x.jpg", "a/b.jpg", "a\\b.jpg", ".", "..", ""):
            self.assertIsNone(frame_cache.legacy_name({"id": pid}), pid)


class TestPruneDir(unittest.TestCase):
    def test_removes_unclaimed_files_and_reports_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.jpg").write_bytes(tiny_image("red"))
            (root / "drop.jpg").write_bytes(tiny_image("green"))
            (root / "also-drop.jpg").write_bytes(tiny_image("blue"))

            removed, freed = frame_cache.prune_dir(root, {"keep.jpg"})

            self.assertEqual(removed, 2)
            self.assertEqual(freed, len(tiny_image("green")) + len(tiny_image("blue")))
            self.assertEqual({p.name for p in root.iterdir()}, {"keep.jpg"})

    def test_leaves_subdirectories_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "inner.jpg").write_bytes(tiny_image())

            removed, freed = frame_cache.prune_dir(root, set())

            self.assertEqual((removed, freed), (0, 0))
            self.assertTrue((root / "sub" / "inner.jpg").exists())

    def test_missing_directory_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(frame_cache.prune_dir(Path(tmp) / "nope", set()), (0, 0))

    def test_empty_keep_set_clears_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.tmp").write_bytes(b"1")
            (root / "b.tmp").write_bytes(b"2")
            self.assertEqual(frame_cache.prune_dir(root, set()), (2, 2))
            self.assertEqual(list(root.iterdir()), [])


class TestWantedNames(unittest.TestCase):
    def test_skips_unusable_entries(self) -> None:
        names = frame_cache.wanted_names(
            [{"key": "photos/a.jpg"}, {}, {"id": "b.jpg"}, {"id": ""}]
        )
        self.assertEqual(len(names), 2)

    def test_empty_and_none_are_empty_sets(self) -> None:
        self.assertEqual(frame_cache.wanted_names([]), set())
        self.assertEqual(frame_cache.wanted_names(None), set())


if __name__ == "__main__":
    unittest.main()
