/**
 * Lexicon.ai app shell — state store, view router, sidebar, global import.
 *
 * Views are self-contained modules registered in VIEWS. Adding a page means
 * writing one module with { id, title, icon, render, update?, destroy? } and
 * appending it here — the sidebar, routing, and refresh loop pick it up.
 */
'use strict';

import { api, uploadFiles, healthCheck } from './api.js';
import { icon, logo } from './icons.js';
import { escHtml, toast, spinner } from './ui.js';
import { dashboardView } from './views/dashboard.js';
import { libraryView } from './views/library.js';
import { wikiView } from './views/wiki.js';
import { graphView } from './views/graph.js';

// ── State store ───────────────────────────────────────────────────────────────

export const state = {
  ready: false,
  config: null,
  overview: null,
  sources: [],
  wiki: { pages: [], edges: [] },
  knowledge: null,
  importing: false,
  route: { id: 'dashboard', params: {} },
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try { fn(payload); } catch (error) { console.error(error); }
  }
}

// ── Router ────────────────────────────────────────────────────────────────────

const VIEWS = [dashboardView, libraryView, wikiView, graphView];
let activeView = null;

export function navigate(id, params = {}) {
  const view = VIEWS.find(v => v.id === id) || VIEWS[0];
  if (activeView?.destroy) activeView.destroy();
  state.route = { id: view.id, params };
  activeView = view;

  document.querySelectorAll('#side-nav .nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view.id);
  });

  const root = document.getElementById('view');
  root.innerHTML = '';
  root.dataset.view = view.id;
  view.render(root, params);
}

// ── Data refresh ──────────────────────────────────────────────────────────────

let lastSnapshot = '';

export async function refreshData({ silent = false } = {}) {
  try {
    const [overview, sources, wiki, knowledge] = await Promise.all([
      api.overview(),
      api.listSources(),
      api.wikiIndex(),
      api.knowledgeStatus(),
    ]);
    state.overview = overview;
    state.sources = Array.isArray(sources) ? sources : [];
    state.wiki = wiki?.ok ? wiki : { pages: [], edges: [] };
    state.knowledge = knowledge?.ok ? knowledge : null;

    const snapshot = JSON.stringify([overview, state.sources, state.wiki.pages, state.wiki.edges, state.knowledge]);
    const changed = snapshot !== lastSnapshot;
    lastSnapshot = snapshot;

    renderNavCounts();
    if (changed) {
      emit('data');
      if (activeView?.update) activeView.update();
    }
    return changed;
  } catch (error) {
    if (!silent) toast('err', error.message || 'Failed to refresh workspace.');
    return false;
  }
}

const POLL_MS = 12000;
setInterval(() => {
  if (!state.ready || document.hidden || state.importing) return;
  refreshData({ silent: true });
}, POLL_MS);

// ── Import flows (shared by modal, dashboard, drag-and-drop) ─────────────────

export async function runImportFiles(files) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length || state.importing) return;
  state.importing = true;
  emit('import-status', { kind: 'info', message: `Importing ${list.length} file${list.length === 1 ? '' : 's'}…` });
  try {
    const result = await uploadFiles(list);
    const imported = result.sources || [];
    if (!result.ok && !imported.length) {
      const message = result.errors?.[0]?.error || result.error || 'Import failed.';
      emit('import-status', { kind: 'err', message });
      toast('err', message);
      return;
    }
    if (result.errors?.length) {
      toast('err', `${result.errors.length} file${result.errors.length === 1 ? '' : 's'} failed: ${result.errors[0].error}`);
    }
    emit('import-status', { kind: 'ok', message: `Imported ${imported.length} source${imported.length === 1 ? '' : 's'}.` });
    toast('ok', `Imported ${imported.length} source${imported.length === 1 ? '' : 's'}.`);
    state.importing = false;
    await refreshData({ silent: true });
    closeImportModal();
    const first = imported[0]?.source;
    navigate('library', first?.id ? { sourceId: first.id } : {});
  } catch (error) {
    emit('import-status', { kind: 'err', message: error.message || 'Import failed.' });
    toast('err', error.message || 'Import failed.');
  } finally {
    state.importing = false;
  }
}

export async function runImportUrl(url) {
  const target = String(url || '').trim();
  if (!target) {
    emit('import-status', { kind: 'err', message: 'Paste a URL first.' });
    return;
  }
  if (state.importing) return;
  state.importing = true;
  emit('import-status', { kind: 'info', message: 'Fetching and processing URL…' });
  try {
    const result = await api.importUrl(target);
    if (!result.ok) {
      const message = result.error || 'URL import failed.';
      emit('import-status', { kind: 'err', message });
      toast('err', message);
      return;
    }
    emit('import-status', { kind: 'ok', message: 'URL imported and processed.' });
    toast('ok', 'URL imported and processed.');
    state.importing = false;
    await refreshData({ silent: true });
    closeImportModal();
    navigate('library', result.source?.id ? { sourceId: result.source.id } : {});
  } catch (error) {
    emit('import-status', { kind: 'err', message: error.message || 'URL import failed.' });
    toast('err', error.message || 'URL import failed.');
  } finally {
    state.importing = false;
  }
}

export function pickFiles() {
  const input = document.getElementById('file-input');
  input.value = '';
  input.click();
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

function renderSidebar() {
  const nav = document.getElementById('side-nav');
  nav.innerHTML = VIEWS.map(view => `
    <button class="nav-item${view.id === state.route.id ? ' active' : ''}" data-view="${view.id}">
      ${icon(view.icon)}
      <span class="nav-label">${escHtml(view.title)}</span>
      <span class="nav-count" data-count-for="${view.id}"></span>
    </button>
  `).join('');
  nav.addEventListener('click', event => {
    const item = event.target.closest('.nav-item');
    if (item) navigate(item.dataset.view);
  });
}

function renderNavCounts() {
  const counts = {
    library: state.overview?.raw_count,
    wiki: state.overview?.wiki_count,
  };
  for (const [viewId, count] of Object.entries(counts)) {
    const el = document.querySelector(`[data-count-for="${viewId}"]`);
    if (el) el.textContent = count ? String(count) : '';
  }
}

function renderModelChip() {
  const el = document.getElementById('model-chip');
  const model = state.config?.default_model || '';
  el.innerHTML = `${icon('cpu')}<span>${escHtml(model ? model.split('/').pop() : 'No model')}</span>`;
  el.title = model;
}

function setEngineStatus(online) {
  const el = document.getElementById('engine-status');
  el.className = `engine-status ${online ? 'online' : 'offline'}`;
  el.innerHTML = `<span class="status-dot"></span>${online ? 'Engine online' : 'Engine offline'}`;
}

// ── Import modal ──────────────────────────────────────────────────────────────

export function openImportModal() {
  const modal = document.getElementById('import-modal');
  modal.hidden = false;
  requestAnimationFrame(() => modal.classList.add('open'));
  setTimeout(() => document.getElementById('import-url-input')?.focus(), 60);
}

export function closeImportModal() {
  const modal = document.getElementById('import-modal');
  modal.classList.remove('open');
  setTimeout(() => { modal.hidden = true; }, 180);
}

function wireImportModal() {
  const modal = document.getElementById('import-modal');
  const urlInput = document.getElementById('import-url-input');
  const statusEl = document.getElementById('import-modal-status');

  document.getElementById('btn-sidebar-import').addEventListener('click', openImportModal);
  document.getElementById('btn-import-close').addEventListener('click', closeImportModal);
  modal.addEventListener('click', event => {
    if (event.target === modal) closeImportModal();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !modal.hidden) closeImportModal();
  });

  document.getElementById('import-dropzone').addEventListener('click', pickFiles);
  document.getElementById('btn-import-url-go').addEventListener('click', () => runImportUrl(urlInput.value));
  urlInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      runImportUrl(urlInput.value);
    }
  });

  on('import-status', ({ kind, message }) => {
    statusEl.className = `import-status status-${kind}`;
    statusEl.innerHTML = kind === 'info' ? `${spinner()}<span>${escHtml(message)}</span>` : escHtml(message);
    if (kind === 'ok') urlInput.value = '';
  });

  const formats = state.overview?.supported_formats || [];
  document.getElementById('import-formats').innerHTML =
    formats.map(f => `<span class="tag">${escHtml(f)}</span>`).join('');
}

// ── Global drag & drop ────────────────────────────────────────────────────────

function wireDragAndDrop() {
  const overlay = document.getElementById('drop-overlay');
  let depth = 0;

  document.addEventListener('dragenter', event => {
    if (!event.dataTransfer?.types?.includes('Files')) return;
    event.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  document.addEventListener('dragover', event => event.preventDefault());
  document.addEventListener('dragleave', event => {
    event.preventDefault();
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  document.addEventListener('drop', event => {
    event.preventDefault();
    depth = 0;
    overlay.hidden = true;
    if (event.dataTransfer?.files?.length) runImportFiles(event.dataTransfer.files);
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────

async function init() {
  document.getElementById('side-brand').innerHTML =
    `${logo()}<span class="brand-name">Lexicon<span class="brand-tld">.ai</span></span>`;

  renderSidebar();
  document.getElementById('file-input').addEventListener('change', event => {
    runImportFiles(event.target.files);
  });
  wireDragAndDrop();

  const online = await healthCheck();
  setEngineStatus(online);
  if (!online) {
    document.getElementById('view').innerHTML = `
      <div class="empty empty-page">
        <div class="empty-icon">${icon('circle-alert')}</div>
        <div class="empty-title">Backend not reachable</div>
        <div class="empty-sub">Run the app shell (make run) to use Lexicon.ai.</div>
      </div>`;
    return;
  }

  state.config = await api.getConfig();
  renderModelChip();
  await refreshData();
  wireImportModal();
  state.ready = true;
  navigate('dashboard');
}

init().catch(error => {
  console.error(error);
  toast('err', error.message || 'Failed to initialize Lexicon.ai.');
});
