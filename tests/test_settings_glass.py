"""Shared theme, bounded settings capture, and independent recovery."""
import sys
import unittest
from unittest.mock import patch

if sys.platform == 'win32':
    from PyQt6.QtCore import QEvent, Qt, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from moonlyrics import windows as native
    from moonlyrics.styles import DEFAULTS


@unittest.skipUnless(sys.platform == 'win32', 'Windows settings glass')
class SettingsGlassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.user32.SetProcessDpiAwarenessContext(native.C.c_void_p(-4))
        fmt=native.QSurfaceFormat();fmt.setVersion(3,3)
        fmt.setProfile(native.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setAlphaBufferSize(8);fmt.setDepthBufferSize(0);fmt.setStencilBufferSize(0)
        fmt.setSamples(0);fmt.setSwapInterval(0);native.QSurfaceFormat.setDefaultFormat(fmt)
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.control = native.Controls(dict(DEFAULTS), save=False, media=False)
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.control.glass.paused = True
        self.backdrop = self.control.settings_backdrop

    def tearDown(self):
        self.backdrop.shutdown()
        self.control.glass.close(); self.control.glass.deleteLater()
        self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_system_theme_signal_changes_both_surfaces_and_explicit_choice_overrides_it(self):
        self.control.appearance_buttons['system'].click()
        for dark in (False, True):
            with patch.object(native, 'system_is_dark', return_value=dark):
                self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
                self.assertEqual(self.control.night, dark)
                self.assertEqual(self.backdrop.view.night, dark)
        self.control.appearance_buttons['day'].click()
        with patch.object(native, 'system_is_dark', return_value=True):
            self.control.sample()
            self.assertFalse(self.backdrop.view.night)
            self.assertFalse(self.control.night)
        self.assertTrue(self.control.glass.paused)

    def test_settings_use_the_shared_renderer_with_panel_corners_and_small_text_protection(self):
        self.assertIsInstance(self.backdrop.view, native.GlassView)
        self.assertFalse(self.backdrop.view.show_text)
        self.assertEqual(self.backdrop.view.corner_radius, 30.)
        self.assertGreaterEqual(self.backdrop.view.material_tone((), False), .56)
        self.assertGreaterEqual(self.backdrop.view.material_tone((), True), .40)
        self.assertEqual(self.control.glass.view.corner_radius, 0.)
        self.assertIs(self.control.title_bar.parentWidget(),self.backdrop.view)
        self.assertIs(self.control.scroll.parentWidget(),self.backdrop.view)
        self.assertFalse(self.backdrop.view.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_approved_mockup_layout_and_ui_fonts(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QFrame
        c=self.control;c.resize(624,992);c.move(-30000,-30000);c.show();self.app.processEvents()
        expected=[(c.title_bar,0,0,624,80),(c.findChild(QFrame,'appearanceSegment'),346,88,252,42),
                  (c.findChild(QFrame,'nowPlayingCard'),26,170,572,155),
                  (c.font_choice,348,683,250,38),(c.sliders['width'],130,735,394,21),
                  (c.sliders['height'],130,770,394,21),(c.fps,475,884,123,38)]
        for widget,x,y,w,h in expected:
            point=widget.mapTo(c,QPoint(0,0))
            for actual,target in zip((point.x(),point.y(),widget.width(),widget.height()),(x,y,w,h)):
                self.assertLessEqual(abs(actual-target),2,(widget.objectName(),actual,target))
        self.assertEqual(c.scroll.verticalScrollBar().maximum(),0)
        self.assertIn('Microsoft YaHei UI',c.font().families())
        self.assertEqual(len({button.mapTo(c,QPoint(0,0)).y() for button in c.position_buttons}),1)

    def test_hidden_settings_stop_the_sampler_and_preserve_the_current_lyric_window(self):
        self.control.move(-30000, -30000)
        with patch.object(self.backdrop, 'refresh'):
            self.control.show(); self.app.processEvents()
            self.backdrop.timer.start(33)
            with patch.object(self.backdrop.capture, 'close') as close:
                self.control.hide()
                self.assertFalse(self.backdrop.timer.isActive())
                close.assert_called_once()
        self.assertTrue(self.control.glass.paused)

    def test_settings_capture_access_denied_retries_without_pausing_lyrics(self):
        self.control.glass.paused = False
        with patch.object(self.control, 'isVisible', return_value=True), \
                patch.object(self.backdrop.view, 'take_frame', side_effect=OSError('access denied')):
            self.backdrop.refresh()
        self.assertEqual(self.backdrop.retry_delay, 1000)
        self.assertFalse(self.control.glass.paused)
        self.assertIsNone(self.control.error)
        with patch.object(self.control, 'isVisible', return_value=True), \
                patch.object(self.backdrop.capture, 'reset') as reset, \
                patch.object(self.backdrop.view, 'take_frame', return_value=True):
            self.backdrop.refresh()
        reset.assert_called_once()
        self.assertEqual(self.backdrop.retry_delay, 0)

    def test_render_failure_leaves_controls_in_a_readable_theme(self):
        self.control.appearance_buttons['night'].click()
        self.backdrop.renderer_failed('test graphics failure')
        self.app.processEvents()
        self.assertFalse(self.control.backdrop_active)
        self.assertFalse(self.backdrop.timer.isActive())
        self.assertEqual(self.control.material[3], 255)
        self.assertTrue(self.control.night)
        self.assertIsNone(self.control.error)
        self.assertIs(self.control.title_bar.parentWidget(),self.control)
        self.assertIs(self.control.scroll.parentWidget(),self.control)
        self.assertFalse(self.control.title_bar.isHidden())

    def test_real_screen_keeps_the_panel_after_cold_start_theme_switch_and_restore(self):
        from PIL import ImageGrab
        from PyQt6.QtGui import QColor,QPainter
        from PyQt6.QtWidgets import QWidget
        class Background(QWidget):
            def paintEvent(self,event):
                painter=QPainter(self);painter.fillRect(self.rect(),QColor('#297ebb'));painter.end()
        back=Background();back.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        bounds=self.app.primaryScreen().availableGeometry();back.setGeometry(bounds);back.show()
        c=self.control;c.move(bounds.left()+100,bounds.top()+30);c.show();c.raise_();c.activateWindow()
        self.glass=c.glass;self.glass.move(bounds.right()-self.glass.width()-30,bounds.top()+50);self.glass.show()
        def screen_pixel():
            awareness=native.api(native.user32,'SetThreadDpiAwarenessContext',native.C.c_void_p,native.C.c_void_p)
            previous=awareness(native.C.c_void_p(-4))
            try:
                origin=native.W.POINT();native.client_origin(int(c.winId()),native.C.byref(origin))
                scale=c.devicePixelRatioF()
                # Do not grab a widget/FBO here: that can hide an on-screen stacking bug.
                return ImageGrab.grab(bbox=(origin.x+round(12*scale),origin.y+round(200*scale),
                    origin.x+round(12*scale)+1,origin.y+round(200*scale)+1),all_screens=True).getpixel((0,0))[:3]
            finally:
                awareness(previous)
        try:
            for mode in ('day','night'):
                c.appearance_buttons[mode].click()
                for _ in range(40):
                    QTest.qWait(50)
                    if c.backdrop_active and self.backdrop.view.frame_geometry is not None:
                        break
                QTest.qWait(150)
                first=screen_pixel()
                self.assertGreater(sum(abs(a-b) for a,b in zip(first,(41,126,187))),65,
                    (mode,first,c.pos(),c.devicePixelRatioF(),self.backdrop.view.geometry(),self.backdrop.view.ready,self.backdrop.view.frames,self.backdrop.view.frame_geometry))
                c.sample();QTest.qWait(80)
                self.assertEqual(screen_pixel(),first)
                target=c.appearance_buttons[mode]
                self.assertIs(c.childAt(target.mapTo(c,target.rect().center())),target)
                c.minimize_settings();QTest.qWait(30);c.restore_settings();QTest.qWait(450)
                for _ in range(20):
                    if screen_pixel()==first:break
                    QTest.qWait(50)
                self.assertEqual(screen_pixel(),first)
                worker=c.glass.capture.worker
                c.minimize_settings();QTest.qWait(3250)
                self.assertTrue(self.backdrop.view.ready)
                self.assertEqual(self.backdrop.view.texture_size,(0,0))
                self.assertIs(c.title_bar.parentWidget(),self.backdrop.view)
                self.assertIs(c.glass.capture.worker,worker)
                self.assertTrue(c.testAttribute(Qt.WidgetAttribute.WA_WState_Created))
                c.sample();c.restore_settings()
                for _ in range(40):
                    QTest.qWait(50)
                    if c.backdrop_active and screen_pixel()==first:break
                self.assertEqual(screen_pixel(),first,(c.backdrop_active,self.backdrop.view.ready,
                    self.backdrop.view.isVisible(),self.backdrop.view.frames,self.backdrop.view.texture_size,
                    self.backdrop.view.geometry(),c.title_bar.parentWidget() is self.backdrop.view))
                self.assertIs(c.title_bar.parentWidget(),self.backdrop.view)
                self.assertFalse(self.backdrop.release_timer.isActive())
            self.backdrop.renderer_failed('injected screen fallback');self.app.processEvents()
            for _ in range(20):
                QTest.qWait(50)
                if screen_pixel()==c.material[:3]:break
            self.assertEqual(screen_pixel(),c.material[:3])
            self.assertTrue(c.runtime_button.isVisible())
        finally:
            c.hide();back.close();back.deleteLater()

    def test_static_settings_sampling_slows_down_but_motion_keeps_its_rate(self):
        self.backdrop.changed_at=native.time.monotonic()-2
        self.assertEqual(self.backdrop.interval(),200)
        self.control.backdrop_dragging=True
        self.assertEqual(self.backdrop.interval(),round(1000/min(self.control.settings['fps'],30)))
