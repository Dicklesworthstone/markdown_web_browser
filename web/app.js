const MAX_EVENT_ROWS = 50;
const EVENTS_RETRY_INTERVAL_MS = 2000;
const EMBEDDING_DIM = 1536;
const WARNING_LABELS = {
  'canvas-heavy': 'Canvas Heavy',
  'video-heavy': 'Video Heavy',
  'sticky-chrome': 'Sticky Overlay',
  'ocr-quota': 'OCR Quota',
};
const LOCAL_STORAGE_CRAWLED_KEY = 'mdwb:crawled-links';
const LINK_ACTION_EVENT = 'mdwb:link-open';
const crawledLinks = new Set(loadCrawledLinkIds());

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

function initSseHandlers() {
  const root = document.querySelector('[data-stream-root]');
  const statusEl = document.getElementById('job-sse-status');
  if (!root || !statusEl) {
    return null;
  }
  if (typeof window.htmx === 'undefined') {
    console.warn('htmx not available; SSE extension disabled');
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
  const ocrQuotaEl = root.querySelector('[data-ocr-quota]');
  const ocrBatchesEl = root.querySelector('[data-ocr-batches]');
  const ocrAutotuneEl = root.querySelector('[data-ocr-autotune]');
  const seamSummaryEl = root.querySelector('[data-seam-summary]');
  const seamTableEl = root.querySelector('[data-seam-table]');
  const domAssistSummaryEl = root.querySelector('[data-dom-assist-summary]');
  const domAssistTableEl = root.querySelector('[data-dom-assist-table]');

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
          seamSummaryEl,
          seamTableEl,
          domAssistSummaryEl,
          domAssistTableEl,
          ocrQuotaEl,
          ocrBatchesEl,
          ocrAutotuneEl,
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
    const template = root.dataset.linksTemplate || '/jobs/{job_id}/links.json';
    try {
      const data = await fetchTemplateJson(template, jobId);
      updateField('links', JSON.stringify(data));
    } catch (error) {
      console.error('Failed to refresh links', error);
    }
  };

  const refreshManifest = async (jobId) => {
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

  const state = {
    jobId: root.dataset.jobId || 'demo',
    streamTemplate: root.dataset.streamTemplate || '/jobs/{job_id}/stream',
  };

  const setConnectUrl = (jobId) => {
    const url = buildTemplateUrl(state.streamTemplate, jobId);
    root.setAttribute('sse-connect', url);
    window.htmx.process(root);
  };

  const connect = (jobId) => {
    const resolved = jobId || 'demo';
    state.jobId = resolved;
    root.dataset.jobId = resolved;
    const jobField = document.getElementById('job-id');
    if (jobField) {
      jobField.value = resolved;
    }
    setStatus('Connecting…', 'pending');
    setConnectUrl(resolved);
    embeddingsPanel?.setJobId(resolved);
    eventsPanel?.connect(resolved);
    refreshManifest(resolved);
    refreshLinks(resolved);
  };

  const parseJson = (value) => {
    if (!value || typeof value !== 'string') {
      return null;
    }
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  };

  const handleDomAssist = (payload) => {
    const data = parseJson(payload) || {};
    eventsPanel?.appendSyntheticEvent?.({
      event: 'dom_assist',
      data,
    });
    renderDomAssistSummary(domAssistSummaryEl, domAssistTableEl, data);
  };

  const eventHandlers = {
    state: (data) => {
      updateField('state', data);
      const normalized = (data || '').trim().toUpperCase();
      if (normalized === 'DONE' || normalized === 'FAILED') {
        refreshManifest(state.jobId);
        refreshLinks(state.jobId);
      }
    },
    progress: (data) => updateField('progress', data),
    runtime: (data) => updateField('runtime', data),
    rendered: (data) => updateField('rendered', data),
    raw: (data) => updateField('raw', data),
    manifest: (data) => updateField('manifest', data),
    links: (data) => updateField('links', data),
    artifacts: (data) => updateField('artifacts', data),
    warnings: (data) => renderWarnings(warningListEl, data),
    blocklist: (data) => renderBlocklistHits(blocklistHitsEl, data),
    sweep: (data) => {
      const parsed = parseJson(data) || {};
      renderSweepStats(sweepStatsEl, parsed);
      updateSweepSummary(sweepSummaryEl, parsed);
    },
    validation: (data) => {
      const parsed = parseJson(data) || [];
      renderValidationFailures(validationListEl, parsed);
      updateValidationSummary(validationSummaryEl, parsed);
    },
    ocr_telemetry: (data) => {
      const parsed = parseJson(data);
      if (parsed) {
        renderOcrQuota(ocrQuotaEl, {
          limit: parsed.quota_limit,
          used: parsed.quota_used,
          warning_triggered: parsed.quota_warning,
        });
        renderOcrAutotune(ocrAutotuneEl, parsed.autotune);
      }
    },
    dom_assist: handleDomAssist,
  };

  document.body.addEventListener('htmx:sseOpen', (event) => {
    if (event.target === root) {
      setStatus(`Connected (${state.jobId})`, 'success');
    }
  });

  document.body.addEventListener('htmx:sseError', (event) => {
    if (event.target === root) {
      setStatus('Disconnected — retrying…', 'warning');
    }
  });

  document.body.addEventListener('htmx:sseClose', (event) => {
    if (event.target === root) {
      setStatus('Connection closed', 'warning');
    }
  });

  document.body.addEventListener('htmx:sseBeforeMessage', (event) => {
    if (event.target !== root) {
      return;
    }
    const message = event.detail;
    const name = message?.type || message?.event || '';
    const handler = eventHandlers[name];
    if (!handler) {
      return;
    }
    event.preventDefault();
    handler(message.data);
  });

  connect(state.jobId);

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
    seamSummaryEl,
    seamTableEl,
    domAssistSummaryEl,
    domAssistTableEl,
    ocrQuotaEl,
    ocrBatchesEl,
    ocrAutotuneEl,
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
  renderSeamSummary(seamSummaryEl, parsedPayload?.seam_markers);
  renderSeamMarkers(seamTableEl, parsedPayload?.seam_markers);
  renderDomAssistSummary(domAssistSummaryEl, domAssistTableEl, parsedPayload?.dom_assist_summary);
  renderOcrQuota(ocrQuotaEl, parsedPayload?.ocr_quota);
  renderOcrBatches(ocrBatchesEl, parsedPayload?.ocr_batches);
  renderOcrAutotune(ocrAutotuneEl, parsedPayload?.ocr_autotune);
  element.textContent = formatted;
}

function renderSeamSummary(element, markers) {
  if (!element) {
    return;
  }
  const entries = Array.isArray(markers) ? markers.filter((entry) => entry && typeof entry === 'object') : [];
  if (!entries.length) {
    element.textContent = 'No seam markers recorded yet.';
    element.classList.add('placeholder');
    return;
  }
  const tiles = new Set();
  const hashes = new Set();
  entries.forEach((entry) => {
    if (entry.tile_index !== undefined && entry.tile_index !== null) {
      tiles.add(entry.tile_index);
    }
    if (entry.hash) {
      hashes.add(entry.hash);
    }
  });
  element.textContent = `${entries.length} marker${entries.length === 1 ? '' : 's'} · ${tiles.size} tile${tiles.size === 1 ? '' : 's'} · ${hashes.size} hash${hashes.size === 1 ? '' : 'es'}`;
  element.classList.remove('placeholder');
}

function renderSeamMarkers(container, markers) {
  if (!container) {
    return;
  }
  const rows = Array.isArray(markers) ? markers.filter((entry) => entry && typeof entry === 'object') : [];
  if (!rows.length) {
    container.innerHTML = '<p class="placeholder">No seam markers recorded yet.</p>';
    return;
  }
  const positionOrder = { top: 0, bottom: 1 };
  const normalized = rows
    .map((entry) => ({
      tile: entry.tile_index,
      position: typeof entry.position === 'string' ? entry.position : '—',
      hash: typeof entry.hash === 'string' ? entry.hash : '—',
    }))
    .sort((a, b) => {
      const aNum = Number(a.tile);
      const bNum = Number(b.tile);
      let tileCompare = 0;
      if (Number.isFinite(aNum) && Number.isFinite(bNum)) {
        tileCompare = aNum - bNum;
      } else {
        tileCompare = String(a.tile ?? '').localeCompare(String(b.tile ?? ''));
      }
      if (tileCompare !== 0) {
        return tileCompare;
      }
      const aOrder = positionOrder[a.position?.toLowerCase?.()] ?? 2;
      const bOrder = positionOrder[b.position?.toLowerCase?.()] ?? 2;
      return aOrder - bOrder;
    });
  const limit = 10;
  const table = document.createElement('table');
  table.innerHTML = `
    <thead>
      <tr>
        <th scope="col">Tile</th>
        <th scope="col">Position</th>
        <th scope="col">Hash</th>
      </tr>
    </thead>
    <tbody>
      ${normalized
        .slice(0, limit)
        .map(
          (entry) => `
        <tr>
          <td>${entry.tile ?? '—'}</td>
          <td>${entry.position}</td>
          <td><code>${entry.hash}</code></td>
        </tr>
      `,
        )
        .join('')}
    </tbody>
  `;
  container.innerHTML = '';
  container.appendChild(table);
  if (normalized.length > limit) {
    const note = document.createElement('p');
    note.className = 'seam-table__note';
    note.textContent = `Showing ${limit} of ${normalized.length} markers`;
    container.appendChild(note);
  }
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

  const groups = groupLinksByDomain(rows);
  const fragment = document.createDocumentFragment();
  groups.forEach((group) => {
    fragment.appendChild(createLinkGroup(group.label, group.entries));
  });

  container.innerHTML = '';
  container.appendChild(fragment);
  ensureLinkActionHandlers(container);
}

function ensureLinkActionHandlers(container) {
  if (container.dataset.linkActionsBound === 'true') {
    return;
  }
  container.addEventListener('click', (event) => handleLinkAction(event));
  container.dataset.linkActionsBound = 'true';
}

async function handleLinkAction(event) {
  const button = event.target.closest('[data-link-action]');
  if (!button) {
    return;
  }
  const action = button.dataset.linkAction;
  const href = button.dataset.href;
  if (!action || !href) {
    return;
  }
  if (action === 'open') {
    document.dispatchEvent(
      new CustomEvent(LINK_ACTION_EVENT, {
        detail: { href },
      }),
    );
    return;
  }
  if (action === 'copy') {
    const markdown = button.dataset.markdown || `[${button.dataset.linkText || href}](${href})`;
    const success = await copyTextToClipboard(markdown);
    if (success) {
      setButtonFeedback(button, 'Copied');
    }
    return;
  }
  if (action === 'crawled') {
    const row = button.closest('[data-link-row]');
    const nextState = !row?.classList.contains('link-row--crawled');
    setLinkCrawledFlag(href, nextState);
    updateRowCrawledState(row, button, nextState);
  }
}

function groupLinksByDomain(rows) {
  const map = new Map();
  rows.forEach((row) => {
    const label = row.domain || deriveDomain(row.href);
    const key = label.toLowerCase();
    if (!map.has(key)) {
      map.set(key, { label, entries: [] });
    }
    map.get(key).entries.push(row);
  });
  return Array.from(map.values()).sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }));
}

function createLinkGroup(domain, entries) {
  const section = document.createElement('section');
  section.className = 'link-group';
  const header = document.createElement('header');
  header.className = 'link-group__header';
  const title = document.createElement('h3');
  title.textContent = domain;
  const count = document.createElement('span');
  count.className = 'link-group__count';
  count.textContent = `${entries.length} link${entries.length === 1 ? '' : 's'}`;
  header.append(title, count);
  section.appendChild(header);

  const table = document.createElement('table');
  table.className = 'link-group__table';
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Link', 'Coverage', 'Attributes', 'Actions'].forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');

  const sorted = [...entries].sort((a, b) => {
    const left = (a.text || a.href || '').toLowerCase();
    const right = (b.text || b.href || '').toLowerCase();
    if (left === right) return 0;
    return left > right ? 1 : -1;
  });

  sorted.forEach((entry) => tbody.appendChild(createLinkRow(entry)));
  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

function createLinkRow(entry) {
  const row = document.createElement('tr');
  row.dataset.linkRow = 'true';
  row.dataset.href = entry.href || '';
  const isCrawled = isLinkCrawled(entry.href);
  row.classList.toggle('link-row--crawled', isCrawled);

  const linkCell = document.createElement('td');
  const title = document.createElement('div');
  title.className = 'link-entry__text';
  title.textContent = entry.text || '(no text)';
  const href = document.createElement('a');
  href.className = 'link-entry__href';
  href.href = entry.href || '#';
  href.target = '_blank';
  href.rel = 'noreferrer noopener';
  href.textContent = entry.href || '—';
  linkCell.append(title, href);

  const coverageCell = document.createElement('td');
  buildCoverageBadges(entry, isCrawled).forEach((badge) => coverageCell.appendChild(badge));

  const attributesCell = document.createElement('td');
  buildAttributeBadges(entry).forEach((badge) => attributesCell.appendChild(badge));
  if (!attributesCell.childElementCount) {
    attributesCell.textContent = '—';
  }

  const actionsCell = document.createElement('td');
  actionsCell.className = 'link-actions';
  const actionButtons = [
    createActionButton('Open in new job', 'open', entry),
    createActionButton('Copy Markdown', 'copy', entry),
    createActionButton('Mark crawled', 'crawled', entry),
  ];
  const crawlButton = actionButtons[2];
  if (isCrawled) {
    crawlButton.textContent = 'Unmark';
  }
  actionButtons.forEach((button) => actionsCell.appendChild(button));

  [linkCell, coverageCell, attributesCell, actionsCell].forEach((cell) => row.appendChild(cell));
  return row;
}

function buildCoverageBadges(entry, isCrawled) {
  const badges = [];
  const coverageLabel = getCoverageLabel(entry.source);
  badges.push(createBadge(coverageLabel, getCoverageVariant(entry.source)));
  const delta = (entry.delta || '').toLowerCase();
  if (entry.delta && entry.delta !== '✓' && delta !== 'dom only' && delta !== 'ocr only') {
    const variant = delta.includes('mismatch') ? 'error' : 'warning';
    badges.push(createBadge(entry.delta, variant));
  }
  if (entry.kind === 'form') {
    badges.push(createBadge('Form', 'info'));
  } else if (entry.kind === 'markdown' && entry.source === 'OCR') {
    badges.push(createBadge('OCR text', 'muted'));
  }
  const crawledBadge = createBadge('Crawled', 'muted');
  crawledBadge.dataset.crawledBadge = 'true';
  crawledBadge.hidden = !isCrawled;
  badges.push(crawledBadge);
  return badges;
}

function buildAttributeBadges(entry) {
  const badges = [];
  if (entry.target) {
    badges.push(createBadge(`target ${entry.target}`, 'info'));
  }
  const relTokens = Array.isArray(entry.rel)
    ? entry.rel
    : entry.rel
    ? String(entry.rel).split(/\s+/)
    : [];
  relTokens
    .map((token) => token.trim())
    .filter(Boolean)
    .forEach((token) => badges.push(createBadge(`rel ${token}`, 'muted')));
  return badges;
}

function createActionButton(label, action, entry) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'link-action';
  button.textContent = label;
  button.dataset.linkAction = action;
  button.dataset.href = entry.href || '';
  button.dataset.markdown = entry.markdown || `[${entry.text || entry.href || 'link'}](${entry.href || '#'})`;
  button.dataset.linkText = entry.text || '';
  button.dataset.defaultLabel = label;
  return button;
}

function createBadge(label, variant = 'muted') {
  const span = document.createElement('span');
  span.className = `link-badge link-badge--${variant}`;
  span.textContent = label;
  return span;
}

function getCoverageLabel(source = '') {
  const normalized = source.toUpperCase();
  if (normalized === 'DOM+OCR') return 'DOM+OCR';
  if (normalized === 'DOM') return 'DOM only';
  if (normalized === 'OCR') return 'OCR only';
  return normalized || 'Unknown';
}

function getCoverageVariant(source = '') {
  const normalized = source.toUpperCase();
  if (normalized === 'DOM+OCR') return 'success';
  if (normalized === 'DOM') return 'info';
  if (normalized === 'OCR') return 'warning';
  return 'muted';
}

function deriveDomain(href) {
  if (!href) {
    return '(unknown)';
  }
  const match = href.match(/^https?:\/\/([^/]+)/i);
  if (match) {
    return match[1].toLowerCase();
  }
  if (href.startsWith('#')) {
    return '(fragment)';
  }
  const schemeMatch = href.match(/^([a-z0-9+.-]+):/i);
  if (schemeMatch) {
    return `${schemeMatch[1].toLowerCase()}:`;
  }
  return '(relative)';
}

function setLinkCrawledFlag(href, isCrawled) {
  if (!href) {
    return;
  }
  if (isCrawled) {
    crawledLinks.add(href);
  } else {
    crawledLinks.delete(href);
  }
  persistCrawledLinks();
}

function isLinkCrawled(href) {
  if (!href) {
    return false;
  }
  return crawledLinks.has(href);
}

function updateRowCrawledState(row, button, isCrawled) {
  if (!row || !button) {
    return;
  }
  row.classList.toggle('link-row--crawled', isCrawled);
  button.textContent = isCrawled ? 'Unmark' : button.dataset.defaultLabel || 'Mark crawled';
  const badge = row.querySelector('[data-crawled-badge]');
  if (badge) {
    badge.hidden = !isCrawled;
  }
}

function loadCrawledLinkIds() {
  if (typeof localStorage === 'undefined') {
    return [];
  }
  try {
    const stored = localStorage.getItem(LOCAL_STORAGE_CRAWLED_KEY);
    if (!stored) {
      return [];
    }
    const parsed = JSON.parse(stored);
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch {
    return [];
  }
}

function persistCrawledLinks() {
  if (typeof localStorage === 'undefined') {
    return;
  }
  try {
    localStorage.setItem(LOCAL_STORAGE_CRAWLED_KEY, JSON.stringify(Array.from(crawledLinks)));
  } catch (error) {
    console.warn('Failed to persist crawled link state', error);
  }
}

async function copyTextToClipboard(text) {
  if (!text) {
    return false;
  }
  try {
    if (typeof navigator !== 'undefined' && navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (error) {
    console.warn('Clipboard API failed, falling back', error);
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let success = false;
  try {
    success = document.execCommand('copy');
  } catch (error) {
    console.warn('Fallback copy failed', error);
    success = false;
  }
  document.body.removeChild(textarea);
  return success;
}

function setButtonFeedback(button, label) {
  const original = button.dataset.defaultLabel || button.textContent;
  button.textContent = label;
  setTimeout(() => {
    button.textContent = original;
  }, 1500);
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

function renderOcrQuota(container, payload) {
  if (!container) {
    return;
  }
  let quota = payload;
  if (typeof payload === 'string') {
    try {
      quota = JSON.parse(payload);
    } catch {
      quota = null;
    }
  }
  container.innerHTML = '';
  if (!quota || Object.values(quota).every((value) => value === null || value === undefined)) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'Quota data will appear once OCR completes.';
    container.appendChild(p);
    return;
  }

  const limit = quota.limit ?? '—';
  const used = quota.used ?? '—';
  const thresholdRatio = quota.threshold_ratio ?? 0;
  const warning = Boolean(quota.warning_triggered);
  const summary = document.createElement('div');
  summary.className = 'ocr-quota__pill';
  summary.textContent = `Limit: ${limit} • Used: ${used}`;
  const threshold = document.createElement('div');
  threshold.className = 'ocr-quota__pill';
  threshold.textContent = `Warning at ${(Number(thresholdRatio) * 100).toFixed(0)}%`;
  container.append(summary, threshold);
  if (warning) {
    const alert = document.createElement('strong');
    alert.className = 'ocr-quota__warning';
    alert.textContent = '⚠ 70% quota threshold exceeded';
    container.append(alert);
  }
}

function renderOcrBatches(container, payload) {
  if (!container) {
    return;
  }
  let batches = payload;
  if (typeof payload === 'string') {
    try {
      batches = JSON.parse(payload);
    } catch {
      batches = null;
    }
  }
  container.innerHTML = '';
  if (!Array.isArray(batches) || !batches.length) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'No OCR batch data yet.';
    container.appendChild(p);
    return;
  }
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['Tile IDs', 'Latency (ms)', 'Status', 'Attempts', 'Request ID', 'Payload (bytes)'].forEach((label) => {
    const th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  batches.forEach((batch) => {
    const tr = document.createElement('tr');
    const cells = [
      Array.isArray(batch.tile_ids) ? batch.tile_ids.join(', ') : '—',
      batch.latency_ms ?? '—',
      batch.status_code ?? '—',
      batch.attempts ?? '—',
      batch.request_id ?? '—',
      batch.payload_bytes ?? '—',
    ];
    cells.forEach((value) => {
      const td = document.createElement('td');
      td.textContent = String(value);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.appendChild(table);
}

function renderOcrAutotune(container, payload) {
  if (!container) {
    return;
  }
  let data = payload;
  if (typeof payload === 'string') {
    try {
      data = JSON.parse(payload);
    } catch {
      data = null;
    }
  }
  container.innerHTML = '';
  if (!data) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'No concurrency adjustments recorded yet.';
    container.appendChild(p);
    return;
  }
  const summary = document.createElement('div');
  summary.className = 'ocr-autotune__summary';
  const metrics = [
    ['Initial', data.initial_limit ?? '—'],
    ['Peak', data.peak_limit ?? '—'],
    ['Final', data.final_limit ?? '—'],
  ];
  metrics.forEach(([label, value]) => {
    const block = document.createElement('div');
    const labelSpan = document.createElement('span');
    labelSpan.textContent = label;
    const valueEl = document.createElement('strong');
    valueEl.textContent = String(value);
    block.append(labelSpan, valueEl);
    summary.appendChild(block);
  });
  container.appendChild(summary);

  const events = Array.isArray(data.events) && data.events.length
    ? data.events
    : data.last_event
    ? [data.last_event]
    : [];
  if (!events.length) {
    const note = document.createElement('p');
    note.className = 'placeholder';
    note.textContent = 'No autotune events yet.';
    container.appendChild(note);
    return;
  }
  const list = document.createElement('ul');
  list.className = 'ocr-autotune__events';
  events.slice(-5).reverse().forEach((event) => {
    if (!event) {
      return;
    }
    const item = document.createElement('li');
    const left = document.createElement('span');
    left.className = 'ocr-autotune__event-reason';
    left.textContent = `${event.previous_limit ?? '—'}→${event.new_limit ?? '—'} (${event.reason || 'change'})`;
    const right = document.createElement('span');
    const latency = event.latency_ms != null ? `${event.latency_ms} ms` : '—';
    right.textContent = `${event.status_code ?? '—'} • ${latency}`;
    item.append(left, right);
    list.appendChild(item);
  });
  container.appendChild(list);
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

  const appendSyntheticEvent = ({ event, data }) => {
    appendEventLine({
      event,
      data,
      timestamp: new Date().toISOString(),
    });
  };

  return { connect, stop, appendSyntheticEvent };
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

  document.addEventListener(LINK_ACTION_EVENT, (event) => {
    const href = event?.detail?.href;
    if (!href || !urlInput) {
      return;
    }
    urlInput.value = href;
    if (runButton.disabled) {
      setRunStatus('A capture is already running. Please wait for it to finish.', 'warning');
      return;
    }
    setRunStatus(`Submitting capture for ${href}…`);
    submitJob();
  });

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
  const sse = initSseHandlers();
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
function renderDomAssistSummary(summaryEl, tableEl, summary) {
  const hasData = summary && typeof summary.count === 'number' && summary.count > 0;
  if (summaryEl) {
    if (!hasData) {
      summaryEl.textContent = 'No DOM assists recorded yet.';
      summaryEl.classList.add('placeholder');
    } else {
      summaryEl.textContent = `${summary.count} assist${summary.count === 1 ? '' : 's'} · ${summary.reasons?.length || 0} reason${summary.reasons?.length === 1 ? '' : 's'}`;
      summaryEl.classList.remove('placeholder');
    }
  }
  if (!tableEl) {
    return;
  }
  if (!hasData) {
    tableEl.innerHTML = '<p class="placeholder">DOM assist reasons will appear here once assists trigger.</p>';
    return;
  }
  const rows = Array.isArray(summary.reason_counts) && summary.reason_counts.length
    ? summary.reason_counts
    : (summary.reasons || []).map((reason) => ({ reason, count: '' }));
  const sample = summary.sample || {};
  const sampleRow = sample.dom_text
    ? `<div class="dom-assist-table__note">Sample (${sample.reason || 'unknown'}): <code>${escapeHtml(sample.dom_text)}</code></div>`
    : '';
  const table = [
    '<table>',
    '<thead><tr><th>Reason</th><th>Count</th></tr></thead>',
    '<tbody>',
    ...rows.map((entry) => `<tr><td>${escapeHtml(String(entry.reason ?? 'unknown'))}</td><td>${entry.count ?? ''}</td></tr>`),
    '</tbody></table>',
    sampleRow,
  ].join('');
  tableEl.innerHTML = table;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
