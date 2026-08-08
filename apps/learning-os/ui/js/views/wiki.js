/**
 * Wiki — the agent-maintained knowledge layer. Pages grouped by folder
 * (weekly folders, topics), a reader with live [[wikilinks]], and backlinks.
 */
'use strict';

import { api } from '../api.js';
import { icon } from '../icons.js';
import { escHtml, escAttr, relTime, fmtNumber, emptyState, renderMarkdown, toast, spinner } from '../ui.js';
import { state, navigate } from '../main.js';

const view = {
  query: '',
  selectedPath: null,
  page: null,
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function slug(text) {
  return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

function resolveTarget(target) {
  const pages = state.wiki.pages || [];
  const wanted = slug(target);
  const byTitle = pages.find(p => slug(p.title) === wanted);
  if (byTitle) return byTitle.path;
  const byStem = pages.find(p => slug(p.name.replace(/\.md$/, '')) === wanted);
  return byStem ? byStem.path : null;
}

function groupedPages() {
  const q = view.query.trim().toLowerCase();
  const pages = (state.wiki.pages || []).filter(p =>
    !q || p.title.toLowerCase().includes(q) || p.path.toLowerCase().includes(q));
  const groups = new Map();
  for (const page of pages) {
    const key = page.folder || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(page);
  }
  return [...groups.entries()].sort((a, b) => {
    if (!a[0]) return 1;   // root pages last, dated folders (newest first) on top
    if (!b[0]) return -1;
    return b[0].localeCompare(a[0]);
  });
}

// ── Tree pane ────────────────────────────────────────────────────────────────

function pageItem(page) {
  const active = page.path === view.selectedPath ? ' active' : '';
  return `
    <button class="tree-item${active}" data-path="${escAttr(page.path)}">
      ${icon('file-text')}
      <span class="tree-item-main">
        <span class="tree-item-title">${escHtml(page.title)}</span>
        <span class="tree-item-sub">${escHtml(relTime(page.modified))} · ${fmtNumber(page.word_count)} words</span>
      </span>
      ${page.backlinks?.length ? `<span class="tree-badge" title="${page.backlinks.length} backlinks">${icon('corner-up-left')}${page.backlinks.length}</span>` : ''}
    </button>`;
}

function renderTree() {
  const el = document.getElementById('wiki-tree');
  if (!el) return;
  const groups = groupedPages();
  if (!(state.wiki.pages || []).length) {
    el.innerHTML = emptyState('sparkles', 'No wiki pages yet',
      'Agents write pages, weekly folders, and backlinks here as they analyze your sources.');
    return;
  }
  if (!groups.length) {
    el.innerHTML = emptyState('search', 'No matches', 'Try a different search.');
    return;
  }
  el.innerHTML = groups.map(([folder, pages]) => `
    <div class="tree-group">
      <div class="tree-group-head">
        ${icon(folder ? 'folder' : 'book-open')}
        <span>${escHtml(folder || 'Pages')}</span>
        <span class="tree-group-count">${pages.length}</span>
      </div>
      ${pages.map(pageItem).join('')}
    </div>`).join('');
}

// ── Reader pane ──────────────────────────────────────────────────────────────

function linkChips(list, label, iconName) {
  if (!list?.length) return '';
  return `
    <div class="link-section">
      <div class="link-section-head">${icon(iconName)}${escHtml(label)}<span class="tree-group-count">${list.length}</span></div>
      <div class="link-chips">
        ${list.map(item => `<button class="link-chip" data-path="${escAttr(item.path)}">${icon('file-text')}${escHtml(item.title)}</button>`).join('')}
      </div>
    </div>`;
}

function renderReader() {
  const el = document.getElementById('wiki-reader');
  if (!el) return;
  if (!view.selectedPath) {
    el.innerHTML = emptyState('book-open', 'Select a page', 'Browse the tree or search to open a wiki page.');
    return;
  }
  if (!view.page) {
    el.innerHTML = `<div class="pane-loading">${spinner()}</div>`;
    return;
  }
  const page = view.page;
  el.innerHTML = `
    <div class="reader-head">
      <div class="reader-meta">
        ${page.folder ? `<span class="tag">${icon('folder')}${escHtml(page.folder)}</span>` : ''}
        <span class="reader-meta-item">${icon('clock')}${escHtml(relTime(page.modified))}</span>
        <span class="reader-meta-item">${icon('hash')}${fmtNumber(page.word_count)} words</span>
      </div>
    </div>
    <article class="markdown" id="wiki-content">${renderMarkdown(page.content, { wikilinks: true })}</article>
    <footer class="reader-footer">
      ${linkChips(page.links, 'Links to', 'arrow-up-right')}
      ${linkChips(page.backlinks, 'Linked from', 'corner-up-left')}
    </footer>`;

  // Wire wikilinks + relative markdown links inside the rendered content.
  el.querySelector('#wiki-content').addEventListener('click', event => {
    const wikilink = event.target.closest('.wikilink');
    if (wikilink) {
      event.preventDefault();
      const target = resolveTarget(wikilink.dataset.wikilink);
      if (target) selectPage(target);
      else toast('info', `No wiki page named “${wikilink.dataset.wikilink}” yet.`);
      return;
    }
    const anchor = event.target.closest('a[href]');
    if (!anchor) return;
    const href = anchor.getAttribute('href') || '';
    event.preventDefault();
    if (/^https?:\/\//i.test(href)) {
      api.openExternal(href).catch(() => {});
      return;
    }
    if (href.endsWith('.md')) {
      const base = view.selectedPath.includes('/')
        ? view.selectedPath.slice(0, view.selectedPath.lastIndexOf('/') + 1) : '';
      const candidate = href.startsWith('../')
        ? href.replace(/^(\.\.\/)+/, '')
        : base + href.replace(/^\.\//, '');
      const pages = state.wiki.pages || [];
      const hit = pages.find(p => p.path === candidate) || pages.find(p => p.path.endsWith(href.replace(/^(\.\.?\/)+/, '')));
      if (hit) selectPage(hit.path);
    }
  });
  el.querySelector('.reader-footer')?.addEventListener('click', event => {
    const chip = event.target.closest('[data-path]');
    if (chip) selectPage(chip.dataset.path);
  });
}

async function selectPage(path) {
  view.selectedPath = path;
  view.page = null;
  renderTree();
  renderReader();
  try {
    const result = await api.wikiPage(path);
    if (result?.ok && view.selectedPath === path) {
      view.page = result.page;
      renderReader();
    } else if (!result?.ok) {
      toast('err', result?.error || 'Could not open wiki page.');
    }
  } catch (error) {
    toast('err', error.message || 'Could not open wiki page.');
  }
}

// ── View module ──────────────────────────────────────────────────────────────

export const wikiView = {
  id: 'wiki',
  title: 'Wiki',
  icon: 'book-open',

  render(root, params = {}) {
    root.innerHTML = `
      <header class="view-header">
        <div>
          <h1>Wiki</h1>
          <p class="view-sub">Durable knowledge, maintained by your agents.</p>
        </div>
        <div class="view-actions">
          <button class="btn btn-ghost" id="wiki-graph-btn">${icon('waypoints')}Graph view</button>
        </div>
      </header>
      <div class="split">
        <aside class="split-list">
          <div class="list-toolbar">
            <div class="input-wrap input-search">
              ${icon('search')}
              <input id="wiki-search" class="input" type="search" placeholder="Search pages…" value="${escAttr(view.query)}" />
            </div>
          </div>
          <div class="scroll-list" id="wiki-tree"></div>
        </aside>
        <section class="split-detail" id="wiki-reader"></section>
      </div>`;

    root.querySelector('#wiki-graph-btn').addEventListener('click', () => navigate('graph'));
    root.querySelector('#wiki-search').addEventListener('input', event => {
      view.query = event.target.value;
      renderTree();
    });
    root.querySelector('#wiki-tree').addEventListener('click', event => {
      const item = event.target.closest('[data-path]');
      if (item && item.dataset.path !== view.selectedPath) selectPage(item.dataset.path);
    });

    renderTree();

    const pages = state.wiki.pages || [];
    const requested = params.path && pages.find(p => p.path === params.path);
    const fallback = view.selectedPath && pages.find(p => p.path === view.selectedPath);
    const initial = requested || fallback || pages[0];
    if (initial) {
      selectPage(initial.path);
    } else {
      view.selectedPath = null;
      view.page = null;
      renderReader();
    }
  },

  update() {
    const root = document.getElementById('view');
    if (!root || root.dataset.view !== 'wiki') return;
    renderTree();
    const pages = state.wiki.pages || [];
    if (view.selectedPath && !pages.some(p => p.path === view.selectedPath)) {
      view.selectedPath = null;
      view.page = null;
      renderReader();
    } else if (view.selectedPath) {
      // Page may have been rewritten by an agent in the background.
      const current = pages.find(p => p.path === view.selectedPath);
      if (current && view.page && current.modified !== view.page.modified) {
        selectPage(view.selectedPath);
      }
    } else if (pages.length) {
      selectPage(pages[0].path);
    }
  },
};
