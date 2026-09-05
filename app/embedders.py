"""Pluggable embedder backends for /embeddings/text.

Three backends ship:
- ``hash-bucket-v1``: deterministic 1536-dim projection, no model weights (default)
- ``openai-compatible``: proxies to any OpenAI-compatible /v1/embeddings endpoint
- ``sentence-transformers``: local sentence-transformers (optional dependency)

The dispatcher exposes ``embed(text) -> list[float]`` and is consumed by both
``/embeddings/text`` and the on-capture section embedder in JobManager.
"""
from __future__ import annotations

import hashlib
import math
from typing import Protocol


class Embedder(Protocol):
    """Minimal contract every embedder must satisfy."""

    name: str
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


# ---------------------------------------------------------------------------
# Backend 1: hash-bucket-v1 (no model weights)
# ---------------------------------------------------------------------------


class HashBucketEmbedder:
    """Deterministic 1536-dim L2-normalized vector. No model weights required."""

    name = "hash-bucket-v1"
    dim = 1536

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text:
            return vec
        step = 4
        for i in range(0, len(text), step):
            window = text[i : i + step].encode("utf-8")
            digest = hashlib.sha256(window + str(i).encode("ascii")).digest()
            seed = int.from_bytes(digest[:8], "big")
            for k in range(8):
                base = seed % self.dim
                for d in range(8):
                    vec[(base + d) % self.dim] += 1.0
                seed = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
            if len(digest) >= 16 and (digest[15] & 1):
                base2 = (seed >> 8) % self.dim
                for d in range(8):
                    vec[(base2 + d) % self.dim] -= 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Backend 2: openai-compatible
# ---------------------------------------------------------------------------


class OpenAICompatibleEmbedder:
    """Proxies to any OpenAI-compatible /v1/embeddings endpoint.

    Reads ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` from env, or accepts them
    via constructor overrides (for testing).
    """

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        import os

        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        self.timeout_s = timeout_s
        self.dim = 1536  # default; refreshed after first call

    def embed(self, text: str) -> list[float]:
        if not self.base_url:
            raise RuntimeError(
                "OPENAI_BASE_URL is not set; cannot use openai-compatible embedder"
            )
        import httpx

        if self.base_url.endswith("/v1"):
            url = f"{self.base_url}/embeddings"
        else:
            url = f"{self.base_url}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "input": text}
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        embedding = data["data"][0]["embedding"]
        if len(embedding) != self.dim:
            self.dim = len(embedding)
        return list(embedding)


# ---------------------------------------------------------------------------
# Backend 3: sentence-transformers (optional local dep)
# ---------------------------------------------------------------------------


class SentenceTransformersEmbedder:
    """Local sentence-transformers embedder (requires the optional ``local-ocr`` group)."""

    name = "sentence-transformers"

    def __init__(self, model_name: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "sentence-transformers is not installed; pip install '.[local-ocr]'"
            ) from exc
        self.model_name = model_name or "sentence-transformers/all-MiniLM-L6-v2"
        self._model = SentenceTransformer(self.model_name)
        try:
            self.dim = int(self._model.get_sentence_embedding_dimension())
        except Exception:
            self.dim = 384

    def embed(self, text: str) -> list[float]:
        vec = self._model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec.tolist()]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_EMBEDDERS: dict[str, Embedder] = {}


def get_embedder(name: str = "hash-bucket-v1") -> Embedder:
    """Return the named embedder, memoized so we don't re-load models per call."""
    if name in _EMBEDDERS:
        return _EMBEDDERS[name]
    if name == "hash-bucket-v1":
        emb: Embedder = HashBucketEmbedder()
    elif name == "openai-compatible":
        emb = OpenAICompatibleEmbedder()
    elif name == "sentence-transformers":
        emb = SentenceTransformersEmbedder()
    else:
        raise ValueError(f"Unknown embedder {name!r}")
    _EMBEDDERS[name] = emb
    return emb


def list_embedders() -> list[str]:
    """Names of the embedders we know about (the dispatcher can build on demand)."""
    return ["hash-bucket-v1", "openai-compatible", "sentence-transformers"]
