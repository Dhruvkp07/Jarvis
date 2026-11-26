"""Qt desktop entry point. Browser/UIA objects live on the task worker thread."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from PySide6.QtCore import QBuffer, QIODevice, QLockFile, QObject, Qt, Signal
from PySide6.QtGui import QFont, QImage
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from jarvis.config import Settings, clear_gemini_key, save_gemini_key, state_dir
from jarvis.control import Control, Stopped
from jarvis.engine import Engine
from jarvis.providers import Provider, ProviderError

STYLE = """
QWidget { background: #141922; color: #e9edf4; font-size: 13px; }
QLabel[title="true"] { font-size: 30px; font-weight: 700; }
QLabel[muted="true"] { color: #a8b4c7; }
QGroupBox { border: 1px solid #354154; border-radius: 7px; margin-top: 12px;
             padding: 13px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 11px; padding: 0 5px; }
QPlainTextEdit, QLineEdit, QComboBox { background: #10151d; border: 1px solid #455267;
             border-radius: 5px; padding: 6px; selection-background-color: #274eb2; }
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border-color: #8eafff; }
QPushButton { background: #263246; border: 1px solid #495875;
              border-radius: 5px; padding: 7px 12px; }
QPushButton:hover { background: #344468; }
QPushButton[primary="true"] { background: #3156bf; border-color: #7195f6; font-weight: 650; }
QPushButton:disabled { color: #818b9e; background: #1b2230; border-color: #333c4d; }
QFrame#notice { background: #202c42; border: 1px solid #8eafff; border-radius: 6px; }
QSplitter::handle { background: #374256; }
"""


class Signals(QObject):
    progress = Signal(str)
    status = Signal(str)
    result = Signal(str)
    question = Signal(str, list, object)
    clear_question = Signal(object)
    connection = Signal(str)


def label(text, *, muted=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if muted:
        widget.setProperty("muted", True)
    return widget


def group(title, widget, layout_type=QVBoxLayout):
    box = QGroupBox(title)
    layout = layout_type(box)
    layout.addWidget(widget)
    return box


def find_chrome():
    if sys.platform == "win32":
        import winreg

        key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    path = winreg.QueryValue(key, None)
                if Path(path).is_file():
                    return path
            except OSError:
                pass
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                path = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if path.is_file():
                    return str(path)
    return shutil.which("google-chrome") or shutil.which("chromium")


class Window(QMainWindow):
    def __init__(self, root, monitor_ready=True):
        super().__init__()
        self.root = root
        self.settings = Settings.load(root)
        self.control = None
        self.response = None
        self.close_pending = False
        self.monitor_ready = monitor_ready
        self.signals = Signals(self)
        self.signals.progress.connect(self._log)
        self.signals.status.connect(self._status)
        self.signals.result.connect(self._done)
        self.signals.question.connect(self._question)
        self.signals.clear_question.connect(self._clear_question)
        self.signals.connection.connect(self._test_done)

        self.setWindowTitle("Jarvis")
        self.setMinimumSize(780, 640)
        self.resize(1050, 780)
        base = QWidget()
        self.setCentralWidget(base)
        outer = QVBoxLayout(base)
        outer.setContentsMargins(17, 13, 17, 14)
        header = QHBoxLayout()
        title = label("Jarvis")
        title.setProperty("title", True)
        header.addWidget(title)
        header.addStretch()
        self.active_provider = label("", muted=True)
        header.addWidget(self.active_provider)
        outer.addLayout(header)

        panes = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(panes, 1)
        task_panel = QWidget()
        task_layout = QVBoxLayout(task_panel)
        task_layout.setContentsMargins(0, 0, 8, 0)
        panes.addWidget(task_panel)

        self.goal = QPlainTextEdit()
        self.goal.setTabChangesFocus(True)
        self.goal.setPlaceholderText(
            "Open the latest quotation in Downloads, open Gmail, attach it, and email it to Priya with a short follow-up."
        )
        self.goal.setMaximumHeight(99)
        task_layout.addWidget(group("Task", self.goal))
        controls = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setProperty("primary", True)
        self.pause_button, self.resume_button, self.stop_button = (
            QPushButton("Pause"),
            QPushButton("Resume"),
            QPushButton("Stop"),
        )
        for button in (self.run_button, self.pause_button, self.resume_button, self.stop_button):
            controls.addWidget(button)
        task_layout.addLayout(controls)
        self.run_button.clicked.connect(self._run)
        self.pause_button.clicked.connect(
            lambda: self.control and self.control.pause("Paused by user")
        )
        self.resume_button.clicked.connect(lambda: self.control and self.control.resume())
        self.stop_button.clicked.connect(self._stop)
        self.status = label("Ready", muted=True)
        task_layout.addWidget(self.status)

        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_layout = QVBoxLayout(self.notice)
        self.question_text = label("")
        self.question_choices = QComboBox()
        self.question_answer = QLineEdit()
        self.question_button = QPushButton("Continue")
        self.question_button.clicked.connect(self._submit_answer)
        self.question_answer.returnPressed.connect(self._submit_answer)
        notice_layout.addWidget(self.question_text)
        notice_layout.addWidget(self.question_choices)
        notice_layout.addWidget(self.question_answer)
        notice_layout.addWidget(self.question_button)
        self.notice.hide()
        task_layout.addWidget(self.notice)

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(300)
        task_layout.addWidget(group("Action log", self.log_view), 1)
        self.result_view = QPlainTextEdit(readOnly=True)
        self.result_view.setPlaceholderText("Verified results appear here.")
        task_layout.addWidget(group("Result", self.result_view), 2)

        setup = QWidget()
        setup_layout = QVBoxLayout(setup)
        setup_layout.setContentsMargins(8, 0, 0, 0)
        panes.addWidget(setup)
        panes.setSizes([630, 380])
        self._build_settings(setup_layout)
        self.hotkey_label = label("Emergency stop  Ctrl+Shift+F10", muted=True)
        outer.addWidget(self.hotkey_label)
        self.setStyleSheet(STYLE)
        self._buttons()
        self._provider_label()

    def _buttons(self):
        active = self.control is not None
        stopping = active and self.control.stopped.is_set()
        paused = active and not self.control.ready.is_set()
        self.run_button.setEnabled(not active and self.monitor_ready and sys.platform == "win32")
        self.pause_button.setEnabled(active and not paused and not stopping)
        self.resume_button.setEnabled(active and paused and not stopping)
        self.stop_button.setEnabled(active and not stopping)
        self.chrome_button.setEnabled(not active)
        self.goal.setReadOnly(active)

    def _build_settings(self, layout):
        box = QGroupBox("Provider settings")
        form = QFormLayout(box)
        s = self.settings
        self.provider = QComboBox()
        self.provider.addItems(["ollama", "gemini"])
        self.provider.setCurrentText(s.provider)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(["", "qwen2.5:7b", "qwen2.5vl:7b", "gemini-2.5-flash"])
        self.model.setCurrentText(s.model)
        self.endpoint = QLineEdit(s.ollama_url)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("Enter a new key")
        self.local = QCheckBox("Local-only model inputs")
        self.local.setChecked(s.local_only)
        self.vision = QCheckBox("Model supports vision")
        self.vision.setChecked(s.vision)
        self.cloud = QCheckBox("Allow Gemini to receive screen images")
        self.cloud.setChecked(s.cloud_images)
        self.hands_off = QCheckBox("Send without review when explicitly requested")
        self.hands_off.setChecked(s.hands_off)
        form.addRow("Provider", self.provider)
        form.addRow("Model", self.model)
        form.addRow("Ollama URL", self.endpoint)
        form.addRow("Gemini key", self.secret)
        for option in (self.local, self.vision, self.cloud, self.hands_off):
            form.addRow(option)
        form.addRow(
            label(
                "Gemini processes task text and applicable screen-derived text. "
                "Screen images require separate permission.",
                muted=True,
            )
        )
        save = QPushButton("Apply settings")
        save.clicked.connect(self._save)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        clear = QPushButton("Remove Gemini key")
        clear.clicked.connect(self._clear_key)
        buttons = QWidget()
        grid = QGridLayout(buttons)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(save, 0, 0)
        grid.addWidget(self.test_button, 0, 1)
        grid.addWidget(clear, 1, 0, 1, 2)
        form.addRow(buttons)
        self.connection_details = label("", muted=True)
        form.addRow(self.connection_details)
        layout.addWidget(box)
        chrome = QGroupBox("Chrome profile")
        chrome_layout = QVBoxLayout(chrome)
        chrome_layout.addWidget(
            label(
                "Sign in to Gmail here once. Close this Chrome window before running a task.",
                muted=True,
            )
        )
        self.chrome_button = QPushButton("Open Jarvis Chrome for sign-in")
        self.chrome_button.clicked.connect(self._open_chrome)
        chrome_layout.addWidget(self.chrome_button)
        layout.addWidget(chrome)
        layout.addStretch()
        self.provider.currentTextChanged.connect(self._provider_fields)
        self.local.toggled.connect(self._provider_fields)
        self._provider_fields()

    def _provider_fields(self):
        cloud_provider = self.provider.currentText() == "gemini"
        self.cloud.setEnabled(cloud_provider and not self.local.isChecked())
        self.endpoint.setEnabled(not cloud_provider)
        self.secret.setEnabled(cloud_provider and sys.platform == "win32")

    def _provider_label(self):
        self.active_provider.setText(
            f"{self.settings.provider.title()} · {self.settings.model or 'Select a model'}"
        )

    def _save(self):
        settings = Settings(
            self.provider.currentText(),
            self.model.currentText().strip(),
            self.endpoint.text().strip(),
            self.vision.isChecked(),
            self.cloud.isChecked(),
            self.local.isChecked(),
            self.hands_off.isChecked(),
        )
        try:
            settings.validate()
            secret = self.secret.text().strip()
            if secret:
                save_gemini_key(secret)
                self.secret.clear()
            settings.save(self.root)
        except ValueError as exc:
            self.connection_details.setText(str(exc))
            return False
        except Exception:
            self.connection_details.setText(
                "Settings could not be saved. Check Windows Credential Manager and folder access."
            )
            return False
        self.settings = settings
        self._provider_label()
        self.connection_details.setText("Settings applied.")
        return True

    def _clear_key(self):
        try:
            clear_gemini_key()
            self.connection_details.setText("Gemini key removed.")
        except Exception:
            self.connection_details.setText(
                "Could not remove the key from Windows Credential Manager."
            )
        self.secret.clear()

    def _test(self):
        if not self._save():
            return
        self.test_button.setEnabled(False)
        self.connection_details.setText("Testing structured model output…")
        settings = self.settings
        provider = Provider(settings)
        png = None
        if provider.can_see():
            image = QImage(20, 20, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.blue)
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            png = bytes(buffer.data())

        def test():
            try:
                provider.test(png)
                mode = "structured response and image input" if png else "structured text response"
                result = f"{settings.provider.title()} connected. Verified {mode}."
            except ProviderError as exc:
                result = str(exc)
            except Exception:
                result = "Connection test failed. Check provider settings."
            self.signals.connection.emit(result)

        Thread(target=test, daemon=True).start()

    def _test_done(self, message):
        self.test_button.setEnabled(True)
        self.connection_details.setText(message)

    def _open_chrome(self):
        executable = find_chrome()
        if not executable:
            QMessageBox.information(
                self, "Chrome", "Install Google Chrome to use the Jarvis profile."
            )
            return
        profile = self.root / "ChromeProfile"
        try:
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    executable,
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "https://mail.google.com/mail/",
                ]
            )
        except OSError:
            QMessageBox.information(self, "Chrome", "Jarvis could not start the Chrome profile.")

    def _log(self, message):
        self.log_view.appendPlainText(message)

    def _status(self, message):
        self.status.setText(message)
        self._buttons()

    def _ask(self, text, choices, guard):
        replies = Queue(maxsize=1)
        self.signals.question.emit(text, choices, replies)
        try:
            while True:
                guard()
                try:
                    return replies.get(timeout=0.1)
                except Empty:
                    continue
        finally:
            self.signals.clear_question.emit(replies)

    def _question(self, text, choices, replies):
        if not self.control or self.control.stopped.is_set():
            return
        self.response = replies
        self.question_text.setText(text)
        self.question_choices.clear()
        self.question_choices.addItems(choices)
        self.question_choices.setCurrentIndex(0 if len(choices) == 1 else -1)
        self.question_choices.setPlaceholderText("Choose an option")
        self.question_choices.setVisible(bool(choices))
        self.question_answer.setVisible(not choices)
        self.question_answer.clear()
        self.notice.show()
        self.status.setText("Waiting for your input")

    def _submit_answer(self):
        if self.response is None:
            return
        answer = (
            self.question_choices.currentText()
            if self.question_choices.isVisible()
            else self.question_answer.text()
        ).strip()
        if answer:
            self.response.put_nowait(answer)
            self.response = None
            self.notice.hide()
            self.status.setText("Running")

    def _clear_question(self, response):
        if self.response is response:
            self.response = None
            self.notice.hide()

    def _run(self):
        goal = self.goal.toPlainText().strip()
        if not goal or len(goal) > 4000:
            self._status("Enter a task of 1 to 4,000 characters.")
            return
        if not self._save():
            return
        self.log_view.clear()
        self.result_view.clear()
        control = self.control = Control(self.signals.status.emit)
        self._status("Running")

        def run():
            try:
                engine = Engine(
                    goal,
                    self.root,
                    lambda: self.settings,
                    control,
                    self.signals.progress.emit,
                    self._ask,
                )
                result = engine.run()
            except Stopped:
                result = "Stopped."
            except Exception:
                result = "Jarvis could not continue. Close any extra dialogs and restart the task."
            self.signals.result.emit(result)

        Thread(target=run, daemon=True).start()

    def _stop(self):
        if self.control:
            self.control.stop()

    def _done(self, result):
        self.control = None
        self.response = None
        self.notice.hide()
        self._status("Ready")
        self.result_view.setPlainText(result)
        if self.close_pending:
            self.close()

    def closeEvent(self, event):
        if self.control:
            self.close_pending = True
            self._stop()
            event.ignore()
        else:
            event.accept()


def main():
    from jarvis.wininput import InputMonitor, enable_dpi_awareness

    windows = sys.platform == "win32"
    if windows:
        enable_dpi_awareness()
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setApplicationName("Jarvis")
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / "app.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "Jarvis", "Jarvis is already running.")
        return 1
    window = Window(root, monitor_ready=windows)
    monitor = None
    if windows:
        import win32gui
        import win32process

        def takeover():
            control = window.control
            if control and control.ready.is_set() and not control.awaiting_user.is_set():
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and win32process.GetWindowThreadProcessId(hwnd)[1] != os.getpid():
                    control.pause("Paused after manual keyboard or mouse input. Resume to refresh.")

        monitor = InputMonitor(window._stop, takeover)
        window.monitor_ready = monitor.start()
        window._buttons()
        if not window.monitor_ready:
            window.hotkey_label.setText(
                "Global input monitoring could not start. Close any app using Ctrl+Shift+F10 and restart Jarvis."
            )
    else:
        window.hotkey_label.setText(
            "Task execution requires Windows 11. Settings can be previewed here."
        )
    window.show()
    try:
        return app.exec()
    finally:
        if monitor:
            monitor.close()
        lock.unlock()
