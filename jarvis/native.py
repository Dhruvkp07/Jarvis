"""Bounded Windows UI Automation and physical-coordinate fallback."""

import ctypes as c
import os
import subprocess
import sys
import time
from contextlib import suppress
from ctypes import wintypes as w
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from jarvis.browser import StepError
from jarvis.files import read_notepad

APPS = {
    "notepad": ("notepad.exe", {"notepad.exe"}),
    "calculator": ("calc.exe", {"calculatorapp.exe", "calculator.exe", "calc.exe"}),
    "explorer": ("explorer.exe", {"explorer.exe"}),
    "paint": ("mspaint.exe", {"mspaint.exe", "paintapp.exe"}),
}
KEYS = {
    "ctrl+a": "^a",
    "ctrl+c": "^c",
    "ctrl+s": "^s",
    "ctrl+shift+s": "^+s",
    "tab": "{TAB}",
    "shift+tab": "+{TAB}",
    "escape": "{ESC}",
    "up": "{UP}",
    "down": "{DOWN}",
}
EDITABLE = {"Edit", "Document"}


@dataclass
class Frame:
    hwnd: int
    bounds: tuple
    pixels: object
    source: tuple

    def png(self):
        data = BytesIO()
        image = self.pixels.copy()
        image.thumbnail((1400, 1400))
        image.save(data, format="PNG")
        return data.getvalue()


class Native:
    def __init__(self, guard, chrome_profile):
        self.guard = guard
        self.profile = Path(chrome_profile)
        self.windows = {}
        self.controls = {}
        self.owned = set()
        self.observed = None
        self.text = {}
        self.chrome_pids = set()
        self.enabled = sys.platform == "win32"
        if self.enabled:
            # Workers must initialize COM on the thread that owns UIA.
            import pythoncom

            try:
                pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            except pythoncom.com_error as exc:
                if exc.hresult != -2147417850:  # RPC_E_CHANGED_MODE
                    raise
                pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            self.com = pythoncom
            import psutil
            import win32gui
            import win32process
            from pywinauto import Desktop, keyboard, mouse

            self.desktop = Desktop(backend="uia", allow_magic_lookup=False)
            self.win = win32gui
            self.process = win32process
            self.psutil = psutil
            self.keyboard, self.mouse = keyboard, mouse

    def close(self):
        if self.enabled:
            self.controls.clear()
            self.windows.clear()
            self.com.CoUninitialize()

    def _require(self):
        if not self.enabled:
            raise StepError("Native controls require an interactive Windows 11 session.")

    def _pid(self, hwnd):
        if hwnd and self.win.IsWindow(hwnd):
            with suppress(self.win.error):
                return self.process.GetWindowThreadProcessId(hwnd)[1]
        return 0

    def _process_name(self, hwnd):
        with suppress(Exception):
            return self.psutil.Process(self._pid(hwnd)).name().lower()
        return ""

    def _chrome(self, hwnd):
        if self._process_name(hwnd) != "chrome.exe":
            return False
        pid = self._pid(hwnd)
        if pid not in self.chrome_pids:
            try:
                args = self.psutil.Process(pid).cmdline()
                selected = next(
                    arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")
                )
                if Path(selected).resolve() != self.profile.resolve():
                    return False
            except (StopIteration, OSError, self.psutil.Error):
                return False
            self.chrome_pids.add(pid)
        return True

    def _owned(self, hwnd):
        return bool(hwnd and (self._pid(hwnd) in self.owned or self._chrome(hwnd)))

    def _wrapper(self, hwnd):
        return self.desktop.window(handle=hwnd).wrapper_object()

    def _foreground(self, hwnd):
        self.guard()
        active = self.win.GetForegroundWindow()
        if active == hwnd:
            return
        if active and self._pid(active) != os.getpid():
            raise StepError("Window focus moved. Bring the intended window forward and resume.")
        self._wrapper(hwnd).set_focus()
        self.guard()
        if self.win.GetForegroundWindow() != hwnd:
            raise StepError("Windows did not focus the intended window.")

    def launch(self, name):
        self._require()
        if name not in APPS:
            raise StepError("Supported launches are Notepad, Calculator, File Explorer and Paint.")
        exe, aliases = APPS[name]
        prior = {item.handle for item in self.desktop.windows(visible_only=True)}
        self.guard()
        system = Path(os.environ["SystemRoot"]) / "System32" / exe
        if name == "explorer":
            system = Path(os.environ["SystemRoot"]) / exe
        proc = subprocess.Popen([str(system)])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.guard()
            hwnd = self.win.GetForegroundWindow()
            if hwnd and (hwnd not in prior or self._process_name(hwnd) in aliases):
                pid = self._pid(hwnd)
                if pid == proc.pid or self._process_name(hwnd) in aliases:
                    self.owned.add(pid)
                    if name == "notepad":
                        if hwnd in prior or not self.win.GetWindowText(hwnd).lower().startswith(
                            "untitled"
                        ):
                            self._foreground(hwnd)
                            self.keyboard.send_keys("^n", pause=0.01)
                            time.sleep(0.35)
                            hwnd = self.win.GetForegroundWindow()
                            if self._process_name(hwnd) not in aliases:
                                raise StepError("Bring the new Notepad document to the foreground.")
                            self.owned.add(self._pid(hwnd))
                        self._foreground(hwnd)
                    self.observe()
                    return self.win.GetWindowText(hwnd)
            time.sleep(0.13)
        raise StepError("The launched app did not become active. Bring it to the front and retry.")

    @staticmethod
    def _info(wrapper):
        info = wrapper.element_info
        return {
            "name": (info.name or "")[:160],
            "role": info.control_type,
            "automation_id": info.automation_id or "",
        }

    @staticmethod
    def _identity(wrapper):
        runtime_id = wrapper.element_info.runtime_id
        return tuple(runtime_id) if runtime_id else None

    def observe(self):
        if not self.enabled:
            return {}
        hwnd = self.win.GetForegroundWindow()
        self.windows = {}
        for candidate in self.desktop.windows(visible_only=True):
            if self._owned(candidate.handle):
                title = self.win.GetWindowText(candidate.handle)
                if title:
                    self.windows[f"w{candidate.handle}"] = (candidate.handle, title)
        if not self._owned(hwnd):
            if (
                hwnd
                and self._pid(hwnd) == os.getpid()
                and self.observed
                and self._owned(self.observed)
            ):
                hwnd = self.observed
            else:
                self.controls = {}
                self.observed = None
                return {"windows": {key: val[1] for key, val in self.windows.items()}}
        self.observed = hwnd
        self.controls = {}
        win = self._wrapper(hwnd)
        # Walk the UIA tree with a cap so large WebViews cannot flood a prompt.
        queue = [win]
        for index in range(220):
            if not queue:
                break
            item = queue.pop(0)
            with suppress(Exception):
                if not self._chrome(hwnd) or self.win.GetClassName(hwnd) == "#32770":
                    queue.extend(item.children()[:100])
                data = self._info(item)
                if item != win and item.is_visible() and item.is_enabled():
                    if not item.element_info.element.CurrentIsPassword:
                        key = f"n{index}"
                        self.controls[key] = (item, self._identity(item), data)
        return {
            "active": self.win.GetWindowText(hwnd),
            "type": self._process_name(hwnd),
            "controls": {key: item[2] for key, item in self.controls.items()},
            "windows": {key: val[1] for key, val in self.windows.items()},
        }

    def switch(self, target):
        self._require()
        window = self.windows.get(target)
        if (
            not window
            or not self._owned(window[0])
            or self.win.GetWindowText(window[0]) != window[1]
        ):
            raise StepError("That window has changed. Use a current window ID.")
        self.guard()
        self._wrapper(window[0]).set_focus()
        if self.win.GetForegroundWindow() != window[0]:
            raise StepError("Windows refused to focus the selected window. Activate it manually.")

    def target(self, key):
        self._require()
        entry = self.controls.get(key)
        if not entry or not self.observed:
            raise StepError("Use an observed Windows control ID.")
        self._foreground(self.observed)
        item, identity, data = entry
        if (
            identity is None
            or self._identity(item) != identity
            or self._info(item) != data
            or not item.is_visible()
            or not item.is_enabled()
        ):
            raise StepError("That Windows control changed. Observe it again.")
        return item, data

    def click(self, key):
        item, _ = self.target(key)
        self.guard()
        try:
            invoke = item.iface_invoke
        except Exception:
            raise StepError(
                "This control has no UI Automation Invoke pattern. Use vision with review."
            ) from None
        invoke.Invoke()

    def fill(self, key, text):
        item, details = self.target(key)
        if details["role"] not in EDITABLE:
            raise StepError("Select a Windows edit or document field.")
        hwnd = self.observed
        self._set_text(item, text, hwnd)
        if self._process_name(hwnd) == "notepad.exe" and details["role"] in EDITABLE:
            self.text[hwnd] = text

    def _set_text(self, item, text, hwnd):
        self._foreground(hwnd)
        item.set_focus()
        self.guard()
        try:
            value_pattern = item.iface_value
        except Exception:
            value_pattern = None
        if value_pattern is not None and not value_pattern.CurrentIsReadOnly:
            value_pattern.SetValue(text)
        else:
            self.keyboard.send_keys("^a", pause=0)
            send_unicode(text, lambda: self._foreground(hwnd))
        try:
            actual = item.iface_value.CurrentValue
        except Exception:
            try:
                actual = item.iface_text.DocumentRange.GetText(-1)
            except Exception:
                actual = None
        if actual is not None and actual.replace("\r\n", "\n").rstrip("\r") != text.replace(
            "\r\n", "\n"
        ):
            raise StepError("The Windows field did not retain the requested text.")

    def keys(self, target):
        self._require()
        if not self.observed or target not in KEYS:
            raise StepError("Use a supported shortcut in an owned Windows window.")
        if self._chrome(self.observed):
            raise StepError(
                "Use browser controls or the dedicated Windows file-dialog action in Chrome."
            )
        self._foreground(self.observed)
        self.keyboard.send_keys(KEYS[target], pause=0.01)

    def scroll(self, direction):
        self._require()
        if not self.observed or direction not in {"up", "down"}:
            raise StepError("Select an active window and a scroll direction.")
        self._foreground(self.observed)
        left, top, right, bottom = self.win.GetWindowRect(self.observed)
        self.mouse.scroll(
            coords=((left + right) // 2, (top + bottom) // 2),
            wheel_dist=4 if direction == "up" else -4,
        )

    def dialog(self, saving=False, expected_pid=None, timeout=3):
        self._require()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.guard()
            active = self.win.GetForegroundWindow()
            handles = [active] if active else []
            if active and self._pid(active) == os.getpid():
                handles = [
                    item.handle
                    for item in self.desktop.windows(visible_only=True)
                    if self.win.GetClassName(item.handle) == "#32770"
                ]
            matches = [
                handle
                for handle in handles
                if self._owned(handle)
                and self.win.GetClassName(handle) == "#32770"
                and (expected_pid is None or self._pid(handle) == expected_pid)
                and self.win.GetWindowText(handle)
                .lower()
                .startswith(("save",) if saving else ("open", "choose file"))
            ]
            if len(matches) == 1:
                hwnd = matches[0]
                self._foreground(hwnd)
                return hwnd, self._wrapper(hwnd)
            time.sleep(0.1)
        raise StepError(
            "Open the owned Windows Open or Save As dialog and resume. An English UI is required."
        )

    def _set_dialog_file(self, hwnd, dialog, path, saving):
        deadline = time.monotonic() + 4
        field = None
        while time.monotonic() < deadline:
            self._foreground(hwnd)
            edits = [item for item in dialog.descendants(control_type="Edit") if item.is_visible()]
            named = [
                item for item in edits if item.element_info.name.lower().startswith("file name")
            ]
            by_id = [item for item in edits if item.element_info.automation_id == "1001"]
            for candidates in (named, by_id):
                if len(candidates) == 1:
                    field = candidates[0]
                    break
            if field is not None:
                break
            time.sleep(0.1)
        if field is None:
            # The common dialog's File name mnemonic also works when the UIA name is absent.
            self._foreground(hwnd)
            self.keyboard.send_keys("%n", pause=0.02)
            time.sleep(0.15)
            focused = [
                item
                for item in dialog.descendants(control_type="Edit")
                if item.is_visible() and item.has_keyboard_focus()
            ]
            if len(focused) != 1:
                raise StepError("Could not focus the Windows file name field.")
            field = focused[0]
        self._set_text(field, str(path), hwnd)
        self._foreground(hwnd)
        label = "Save" if saving else "Open"
        buttons = [
            item
            for item in dialog.descendants(control_type="Button")
            if item.element_info.name.replace("&", "") == label and item.is_visible()
        ]
        if len(buttons) != 1:
            raise StepError(f"Could not identify the Windows {label} button.")
        self.guard()
        buttons[0].iface_invoke.Invoke()

    def open_dialog_file(self, path):
        hwnd, dialog = self.dialog()
        if not self._chrome(hwnd):
            raise StepError("Select the file from a dialog owned by the Jarvis Chrome profile.")
        self._set_dialog_file(hwnd, dialog, path, saving=False)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.guard()
            if not self.win.IsWindow(hwnd) or not self.win.IsWindowVisible(hwnd):
                return
            time.sleep(0.12)
        raise StepError("The Windows Open dialog stayed open. Inspect it before retrying.")

    def save_notepad(self, path):
        self._require()
        active = self.win.GetForegroundWindow()
        hwnd = self.observed or active
        if self._process_name(hwnd) != "notepad.exe" or hwnd not in self.text:
            # After a pause the Save As dialog can still be in front of the editor.
            hwnd = next(
                (
                    item
                    for item in self.text
                    if self.win.IsWindow(item) and active and self._pid(item) == self._pid(active)
                ),
                0,
            )
        if not hwnd or not self._owned(hwnd) or hwnd not in self.text:
            raise StepError("Enter text in the active Notepad document before saving.")
        expected = self.text[hwnd].replace("\r\n", "\n")
        if path.exists():
            if path.is_file() and read_notepad(path) == expected:
                return f"Verified saved file {path}"
            raise StepError("That file exists with different contents. Choose a new filename.")
        try:
            dialog_hwnd, dialog = self.dialog(
                saving=True, expected_pid=self._pid(hwnd), timeout=0.25
            )
        except StepError:
            self._foreground(hwnd)
            self.keyboard.send_keys("^+s", pause=0.01)
            dialog_hwnd, dialog = self.dialog(saving=True, expected_pid=self._pid(hwnd))
        self._set_dialog_file(dialog_hwnd, dialog, path, saving=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.guard()
            if path.is_file() and read_notepad(path) == expected:
                return f"Verified saved file {path}"
            time.sleep(0.13)
        raise StepError(
            "Notepad did not produce the expected file contents. Inspect the Save As dialog."
        )

    def _geometry(self, hwnd):
        from PIL import ImageGrab

        left, top, right, bottom = self.win.GetWindowRect(hwnd)
        user = c.windll.user32
        vx, vy, vw, vh = [user.GetSystemMetrics(code) for code in (76, 77, 78, 79)]
        box = max(vx, left), max(vy, top), min(vx + vw, right), min(vy + vh, bottom)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise StepError("Move the active window onto a visible monitor.")
        return box, ImageGrab

    def capture(self):
        self._require()
        hwnd = self.observed
        if not self._owned(hwnd):
            raise StepError("Bring a Jarvis-owned Chrome or Windows app to the foreground.")
        self._foreground(hwnd)
        box, grab = self._geometry(hwnd)
        image = grab.grab(bbox=box, all_screens=True)
        return Frame(hwnd, box, image, (self.win.GetWindowText(hwnd), self._pid(hwnd)))

    def vision_click(self, frame, x, y):
        self._foreground(frame.hwnd)
        box, grab = self._geometry(frame.hwnd)
        identity = (self.win.GetWindowText(frame.hwnd), self._pid(frame.hwnd))
        if frame.bounds != box or frame.source != identity:
            raise StepError("The visual target window moved or changed. Capture it again.")
        px, py = round(x * (frame.pixels.width - 1)), round(y * (frame.pixels.height - 1))
        sx, sy = box[0] + px, box[1] + py
        hit = self.win.WindowFromPoint((sx, sy))
        user = c.windll.user32
        user.GetAncestor.argtypes, user.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        if not hit or user.GetAncestor(hit, 2) != frame.hwnd:
            raise StepError("Another window covers the visual target. Move it and capture again.")
        region = (
            max(0, px - 18),
            max(0, py - 18),
            min(frame.pixels.width, px + 19),
            min(frame.pixels.height, py + 19),
        )
        physical = (region[0] + box[0], region[1] + box[1], region[2] + box[0], region[3] + box[1])
        current = grab.grab(bbox=physical, all_screens=True)
        if current.tobytes() != frame.pixels.crop(region).tobytes():
            raise StepError("The visual target changed. Capture a fresh image before clicking.")
        self.guard()
        if self.win.GetForegroundWindow() != frame.hwnd:
            raise StepError("The active window changed immediately before the click.")
        self.mouse.click(coords=(sx, sy))


def send_unicode(text, guard):
    class Keyboard(c.Structure):
        _fields_ = [
            ("vk", w.WORD),
            ("scan", w.WORD),
            ("flags", w.DWORD),
            ("time", w.DWORD),
            ("extra", c.c_size_t),
        ]

    class Mouse(c.Structure):
        _fields_ = [
            ("x", w.LONG),
            ("y", w.LONG),
            ("data", w.DWORD),
            ("flags", w.DWORD),
            ("time", w.DWORD),
            ("extra", c.c_size_t),
        ]

    class Data(c.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

    class Input(c.Structure):
        _fields_ = [("type", w.DWORD), ("data", Data)]

    raw = text.replace("\r\n", "\n").encode("utf-16-le")
    codes = [int.from_bytes(raw[index : index + 2], "little") for index in range(0, len(raw), 2)]
    send = c.windll.user32.SendInput
    send.argtypes, send.restype = [w.UINT, c.POINTER(Input), c.c_int], w.UINT
    for start in range(0, len(codes), 32):
        guard()
        events = []
        for code in codes[start : start + 32]:
            for extra in (0, 2):
                key = Keyboard(13, 0, extra) if code == 10 else Keyboard(0, code, 4 | extra)
                events.append(Input(1, Data(keyboard=key)))
        batch = (Input * len(events))(*events)
        if send(len(batch), batch, c.sizeof(Input)) != len(batch):
            raise StepError(
                "Windows rejected text input. Run the target app at the same privilege level."
            )
