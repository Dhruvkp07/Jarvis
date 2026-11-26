"""Windows-wide physical input detection and emergency stop."""

import ctypes as c
from ctypes import wintypes as w
from threading import Event, Thread


def enable_dpi_awareness():
    c.windll.user32.SetProcessDpiAwarenessContext(c.c_void_p(-4))


class InputMonitor:
    def __init__(self, stop, takeover):
        self.stop = stop
        self.takeover = takeover
        self.ready = Event()
        self.hotkey_ok = False
        self.hooks_ok = False
        self.thread_id = 0
        self.worker = Thread(target=self._run, daemon=True)

    def start(self):
        self.worker.start()
        self.ready.wait(2)
        return self.hotkey_ok and self.hooks_ok

    def close(self):
        if self.thread_id:
            c.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)

    def _run(self):
        user32 = c.WinDLL("user32", use_last_error=True)
        kernel32 = c.WinDLL("kernel32", use_last_error=True)
        lresult = c.c_ssize_t
        hook_fn = c.WINFUNCTYPE(lresult, c.c_int, w.WPARAM, w.LPARAM)
        user32.SetWindowsHookExW.argtypes = [c.c_int, hook_fn, w.HINSTANCE, w.DWORD]
        user32.SetWindowsHookExW.restype = w.HANDLE
        user32.CallNextHookEx.argtypes = [w.HANDLE, c.c_int, w.WPARAM, w.LPARAM]
        user32.CallNextHookEx.restype = lresult
        user32.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel32.GetModuleHandleW.restype = w.HMODULE

        class Keyboard(c.Structure):
            _fields_ = [
                ("vk", w.DWORD),
                ("scan", w.DWORD),
                ("flags", w.DWORD),
                ("time", w.DWORD),
                ("extra", c.c_size_t),
            ]

        class Mouse(c.Structure):
            _fields_ = [
                ("point", w.POINT),
                ("data", w.DWORD),
                ("flags", w.DWORD),
                ("time", w.DWORD),
                ("extra", c.c_size_t),
            ]

        @hook_fn
        def keyboard(code, message, addr):
            if code == 0 and message in {0x0100, 0x0104}:
                if not c.cast(addr, c.POINTER(Keyboard)).contents.flags & 0x10:
                    self.takeover()
            return user32.CallNextHookEx(None, code, message, addr)

        last = None

        @hook_fn
        def mouse(code, message, addr):
            nonlocal last
            if code == 0:
                data = c.cast(addr, c.POINTER(Mouse)).contents
                if not data.flags & 1:
                    point = data.point.x, data.point.y
                    if message in {0x0201, 0x0204, 0x0207, 0x020A}:
                        self.takeover()
                    elif message == 0x0200 and last != point:
                        self.takeover()
                    last = point
            return user32.CallNextHookEx(None, code, message, addr)

        self.thread_id = kernel32.GetCurrentThreadId()
        msg = w.MSG()
        user32.PeekMessageW(c.byref(msg), None, 0, 0, 0)
        self.hotkey_ok = bool(user32.RegisterHotKey(None, 1, 0x4000 | 0x0002 | 0x0004, 0x79))
        module = kernel32.GetModuleHandleW(None)
        keyboard_id = user32.SetWindowsHookExW(13, keyboard, module, 0)
        mouse_id = user32.SetWindowsHookExW(14, mouse, module, 0)
        self.hooks_ok = bool(keyboard_id and mouse_id)
        self.ready.set()
        try:
            while user32.GetMessageW(c.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and msg.wParam == 1:
                    self.stop()
        finally:
            if keyboard_id:
                user32.UnhookWindowsHookEx(keyboard_id)
            if mouse_id:
                user32.UnhookWindowsHookEx(mouse_id)
            if self.hotkey_ok:
                user32.UnregisterHotKey(None, 1)
