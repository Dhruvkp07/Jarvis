import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class DesktopUITest(unittest.TestCase):
    def test_settings_and_task_controls_construct(self):
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QApplication

        from jarvis.app import Window
        from jarvis.config import Settings

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            win = Window(Path(folder))
            self.assertEqual(win.windowTitle(), "Jarvis")
            self.assertFalse(win.pause_button.isEnabled())
            win.model.setCurrentText("local-test")
            self.assertTrue(win._save())
            self.assertEqual(Settings.load(Path(folder)).model, "local-test")
            self.assertIn("Ollama", win.active_provider.text())
            self.assertIn("Ctrl+Shift+F10", win.hotkey_label.text())
            win.close()
            win.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
