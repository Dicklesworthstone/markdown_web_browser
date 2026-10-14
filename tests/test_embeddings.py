"""Real SQLite vector persistence and cosine-ranking contracts, without model-quality claims."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from app.embeddings import EMBEDDING_DIM, SectionEmbedding
from app.store import StorageConfig, Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    for job_id in ("first", "other' OR 1=1 --"):
        instance.allocate_run(
            job_id=job_id,
            url=f"https://example.com/{len(job_id)}",
            started_at=datetime.now(timezone.utc),
        )
    try:
        yield instance
    finally:
        instance.engine.dispose()


def _vector(x: float, y: float) -> list[float]:
    return [x, y] + [0.0] * (EMBEDDING_DIM - 2)


def test_persisted_ranking_matches_independent_geometry(store: Store) -> None:
    store.upsert_embeddings(
        run_id="first",
        sections=[
            SectionEmbedding("same-direction", 0, 1, _vector(3, 4)),
            SectionEmbedding("vertical", 2, 3, _vector(0, 10)),
            SectionEmbedding("horizontal", 4, 5, _vector(10, 0)),
        ],
    )
    query = _vector(6, 8)
    total, matches = store.search_section_embeddings(job_id="first", vector=query, top_k=2)
    assert total == 3
    assert [match.section_id for match in matches] == ["same-direction", "vertical"]
    assert [match.similarity for match in matches] == pytest.approx([1.0, 0.8], abs=1e-6)
    assert [match.distance for match in matches] == pytest.approx([0.0, 0.2], abs=1e-6)
    assert [(match.tile_start, match.tile_end) for match in matches] == [(0, 1), (2, 3)]
    assert query == _vector(6, 8)


def test_upsert_replaces_one_section_and_keeps_runs_isolated(store: Store) -> None:
    other_id = "other' OR 1=1 --"
    store.upsert_embeddings(
        run_id="first", sections=[SectionEmbedding("intro", 0, 1, _vector(1, 0))]
    )
    store.upsert_embeddings(
        run_id=other_id, sections=[SectionEmbedding("other", 9, 10, _vector(1, 0))]
    )
    store.upsert_embeddings(
        run_id="first", sections=[SectionEmbedding("intro", 3, 4, _vector(0, 1))]
    )
    reopened = Store(store.config)
    try:
        total, first = reopened.search_section_embeddings(
            job_id="first", vector=_vector(1, 0), top_k=5
        )
        assert total == 1
        assert [
            (match.section_id, match.tile_start, match.tile_end, match.similarity)
            for match in first
        ] == [("intro", 3, 4, 0.0)]
        total, other = reopened.search_section_embeddings(
            job_id=other_id, vector=_vector(1, 0), top_k=5
        )
        assert total == 1
        assert [match.section_id for match in other] == ["other"]
        assert other[0].similarity == 1.0
    finally:
        reopened.engine.dispose()


def test_equal_scores_return_unique_stable_matches(store: Store) -> None:
    store.upsert_embeddings(
        run_id="first",
        sections=[
            SectionEmbedding(name, index, index + 1, _vector(1, 0))
            for index, name in enumerate(("charlie", "alpha", "bravo"))
        ],
    )
    total, matches = store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=2)
    assert total == 3
    assert len(matches) == 2
    assert len({match.section_id for match in matches}) == 2
    assert {match.section_id for match in matches} <= {"charlie", "alpha", "bravo"}
    assert all(match.similarity == 1.0 and match.distance == 0.0 for match in matches)
    assert store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=2) == (
        total,
        matches,
    )


@pytest.mark.parametrize("query", [[], [1.0] * (EMBEDDING_DIM - 1), [0.0] * EMBEDDING_DIM])
def test_invalid_queries_are_rejected(store: Store, query: list[float]) -> None:
    with pytest.raises(ValueError, match="embedding length|all zeros"):
        store.search_section_embeddings(job_id="first", vector=query, top_k=1)


@pytest.mark.parametrize("top_k", [0, -1])
def test_nonpositive_result_limit_is_rejected(store: Store, top_k: int) -> None:
    with pytest.raises(ValueError, match="top_k must be positive"):
        store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=top_k)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_are_refused_before_storage_or_search(store: Store, value: float) -> None:
    vector = _vector(value, 0)
    with pytest.raises(ValueError, match="finite"):
        store.upsert_embeddings(
            run_id="first", sections=[SectionEmbedding("invalid", 0, 1, vector)]
        )
    with pytest.raises(ValueError, match="finite"):
        store.search_section_embeddings(job_id="first", vector=vector, top_k=1)
    assert store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=5) == (0, [])


def test_invalid_batch_rolls_back_valid_prefix(store: Store) -> None:
    with pytest.raises(ValueError, match="embedding length"):
        store.upsert_embeddings(
            run_id="first",
            sections=[
                SectionEmbedding("valid-prefix", 0, 1, _vector(1, 0)),
                SectionEmbedding("bad-dimension", 2, 3, [1.0]),
            ],
        )
    assert store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=5) == (0, [])


def test_invalid_replacement_preserves_existing_section(store: Store) -> None:
    store.upsert_embeddings(
        run_id="first", sections=[SectionEmbedding("original", 0, 1, _vector(1, 0))]
    )
    with pytest.raises(ValueError, match="embedding length"):
        store.upsert_embeddings(
            run_id="first",
            sections=[
                SectionEmbedding("original", 8, 9, _vector(0, 1)),
                SectionEmbedding("invalid", 10, 11, [1.0]),
            ],
        )
    total, matches = store.search_section_embeddings(job_id="first", vector=_vector(1, 0), top_k=5)
    assert total == 1
    assert [
        (match.section_id, match.tile_start, match.tile_end, match.similarity) for match in matches
    ] == [("original", 0, 1, 1.0)]
