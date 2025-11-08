# Markdown Web Browser Gallery

This gallery showcases example captures demonstrating the Markdown Web Browser's capabilities across various types of content and scenarios.

## Quick Examples

### 1. Technical Documentation
**URL**: `https://docs.python.org/3/tutorial/index.html`
- **Challenge**: Multi-column layout, code blocks, navigation sidebars
- **Features**: Preserves code formatting, maintains heading hierarchy, extracts navigation links
- **Capture Time**: ~15s
- **Output**: Clean Markdown with properly formatted code blocks and link appendix

### 2. News Article with Images
**URL**: `https://www.nytimes.com/` (article pages)
- **Challenge**: Lazy-loaded images, dynamic ads, paywall overlays
- **Features**: Blocklist removes overlays, captures alt text for images, preserves article structure
- **Capture Time**: ~20s
- **Warnings**: `canvas-heavy` (for interactive graphics)

### 3. Data Dashboard
**URL**: GitHub repository insights pages
- **Challenge**: Canvas charts, SVG graphics, dynamic data loading
- **Features**: Captures data tables, extracts chart labels where possible
- **Capture Time**: ~25s
- **Warnings**: `canvas-heavy`, possible `scroll-shrink` on infinite scroll

### 4. E-commerce Product Page
**URL**: Amazon/eBay product listings
- **Challenge**: Image carousels, reviews with pagination, recommendation widgets
- **Features**: Captures product details, review summaries, extracts structured data
- **Capture Time**: ~30s
- **Blocklist hits**: Cookie banners, chat widgets

### 5. Academic Paper (PDF alternative)
**URL**: ArXiv HTML papers
- **Challenge**: Mathematical formulas (MathJax), citations, multi-column layout
- **Features**: Preserves formula representations, maintains citation links
- **Capture Time**: ~18s

## CLI Command Examples

### Basic capture with watching
```bash
mdwb fetch https://example.com/article --watch
```

### Capture with webhook notifications
```bash
mdwb fetch https://example.com/dashboard \
  --webhook-url https://your.webhook/endpoint \
  --webhook-event DONE \
  --watch
```

### Replay a previous capture
```bash
# First, get the manifest from a previous job
mdwb jobs artifacts manifest job-abc123 --out manifest.json

# Then replay it
mdwb jobs replay manifest manifest.json
```

### Extract links from a capture
```bash
mdwb jobs artifacts links job-abc123 --pretty
```

### Download the tar bundle for offline debugging
```bash
mdwb jobs bundle job-abc123 --out bundles/job-abc123.tar.zst
```

## Sample Output Structure

### Markdown Output (`out.md`)
```markdown
# Main Article Title
<!-- source: tile_0000 @ 0,0 1280x2000 -->

Article introduction paragraph...

## Section 1
<!-- source: tile_0001 @ 0,2000 1280x2000 -->

Content continues...

---

## Links Appendix
<!-- Generated from DOM snapshot + OCR extraction -->

### Navigation
- [Home](https://example.com)
- [About](https://example.com/about)

### External
- [Reference 1](https://external.com/ref1)
```

### Manifest Structure (`manifest.json`)
```json
{
  "job_id": "abc123def456",
  "url": "https://example.com/article",
  "cft_version": "chrome-130.0.6723.69",
  "cft_label": "Stable-1",
  "tiles_total": 8,
  "capture_ms": 15234,
  "ocr_ms": 8965,
  "stitch_ms": 342,
  "warnings": [
    {
      "code": "canvas-heavy",
      "message": "High canvas count may hide chart labels",
      "count": 3,
      "threshold": 2
    }
  ],
  "blocklist_hits": {
    "#cookie-banner": 1,
    ".chat-widget": 1
  },
  "sweep_stats": {
    "sweep_count": 2,
    "total_scroll_height": 12000,
    "shrink_events": 0,
    "retry_attempts": 0,
    "overlap_pairs": 7,
    "overlap_match_ratio": 0.92
  }
}
```

## Common Patterns & Solutions

### Infinite Scroll Pages
**Problem**: Content loads dynamically as you scroll
**Solution**: Set `MAX_VIEWPORT_SWEEPS=50` for reasonable depth, monitor `scroll-shrink` warnings
```bash
MAX_VIEWPORT_SWEEPS=50 mdwb fetch https://infinite-scroll-site.com --watch
```

### Canvas-Heavy Visualizations
**Problem**: Charts and graphics rendered in canvas elements
**Solution**: OCR extracts visible text, manifest includes canvas-heavy warning
```bash
# Check warnings after capture
mdwb jobs artifacts manifest job-id | jq '.warnings'
```

### Authenticated Content
**Problem**: Content behind login
**Solution**: Use browser profiles (coming in M2)
```bash
# Future: mdwb fetch https://app.example.com --profile saved-session
```

### Multi-language Content
**Problem**: Mixed RTL/LTR text, non-Latin scripts
**Solution**: olmOCR handles Unicode well, preserves directionality markers
```bash
mdwb fetch https://multilingual-site.com --watch
```

## Performance Benchmarks

| Content Type | Avg Capture Time | Avg Total Time | Tiles | Size |
|--------------|------------------|----------------|--------|------|
| Simple article | 8s | 15s | 3-5 | 2MB |
| Long-form doc | 15s | 30s | 10-15 | 8MB |
| Dashboard | 20s | 45s | 8-12 | 6MB |
| Image gallery | 25s | 40s | 15-20 | 12MB |
| Infinite scroll | 30s+ | 60s+ | 20-50 | 20MB+ |

## Troubleshooting Common Issues

### "No content captured"
- Check if the site requires authentication
- Verify JavaScript is enabled (default: yes)
- Look for `validation_failures` in the manifest

### "Duplicate content at seams"
- Check `overlap_match_ratio` in manifest (should be > 0.65)
- Increase `TILE_OVERLAP_PX` if needed
- Review `validation_failures` for seam issues

### "Missing interactive elements"
- Check for `canvas-heavy` warnings
- Canvas/WebGL content appears as images in Markdown
- Consider screenshot-only mode for heavy graphics

### "Slow capture times"
- Reduce `SCROLL_SETTLE_MS` for faster scrolling
- Lower `MAX_VIEWPORT_SWEEPS` to limit depth
- Check network latency to the target site

## Advanced Usage

### Extracting Structured Data
```bash
# Get links with DOM vs OCR comparison
mdwb dom links --job-id job-abc123 --json | \
  jq '[.[] | select(.delta == "ocr-only")]'
```

### Monitoring Capture Quality
```bash
# Check sweep stability
mdwb warnings tail --count 50 --json | \
  jq 'select(.overlap_match_ratio < 0.7)'
```

### Batch Processing
```bash
# Process multiple URLs
for url in $(cat urls.txt); do
  mdwb fetch "$url" --webhook-url https://notify.me
  sleep 2
done
```

## Integration Examples

### GitHub Action
```yaml
- name: Capture documentation
  run: |
    mdwb fetch ${{ github.event.inputs.url }} \
      --watch \
      --webhook-url ${{ secrets.WEBHOOK_URL }}
```

### Python Script
```python
import subprocess
import json

result = subprocess.run(
    ["mdwb", "fetch", url, "--watch"],
    capture_output=True,
    text=True
)
# Process result.stdout
```

### Monitoring Dashboard
Connect Prometheus to `/metrics` or port 9000:
- `mdwb_capture_duration_seconds` - Capture timing histogram
- `mdwb_ocr_duration_seconds` - OCR processing time
- `mdwb_job_completions_total` - Success/failure rates
- `mdwb_capture_warnings_total` - Warning frequency by type

## Sample Captures

We maintain a test set of captures in `benchmarks/samples/`:
- `news_article.md` - Typical news article with images
- `technical_doc.md` - API documentation with code blocks
- `dashboard.md` - Analytics dashboard with charts
- `product_page.md` - E-commerce product listing
- `academic_paper.md` - Research paper with citations

Run the gallery test suite:
```bash
# Replay all sample manifests
for manifest in benchmarks/samples/*.json; do
  mdwb jobs replay manifest "$manifest" --json
done
```

## Contributing Examples

To add your own examples to the gallery:

1. Capture an interesting URL
2. Save the manifest: `mdwb jobs artifacts manifest JOB_ID --out my-example.json`
3. Download the tar bundle so reviewers can reproduce the capture: `mdwb jobs bundle JOB_ID --out bundles/JOB_ID.tar.zst`
4. Document any special handling needed
5. Submit a PR adding to this gallery

Please include:
- URL (or type if sensitive)
- Challenges/interesting aspects
- Capture metrics from manifest
- Any warnings or issues encountered
- Sample output snippets

## See Also

- [Configuration Guide](config.md) - Tuning capture parameters
- [Operations Guide](ops.md) - Production deployment
- [Architecture](architecture.md) - Technical deep dive
- [OCR Integration](olmocr_cli_integration.md) - OCR setup and tuning
