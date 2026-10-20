"""Section embedding persistence, ranking, idempotent re-embedding, and across-run search."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Iterator

from fastapi.testclient import TestClient
import pytest

from app.embedders import HashBucketEmbedder, get_embedder
from app.embeddings import (
    EMBEDDING_DIM,
    EmbeddingMatch,
    SectionEmbedding,
    get_run_embedding_meta,
    search_embeddings,
    search_embeddings_across_runs,
    upsert_embeddings,
)
from app.jobs import _parse_markdown_sections
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


def _unit_vec(dim: int, hot_index: int = 0, secondary_index: int = 1, alpha: float = 0.8) -> list[float]:
    vec = [0.0] * dim
    vec[hot_index % dim] = alpha
    vec[secondary_index % dim] = math.sqrt(max(0.0, 1.0 - alpha * alpha))
    return vec


def test_equal_score_vectors_do_not_crash_and_break_ties_deterministically(tmp_path: Path) -> None:
    """Identical or equal-similarity vectors must never raise TypeError during tie-breaking."""
    store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    store.allocate_run(
        job_id="tie-run",
        url="https://example.com/ties",
        started_at=datetime.now(timezone.utc),
    )

    identical = _unit_vec(EMBEDDING_DIM, hot_index=0, secondary_index=1, alpha=0.9)
    with store.session() as session:
        upsert_embeddings(
            session=session,
            run_id="tie-run",
            sections=[
                SectionEmbedding(section_id="sec-zeta", tile_start=2, tile_end=3, vector=identical),
                SectionEmbedding(section_id="sec-alpha", tile_start=0, tile_end=1, vector=identical),
                SectionEmbedding(section_id="sec-beta", tile_start=1, tile_end=2, vector=identical),
            ],
        )

    total, matches = store.search_section_embeddings(
        job_id="tie-run",
        vector=identical,
        top_k=3,
    )
    assert total == 3
    assert [m.section_id for m in matches] == ["sec-alpha", "sec-beta", "sec-zeta"]
    assert all(isinstance(m, EmbeddingMatch) for m in matches)


def test_invalid_vectors_and_top_k_rejected_cleanly(tmp_path: Path) -> None:
    """Zero vectors, NaN/Inf, wrong dimensions, and non-positive top_k must raise ValueError."""
    store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    store.allocate_run(
        job_id="val-run",
        url="https://example.com/validate",
        started_at=datetime.now(timezone.utc),
    )
    valid = _unit_vec(EMBEDDING_DIM, 0, 1, 0.7)

    with store.session() as session:
        # All-zero vector rejected on upsert
        with pytest.raises(ValueError, match="all zeros"):
            upsert_embeddings(
                session=session,
                run_id="val-run",
                sections=[
                    SectionEmbedding(
                        section_id="zero",
                        tile_start=0,
                        tile_end=0,
                        vector=[0.0] * EMBEDDING_DIM,
                    )
                ],
            )

        # NaN / Inf rejected on upsert
        nan_vec = list(valid)
        nan_vec[0] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            upsert_embeddings(
                session=session,
                run_id="val-run",
                sections=[
                    SectionEmbedding(section_id="nan", tile_start=0, tile_end=0, vector=nan_vec)
                ],
            )

        inf_vec = list(valid)
        inf_vec[0] = float("inf")
        with pytest.raises(ValueError, match="finite"):
            upsert_embeddings(
                session=session,
                run_id="val-run",
                sections=[
                    SectionEmbedding(section_id="inf", tile_start=0, tile_end=0, vector=inf_vec)
                ],
            )

        # Unsupported dimension rejected on upsert
        with pytest.raises(ValueError, match="supported dimension"):
            upsert_embeddings(
                session=session,
                run_id="val-run",
                sections=[
                    SectionEmbedding(
                        section_id="bad-dim",
                        tile_start=0,
                        tile_end=0,
                        vector=[0.5, 0.5, 0.5],
                    )
                ],
            )

        # Insert valid section so we can test search validation
        upsert_embeddings(
            session=session,
            run_id="val-run",
            sections=[SectionEmbedding(section_id="ok", tile_start=0, tile_end=1, vector=valid)],
        )

        with pytest.raises(ValueError, match="top_k must be a positive integer"):
            search_embeddings(session=session, run_id="val-run", query_vector=valid, top_k=0)

        with pytest.raises(ValueError, match="all zeros"):
            search_embeddings(
                session=session,
                run_id="val-run",
                query_vector=[0.0] * EMBEDDING_DIM,
                top_k=1,
            )

        with pytest.raises(ValueError, match="finite"):
            search_embeddings(session=session, run_id="val-run", query_vector=nan_vec, top_k=1)

        with pytest.raises(ValueError, match="Expected embedding length"):
            search_embeddings(
                session=session,
                run_id="val-run",
                query_vector=_unit_vec(384, 0, 1, 0.8),
                top_k=1,
            )


def test_multi_dimension_embeddings_384_and_incompatible_space_guard(tmp_path: Path) -> None:
    """384-dim embeddings (e.g. sentence-transformers) are stored and searched accurately without mixing spaces."""
    store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    store.allocate_run(
        job_id="st-run",
        url="https://example.com/st",
        started_at=datetime.now(timezone.utc),
    )

    vec_a = _unit_vec(384, hot_index=3, secondary_index=4, alpha=0.95)
    vec_b = _unit_vec(384, hot_index=10, secondary_index=11, alpha=0.95)
    store.upsert_embeddings(
        run_id="st-run",
        model="sentence-transformers",
        sections=[
            SectionEmbedding(
                section_id="pricing",
                tile_start=0,
                tile_end=1,
                vector=vec_a,
                model="sentence-transformers",
                dim=384,
            ),
            SectionEmbedding(
                section_id="security",
                tile_start=2,
                tile_end=3,
                vector=vec_b,
                model="sentence-transformers",
                dim=384,
            ),
        ],
    )

    run_record = store.fetch_run("st-run")
    assert run_record is not None
    assert run_record.embedding_model == "sentence-transformers"
    assert run_record.embedding_dim == 384

    total, matches = store.search_section_embeddings(
        job_id="st-run",
        vector=_unit_vec(384, hot_index=3, secondary_index=4, alpha=0.98),
        top_k=2,
        model="sentence-transformers",
    )
    assert total == 2
    assert matches[0].section_id == "pricing"
    assert matches[0].similarity > 0.95

    # Querying with a 1536-dim vector or wrong model must be rejected cleanly
    with pytest.raises(ValueError, match="Expected embedding length 384"):
        store.search_section_embeddings(
            job_id="st-run",
            vector=_unit_vec(1536, 3, 4, 0.98),
            top_k=1,
        )
    with pytest.raises(ValueError, match="Incompatible embedding model"):
        store.search_section_embeddings(
            job_id="st-run",
            vector=_unit_vec(384, 3, 4, 0.98),
            top_k=1,
            model="hash-bucket-v1",
        )


def test_idempotent_re_embedding_replaces_rows_and_survives_reopen(tmp_path: Path) -> None:
    """Upserting embeddings for the same run replaces prior vec0 rows instead of duplicating them."""
    db_path = tmp_path / "runs.db"
    cache_root = tmp_path / "cache"
    store = Store(StorageConfig(cache_root=cache_root, db_path=db_path))
    store.allocate_run(
        job_id="reembed-run",
        url="https://example.com/reembed",
        started_at=datetime.now(timezone.utc),
    )

    store.upsert_embeddings(
        run_id="reembed-run",
        sections=[
            SectionEmbedding(section_id="s1", tile_start=0, tile_end=0, vector=_unit_vec(1536, 0, 1)),
            SectionEmbedding(section_id="s2", tile_start=1, tile_end=1, vector=_unit_vec(1536, 2, 3)),
            SectionEmbedding(section_id="s3", tile_start=2, tile_end=2, vector=_unit_vec(1536, 4, 5)),
        ],
    )
    total_before, _ = store.search_section_embeddings(
        job_id="reembed-run", vector=_unit_vec(1536, 0, 1), top_k=5
    )
    assert total_before == 3

    # Re-embed with only 2 sections; prior 3 rows must be replaced cleanly
    store.upsert_embeddings(
        run_id="reembed-run",
        sections=[
            SectionEmbedding(section_id="s1-new", tile_start=0, tile_end=1, vector=_unit_vec(1536, 6, 7)),
            SectionEmbedding(section_id="s2-new", tile_start=2, tile_end=3, vector=_unit_vec(1536, 8, 9)),
        ],
    )

    # Re-instantiate Store from disk to prove persistence across process restarts
    reopened = Store(StorageConfig(cache_root=cache_root, db_path=db_path))
    total_after, matches = reopened.search_section_embeddings(
        job_id="reembed-run", vector=_unit_vec(1536, 6, 7), top_k=5
    )
    assert total_after == 2
    assert {m.section_id for m in matches} == {"s1-new", "s2-new"}
    assert matches[0].section_id == "s1-new"


def test_capture_write_markdown_auto_indexes_sections_with_tile_provenance(tmp_path: Path) -> None:
    """Writing stitched markdown automatically indexes sections with tile provenance for immediate search."""
    store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    store.allocate_run(
        job_id="auto-job",
        url="https://example.com/docs",
        started_at=datetime.now(timezone.utc),
    )

    stitched_md = "\n".join(
        [
            "<!-- source: tile_0000, y=0..1200, sha256=aaa, scale=1.0000 -->",
            "# Authentication & API Keys",
            "Use bearer tokens and secret rotation to authenticate requests.",
            "<!-- source: tile_0001, y=1080..2280, sha256=bbb, scale=1.0000 -->",
            "## Enterprise Pricing & Billing",
            "Monthly subscription cost, invoice billing, and enterprise quota tiers.",
            "<!-- source: tile_0002, y=2160..3360, sha256=ccc, scale=1.0000 -->",
            "## Browser Viewport Rendering",
            "Playwright screenshot capture freezes CSS animations and scrolls lazy images.",
        ]
    )

    parsed = _parse_markdown_sections(stitched_md)
    assert [s["anchor"] for s in parsed] == [
        "authentication-api-keys",
        "enterprise-pricing-billing",
        "browser-viewport-rendering",
    ]
    assert parsed[0]["tile_indices"] == [0]
    assert parsed[1]["tile_indices"] == [1]
    assert parsed[2]["tile_indices"] == [2]

    store.write_markdown(job_id="auto-job", content=stitched_md)

    run_record = store.fetch_run("auto-job")
    assert run_record is not None
    assert run_record.embedding_model == "hash-bucket-v1"
    assert run_record.embedding_dim == 1536

    with store.session() as session:
        meta = get_run_embedding_meta(session=session, run_id="auto-job")
    assert meta == ("hash-bucket-v1", 1536)

    embedder = get_embedder("hash-bucket-v1")
    # Synonym query: "tariff fee payment plan" shares concept cluster with "pricing/billing/subscription/cost/invoice"
    query_vec = embedder.embed("tariff fee payment subscription plan")
    total, matches = store.search_section_embeddings(
        job_id="auto-job",
        vector=query_vec,
        top_k=3,
    )
    assert total == 3
    assert matches[0].section_id == "enterprise-pricing-billing"
    assert matches[0].tile_start == 1
    assert matches[0].tile_end == 1
    assert matches[0].similarity > matches[-1].similarity

    # Cached run registration clones section embeddings so cached runs are also immediately searchable
    store.register_cached_run(
        job_id="cached-auto-job",
        source=run_record,
    )
    cached_total, cached_matches = store.search_section_embeddings(
        job_id="cached-auto-job",
        vector=query_vec,
        top_k=3,
    )
    assert cached_total == 3
    assert cached_matches[0].section_id == "enterprise-pricing-billing"


def test_hash_bucket_embedder_position_invariance_and_semantic_clusters() -> None:
    """HashBucketEmbedder is position-invariant to preamble shifts and separates semantic topics."""
    embedder = HashBucketEmbedder()
    doc_a = "Pricing and subscription billing plans for enterprise customers."
    doc_a_shifted = "Note: updated today. Pricing and subscription billing plans for enterprise customers."
    doc_unrelated = "Playwright chromium viewport screenshot tile overlap and CSS animation freeze."

    vec_a = embedder.embed(doc_a)
    vec_shifted = embedder.embed(doc_a_shifted)
    vec_unrelated = embedder.embed(doc_unrelated)

    sim_shifted = sum(x * y for x, y in zip(vec_a, vec_shifted, strict=True))
    sim_unrelated = sum(x * y for x, y in zip(vec_a, vec_unrelated, strict=True))
    assert sim_shifted > 0.75
    assert sim_shifted > sim_unrelated + 0.40


def test_across_runs_search_and_http_embedding_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-run search and FastAPI /jobs/{id}/embeddings/search (vector + query) work end-to-end."""
    import app.main as main_mod

    test_store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    test_store.allocate_run(
        job_id="http-job-1",
        url="https://example.com/one",
        started_at=datetime.now(timezone.utc),
    )
    test_store.allocate_run(
        job_id="http-job-2",
        url="https://example.com/two",
        started_at=datetime.now(timezone.utc),
    )
    test_store.write_markdown(
        job_id="http-job-1",
        content="# Security & Access Control\nBearer token authentication and password credentials.",
    )
    test_store.write_markdown(
        job_id="http-job-2",
        content="# Latency & Throughput Metrics\nBenchmark p95 millisecond duration and SLO budgets.",
    )

    embedder = get_embedder("hash-bucket-v1")
    with test_store.session() as session:
        total, multi_matches = search_embeddings_across_runs(
            session=session,
            query_vector=embedder.embed("login oauth token permission"),
            top_k=2,
            run_ids=["http-job-1", "http-job-2"],
        )
    assert total == 2
    assert multi_matches[0].run_id == "http-job-1"
    assert multi_matches[0].section_id == "security-access-control"

    monkeypatch.setattr(main_mod, "store", test_store)
    monkeypatch.setattr(main_mod.JOB_MANAGER, "store", test_store)
    client = TestClient(main_mod.app)

    # Search by natural language query directly without precomputing vector
    res = client.post(
        "/jobs/http-job-1/embeddings/search",
        json={"query": "bearer token authentication login", "top_k": 2},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["total_sections"] == 1
    assert payload["matches"][0]["section_id"] == "security-access-control"

    # Zero vector rejected with HTTP 400
    bad_res = client.post(
        "/jobs/http-job-1/embeddings/search",
        json={"vector": [0.0] * 1536, "top_k": 2},
    )
    assert bad_res.status_code == 400

    # Wrong dimension rejected with HTTP 400
    wrong_dim_res = client.post(
        "/jobs/http-job-1/embeddings/search",
        json={"vector": [0.5, 0.5, 0.5], "top_k": 2},
    )
    assert wrong_dim_res.status_code == 400

    # /metrics/embedders reflects persisted embedding_model counts
    stats_res = client.get("/metrics/embedders")
    assert stats_res.status_code == 200
    stats = stats_res.json()
    assert stats["counts"].get("hash-bucket-v1", 0) >= 2


def test_across_runs_single_query_matches_incumbent_and_reduces_sql_statements(
    tmp_path: Path,
) -> None:
    """Compare single-query search_embeddings_across_runs against incumbent per-run loop in the same invocation."""
    import time
    from sqlalchemy import event
    from app.embeddings import _search_embeddings_across_runs_incumbent

    store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
    num_runs = 40
    run_ids: list[str] = []
    for i in range(num_runs):
        rid = f"bench-run-{i:03d}"
        run_ids.append(rid)
        store.allocate_run(
            job_id=rid,
            url=f"https://example.com/{i}",
            started_at=datetime.now(timezone.utc),
        )
        store.upsert_embeddings(
            run_id=rid,
            sections=[
                SectionEmbedding(
                    section_id=f"sec-{i}-a",
                    tile_start=0,
                    tile_end=1,
                    vector=_unit_vec(EMBEDDING_DIM, hot_index=i % 20, secondary_index=(i + 1) % 20, alpha=0.85),
                ),
                SectionEmbedding(
                    section_id=f"sec-{i}-b",
                    tile_start=1,
                    tile_end=2,
                    vector=_unit_vec(EMBEDDING_DIM, hot_index=(i + 5) % 20, secondary_index=(i + 6) % 20, alpha=0.75),
                ),
            ],
        )

    query_vec = _unit_vec(EMBEDDING_DIM, hot_index=3, secondary_index=4, alpha=0.9)
    statements_executed: list[str] = []

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        statements_executed.append(statement)

    event.listen(store.engine, "before_cursor_execute", _before_cursor_execute)
    try:
        with store.session() as session:
            statements_executed.clear()
            t0_inc = time.perf_counter()
            inc_total, inc_matches = _search_embeddings_across_runs_incumbent(
                session=session,
                query_vector=query_vec,
                top_k=5,
                run_ids=run_ids,
            )
            inc_ms = (time.perf_counter() - t0_inc) * 1000.0
            inc_stmt_count = len(statements_executed)

            statements_executed.clear()
            t0_new = time.perf_counter()
            new_total, new_matches = search_embeddings_across_runs(
                session=session,
                query_vector=query_vec,
                top_k=5,
                run_ids=run_ids,
            )
            new_ms = (time.perf_counter() - t0_new) * 1000.0
            new_stmt_count = len(statements_executed)
    finally:
        event.remove(store.engine, "before_cursor_execute", _before_cursor_execute)

    assert new_total == inc_total == num_runs * 2
    assert [(m.run_id, m.section_id) for m in new_matches] == [
        (m.run_id, m.section_id) for m in inc_matches
    ]
    # Incumbent executes >= 3 statements per run (120+ statements for 40 runs); single-query executes <= 5
    assert inc_stmt_count >= num_runs * 3
    assert new_stmt_count <= 5
    assert new_ms < inc_ms

