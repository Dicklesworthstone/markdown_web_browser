# Real Data Testing Requirements

## Changes Made to Remove All Mocks

### 1. Removed Mock Infrastructure
- ✅ Deleted `scripts/mock_ocr_server.py` - No more mock OCR server
- ✅ Deleted `app/ocr_fallback.py` - No more fallback to mock OCR
- ✅ Removed all `MockTransport` usage from tests
- ✅ Replaced `test_ocr_client.py` with real OCR API test suite

### 2. Created Real Data Test Infrastructure

#### New Real OCR Test Suite (`tests/test_ocr_client.py`)
- Uses real PNG images generated with PIL
- Makes actual OCR API calls to the olmOCR service
- Tests with real website screenshots using Playwright
- No fake data or mock responses

#### Updated E2E Test Suite (`scripts/test_e2e_comprehensive.py`)
- Uses only real websites (example.com, wikipedia.org, github.com, etc.)
- No fake URLs or mock endpoints
- Requires real OCR API for processing screenshots

## Requirements for Running Tests

### Essential Requirements

1. **OCR API Key**
   ```bash
   export OCR_API_KEY="your-real-ocr-api-key"
   ```
   - Must be a valid key for the olmOCR service
   - Without this, all OCR-dependent tests will fail

2. **OCR API Endpoint**
   ```bash
   export OCR_SERVER_URL="https://api.example.com/v1/ocr"
   ```
   - Must be accessible from the test environment
   - Default expects the production olmOCR endpoint

3. **System Dependencies**
   ```bash
   # Required for image processing
   sudo apt-get install libvips-dev

   # Required for browser automation
   playwright install chromium
   playwright install-deps chromium
   ```

4. **Network Access**
   - Tests require internet access to reach real websites
   - Firewall must allow HTTPS connections to test sites

### Test Execution

#### Run Real OCR Tests
```bash
# Only runs if OCR_API_KEY is set
pytest tests/test_ocr_client.py -v

# Skip if no API key available
pytest tests/test_ocr_client.py -v -k "not real_ocr"
```

#### Run E2E Tests with Real Data
```bash
# Start server (no mock configuration)
uv run python scripts/run_server.py

# Run comprehensive tests
uv run python scripts/test_e2e_comprehensive.py \
  --api-url http://localhost:8000 \
  --verbose

# Run specific test categories
uv run python scripts/test_e2e_comprehensive.py \
  --category "Smoke Tests" \
  --priority 1
```

## What This Means

### Advantages of Real-Only Testing
1. **No False Positives** - Tests fail if real infrastructure isn't working
2. **Production Parity** - Tests use exact same APIs as production
3. **Real Performance Data** - Metrics reflect actual OCR processing times
4. **True Integration Testing** - Validates entire system end-to-end

### Challenges
1. **Cost** - Real OCR API calls cost money per request
2. **Speed** - Real API calls are slower than mocks
3. **Reliability** - Tests depend on external service availability
4. **Setup Complexity** - Requires API keys and network access

## Test Categories and Real Data Usage

| Test Type | Real Websites | Real OCR | Real Images |
|-----------|--------------|----------|-------------|
| Unit Tests (CLI) | ✅ | Requires API Key | N/A |
| Integration Tests | ✅ | Requires API Key | ✅ |
| E2E Tests | ✅ | Requires API Key | ✅ |
| Performance Tests | ✅ | Requires API Key | ✅ |

## Websites Used in Tests

All tests now use real, publicly accessible websites:
- https://example.com - Simple HTML page
- https://en.wikipedia.org/wiki/Artificial_intelligence - Complex content
- https://github.com/anthropics/anthropic-sdk-python - Dynamic SPA
- https://www.bbc.com/news - Media-rich content
- https://news.ycombinator.com - Text-heavy content
- https://unsplash.com - Image-heavy content

## Running Tests in CI/CD

For CI/CD pipelines, you must:

1. Set OCR_API_KEY as a secret environment variable
2. Ensure the CI runner has internet access
3. Install all system dependencies in the CI image
4. Consider rate limits and API quotas

Example GitHub Actions:
```yaml
env:
  OCR_API_KEY: ${{ secrets.OCR_API_KEY }}

steps:
  - name: Install dependencies
    run: |
      sudo apt-get update
      sudo apt-get install -y libvips-dev
      playwright install chromium
      playwright install-deps chromium

  - name: Run real tests
    run: |
      uv run pytest tests/test_ocr_client.py
      uv run python scripts/test_e2e_comprehensive.py
```

## Migration from Mock-Based Tests

If you previously used mock-based tests, here's what changed:

| Before (With Mocks) | After (Real Data Only) |
|---------------------|------------------------|
| Fast test execution | Slower but realistic |
| No API costs | Requires API budget |
| Works offline | Requires internet |
| Predictable responses | Variable real responses |
| Simple setup | Requires API keys |

## Conclusion

All tests now use 100% real data, real websites, real images, and real OCR API calls. There are NO mocks, NO fake data, and NO simulated responses anywhere in the test suite. This ensures complete production parity but requires proper infrastructure setup including API keys and network access.