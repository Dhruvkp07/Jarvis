import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread

import httpx
from pydantic import ValidationError

from jarvis.actions import Action
from jarvis.config import Settings
from jarvis.control import Control, Interrupted, Stopped
from jarvis.files import SelectedFile, search
from jarvis.mail import Ledger, allows_send, user_recipient
from jarvis.providers import Provider, ProviderError, interruptible


class CoreTests(unittest.TestCase):
    def test_action_schema_rejects_freeform_executable_and_coordinates(self):
        with self.assertRaises(ValidationError):
            Action.model_validate({"kind": "launch", "target": "notepad", "command": "echo no"})
        with self.assertRaises(ValidationError):
            Action(kind="vision_click", target="Save", x=1.01, y=0.2)
        with self.assertRaises(ValidationError):
            Action(kind="mail_prepare", target="Priya")

    def test_explicit_user_send_authorization(self):
        goal = "Open the latest quotation in Downloads, open Gmail in Chrome, attach the quotation, and email it to Priya with a short follow-up."
        self.assertTrue(allows_send(goal))
        self.assertEqual(user_recipient(goal), "Priya")
        self.assertFalse(allows_send("Find email from Priya and draft a reply. Do not send it."))
        self.assertFalse(allows_send("Read an email in Gmail."))
        self.assertEqual(user_recipient("Send it to priya@example.test."), "priya@example.test")

    def test_pause_resume_invalidate_actions_and_stop_interrupts_model(self):
        control = Control()
        old = control.wait_ready()
        control.pause()
        control.resume()
        with self.assertRaises(Interrupted):
            control.check(old)
        current = control.wait_ready()
        started = Event()
        release = Event()
        result = []

        def slow():
            started.set()
            release.wait(5)
            return "obsolete"

        def wait_for_answer():
            try:
                interruptible(slow, lambda: control.check(current))
            except Stopped:
                result.append("stopped")

        thread = Thread(target=wait_for_answer)
        thread.start()
        self.assertTrue(started.wait(2))
        control.stop()
        thread.join(1)
        release.set()
        self.assertEqual(result, ["stopped"])

    def test_file_search_order_and_change_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, name in enumerate(("Quotation_old.pdf", "Quote_fresh.pdf", "quotation.exe")):
                path = root / name
                path.write_text("test contents", encoding="utf-8")
                os.utime(path, (1000 + index * 4, 1000 + index * 4))
            matches = search(root, "latest quotation")
            self.assertEqual(
                [item[0].name for item in matches], ["Quote_fresh.pdf", "Quotation_old.pdf"]
            )
            selected = SelectedFile.select(matches[0][0])
            selected.path.write_text("changed file", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed"):
                selected.check()

    def test_persistent_ledger_single_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "operations.db"
            ledger = Ledger(path)
            self.assertTrue(ledger.begin("single"))
            self.assertFalse(Ledger(path).begin("single"))
            self.assertEqual(Ledger(path).state("single"), "pending")
            ledger.verified("single")
            self.assertEqual(Ledger(path).state("single"), "verified")
            self.assertTrue(ledger.begin("first-draft", "original-task"))
            self.assertFalse(Ledger(path).begin("changed-draft", "original-task"))
            self.assertEqual(Ledger(path).previous_mail("original-task"), "first-draft")

    def test_ollama_text_action_and_local_validation(self):
        requests = []

        def handle(req):
            body = json.loads(req.content)
            requests.append((req.url.path, body))
            if req.url.path == "/api/show":
                return httpx.Response(
                    200,
                    json={
                        "modelfile": "FROM C:\\Ollama\\models\\blobs\\sha256-test",
                        "capabilities": ["completion"],
                    },
                )
            return httpx.Response(200, json={"message": {"content": '{"kind":"finish"}'}})

        provider = Provider(Settings(model="local"), transport=httpx.MockTransport(handle))
        self.assertEqual(provider.decide({"goal": "test"}).kind, "finish")
        self.assertNotIn("images", requests[1][1]["messages"][1])
        self.assertIsInstance(requests[1][1]["format"], dict)
        with self.assertRaises(ValueError):
            Provider(Settings(model="local", ollama_url="http://192.0.2.55:11434"))
        with self.assertRaisesRegex(ProviderError, "Enable a vision"):
            provider.decide({}, b"image")
        flagged = Provider(
            Settings(model="local", vision=True), transport=httpx.MockTransport(handle)
        )
        with self.assertRaisesRegex(ProviderError, "text-only"):
            flagged.decide({}, b"image")

    def test_ollama_rejects_cloud_alias_before_task_data(self):
        mocked = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "remote_host": "https://cloud.example",
                    "modelfile": "FROM cloud",
                },
            )
        )
        with self.assertRaisesRegex(ProviderError, "locally installed"):
            Provider(Settings(model="alias"), transport=mocked).decide({"data": "private"})

    def test_gemini_screen_permission_and_http_error_sanitization(self):
        requests = []

        def handle(req):
            requests.append(req)
            return httpx.Response(503, text="private-key sensitive response")

        with self.assertRaisesRegex(ValueError, "local-only"):
            Provider(Settings(provider="gemini", model="test"))
        provider = Provider(
            Settings(provider="gemini", model="test", local_only=False, vision=True),
            transport=httpx.MockTransport(handle),
            key_loader=lambda: "private-key",
        )
        with self.assertRaisesRegex(ProviderError, "Enable a vision"):
            provider.decide({}, b"image")
        self.assertEqual(requests, [])
        provider = Provider(
            Settings(
                provider="gemini", model="test", local_only=False, vision=True, cloud_images=True
            ),
            transport=httpx.MockTransport(handle),
            key_loader=lambda: "private-key",
        )
        with self.assertRaises(ProviderError) as err:
            provider.decide({}, b"image")
        self.assertIn("503", str(err.exception))
        self.assertNotIn("private-key", str(err.exception))
        self.assertNotIn("private-key", str(requests[0].url))
        self.assertIn("inlineData", json.loads(requests[0].content)["contents"][0]["parts"][1])
