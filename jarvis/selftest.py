"""Exercise packaged imports, the desktop window and the browser driver with local inputs."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from jarvis.app import Window
from jarvis.browser import Browser


def run(report: Path):
    app = QApplication.instance() or QApplication([])
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        window = Window(root)
        window.show()
        app.processEvents()
        browser = Browser(root / "profile", lambda: None, headless=True)
        try:
            if window.windowTitle() != "Jarvis":
                raise RuntimeError("Desktop window did not initialize.")
            # The probe uses the same packaged Playwright driver as the task worker.
            page = browser.start_page("about:blank")
            page.set_content(
                "<label>Reference<input></label><button>Apply</button><output></output>"
            )
            page.evaluate("""() => document.querySelector('button').onclick = () => {
                document.querySelector('output').textContent = document.querySelector('input').value
            }""")
            page.get_by_label("Reference").fill("Q-107")
            page.get_by_role("button", name="Apply").click()
            if page.locator("output").inner_text() != "Q-107":
                raise RuntimeError("Browser result did not match.")
            report.write_text(
                json.dumps({"passed": True, "browser_result": "Q-107"}), encoding="utf-8"
            )
            return 0
        except Exception as exc:
            report.write_text(
                json.dumps({"passed": False, "error": type(exc).__name__}), encoding="utf-8"
            )
            return 1
        finally:
            browser.close()
            window.close()
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
