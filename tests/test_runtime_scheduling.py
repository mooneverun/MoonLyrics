"""Idle sampling backs off while animation, capture recovery and drag remain live."""
import sys
import unittest
from unittest.mock import patch

if sys.platform == 'win32':
    from PyQt6.QtCore import QEvent, QPoint, QTimer
    from PyQt6.QtWidgets import QApplication
    from moonlyrics import windows as native
    from moonlyrics.styles import DEFAULTS
    from moonlyrics.playback import PlaybackSnapshot
    from moonlyrics.lyrics import Lyrics, LyricLine


@unittest.skipUnless(sys.platform == 'win32', 'Windows scheduling')
class RuntimeSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.control = native.Controls(dict(DEFAULTS, fps=120), save=False, media=False)
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.glass = self.control.glass
        self.glass.paused = True

    def tearDown(self):
        for timer in self.control.findChildren(QTimer): timer.stop()
        self.glass.close(); self.glass.deleteLater(); self.control.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_static_idle_backs_off_but_motion_and_drag_keep_the_selected_limit(self):
        self.glass.background_changed_at = 100.
        with patch.object(native.time, 'monotonic', return_value=102.):
            self.assertEqual(self.glass.refresh_interval(), 67)
            self.glass.playback_active = True
            self.assertEqual(self.glass.refresh_interval(), 33)
            self.glass.background_changed_at = 101.8
            self.assertEqual(self.glass.refresh_interval(), 8)
            self.glass.background_changed_at = 100.
            self.glass.drag_anchor = QPoint(0, 0)
            self.assertEqual(self.glass.refresh_interval(), 8)
            self.glass.settings['fps'] = 15
            self.assertEqual(self.glass.refresh_interval(), 67)

    def test_idle_stops_animation_and_resume_restarts_it_without_resuming_manual_capture_pause(self):
        self.control.set_playback(PlaybackSnapshot(status='paused', title='Test'))
        self.assertFalse(self.control.media_timer.isActive())
        self.control.set_playback(PlaybackSnapshot(status='playing', title='Test', observed_at=native.time.perf_counter()))
        self.assertTrue(self.control.media_timer.isActive())
        self.assertEqual(self.control.media_timer.interval(), 33)
        self.assertTrue(self.glass.paused)
        self.assertFalse(self.glass.timer.isActive())

    def test_hidden_settings_do_not_refresh_labels_while_lyrics_advance(self):
        self.control.hide()
        self.glass.view.set_lyrics(Lyrics((LyricLine(0, 'Moonlight'), LyricLine(3, 'Next'))))
        with patch.object(self.glass.view, 'set_lyric_time', wraps=self.glass.view.set_lyric_time) as lyrics, \
                patch.object(self.control.media_time, 'setText') as label, \
                patch.object(self.control.media_progress, 'setValue') as progress:
            self.control.advance_display()
            lyrics.assert_called_once(); label.assert_not_called(); progress.assert_not_called()
        with patch.object(self.control, 'render_stats') as stats:
            self.control.sample_timer.timeout.emit()
            stats.assert_not_called()
        self.assertTrue(self.control.samples)

    def test_hidden_settings_skip_progress_updates_from_playback_snapshots(self):
        self.control.hide()
        with patch.object(self.control.media_progress,'setValue') as progress:
            self.control.set_playback(PlaybackSnapshot(status='playing',title='Test',artist='MoonLyrics',
                position=21,duration=180,observed_at=native.time.perf_counter()))
            progress.assert_not_called()
            with patch.object(self.control,'isVisible',return_value=True):
                self.control.update_playback_display()
            progress.assert_called_once()

    def test_cooldown_does_not_replace_retry_backoff(self):
        self.glass.paused = False
        self.glass.retry_delay = 5000
        self.glass.timer.start(5000)
        self.glass.update_refresh_schedule(wake=True)
        self.assertEqual(self.glass.timer.interval(), 5000)

    def test_position_only_refresh_does_not_restart_periodic_desktop_sampling(self):
        self.glass.paused = False
        self.glass.timer.stop()
        self.glass.view.background_changed = False
        with patch.object(self.glass, 'isVisible', return_value=True), \
                patch.object(self.glass.view, 'take_frame', return_value=True) as take:
            self.glass.refresh(moving=True)
        take.assert_called_once_with(refresh=False)
        self.assertFalse(self.glass.timer.isActive())

    def test_first_changed_frame_restores_the_high_refresh_limit(self):
        self.glass.paused = False
        self.glass.background_changed_at = native.time.monotonic()-5
        def changed():
            self.glass.view.background_changed = True
            return True
        with patch.object(self.glass, 'isVisible', return_value=True), \
                patch.object(self.glass.view, 'take_frame', side_effect=changed):
            self.glass.update_refresh_schedule()
            self.assertEqual(self.glass.timer.interval(), 67)
            self.glass.refresh()
            self.assertEqual(self.glass.timer.interval(), 8)
