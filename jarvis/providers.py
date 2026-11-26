import base64
import json
from queue import Empty, Queue
from threading import Thread
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from jarvis.actions import PLANNER, SCHEMA, Action
from jarvis.config import Settings, gemini_key


class ProviderError(RuntimeError):
    pass


def interruptible(call, check):
    """Wake the agent on pause/stop without waiting for a remote response."""
    result = Queue(maxsize=1)

    def request():
        try:
            result.put((True, call()))
        except Exception as exc:
            result.put((False, exc))

    check()
    Thread(target=request, daemon=True).start()
    while True:
        check()
        try:
            success, value = result.get(timeout=0.1)
        except Empty:
            continue
        check()
        if success:
            return value
        raise value


class Provider:
    def __init__(self, settings: Settings, transport=None, key_loader=gemini_key):
        settings.validate()
        self.settings = settings
        self.transport = transport
        self.key_loader = key_loader

    def can_see(self):
        s = self.settings
        return s.vision and (s.provider == "ollama" or (not s.local_only and s.cloud_images))

    def _ollama(self, client, system, prompt, image, schema):
        base = self.settings.ollama_url.rstrip("/")
        # Inspect the actual installed model. A local Ollama server can proxy cloud models.
        shown = client.post(f"{base}/api/show", json={"model": self.settings.model})
        shown.raise_for_status()
        metadata = shown.json()
        details = metadata.get("details") or {}
        model_info = metadata.get("model_info") or {}
        modelfile = metadata.get("modelfile", "")
        cloud = metadata.get("remote_model") or metadata.get("remote_host")
        if (
            cloud
            or details.get("remote_host")
            or "cloud" in str(model_info.get("general.architecture", "")).lower()
        ):
            raise ProviderError("Select a locally installed Ollama model.")
        if "FROM " not in modelfile or not any(
            "/blobs/sha256-" in line.replace("\\", "/").lower()
            for line in modelfile.splitlines()
            if line.startswith("FROM ")
        ):
            raise ProviderError(
                "Ollama did not report local model weights. Pull a local model first."
            )
        capabilities = metadata.get("capabilities", [])
        if image and "vision" not in capabilities:
            raise ProviderError(
                "This Ollama model reports text-only capability. Switch models and resume."
            )
        msg = {"role": "user", "content": prompt}
        if image:
            msg["images"] = [image]
        response = client.post(
            f"{base}/api/chat",
            json={
                "model": self.settings.model,
                "stream": False,
                "format": schema,
                "messages": [{"role": "system", "content": system}, msg],
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1800},
            },
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    def _gemini(self, client, system, prompt, image, schema):
        key = self.key_loader()
        if not key:
            raise ProviderError("Save a Gemini API key in Settings, then resume.")
        model = quote(self.settings.model.removeprefix("models/"), safe="")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        parts = [{"text": prompt}]
        if image:
            parts.append({"inlineData": {"mimeType": "image/png", "data": image}})
        response = client.post(
            url,
            headers={"x-goog-api-key": key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                    "temperature": 0,
                },
            },
        )
        response.raise_for_status()
        return "".join(
            p.get("text", "")
            for p in response.json()["candidates"][0]["content"]["parts"]
            if not p.get("thought")
        )

    def _run(self, system, prompt, png, schema):
        if png and not self.can_see():
            raise ProviderError(
                "Enable a vision-capable local model or enable Gemini screen access in Settings."
            )
        image = base64.b64encode(png).decode("ascii") if png else None
        try:
            with httpx.Client(
                timeout=httpx.Timeout(55, connect=5),
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                if self.settings.provider == "ollama":
                    return self._ollama(client, system, prompt, image, schema)
                return self._gemini(client, system, prompt, image, schema)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            raise ProviderError(
                f"Provider returned HTTP {code}. Check the model name, access, or quota."
            ) from None
        except httpx.RequestError:
            raise ProviderError(
                "Model request timed out or could not connect. Check the service and try again."
            ) from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise ProviderError(
                "The provider returned no usable response. Check the selected model."
            ) from None

    def test(self, png=None):
        schema = {
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
            "required": ["ready"],
        }
        raw = self._run(
            "Return ready=true.",
            "Connection test. Any image is a synthetic test card.",
            png,
            schema,
        )
        try:
            if json.loads(raw).get("ready") is True:
                return
        except (ValueError, AttributeError, TypeError):
            pass
        raise ProviderError("The model did not return the expected structured connection test.")

    def decide(self, payload, png=None):
        raw = self._run(PLANNER, json.dumps(payload, ensure_ascii=False), png, SCHEMA)
        try:
            return Action.model_validate_json(raw)
        except ValidationError:
            raise ProviderError(
                "The model returned an invalid action. Jarvis did not execute it."
            ) from None
