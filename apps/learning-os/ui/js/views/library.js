/**
 * Library — every imported source, searchable and filterable, with a
 * reading pane (processed document / raw preview / record details).
 */
'use strict';

import { api } from '../api.js';
import { icon, formatIcon } from '../icons.js';
import {
  escHtml, escAttr, fmtDate, relTime, fmtBytes, fmtNumber,
  statusBadge, tag, emptyState, renderMarkdown, toast, spinner, confirmDialog,
} from '../ui.js';
import { state, refreshData } from '../main.js';

const view = {
  query: '',
  status: 'all',
  selectedId: null,
  detail: null,
  tab: 'document',
  busy: false,
  deleting: false,
};

// ── Filtering ────────────────────────────────────────────────────────────────

function filteredSources() {
  const q = view.query.trim().toLowerCase();
  return state.sources.filter(source => {
    if (view.status !== 'all' && source.status !== view.status) return false;
    if (!q) return true;
    return [source.title, source.original_name, source.source_url, source.format, source.kind]
      .some(field => String(field || '').toLowerCase().includes(q));
  });
}

// ── List pane ────────────────────────────────────────────────────────────────

function sourceItem(source) {
  const active = source.id === view.selectedId ? ' active' : '';
  return `
    <button class="source-item${active}" data-source-id="${escAttr(source.id)}">
      <span class="source-item-icon">${formatIcon(source.format)}</span>
      <span class="source-item-main">
        <span class="source-item-title">${escHtml(source.title)}</span>
        <span class="source-item-sub">${escHtml(source.original_name || source.source_url || '')}</span>
        <span class="source-item-meta">
          ${tag(source.format.toUpperCase(), 'tag-format')}
          ${statusBadge(source.status)}
          <span class="row-time">${escHtml(relTime(source.imported_at))}</span>
        </span>
      </span>
    </button>`;
}

function renderList() {
  const listEl = document.getElementById('lib-list');
  if (!listEl) return;
  const items = filteredSources();
  document.getElementById('lib-count').textContent =
    `${items.length} of ${state.sources.length}`;
  if (!state.sources.length) {
    listEl.innerHTML = emptyState('inbox', 'Your library is empty', 'Import files or URLs — originals are preserved and converted to clean markdown.');
    return;
  }
  if (!items.length) {
    listEl.innerHTML = emptyState('search', 'No matches', 'Try a different search or filter.');
    return;
  }
  listEl.innerHTML = items.map(sourceItem).join('');
}

// ── Detail pane ──────────────────────────────────────────────────────────────

const TABS = [
  { id: 'document', label: 'Document', icon: 'book-open' },
  { id: 'raw', label: 'Raw', icon: 'code' },
  { id: 'details', label: 'Details', icon: 'info' },
];

function detailShell(source) {
  return `
    <div class="detail-head">
      <div class="detail-title-wrap">
        <span class="detail-format-icon">${formatIcon(source.format)}</span>
        <div>
          <h2 class="detail-title">${escHtml(source.title)}</h2>
          <div class="detail-sub">
            ${tag(source.format.toUpperCase(), 'tag-format')}
            ${statusBadge(source.status)}
            ${source.word_count ? `<span class="detail-words">${fmtNumber(source.word_count)} words</span>` : ''}
          </div>
        </div>
      </div>
      <div class="detail-actions">
        ${source.source_url ? `<button class="btn btn-ghost" id="lib-open-source" title="Open original in browser">${icon('external-link')}Source</button>` : ''}
        <button class="btn btn-ghost" id="lib-reprocess" ${view.busy ? 'disabled' : ''} title="Regenerate the processed document from the raw artifact">
          ${view.busy ? spinner() : icon('refresh-cw')}Reprocess
        </button>
        <button class="btn btn-danger" id="lib-delete" ${view.deleting ? 'disabled' : ''} title="Delete this source and everything built from it">
          ${view.deleting ? spinner() : icon('trash')}Delete
        </button>
      </div>
    </div>
    <nav class="tabs" id="lib-tabs">
      ${TABS.map(t => `<button class="tab${view.tab === t.id ? ' active' : ''}" data-tab="${t.id}">${icon(t.icon)}${t.label}</button>`).join('')}
    </nav>
    <div class="detail-body" id="lib-tab-body"></div>`;
}

function renderTabBody(source) {
  const body = document.getElementById('lib-tab-body');
  if (!body) return;

  if (view.tab === 'document') {
    if (source.processed_markdown) {
      body.innerHTML = `<article class="markdown">${renderMarkdown(source.processed_markdown)}</article>`;
    } else if (source.processing_error) {
      body.innerHTML = emptyState('circle-alert', 'Processing failed', source.processing_error,
        `<button class="btn btn-primary" id="lib-retry">${icon('refresh-cw')}Try again</button>`);
      body.querySelector('#lib-retry')?.addEventListener('click', reprocessSelected);
    } else {
      body.innerHTML = emptyState('clock', 'Not processed yet', 'Reprocess this source to generate its markdown document.');
    }
    return;
  }

  if (view.tab === 'raw') {
    const items = source.raw_items || source.metadata?.raw_items || [];
    body.innerHTML = `
      <div class="artifact-list">
        ${items.map(item => `
          <div class="artifact">
            ${icon(item.size ? 'file' : 'link')}
            <div class="artifact-main">
              <span class="artifact-label">${escHtml(item.label)}</span>
              <span class="artifact-path">${escHtml(item.path)}</span>
            </div>
            ${item.size ? `<span class="artifact-size">${escHtml(fmtBytes(item.size))}</span>` : ''}
          </div>`).join('') || emptyState('file', 'No raw artifacts recorded')}
      </div>
      <pre class="code-block">${escHtml(source.raw_preview || source.processing_error || 'No raw preview available for this source.')}</pre>`;
    return;
  }

  const meta = source.metadata || {};
  const rows = [
    ['Source ID', source.id],
    ['Kind', source.kind],
    ['Format', source.format],
    ['Status', source.status],
    ['Original name', source.original_name || '—'],
    ['Source URL', source.source_url || '—'],
    ['Imported', fmtDate(source.imported_at)],
    ['Processed', fmtDate(source.processed_at)],
    ['Words', fmtNumber(source.word_count)],
    ['MIME type', meta.mime_type || '—'],
    ['SHA-256', meta.sha256 || '—'],
    ['Processed path', source.processed_path || '—'],
  ];
  body.innerHTML = `
    <dl class="meta-grid">
      ${rows.map(([label, value]) => `
        <div class="meta-row">
          <dt>${escHtml(label)}</dt>
          <dd>${escHtml(String(value))}</dd>
        </div>`).join('')}
    </dl>`;
}

function renderDetail() {
  const pane = document.getElementById('lib-detail');
  if (!pane) return;
  if (!view.detail) {
    pane.innerHTML = emptyState('library', 'Select a source', 'Pick anything from the list to read its processed document.');
    return;
  }
  const source = view.detail;
  pane.innerHTML = detailShell(source);
  renderTabBody(source);

  pane.querySelector('#lib-tabs').addEventListener('click', event => {
    const tab = event.target.closest('[data-tab]');
    if (!tab) return;
    view.tab = tab.dataset.tab;
    pane.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === view.tab));
    renderTabBody(source);
  });
  pane.querySelector('#lib-reprocess')?.addEventListener('click', reprocessSelected);
  pane.querySelector('#lib-delete')?.addEventListener('click', deleteSelected);
  pane.querySelector('#lib-open-source')?.addEventListener('click', () => {
    if (source.source_url) api.openExternal(source.source_url).catch(() => {});
  });
}

async function selectSource(sourceId) {
  view.selectedId = sourceId;
  renderList();
  const pane = document.getElementById('lib-detail');
  if (pane && !view.detail) pane.innerHTML = `<div class="pane-loading">${spinner()}</div>`;
  try {
    const result = await api.getSource(sourceId);
    if (result?.ok && view.selectedId === sourceId) {
      view.detail = result.source;
      renderDetail();
    }
  } catch (error) {
    toast('err', error.message || 'Could not load source.');
  }
}

async function reprocessSelected() {
  if (!view.selectedId || view.busy) return;
  view.busy = true;
  renderDetail();
  try {
    const result = await api.reprocessSource(view.selectedId);
    if (!result.ok) {
      toast('err', result.error || 'Reprocessing failed.');
    } else {
      toast('ok', 'Source reprocessed.');
      view.detail = result.source;
    }
  } catch (error) {
    toast('err', error.message || 'Reprocessing failed.');
  } finally {
    view.busy = false;
    renderDetail();
  }
}

async function deleteSelected() {
  if (!view.selectedId || view.deleting) return;
  const sourceId = view.selectedId;
  const source = view.detail;

  let preview;
  try {
    preview = await api.previewDelete(sourceId);
  } catch (error) {
    toast('err', error.message || 'Could not check delete impact.');
    return;
  }
  if (!preview.ok) {
    toast('err', preview.error || 'Could not check delete impact.');
    return;
  }

  const removed = preview.entities_removed || [];
  const affected = preview.entities_affected || [];
  const relCount = preview.relations_removed || 0;
  const title = source?.title || sourceId;

  const parts = [`<p>This permanently deletes <strong>${escHtml(title)}</strong> — its raw file and processed document` +
    (preview.was_indexed ? ', its wiki note, and everything indexed from it.</p>' : ' (it was never indexed, so nothing else references it).</p>')];

  if (preview.active_job) {
    parts.push('<p>A knowledge-build job for this source is running — it will be cancelled and its output discarded.</p>');
  }
  if (removed.length) {
    parts.push(`<div class="confirm-impact-head">Entities that will be fully deleted (${removed.length})</div>`);
    parts.push(`<div class="confirm-chip-list">${removed.map(e => tag(e.name)).join('')}</div>`);
  }
  if (affected.length) {
    parts.push(`<div class="confirm-impact-head">Entities that will lose this source, but remain (${affected.length})</div>`);
    parts.push(`<div class="confirm-chip-list">${affected.map(e => tag(e.name)).join('')}</div>`);
  }
  if (relCount) {
    parts.push(`<p style="margin-top:10px">${relCount} relation${relCount === 1 ? '' : 's'} derived only from this source will also be removed.</p>`);
  }
  if (preview.was_indexed && !removed.length && !affected.length) {
    parts.push('<p>No entities were derived from this source yet.</p>');
  }

  const confirmed = await confirmDialog({
    title: 'Delete source?',
    bodyHtml: parts.join(''),
    confirmLabel: 'Delete permanently',
    danger: true,
  });
  if (!confirmed) return;

  view.deleting = true;
  renderDetail();
  try {
    const result = await api.deleteSource(sourceId);
    if (!result.ok) {
      toast('err', result.error || 'Delete failed.');
      return;
    }
    toast('ok', `Deleted "${title}" and everything built from it.`);
    view.selectedId = null;
    view.detail = null;
    await refreshData({ silent: true });
    renderList();
    renderDetail();
  } catch (error) {
    toast('err', error.message || 'Delete failed.');
  } finally {
    view.deleting = false;
  }
}

// ── View module ──────────────────────────────────────────────────────────────

export const libraryView = {
  id: 'library',
  title: 'Library',
  icon: 'library',

  render(root, params = {}) {
    root.innerHTML = `
      <header class="view-header">
        <div>
          <h1>Library</h1>
          <p class="view-sub">Raw sources and their processed documents.</p>
        </div>
      </header>
      <div class="split">
        <aside class="split-list">
          <div class="list-toolbar">
            <div class="input-wrap input-search">
              ${icon('search')}
              <input id="lib-search" class="input" type="search" placeholder="Search sources…" value="${escAttr(view.query)}" />
            </div>
            <div class="seg" id="lib-status-filter">
              ${['all', 'processed', 'imported', 'error'].map(s =>
                `<button class="seg-item${view.status === s ? ' active' : ''}" data-status="${s}">${s === 'all' ? 'All' : s[0].toUpperCase() + s.slice(1)}</button>`).join('')}
            </div>
            <span class="list-count" id="lib-count"></span>
          </div>
          <div class="scroll-list" id="lib-list"></div>
        </aside>
        <section class="split-detail" id="lib-detail"></section>
      </div>`;

    root.querySelector('#lib-search').addEventListener('input', event => {
      view.query = event.target.value;
      renderList();
    });
    root.querySelector('#lib-status-filter').addEventListener('click', event => {
      const seg = event.target.closest('[data-status]');
      if (!seg) return;
      view.status = seg.dataset.status;
      root.querySelectorAll('.seg-item').forEach(el =>
        el.classList.toggle('active', el.dataset.status === view.status));
      renderList();
    });
    root.querySelector('#lib-list').addEventListener('click', event => {
      const item = event.target.closest('[data-source-id]');
      if (item && item.dataset.sourceId !== view.selectedId) {
        view.detail = null;
        view.tab = 'document';
        selectSource(item.dataset.sourceId);
      }
    });

    renderList();

    const requested = params.sourceId && state.sources.find(s => s.id === params.sourceId);
    const fallback = view.selectedId && state.sources.find(s => s.id === view.selectedId);
    const initial = requested || fallback || state.sources[0];
    if (initial) {
      view.detail = null;
      selectSource(initial.id);
    } else {
      view.selectedId = null;
      view.detail = null;
      renderDetail();
    }
  },

  update() {
    const root = document.getElementById('view');
    if (!root || root.dataset.view !== 'library') return;
    renderList();
    if (view.selectedId && !state.sources.some(s => s.id === view.selectedId)) {
      view.selectedId = null;
      view.detail = null;
      renderDetail();
    }
  },
};
