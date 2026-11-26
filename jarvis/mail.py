"""Gmail browser workflow and persisted single-attempt sends."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError

from jarvis.browser import StepError

EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
SEND = re.compile(
    r"\b(?:send|e-?mail)\s+(?:(?:it|this|the (?:message|draft|email|quotation))\s+)?to\s+", re.I
)
RECIPIENT = re.compile(
    r"([^\n,;]+?)(?=\s+(?:with|about|regarding|and|saying|the|a)\b|[!?;,]|"
    r"\.(?=\s|$)|$)",
    re.I,
)
DIRECT_EMAIL = re.compile(r"\be-?mail\s+(?!from\b|in\b|it\b|this\b|the\b)", re.I)


def user_recipient(goal):
    verb = SEND.search(goal) or DIRECT_EMAIL.search(goal)
    recipient = RECIPIENT.match(goal, verb.end()) if verb else None
    if recipient is None:
        return None
    rest = goal[recipient.end() :]
    if re.match(r"\s+and\s+(?!(?:attach|include|verify|check|then)\b)", rest, re.I):
        return None
    name = recipient.group(1).strip().strip("\"'")
    return name if len(name) <= 90 and len(name.split()) <= 5 else None


def allows_send(goal):
    if re.search(r"\b(?:draft|do not|don['’]t|never|without sending|review before)\b", goal, re.I):
        return False
    return bool(
        SEND.search(goal)
        or re.search(
            r"\b(?:send (?:it|this|the message|the email)|email\s+[\w.+@-]+\s+(?:with|a|about))\b",
            goal,
            re.I,
        )
    )


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mail_tasks (scope TEXT PRIMARY KEY, operation TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=4)
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, key):
        with self._connect() as db:
            result = db.execute("SELECT state FROM operations WHERE key=?", (key,)).fetchone()
        return result[0] if result else None

    def previous_mail(self, scope):
        with self._connect() as db:
            result = db.execute(
                "SELECT operation FROM mail_tasks WHERE scope=?", (scope,)
            ).fetchone()
        return result[0] if result else None

    def begin(self, key, scope=None):
        with self._connect() as db:
            if scope:
                claim = db.execute("INSERT OR IGNORE INTO mail_tasks VALUES (?, ?)", (scope, key))
                if claim.rowcount != 1:
                    return False
            result = db.execute("INSERT OR IGNORE INTO operations VALUES (?, 'pending')", (key,))
            return result.rowcount == 1

    def verified(self, key):
        with self._connect() as db:
            db.execute("UPDATE operations SET state='verified' WHERE key=?", (key,))


@dataclass(frozen=True)
class Draft:
    email: str
    subject: str
    body: str
    files: tuple

    def key(self, mailbox):
        raw = json.dumps(
            [
                mailbox,
                self.email,
                self.subject,
                self.body,
                [(file.path.name, file.digest) for file in self.files],
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def summary(self):
        names = ", ".join(file.path.name for file in self.files) or "None"
        return f"To {self.email}\nSubject {self.subject}\nAttachments {names}\n\n{self.body}"


GMAIL_HOST = "mail.google.com"


def _unique(locator, description, timeout=3500):
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
    except PlaywrightError:
        raise StepError(
            f"Could not find {description}. Check the browser tab and resume."
        ) from None
    if locator.count() != 1 or not locator.is_visible():
        raise StepError(f"Could not identify one {description}. Close extra dialogs and resume.")
    return locator


class Gmail:
    def __init__(self, browser, ledger: Ledger, ask, guard, *, fixture=False, task_goal=""):
        self.browser = browser
        self.ledger = ledger
        self.ask = ask
        self.guard = guard
        self.fixture = fixture
        self.task_goal = " ".join(task_goal.lower().split())
        self.attempted = None
        self.composer = None
        self.page = None
        self.draft = None
        self.mailbox = None
        self.attachments = ()
        self.upload_started = False
        self.verified_to = ""
        self.keep_draft = False

    def _check_page(self):
        page = self.browser.page
        if self.fixture:
            if not page or urlsplit(page.url).hostname != "127.0.0.1":
                raise StepError("Open the local mail fixture.")
        elif not page or urlsplit(page.url).hostname != GMAIL_HOST:
            raise StepError("Open Gmail in Jarvis Chrome.")
        return page

    def _composer(self):
        page = self._check_page()
        dialogs = page.get_by_role("dialog").filter(has=page.locator('input[name="subjectbox"]'))
        if self.page != page or self.composer is None or not self.composer.is_visible():
            if dialogs.count():
                raise StepError(
                    "Close existing Gmail compose windows before Jarvis creates a draft."
                )
            button = page.get_by_role("button", name="Compose", exact=True).and_(
                page.locator(":visible")
            )
            try:
                compose = _unique(button, "Gmail Compose button", timeout=4500)
            except StepError:
                compose = _unique(
                    page.get_by_text("Compose", exact=True).and_(page.locator(":visible")),
                    "Gmail Compose button",
                )
            self.guard()
            compose.click()
            self.composer = _unique(dialogs, "Gmail draft")
            self.page = page
            self.mailbox = self._account_url(page.url)
            self.draft = None
            self.attachments = ()
            self.upload_started = False
            self.verified_to = ""
        return self.composer

    @staticmethod
    def _account_url(url):
        parts = urlsplit(url)
        match = re.match(r"(/mail/u/\d+/)", parts.path)
        path = match.group(1) if match else parts.path
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _recipient_input(dialog):
        return _unique(
            dialog.locator(
                'input[role="combobox"]:visible,input[name="to"]:visible,'
                'textarea[name="to"]:visible,input[aria-label="To recipients"]:visible'
            ),
            "To field",
        )

    @staticmethod
    def _recipients(dialog):
        fields = dialog.locator(
            "[email]:visible,[data-hovercard-id]:visible,[data-email]:visible,[data-jarvis-recipient]:visible"
        )
        result = set()
        for element in fields.all():
            values = [
                element.get_attribute(key) or ""
                for key in ("email", "data-hovercard-id", "data-email", "data-jarvis-recipient")
            ]
            result.update(value.lower() for value in values if EMAIL.fullmatch(value))
        return result

    def _resolve(self, dialog, query):
        if EMAIL.fullmatch(query):
            return query.lower()
        field = self._recipient_input(dialog)
        self.guard()
        field.fill(query)
        page = self.page
        deadline = time.monotonic() + 4
        candidates = {}
        stable_for = 0
        while time.monotonic() < deadline:
            self.guard()
            suggestions = page.locator(
                '[role="option"]:visible,[role="listbox"] [email]:visible,'
                '[role="listbox"] [data-hovercard-id]:visible'
            )
            current = {}
            for item in suggestions.all()[:20]:
                name = item.inner_text(timeout=800).strip()
                attrs = [item.get_attribute(key) or "" for key in ("email", "data-hovercard-id")]
                addresses = [address for address in EMAIL.findall(" ".join([name] + attrs))]
                name_words = set(re.findall(r"[\w]+", name.lower()))
                if (
                    len(set(addresses)) == 1
                    and set(re.findall(r"[\w]+", query.lower())) <= name_words
                ):
                    current[addresses[0].lower()] = " ".join(name.split())[:160]
            if current and current == candidates:
                stable_for += 1
                if stable_for >= 3:
                    break
            else:
                stable_for = 0
            candidates = current
            page.wait_for_timeout(300)
        self.guard()
        field.fill("")
        if len(candidates) == 1:
            return next(iter(candidates))
        options = sorted(candidates)
        answer = self.ask(f"Which email address should Jarvis use for {query}?", options)
        if not EMAIL.fullmatch(answer) or (options and answer.lower() not in options):
            raise StepError(
                "Select a matching address or enter a full email address if no match was shown."
            )
        return answer.lower()

    @staticmethod
    def _body(dialog):
        return _unique(
            dialog.locator(
                '[contenteditable="true"][role="textbox"]:visible,'
                '[contenteditable="true"][aria-label="Message Body"]:visible'
            ),
            "message body",
        )

    @staticmethod
    def _attachment_visible(scope, name):
        return (
            scope.get_by_text(name, exact=True).and_(scope.locator(":visible")).count() > 0
            or scope.get_by_title(name, exact=True).and_(scope.locator(":visible")).count() > 0
            or scope.get_by_label(re.compile(r"(?:Attachment|Download)\s+" + re.escape(name), re.I))
            .and_(scope.locator(":visible"))
            .count()
            > 0
        )

    def prepare(self, query, subject, body, files):
        dialog = self._composer()
        self.draft = None
        email = self.verified_to or self._resolve(dialog, query)
        if not self.verified_to:
            if self._recipients(dialog):
                raise StepError(
                    "The draft already has a recipient. Inspect it in Chrome before continuing."
                )
            field = self._recipient_input(dialog)
            self.guard()
            field.fill(email)
            field.press("Enter")
            self.page.wait_for_timeout(250)
            if self._recipients(dialog) != {email}:
                raise StepError("Gmail did not commit the expected recipient.")
            self.verified_to = email
        if EMAIL.fullmatch(query) and query.lower() != email:
            raise StepError("The draft recipient changed. Close this draft and start a new one.")
        self.guard()
        _unique(dialog.locator('input[name="subjectbox"]:visible'), "subject field").fill(subject)
        self.guard()
        self._body(dialog).fill(body)
        files = tuple(files)
        names = [file.path.name for file in files]
        if len(set(names)) != len(names):
            raise StepError("Use attachments with distinct filenames.")
        paths = [file.check() for file in files]
        if self.upload_started and self.attachments != files:
            raise StepError(
                "Attachment upload was interrupted. Inspect the draft before starting a new task."
            )
        if files and not self.upload_started:
            self.guard()
            attach = _unique(
                dialog.get_by_label("Attach files", exact=True).or_(
                    dialog.get_by_role("button", name="Attach files", exact=True)
                ),
                "Attach files button",
            )
            self.upload_started = True
            with self.page.expect_file_chooser(timeout=4500) as chooser:
                self.guard()
                attach.click()
            self.guard()
            chooser.value.set_files([str(path) for path in paths], timeout=4500)
            self.attachments = files
        draft = Draft(email, subject, body, files)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.guard()
            if self._matches(dialog, draft):
                self.draft = draft
                return draft
            self.page.wait_for_timeout(250)
        raise StepError(
            "Gmail did not show the exact recipient, subject, body and completed attachments."
        )

    def _matches(self, dialog, draft):
        recipient_input = self._recipient_input(dialog)
        if recipient_input.input_value().strip():
            return False
        if self._recipients(dialog) != {draft.email}:
            return False
        for label in ("Cc", "Bcc"):
            field = dialog.locator(f'input[name="{label.lower()}"]:visible')
            if field.count() and field.first.input_value().strip():
                return False
        subject = dialog.locator('input[name="subjectbox"]:visible')
        if subject.count() != 1 or subject.input_value() != draft.subject:
            return False
        if self._body(dialog).inner_text() != draft.body:
            return False
        if dialog.get_by_role("progressbar").and_(dialog.locator(":visible")).count():
            return False
        removals = dialog.get_by_label(re.compile(r"^Remove attachment", re.I)).and_(
            dialog.locator(":visible")
        )
        if removals.count() and removals.count() != len(draft.files):
            return False
        return all(self._attachment_visible(dialog, item.path.name) for item in draft.files)

    def _sent_match(self, draft):
        """Require one exact result and verify message fields in its Sent view."""
        self.guard()
        lookup = self.page.context.new_page()
        try:
            query_subject = re.sub(r'["\\\r\n{}()]', " ", draft.subject)
            query = quote_plus(f'in:sent to:{draft.email} subject:"{query_subject}"', safe="")
            lookup.goto(f"{self.mailbox}#search/{query}", wait_until="domcontentloaded")
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                self.guard()
                results = (
                    lookup.get_by_role("main")
                    .get_by_text(draft.subject, exact=True)
                    .and_(lookup.locator(":visible"))
                )
                if results.count() == 1:
                    break
                if results.count() > 1:
                    return None
                empty = (
                    lookup.get_by_role("main")
                    .get_by_text(
                        re.compile(
                            r"^No (?:conversations found|messages matched your search)\b", re.I
                        )
                    )
                    .and_(lookup.locator(":visible"))
                )
                if empty.count() == 1 and not lookup.locator("tr[role=row]:visible").count():
                    return False
                lookup.wait_for_timeout(250)
            else:
                return None
            self.guard()
            results.click()
            main = lookup.get_by_role("main")
            body = main.get_by_text(draft.body, exact=True).and_(lookup.locator(":visible"))
            body.first.wait_for(state="visible", timeout=3500)
            if body.count() != 1:
                return None
            details = main.get_by_label("Show details", exact=True).and_(lookup.locator(":visible"))
            if details.count() == 1:
                self.guard()
                details.click()
            shown_subject = main.get_by_text(draft.subject, exact=True).and_(
                lookup.locator(":visible")
            )
            if shown_subject.count() != 1:
                return None
            recipient_row = (
                lookup.get_by_role("row")
                .filter(has=lookup.get_by_role("cell", name=re.compile(r"^to:$", re.I)))
                .and_(lookup.locator(":visible"))
            )
            if recipient_row.count() != 1:
                return None
            recipients = set(EMAIL.findall(recipient_row.inner_text().lower()))
            if recipients != {draft.email}:
                return None
            return (
                all(self._attachment_visible(main, item.path.name) for item in draft.files) or None
            )
        except PlaywrightError:
            return None
        finally:
            lookup.close()
            if self.page and not self.page.is_closed():
                self.page.bring_to_front()

    def send(self, hands_off):
        draft = self.draft
        if draft is None:
            raise StepError("Prepare and verify a draft before sending.")
        key = draft.key(self.mailbox)
        scope = (
            hashlib.sha256(
                json.dumps(
                    [
                        self.task_goal,
                        self.mailbox,
                        draft.email,
                        sorted(item.digest for item in draft.files),
                    ]
                ).encode()
            ).hexdigest()
            if self.task_goal
            else None
        )
        prior_task = self.attempted or (self.ledger.previous_mail(scope) if scope else None)
        if prior_task and prior_task != key:
            raise StepError(
                "A different draft for this task was already attempted. Check Gmail Sent."
            )
        if self.keep_draft:
            return "Verified draft kept in Gmail."
        previous = self.ledger.state(key)
        found = self._sent_match(draft)
        self.guard()
        if found:
            if previous is None:
                self.ledger.begin(key, scope)
            self.ledger.verified(key)
            return (
                f"Verified in Sent to {draft.email}. Subject “{draft.subject}”. Matching message already existed."
                if not previous
                else (f"Verified in Sent to {draft.email}. Subject “{draft.subject}”.")
            )
        if previous:
            raise StepError(
                "A matching send was already attempted. Check Gmail Sent manually. Jarvis will not resend it."
            )
        if found is None:
            raise StepError(
                "Jarvis could not check Sent for duplicates. Inspect Gmail before attempting to send."
            )
        if not hands_off:
            answer = self.ask(
                "Send the verified Gmail draft?\n\n" + draft.summary(), ["Send", "Keep draft"]
            )
            if answer != "Send":
                self.keep_draft = True
                return "Verified draft kept in Gmail."
        self.guard()
        dialog = self.composer
        if not self._matches(dialog, draft):
            raise StepError("The draft changed after review. Inspect it in Gmail before sending.")
        for item in draft.files:
            item.check()
        send = _unique(
            dialog.get_by_role("button", name=re.compile(r"^Send(?:\s*\(|$)")), "Send button"
        )
        self.guard()
        if not self.ledger.begin(key, scope):
            raise StepError("This send is already being tracked. Check Gmail Sent.")
        self.attempted = key
        self.guard()
        try:
            send.click(timeout=3500)
        except PlaywrightError:
            pass
        if self._sent_match(draft):
            self.ledger.verified(key)
            return f"Verified in Sent to {draft.email}. Subject “{draft.subject}”."
        raise StepError(
            "Send was attempted but could not be verified in Sent. Inspect Gmail Sent. Jarvis will not resend it."
        )
