import os
import shutil
import tempfile
import unittest
from pathlib import Path

from jarvis.actions import Action
from jarvis.browser import Browser, StepError
from jarvis.config import Settings
from jarvis.control import Control
from jarvis.engine import Engine
from jarvis.files import SelectedFile
from jarvis.mail import Gmail, Ledger
from tests.fixtures import fixture_server

CHROMIUM = (
    os.environ.get("JARVIS_TEST_CHROMIUM")
    or shutil.which("chromium")
    or shutil.which("google-chrome")
)
ENABLED = bool(CHROMIUM) or os.name == "nt"


@unittest.skipUnless(ENABLED, "Install Chromium or Google Chrome for real browser checks.")
class BrowserTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        server = fixture_server()
        self.site = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)

    def browser(self, profile, guard):
        return Browser(profile, guard, executable=CHROMIUM, headless=True)

    def test_task_loop_completes_visible_browser_form(self):
        class Planner:
            def __init__(self):
                self.opened = False

            def decide(self, state, _png):
                browser = state["observation"].get("browser")
                if browser is None:
                    return Action(kind="open_browser", target=site)
                verified = state["verified_results"]
                if any("Form submitted" in line for line in verified):
                    return Action(kind="finish")
                if not verified:
                    return Action(kind="read_page")
                if "Browser field value verified." not in str(state["recent_results"]):
                    field = next(
                        key
                        for key, value in browser["controls"].items()
                        if value["name"] == "Full name"
                    )
                    return Action(kind="fill", target=field, text="Dhruv")
                submit = next(
                    key
                    for key, value in browser["controls"].items()
                    if value["name"] == "Submit form"
                )
                return Action(kind="click", target=submit)

        site = self.site
        planner = Planner()
        engine = Engine(
            f"Read and submit the test form at {site}",
            self.root,
            lambda: Settings(model="fixture"),
            Control(),
            lambda _: None,
            lambda *_: self.fail("Routine test form should not require confirmation"),
            browser_factory=self.browser,
            provider_factory=lambda _: planner,
        )
        result = engine.run()
        self.assertIn("Reference code Q-107", result)
        self.assertIn("Form submitted for Dhruv", result)

    def test_stale_browser_target_and_real_input_upload(self):
        browser = self.browser(self.root / "profile", lambda: None)
        self.addCleanup(browser.close)
        page = browser.start_page(self.site)
        state = browser.observe()
        full_name = next(
            key for key, details in state.controls.items() if details["name"] == "Full name"
        )
        page.locator("[name=name]").evaluate("el => el.outerHTML = el.outerHTML")
        with self.assertRaisesRegex(StepError, "changed"):
            browser.fill(full_name, "Dhruv")
        selected = self.root / "quotation.txt"
        selected.write_text("Quotation Q-107", encoding="utf-8")
        state = browser.observe()
        upload = next(key for key, details in state.controls.items() if details["type"] == "file")
        browser.upload(upload, [selected])
        self.assertEqual(
            page.locator("input[type=file]").evaluate("el => el.files[0].name"), selected.name
        )

    def test_browser_reopens_after_the_profile_window_closes(self):
        browser = self.browser(self.root / "restart", lambda: None)
        self.addCleanup(browser.close)
        browser.start_page(self.site)
        browser.context.close()
        self.assertIsNone(browser.observe())
        browser.start_page(self.site)
        self.assertIn("Engineering inquiry", browser.observe().text)

    def test_ambiguous_downloads_asks_once_and_returns_file_reference(self):
        for filename in ("Quotation_A.pdf", "Quotation_B.pdf"):
            (self.root / filename).write_text("fixture", encoding="utf-8")
        asked = []

        def ask(question, choices, _guard):
            asked.append(question)
            return next(choice for choice in choices if "Quotation_B.pdf" in choice)

        engine = Engine(
            "Find the quotation in Downloads",
            self.root,
            lambda: Settings(model="test"),
            Control(),
            lambda _: None,
            ask,
            downloads=self.root,
            browser_factory=self.browser,
        )
        result = engine.execute(Action(kind="find_file", target="quotation"))
        self.assertIn("Quotation_B.pdf", result)
        self.assertEqual(len(asked), 1)

    def _mail(self, suffix=""):
        browser = self.browser(self.root / "mailprofile", lambda: None)
        self.addCleanup(browser.close)
        page = browser.start_page(f"{self.site}/mail/{suffix}")
        calls = []

        def choose(question, options):
            calls.append(question)
            return "Send" if "Send" in options else "priya.rao@example.test"

        attachment = self.root / "Quotation Q107.txt"
        attachment.write_text("Product quotation", encoding="utf-8")
        ledger_path = self.root / "mail.db"
        gmail = Gmail(browser, Ledger(ledger_path), choose, lambda: None, fixture=True)
        draft = gmail.prepare(
            "Priya",
            "Quotation Q-107",
            "Hi Priya, please see the attached quotation.",
            [SelectedFile.select(attachment)],
        )
        self.assertEqual(draft.email, "priya.rao@example.test")
        self.assertEqual(len(draft.files), 1)
        self.assertEqual(len(calls), 1)
        return gmail, draft, page, ledger_path, calls

    def test_reviewed_sends_match_sent_message_and_do_not_duplicate(self):
        gmail, draft, page, ledger, calls = self._mail()
        self.assertIn("Verified in Sent", gmail.send(hands_off=False))
        self.assertIn("Quotation Q107.txt", calls[-1])
        self.assertEqual(page.evaluate("localStorage.attempts"), "1")
        self.assertEqual(Ledger(ledger).state(draft.key(gmail.mailbox)), "verified")
        self.assertIn("Verified in Sent", gmail.send(hands_off=False))
        self.assertEqual(page.evaluate("localStorage.attempts"), "1")
        self.assertEqual(len(calls), 2)

    def test_uncertain_hands_off_send_stays_pending_and_never_retries(self):
        gmail, draft, page, ledger, calls = self._mail("?uncertain=1")
        with self.assertRaisesRegex(StepError, "could not be verified"):
            gmail.send(hands_off=True)
        self.assertEqual(Ledger(ledger).state(draft.key(gmail.mailbox)), "pending")
        gmail.ledger = Ledger(ledger)
        with self.assertRaisesRegex(StepError, "already attempted"):
            gmail.send(hands_off=True)
        self.assertEqual(page.evaluate("localStorage.attempts"), "1")
        self.assertEqual(len(calls), 1)

    def test_changed_draft_after_review_is_not_sent(self):
        gmail, _draft, page, _ledger, _calls = self._mail()

        def change_then_approve(*_):
            page.locator("[name=subjectbox]").fill("Unexpected subject")
            return "Send"

        gmail.ask = change_then_approve
        with self.assertRaisesRegex(StepError, "draft changed"):
            gmail.send(hands_off=False)
        self.assertIsNone(page.evaluate("localStorage.attempts"))
