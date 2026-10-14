# API Reference

Complete reference for the Markdown Web Browser REST API.

## Base URL

```
Production: https://mdwb.example.com
Development: http://localhost:8000
```

## Authentication

All requests require an API key (except health check endpoints).

### API Key Header

```http
X-API-Key: mdwb_your_api_key_here
```

### Generate API Key

```bash
# Using the CLI tool
python scripts/manage_api_keys.py create "my-application" --rate-limit 100

# Or via kubectl in Kubernetes
kubectl exec -it deployment/mdwb-web -n mdwb -- \
  python scripts/manage_api_keys.py create "my-app"
```

### Rate Limits

Default: 60 requests/minute per API key

Rate limit headers are included in all responses:

```http
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 42
X-RateLimit-Reset: 1699876543
```

When rate limit is exceeded:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 30

{"detail": "Rate limit exceeded. Please try again later."}
```

## Endpoints

### Job event cursors

`GET /jobs/{id}/events` streams NDJSON entries with a per-job `sequence`.
Reconnect with `?after=<last sequence received>` to resume strictly after that
entry. `?after=-1` requests history from the first entry. `?since=<ISO timestamp>`
is a timestamp filter; use the sequence cursor for exact reconnection.
`GET /jobs/{id}/events.json` returns the same retained entries in one response.
Unknown jobs return 404. Invalid cursors return 400/422; expired or future cursors
return 409 with `code: event-history-gap`, the available sequence range and the
job snapshot URL to reload.

History retains 500 events and each subscription queues at most 128. The stream
replays a subscriber's missed entries while they remain in history. If they have
expired, it emits an `event: gap` NDJSON entry and closes, so clients can reload
the snapshot instead of silently losing progress. Canonical entries and both
cursor sequences are stored atomically in SQLite before publication. Reopening
the API preserves the same IDs and replays the same retained suffix. The active
replay window remains500 entries; older journal rows are retained as storage,
with retention/purge policy still pending. No history is regenerated for results
created before journaling. SSE can display their current stored result without
issuing a fabricated sequence ID.

`GET /jobs/{id}/stream` uses the same event history and replay rules. Supply
`?after=<sequence>` or the native EventSource `Last-Event-ID` header (the header
takes precedence on reconnect). Each entry sends its HTML progress and, for
DONE, Markdown first; the final `job-event` frame carries the canonical JSON
entry and `id: <sequence>`. A disconnect before this final frame replays the
entire update. Heartbeats do not advance the cursor. Expiration returns 409
before streaming, or an SSE `gap` event followed by closure during streaming.

`GET /jobs/stream` streams canonical JSON entries across this owner's jobs.
Its SSE ID is an opaque owner-scoped cursor; supply it unchanged as
`Last-Event-ID` or `?after=<cursor>`. A new subscription starts at the current
tail, then receives live events; reconnect replays strictly after the ID.
Each owner retains up to 500 entries, independently of other owners' traffic.
Cursors from another owner, expired history or a future position return409;
malformed cursors return400. A valid cursor survives process restart. A bounded subscriber recovers drops from retained history
or receives an explicit `gap`. Reload `/jobs` after a gap. The dashboard event
panel also reconnects NDJSON by sequence rather than timestamp.

Both transports poll committed history every second while idle, so another
process writing to the same database is observed without needing a reconnect.
The writer admits at most1024 pending events; each subscriber queues at most128.
Storage failure returns503 with `code: event-journal-unavailable` before a feed
opens, or an `error` entry followed by closure after streaming has started.
Uncommitted events never advance a client cursor.

A crash after a terminal run write but before its event commit is reconciled
under the same SQLite write lock. The journal records one new current snapshot
with `data.reason: terminal-record-recovery`; it does not invent missing earlier
stages. An already acknowledged cancellation/failure reconciles the run's
terminal status without duplicating its event. Durable job admission, requeueing
interrupted captures and worker recovery remain separate unfinished queue work.

### Health Check

```http
GET /health
```

Check if the service is healthy. No authentication required.

**Response 200 OK**:
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "timestamp": "2024-11-09T12:00:00Z"
}
```

---

### Create Capture Job

```http
POST /jobs
```

Submit a URL for capture and conversion to Markdown.

**Headers**:
```http
Content-Type: application/json
X-API-Key: mdwb_your_api_key
```

**Request Body**:
```json
{
  "url": "https://example.com/article",
  "profile_id": "default",
  "reuse_cache": true
}
```

**Parameters**:
- `url` (required): URL to capture
- `profile_id` (optional): Browser profile ID for authenticated captures
- `reuse_cache` (optional, default: true): Use cached result if available

**Response 202 Accepted**:
```json
{
  "id": "job_abc123def456",
  "state": "BROWSER_STARTING",
  "url": "https://example.com/article",
  "created_at": "2024-11-09T12:00:00Z",
  "cache_hit": false
}
```

**Response 200 OK** (Cache Hit):
```json
{
  "id": "job_abc123def456",
  "state": "DONE",
  "url": "https://example.com/article",
  "created_at": "2024-11-09T11:55:00Z",
  "cache_hit": true,
  "cache_source_job_id": "job_xyz789",
  "manifest": { ... }
}
```

---

### Get Job Status

```http
GET /jobs/{job_id}
```

Get the current status and details of a capture job.

**Path Parameters**:
- `job_id`: Job identifier returned from POST /jobs

**Response 200 OK**:
```json
{
  "id": "job_abc123def456",
  "state": "OCR_RUNNING",
  "url": "https://example.com/article",
  "progress": {
    "done": 5,
    "total": 10
  },
  "manifest": {
    "tiles_total": 10,
    "capture_ms": 3421,
    "ocr_ms": null,
    "stitch_ms": null
  }
}
```

**Job States**:
- `BROWSER_STARTING`: Launching browser
- `NAVIGATING`: Loading page
- `SCROLLING`: Performing viewport sweeps
- `CAPTURING`: Taking screenshots
- `TILING`: Processing images
- `OCR_SUBMITTING`: Submitting tiles to OCR
- `OCR_WAITING`: Waiting for OCR completion
- `STITCHING`: Assembling Markdown
- `DONE`: Completed successfully
- `FAILED`: Job failed (check `error` field)

---

### Get Markdown Output

```http
GET /jobs/{job_id}/out.md
```

Download the generated Markdown for a completed job.

**Response 200 OK**:
```markdown
# Example Page

This is the content of the captured page...

<!-- tile_id=0 sha256=abc123... -->
More content from first tile...

---

## Links

### Internal Links
- [Home](https://example.com/)
...
```

**Response 404 Not Found**:
```json
{
  "detail": "Job not found or not completed"
}
```

---

### Get Links JSON

```http
GET /jobs/{job_id}/links.json
```

Download extracted links as JSON.

**Response 200 OK**:
```json
{
  "links": [
    {
      "href": "https://example.com/about",
      "text": "About Us",
      "rel": "",
      "source": "dom"
    }
  ],
  "by_domain": {
    "example.com": [
      {
        "href": "https://example.com/about",
        "text": "About Us"
      }
    ]
  },
  "total": 42,
  "internal": 30,
  "external": 12
}
```

---

### Get Manifest

```http
GET /jobs/{job_id}/manifest.json
```

Download complete capture metadata and provenance information.

**Response 200 OK**:
```json
{
  "url": "https://example.com/article",
  "started_at": "2024-11-09T12:00:00Z",
  "finished_at": "2024-11-09T12:01:23Z",
  "metadata": {
    "cft_version": "chrome-130.0.6723.69",
    "playwright_version": "1.48.0",
    "ocr_model": "olmOCR-2-7B-1025-FP8",
    "viewport": {
      "width": 1280,
      "height": 2000,
      "device_scale_factor": 2
    }
  },
  "tiles_total": 10,
  "capture_ms": 3421,
  "ocr_ms": 8765,
  "stitch_ms": 234,
  "sweep_stats": {
    "sweep_count": 10,
    "total_scroll_height": 18500,
    "shrink_events": 0
  },
  "warnings": [],
  "seam_markers": [
    {
      "prev_tile": 0,
      "next_tile": 1,
      "overlap_hash": "abc123"
    }
  ]
}
```

---

### List Jobs

```http
GET /jobs?limit=20&offset=0&status=DONE
```

List recent jobs (newest first).

**Query Parameters**:
- `limit` (optional, default: 20): Max results
- `offset` (optional, default: 0): Pagination offset
- `status` (optional): Filter by job state

**Response 200 OK**:
```json
{
  "jobs": [
    {
      "id": "job_abc123",
      "url": "https://example.com/article",
      "state": "DONE",
      "created_at": "2024-11-09T12:00:00Z",
      "cache_hit": false
    }
  ],
  "total": 156,
  "limit": 20,
  "offset": 0
}
```

---

### Cancel Job

```http
DELETE /jobs/{job_id}
```

Cancel a pending or running job.

**Response 200 OK**:
```json
{
  "id": "job_abc123",
  "status": "cancelled"
}
```

**Response 404 Not Found**:
```json
{
  "detail": "Job not found"
}
```

---

### Start Crawl

```http
POST /crawl
```

Start a depth-1 crawl from a seed URL.

**Request Body**:
```json
{
  "seed_url": "https://example.com/index.html",
  "max_depth": 1,
  "domain_allowlist": ["example.com"]
}
```

**Response 202 Accepted**:
```json
{
  "crawl_id": "crawl_xyz789",
  "seed_url": "https://example.com/index.html",
  "status": "running",
  "discovered": 0,
  "completed": 0
}
```

---

### Get Crawl Status

```http
GET /crawl/{crawl_id}
```

Get status of a crawl job.

**Response 200 OK**:
```json
{
  "crawl_id": "crawl_xyz789",
  "seed_url": "https://example.com/index.html",
  "status": "running",
  "discovered": 42,
  "completed": 30,
  "failed": 2,
  "urls": [
    {
      "url": "https://example.com/page1",
      "job_id": "job_abc123",
      "status": "DONE"
    }
  ]
}
```

---

### Metrics

```http
GET /metrics
```

Prometheus metrics endpoint. No authentication required.

**Response 200 OK** (Prometheus format):
```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="POST",endpoint="/jobs"} 1234

# HELP capture_duration_seconds_sum Total capture time
# TYPE capture_duration_seconds_sum counter
capture_duration_seconds_sum 45678.9

# HELP ocr_tiles_processed_total Total OCR tiles processed
# TYPE ocr_tiles_processed_total counter
ocr_tiles_processed_total 56789
```

---

## Error Responses

### 400 Bad Request

```json
{
  "detail": "Invalid URL format"
}
```

### 401 Unauthorized

```json
{
  "detail": "Missing API key. Provide X-API-Key header."
}
```

### 404 Not Found

```json
{
  "detail": "Job not found"
}
```

### 429 Too Many Requests

```json
{
  "detail": "Rate limit exceeded. Please try again later."
}
```

### 500 Internal Server Error

```json
{
  "detail": "Internal server error",
  "error_id": "err_abc123"
}
```

---

## Client Examples

### Python

```python
import httpx

API_KEY = "mdwb_your_api_key"
BASE_URL = "https://mdwb.example.com"

async def capture_url(url: str) -> dict:
    headers = {"X-API-Key": API_KEY}

    async with httpx.AsyncClient() as client:
        # Submit job
        response = await client.post(
            f"{BASE_URL}/jobs",
            json={"url": url},
            headers=headers,
        )
        job = response.json()

        # Poll for completion
        while job["state"] not in ["DONE", "FAILED"]:
            await asyncio.sleep(2)
            response = await client.get(
                f"{BASE_URL}/jobs/{job['id']}",
                headers=headers,
            )
            job = response.json()

        # Get markdown
        if job["state"] == "DONE":
            response = await client.get(
                f"{BASE_URL}/jobs/{job['id']}/out.md",
                headers=headers,
            )
            return response.text

        raise Exception(f"Job failed: {job.get('error')}")
```

### cURL

```bash
# Submit job
curl -X POST https://mdwb.example.com/jobs \
  -H "X-API-Key: mdwb_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/article"}'

# Get job status
curl https://mdwb.example.com/jobs/job_abc123 \
  -H "X-API-Key: mdwb_your_api_key"

# Download markdown
curl https://mdwb.example.com/jobs/job_abc123/out.md \
  -H "X-API-Key: mdwb_your_api_key" \
  -o article.md
```

### JavaScript

```javascript
const API_KEY = "mdwb_your_api_key";
const BASE_URL = "https://mdwb.example.com";

async function captureUrl(url) {
  const headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
  };

  // Submit job
  const jobResponse = await fetch(`${BASE_URL}/jobs`, {
    method: "POST",
    headers,
    body: JSON.stringify({ url }),
  });
  let job = await jobResponse.json();

  // Poll for completion
  while (!["DONE", "FAILED"].includes(job.state)) {
    await new Promise(resolve => setTimeout(resolve, 2000));

    const statusResponse = await fetch(
      `${BASE_URL}/jobs/${job.id}`,
      { headers }
    );
    job = await statusResponse.json();
  }

  // Get markdown
  if (job.state === "DONE") {
    const mdResponse = await fetch(
      `${BASE_URL}/jobs/${job.id}/out.md`,
      { headers }
    );
    return await mdResponse.text();
  }

  throw new Error(`Job failed: ${job.error}`);
}
```

---

## Webhooks

Configure webhooks to receive notifications when jobs complete.

### Webhook Configuration

Set webhook URL via environment variable:

```bash
WEBHOOK_URL=https://your-app.com/webhooks/mdwb
WEBHOOK_SECRET=your_secret_key
```

### Webhook Payload

```json
{
  "event": "job.completed",
  "job_id": "job_abc123",
  "url": "https://example.com/article",
  "state": "DONE",
  "timestamp": "2024-11-09T12:01:23Z",
  "signature": "sha256=abc123..."
}
```

### Signature Verification

```python
import hmac
import hashlib

def verify_webhook(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

---

## Best Practices

### 1. Use Cache

Enable `reuse_cache: true` to avoid redundant captures:

```json
{
  "url": "https://example.com",
  "reuse_cache": true
}
```

### 2. Handle Rate Limits

Implement exponential backoff when hitting rate limits:

```python
async def capture_with_retry(url: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await capture_url(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 60))
                await asyncio.sleep(retry_after * (2 ** attempt))
            else:
                raise
    raise Exception("Max retries exceeded")
```

### 3. Implement Timeouts

Always set client timeouts:

```python
client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))  # 5 minutes
```

### 4. Monitor Rate Limit Headers

Track your usage:

```python
remaining = int(response.headers["X-RateLimit-Remaining"])
if remaining < 10:
    # Slow down requests
    await asyncio.sleep(5)
```

---

## Support

- **Documentation**: https://github.com/Dicklesworthstone/markdown_web_browser/tree/main/docs
- **Issues**: https://github.com/Dicklesworthstone/markdown_web_browser/issues
- **API Status**: https://mdwb.example.com/health
