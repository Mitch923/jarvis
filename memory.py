"""Tiny long-term memory: a JSON file of short notes, injected into the agent's prompt.

Deliberately simple and inspectable: `!memory` lists it, `!forget <id>` deletes, and the file is
plain JSON you can edit by hand. Writes are atomic and only happen when something changes.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("memory")

MAX_NOTE_CHARS = 300
SECRET_RE = re.compile(r"(ghp_|gho_|github_pat_|sk-or-|sk-ant-|xox[bp]-|AKIA)[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{24,}")


class NoteRefused(Exception):
    """A note was refused; the message is safe to show the model and the user."""


class Memory:
    def __init__(self, data_dir: str, max_notes: int = 40):
        self.path = Path(data_dir) / "memory.json"
        self.max_notes = max_notes
        self._lock = threading.Lock()
        self._notes: list[dict] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self._notes = [n for n in data.get("notes", []) if isinstance(n.get("id"), int) and isinstance(n.get("text"), str)]
            self._next_id = max([data.get("next_id", 1)] + [n["id"] + 1 for n in self._notes])
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError):
            backup = self.path.with_suffix(".corrupt")
            log.exception("memory file unreadable; moving it to %s and starting empty", backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next_id": self._next_id, "notes": self._notes}, indent=1, ensure_ascii=False))
        os.replace(tmp, self.path)  # atomic: a power cut can't leave half a file

    def add(self, text: str, source: str = "user") -> int:
        text = " ".join(text.split())
        if not text:
            raise NoteRefused("Empty note.")
        if len(text) > MAX_NOTE_CHARS:
            raise NoteRefused(f"Note is too long ({len(text)} chars; max {MAX_NOTE_CHARS}). Shorten it.")
        if SECRET_RE.search(text):
            raise NoteRefused("That looks like a secret/API key; I won't store it.")
        with self._lock:
            for n in self._notes:
                if n["text"].lower() == text.lower():
                    return n["id"]  # already known
            if len(self._notes) >= self.max_notes:
                raise NoteRefused(f"Memory is full ({self.max_notes} notes). Ask the user which to forget.")
            note = {"id": self._next_id, "text": text, "source": source, "created": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            self._next_id += 1
            self._notes.append(note)
            self._save()
            return note["id"]

    def remove(self, note_id: int) -> bool:
        with self._lock:
            kept = [n for n in self._notes if n["id"] != note_id]
            if len(kept) == len(self._notes):
                return False
            self._notes = kept
            self._save()
            return True

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._notes)

    def render(self, limit: int = 1500) -> str:
        """Notes as prompt text, newest last, trimmed to `limit` characters (oldest dropped first)."""
        lines = [f"- [{n['id']}] {n['text']}" for n in self.all()]
        while lines and sum(len(x) + 1 for x in lines) > limit:
            lines.pop(0)
        return "\n".join(lines)
