"""Render known textures through the actual GPU pipeline, without desktop capture."""
import ctypes as C
import math
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if sys.platform == "win32":
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QImage
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
    from moonlyrics import windows as native
    from moonlyrics.styles import DEFAULTS, normalized


@unittest.skipUnless(sys.platform == "win32", "Windows OpenGL material")
class GlassMaterialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = QWidget()
        self.window.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.capture = SimpleNamespace(size=(0, 0), bounds=(0., 0., 1., 1.), revision=0, capture_ms=0.)
        self.capture.capture = lambda *args, **kwargs: (self.width, self.height, self.pad)
        self.view = native.GlassView(normalized(dict(DEFAULTS, appearance_mode="day")), self.capture, self.window)
        self.view.show_text = False
        self.errors = []
        self.view.failed.connect(self.errors.append)
        layout = QVBoxLayout(self.window); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.window.resize(580, 80); self.window.move(-30000, -30000)
        self.window.show(); QTest.qWait(50)
        self.assertTrue(self.view.ready, self.errors)
        self.dpr = self.view.devicePixelRatioF()
        self.width, self.height = (round(v * self.dpr) for v in (580, 80))
        self.pad = math.ceil(80 * self.dpr)

    def tearDown(self):
        self.view.shutdown(); self.window.close(); self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def load_backdrop(self, backdrop):
        self.pixels = C.create_string_buffer(backdrop.tobytes("raw", "BGRX", 0, -1))
        self.capture.size = backdrop.size
        self.capture.bits = C.cast(self.pixels, C.c_void_p)
        self.capture.bounds = (.5 / backdrop.width, .5 / backdrop.height,
                               1 - .5 / backdrop.width, 1 - .5 / backdrop.height)
        self.capture.offset = (self.pad, self.pad)
        self.capture.revision += 1

    def read_frame(self):
        frame = self.view.grabFramebuffer().convertToFormat(QImage.Format.Format_RGBA8888)
        self.assertFalse(self.errors, self.errors)
        return Image.frombytes("RGBA", (frame.width(), frame.height()), frame.bits().asstring(frame.sizeInBytes()))

    def render(self, mode=None, color=None):
        size = (self.width + self.pad * 2, self.height + self.pad * 2)
        backdrop = Image.new("RGB", size, color or "#252a33")
        if color is None:
            draw = ImageDraw.Draw(backdrop)
            step = max(1, round(12 * self.dpr))
            for y in range(0, size[1], step):
                for x in range(0, size[0], step):
                    draw.rectangle((x, y, x + step - 1, y + step - 1),
                                   fill="#e3ddcc" if (x // step + y // step) % 2 else "#334f75")
        self.backdrop = backdrop
        self.load_backdrop(backdrop)
        if mode is not None:
            self.view.settings["appearance_mode"] = mode; self.view.update_appearance()
        self.view.take_frame()
        return self.read_frame()

    def region(self, image, box):
        return image.crop(tuple(round(v * self.dpr) for v in box))

    def test_cached_position_update_does_not_reupload_or_repeat_blur_or_lens(self):
        self.render()
        with patch.object(self.view.gl, "glTexSubImage2D", wraps=self.view.gl.glTexSubImage2D) as upload, \
                patch.object(self.view, "draw", wraps=self.view.draw) as draw:
            self.view.take_frame(); self.view.grabFramebuffer()
        upload.assert_not_called()
        self.assertFalse(any(call.args[0] in (self.view.blur_program, self.view.lens_program)
                             for call in draw.call_args_list))

    def test_unchanged_capture_does_not_request_another_repaint(self):
        self.render()
        with patch.object(self.view, 'update') as repaint:
            self.view.take_frame()
        repaint.assert_not_called()

    def test_native_top_down_pixels_match_the_original_glass_frame(self):
        size = (self.width+self.pad*2, self.height+self.pad*2)
        backdrop = Image.new('RGB', size)
        paint = ImageDraw.Draw(backdrop)
        for y in range(size[1]):
            paint.line((0, y, size[0], y), fill=(y%256, (y*3)%256, 110))
        paint.rectangle((self.pad+5, self.pad+5, self.pad+120, self.pad+40), fill='#12c0e0')
        self.load_backdrop(backdrop)
        self.view.take_frame()
        original = self.read_frame()
        tones = self.view.background_colors
        self.capture.top_down = True
        self.pixels = C.create_string_buffer(backdrop.tobytes('raw', 'BGRX', 0, 1))
        self.capture.bits = C.cast(self.pixels, C.c_void_p)
        self.capture.revision += 1
        self.view.take_frame()
        actual = self.read_frame()
        self.assertEqual(self.view.background_colors, tones)
        self.assertLessEqual(max(high for low, high in ImageChops.difference(original, actual).getextrema()), 1)

    def test_world_aligned_background_does_not_jump_when_the_capture_anchor_changes(self):
        size = (self.width+self.pad*2, self.height+self.pad*2)
        master = Image.new("RGB", (size[0]+40, size[1]+40))
        paint = ImageDraw.Draw(master)
        for y in range(0, master.height, 12):
            for x in range(0, master.width, 12):
                paint.rectangle((x, y, x+11, y+11), fill="#e3ddcc" if (x//12+y//12)%2 else "#334f75")
        origin = native.W.POINT()
        origin_fn = native.api(native.user32, "ClientToScreen", native.W.BOOL, native.W.HWND, C.POINTER(native.W.POINT))
        self.assertTrue(origin_fn(int(self.window.winId()), C.byref(origin)))
        for mode in ("day", "night"):
            self.view.settings["appearance_mode"] = mode; self.view.update_appearance()
            frames, tones = [], []
            for dx, dy in ((0, 0), (16, 12)):
                self.load_backdrop(master.crop((dx, dy, dx+size[0], dy+size[1])))
                self.capture.source_rect = (int(self.window.winId()), origin.x-self.pad+dx, origin.y-self.pad+dy, *size)
                self.capture.offset = (self.pad-dx, self.pad+dy)
                self.view.take_frame()
                frames.append(self.read_frame()); tones.append(self.view.tint_alpha)
            self.assertAlmostEqual(*tones)
            difference = ImageChops.difference(frames[0], frames[1])
            self.assertLess(max(ImageStat.Stat(difference).mean), 1, f"Anchor switch moved the {mode} background")
            self.assertLessEqual(max(high for low, high in difference.getextrema()), 2)

    def test_day_and_night_use_different_tones_and_repaint_a_retained_texture(self):
        day = self.render(mode="day", color="#808080")
        self.assertTrue(self.view.dark_text)
        with patch.object(self.view.gl, "glTexSubImage2D", wraps=self.view.gl.glTexSubImage2D) as upload, \
                patch.object(self.view, "draw", wraps=self.view.draw) as draw:
            self.view.settings["appearance_mode"] = "night"; self.view.update_appearance()
            night = self.read_frame()
        self.assertFalse(self.view.dark_text)
        upload.assert_not_called()
        self.assertFalse(any(call.args[0] is self.view.blur_program for call in draw.call_args_list))
        self.assertGreater(day.getpixel((self.width//2, self.height//2))[0],
                           night.getpixel((self.width//2, self.height//2))[0]+25)

    def test_large_settings_surface_keeps_small_panel_corners_and_the_shared_transmission(self):
        self.view.settings_surface = True
        self.view.corner_radius = 30.
        self.window.resize(580, 500); self.app.processEvents()
        self.width, self.height = (round(value*self.dpr) for value in (580, 500))
        for mode in ('day', 'night'):
            frame = self.render(mode=mode, color='#808080')
            self.assertEqual(frame.getpixel((round(5*self.dpr), self.height//2))[3], 255)
            self.assertEqual(frame.getpixel((round(3*self.dpr), round(3*self.dpr)))[3], 0)
            alpha = self.view.tint_alpha
            expected = 128*(.8 if mode == 'night' else 1)*(1-alpha)+(30 if mode == 'night' else 255)*alpha
            for channel in frame.getpixel((self.width//2, self.height//2))[:3]:
                self.assertAlmostEqual(channel, expected, delta=2)

    def test_glasswork_fixed_two_pixel_blur_runs_in_both_modes(self):
        for mode in ("day", "night"):
            with patch.object(self.view, "draw", wraps=self.view.draw) as draw:
                frame = self.render(mode=mode)
            self.assertEqual(sum(call.args[0] is self.view.blur_program for call in draw.call_args_list), 2)
            self.assertEqual(self.view.blur_key, 2*self.dpr)
            self.view.makeCurrent()
            q = self.view.fbos[-1].toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            actual = Image.frombytes("RGBA", (q.width(), q.height()), q.bits().asstring(q.sizeInBytes())).convert("RGB")
            expected = self.backdrop.filter(ImageFilter.GaussianBlur(2*self.dpr))
            inner = (self.pad+20, self.pad+10, self.pad+self.width-20, self.pad+self.height-10)
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(actual, expected).crop(inner)).mean), 2)

    def test_lens_map_matches_the_approved_neutral_caps_preview(self):
        self.render()
        self.view.makeCurrent()
        q = self.view.lens_fbo.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        # Pixel-centre reference values read from the approved browser preview:
        # 580x80, rim=23, Atrium profile radius=30, original power=2.6.
        # The straight centre retains the old lens; only the ends change.
        for x, y, red, green in ((290,0,128,248), (290,5,128,190), (290,12,128,145),
                                  (290,22,128,128), (290,40,128,128), (10,10,195,195),
                                  (25,5,139,195), (5,25,195,139), (579,40,8,128),
                                  (569,69,61,61), (10,5,206,228), (30,3,132,212)):
            actual = q.pixelColor(x, y)
            self.assertLessEqual(abs(actual.red()-red), 1, (x, y, actual.getRgb()))
            self.assertLessEqual(abs(actual.green()-green), 1, (x, y, actual.getRgb()))

    def test_centre_preserves_backdrop_channel_balance_without_extra_saturation(self):
        # On a flat backdrop, neutral glass is a scalar exposure plus neutral
        # tint. This catches hue shifts and channel clipping independently of
        # the implementation of the refraction and corner functions.
        for mode in ("day", "night"):
            for rgb in ((80, 154, 208), (208, 144, 80), (216, 104, 152), (128, 128, 128)):
                with self.subTest(mode=mode, color=rgb):
                    frame = self.render(mode=mode, color=rgb)
                    actual = frame.getpixel((self.width//2, self.height//2))[:3]
                    alpha = self.view.tint_alpha
                    exposure, tint = (.8, 30) if mode == "night" else (1., 255)
                    expected = [value*exposure*(1-alpha)+tint*alpha for value in rgb]
                    for value, reference in zip(actual, expected):
                        self.assertAlmostEqual(value, reference, delta=2)
                    if len(set(rgb)) == 1:
                        self.assertLessEqual(max(actual)-min(actual), 1)

    def test_full_end_profile_remains_consistent_when_the_bar_is_resized(self):
        for width, height in ((320, 80), (580, 80), (960, 240)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height); QTest.qWait(20)
                self.width, self.height = (round(v*self.dpr) for v in (width, height))
                frame = self.render(color="#808080")
                ratio = height/80
                # A fuller end removes the old, squarer upper corner, but its
                # midpoint still reaches the full width. Sample both ends.
                for x in (10*ratio, width-10*ratio-1):
                    self.assertLess(frame.getpixel((round(x*self.dpr), round(5*ratio*self.dpr)))[3], 10)
                for x in (3*ratio, width-3*ratio-1):
                    self.assertEqual(frame.getpixel((round(x*self.dpr), self.height//2))[3], 255)

    def test_liquid_edges_and_centre_both_update_with_the_backdrop(self):
        for mode in ("day", "night"):
            red = self.render(mode=mode, color="#d82828")
            blue = self.render(mode=mode, color="#2828d8")
            for image, channel in ((red, 0), (blue, 2)):
                for box in ((3, 32, 9, 48), (280, 32, 300, 48)):
                    mean = ImageStat.Stat(self.region(image.convert("RGB"), box)).mean
                    self.assertGreater(mean[channel] - mean[2-channel], 45)

    def test_corners_remain_clipped_in_both_modes(self):
        for mode in ("day", "night"):
            frame = self.render(mode=mode, color="#808080")
            self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0, 0))
            self.assertEqual(frame.getpixel((self.width//2, self.height//2))[3], 255)


if __name__ == "__main__":
    unittest.main()
