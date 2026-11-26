"""Interactive Windows 11 integration checks with local data and a real Chrome window."""

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from jarvis.browser import Browser
from jarvis.control import Control, Interrupted
from jarvis.native import Native
from jarvis.wininput import InputMonitor, enable_dpi_awareness
from tests.fixtures import fixture_server


def main():
    if sys.platform != "win32":
        raise SystemExit("This check requires an interactive Windows 11 session.")
    enable_dpi_awareness()
    with TemporaryDirectory() as tmp, fixture_server() as site:
        root = Path(tmp)
        browser = Browser(root / "ChromeProfile", lambda: None)
        native = Native(lambda: None, root / "ChromeProfile")
        hotkey = Event()
        takeover = Event()
        monitor = InputMonitor(hotkey.set, takeover.set)
        try:
            native.launch("notepad")
            controls = native.observe()["controls"]
            editor = next(
                key
                for key, data in controls.items()
                if data["role"] == "Document" or data["automation_id"] in {"15", "TextEditor"}
            )
            note = root / "Jarvis_smoke.txt"
            native.fill(editor, "Jarvis Windows smoke\nQ-107, café")
            print(native.save_notepad(note))

            page = browser.start_page(site)
            state = browser.observe()
            file_field = next(key for key, data in state.controls.items() if data["type"] == "file")
            browser.click(file_field)
            native.open_dialog_file(note)
            names = page.locator("input[type=file]").evaluate(
                "el => Array.from(el.files, f => f.name)"
            )
            if names != [note.name]:
                raise AssertionError("The native dialog did not select the test file.")
            print("Chrome verified a file chosen through the Windows Open dialog.")

            if not monitor.start():
                raise AssertionError(
                    "Could not register global hooks and the stop shortcut. Close Jarvis first."
                )
            control = Control()
            old = control.wait_ready()
            control.pause()
            control.resume()
            try:
                control.check(old)
            except Interrupted:
                print("Pause and resume invalidated an outstanding action.")
            else:
                raise AssertionError("The paused action was still valid.")
            takeover.clear()
            print("With Chrome focused, move the mouse and press Ctrl+Shift+F10 within 25 seconds.")
            if not hotkey.wait(timeout=25):
                raise AssertionError("The emergency stop shortcut was not observed.")
            if not takeover.is_set():
                raise AssertionError("Physical keyboard/mouse takeover was not observed.")
            print("Global emergency shortcut and manual input were observed outside Jarvis.")
            time.sleep(0.2)
        finally:
            monitor.close()
            browser.close()
            native.close()


if __name__ == "__main__":
    main()
