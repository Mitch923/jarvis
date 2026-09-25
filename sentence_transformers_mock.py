"""Mock sentence_transformers module for testing."""
import sys
from types import ModuleType
import numpy as np

# Create a mock module
mock_module = ModuleType("sentence_transformers")

class MockSentenceTransformer:
    def __init__(self, *args, **kwargs):
        pass

    def encode(self, texts, convert_to_tensor=False, normalize_embeddings=True):
        """Return deterministic 384-dim vectors based on text content."""
        result = []
        for t in texts:
            if "tabs" in t:
                vec = [1.0] + [0.0] * 383
            elif "spaces" in t:
                vec = [0.0, 1.0] + [0.0] * 382
            elif "short" in t:
                vec = [0.0, 0.0, 1.0] + [0.0] * 381
            else:
                vec = [0.0] * 384
            result.append(np.array(vec, dtype=np.float32))
        if convert_to_tensor:
            import torch
            return torch.tensor(np.array(result))
        return np.array(result)

    def encode_single(self, text):
        return self.encode([text])[0]

mock_module.SentenceTransformer = MockSentenceTransformer

# Replace the real module
sys.modules["sentence_transformers"] = mock_module