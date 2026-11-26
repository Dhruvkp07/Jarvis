"""Observe, plan, execute and verify a single task on one worker thread."""

import hashlib
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from jarvis.browser import Browser, StepError, origin
from jarvis.control import Interrupted, Stopped
from jarvis.files import SelectedFile, display_file, known_folder, search
from jarvis.mail import Gmail, Ledger, allows_send, user_recipient
from jarvis.native import Native
from jarvis.providers import Provider, ProviderError, interruptible

URL = re.compile(r"https?://[^\s<>\"']+")
RISK = re.compile(
    r"\b(delete|remove|discard|reset|uninstall|purchase|buy|pay|transfer|publish|send|share|post|"
    r"checkout|subscribe|confirm|accept|allow|agree|replace|overwrite|grant|install|sign in|log in)\b",
    re.I,
)
SAVE_NAME = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+\.txt$", re.I)
GMAIL = "https://mail.google.com"


class Engine:
    def __init__(
        self,
        goal,
        root,
        settings,
        control,
        log,
        ask,
        *,
        browser_factory=Browser,
        native_factory=Native,
        provider_factory=Provider,
        downloads=None,
        documents=None,
    ):
        self.goal = goal
        self.settings = settings
        self.control = control
        self.log = log
        self.ask_user = ask
        self.generation = 0
        self.browser = browser_factory(root / "ChromeProfile", self.guard)
        self.native = native_factory(self.guard, root / "ChromeProfile")
        self.provider_factory = provider_factory
        self.ledger = Ledger(root / "operations.sqlite3")
        self.gmail = Gmail(self.browser, self.ledger, self.ask, self.guard, task_goal=goal)
        self.downloads = downloads or known_folder("Downloads")
        self.documents = documents or known_folder("Documents")
        self.origins = set()
        for match in URL.findall(goal):
            try:
                self.origins.add(origin(match.rstrip(".,)")))
            except StepError:
                pass
        if re.search(r"\bgmail\b", goal, re.I):
            self.origins.add(GMAIL)
        self.files = {}
        self.answers = []
        self.approvals = set()
        self.evidence = []
        self.recent = deque(maxlen=7)
        self.frame = self.state = None
        self.selected_recipient = user_recipient(goal)
        self.failure = ""
        self.failures = {}
        self.stall = 0
        self.last_signature = None

    def guard(self):
        self.control.check(self.generation)

    def ask(self, question, choices=()):
        self.guard()
        self.control.awaiting_user.set()
        try:
            answer = self.ask_user(question, list(choices), self.guard)
        finally:
            self.control.awaiting_user.clear()
        self.guard()
        if not answer or (choices and answer not in choices):
            raise Stopped()
        return answer

    def approve(self, key, text):
        if key not in self.approvals:
            if self.ask(text, ["Continue", "Stop"]) != "Continue":
                raise Stopped()
            self.approvals.add(key)

    def record(self, text):
        if text not in self.evidence:
            self.evidence.append(text)
        return text

    def _allow_origin(self, url):
        site = origin(url)
        if site not in self.origins:
            self.approve(
                ("origin", site),
                f"Allow Jarvis to operate this site for the current task?\n\n{site}",
            )
            self.origins.add(site)
        return site

    def _browser_target(self, key):
        if not self.state or self.state.challenge:
            raise StepError("Open the intended Chrome page before using browser controls.")
        site = self._allow_origin(self.state.url)
        if site == GMAIL:
            raise StepError("Use the verified Gmail draft and send actions.")
        self.guard()
        if self.browser.page.evaluate("document.visibilityState") == "hidden":
            raise StepError("Select the intended Chrome tab and resume.")
        return site, self.browser.target(key)[1]

    def _resolved_files(self, keys):
        if any(key not in self.files for key in keys):
            raise StepError("Locate the requested file in Downloads before attaching it.")
        return [self.files[key] for key in keys]

    def _find(self, action):
        candidates = search(self.downloads, action.target)
        if not candidates:
            raise StepError(
                "No matching file in Downloads. Move the file there or search for its filename."
            )
        matches = candidates[:8]
        newest = action.text.lower() == "latest" and re.search(
            r"\b(latest|newest|most recent)\b", self.goal, re.I
        )
        if newest and len(matches) > 1:
            # Timestamps rounded to the same displayed second require a choice.
            cutoff = matches[0][1] // 1_000_000_000
            matches = [item for item in matches if item[1] // 1_000_000_000 == cutoff]
        confirmed_file = len(matches) > 1
        if len(matches) == 1:
            path = matches[0][0]
        else:
            options = {display_file(p, stamp): p for p, stamp in matches}
            if len(candidates) > len(matches) and not newest:
                options["Search using a more specific filename"] = None
            path = options[self.ask("Which file in Downloads should Jarvis use?", options)]
            if path is None:
                raise StepError(
                    "Search again using the full filename or a more specific part of it."
                )
        filename_terms = set(re.findall(r"[^\W_]+", path.stem.lower()))
        goal_terms = set(re.findall(r"\w+", self.goal.lower()))
        if {"quote", "quotation"} & goal_terms:
            goal_terms.update({"quote", "quotation"})
        if not confirmed_file and not filename_terms & (
            goal_terms - {"a", "the", "in", "email", "file", "and", "my", "to"}
        ):
            self.approve(("file", path), f"Allow Jarvis to use this file for the task?\n\n{path}")
        item = SelectedFile.select(path)
        key = next((key for key, file in self.files.items() if file == item), "")
        if not key:
            key = f"file-{len(self.files) + 1}"
            self.files[key] = item
        return f"Selected {key} · {path.name}"

    def _save(self, target):
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = self.documents / path
        if (
            not SAVE_NAME.fullmatch(path.name)
            or not path.parent.is_dir()
            or path.is_symlink()
            or path.drive.startswith("\\")
        ):
            raise StepError("Choose an existing folder and a valid .txt filename.")
        path = path.resolve()
        authorized_location = (
            path.parent == self.documents.resolve()
            or str(path).lower() in self.goal.lower()
            or any(
                path.parent == known_folder(folder).resolve()
                and re.search(rf"\b{folder}\b", self.goal, re.I)
                for folder in ("Downloads", "Desktop")
            )
        )
        if path.name.lower() not in self.goal.lower() or not authorized_location:
            self.approve(("save", path), f"Save the Notepad document here?\n\n{path}")
        return self.record(self.native.save_notepad(path))

    def _click(self, key):
        site, control = self._browser_target(key)
        name, kind = control["name"], control["role"]
        if control["href"] and origin(control["href"]) != site:
            self._allow_origin(control["href"])
        loopback = urlsplit(site).hostname in {"127.0.0.1", "::1", "localhost"}
        is_submit = control["type"] in {"submit", "image"}
        is_form_button = kind == "button" and control["form"] and control["type"] != "button"
        risky = bool(RISK.search(name) or is_submit or is_form_button)
        if kind == "link" and not RISK.search(name):
            risky = False
        if risky or (not name and kind == "button"):
            if not (
                loopback
                and re.search(r"\b(?:complete|submit|test)\b", self.goal, re.I)
                and not RISK.search(name)
            ):
                self.approve(
                    ("web-click", self.state.url, name, key),
                    f"Allow this browser action?\n\n{site}\n{name or kind}",
                )
        if not risky:
            self.browser.click(key)
            return "Clicked browser control. Refreshing page state."
        signature = json.dumps([self.goal, self.state.url, control], sort_keys=True)
        unique = "browser:" + hashlib.sha256(signature.encode()).hexdigest()
        if not self.ledger.begin(unique):
            raise StepError(
                "This browser action was already attempted. Inspect the site for its result before retrying."
            )
        self.guard()
        self.browser.click(key)
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            self.guard()
            latest = self.browser.observe()
            if not latest or latest.challenge:
                break
            old_lines = set(self.state.text.splitlines())
            added = [line.strip() for line in latest.text.splitlines() if line not in old_lines]
            confirmation = next(
                (
                    line
                    for line in added
                    if re.search(
                        r"\b(submitted|received|completed|successful|successfully|thank you|confirmed|saved)\b",
                        line,
                        re.I,
                    )
                ),
                "",
            )
            if confirmation and origin(latest.url) == site:
                self.ledger.verified(unique)
                return self.record(f"Browser displayed after the action: {confirmation[:230]}")
            time.sleep(0.2)
        raise StepError(
            "The browser action was attempted. Its result is uncertain. Inspect the page before retrying."
        )

    def _native_file(self, key):
        path = self._resolved_files([key])[0].check()
        state = self.state
        if not state or state.challenge or self._allow_origin(state.url) == GMAIL:
            raise StepError(
                "Use a supported Chrome file upload page. Gmail uses the draft workflow."
            )
        self.approve(
            ("native-file", state.url, path),
            f"Select this file in the Windows upload dialog?\n\n{state.url}\n{path}",
        )
        if (
            self.browser.page.url != state.url
            or self.browser.page.evaluate("document.visibilityState") != "visible"
        ):
            raise StepError("Chrome changed tabs or pages before the file was selected.")
        self._resolved_files([key])[0].check()
        self.native.open_dialog_file(path)
        names = self.browser.page.locator("input[type=file]").evaluate_all(
            "els => els.flatMap(el => Array.from(el.files, f => f.name))"
        )
        if path.name not in names:
            raise StepError(
                "The native dialog closed, but Chrome did not report the expected file."
            )
        return self.record(f"Chrome reports {path.name} selected through the Windows Open dialog.")

    def _mail_prepare(self, action):
        if GMAIL not in self.origins or not self.state or origin(self.state.url) != GMAIL:
            raise StepError("Open Gmail for the requested mail task first.")
        if self.selected_recipient is None:
            self.selected_recipient = self.ask(
                "Who should receive this email? Enter the name or full email address."
            )
        subject, body = action.subject, action.text
        files = self._resolved_files(action.files)
        requested_attachment = bool(
            re.search(r"\b(attach|attached|attachment|quotation)\b", self.goal, re.I)
        )
        if requested_attachment and not files:
            raise StepError(
                "Locate and include the requested attachment when preparing this draft."
            )
        if files and not requested_attachment:
            self.approve(
                ("mail-files", tuple(files)),
                "Include these files in the Gmail draft?\n\n"
                + "\n".join(str(item.path) for item in files),
            )
        draft = self.gmail.prepare(self.selected_recipient, subject, body, files)
        self.selected_recipient = draft.email
        names = ", ".join(item.path.name for item in draft.files) or "none"
        return self.record(
            f"Verified Gmail draft to {draft.email}. Subject “{draft.subject}”. Attachments: {names}. Body matched."
        )

    def _mail_send(self):
        if not allows_send(self.goal):
            raise StepError(
                "The original task does not explicitly authorize sending. A verified draft can be kept."
            )
        return self.record(self.gmail.send(self.settings().hands_off))

    def execute(self, action):
        kind, target = action.kind, action.target
        if kind in {"open_browser", "new_tab"}:
            self._allow_origin(target)
            page = self.browser.start_page(target, new_tab=kind == "new_tab")
            return f"Chrome opened {urlsplit(page.url).hostname or 'a blank tab'}."
        if kind == "tab":
            self.browser.switch(target)
            return "Chrome tab activated."
        if kind == "click":
            return self._click(target)
        if kind == "fill":
            self._browser_target(target)
            self.browser.fill(target, action.text)
            return "Browser field value verified."
        if kind == "scroll":
            if not self.state or self._allow_origin(self.state.url) == GMAIL:
                raise StepError("Open the intended browser page before scrolling.")
            self.browser.scroll(target)
            return "Browser scrolled."
        if kind == "upload":
            site, _ = self._browser_target(target)
            files = self._resolved_files(action.files)
            if not files:
                raise StepError("Select at least one attachment.")
            self.approve(
                ("upload", self.state.url, tuple(files)),
                f"Select these files for {site}?\n\n" + "\n".join(str(f.path) for f in files),
            )
            paths = [item.check() for item in files]
            self.browser.upload(target, paths)
            return self.record("Browser file input verified: " + ", ".join(p.name for p in paths))
        if kind == "read_page":
            if (
                not self.state
                or self.state.challenge
                or self._allow_origin(self.state.url) == GMAIL
            ):
                raise StepError("Open a non-Gmail page with visible information.")
            text = self.state.text.strip()
            if not text:
                raise StepError("There is no visible page text yet.")
            return self.record(
                f"Visible page text from {self.state.title or origin(self.state.url)}\n{text[:1100]}"
            )
        if kind == "launch":
            return f"Opened Windows app: {self.native.launch(target)}"
        if kind == "switch_window":
            self.native.switch(target)
            return "Windows window activated."
        if kind == "native_keys":
            self.native.keys(target)
            return "Windows shortcut issued."
        if kind == "native_scroll":
            self.native.scroll(target)
            return "Windows window scrolled."
        if kind == "native_click":
            _, details = self.native.target(target)
            if RISK.search(details["name"]) or details["role"] in {
                "Button",
                "MenuItem",
                "SplitButton",
            }:
                if details["name"].lower() not in {
                    "open",
                    "cancel",
                    "close",
                    "file",
                    "edit",
                    "view",
                    "new tab",
                }:
                    self.approve(
                        ("native", self.native.observed, target, details["name"]),
                        f"Activate this Windows control?\n\n{details['name'] or details['role']}",
                    )
            self.native.click(target)
            return "Windows control invoked."
        if kind == "native_fill":
            self.native.fill(target, action.text)
            return "Windows text entered."
        if kind == "file_dialog":
            return self._native_file(target)
        if kind == "save_notepad":
            return self._save(target)
        if kind == "find_file":
            return self._find(action)
        if kind == "open_file":
            path = self._resolved_files([target])[0].check()
            if not hasattr(os, "startfile"):
                raise StepError("Opening the native file viewer requires Windows.")
            self.guard()
            os.startfile(path)
            return f"Windows accepted the request to open {path.name}."
        if kind == "mail_prepare":
            return self._mail_prepare(action)
        if kind == "mail_send":
            return self._mail_send()
        if kind == "vision":
            if not self.provider_factory(self.settings()).can_see():
                self.ask(
                    "This screen needs vision. Select a vision model in Settings, enable cloud "
                    "screen access if using Gemini, then continue.",
                    ["Continue"],
                )
                if not self.provider_factory(self.settings()).can_see():
                    raise StepError(
                        "Vision is still unavailable. Use a structured control or switch models."
                    )
            if self.state and (self.state.challenge or origin(self.state.url) == GMAIL):
                raise StepError("Complete the challenge manually or use the Gmail workflow.")
            self.frame = self.native.capture()
            return "Captured the active task window in memory."
        if kind == "vision_click":
            if not self.frame:
                raise StepError("Capture a fresh active-window image first.")
            self.approve(
                ("vision", id(self.frame), action.x, action.y),
                f"Click this visual target in the task window?\n\n{target}",
            )
            self.native.vision_click(self.frame, action.x, action.y)
            self.frame = None
            return "Clicked verified visual target. Refreshing window state."
        if kind == "ask":
            if re.search(
                r"password|passcode|verification code|api key|credit card|secret token",
                target,
                re.I,
            ):
                raise StepError(
                    "Enter credentials directly in the app or website during manual sign-in."
                )
            answer = self.ask(target, action.options)
            self.answers.append(answer)
            return "User answered the clarification."
        if kind == "wait":
            for _ in range(6):
                self.guard()
                time.sleep(0.1)
            return "Waited for the interface."
        raise StepError("Unsupported agent action.")

    def _observe(self):
        self.state = self.browser.observe()
        if self.state and self.state.challenge:
            self.ask(
                "Chrome requires sign-in or a verification challenge. Complete it in Jarvis Chrome, "
                "then continue here.",
                ["Continue"],
            )
            self.state = self.browser.observe()
            if self.state and self.state.challenge:
                raise StepError("The sign-in or verification challenge is still active.")
        data = self.native.observe()
        if self.state and urlsplit(self.state.url).scheme in {"http", "https"}:
            self._allow_origin(self.state.url)
            if origin(self.state.url) == GMAIL:
                data["browser"] = {"title": "Gmail", "url": self.state.url, "tab": self.state.tab}
            else:
                data["browser"] = self.state.planner_data()
        data["tabs"] = {
            key: urlsplit(url)._replace(query="", fragment="").geturl()
            for key, url in self.browser.available_tabs().items()
            if urlsplit(url).scheme in {"http", "https"} and origin(url) in self.origins
        }
        data["files"] = {key: item.path.name for key, item in self.files.items()}
        return data

    def _result(self, status, notes=""):
        return "\n\n".join([status, *self.evidence, notes]).strip()

    def run(self):
        provider_failures = 0
        try:
            for _ in range(90):
                self.generation = self.control.wait_ready()
                try:
                    observation = self._observe()
                    provider = self.provider_factory(self.settings())
                    frame = self.frame
                    action = interruptible(
                        lambda: provider.decide(
                            {
                                "goal": self.goal,
                                "user_answers": self.answers[-8:],
                                "observation": observation,
                                "recent_results": list(self.recent),
                                "verified_results": [item[:500] for item in self.evidence[-7:]],
                            },
                            frame.png() if frame else None,
                        ),
                        self.guard,
                    )
                    provider_failures = 0
                    self.guard()
                    if action.kind != "vision_click" and action.kind != "vision":
                        self.frame = None
                    if action.kind == "finish":
                        status = (
                            "Task ended. Verified results follow."
                            if self.evidence
                            else "Task ended without a verified result."
                        )
                        if self.failure:
                            status = f"Task ended with a remaining issue. {self.failure}"
                        elif (
                            GMAIL in self.origins
                            and allows_send(self.goal)
                            and not any(
                                item.startswith("Verified in Sent") for item in self.evidence
                            )
                        ):
                            status = "Task ended without verifying a sent message. Check the Gmail draft or Sent."
                        return self._result(
                            status, f"Planner note: {action.text}" if action.text else ""
                        )
                    signature = (
                        action.kind,
                        action.target,
                        action.text,
                        observation.get("active"),
                        self.state.url if self.state else "",
                        self.state.text if self.state else "",
                    )
                    if signature == self.last_signature:
                        self.stall += 1
                    else:
                        self.stall = 0
                    self.last_signature = signature
                    if self.stall >= 3:
                        return self._result(
                            "Stopped after repeated identical steps. Inspect the active app and retry with a more specific instruction."
                        )
                    try:
                        result = self.execute(action)
                    except (StepError, PlaywrightError, OSError) as exc:
                        detail = (
                            str(exc)
                            if isinstance(exc, StepError)
                            else (
                                "The target changed or became unavailable. Inspect the browser or window and retry."
                            )
                        )
                        self.failure = detail
                        fail_key = (action.kind, action.target)
                        self.failures[fail_key] = self.failures.get(fail_key, 0) + 1
                        self.recent.append({"action": action.kind, "error": detail})
                        self.log(f"Step needs attention · {detail}")
                        if self.failures[fail_key] >= 3:
                            return self._result(f"Stopped after three attempts. {detail}")
                        continue
                    self.failure = ""
                    self.recent.append({"action": action.kind, "result": result[:1300]})
                    self.log(f"Completed step · {action.kind.replace('_', ' ')}")
                except Stopped:
                    raise
                except Interrupted:
                    self.frame = None
                    self.log("State changed while paused. Refreshing before the next action.")
                except (ProviderError, ValueError) as exc:
                    provider_failures += 1
                    self.log(str(exc))
                    self.frame = None
                    if provider_failures >= 3:
                        return self._result(f"Stopped after three model errors. {exc}")
                    try:
                        self.ask(
                            "Model request failed. Change the provider or model in Settings, then continue.\n\n"
                            + str(exc),
                            ["Continue"],
                        )
                    except Interrupted as interrupted:
                        if isinstance(interrupted, Stopped):
                            raise
                except (StepError, PlaywrightError, OSError) as exc:
                    detail = (
                        str(exc)
                        if isinstance(exc, StepError)
                        else ("The browser or window changed. Restore the target and resume.")
                    )
                    self.failure = detail
                    try:
                        self.ask(detail, ["Continue"])
                    except Interrupted as interrupted:
                        if isinstance(interrupted, Stopped):
                            raise
            return self._result(
                "Reached the step limit. Inspect the current app before starting a follow-up task."
            )
        except Stopped:
            return self._result("Stopped. Any action already issued may have completed.")
        finally:
            self.frame = None
            self.browser.close()
            self.native.close()
