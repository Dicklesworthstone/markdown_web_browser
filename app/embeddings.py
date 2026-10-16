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
SUPPORTED_EMBEDDING_DIMS: tuple[int, ...] = (384, 512, 768, 1024, 1536)
DEFAULT_EMBEDDING_MODEL = "hash-bucket-v1"


@dataclass(frozen=True)
class SectionEmbedding:
    """In-memory representation of a Markdown section embedding."""

    section_id: str
    tile_start: int | None
    tile_end: int | None
    vector: Sequence[float]
    model: str | None = None
    dim: int | None = None


@dataclass(frozen=True)
class EmbeddingMatch:
    """Similarity score for a section embedding."""

    section_id: str
    tile_start: int | None
    tile_end: int | None
    similarity: float
    distance: float
    run_id: str | None = None
    model: str | None = None

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, EmbeddingMatch):
            return NotImplemented
        return (
            self.section_id,
            self.tile_start if self.tile_start is not None else -1,
            self.tile_end if self.tile_end is not None else -1,
            self.run_id or "",
        ) < (
            other.section_id,
            other.tile_start if other.tile_start is not None else -1,
            other.tile_end if other.tile_end is not None else -1,
            other.run_id or "",
        )


def _ensure_meta_table(connection) -> None:  # noqa: ANN001
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS section_embedding_meta (
            run_id TEXT NOT NULL,
            section_id TEXT NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            PRIMARY KEY (run_id, section_id)
        )
        """
    )


def _validate_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError(f"top_k must be a positive integer; got {top_k!r}")


def _validate_finite_nonzero(vector: Sequence[float], *, expected_dim: int | None = None) -> list[float]:
    if not vector:
        raise ValueError("Embedding vector must not be empty")
    if expected_dim is not None and len(vector) != expected_dim:
        raise ValueError(f"Expected embedding length {expected_dim}, received {len(vector)}")
    if expected_dim is None and len(vector) not in SUPPORTED_EMBEDDING_DIMS:
        raise ValueError(
            f"section embedding length must match EMBEDDING_DIM ({EMBEDDING_DIM}) "
            f"or supported dimension {SUPPORTED_EMBEDDING_DIMS}; got {len(vector)}"
        )
    floats: list[float] = []
    sq_sum = 0.0
    for val in vector:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError("Embedding vector elements must be finite numbers")
        fval = float(val)
        if not math.isfinite(fval):
            raise ValueError("Embedding vector must contain only finite numbers")
        floats.append(fval)
        sq_sum += fval * fval
    norm = math.sqrt(sq_sum)
    if norm == 0.0 or not math.isfinite(norm):
        raise ValueError("Embedding vector must not be all zeros")
    return [v / norm for v in floats]


def _pad_to_storage_dim(vector: Sequence[float]) -> list[float]:
    if len(vector) == EMBEDDING_DIM:
        return list(vector)
    if len(vector) < EMBEDDING_DIM:
        return list(vector) + [0.0] * (EMBEDDING_DIM - len(vector))
    raise ValueError(f"Vector length {len(vector)} exceeds storage EMBEDDING_DIM {EMBEDDING_DIM}")


def get_run_embedding_meta(
    *,
    session: Session,
    run_id: str,
) -> tuple[str | None, int | None]:
    """Return (model, dim) recorded for ``run_id`` if present."""
    connection = session.connection()
    _ensure_meta_table(connection)
    row = connection.exec_driver_sql(
        "SELECT model, dim FROM section_embedding_meta WHERE run_id = :run_id LIMIT 1",
        {"run_id": run_id},
    ).fetchone()
    if not row:
        return None, None
    return str(row[0]) if row[0] else None, int(row[1]) if row[1] is not None else None


def upsert_embeddings(
    *,
    session: Session,
    run_id: str,
    sections: Sequence[SectionEmbedding],
    model: str | None = None,
    replace_run: bool = True,
) -> None:
    """Idempotently replace and persist embeddings for ``run_id``."""

    if not sections:
        return

    # Deduplicate by section_id while preserving order (last wins)
    deduped_map: dict[str, SectionEmbedding] = {}
    for section in sections:
        if not section.section_id or not str(section.section_id).strip():
            raise ValueError("section_id must be a non-empty string")
        deduped_map[str(section.section_id)] = section
    ordered_sections = list(deduped_map.values())

    # Validate all vectors and ensure consistent dimension/model before mutating DB
    batch_dim: int | None = None
    batch_model: str | None = model
    prepared: list[tuple[SectionEmbedding, list[float], str, int]] = []
    for section in ordered_sections:
        raw_dim = section.dim if section.dim is not None else len(section.vector)
        if raw_dim != len(section.vector):
            raise ValueError(
                f"section.dim ({raw_dim}) does not match vector length ({len(section.vector)})"
            )
        normalized = _validate_finite_nonzero(section.vector, expected_dim=None)
        if batch_dim is None:
            batch_dim = raw_dim
        elif raw_dim != batch_dim:
            raise ValueError(
                f"Inconsistent embedding dimensions within run {run_id}: {batch_dim} vs {raw_dim}"
            )
        sec_model = section.model or batch_model or DEFAULT_EMBEDDING_MODEL
        if batch_model is None:
            batch_model = sec_model
        elif section.model and section.model != batch_model:
            raise ValueError(
                f"Inconsistent embedding models within run {run_id}: {batch_model!r} vs {section.model!r}"
            )
        padded = _pad_to_storage_dim(normalized)
        prepared.append((section, padded, sec_model, raw_dim))

    connection = session.connection()
    _ensure_meta_table(connection)

    if replace_run:
        # vec0 virtual tables allocate new rowids on INSERT OR REPLACE unless deleted first
        connection.exec_driver_sql(
            "DELETE FROM section_embeddings WHERE run_id = :run_id",
            {"run_id": run_id},
        )
        connection.exec_driver_sql(
            "DELETE FROM section_embedding_meta WHERE run_id = :run_id",
            {"run_id": run_id},
        )
    else:
        for section, _, _, _ in prepared:
            connection.exec_driver_sql(
                "DELETE FROM section_embeddings WHERE run_id = :run_id AND section_id = :section_id",
                {"run_id": run_id, "section_id": section.section_id},
            )

    insert_vec_sql = (
        "INSERT INTO section_embeddings "
        "(run_id, section_id, tile_start, tile_end, embedding) "
        "VALUES (:run_id, :section_id, :tile_start, :tile_end, :embedding)"
    )
    insert_meta_sql = (
        "INSERT OR REPLACE INTO section_embedding_meta "
        "(run_id, section_id, model, dim) "
        "VALUES (:run_id, :section_id, :model, :dim)"
    )
    for section, padded_vec, sec_model, raw_dim in prepared:
        payload = sqlite_vec.serialize_float32(padded_vec)
        connection.exec_driver_sql(
            insert_vec_sql,
            {
                "run_id": run_id,
                "section_id": section.section_id,
                "tile_start": section.tile_start,
                "tile_end": section.tile_end,
                "embedding": payload,
            },
        )
        connection.exec_driver_sql(
            insert_meta_sql,
            {
                "run_id": run_id,
                "section_id": section.section_id,
                "model": sec_model,
                "dim": raw_dim,
            },
        )
    session.commit()


def delete_embeddings(
    *,
    session: Session,
    run_id: str,
    model: str | None = None,
) -> int:
    """Remove embeddings associated with a run (optionally scoped to ``model``)."""

    connection = session.connection()
    _ensure_meta_table(connection)
    if model:
        matching = connection.exec_driver_sql(
            "SELECT section_id FROM section_embedding_meta WHERE run_id = :run_id AND model = :model",
            {"run_id": run_id, "model": model},
        ).fetchall()
        has_any_meta = connection.exec_driver_sql(
            "SELECT 1 FROM section_embedding_meta WHERE run_id = :run_id LIMIT 1",
            {"run_id": run_id},
        ).fetchone()
        if has_any_meta and not matching:
            return 0
    res = connection.exec_driver_sql(
        "DELETE FROM section_embeddings WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    connection.exec_driver_sql(
        "DELETE FROM section_embedding_meta WHERE run_id = :run_id",
        {"run_id": run_id},
    )
    session.commit()
    return int(res.rowcount or 0)


def search_embeddings(
    *,
    session: Session,
    run_id: str,
    query_vector: Sequence[float],
    top_k: int,
    model: str | None = None,
) -> tuple[int, list[EmbeddingMatch]]:
    """Return the most similar sections for the provided query vector."""

    _validate_top_k(top_k)
    connection = session.connection()
    _ensure_meta_table(connection)

    stored_model, stored_dim = get_run_embedding_meta(session=session, run_id=run_id)
    if model is not None and stored_model is not None and model != stored_model:
        raise ValueError(
            f"Incompatible embedding model {model!r} for run {run_id!r} (indexed with {stored_model!r})"
        )
    expected_dim = stored_dim if stored_dim is not None else EMBEDDING_DIM
    normalized_query = _validate_finite_nonzero(query_vector, expected_dim=expected_dim)
    padded_query = _pad_to_storage_dim(normalized_query)
    query_blob = sqlite_vec.serialize_float32(padded_query)

    count_row = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM section_embeddings WHERE run_id = :run_id",
        {"run_id": run_id},
    ).fetchone()
    total = int(count_row[0]) if count_row and count_row[0] is not None else 0
    if total == 0:
        return 0, []

    cursor = connection.exec_driver_sql(
        "SELECT section_id, tile_start, tile_end, "
        "COALESCE(vec_distance_cosine(embedding, :query_blob), 1.0) AS raw_dist "
        "FROM section_embeddings "
        "WHERE run_id = :run_id "
        "ORDER BY ROUND(COALESCE(vec_distance_cosine(embedding, :query_blob), 1.0), 6) ASC, "
        "section_id ASC, COALESCE(tile_start, -1) ASC, COALESCE(tile_end, -1) ASC "
        "LIMIT :limit",
        {"run_id": run_id, "query_blob": query_blob, "limit": top_k},
    )

    heap: list[tuple[tuple[float, str, int, int], EmbeddingMatch]] = []
    for section_id, tile_start, tile_end, raw_dist in cursor.fetchall():
        similarity = round(1.0 - float(raw_dist), 6)
        distance = round(1.0 - similarity, 6)
        match = EmbeddingMatch(
            section_id=str(section_id),
            tile_start=int(tile_start) if tile_start is not None else None,
            tile_end=int(tile_end) if tile_end is not None else None,
            similarity=similarity,
            distance=distance,
            run_id=run_id,
            model=stored_model,
        )
        sort_key = (
            -similarity,
            match.section_id,
            match.tile_start if match.tile_start is not None else -1,
            match.tile_end if match.tile_end is not None else -1,
        )
        heap.append((sort_key, match))

    best = heapq.nsmallest(top_k, heap, key=lambda item: item[0])
    return total, [item[1] for item in best]


def _search_embeddings_across_runs_incumbent(
    *,
    session: Session,
    query_vector: Sequence[float],
    top_k: int,
    run_ids: Sequence[str] | None = None,
    model: str | None = None,
) -> tuple[int, list[EmbeddingMatch]]:
    """Incumbent per-run loop implementation retained for live side-by-side comparison."""
    _validate_top_k(top_k)
    normalized_query = _validate_finite_nonzero(query_vector, expected_dim=None)
    query_dim = len(normalized_query)

    connection = session.connection()
    _ensure_meta_table(connection)

    if run_ids is not None:
        target_runs = [str(r) for r in dict.fromkeys(run_ids) if str(r).strip()]
        if not target_runs:
            return 0, []
        for rid in target_runs:
            s_model, s_dim = get_run_embedding_meta(session=session, run_id=rid)
            if s_dim is not None and s_dim != query_dim:
                raise ValueError(
                    f"Incompatible embedding dimension for run {rid!r}: expected {s_dim}, got {query_dim}"
                )
            if model is not None and s_model is not None and s_model != model:
                raise ValueError(
                    f"Incompatible embedding model for run {rid!r}: expected {s_model!r}, got {model!r}"
                )
    else:
        meta_rows = connection.exec_driver_sql(
            "SELECT DISTINCT run_id, model, dim FROM section_embedding_meta"
        ).fetchall()
        compatible_runs = {
            str(r[0])
            for r in meta_rows
            if int(r[2]) == query_dim and (model is None or str(r[1]) == model)
        }
        if not meta_rows and query_dim == EMBEDDING_DIM:
            all_rows = connection.exec_driver_sql(
                "SELECT DISTINCT run_id FROM section_embeddings"
            ).fetchall()
            compatible_runs = {str(r[0]) for r in all_rows}
        target_runs = sorted(compatible_runs)
        if not target_runs:
            return 0, []

    total = 0
    all_candidates: list[tuple[tuple[float, str, str, int, int], EmbeddingMatch]] = []
    for rid in target_runs:
        run_total, run_matches = search_embeddings(
            session=session,
            run_id=rid,
            query_vector=normalized_query,
            top_k=top_k,
            model=model,
        )
        total += run_total
        for match in run_matches:
            key = (
                -match.similarity,
                rid,
                match.section_id,
                match.tile_start if match.tile_start is not None else -1,
                match.tile_end if match.tile_end is not None else -1,
            )
            all_candidates.append((key, match))

    best = heapq.nsmallest(top_k, all_candidates, key=lambda item: item[0])
    return total, [item[1] for item in best]


def search_embeddings_across_runs(
    *,
    session: Session,
    query_vector: Sequence[float],
    top_k: int,
    run_ids: Sequence[str] | None = None,
    model: str | None = None,
) -> tuple[int, list[EmbeddingMatch]]:
    """Search section embeddings across multiple runs without mixing incompatible spaces."""

    _validate_top_k(top_k)
    normalized_query = _validate_finite_nonzero(query_vector, expected_dim=None)
    query_dim = len(normalized_query)

    connection = session.connection()
    _ensure_meta_table(connection)

    run_models: dict[str, str | None] = {}
    if run_ids is not None:
        target_runs = [str(r) for r in dict.fromkeys(run_ids) if str(r).strip()]
        if not target_runs:
            return 0, []
        placeholders = ", ".join(f":r{i}" for i in range(len(target_runs)))
        params = {f"r{i}": rid for i, rid in enumerate(target_runs)}
        meta_rows = connection.exec_driver_sql(
            f"SELECT run_id, model, dim FROM section_embedding_meta WHERE run_id IN ({placeholders}) GROUP BY run_id",
            params,
        ).fetchall()
        meta_by_run = {
            str(r[0]): (str(r[1]) if r[1] else None, int(r[2]) if r[2] is not None else None)
            for r in meta_rows
        }
        for rid in target_runs:
            s_model, s_dim = meta_by_run.get(rid, (None, None))
            if s_dim is not None and s_dim != query_dim:
                raise ValueError(
                    f"Incompatible embedding dimension for run {rid!r}: expected {s_dim}, got {query_dim}"
                )
            if model is not None and s_model is not None and s_model != model:
                raise ValueError(
                    f"Incompatible embedding model for run {rid!r}: expected {s_model!r}, got {model!r}"
                )
            run_models[rid] = s_model
    else:
        meta_rows = connection.exec_driver_sql(
            "SELECT DISTINCT run_id, model, dim FROM section_embedding_meta"
        ).fetchall()
        compatible_runs: set[str] = set()
        for r in meta_rows:
            rid_str = str(r[0])
            s_model = str(r[1]) if r[1] else None
            s_dim = int(r[2])
            if s_dim == query_dim and (model is None or s_model == model):
                compatible_runs.add(rid_str)
                run_models[rid_str] = s_model
        if not meta_rows and query_dim == EMBEDDING_DIM:
            all_rows = connection.exec_driver_sql(
                "SELECT DISTINCT run_id FROM section_embeddings"
            ).fetchall()
            compatible_runs = {str(r[0]) for r in all_rows}
        target_runs = sorted(compatible_runs)
        if not target_runs:
            return 0, []

    padded_query = _pad_to_storage_dim(normalized_query)
    query_blob = sqlite_vec.serialize_float32(padded_query)
    placeholders = ", ".join(f":r{i}" for i in range(len(target_runs)))
    params_base: dict[str, object] = {f"r{i}": rid for i, rid in enumerate(target_runs)}

    count_row = connection.exec_driver_sql(
        f"SELECT COUNT(*) FROM section_embeddings WHERE run_id IN ({placeholders})",
        params_base,
    ).fetchone()
    total = int(count_row[0]) if count_row and count_row[0] is not None else 0
    if total == 0:
        return 0, []

    query_params = dict(params_base)
    query_params["query_blob"] = query_blob
    query_params["limit"] = top_k
    cursor = connection.exec_driver_sql(
        "SELECT run_id, section_id, tile_start, tile_end, "
        "COALESCE(vec_distance_cosine(embedding, :query_blob), 1.0) AS raw_dist "
        f"FROM section_embeddings WHERE run_id IN ({placeholders}) "
        "ORDER BY ROUND(COALESCE(vec_distance_cosine(embedding, :query_blob), 1.0), 6) ASC, "
        "run_id ASC, section_id ASC, COALESCE(tile_start, -1) ASC, COALESCE(tile_end, -1) ASC "
        "LIMIT :limit",
        query_params,
    )

    all_candidates: list[tuple[tuple[float, str, str, int, int], EmbeddingMatch]] = []
    for rid_val, section_id, tile_start, tile_end, raw_dist in cursor.fetchall():
        rid_str = str(rid_val)
        similarity = round(1.0 - float(raw_dist), 6)
        distance = round(1.0 - similarity, 6)
        match = EmbeddingMatch(
            section_id=str(section_id),
            tile_start=int(tile_start) if tile_start is not None else None,
            tile_end=int(tile_end) if tile_end is not None else None,
            similarity=similarity,
            distance=distance,
            run_id=rid_str,
            model=run_models.get(rid_str, model),
        )
        key = (
            -similarity,
            rid_str,
            match.section_id,
            match.tile_start if match.tile_start is not None else -1,
            match.tile_end if match.tile_end is not None else -1,
        )
        all_candidates.append((key, match))

    best = heapq.nsmallest(top_k, all_candidates, key=lambda item: item[0])
    return total, [item[1] for item in best]


def _validate_query_vector(vector: Sequence[float], *, expected_dim: int = EMBEDDING_DIM) -> None:
    _validate_finite_nonzero(vector, expected_dim=expected_dim)


def _normalize_vector(vector: Sequence[float]) -> list[float]:
    return _validate_finite_nonzero(vector, expected_dim=None)


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
    "SUPPORTED_EMBEDDING_DIMS",
    "DEFAULT_EMBEDDING_MODEL",
    "get_run_embedding_meta",
    "upsert_embeddings",
    "delete_embeddings",
    "search_embeddings",
    "search_embeddings_across_runs",
]
