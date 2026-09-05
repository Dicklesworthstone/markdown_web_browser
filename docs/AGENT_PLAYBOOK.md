# Agent Playbook

> A condensed, example-first guide for AI agents using the `markdown_web_browser` API. Read `README.md` for marketing + setup; this file is the contract you program against.

## 1. Get oriented in one HTTP call

```bash
curl -s http://localhost:8000/schema | jq
```

Returns ~5 KB of grouped intent (capture / live / artifacts / embed / control / observability), invariants, CLI mapping, and `agent_tips`. **Do this first** before exploring the OpenAPI schema.

## 2. Capture one URL (and reuse the cache)

```bash
# Submit + (optionally) stream
curl -s -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com", "reuse_cache": true}'
# Returns 202 + {"id": "abc...", "state": "PENDING", "cache_hit": false}

# Poll status
curl -s http://localhost:8000/jobs/abc
# Once state == "DONE", fetch the structured result
curl -s http://localhost:8000/jobs/abc/result.json | jq
```

The `cache_key` is content-addressed: `url + CfT + viewport + DSF + OCR model + profile_id`. Identical requests return `cache_hit: true` instantly.

## 3. Capture many URLs (one round-trip)

```bash
curl -s -X POST http://localhost:8000/jobs/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "urls": ["https://example.com", "https://news.ycombinator.com", "https://github.com/python/cpython"],
    "reuse_cache": true,
    "tags": ["dataset:2026-q1", "team:research"]
  }'
# Returns 202 + BatchJobResponse with per-URL cache_hit + job_id
```

Each URL becomes its own job (visible in `/jobs`), so you can `GET /jobs/{id}` on any of them independently. Use this for "process this list of pages" workflows where you want amortized HTTP overhead.

## 4. Tag a job (for scoped follow-up queries)

```bash
# Tag a job after the fact
curl -s -X POST http://localhost:8000/jobs/abc/tag \
  -H 'Content-Type: application/json' \
  -d '{"tag": "reviewed"}'

# Or submit with --tag baked in
mdwb jobs tag abc high-priority

# Then scope a follow-up query
curl -s "http://localhost:8000/jobs?tag=high-priority&state=DONE&limit=50" | jq
```

Tags are persisted on the SQLite `runs` row, so they survive restarts.

## 5. Get the structured result (TOC + outbound links)

```bash
curl -s http://localhost:8000/jobs/abc/result.json | jq
```

Returns:
```json
{
  "job_id": "abc",
  "url": "https://example.com",
  "state": "DONE",
  "word_count": 412,
  "char_count": 2403,
  "sections": [
    {"level": 1, "heading": "Example Domain", "body": "...", "anchor": "example-domain", "tile_indices": []}
  ],
  "links": [
    {"href": "https://iana.org/domains/example", "text": "IANA", "source": "dom", "delta": "match"}
  ],
  "cache_hit": false,
  "profile_id": null
}
```

**Prefer this over `/result.md`** when you want a TOC or outbound-link list without re-parsing Markdown.

## 6. Depth-1 crawl (one seed → many pages)

```bash
curl -s -X POST http://localhost:8000/jobs/crawl \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://news.ycombinator.com",
    "max_pages": 10,
    "max_depth": 1,
    "domain_allowlist": ["news.ycombinator.com", "ycombinator.com"],
    "respect_robots_txt": true
  }'
# Returns 202 + {"crawl_id": "crawl_abc", "queued_urls": [...]}

# Poll crawl status
curl -s http://localhost:8000/crawl/crawl_abc | jq
```

Each visited URL becomes its own job; depth-1 expansion reads the seed's `links.json` so anchors are the same ones the dashboard shows.

## 7. Embeddings search (semantic)

```bash
# You compute a 1536-dim float32 vector, then:
curl -s -X POST http://localhost:8000/jobs/abc/embeddings/search \
  -H 'Content-Type: application/json' \
  -d '{"vector": [0.012, -0.034, ...], "top_k": 5}'
```

Returns sections ranked by cosine distance. Sections are auto-extracted from the stitched Markdown at job time and persisted in `sqlite-vec`.

## 8. Webhooks beat polling for long captures

```bash
curl -s -X POST http://localhost:8000/jobs/abc/webhooks \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://myagent.com/cb", "events": ["DONE", "FAILED"]}'
```

POSTed to your URL on state transitions. HMAC-SHA256 signed with `WEBHOOK_SECRET` if configured.

## 9. Live event stream (SSE or NDJSON)

```bash
# Server-Sent Events (one event per state/manifest change)
curl -N http://localhost:8000/jobs/abc/stream

# NDJSON (cursor-based; great for tailing long captures)
curl -N "http://localhost:8000/jobs/abc/events?since=2026-01-01T00:00:00Z"
```

## 10. Recipes

### Bulk-archive a list of research papers

```bash
mdwb batch urls.txt --tag dataset:2026-q1 --reuse-cache --watch
# urls.txt: one URL per line, or use `-` for stdin.
```

### Watch a long crawl, then summarize when done

```bash
mdwb crawl https://example.com --max-pages 50 --watch --domain-allowlist example.com
# Each child is its own job. After it finishes:
mdwb jobs result <job_id> --json | jq '.sections[].heading'  # TOC
mdwb jobs embeddings search <job_id> --vector @query.vec.json  # semantic
```

### Tag-and-summarize for a specific question

```bash
# 1. Capture
mdwb fetch https://blog.example.com --tag q:pricing --watch
# 2. Get the structured result + a short agent summary
mdwb jobs result <id> --json
# 3. (Optional) Search for the most relevant section
mdwb jobs embeddings search <id> --vector @query.vec.json
```

## 11. CLI vs API quick reference

| You want | CLI | API |
|---|---|---|
| Submit one URL | `mdwb fetch URL --watch` | `POST /jobs` |
| Submit many URLs | `mdwb batch urls.txt --tag X` | `POST /jobs/batch` |
| Depth-1 crawl | `mdwb crawl URL --max-pages N` | `POST /jobs/crawl` |
| List jobs | `mdwb jobs list --tag X` | `GET /jobs?tag=X` |
| Tag a job | `mdwb jobs tag ID LABEL` | `POST /jobs/{id}/tag` |
| Structured result | `mdwb jobs result ID --json` | `GET /jobs/{id}/result.json` |
| Raw Markdown | `mdwb show ID` or `GET /jobs/ID/result.md` | `GET /jobs/{id}/result.md` |
| Live events | `mdwb events ID --follow` | `GET /jobs/{id}/events?since=...` |
| Embeddings | `mdwb jobs embeddings search ID --vector @v.json` | `POST /jobs/{id}/embeddings/search` |
| Schema discovery | `mdwb schema` | `GET /schema` |
| SLO rollup | `mdwb slo --json` | `GET /metrics/slo` |

## 12. Invariants (read these)

- `cache_key` = `url + CfT + viewport + DSF + OCR model + profile_id` (content-addressed; identical requests return instantly)
- Default OCR model: `olmOCR-2-7B-1025-FP8` (hosted); local vLLM/SGLang supported
- `long_side_px` = 1288, `viewport_overlap_px` = 120
- Embedding dim = 1536 (float32)
- CfT (Chrome for Testing) is pinned; reduced motion + animations frozen for determinism
- Rate limit 429: honor `Retry-After` (2-4 s)

## 13. Common errors and what they mean

| Status | Cause | Fix |
|---|---|---|
| 404 | job_id not found | Confirm via `GET /jobs?url_contains=...` or `mdwb jobs list` |
| 409 | duplicate cache_key under active run | Wait for the active run or pass `reuse_cache=false` |
| 422 | URL validation failed | Must be `http://` or `https://`; non-empty; valid domain |
| 429 | rate limit | Honor `Retry-After`; for big batches add `crawl_delay_ms` or stagger |
| 500 | OCR backend unreachable | Check `mdwb diag` + `mdwb jobs ocr-metrics ID`; or set `OCR_LOCAL_URL` for local |
| 502 | host unreachable (during capture) | Network; retry with `reuse_cache=true` once the host recovers |

## 15. Round-4 surfaces (new in this round)

### Tag, compare, search, embed, slice — without re-rolling the job

```bash
# Multi-tag in one call
curl -X POST http://localhost:8000/jobs/$ID/tags -d '{"tags":["a","b","c"]}' -H 'Content-Type: application/json'

# Single-tag delete
curl -X DELETE http://localhost:8000/jobs/$ID/tag/some-tag

# Cancel an in-flight job
curl -X POST http://localhost:8000/jobs/$ID/cancel?reason=user+stop

# Diff two captures (sections + link sets)
curl -X POST http://localhost:8000/jobs/$A/diff -d '{"other_job_id":"$B"}' -H 'Content-Type: application/json'

# Full-text search across all stored Markdown
curl -X POST http://localhost:8000/jobs/search -d '{"query":"pricing","tag":"dataset:2026-q1"}' -H 'Content-Type: application/json'

# Deterministic embedding (no model weights)
curl -X POST http://localhost:8000/embeddings/text -d '{"text":"hello"}' -H 'Content-Type: application/json'
# Returns 1536-dim L2-normalized float32 vector
```

### Per-job surfaces

```bash
# Raw Markdown (no provenance comments) — perfect for LLM input
curl 'http://localhost:8000/jobs/$ID/result.md?raw=true'
curl 'http://localhost:8000/jobs/$ID/result.md/raw'   # alias

# Per-job SLO (vs. the global /metrics/slo)
curl http://localhost:8000/jobs/$ID/slo
curl http://localhost:8000/jobs/$ID/slo.json   # alias

# Per-source link breakdown (dom | ocr | both | other)
curl http://localhost:8000/jobs/$ID/links | jq '.counts, .by_source | keys'

# Single-shot event log dump
curl http://localhost:8000/jobs/$ID/events.json | jq '.count, (.events | length)'

# Artifact inventory (every file produced)
curl http://localhost:8000/jobs/$ID/artifacts | jq '.files[] | .path'
```

### Batch / multi-job ops

```bash
# Poll N job ids in one call (N <= 500) — replaces N HTTP round-trips for dashboards
curl -X POST http://localhost:8000/jobs/batch/status -d '{"job_ids":["a","b","c"]}' -H 'Content-Type: application/json'

# CLI equivalents
mdwb tags $ID list
mdwb tags $ID add high-priority
mdwb tags $ID rm high-priority
mdwb cancel $ID
mdwb batch-status a b c
mdwb events-json $ID --json > events.json
mdwb links $ID --json | jq '.counts'
mdwb artifacts $ID --json
mdwb schema.json --out /tmp/schema.json   # offline reference
```

## 14. Where to read next

- `README.md` — vision + setup + quickstart
- `docs/GALLERY.md` — copy-pasteable examples
- `docs/architecture.md` — system design
- `docs/api.md` — full API reference
- `docs/ops.md` — operational playbooks
- `GET /schema` — live, agent-oriented API surface
- `GET /openapi.json` — full Swagger schema for code-gen clients
