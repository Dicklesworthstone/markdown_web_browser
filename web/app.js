const MAX_EVENT_ROWS = 50;
const EVENTS_RETRY_INTERVAL_MS = 2000;
const EMBEDDING_DIM = 1536;
const WARNING_LABELS = {
  'canvas-heavy': 'Canvas Heavy',
  'video-heavy': 'Video Heavy',
  'sticky-chrome': 'Sticky Overlay',
};

function setupTabs() {
  const tabButtons = Array.from(document.querySelectorAll('[data-tab-target]'));
  const panels = new Map(
    Array.from(document.querySelectorAll('[data-tab-panel]')).map((panel) => [
      panel.dataset.tabPanel,
      panel,
    ]),
  );

  if (!tabButtons.length || !panels.size) {
    return;
  }

  const activate = (id) => {
    tabButtons.forEach((btn) => {
      const isActive = btn.dataset.tabTarget === id;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', String(isActive));
    });
    panels.forEach((panel, key) => {
      panel.hidden = key !== id;
    });
  };

  tabButtons.forEach((button) => {
    button.addEventListener('click', () => activate(button.dataset.tabTarget));
  });

  activate(tabButtons[0].dataset.tabTarget);
}

function initSseBridge() {
  const root = document.querySelector('[data-stream-root]');
  const statusEl = document.getElementById('job-sse-status');
  if (!root || !statusEl) {
    return null;
  }
  const embeddingsPanel = initEmbeddingsPanel(root);
  const eventsPanel = initEventsPanel(root);

  const fieldMap = new Map();
  root.querySelectorAll('[data-sse-field]').forEach((el) => {
    fieldMap.set(el.dataset.sseField, el);
  });
  const warningListEl = root.querySelector('[data-warning-list]');
  const blocklistHitsEl = root.querySelector('[data-blocklist-hits]');
  const sweepStatsEl = root.querySelector('[data-sweep-stats]');
  const validationListEl = root.querySelector('[data-validation-list]');
  const sweepSummaryEl = root.querySelector('[data-sweep-summary]');
  const validationSummaryEl = root.querySelector('[data-validation-summary]');

  const setStatus = (value, variant = 'info') => {
    statusEl.textContent = value;
    statusEl.dataset.variant = variant;
  };

  const updateField = (field, payload) => {
    const el = fieldMap.get(field);
    if (!el) {
      return;
    }
    switch (field) {
      case 'manifest':
        renderManifest(el, payload, {
          warningListEl,
          blocklistHitsEl,
          sweepStatsEl,
          validationListEl,
          sweepSummaryEl,
          validationSummaryEl,
        });
        break;
      case 'raw':
        el.textContent = payload;
        break;
      case 'links':
        renderLinks(el, payload);
        break;
      case 'artifacts':
        renderArtifacts(el, payload);
        break;
      default:
        el.innerHTML = payload;
    }
  };

  const fetchTemplateJson = async (template, jobId) => {
    const target = buildTemplateUrl(template, jobId);
    const response = await fetch(target);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  };

  const refreshLinks = async (jobId) => {
    if (!root) {
      return;
    }
    const template = root.dataset.linksTemplate || '/jobs/{job_id}/links.json';
    try {
      const data = await fetchTemplateJson(template, jobId);
      updateField('links', JSON.stringify(data));
    } catch (error) {
      console.error('Failed to refresh links', error);
    }
  };

  const refreshManifest = async (jobId) => {
    if (!root) {
      return;
    }
    const template = root.dataset.manifestTemplate || '/jobs/{job_id}/manifest.json';
    try {
      const data = await fetchTemplateJson(template, jobId);
      updateField('manifest', JSON.stringify(data));
    } catch (error) {
      if (!(error?.message || '').includes('404')) {
        console.error('Failed to refresh manifest', error);
      }
    }
  };

  let source = null;
  let currentJobId = root.dataset.jobId || 'demo';

  const connect = (jobId) => {
    if (source) {
      source.close();
    }
    const template = root.dataset.streamTemplate || '/jobs/{job_id}/stream';
    currentJobId = jobId || 'demo';
    const url = buildTemplateUrl(template, currentJobId);
    root.dataset.jobId = currentJobId;
    const jobField = document.getElementById('job-id');
    if (jobField) {
      jobField.value = currentJobId;
    }
    source = new EventSource(url);
    setStatus('Connecting…', 'pending');
    embeddingsPanel?.setJobId(currentJobId);
    eventsPanel?.connect(currentJobId);
    refreshManifest(currentJobId);
    refreshLinks(currentJobId);
    source.addEventListener('open', () => setStatus(`Connected (${currentJobId})`, 'success'));
    source.addEventListener('error', () => setStatus('Disconnected — retrying…', 'warning'));
    source.addEventListener('state', (event) => {
      updateField('state', event.data);
      const normalized = (event.data || '').trim().toUpperCase();
      if (normalized === 'DONE' || normalized === 'FAILED') {
        refreshManifest(currentJobId);
        refreshLinks(currentJobId);
      }
    });
    source.addEventListener('progress', (event) => updateField('progress', event.data));
    source.addEventListener('runtime', (event) => updateField('runtime', event.data));
    source.addEventListener('manifest', (event) => updateField('manifest', event.data));
    source.addEventListener('rendered', (event) => updateField('rendered', event.data));
    source.addEventListener('raw', (event) => updateField('raw', event.data));
    source.addEventListener('links', (event) => updateField('links', event.data));
    source.addEventListener('artifacts', (event) => updateField('artifacts', event.data));
    source.addEventListener('warnings', (event) => renderWarnings(warningListEl, event.data));
  };

  const defaultJob = root.dataset.jobId || 'demo';
  connect(defaultJob);

  window.addEventListener('beforeunload', () => {
    if (source) {
      source.close();
    }
    eventsPanel?.stop?.();
  });

  return { connect, refreshLinks };
}

function renderManifest(
  element,
  payload,
  {
    warningListEl,
    blocklistHitsEl,
    sweepStatsEl,
    validationListEl,
    sweepSummaryEl,
    validationSummaryEl,
  },
) {
  if (!element) {
    return;
  }
  let formatted = '';
  let parsedPayload = null;
  if (typeof payload === 'string') {
    formatted = payload;
    try {
      parsedPayload = JSON.parse(payload);
      formatted = JSON.stringify(parsedPayload, null, 2);
    } catch {
      // keep raw payload
    }
  } else if (payload) {
    parsedPayload = payload;
    formatted = JSON.stringify(payload, null, 2);
  } else {
    formatted = 'Manifest not available yet.';
  }
  if (parsedPayload?.warnings) {
    renderWarnings(warningListEl, parsedPayload.warnings);
  }
  if (parsedPayload?.blocklist_hits) {
    renderBlocklistHits(blocklistHitsEl, parsedPayload.blocklist_hits);
  }
  renderSweepStats(sweepStatsEl, parsedPayload);
  renderValidationFailures(validationListEl, parsedPayload?.validation_failures);
  updateSweepSummary(sweepSummaryEl, parsedPayload);
  updateValidationSummary(validationSummaryEl, parsedPayload?.validation_failures);
  element.textContent = formatted;
}

function renderLinks(container, raw) {
  if (!container) {
    return;
  }
  let rows = raw;
  if (typeof raw === 'string') {
    try {
      rows = JSON.parse(raw);
    } catch {
      rows = null;
    }
  }

  if (!Array.isArray(rows)) {
    container.innerHTML = `<p class="placeholder">Invalid links payload</p>`;
    return;
  }

  if (!rows.length) {
    container.innerHTML = `<p class="placeholder">No links yet.</p>`;
    return;
  }

  const header = ['text', 'href', 'source', 'delta'];
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  header.forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label.toUpperCase();
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  rows.forEach((row) => {
    const tr = document.createElement('tr');
    header.forEach((key) => {
      const td = document.createElement('td');
      td.textContent = row[key] ?? '—';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.innerHTML = '';
  container.appendChild(table);
}

function renderArtifacts(container, raw) {
  if (!container) {
    return;
  }
  let rows = raw;
  if (typeof raw === 'string') {
    try {
      rows = JSON.parse(raw);
    } catch {
      rows = null;
    }
  }

  if (!Array.isArray(rows)) {
    container.innerHTML = `<li class="placeholder">Invalid artifact payload</li>`;
    return;
  }

  if (!rows.length) {
    container.innerHTML = `<li class="placeholder">No artifacts yet.</li>`;
    return;
  }

  container.innerHTML = '';
  rows.forEach((artifact) => {
    const li = document.createElement('li');
    const left = document.createElement('div');
    const idEl = document.createElement('strong');
    idEl.textContent = artifact.id ?? 'tile';
    const offsetEl = document.createElement('small');
    offsetEl.textContent = artifact.offset ?? '';
    left.append(idEl);
    left.append(document.createElement('br'));
    left.append(offsetEl);
    const right = document.createElement('div');
    right.textContent = artifact.sha ?? '';
    li.append(left, right);
    container.appendChild(li);
  });
}

function renderWarnings(container, payload) {
  if (!container) {
    return;
  }
  let warnings = payload;
  if (typeof payload === 'string') {
    try {
      warnings = JSON.parse(payload);
    } catch {
      warnings = null;
    }
  }
  container.innerHTML = '';
  if (!Array.isArray(warnings) || warnings.length === 0) {
    const span = document.createElement('span');
    span.className = 'warning-empty';
    span.textContent = 'None detected.';
    container.appendChild(span);
    return;
  }
  warnings.forEach((warning) => {
    const pill = document.createElement('div');
    pill.className = 'warning-pill';
    const code = document.createElement('span');
    code.className = 'warning-pill__code';
    code.textContent = WARNING_LABELS[warning.code] || warning.code;
    const meta = document.createElement('span');
    meta.className = 'warning-pill__meta';
    const count = warning.count ?? '?';
    const threshold = warning.threshold ?? '?';
    meta.textContent = `${count} hits (>= ${threshold})`;
    const message = document.createElement('span');
    message.textContent = warning.message || '';
    pill.append(code, meta, message);
    container.appendChild(pill);
  });
}

function renderBlocklistHits(container, payload) {
  if (!container) {
    return;
  }
  let hits = payload;
  if (typeof payload === 'string') {
    try {
      hits = JSON.parse(payload);
    } catch {
      hits = null;
    }
  }
  container.innerHTML = '';
  if (!hits || !Object.keys(hits).length) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'No selectors matched during this run.';
    container.appendChild(p);
    return;
  }
  Object.entries(hits).forEach(([selector, count]) => {
    const row = document.createElement('div');
    row.className = 'blocklist-entry';
    const left = document.createElement('span');
    left.className = 'blocklist-entry__selector';
    left.textContent = selector;
    const right = document.createElement('strong');
    right.textContent = count.toString();
    row.append(left, right);
    container.appendChild(row);
  });
}

function renderSweepStats(container, manifest) {
  if (!container) {
    return;
  }
  container.innerHTML = '';
  if (!manifest) {
    container.innerHTML = `<p class="placeholder">No sweep data yet.</p>`;
    return;
  }
  const stats = manifest.sweep_stats || {};
  const ratio =
    manifest.overlap_match_ratio ?? stats.overlap_match_ratio ?? null;
  const entries = [
    ['Sweeps', stats.sweep_count],
    ['Shrink events', stats.shrink_events],
    ['Retries', stats.retry_attempts],
    ['Overlap pairs', stats.overlap_pairs],
  ];
  if (ratio !== null && ratio !== undefined) {
    entries.push(['Overlap ratio', Number(ratio).toFixed(2)]);
  }
  const hasData = entries.some(([, value]) => value !== undefined && value !== null);
  if (!hasData) {
    container.innerHTML = `<p class="placeholder">No sweep data yet.</p>`;
    return;
  }
  entries.forEach(([label, value]) => {
    const row = document.createElement('div');
    row.className = 'sweep-entry';
    const left = document.createElement('span');
    left.textContent = label;
    const right = document.createElement('strong');
    right.textContent =
      value === undefined || value === null ? '—' : value.toString();
    row.append(left, right);
    container.appendChild(row);
  });
}

function renderValidationFailures(container, payload) {
  if (!container) {
    return;
  }
  container.innerHTML = '';
  if (!Array.isArray(payload) || !payload.length) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'No validation issues detected.';
    container.appendChild(p);
    return;
  }
  const list = document.createElement('ul');
  list.className = 'validation-list';
  payload.forEach((entry) => {
    const item = document.createElement('li');
    item.textContent = entry;
    list.appendChild(item);
  });
  container.appendChild(list);
}

function updateSweepSummary(element, manifest) {
  if (!element) {
    return;
  }
  if (!manifest) {
    element.textContent = 'No sweep data yet.';
    return;
  }
  const stats = manifest.sweep_stats || {};
  const ratio =
    manifest.overlap_match_ratio ?? stats.overlap_match_ratio ?? null;
  const shrink = stats.shrink_events ?? 0;
  const retries = stats.retry_attempts ?? 0;
  if (
    shrink === undefined &&
    retries === undefined &&
    (ratio === null || ratio === undefined)
  ) {
    element.textContent = 'No sweep data yet.';
    return;
  }
  const parts = [];
  if (ratio !== null && ratio !== undefined) {
    parts.push(`ratio ${Number(ratio).toFixed(2)}`);
  }
  if (shrink) {
    parts.push(`shrink ${shrink}`);
  }
  if (retries) {
    parts.push(`retries ${retries}`);
  }
  element.textContent = parts.length ? parts.join(' · ') : 'Sweep stable';
}

function updateValidationSummary(element, payload) {
  if (!element) {
    return;
  }
  if (!Array.isArray(payload) || payload.length === 0) {
    element.textContent = 'No validation issues.';
    return;
  }
  if (payload.length === 1) {
    element.textContent = payload[0];
    return;
  }
  element.textContent = `${payload.length} validation issues`;
}

function initEmbeddingsPanel(streamRoot) {
  const panel = document.querySelector('[data-embeddings-panel]');
  if (!panel) {
    return null;
  }
  const vectorInput = panel.querySelector('[data-embeddings-vector]');
  const topKInput = panel.querySelector('[data-embeddings-topk]');
  const runButton = panel.querySelector('[data-embeddings-run]');
  const demoButton = panel.querySelector('[data-embeddings-demo]');
  const statusEl = panel.querySelector('[data-embeddings-status]');
  const resultsEl = panel.querySelector('[data-embeddings-results]');
  let currentJobId = streamRoot?.dataset.jobId || 'demo';

  const setStatus = (text) => {
    if (statusEl) {
      statusEl.textContent = text;
    }
  };

  const renderResults = (matches, total) => {
    if (!resultsEl) {
      return;
    }
    resultsEl.innerHTML = '';
    if (!matches?.length) {
      const p = document.createElement('p');
      p.className = 'placeholder';
      p.textContent = total ? 'No matches for this vector.' : 'No embeddings available for this job yet.';
      resultsEl.appendChild(p);
      return;
    }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['section', 'tiles', 'similarity', 'distance'].forEach((title) => {
      const th = document.createElement('th');
      th.textContent = title.toUpperCase();
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    matches.forEach((match) => {
      const row = document.createElement('tr');
      const section = document.createElement('td');
      section.textContent = match.section_id;
      const tiles = document.createElement('td');
      const start = match.tile_start ?? '—';
      const end = match.tile_end ?? '—';
      tiles.textContent = `${start} → ${end}`;
      const similarity = document.createElement('td');
      similarity.textContent = match.similarity.toFixed(4);
      const distance = document.createElement('td');
      distance.textContent = match.distance.toFixed(4);
      row.append(section, tiles, similarity, distance);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    resultsEl.appendChild(table);
  };

  const parseVector = () => {
    if (!vectorInput) {
      throw new Error('Vector input not available');
    }
    const raw = vectorInput.value.trim();
    if (!raw) {
      throw new Error('Provide a JSON array with 1,536 numbers.');
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new Error('Vector must be valid JSON.');
    }
    if (!Array.isArray(parsed) || parsed.length !== EMBEDDING_DIM) {
      throw new Error(`Vector must contain exactly ${EMBEDDING_DIM} numbers.`);
    }
    return parsed;
  };

  const runSearch = async () => {
    try {
      setStatus('Searching…');
      const vector = parseVector();
      const topK = Math.min(
        50,
        Math.max(1, parseInt(topKInput?.value || '5', 10) || 5),
      );
      const jobId = currentJobId || 'demo';
      const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/embeddings/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ vector, top_k: topK }),
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const data = await response.json();
      renderResults(data.matches, data.total_sections);
      setStatus(`Found ${data.matches.length} of ${data.total_sections} sections for job ${jobId}.`);
    } catch (error) {
      console.error('Embeddings search failed', error);
      setStatus(error.message || 'Search failed');
    }
  };

  const buildDemoVector = () => {
    const vec = Array(EMBEDDING_DIM).fill(0);
    vec[0] = 1;
    vec[1] = 0.5;
    return vec;
  };

  runButton?.addEventListener('click', runSearch);
  demoButton?.addEventListener('click', () => {
    if (!vectorInput) {
      return;
    }
    vectorInput.value = JSON.stringify(buildDemoVector());
    setStatus('Demo vector loaded. Adjust as needed, then click Search.');
  });

  const setJobId = (jobId) => {
    currentJobId = jobId || 'demo';
    setStatus(`Ready to query embeddings for job ${currentJobId}.`);
  };

  setJobId(currentJobId);
  return { setJobId };
}

function initEventsPanel(root) {
  const logEl = root.querySelector('[data-events-log]');
  if (!logEl) {
    return null;
  }
  const statusEl = root.querySelector('[data-events-status]');
  const pauseButton = root.querySelector('[data-events-pause]');
  const resumeButton = root.querySelector('[data-events-resume]');
  let abortController = null;
  let streamTask = null;
  let activeJobId = root.dataset.jobId || 'demo';
  let cursor = null;

  const setStatus = (text, variant = 'info') => {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text;
    statusEl.dataset.variant = variant;
  };

  const resetLog = () => {
    logEl.innerHTML = '';
  };

  const appendEntry = (entry) => {
    const item = document.createElement('li');
    const meta = document.createElement('div');
    meta.className = 'event-feed__meta';
    meta.textContent = `${formatEventTimestamp(entry.timestamp)} · #${entry.sequence ?? '—'}`;
    const summary = document.createElement('div');
    summary.className = 'event-feed__summary';
    const snapshot = entry.snapshot || {};
    let details = snapshot.state || entry.event || 'snapshot';
    const progress = snapshot.progress || {};
    const done = progress.done ?? null;
    const total = progress.total ?? null;
    if (Number.isFinite(done) && Number.isFinite(total) && (done || total)) {
      details += ` · ${done}/${total} tiles`;
    }
    if (snapshot.error) {
      details += ` · ${snapshot.error}`;
    }
    if (!snapshot.state && entry.event && entry.event !== 'snapshot') {
      details = `${entry.event}${entry.data?.count ? ` #${entry.data.count}` : ''}`;
    }
    summary.textContent = details;
    item.append(meta, summary);
    logEl.prepend(item);
    while (logEl.children.length > MAX_EVENT_ROWS) {
      logEl.removeChild(logEl.lastChild);
    }
  };

  const formatEventData = (data) => {
    if (data === undefined || data === null) {
      return '';
    }
    if (typeof data === 'string') {
      return data;
    }
    try {
      return JSON.stringify(data);
    } catch {
      return String(data);
    }
  };

  const appendEventLine = (entry) => {
    const item = document.createElement('li');
    const meta = document.createElement('div');
    meta.className = 'event-feed__meta';
    meta.textContent = `${formatEventTimestamp(entry.timestamp)} · ${
      entry.event || 'event'
    }`;
    const summary = document.createElement('div');
    summary.className = 'event-feed__summary';
    let details = entry.event || 'event';
    if (entry.data) {
      const formatted = formatEventData(entry.data);
      if (formatted) {
        details += ` · ${formatted}`;
      }
    }
    summary.textContent = details;
    item.append(meta, summary);
    logEl.prepend(item);
    while (logEl.children.length > MAX_EVENT_ROWS) {
      logEl.removeChild(logEl.lastChild);
    }
  };

  const handleLine = (line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      return;
    }
    try {
      const entry = JSON.parse(trimmed);
      const kind = entry.event || 'snapshot';
      if (kind === 'heartbeat') {
        setStatus(`Heartbeat ${entry.data?.count ?? ''}`.trim(), 'pending');
      } else if (entry.snapshot) {
        appendEntry(entry);
        setStatus(`Event ${entry.sequence ?? '—'} received.`, 'success');
      } else if (entry.event) {
        appendEventLine(entry);
        setStatus(`Event ${entry.event}`, 'info');
      }
      if (entry.timestamp) {
        cursor = entry.timestamp;
      }
    } catch (error) {
      console.error('Failed to parse events payload', error);
    }
  };

  const streamOnce = async () => {
    const params = new URLSearchParams();
    if (cursor) {
      params.set('since', cursor);
    }
    const template = root.dataset.eventsTemplate || '/jobs/{job_id}/events';
    const response = await fetch(buildTemplateUrl(template, activeJobId, params), {
      signal: abortController.signal,
    });
    if (response.status === 404) {
      setStatus('Events feed not available yet.', 'warning');
      await sleep(EVENTS_RETRY_INTERVAL_MS);
      return;
    }
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    if (!(response.body?.getReader)) {
      const text = await response.text();
      text.split('\n').forEach(handleLine);
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    setStatus(`Streaming events for ${activeJobId}…`, 'success');
    while (!abortController?.signal.aborted) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        if (buffer) {
          buffer.split('\n').forEach(handleLine);
        }
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let newlineIndex = buffer.indexOf('\n');
      while (newlineIndex >= 0) {
        const line = buffer.slice(0, newlineIndex);
        handleLine(line);
        buffer = buffer.slice(newlineIndex + 1);
        newlineIndex = buffer.indexOf('\n');
      }
    }
  };

  const streamLoop = async () => {
    while (abortController && !abortController.signal.aborted) {
      try {
        await streamOnce();
      } catch (error) {
        if (abortController?.signal.aborted) {
          return;
        }
        console.error('Events feed failed', error);
        setStatus(error.message || 'Events feed error', 'error');
      }
      if (abortController?.signal.aborted) {
        break;
      }
      await sleep(EVENTS_RETRY_INTERVAL_MS);
    }
  };

  const connect = (jobId, { resetCursor = true } = {}) => {
    activeJobId = jobId || 'demo';
    if (resetCursor) {
      cursor = null;
    }
    resetLog();
    stop();
    abortController = new AbortController();
    setStatus(`Connecting to events for ${activeJobId}…`, 'pending');
    if (pauseButton) {
      pauseButton.disabled = false;
    }
    if (resumeButton) {
      resumeButton.disabled = true;
    }
    streamTask = streamLoop().catch((error) => {
      if (!abortController?.signal.aborted) {
        console.error('Events stream crashed', error);
        setStatus(error.message || 'Events feed error', 'error');
        if (resumeButton) {
          resumeButton.disabled = false;
        }
      }
    });
  };

  const stop = () => {
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    streamTask = null;
    if (pauseButton) {
      pauseButton.disabled = true;
    }
    if (resumeButton) {
      resumeButton.disabled = false;
    }
  };

  pauseButton?.addEventListener('click', () => {
    stop();
    setStatus('Stream paused.', 'warning');
  });

  resumeButton?.addEventListener('click', () => {
    if (resumeButton.disabled) {
      return;
    }
    connect(activeJobId, { resetCursor: false });
  });

  return { connect, stop };
}

function initStreamControls(sse) {
  const runButton = document.getElementById('run-job');
  const urlInput = document.getElementById('job-url');
  const jobInput = document.getElementById('job-id');
  const profileSelect = document.getElementById('profile');
  const ocrSelect = document.getElementById('ocr-policy');
  const root = document.querySelector('[data-stream-root]');
  const statusEl = document.querySelector('[data-run-status]');
  if (!runButton || !jobInput || !root || !sse?.connect) {
    return;
  }

  const setRunStatus = (text, variant = 'info') => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.dataset.variant = variant;
  };

  const submitJob = async () => {
    const urlValue = urlInput?.value.trim();
    if (!urlValue) {
      const existingJob = jobInput.value.trim();
      if (existingJob) {
        setRunStatus(`Attaching to job ${existingJob}…`);
        sse.connect(existingJob);
      } else {
        setRunStatus('Provide a URL or job id first.', 'error');
      }
      return;
    }

    const payload = { url: urlValue };
    if (profileSelect?.value && profileSelect.value !== 'default') {
      payload.profile_id = profileSelect.value;
    }
    if (ocrSelect?.value) {
      payload.ocr = { policy: ocrSelect.value };
    }

    runButton.disabled = true;
    setRunStatus('Submitting capture job…');
    try {
      const response = await fetch('/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const job = await response.json();
      if (job?.id) {
        jobInput.value = job.id;
        if (root) {
          root.dataset.jobId = job.id;
        }
        setRunStatus(`Job ${job.id} submitted. Connecting to stream…`, 'success');
        sse.connect(job.id);
      } else {
        setRunStatus('Submission succeeded but response missing job id.', 'error');
      }
    } catch (error) {
      console.error('Job submission failed', error);
      setRunStatus(error.message || 'Failed to submit job', 'error');
    } finally {
      runButton.disabled = false;
    }
  };

  runButton.addEventListener('click', submitJob);

  const refreshButton = document.querySelector('[data-links-refresh]');
  if (refreshButton) {
    const manualRefresh = async () => {
      const template = root.dataset.linksTemplate || '/jobs/{job_id}/links.json';
      const jobId = root.dataset.jobId || jobInput.value.trim() || 'demo';
      const url = buildTemplateUrl(template, jobId);
      try {
        const response = await fetch(url);
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        const target = root.querySelector('[data-sse-field=\"links\"]');
        renderLinks(target, JSON.stringify(data));
      } catch (error) {
        console.error('Failed to refresh links', error);
      }
    };

    refreshButton.addEventListener('click', () => {
      const jobId = root.dataset.jobId || jobInput.value.trim() || 'demo';
      if (sse?.refreshLinks) {
        sse.refreshLinks(jobId);
        return;
      }
      manualRefresh();
    });
  }

}

function init() {
  setupTabs();
  const sse = initSseBridge();
  initStreamControls(sse);
}

init();

function buildTemplateUrl(template, jobId, params) {
  const target = template.replace('{job_id}', encodeURIComponent(jobId || 'demo'));
  if (!params || !params.toString()) {
    return target;
  }
  const separator = target.includes('?') ? '&' : '?';
  return `${target}${separator}${params.toString()}`;
}

function formatEventTimestamp(value) {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
