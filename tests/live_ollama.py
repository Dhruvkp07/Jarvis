"""Run a real Ollama model through a short, read-only Chrome task."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from jarvis.browser import Browser
from jarvis.config import Settings
from jarvis.control import Control
from jarvis.engine import Engine
from jarvis.providers import Provider
from tests.fixtures import fixture_server


def main():
    settings = Settings(model=os.environ.get("JARVIS_LIVE_MODEL", "qwen2.5:3b"))
    Provider(settings).test()
    with TemporaryDirectory() as tmp, fixture_server() as site:
        goal = (
            f"Open {site}/ in Chrome. Read the visible page using read_page, "
            "then finish and report the reference code. Only read the page."
        )

        def clarification(question, _choices, _guard):
            raise RuntimeError(question)

        engine = Engine(
            goal,
            Path(tmp),
            lambda: settings,
            Control(),
            print,
            clarification,
            browser_factory=lambda profile, guard: Browser(profile, guard, headless=True),
        )
        result = engine.run()
        print(result)
        if not any(
            "Q-107" in item and item.startswith("Visible page text") for item in engine.evidence
        ):
            raise RuntimeError("The model did not produce a verified browser reading.")


if __name__ == "__main__":
    main()
