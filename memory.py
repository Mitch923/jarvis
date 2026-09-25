"""Tiny long-term memory: a JSON file of short notes, injected into the agent's prompt.

Deliberately simple and inspectable: `!memory` lists it, `!forget <id>` deletes, and the file is
plain JSON you can edit by hand. Writes are atomic and only happen when something changes.

New in Spec 3:
- Notes are namespaced by `repo` (string) or `null` for global notes.
- Each note has an `embedding` (list[float]) for semantic recall via cosine similarity.
- `recall(query_text, repo=None, top_k=5, threshold=0.35)` returns relevant notes.
- Embeddings use sentence-transformers (all-MiniLM-L6-v2, 384-dim) locally; lazy-loaded.
- Migration on load: legacy notes get `repo=null` and embeddings generated in background.
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
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, fast, ~90MB
EMBEDDING_DIM = 384


class NoteRefused(Exception):
    """A note was refused; the message is safe to show the model and the user."""


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class _EmbeddingModel:
    """Lazy-loaded sentence-transformers model for local embeddings."""

    _instance: "_EmbeddingModel | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self._model = None
        self._load_error: str | None = None

    @classmethod
    def get(cls) -> "_EmbeddingModel":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        if self._load_error is not None:
            return False
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            log.info("Loaded embedding model: %s", EMBEDDING_MODEL_NAME)
            return True
        except Exception as e:  # noqa: BLE001
            self._load_error = str(e)
            log.warning("Failed to load sentence-transformers model (%s); embeddings disabled", e)
            return False

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a list of texts to embeddings. Returns empty lists on failure."""
        if not self._ensure_loaded():
            return [[] for _ in texts]
        try:
            embeddings = self._model.encode(texts, convert_to_tensor=False, normalize_embeddings=True)
            return [emb.tolist() for emb in embeddings]
        except Exception:  # noqa: BLE001
            log.exception("Embedding generation failed")
            return [[] for _ in texts]

    def encode_single(self, text: str) -> list[float]:
        """Encode a single text to embedding. Returns empty list on failure."""
        result = self.encode([text])
        return result[0] if result else []


class Memory:
    def __init__(self, data_dir: str, max_notes: int = 40, *, _test_mode: bool = False):
        self.path = Path(data_dir) / "memory.json"
        self.max_notes = max_notes
        self._lock = threading.Lock()
        self._notes: list[dict] = []
        self._next_id = 1
        self._embedding_model = _EmbeddingModel.get()
        self._load()
        if not _test_mode:
            self._start_background_embedding_generation()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            raw_notes = data.get("notes", [])
            self._notes = []
            for n in raw_notes:
                if not (isinstance(n.get("id"), int) and isinstance(n.get("text"), str)):
                    continue
                # Migration: backfill missing fields
                note = dict(n)
                note.setdefault("repo", None)
                note.setdefault("embedding", [])
                note.setdefault("source", "user")
                note.setdefault("created", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                self._notes.append(note)
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

    def _start_background_embedding_generation(self) -> None:
        """Generate embeddings for notes missing them, in a background thread."""
        missing = [n for n in self._notes if not n.get("embedding")]
        if not missing:
            return

        def worker():
            texts = [n["text"] for n in missing]
            embeddings = self._embedding_model.encode(texts)
            with self._lock:
                # Re-fetch notes in case they changed; match by id
                id_to_note = {n["id"]: n for n in self._notes}
                for note, emb in zip(missing, embeddings):
                    if emb and note["id"] in id_to_note:
                        id_to_note[note["id"]]["embedding"] = emb
                self._save()

        threading.Thread(target=worker, daemon=True, name="memory-embeddings").start()

    def add(self, text: str, source: str = "user", repo: str | None = None) -> int:
        text = " ".join(text.split())
        if not text:
            raise NoteRefused("Empty note.")
        if len(text) > MAX_NOTE_CHARS:
            raise NoteRefused(f"Note is too long ({len(text)} chars; max {MAX_NOTE_CHARS}). Shorten it.")
        if SECRET_RE.search(text):
            raise NoteRefused("That looks like a secret/API key; I won't store it.")
        with self._lock:
            for n in self._notes:
                if n["text"].lower() == text.lower() and n.get("repo") == repo:
                    return n["id"]  # already known
            if len(self._notes) >= self.max_notes:
                raise NoteRefused(f"Memory is full ({self.max_notes} notes). Ask the user which to forget.")
            note = {
                "id": self._next_id,
                "text": text,
                "source": source,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "repo": repo,
                "embedding": [],
            }
            self._next_id += 1
            self._notes.append(note)
            self._save()
        # Generate embedding outside the lock (background)
        emb = self._embedding_model.encode_single(text)
        if emb:
            with self._lock:
                for n in self._notes:
                    if n["id"] == note["id"]:
                        n["embedding"] = emb
                        break
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

    def by_repo(self, repo: str | None) -> list[dict]:
        """Return notes for a specific repo (None for global)."""
        with self._lock:
            return [n for n in self._notes if n.get("repo") == repo]

    def render(self, limit: int = 1500) -> str:
        """Notes as prompt text, newest last, trimmed to `limit` characters (oldest dropped first)."""
        lines = [f"- [{n['id']}] {n['text']}" for n in self.all()]
        while lines and sum(len(x) + 1 for x in lines) > limit:
            lines.pop(0)
        return "\n".join(lines)

    def recall(
        self,
        query_text: str,
        repo: str | None = None,
        top_k: int = 5,
        threshold: float = 0.35,
    ) -> list[dict]:
        """Semantic recall: return top-k notes by cosine similarity to query_text.

        Args:
            query_text: The query to search for.
            repo: If set, only search notes with this repo. If None, search global (repo=None) notes.
                  Note: this does NOT combine global + repo; caller should merge if needed.
            top_k: Maximum number of results to return.
            threshold: Minimum cosine similarity (0-1) to include a note.

        Returns:
            List of note dicts with 'similarity' key added, sorted by similarity descending.
        """
        query_emb = self._embedding_model.encode_single(query_text)
        if not query_emb:
            return []

        with self._lock:
            candidates = [n for n in self._notes if n.get("repo") == repo and n.get("embedding")]

        if not candidates:
            return []

        scored = []
        for n in candidates:
            sim = _cosine_similarity(query_emb, n["embedding"])
            if sim >= threshold:
                scored.append((sim, n))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for sim, n in scored[:top_k]:
            result = dict(n)
            result["similarity"] = round(sim, 3)
            results.append(result)
        return results

    def recall_combined(
        self,
        query_text: str,
        current_repo: str | None = None,
        top_k: int = 5,
        threshold: float = 0.35,
    ) -> list[dict]:
        """Recall relevant notes from global + current repo, merged and deduplicated.

        Returns top_k total notes, preferring current_repo matches first, then global.
        """
        repo_results = self.recall(query_text, repo=current_repo, top_k=top_k, threshold=threshold)
        global_results = self.recall(query_text, repo=None, top_k=top_k, threshold=threshold)

        # Merge, deduplicating by id (repo notes take precedence)
        seen = set()
        merged = []
        for n in repo_results + global_results:
            if n["id"] not in seen:
                seen.add(n["id"])
                merged.append(n)
        return merged[:top_k]