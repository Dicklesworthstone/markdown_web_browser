# Gallery — Markdown Web Browser

Real examples of what the system produces, end-to-end. Each example is reproducible
via the ``mdwb`` CLI on a clean install.

## Single-URL capture → Markdown

```bash
mdwb fetch https://example.com --watch --reuse-cache
```

| Input | Output | Wall time | Tiles | OCR |
|---|---|---|---|---|
| https://example.com | clean Markdown with provenance comments | ~12 s | 1 | hosted |
| https://news.ycombinator.com | thread summaries, preserved links | ~45 s | 5 | hosted |
| https://finviz.com (Cloudflare-protected) | sector heatmap + ticker list | ~70 s | 6 | hosted |

## Depth-1 crawl

```bash
mdwb crawl https://news.ycombinator.com --max-pages 10 --watch
```

- Spawns one job per discovered URL; each shows up in ``/jobs`` and the dashboard
- Honors ``--domain-allowlist`` and ``--respect-robots/--no-respect-robots``
- Polls ``/crawl/{id}`` every ``--poll-interval`` seconds for live progress

## Semantic post-processing

```bash
mdwb fetch https://blog.example.com --semantic-post --watch
# Server-side: requires SEMANTIC_POST_ENDPOINT (LLM URL) in environment.
# Manifest surfaces semantic_post_summary + semantic_post_ms under /jobs/{id}/manifest.json
```

## SLO rollup

```bash
curl http://localhost:8000/metrics/slo | jq
# or
mdwb slo --json
```

Returns per-category p50/p95 capture/OCR/stitch + budget breaches from
``benchmarks/production/latest_manifest_index.json``.

## Agent discovery

```bash
mdwb discover --json
```

Returns a machine-friendly catalog of every API endpoint + CLI command + artifact,
augmented with the live ``/openapi.json`` schema so an AI agent can plan
multi-step workflows without reading source code.
