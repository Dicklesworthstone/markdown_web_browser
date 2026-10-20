// Markdown Web Browser - Main logic
import { renderSafeHtml, setupAuthentication } from './client.js';

// ============================================================================
// CONSTANTS & CONFIGURATION
// ============================================================================

const POLL_INTERVAL_MS = 1000; // Poll job status every second
const MAX_POLL_ATTEMPTS = 600; // Max 10 minutes (600 seconds)
const GOOGLE_SEARCH_URL = 'https://www.google.com/search?q=';
const API_KEY_STORAGE_KEY = 'mdwb_api_key';

// ============================================================================
// AUTH & SECURITY HELPERS
// ============================================================================

function getApiKey() {
    try {
        if (typeof window !== 'undefined' && window.location?.search) {
            const params = new URLSearchParams(window.location.search);
            const fromQuery = params.get('api_key');
            if (fromQuery && fromQuery.trim()) {
                setApiKey(fromQuery.trim());
                return fromQuery.trim();
            }
        }
        if (typeof localStorage !== 'undefined') {
            const stored = localStorage.getItem(API_KEY_STORAGE_KEY);
            if (stored && stored.trim()) {
                return stored.trim();
            }
        }
    } catch {
        // Ignore storage errors in restricted contexts
    }
    return '';
}

function setApiKey(apiKey) {
    const normalized = (apiKey || '').trim();
    try {
        if (typeof localStorage !== 'undefined') {
            if (normalized) {
                localStorage.setItem(API_KEY_STORAGE_KEY, normalized);
            } else {
                localStorage.removeItem(API_KEY_STORAGE_KEY);
            }
        }
        if (typeof document !== 'undefined') {
            if (normalized) {
                document.cookie = `mdwb_api_key=${encodeURIComponent(normalized)}; path=/; SameSite=Lax`;
            } else {
                document.cookie = 'mdwb_api_key=; Max-Age=0; path=/; SameSite=Lax';
            }
        }
    } catch {
        // Ignore storage/cookie errors
    }
}

function authHeaders(extra = {}) {
    const headers = { ...extra };
    const apiKey = getApiKey();
    if (apiKey) {
        headers['X-API-Key'] = apiKey;
    }
    return headers;
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function sanitizeUrl(rawUrl, baseUrl = null) {
    if (!rawUrl || typeof rawUrl !== 'string') {
        return '';
    }
    // Strip ASCII control characters and surrounding whitespace
    const cleaned = rawUrl.replace(/[\u0000-\u001F\u007F]+/g, '').trim();
    if (!cleaned) {
        return '';
    }
    if (cleaned.startsWith('#')) {
        return cleaned;
    }
    // Reject dangerous schemes explicitly
    const compactSchemeCheck = cleaned.replace(/\s+/g, '').toLowerCase();
    if (
        compactSchemeCheck.startsWith('javascript:') ||
        compactSchemeCheck.startsWith('data:') ||
        compactSchemeCheck.startsWith('vbscript:') ||
        compactSchemeCheck.startsWith('file:') ||
        compactSchemeCheck.startsWith('blob:')
    ) {
        return '';
    }
    try {
        const base =
            baseUrl && /^https?:\/\//i.test(baseUrl)
                ? baseUrl
                : typeof window !== 'undefined' && window.location?.origin
                  ? window.location.origin
                  : 'https://example.com';
        const resolved = new URL(cleaned, base);
        const protocol = resolved.protocol.toLowerCase();
        if (protocol === 'http:' || protocol === 'https:' || protocol === 'mailto:') {
            return resolved.href;
        }
        return '';
    } catch {
        return '';
    }
}

function formatInlineMarkdown(text, baseUrl = null) {
    if (!text) {
        return '';
    }
    const codeSpans = [];
    const withPlaceholders = String(text).replace(/`([^`]+)`/g, (_, code) => {
        const idx = codeSpans.length;
        codeSpans.push(`<code>${escapeHtml(code)}</code>`);
        return `\u0000CODE_${idx}\u0000`;
    });

    let escaped = escapeHtml(withPlaceholders);

    // Images: ![alt](url)
    escaped = escaped.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_, alt, rawSrc) => {
        const safeSrc = sanitizeUrl(rawSrc.replace(/&amp;/g, '&'), baseUrl);
        if (!safeSrc) {
            return alt ? `[Image: ${alt}]` : '';
        }
        return `<img src="${escapeHtml(safeSrc)}" alt="${alt}" loading="lazy">`;
    });

    // Links: [text](url)
    escaped = escaped.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_, label, rawHref) => {
        const safeHref = sanitizeUrl(rawHref.replace(/&amp;/g, '&'), baseUrl);
        if (!safeHref) {
            return label;
        }
        return `<a href="${escapeHtml(safeHref)}" rel="noopener noreferrer">${label}</a>`;
    });

    // Bold & italic
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    escaped = escaped.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    escaped = escaped.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');

    return escaped.replace(/\u0000CODE_(\d+)\u0000/g, (_, idx) => codeSpans[Number(idx)] || '');
}

function parseMarkdownSafe(markdown, baseUrl = null) {
    if (!markdown || typeof markdown !== 'string') {
        return '';
    }
    // Strip HTML provenance comments (<!-- source: ... -->) from rendered output while keeping raw text safe
    const cleaned = markdown.replace(/<!--[\s\S]*?-->/g, '');
    const lines = cleaned.replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
        const line = lines[i];

        // Fenced code blocks
        const fenceMatch = line.match(/^```([a-zA-Z0-9_-]*)\s*$/);
        if (fenceMatch) {
            const lang = fenceMatch[1] ? ` class="language-${escapeHtml(fenceMatch[1])}"` : '';
            const codeLines = [];
            i++;
            while (i < lines.length && !/^```\s*$/.test(lines[i])) {
                codeLines.push(escapeHtml(lines[i]));
                i++;
            }
            if (i < lines.length) {
                i++; // consume closing fence
            }
            out.push(`<pre><code${lang}>${codeLines.join('\n')}</code></pre>`);
            continue;
        }

        // Blank line
        if (!line.trim()) {
            i++;
            continue;
        }

        // Horizontal rule
        if (/^(?:---|\*\*\*|___)\s*$/.test(line.trim())) {
            out.push('<hr>');
            i++;
            continue;
        }

        // ATX Headings (# .. ######)
        const headingMatch = line.match(/^(#{1,6})\s+(.+?)\s*$/);
        if (headingMatch) {
            const level = headingMatch[1].length;
            out.push(`<h${level}>${formatInlineMarkdown(headingMatch[2], baseUrl)}</h${level}>`);
            i++;
            continue;
        }

        // Blockquotes
        if (/^>\s?/.test(line)) {
            const quoteLines = [];
            while (i < lines.length && /^>\s?/.test(lines[i])) {
                quoteLines.push(formatInlineMarkdown(lines[i].replace(/^>\s?/, ''), baseUrl));
                i++;
            }
            out.push(`<blockquote><p>${quoteLines.join('<br>')}</p></blockquote>`);
            continue;
        }

        // GFM Tables
        if (
            line.includes('|') &&
            i + 1 < lines.length &&
            /^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(lines[i + 1])
        ) {
            const parseCells = (rowText) =>
                rowText
                    .trim()
                    .replace(/^\||\|$/g, '')
                    .split('|')
                    .map((cell) => formatInlineMarkdown(cell.trim(), baseUrl));
            const headers = parseCells(line);
            i += 2; // skip header + separator
            const bodyRows = [];
            while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
                bodyRows.push(parseCells(lines[i]));
                i++;
            }
            const thead = `<thead><tr>${headers.map((h) => `<th>${h}</th>`).join('')}</tr></thead>`;
            const tbody = bodyRows.length
                ? `<tbody>${bodyRows
                      .map((row) => `<tr>${row.map((c) => `<td>${c}</td>`).join('')}</tr>`)
                      .join('')}</tbody>`
                : '';
            out.push(`<table>${thead}${tbody}</table>`);
            continue;
        }

        // Unordered list
        if (/^\s*[-*+]\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
                items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*[-*+]\s+/, ''), baseUrl)}</li>`);
                i++;
            }
            out.push(`<ul>${items.join('')}</ul>`);
            continue;
        }

        // Ordered list
        if (/^\s*\d+\.\s+/.test(line)) {
            const items = [];
            while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
                items.push(`<li>${formatInlineMarkdown(lines[i].replace(/^\s*\d+\.\s+/, ''), baseUrl)}</li>`);
                i++;
            }
            out.push(`<ol>${items.join('')}</ol>`);
            continue;
        }

        // Paragraph
        const paraLines = [];
        while (
            i < lines.length &&
            lines[i].trim() &&
            !/^(#{1,6}\s+|```|>\s?|(?:\s*[-*+]\s+)|(?:\s*\d+\.\s+)|(?:---|\*\*\*|___)\s*$)/.test(lines[i])
        ) {
            paraLines.push(formatInlineMarkdown(lines[i], baseUrl));
            i++;
        }
        out.push(`<p>${paraLines.join('<br>')}</p>`);
    }

    return out.join('\n');
}

function sanitizeRenderedDom(container, baseUrl = null) {
    if (!container || typeof container.querySelectorAll !== 'function') {
        return;
    }
    const blockedTags = 'script, iframe, object, embed, style, link, meta, base, form, input, button, textarea, select';
    container.querySelectorAll(blockedTags).forEach((node) => node.remove());

    container.querySelectorAll('*').forEach((el) => {
        const attrs = Array.from(el.attributes || []);
        for (const attr of attrs) {
            const name = attr.name.toLowerCase();
            if (name.startsWith('on') || name === 'style' || name === 'srcdoc') {
                el.removeAttribute(attr.name);
                continue;
            }
            if (name === 'href' || name === 'src' || name === 'xlink:href' || name === 'action') {
                const safe = sanitizeUrl(attr.value, baseUrl);
                if (!safe) {
                    el.removeAttribute(attr.name);
                } else {
                    el.setAttribute(attr.name, safe);
                }
            }
        }
        if (el.tagName === 'A') {
            el.setAttribute('rel', 'noopener noreferrer');
        }
    });
}

function highlightMarkdownSafe(markdown) {
    const lines = String(markdown ?? '').replace(/\r\n/g, '\n').split('\n');
    return lines
        .map((line) => {
            const escaped = escapeHtml(line);
            if (/^&lt;!--[\s\S]*--&gt;$/.test(escaped.trim())) {
                return `<span class="token comment">${escaped}</span>`;
            }
            if (/^#{1,6}\s+/.test(escaped)) {
                return `<span class="token title important">${escaped}</span>`;
            }
            if (/^&gt;\s?/.test(escaped)) {
                return `<span class="token blockquote">${escaped}</span>`;
            }
            if (/^```/.test(escaped)) {
                return `<span class="token code keyword">${escaped}</span>`;
            }
            return escaped
                .replace(/(`[^`]+`)/g, '<span class="token code">$1</span>')
                .replace(/(\[[^\]]+\]\([^)]+\))/g, '<span class="token url">$1</span>');
        })
        .join('\n');
}

// ============================================================================
// STATE MANAGEMENT
// ============================================================================

class BrowserState {
    constructor() {
        this.history = [];
        this.currentIndex = -1;
        this.cache = new Map(); // URL -> { markdown, jobId, timestamp }
        this.currentView = 'rendered'; // 'rendered' or 'raw'
        this.currentJobId = null;
        this.currentUrl = null;
    }

    canGoBack() {
        return this.currentIndex > 0;
    }

    canGoForward() {
        return this.currentIndex < this.history.length - 1;
    }

    goBack() {
        if (this.canGoBack()) {
            this.currentIndex--;
            return this.history[this.currentIndex];
        }
        return null;
    }

    goForward() {
        if (this.canGoForward()) {
            this.currentIndex++;
            return this.history[this.currentIndex];
        }
        return null;
    }

    pushUrl(url) {
        // Remove any forward history when navigating to new URL
        this.history = this.history.slice(0, this.currentIndex + 1);
        this.history.push(url);
        this.currentIndex = this.history.length - 1;
        this.currentUrl = url;
    }

    getCached(url) {
        const cached = this.cache.get(url);
        if (!cached) return null;

        // Optional: Implement cache expiration (e.g., 1 hour)
        const age = Date.now() - cached.timestamp;
        const MAX_CACHE_AGE = 60 * 60 * 1000; // 1 hour
        if (age > MAX_CACHE_AGE) {
            this.cache.delete(url);
            return null;
        }

        return cached;
    }

    setCached(url, markdown, jobId) {
        this.cache.set(url, {
            markdown,
            jobId,
            timestamp: Date.now()
        });
    }
}

const state = new BrowserState();
let navigationController = null;

// ============================================================================
// DOM ELEMENTS
// ============================================================================

const elements = {
    // Navigation
    backBtn: null,
    forwardBtn: null,
    refreshBtn: null,
    urlInput: null,
    goBtn: null,

    // View toggle
    renderedBtn: null,
    rawBtn: null,

    // Status
    statusBar: null,
    statusText: null,
    loadingSpinner: null,
    loadingMessage: null,
    progressContainer: null,
    progressFill: null,
    progressText: null,

    // Content
    renderedView: null,
    rawView: null,
    errorView: null,
    renderedContent: null,
    rawContent: null,
    errorTitle: null,
    errorMessage: null,
    retryBtn: null
};

// ============================================================================
// INITIALIZATION
// ============================================================================

function init() {
    // Sync any API key from query string / localStorage to cookie on startup.
    getApiKey();

    // Parse Markdown, then sanitize the final HTML at the rendering boundary.
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            breaks: true,
            gfm: true
        });
        marked.use({
            walkTokens(token) {
                if (token.type === 'html') {
                    token.type = 'text';
                    token.raw = token.text;
                }
                if ((token.type === 'link' || token.type === 'image') && token.href) {
                    const safeHref = sanitizeUrl(token.href, state.currentUrl);
                    token.href = safeHref || '#';
                }
            }
        });
    }

    // Get all DOM elements
    elements.backBtn = document.getElementById('back-btn');
    elements.forwardBtn = document.getElementById('forward-btn');
    elements.refreshBtn = document.getElementById('refresh-btn');
    elements.urlInput = document.getElementById('url-input');
    elements.goBtn = document.getElementById('go-btn');

    elements.renderedBtn = document.getElementById('rendered-btn');
    elements.rawBtn = document.getElementById('raw-btn');

    elements.statusBar = document.getElementById('status-bar');
    elements.statusText = document.getElementById('status-text');
    elements.loadingSpinner = document.getElementById('loading-spinner');
    elements.loadingMessage = document.getElementById('loading-message');
    elements.progressContainer = document.getElementById('progress-container');
    elements.progressFill = document.getElementById('progress-fill');
    elements.progressText = document.getElementById('progress-text');

    elements.renderedView = document.getElementById('rendered-view');
    elements.rawView = document.getElementById('raw-view');
    elements.errorView = document.getElementById('error-view');
    elements.renderedContent = document.getElementById('rendered-content');
    elements.rawContent = document.getElementById('raw-content');
    elements.errorTitle = document.getElementById('error-title');
    elements.errorMessage = document.getElementById('error-message');
    elements.retryBtn = document.getElementById('retry-btn');

    // Attach event listeners
    attachEventListeners();

    // Set initial state
    updateNavigationButtons();
    setStatus('Ready');
    setupAuthentication(() => {
        navigationController?.abort();
        state.cache.clear();
        state.currentJobId = null;
        elements.renderedContent.textContent = '';
        elements.rawContent.textContent = '';
        setLoading(false);
        setStatus('Access changed. Load a page to continue.');
    }).catch(error => showError('Access settings unavailable', error.message));
}

function attachEventListeners() {
    // Navigation buttons
    elements.backBtn.addEventListener('click', handleBack);
    elements.forwardBtn.addEventListener('click', handleForward);
    elements.refreshBtn.addEventListener('click', handleRefresh);
    elements.goBtn.addEventListener('click', handleGo);

    // URL input
    elements.urlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            handleGo();
        }
    });

    // View toggle
    elements.renderedBtn.addEventListener('click', () => switchView('rendered'));
    elements.rawBtn.addEventListener('click', () => switchView('raw'));

    // Retry button
    elements.retryBtn.addEventListener('click', handleRetry);

    // Intercept link clicks inside rendered Markdown so navigation stays inside the browser UI
    elements.renderedContent.addEventListener('click', (e) => {
        const anchor = e.target.closest?.('a[href]');
        if (!anchor) {
            return;
        }
        const rawHref = anchor.getAttribute('href') || '';
        if (rawHref.startsWith('#')) {
            return;
        }
        const safeHref = sanitizeUrl(rawHref, state.currentUrl);
        if (!safeHref) {
            e.preventDefault();
            return;
        }
        if (/^https?:\/\//i.test(safeHref)) {
            e.preventDefault();
            loadUrl(safeHref);
        }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboardShortcuts);
}

// ============================================================================
// NAVIGATION HANDLERS
// ============================================================================

async function handleBack() {
    const url = state.goBack();
    if (url) {
        await loadUrl(url, { skipHistory: true });
    }
}

async function handleForward() {
    const url = state.goForward();
    if (url) {
        await loadUrl(url, { skipHistory: true });
    }
}

async function handleRefresh() {
    if (state.currentUrl) {
        // Clear client cache and instruct backend to bypass server cache (reuse_cache: false)
        state.cache.delete(state.currentUrl);
        await loadUrl(state.currentUrl, { skipHistory: true, reuseCache: false });
    }
}

async function handleGo() {
    const input = elements.urlInput.value.trim();
    if (!input) {
        return;
    }

    const url = normalizeInput(input);
    await loadUrl(url);
}

async function handleRetry() {
    if (state.currentUrl) {
        await loadUrl(state.currentUrl, { skipHistory: true, reuseCache: false });
    }
}

// ============================================================================
// URL PROCESSING
// ============================================================================

function normalizeInput(input) {
    // Check if it's a URL
    if (input.match(/^https?:\/\//i)) {
        return input;
    }

    // Check if it looks like a domain
    if (input.match(/^[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,}(:[0-9]{1,5})?(\/.*)?$/i)) {
        return 'https://' + input;
    }

    // Otherwise, treat as search query
    return GOOGLE_SEARCH_URL + encodeURIComponent(input);
}

// ============================================================================
// MAIN LOAD FUNCTION
// ============================================================================

async function loadUrl(url, options = {}) {
    const { skipHistory = false, reuseCache = true } = options;
    navigationController?.abort();
    const controller = new AbortController();
    navigationController = controller;
    const signal = controller.signal;
    state.currentJobId = null;

    // Update URL input
    elements.urlInput.value = url;

    // Update history
    if (!skipHistory) {
        state.pushUrl(url);
    }
    state.currentUrl = url;

    // Update navigation buttons
    updateNavigationButtons();

    // Hide error view
    hideError();

    // Check cache first
    const cached = reuseCache ? state.getCached(url) : null;
    if (cached) {
        state.currentJobId = cached.jobId;
        setStatus(`Loaded from cache: ${url}`);
        displayMarkdown(cached.markdown);
        setLoading(false);
        return;
    }

    // Start loading
    try {
        setLoading(true, 'Submitting capture job...');
        setStatus(`Loading: ${url}`);

        // Submit capture job
        const jobId = await submitCaptureJob(url, reuseCache, signal);
        signal.throwIfAborted();
        state.currentJobId = jobId;

        setStatus(`Job ${jobId} submitted, waiting for completion...`);

        // Poll for completion
        await pollJobCompletion(jobId, signal);

        // Fetch markdown
        setLoading(true, 'Fetching markdown...');
        const markdown = await fetchMarkdown(jobId, signal);
        signal.throwIfAborted();

        // Cache the result
        state.setCached(url, markdown, jobId);

        // Display
        displayMarkdown(markdown);
        setStatus(`Successfully loaded: ${url}`);
        setLoading(false);

    } catch (error) {
        if (signal.aborted) return;
        console.error('Failed to load URL:', error);
        showError('Failed to load page', error.message);
        setLoading(false);
    }
}

// ============================================================================
// API CALLS
// ============================================================================

async function submitCaptureJob(url, reuseCache, signal) {
    const response = await fetch('/jobs', {
        method: 'POST',
        headers: authHeaders({
            'Content-Type': 'application/json'
        }),
        body: JSON.stringify({ url, reuse_cache: reuseCache }),
        signal,
    });

    if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
    }

    const data = await response.json();
    if (!data.id) {
        throw new Error('No job ID in response');
    }

    return data.id;
}

async function pollJobCompletion(jobId, signal) {
    let attempts = 0;

    while (attempts < MAX_POLL_ATTEMPTS) {
        signal?.throwIfAborted();
        const response = await fetch(`/jobs/${encodeURIComponent(jobId)}`, {
            headers: authHeaders(),
            signal,
        });

        if (!response.ok) {
            throw new Error(`Failed to fetch job status: HTTP ${response.status}`);
        }

        const job = await response.json();
        signal?.throwIfAborted();
        const jobState = job.state?.toUpperCase();

        // Update progress
        const progress = job.progress || {};
        const done = progress.done || 0;
        const total = progress.total || 0;

        if (total > 0) {
            const percentage = Math.round((done / total) * 100);
            updateProgress(percentage, `${done}/${total} tiles processed`);
        }

        if (jobState === 'DONE') {
            setLoading(true, 'Capture complete!');
            return job;
        }

        if (jobState === 'FAILED' || jobState === 'CANCELLED') {
            throw new Error(job.error || 'Job failed');
        }

        // Update status message based on current state
        if (jobState) {
            setLoading(true, `${jobState}: ${done}/${total} tiles`);
        }

        // Wait before next poll
        await sleep(POLL_INTERVAL_MS, signal);
        attempts++;
    }

    throw new Error('Job timeout - took longer than expected');
}

async function fetchMarkdown(jobId, signal) {
    const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/result.md`, {
        headers: authHeaders(),
        signal,
    });

    if (!response.ok) {
        // Handle 404 specifically - job might not have markdown yet
        if (response.status === 404) {
            throw new Error(`Markdown not available yet for job ${jobId}`);
        }
        throw new Error(`Failed to fetch markdown: HTTP ${response.status}`);
    }

    const text = await response.text();
    if (!text || text.trim().length === 0) {
        throw new Error('Markdown content is empty');
    }

    return text;
}

// ============================================================================
// DISPLAY FUNCTIONS
// ============================================================================

function displayMarkdown(markdown) {
    // Render for both views
    renderMarkdown(markdown);
    renderRawMarkdown(markdown);

    // Show the current active view
    showContentView();
}

function renderMarkdown(markdown) {
    // Prefer vendored marked + DOMPurify; fall back to the offline parser.
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
        renderSafeHtml(elements.renderedContent, marked.parse(markdown));
    } else {
        const html = parseMarkdownSafe(markdown, state.currentUrl);
        elements.renderedContent.innerHTML = html;
        sanitizeRenderedDom(elements.renderedContent, state.currentUrl);
    }
    elements.renderedContent.classList.add('markdown-body');
}

function renderRawMarkdown(markdown) {
    try {
        if (typeof Prism !== 'undefined' && Prism.languages?.markdown) {
            const highlighted = Prism.highlight(markdown, Prism.languages.markdown, 'markdown');
            renderSafeHtml(elements.rawContent, highlighted);
        } else {
            elements.rawContent.innerHTML = highlightMarkdownSafe(markdown);
        }
    } catch (error) {
        console.error('Error highlighting markdown:', error);
        elements.rawContent.textContent = markdown;
    }
}

function switchView(viewName) {
    state.currentView = viewName;

    if (viewName === 'rendered') {
        elements.renderedBtn.classList.add('active');
        elements.rawBtn.classList.remove('active');
        elements.renderedView.classList.add('active');
        elements.rawView.classList.remove('active');
    } else {
        elements.rawBtn.classList.add('active');
        elements.renderedBtn.classList.remove('active');
        elements.rawView.classList.add('active');
        elements.renderedView.classList.remove('active');
    }
}

function showContentView() {
    // Don't use inline styles - let CSS classes control display
    // Remove any inline display styles that might override CSS
    elements.renderedView.style.display = '';
    elements.rawView.style.display = '';
    elements.errorView.style.display = 'none';

    // Apply current view (this adds/removes .active class)
    switchView(state.currentView);
}

function showError(title, message) {
    elements.errorTitle.textContent = title;
    elements.errorMessage.textContent = message;
    elements.errorView.style.display = 'flex';
    elements.renderedView.style.display = 'none';
    elements.rawView.style.display = 'none';
    setStatus('Error: ' + title);
}

function hideError() {
    elements.errorView.style.display = 'none';
}

// ============================================================================
// UI STATE FUNCTIONS
// ============================================================================

function updateNavigationButtons() {
    elements.backBtn.disabled = !state.canGoBack();
    elements.forwardBtn.disabled = !state.canGoForward();
}

function setStatus(text) {
    elements.statusText.textContent = text;
}

function setLoading(isLoading, message = 'Loading...') {
    if (isLoading) {
        elements.loadingSpinner.style.display = 'flex';
        elements.loadingMessage.textContent = message;
        elements.statusText.style.display = 'none';
        elements.progressContainer.style.display = 'none';
    } else {
        elements.loadingSpinner.style.display = 'none';
        elements.statusText.style.display = 'block';
        elements.progressContainer.style.display = 'none';
    }
}

function updateProgress(percentage, text = '') {
    elements.progressContainer.style.display = 'flex';
    elements.loadingSpinner.style.display = 'none';
    elements.statusText.style.display = 'none';

    elements.progressFill.style.width = percentage + '%';
    elements.progressText.textContent = text || percentage + '%';
}

// ============================================================================
// KEYBOARD SHORTCUTS
// ============================================================================

function handleKeyboardShortcuts(e) {
    // Alt + Left Arrow = Back
    if (e.altKey && e.key === 'ArrowLeft') {
        e.preventDefault();
        handleBack();
    }

    // Alt + Right Arrow = Forward
    if (e.altKey && e.key === 'ArrowRight') {
        e.preventDefault();
        handleForward();
    }

    // Ctrl/Cmd + R = Refresh
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
        e.preventDefault();
        handleRefresh();
    }

    // Ctrl/Cmd + U = Toggle Raw View
    if ((e.ctrlKey || e.metaKey) && e.key === 'u') {
        e.preventDefault();
        switchView(state.currentView === 'rendered' ? 'raw' : 'rendered');
    }

    // Ctrl/Cmd + L = Focus URL bar
    if ((e.ctrlKey || e.metaKey) && e.key === 'l') {
        e.preventDefault();
        elements.urlInput.focus();
        elements.urlInput.select();
    }
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
        signal.throwIfAborted();
        const abort = () => {
            clearTimeout(timer);
            reject(signal.reason);
        };
        const timer = setTimeout(() => {
            signal.removeEventListener('abort', abort);
            resolve();
        }, ms);
        signal.addEventListener('abort', abort, { once: true });
    });
}

// ============================================================================
// STARTUP
// ============================================================================

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

// Export for debugging and testing
window.mdwbBrowser = {
    state,
    loadUrl,
    elements,
    sanitizeUrl,
    parseMarkdownSafe,
    sanitizeRenderedDom,
    setApiKey,
    getApiKey
};

