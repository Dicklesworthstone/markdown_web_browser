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
import re
from importlib import import_module
from typing import Protocol


class Embedder(Protocol):
    """Minimal contract every embedder must satisfy."""

    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
    }
)

# Curated semantic concept clusters so synonym/paraphrase queries align with
# relevant document sections without external model weights.
_CONCEPT_CLUSTERS: dict[str, tuple[str, ...]] = {
    "pricing_billing": (
        "price",
        "pricing",
        "cost",
        "costs",
        "billing",
        "bill",
        "invoice",
        "invoicing",
        "subscription",
        "subscriptions",
        "plan",
        "plans",
        "tier",
        "tiers",
        "fee",
        "fees",
        "payment",
        "payments",
        "pay",
        "paid",
        "charge",
        "charges",
        "monthly",
        "annual",
        "enterprise",
        "quota",
        "credit",
        "credits",
        "budget",
        "discount",
    ),
    "auth_security": (
        "auth",
        "authentication",
        "authenticate",
        "authorize",
        "authorization",
        "login",
        "signin",
        "logout",
        "password",
        "passcode",
        "credential",
        "credentials",
        "token",
        "tokens",
        "apikey",
        "bearer",
        "oauth",
        "sso",
        "session",
        "permission",
        "permissions",
        "access",
        "secret",
        "security",
        "secure",
        "encryption",
        "tls",
        "hmac",
        "signature",
        "ssrf",
        "xss",
    ),
    "install_setup": (
        "install",
        "installation",
        "setup",
        "quickstart",
        "configure",
        "configuration",
        "config",
        "settings",
        "environment",
        "dotenv",
        "deploy",
        "deployment",
        "docker",
        "dockerfile",
        "compose",
        "container",
        "kubernetes",
        "k8s",
        "bootstrap",
        "initialize",
        "init",
    ),
    "performance_latency": (
        "performance",
        "latency",
        "speed",
        "fast",
        "faster",
        "quick",
        "rapid",
        "throughput",
        "slo",
        "sla",
        "p50",
        "p95",
        "p99",
        "benchmark",
        "benchmarks",
        "metric",
        "metrics",
        "millisecond",
        "milliseconds",
        "duration",
        "timing",
        "timings",
        "optimize",
        "optimization",
    ),
    "browser_capture": (
        "browser",
        "playwright",
        "chromium",
        "chrome",
        "cft",
        "viewport",
        "screenshot",
        "screenshots",
        "capture",
        "capturing",
        "scroll",
        "scrolling",
        "sweep",
        "sweeps",
        "tile",
        "tiles",
        "tiling",
        "slice",
        "slicing",
        "cdp",
        "bidi",
        "dom",
        "render",
        "rendering",
    ),
    "ocr_markdown": (
        "ocr",
        "olmocr",
        "glm",
        "vision",
        "markdown",
        "stitch",
        "stitching",
        "seam",
        "overlap",
        "ssim",
        "dedup",
        "deduplication",
        "heading",
        "headings",
        "section",
        "sections",
        "table",
        "tables",
        "provenance",
        "extract",
        "extraction",
    ),
    "search_embeddings": (
        "search",
        "query",
        "retrieve",
        "retrieval",
        "find",
        "lookup",
        "index",
        "indexing",
        "embed",
        "embedder",
        "embedders",
        "embedding",
        "embeddings",
        "vector",
        "vectors",
        "cosine",
        "similarity",
        "semantic",
        "nearest",
        "neighbor",
        "rank",
        "ranking",
    ),
    "database_storage": (
        "database",
        "db",
        "sqlite",
        "sql",
        "sqlmodel",
        "sqlalchemy",
        "store",
        "storage",
        "persist",
        "persistence",
        "cache",
        "caching",
        "cached",
        "artifact",
        "artifacts",
        "manifest",
        "bundle",
        "retention",
        "prune",
        "delete",
        "purge",
    ),
    "api_webhooks": (
        "api",
        "rest",
        "http",
        "https",
        "endpoint",
        "endpoints",
        "route",
        "routes",
        "request",
        "response",
        "webhook",
        "webhooks",
        "callback",
        "sse",
        "stream",
        "streaming",
        "events",
        "json",
        "payload",
    ),
    "error_reliability": (
        "error",
        "errors",
        "bug",
        "bugs",
        "fail",
        "failed",
        "failure",
        "failures",
        "exception",
        "crash",
        "timeout",
        "timeouts",
        "retry",
        "retries",
        "fallback",
        "failover",
        "cancel",
        "cancelled",
        "cancellation",
        "warning",
        "warnings",
        "troubleshoot",
        "troubleshooting",
        "diagnostics",
        "doctor",
        "health",
    ),
    "jobs_workers": (
        "job",
        "jobs",
        "queue",
        "queued",
        "worker",
        "workers",
        "background",
        "async",
        "concurrency",
        "parallel",
        "batch",
        "crawl",
        "crawler",
        "spider",
        "watchdog",
        "status",
        "progress",
    ),
    "automobile_transport": (
        "car",
        "cars",
        "automobile",
        "automobiles",
        "vehicle",
        "vehicles",
        "truck",
        "sedan",
        "suv",
        "driving",
        "motor",
        "engine",
        "highway",
    ),
    "medical_health": (
        "doctor",
        "doctors",
        "physician",
        "medical",
        "medicine",
        "hospital",
        "clinic",
        "patient",
        "patients",
        "treatment",
        "therapy",
        "diagnosis",
        "clinical",
    ),
}

_TOKEN_TO_CONCEPTS: dict[str, list[str]] = {}
for _cid, _words in _CONCEPT_CLUSTERS.items():
    for _w in _words:
        _TOKEN_TO_CONCEPTS.setdefault(_w, []).append(_cid)


def _stem_token(token: str) -> str:
    """Lightweight deterministic English suffix stripper."""
    if len(token) <= 3:
        return token
    for suffix in ("ation", "ition", "ments", "ment", "ings", "ing", "edly", "ness", "able", "ible", "alli", "less"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            base = token[: -len(suffix)]
            if len(base) >= 4 and base[-1] == base[-2]:
                base = base[:-1]
            return base
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("ed") and len(token) > 4:
        base = token[:-2]
        if len(base) >= 4 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _accumulate_feature(vec: list[float], feature: str, weight: float, dim: int) -> None:
    """Project a symbolic feature into ``dim`` coordinates with signed Rademacher probes."""
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    for probe in range(4):
        offset = probe * 8
        chunk = int.from_bytes(digest[offset : offset + 8], "big")
        idx = chunk % dim
        sign = 1.0 if ((chunk >> 32) & 1) == 0 else -1.0
        vec[idx] += sign * weight


# ---------------------------------------------------------------------------
# Backend 1: hash-bucket-v1 (no model weights)
# ---------------------------------------------------------------------------


class HashBucketEmbedder:
    """Deterministic 1536-dim L2-normalized semantic/lexical feature hash embedder."""

    name = "hash-bucket-v1"
    dim = 1536

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text:
            return vec

        tokens = _TOKEN_RE.findall(text.lower())
        content_tokens = [t for t in tokens if t not in _STOPWORDS] or tokens
        stems: list[str] = []
        seen_concepts: dict[str, int] = {}

        for tok in content_tokens:
            stem = _stem_token(tok)
            stems.append(stem)
            _accumulate_feature(vec, f"tok:{tok}", 2.5, self.dim)
            _accumulate_feature(vec, f"stem:{stem}", 3.0, self.dim)
            for candidate in (tok, stem):
                for cid in _TOKEN_TO_CONCEPTS.get(candidate, ()):
                    seen_concepts[cid] = seen_concepts.get(cid, 0) + 1
            if len(tok) >= 4:
                padded = f"<{tok}>"
                for n in (3, 4):
                    for i in range(len(padded) - n + 1):
                        _accumulate_feature(vec, f"cg:{padded[i : i + n]}", 0.6, self.dim)

        for cid, count in seen_concepts.items():
            boost = 4.5 * (1.0 + math.log1p(count))
            _accumulate_feature(vec, f"concept:{cid}", boost, self.dim)

        for i in range(len(stems) - 1):
            _accumulate_feature(vec, f"bi:{stems[i]}_{stems[i + 1]}", 1.5, self.dim)

        # Small whole-text digest component guarantees distinct strings remain distinct
        _accumulate_feature(vec, f"raw:{text}", 0.08, self.dim)

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
        from app.settings import load_config

        config = load_config()
        self.base_url = (
            base_url if base_url is not None else config("OPENAI_BASE_URL", default="")
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else config("OPENAI_API_KEY", default="")
        self.model = (
            model
            if model is not None
            else config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")
        )
        self.timeout_s = timeout_s
        self.dim = 1536  # default; refreshed after first call

    def embed(self, text: str) -> list[float]:
        if not self.base_url:
            raise RuntimeError("OPENAI_BASE_URL is not set; cannot use openai-compatible embedder")
        import httpx

        if self.base_url.endswith("/v1"):
            url = f"{self.base_url}/embeddings"
        else:
            url = f"{self.base_url}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            raw_key = self.api_key.strip()
            headers["Authorization"] = (
                raw_key if raw_key.lower().startswith("bearer ") else f"Bearer {raw_key}"
            )
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
            SentenceTransformer = import_module("sentence_transformers").SentenceTransformer
        except Exception as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "sentence-transformers is not installed; install the optional backend with uv"
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
