// Markdown Web Browser - Main logic
import { renderSafeHtml, setupAuthentication } from './client.js';

// ============================================================================
// CONSTANTS & CONFIGURATION
// ============================================================================

const POLL_INTERVAL_MS = 1000; // Poll job status every second
const MAX_POLL_ATTEMPTS = 600; // Max 10 minutes (600 seconds)
const GOOGLE_SEARCH_URL = 'https://www.google.com/search?q=';

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
    // Parse Markdown, then sanitize the final HTML at the rendering boundary.
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            breaks: true,
            gfm: true
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
        // Clear cache for this URL to force refresh
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
        headers: {
            'Content-Type': 'application/json'
        },
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
        signal.throwIfAborted();
        const response = await fetch(`/jobs/${jobId}`, { signal });

        if (!response.ok) {
            throw new Error(`Failed to fetch job status: HTTP ${response.status}`);
        }

        const job = await response.json();
        signal.throwIfAborted();
        const state = job.state?.toUpperCase();

        // Update progress
        const progress = job.progress || {};
        const done = progress.done || 0;
        const total = progress.total || 0;

        if (total > 0) {
            const percentage = Math.round((done / total) * 100);
            updateProgress(percentage, `${done}/${total} tiles processed`);
        }

        if (state === 'DONE') {
            setLoading(true, 'Capture complete!');
            return job;
        }

        if (state === 'FAILED' || state === 'CANCELLED') {
            throw new Error(job.error || 'Job failed');
        }

        // Update status message based on current state
        if (state) {
            setLoading(true, `${state}: ${done}/${total} tiles`);
        }

        // Wait before next poll
        await sleep(POLL_INTERVAL_MS, signal);
        attempts++;
    }

    throw new Error('Job timeout - took longer than expected');
}

async function fetchMarkdown(jobId, signal) {
    const response = await fetch(`/jobs/${jobId}/result.md`, { signal });

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
    // Use marked.js to render markdown to HTML
    const html = marked.parse(markdown);
    renderSafeHtml(elements.renderedContent, html);
    elements.renderedContent.classList.add('markdown-body');
}

function renderRawMarkdown(markdown) {
    // Apply syntax highlighting using Prism.highlight()
    try {
        if (typeof Prism !== 'undefined' && Prism.languages.markdown) {
            const highlighted = Prism.highlight(markdown, Prism.languages.markdown, 'markdown');
            renderSafeHtml(elements.rawContent, highlighted);
        } else {
            // Fallback if Prism isn't loaded
            elements.rawContent.textContent = markdown;
        }
    } catch (error) {
        console.error('Error highlighting markdown:', error);
        // Fallback to plain text if highlighting fails
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
        // Syntax highlighting is already applied in renderRawMarkdown() using Prism.highlight()
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

// Export for debugging (optional)
window.mdwbBrowser = {
    state,
    loadUrl,
    elements
};
