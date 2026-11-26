import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


def state_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    return Path(root or Path.home() / ".local" / "share") / "Jarvis"


@dataclass(frozen=True)
class Settings:
    provider: str = "ollama"
    model: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    vision: bool = False
    cloud_images: bool = False
    local_only: bool = True
    hands_off: bool = False

    def validate(self):
        if self.provider not in {"gemini", "ollama"}:
            raise ValueError("Select Gemini or Ollama.")
        if not self.model.strip():
            raise ValueError("Enter the model name.")
        if self.local_only and self.provider != "ollama":
            raise ValueError("Select Ollama or turn off local-only mode.")
        if self.provider == "ollama":
            parsed = urlsplit(self.ollama_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Use a numeric loopback Ollama address, such as http://127.0.0.1:11434."
                )
            _ = parsed.port
            if "cloud" in self.model.lower():
                raise ValueError("Choose a locally installed Ollama model.")

    def save(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        temp = root / "settings.tmp"
        temp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temp.replace(root / "settings.json")

    @classmethod
    def load(cls, root: Path):
        try:
            return cls(**json.loads((root / "settings.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()


def gemini_key() -> str:
    if sys.platform != "win32":
        return ""
    import win32cred

    try:
        data = win32cred.CredRead("Jarvis/Gemini", win32cred.CRED_TYPE_GENERIC)
    except win32cred.error as exc:
        if exc.winerror == 1168:
            return ""
        raise
    blob = data["CredentialBlob"]
    return blob.decode("utf-16-le") if isinstance(blob, bytes) else blob


def save_gemini_key(secret: str):
    import win32cred

    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": "Jarvis/Gemini",
            "CredentialBlob": secret.encode("utf-16-le"),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "UserName": "Gemini",
        },
        0,
    )


def clear_gemini_key():
    import win32cred

    try:
        win32cred.CredDelete("Jarvis/Gemini", win32cred.CRED_TYPE_GENERIC)
    except win32cred.error as exc:
        if exc.winerror != 1168:
            raise
