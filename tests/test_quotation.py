import json
import tempfile
import unittest
from pathlib import Path

import httpx

from jarvis.browser import Browser
from jarvis.config import Settings
from jarvis.control import Control
from jarvis.engine import Engine
from jarvis.providers import Provider
from tests.fixtures import MAIL
from tests.test_browser import CHROMIUM, ENABLED


@unittest.skipUnless(ENABLED, "Requires a Chromium installation.")
class QuotationTask(unittest.TestCase):
    def test_instruction_to_verified_sent_message(self):
        goal = "Open Gmail, attach the latest quotation in Downloads, and email it to Priya with a short follow-up."
        plan = iter(
            [
                {"kind": "find_file", "target": "quotation", "text": "latest"},
                {"kind": "open_browser", "target": "https://mail.google.com/mail/"},
                {
                    "kind": "mail_prepare",
                    "target": "Priya",
                    "subject": "Quotation Q-107",
                    "text": "Hi Priya, please see the attached quotation.",
                    "files": ["file-1"],
                },
                {"kind": "mail_send"},
                {"kind": "finish"},
            ]
        )

        def inference(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"modelfile": "FROM /models/blobs/sha256-fixture"})
            prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
            self.assertEqual(prompt["goal"], goal)
            self.assertFalse(any("error" in step for step in prompt["recent_results"]))
            return httpx.Response(200, json={"message": {"content": json.dumps(next(plan))}})

        class OfflineMailBrowser(Browser):
            def _ensure(self):
                if self.context is None:
                    super()._ensure()
                    self.context.route(
                        "**/*",
                        lambda route: route.fulfill(
                            content_type="text/html; charset=utf-8", body=MAIL
                        ),
                    )

        clarifications = []

        def choose(question, options, guard):
            guard()
            clarifications.append(question)
            self.assertIn("priya.rao@example.test", options)
            return "priya.rao@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Quotation Q-107.txt").write_text("Quotation Q-107", encoding="utf-8")
            engine = Engine(
                goal,
                root,
                lambda: Settings(model="fixture", hands_off=True),
                Control(),
                lambda _: None,
                choose,
                downloads=root,
                browser_factory=lambda profile, guard: OfflineMailBrowser(
                    profile, guard, executable=CHROMIUM, headless=True
                ),
                provider_factory=lambda settings: Provider(
                    settings, transport=httpx.MockTransport(inference)
                ),
            )
            result = engine.run()
            self.assertIn("Verified in Sent to priya.rao@example.test", result)
            self.assertIn("Quotation Q-107.txt", result)
            self.assertEqual(len(clarifications), 1)
