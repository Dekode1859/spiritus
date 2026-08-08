/**
 * Dashboard — workspace pulse: stats, quick capture, recent activity.
 */
'use strict';

import { api } from '../api.js';
import { icon, formatIcon } from '../icons.js';
import { escHtml, escAttr, relTime, fmtNumber, statusBadge, emptyState, spinner, toast, confirmDialog } from '../ui.js';
import { state, on, navigate, refreshData, pickFiles, runImportUrl, openImportModal } from '../main.js';

let offData = null;
let offStatus = null;

// ── Knowledge Build card ──────────────────────────────────────────────────────

function pill(iconName, label, value, cls = '') {
  return `<span class="kb-pill${cls ? ` ${cls}` : ''}">${icon(iconName)}<b>${fmtNumber(value)}</b>${escHtml(label)}</span>`;
}

function knowledgeCard() {
  const kb = state.knowledge;
  if (!kb) {
    return `
      <div class="panel kb-card">
        <div class="kb-head">
          <h2>${icon('sparkles')}Knowledge Build</h2>
          <span class="kb-note">${icon('circle-alert')}Indexing paused — engine offline</span>
        </div>
      </div>`;
  }
  const c = kb.counts || {};
  const s = kb.sources || {};
  const reg = kb.registry || {};
  const active = (c.running || 0) + (c.queued || 0);
  return `
    <div class="panel kb-card">
      <div class="kb-head">
        <h2>${active ? spinner() : icon('sparkles')}Knowledge Build</h2>
        <div class="kb-actions">
          ${c.failed ? `<button class="btn btn-ghost btn-sm" id="kb-retry">${icon('refresh-cw')}Retry ${c.failed} failed</button>` : ''}
          ${(reg.total || 0) > (reg.enriched || 0) ? `<button class="btn btn-ghost btn-sm" id="kb-enrich"${active ? ' disabled' : ''} title="New sources enrich automatically — this only catches up entities from before that existed">${icon('sparkles')}Catch up ${(reg.total || 0) - (reg.enriched || 0)}</button>` : ''}
          <button class="btn btn-ghost btn-sm" id="kb-rebuild"${active ? ' disabled' : ''} title="Re-index and re-enrich every source from scratch">${icon('zap')}Rebuild all</button>
        </div>
      </div>
      <p class="kb-auto-note">${icon('info')}New sources index, get related, and get enriched automatically — nothing here is required.</p>
      <div class="kb-pills">
        ${pill('circle-check', 'indexed', s.indexed || 0, 'ok')}
        ${pill('clock', 'stale', s.stale || 0, (s.stale ? 'warn' : ''))}
        ${pill('inbox', 'unindexed', s.unindexed || 0)}
        ${pill('hash', 'entities', reg.total || 0, 'accent')}
        ${pill('book-open', 'enriched', reg.enriched || 0, ((reg.enriched || 0) === (reg.total || 0) && reg.total ? 'ok' : ''))}
        <span class="kb-sep"></span>
        ${c.running ? pill('loader', 'running', c.running, 'accent') : ''}
        ${c.queued ? pill('clock', 'queued', c.queued) : ''}
        ${c.failed ? pill('circle-alert', 'failed', c.failed, 'err') : ''}
      </div>
    </div>`;
}

function wireKnowledgeCard(root) {
  root.querySelector('#kb-rebuild')?.addEventListener('click', async () => {
    const total = (state.overview?.processed_count) || 0;
    const confirmed = await confirmDialog({
      title: 'Rebuild the entire knowledge base?',
      bodyHtml: `<p>This re-indexes and re-enriches <strong>every</strong> processed source from
        scratch — one model call per source plus one per entity. For ${total || 'your'}
        source${total === 1 ? '' : 's'} that can mean a lot of token usage and take a while.</p>
        <p>New sources already do this automatically as you import them — you only need this
        to refresh everything after changing how indexing works, not for routine use.</p>`,
      confirmLabel: 'Rebuild everything',
      danger: true,
    });
    if (!confirmed) return;
    try {
      const result = await api.rebuildAll();
      toast('ok', `Queued ${result.queued || 0} source${result.queued === 1 ? '' : 's'} for indexing.`);
      await refreshData({ silent: true });
    } catch (error) {
      toast('err', error.message || 'Could not start rebuild.');
    }
  });
  root.querySelector('#kb-retry')?.addEventListener('click', async () => {
    try {
      const result = await api.retryFailed();
      toast('ok', `Retrying ${result.queued || 0} job${result.queued === 1 ? '' : 's'}.`);
      await refreshData({ silent: true });
    } catch (error) {
      toast('err', error.message || 'Could not retry jobs.');
    }
  });
  root.querySelector('#kb-enrich')?.addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.innerHTML = `${spinner()}Enriching…`;
    toast('info', 'Enriching entities with general knowledge — this runs the model per entity.');
    try {
      const result = await api.enrich();
      toast('ok', `Enriched ${result.enriched || 0} entit${result.enriched === 1 ? 'y' : 'ies'}.`);
      await refreshData({ silent: true });
    } catch (error) {
      toast('err', error.message || 'Enrichment failed.');
    }
  });
}

function statTile(iconName, label, value, viewId, accent) {
  return `
    <button class="stat-tile accent-${accent}" data-goto="${viewId}">
      <div class="stat-icon">${icon(iconName)}</div>
      <div class="stat-body">
        <span class="stat-value">${fmtNumber(value)}</span>
        <span class="stat-label">${escHtml(label)}</span>
      </div>
      ${icon('arrow-up-right', 'stat-go')}
    </button>`;
}

function recentSourceRow(source) {
  return `
    <button class="row" data-source-id="${escAttr(source.id)}">
      <span class="row-icon">${formatIcon(source.format)}</span>
      <span class="row-main">
        <span class="row-title">${escHtml(source.title)}</span>
        <span class="row-sub">${escHtml(source.original_name || source.source_url || source.format)}</span>
      </span>
      ${statusBadge(source.status)}
      <span class="row-time">${escHtml(relTime(source.imported_at))}</span>
    </button>`;
}

function recentWikiRow(page) {
  return `
    <button class="row" data-wiki-path="${escAttr(page.path)}">
      <span class="row-icon">${icon(page.folder ? 'calendar' : 'file-text')}</span>
      <span class="row-main">
        <span class="row-title">${escHtml(page.title)}</span>
        <span class="row-sub">${escHtml(page.folder || 'wiki')}</span>
      </span>
      <span class="row-time">${escHtml(relTime(page.modified))}</span>
    </button>`;
}

function template() {
  const overview = state.overview || {};
  const sources = state.sources.slice(0, 6);
  const pages = (state.wiki.pages || []).slice(0, 6);

  return `
    <header class="view-header">
      <div>
        <h1>Dashboard</h1>
        <p class="view-sub">Your knowledge base at a glance.</p>
      </div>
      <div class="view-actions">
        <button class="btn btn-primary" id="dash-import">${icon('plus')}Import</button>
      </div>
    </header>

    <section class="stat-grid" id="dash-stats">
      ${statTile('inbox', 'Raw sources', overview.raw_count || 0, 'library', 'cyan')}
      ${statTile('file-check', 'Processed documents', overview.processed_count || 0, 'library', 'violet')}
      ${statTile('book-open', 'Wiki pages', overview.wiki_count || 0, 'wiki', 'magenta')}
    </section>

    <section class="kb-strip" id="dash-knowledge">
      ${knowledgeCard()}
    </section>

    <section class="dash-columns">
      <div class="panel capture-panel">
        <div class="panel-head">
          <h2>${icon('zap')}Quick capture</h2>
        </div>
        <button class="dropzone" id="dash-dropzone">
          ${icon('upload')}
          <span class="dropzone-title">Drop files anywhere, or browse</span>
          <span class="dropzone-sub">PDF · EPUB · DOCX · HTML · Markdown · TXT · JSON · CSV</span>
        </button>
        <div class="url-capture">
          <div class="input-wrap">
            ${icon('link')}
            <input id="dash-url" class="input" type="url" placeholder="Paste a URL to capture an article…" />
          </div>
          <button class="btn btn-primary" id="dash-url-go">${icon('corner-down-left')}Capture</button>
        </div>
        <div class="import-status" id="dash-status"></div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>${icon('clock')}Recent sources</h2>
          <button class="btn btn-ghost btn-sm" data-goto="library">View all${icon('chevron-right')}</button>
        </div>
        <div class="row-list" id="dash-recent-sources">
          ${sources.length ? sources.map(recentSourceRow).join('')
            : emptyState('inbox', 'Nothing imported yet', 'Drop a file or paste a URL to start your library.')}
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>${icon('sparkles')}Wiki activity</h2>
          <button class="btn btn-ghost btn-sm" data-goto="wiki">Open wiki${icon('chevron-right')}</button>
        </div>
        <div class="row-list" id="dash-recent-wiki">
          ${pages.length ? pages.map(recentWikiRow).join('')
            : emptyState('sparkles', 'The wiki is empty', 'Agents build this space from your processed sources — new pages and weekly folders appear here.')}
        </div>
      </div>
    </section>`;
}

export const dashboardView = {
  id: 'dashboard',
  title: 'Dashboard',
  icon: 'layout-dashboard',

  render(root) {
    root.innerHTML = template();

    root.addEventListener('click', event => {
      const goto = event.target.closest('[data-goto]');
      if (goto) return navigate(goto.dataset.goto);
      const source = event.target.closest('[data-source-id]');
      if (source) return navigate('library', { sourceId: source.dataset.sourceId });
      const page = event.target.closest('[data-wiki-path]');
      if (page) return navigate('wiki', { path: page.dataset.wikiPath });
    });

    wireKnowledgeCard(root);
    root.querySelector('#dash-import').addEventListener('click', openImportModal);
    root.querySelector('#dash-dropzone').addEventListener('click', pickFiles);
    const urlInput = root.querySelector('#dash-url');
    root.querySelector('#dash-url-go').addEventListener('click', () => runImportUrl(urlInput.value));
    urlInput.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runImportUrl(urlInput.value);
      }
    });

    offStatus = on('import-status', ({ kind, message }) => {
      const el = root.querySelector('#dash-status');
      if (!el) return;
      el.className = `import-status status-${kind}`;
      el.innerHTML = kind === 'info' ? `${spinner()}<span>${escHtml(message)}</span>` : escHtml(message);
      if (kind === 'ok') urlInput.value = '';
    });
  },

  update() {
    // Data changed in the background: refresh the read-only regions in place
    // so typing in the capture input is never interrupted.
    const root = document.getElementById('view');
    if (!root || root.dataset.view !== 'dashboard') return;
    const overview = state.overview || {};
    const stats = root.querySelector('#dash-stats');
    if (stats) {
      stats.innerHTML = [
        statTile('inbox', 'Raw sources', overview.raw_count || 0, 'library', 'cyan'),
        statTile('file-check', 'Processed documents', overview.processed_count || 0, 'library', 'violet'),
        statTile('book-open', 'Wiki pages', overview.wiki_count || 0, 'wiki', 'magenta'),
      ].join('');
    }
    const knowledge = root.querySelector('#dash-knowledge');
    if (knowledge) {
      knowledge.innerHTML = knowledgeCard();
      wireKnowledgeCard(knowledge);
    }
    const sources = state.sources.slice(0, 6);
    const recent = root.querySelector('#dash-recent-sources');
    if (recent) {
      recent.innerHTML = sources.length ? sources.map(recentSourceRow).join('')
        : emptyState('inbox', 'Nothing imported yet', 'Drop a file or paste a URL to start your library.');
    }
    const pages = (state.wiki.pages || []).slice(0, 6);
    const wikiList = root.querySelector('#dash-recent-wiki');
    if (wikiList) {
      wikiList.innerHTML = pages.length ? pages.map(recentWikiRow).join('')
        : emptyState('sparkles', 'The wiki is empty', 'Agents build this space from your processed sources — new pages and weekly folders appear here.');
    }
  },

  destroy() {
    if (offStatus) offStatus();
    if (offData) offData();
    offStatus = offData = null;
  },
};
