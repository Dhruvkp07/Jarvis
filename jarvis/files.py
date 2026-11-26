import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis.browser import StepError

FILE_TYPES = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".webp"}


def known_folder(name):
    if sys.platform == "win32":
        from ctypes import (
            POINTER,
            byref,
            c_ubyte,
            c_uint32,
            c_void_p,
            windll,
            wstring_at,
        )
        from uuid import UUID

        folder_ids = {
            "Downloads": "374de290-123f-4565-9164-39c4925e467b",
            "Documents": "fdd39ad0-238f-46af-adb4-6c85480369c7",
            "Desktop": "b4bfcc3a-db2c-424c-b029-7fe99a87c641",
        }
        guid = (c_ubyte * 16).from_buffer_copy(UUID(folder_ids[name]).bytes_le)
        result = c_void_p()
        fn = windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [c_void_p, c_uint32, c_void_p, POINTER(c_void_p)]
        if fn(byref(guid), 0, None, byref(result)) == 0:
            try:
                return Path(wstring_at(result))
            finally:
                windll.ole32.CoTaskMemFree.argtypes = [c_void_p]
                windll.ole32.CoTaskMemFree(result)
    return Path.home() / name


@dataclass(frozen=True)
class SelectedFile:
    path: Path
    size: int
    modified: int
    digest: str

    @classmethod
    def select(cls, path):
        try:
            if path.is_symlink() or path.suffix.lower() not in FILE_TYPES:
                raise StepError("Choose a document, spreadsheet, text file or image.")
            stat = path.stat()
            if stat.st_size > 30 * 1024 * 1024:
                raise StepError("Choose an attachment smaller than 30 MB.")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                read_stat = os.fstat(stream.fileno())
        except OSError:
            raise StepError("The selected file is unavailable.") from None
        if (read_stat.st_size, read_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise StepError("The selected file changed while Jarvis read it.")
        return cls(path, stat.st_size, stat.st_mtime_ns, digest)

    def check(self):
        if self != self.select(self.path):
            raise StepError("An attachment changed after selection. Select it again.")
        return self.path


def search(folder, query):
    query = re.sub(r"^(latest|newest|most recent)\s+", "", query.strip(), flags=re.I)
    words = [w for w in re.findall(r"[\w]+", query.lower()) if w not in {"file", "document", "the"}]
    synonyms = {"quotation": ("quotation", "quote"), "quote": ("quote", "quotation")}
    try:
        candidates = [
            p
            for p in folder.iterdir()
            if not p.is_symlink()
            and p.is_file()
            and p.suffix.lower() in FILE_TYPES
            and words
            and all(
                any(term in p.name.lower() for term in synonyms.get(word, (word,)))
                for word in words
            )
        ]
        return sorted(
            [(p, p.stat().st_mtime_ns) for p in candidates], key=lambda item: item[1], reverse=True
        )
    except OSError:
        raise StepError("Downloads is unavailable. Move the file there and resume.") from None


def display_file(path, modified):
    when = datetime.fromtimestamp(modified / 1e9).strftime("%d %b %Y %H:%M:%S")
    return f"{path.name} ({when})"


def read_notepad(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        value = data.decode("utf-16")
    else:
        value = data.decode("utf-8-sig")
    return value.replace("\r\n", "\n")
