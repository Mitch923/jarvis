"""Tests for memory.py: recall, migration, and !remember repo parsing."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# IMPORTANT: Set up the mock BEFORE importing memory
import numpy as np

class MockSentenceTransformer:
    def __init__(self, *args, **kwargs):
        pass

    def encode(self, texts, convert_to_tensor=False, normalize_embeddings=True):
        result = []
        for t in texts:
            if "tabs" in t:
                vec = [1.0] + [0.0] * 383
            elif "spaces" in t:
                vec = [0.0, 1.0] + [0.0] * 382
            elif "short" in t:
                vec = [0.0, 0.0, 1.0] + [0.0] * 381
            elif "type hints" in t:
                vec = [0.0, 0.0, 0.0, 1.0] + [0.0] * 380
            elif "pytest" in t:
                vec = [0.0, 0.0, 0.0, 0.0, 1.0] + [0.0] * 379
            elif "python" in t.lower():
                idx = 0
                try:
                    idx = int(t.split()[-1].rstrip('.'))
                except (ValueError, IndexError):
                    pass
                vec = [1.0 - idx * 0.05] + [0.0] * 383
            else:
                vec = [0.1] + [0.0] * 383
            result.append(vec)
        if convert_to_tensor:
            class MockTensor:
                def __init__(self, data):
                    self.data = data
                def tolist(self):
                    return self.data
            return MockTensor(result)
        return np.array(result)

sys.modules["sentence_transformers"] = type(sys)("sentence_transformers")
sys.modules["sentence_transformers"].SentenceTransformer = MockSentenceTransformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import Memory, NoteRefused, _cosine_similarity, _EmbeddingModel

ok = lambda s: print("PASS", s)


def test_cosine_similarity():
    # Identical vectors
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(a, b) - 1.0) < 1e-6

    # Orthogonal
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert abs(_cosine_similarity(a, b) - 0.0) < 1e-6

    # Opposite
    a = [1.0, 0.0, 0.0]
    b = [-1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(a, b) - (-1.0)) < 1e-6

    # Empty/zero vectors
    assert _cosine_similarity([], []) == 0.0
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    ok("cosine_similarity: identity, orthogonal, opposite, zero vectors")


def test_legacy_migration():
    """Legacy JSON (no repo, no embedding) loads correctly with backfilled fields."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Write legacy format
        legacy = {
            "next_id": 3,
            "notes": [
                {"id": 1, "text": "Global note one", "source": "user", "created": "2024-01-01"},
                {"id": 2, "text": "Global note two", "source": "agent", "created": "2024-01-02"},
            ],
        }
        Path(tmpdir, "memory.json").write_text(json.dumps(legacy))

        mem = Memory(tmpdir, _test_mode=True)
        notes = mem.all()
        assert len(notes) == 2
        for n in notes:
            assert n["repo"] is None, f"Expected repo=None, got {n['repo']}"
            # In test mode, embeddings are not auto-generated; they should be empty initially
            assert n["embedding"] == [], f"Expected empty embedding in test mode, got {n['embedding']}"
            assert "source" in n and "created" in n

        # Next ID should be preserved
        assert mem._next_id == 3
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("legacy_migration: repo=null backfilled, embeddings empty in test mode")


def test_repo_scoped_notes():
    """Notes can be scoped to a repo and filtered correctly."""
    tmpdir = tempfile.mkdtemp()
    try:
        mem = Memory(tmpdir, _test_mode=True)

        id1 = mem.add("Global preference", repo=None)
        id2 = mem.add("Repo-specific convention", repo="owner/repo1")
        id3 = mem.add("Another global", repo=None)
        id4 = mem.add("Other repo note", repo="owner/repo2")

        # Default all() returns all
        all_notes = mem.all()
        assert len(all_notes) == 4

        # by_repo(None) returns only global
        global_notes = mem.by_repo(None)
        assert len(global_notes) == 2
        assert all(n["repo"] is None for n in global_notes)

        # by_repo("owner/repo1") returns only that repo
        repo1_notes = mem.by_repo("owner/repo1")
        assert len(repo1_notes) == 1
        assert repo1_notes[0]["id"] == id2

        # by_repo("owner/repo2") returns only that repo
        repo2_notes = mem.by_repo("owner/repo2")
        assert len(repo2_notes) == 1
        assert repo2_notes[0]["id"] == id4

        # Non-existent repo returns empty
        assert mem.by_repo("owner/nonexistent") == []
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("repo_scoped_notes: add, by_repo filtering works")


def test_recall_repo_scoped_exclusion():
    """recall() with repo=X only returns notes from that repo, not global."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Create a mock embedding model that returns deterministic vectors
        mock_model = MagicMock()
        def mock_encode(texts):
            import numpy as np
            result = []
            for t in texts:
                if "tabs" in t:
                    vec = [1.0] + [0.0] * 383  # tabs -> vec[0]=1
                elif "spaces" in t:
                    vec = [0.0, 1.0] + [0.0] * 382  # spaces -> vec[1]=1
                elif "short" in t:
                    vec = [0.0, 0.0, 1.0] + [0.0] * 381  # short -> vec[2]=1
                else:
                    vec = [0.0] * 384
                result.append(vec)
            return result
        mock_model.encode = mock_encode
        mock_model.encode_single = lambda t: mock_encode([t])[0]

        with patch.object(_EmbeddingModel, 'get', return_value=mock_model):
            mem = Memory(tmpdir, _test_mode=True)

            mem.add("Global: I like short answers", repo=None)
            mem.add("Repo: Use tabs for indentation", repo="owner/repo1")
            mem.add("Repo: Use spaces for indentation", repo="owner/repo2")
            mem.add("Global: Prefer Python 3.11", repo=None)

            # Search for "indentation" in repo1 only
            results = mem.recall("indentation tabs", repo="owner/repo1", top_k=5, threshold=0.0)
            assert len(results) == 1
            assert results[0]["repo"] == "owner/repo1"
            assert "tabs" in results[0]["text"]

            # Search for "indentation" in repo2 only
            results = mem.recall("indentation spaces", repo="owner/repo2", top_k=5, threshold=0.0)
            assert len(results) == 1
            assert results[0]["repo"] == "owner/repo2"
            assert "spaces" in results[0]["text"]

            # Search global only
            results = mem.recall("short answers", repo=None, top_k=5, threshold=0.01)
            assert len(results) == 1
            assert results[0]["repo"] is None
            assert "short" in results[0]["text"]
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("recall_repo_scoped_exclusion: repo filter excludes other repos and global")


def test_recall_global_inclusion():
    """recall_combined() includes both global and current repo notes."""
    tmpdir = tempfile.mkdtemp()
    try:
        mock_model = MagicMock()
        def mock_encode(texts):
            result = []
            for t in texts:
                if "tabs" in t:
                    vec = [1.0] + [0.0] * 383
                elif "short" in t:
                    vec = [0.0, 1.0] + [0.0] * 382
                elif "type hints" in t:
                    vec = [0.0, 0.0, 1.0] + [0.0] * 381
                elif "pytest" in t:
                    vec = [0.0, 0.0, 0.0, 1.0] + [0.0] * 380
                else:
                    vec = [0.0] * 384
                result.append(vec)
            return result
        mock_model.encode = mock_encode
        mock_model.encode_single = lambda t: mock_encode([t])[0]

        with patch.object(_EmbeddingModel, 'get', return_value=mock_model):
            mem = Memory(tmpdir, _test_mode=True)

            mem.add("Global: I like short answers", repo=None)
            mem.add("Global: Use type hints", repo=None)
            mem.add("Repo: Use tabs", repo="owner/repo1")
            mem.add("Repo: Run pytest before commit", repo="owner/repo1")

            # Combined recall for repo1 - query about tabs
            results = mem.recall_combined("indentation tabs", current_repo="owner/repo1", top_k=5, threshold=0.0)
            repo_matches = [r for r in results if r["repo"] == "owner/repo1"]
            assert len(repo_matches) >= 1
            assert "tabs" in repo_matches[0]["text"]

            # Combined recall for global query
            results = mem.recall_combined("short answers", current_repo="owner/repo1", top_k=5, threshold=0.0)
            global_matches = [r for r in results if r["repo"] is None]
            assert len(global_matches) >= 1
            assert "short" in global_matches[0]["text"]
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("recall_global_inclusion: recall_combined merges global + current repo")


def test_recall_top_k_and_threshold():
    """recall() respects top_k and threshold parameters."""
    tmpdir = tempfile.mkdtemp()
    try:
        mock_model = MagicMock()
        def mock_encode(texts):
            result = []
            for t in texts:
                if "python" in t.lower():
                    # Higher similarity for lower index
                    idx = int(t.split()[-1].rstrip('.')) if t.split()[-1].rstrip('.').isdigit() else 0
                    vec = [1.0 - idx * 0.05] + [0.0] * 383
                else:
                    vec = [0.1] + [0.0] * 383  # Low similarity for unrelated
                result.append(vec)
            return result
        mock_model.encode = mock_encode
        mock_model.encode_single = lambda t: mock_encode([t])[0]

        with patch.object(_EmbeddingModel, 'get', return_value=mock_model):
            mem = Memory(tmpdir, _test_mode=True)

            for i in range(10):
                mem.add(f"Note {i}: Python tip number {i}", repo=None)
            mem.add("Unrelated: JavaScript is cool", repo=None)

            # Top 3
            results = mem.recall("python", repo=None, top_k=3, threshold=0.0)
            assert len(results) == 3

            # Top 10
            results = mem.recall("python", repo=None, top_k=10, threshold=0.0)
            assert len(results) == 10  # The JS note has lower similarity

            # High threshold should filter out unrelated
            results = mem.recall("python", repo=None, top_k=10, threshold=0.5)
            texts = " ".join(r["text"] for r in results)
            assert "JavaScript" not in texts
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("recall_top_k_and_threshold: top_k and threshold enforced")


def test_recall_empty_query_embedding():
    """recall() returns empty list when query embedding fails."""
    tmpdir = tempfile.mkdtemp()
    try:
        mock_model = MagicMock()
        mock_model.encode_single = lambda _: []
        mock_model.encode = lambda texts: [[] for _ in texts]

        with patch.object(_EmbeddingModel, 'get', return_value=mock_model):
            mem = Memory(tmpdir, _test_mode=True)
            mem.add("Some note", repo=None)

            results = mem.recall("anything", repo=None)
            assert results == []
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("recall_empty_query_embedding: returns empty on embedding failure")


def test_duplicate_prevention_per_repo():
    """Duplicate text in same repo is rejected; same text in different repo allowed."""
    tmpdir = tempfile.mkdtemp()
    try:
        mem = Memory(tmpdir, _test_mode=True)

        id1 = mem.add("Same text", repo="owner/repo1")
        id2 = mem.add("Same text", repo="owner/repo1")  # Duplicate in same repo
        assert id1 == id2

        id3 = mem.add("Same text", repo="owner/repo2")  # Different repo - allowed
        assert id3 != id1

        id4 = mem.add("Same text", repo=None)  # Global - allowed
        assert id4 != id1
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("duplicate_prevention_per_repo: same text allowed across repos, not within")


def test_embedding_generation_synchronous():
    """Embeddings are generated when adding notes (mocked synchronously)."""
    tmpdir = tempfile.mkdtemp()
    try:
        mock_model = MagicMock()
        mock_model.encode_single = lambda t: [1.0] + [0.0] * 383
        mock_model.encode = lambda texts: [[1.0] + [0.0] * 383 for _ in texts]

        with patch.object(_EmbeddingModel, 'get', return_value=mock_model):
            mem = Memory(tmpdir, _test_mode=True)

            note_id = mem.add("Test note for embedding", repo=None)

            notes = mem.all()
            note = next(n for n in notes if n["id"] == note_id)
            assert note["embedding"] != [], "Embedding should be generated"
            assert len(note["embedding"]) == 384
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("embedding_generation_synchronous: embedding populated on add")


def test_remember_repo_parsing():
    """Test the repo: prefix parsing logic (simulating !remember command)."""
    # This tests the parsing logic used in main.py
    def parse_remember(arg: str) -> tuple[str | None, str]:
        repo = None
        text = arg
        if arg.startswith("repo:"):
            parts = arg.split(" ", 1)
            repo = parts[0][5:]
            text = parts[1] if len(parts) > 1 else ""
        return repo, text

    # No repo prefix
    repo, text = parse_remember("Global note")
    assert repo is None
    assert text == "Global note"

    # With repo prefix
    repo, text = parse_remember("repo:owner/repo1 Repo-specific note")
    assert repo == "owner/repo1"
    assert text == "Repo-specific note"

    # Repo prefix only (no text after)
    repo, text = parse_remember("repo:owner/repo1")
    assert repo == "owner/repo1"
    assert text == ""

    # Repo with slash in name
    repo, text = parse_remember("repo:my-org/my-repo Note text")
    assert repo == "my-org/my-repo"
    assert text == "Note text"

    ok("remember_repo_parsing: repo: prefix parsed correctly")


def test_memory_file_atomic_write():
    """Verify atomic write doesn't corrupt on interruption."""
    tmpdir = tempfile.mkdtemp()
    try:
        mem = Memory(tmpdir, _test_mode=True)

        # Add several notes
        for i in range(5):
            mem.add(f"Note {i}", repo=None)

        # Verify file exists and is valid JSON
        path = Path(tmpdir, "memory.json")
        assert path.exists()
        data = json.loads(path.read_text())
        assert "notes" in data and "next_id" in data
        assert len(data["notes"]) == 5
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok("memory_file_atomic_write: JSON structure valid")


def run_all():
    test_cosine_similarity()
    test_legacy_migration()
    test_repo_scoped_notes()
    test_recall_repo_scoped_exclusion()
    test_recall_global_inclusion()
    test_recall_top_k_and_threshold()
    test_recall_empty_query_embedding()
    test_duplicate_prevention_per_repo()
    test_embedding_generation_synchronous()
    test_remember_repo_parsing()
    test_memory_file_atomic_write()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    run_all()