"""sqlite-vec helpers for section embeddings."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import heapq
import math
from typing import Sequence

import sqlite_vec
from sqlmodel import Session


EMBEDDING_DIM = 1536


@dataclass(frozen=True)
class SectionEmbedding:
    """In-memory representation of a Markdown section embedding."""

    section_id: str
    tile_start: int
    tile_end: int
    vector: Sequence[float]


@dataclass(frozen=True)
class EmbeddingMatch:
    """Similarity score for a section embedding."""

    section_id: str
    tile_start: int | None
    tile_end: int | None
    similarity: float
    distance: float


def upsert_embeddings(
    *,
    session: Session,
    run_id: str,
    sections: Sequence[SectionEmbedding],
) -> None:
    """Insert or update embeddings for the given run."""

    if not sections:
        return

    for section in sections:
        _validate_query_vector(section.vector)

    insert_sql = (
        "INSERT INTO section_embeddings "
        "(run_id, section_id, tile_start, tile_end, embedding) "
        "VALUES (:run_id, :section_id, :tile_start, :tile_end, :embedding)"
    )
    connection = session.connection()
    for section in sections:
        # vec0 has no unique (run_id, section_id) constraint. OR REPLACE alone
        # silently accumulates duplicates; replace this identity explicitly.
        connection.exec_driver_sql(
            "DELETE FROM section_embeddings WHERE run_id = :run_id AND section_id = :section_id",
            {"run_id": run_id, "section_id": section.section_id},
        )
        payload = sqlite_vec.serialize_float32(list(section.vector))
        connection.exec_driver_sql(
            insert_sql,
            {
                "run_id": run_id,
                "section_id": section.section_id,
                "tile_start": section.tile_start,
                "tile_end": section.tile_end,
                "embedding": payload,
            },
        )
    session.commit()


def delete_embeddings(*, session: Session, run_id: str) -> None:
    """Remove embeddings associated with a run (e.g., before regeneration)."""

    session.connection().exec_driver_sql(
        "DELETE FROM section_embeddings WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    session.commit()


def search_embeddings(
    *,
    session: Session,
    run_id: str,
    query_vector: Sequence[float],
    top_k: int,
) -> tuple[int, list[EmbeddingMatch]]:
    """Return the most similar sections for the provided query vector."""

    _validate_query_vector(query_vector)
    if top_k < 1:
        raise ValueError("top_k must be positive")
    normalized_query = _normalize_vector(query_vector)
    cursor = session.connection().exec_driver_sql(
        "SELECT section_id, tile_start, tile_end, embedding "
        "FROM section_embeddings WHERE run_id = :run_id "
        "ORDER BY section_id, tile_start, tile_end",
        {"run_id": run_id},
    )
    total = 0
    heap: list[tuple[float, int, EmbeddingMatch]] = []
    for section_id, tile_start, tile_end, blob in cursor.fetchall():
        total += 1
        similarity = _cosine_similarity(normalized_query, blob)
        match = EmbeddingMatch(
            section_id=section_id,
            tile_start=tile_start,
            tile_end=tile_end,
            similarity=similarity,
            distance=1.0 - similarity,
        )
        if len(heap) < top_k:
            heapq.heappush(heap, (similarity, -total, match))
        else:
            heapq.heappushpop(heap, (similarity, -total, match))

    matches = [item[2] for item in heapq.nlargest(top_k, heap, key=lambda entry: entry[:2])]
    return total, matches


def _validate_query_vector(vector: Sequence[float]) -> None:
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(f"Expected embedding length {EMBEDDING_DIM}, received {len(vector)}")
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("Embedding values must be finite")


def _normalize_vector(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("Embedding vector must not be all zeros")
    return [value / norm for value in vector]


def _cosine_similarity(query: Sequence[float], blob: bytes) -> float:
    target = _deserialize_float32(blob)
    norm = math.sqrt(sum(value * value for value in target))
    if norm == 0:
        return 0.0
    dot = sum(q * (t / norm) for q, t in zip(query, target))
    return round(dot, 6)


def _deserialize_float32(blob: bytes) -> list[float]:
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    arr = array("f")
    arr.frombytes(blob)
    return arr.tolist()


__all__ = [
    "SectionEmbedding",
    "EmbeddingMatch",
    "EMBEDDING_DIM",
    "upsert_embeddings",
    "delete_embeddings",
    "search_embeddings",
]
