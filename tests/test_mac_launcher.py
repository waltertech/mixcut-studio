"""The installed app must never attach its new UI to an old server process."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import maclaunch


class MacLauncherTests(unittest.TestCase):
    def test_native_window_supports_confirmation_text_input(self):
        source = (Path(__file__).resolve().parent.parent / 'macos' / 'build_tools' / 'window.swift').read_text(encoding='utf-8')
        self.assertIn('runJavaScriptTextInputPanelWithPrompt', source)
        self.assertIn('data.api_protocol === 16', source)

    def test_same_version_without_protocol_is_still_incompatible(self):
        old = {'version': '1.6.0', 'batches': [], 'ffmpeg_available': True}
        self.assertFalse(maclaunch._compatible(old))
        self.assertFalse(maclaunch._compatible({**old, 'api_protocol': 3}))
        self.assertTrue(maclaunch._compatible({**old, 'api_protocol': 16}))

    def test_idle_old_server_is_stopped_before_new_window_opens(self):
        old = {'version': '1.4.4', 'api_protocol': 3, 'batches': [], 'ffmpeg_available': True}
        current = {**old, 'version': '1.6.0', 'api_protocol': 16}
        with tempfile.TemporaryDirectory() as directory:
            events = []
            with patch('mixcut.runtime.data_root', return_value=Path(directory)), \
                 patch('mixcut.runtime.activate_bundled_tools'), \
                 patch.object(maclaunch, '_readiness', side_effect=[old, current]), \
                 patch.object(maclaunch, '_stop', side_effect=lambda: events.append('stop') or 0), \
                 patch.object(maclaunch, '_ffmpeg_from_path', return_value=True), \
                 patch.object(maclaunch, '_spawn', side_effect=lambda _: events.append('spawn') or Mock()), \
                 patch.object(maclaunch, '_open_client', side_effect=lambda: events.append('window') or 0):
                self.assertEqual(0, maclaunch.main([]))
            self.assertEqual(['stop', 'spawn', 'window'], events)

    def test_running_old_tasks_prevent_automatic_shutdown(self):
        old = {'version': '1.4.4', 'api_protocol': 3, 'ffmpeg_available': True,
               'batches': [{'id': 'job', 'status': 'running'}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch('mixcut.runtime.data_root', return_value=Path(directory)), \
                 patch('mixcut.runtime.activate_bundled_tools'), \
                 patch.object(maclaunch, '_readiness', return_value=old), \
                 patch.object(maclaunch, '_stop') as stop:
                self.assertEqual(1, maclaunch.main([]))
                stop.assert_not_called()

    def test_hidden_active_work_also_prevents_automatic_shutdown(self):
        old = {'version': '1.4.4', 'api_protocol': 3, 'ffmpeg_available': True,
               'batches': [], 'active_work': True}
        self.assertTrue(maclaunch._active_tasks(old))


if __name__ == '__main__':
    unittest.main()
