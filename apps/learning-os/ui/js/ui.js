/**
 * Shared UI primitives: formatting, toasts, badges, empty states, markdown.
 * Views compose these instead of re-implementing them.
 */
'use strict';

import { icon } from './icons.js';

export function escHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function escAttr(value) {
  return escHtml(value);
}

export function fmtDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
    + ' · ' + date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function relTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const diff = Date.now() - date.getTime();
  const minute = 60000, hour = 60 * minute, day = 24 * hour;
  if (diff < minute) return 'just now';
  if (diff < hour) return `${Math.floor(diff / minute)}m ago`;
  if (diff < day) return `${Math.floor(diff / hour)}h ago`;
  if (diff < 7 * day) return `${Math.floor(diff / day)}d ago`;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function fmtBytes(bytes) {
  const size = Number(bytes || 0);
  if (!size) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = size;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  return `${value >= 10 || idx === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[idx]}`;
}

export function fmtNumber(value) {
  return Number(value || 0).toLocaleString();
}

// ── Toasts ───────────────────────────────────────────────────────────────────

const TOAST_ICONS = { ok: 'circle-check', err: 'circle-alert', info: 'info' };

export function toast(kind, message, ms = 3600) {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;
  const node = document.createElement('div');
  node.className = `toast toast-${kind}`;
  node.innerHTML = `${icon(TOAST_ICONS[kind] || 'info')}<span>${escHtml(message)}</span>`;
  stack.appendChild(node);
  requestAnimationFrame(() => node.classList.add('toast-in'));
  setTimeout(() => {
    node.classList.remove('toast-in');
    setTimeout(() => node.remove(), 220);
  }, ms);
}

// ── Small components ─────────────────────────────────────────────────────────

const STATUS_META = {
  processed: { cls: 'ok', icon: 'circle-check', label: 'Processed' },
  imported: { cls: 'info', icon: 'clock', label: 'Imported' },
  error: { cls: 'err', icon: 'circle-alert', label: 'Error' },
};

export function statusBadge(status) {
  const meta = STATUS_META[status] || { cls: 'muted', icon: 'info', label: status || 'unknown' };
  return `<span class="badge badge-${meta.cls}">${icon(meta.icon)}${escHtml(meta.label)}</span>`;
}

export function tag(text, cls = '') {
  return `<span class="tag${cls ? ` ${cls}` : ''}">${escHtml(text)}</span>`;
}

export function emptyState(iconName, title, sub = '', actionsHtml = '') {
  return `
    <div class="empty">
      <div class="empty-icon">${icon(iconName)}</div>
      <div class="empty-title">${escHtml(title)}</div>
      ${sub ? `<div class="empty-sub">${escHtml(sub)}</div>` : ''}
      ${actionsHtml ? `<div class="empty-actions">${actionsHtml}</div>` : ''}
    </div>`;
}

export function spinner(cls = '') {
  return icon('loader', `spin${cls ? ` ${cls}` : ''}`);
}

// ── Markdown ─────────────────────────────────────────────────────────────────

const WIKILINK_RE = /\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]/g;

/**
 * Render markdown to sanitized HTML. `[[wikilinks]]` become
 * <a class="wikilink" data-wikilink="Target"> anchors that views can wire up.
 */
export function renderMarkdown(markdown, { wikilinks = false } = {}) {
  let source = String(markdown ?? '');
  if (wikilinks) {
    source = source.replace(WIKILINK_RE, (_, target, label) =>
      `<a class="wikilink" data-wikilink="${escAttr(target.trim())}">${escHtml((label || target).trim())}</a>`);
  }
  if (typeof marked === 'undefined') {
    return `<pre class="md-fallback">${escHtml(source)}</pre>`;
  }
  return sanitizeHtml(marked.parse(source, { breaks: true, gfm: true }));
}

// ── Confirm dialog ───────────────────────────────────────────────────────────

/**
 * A themed yes/no dialog, built and torn down entirely in JS (reuses the
 * .modal-backdrop/.modal styling already defined for the import modal, so no
 * markup needs to live in index.html for every future confirmation).
 *
 * `bodyHtml` may contain markup (e.g. lists of what will be affected) — the
 * caller is responsible for escaping any untrusted text it interpolates.
 * Resolves true if confirmed, false if cancelled/dismissed.
 */
export function confirmDialog({ title, bodyHtml, confirmLabel = 'Confirm', danger = false }) {
  return new Promise(resolve => {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.innerHTML = `
      <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
        <div class="modal-head">
          <h2 id="confirm-title">${escHtml(title)}</h2>
        </div>
        <div class="confirm-body">${bodyHtml}</div>
        <div class="confirm-actions">
          <button class="btn btn-ghost" data-action="cancel">Cancel</button>
          <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-action="confirm">${escHtml(confirmLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    requestAnimationFrame(() => backdrop.classList.add('open'));

    const finish = (result) => {
      backdrop.classList.remove('open');
      document.removeEventListener('keydown', onKey);
      setTimeout(() => backdrop.remove(), 180);
      resolve(result);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') finish(false);
    };
    backdrop.addEventListener('click', event => {
      if (event.target === backdrop) finish(false);
    });
    backdrop.querySelector('[data-action="cancel"]').addEventListener('click', () => finish(false));
    backdrop.querySelector('[data-action="confirm"]').addEventListener('click', () => finish(true));
    document.addEventListener('keydown', onKey);
    backdrop.querySelector('[data-action="confirm"]').focus();
  });
}

/** Strip active content from rendered markdown (scripts, event handlers, js: URLs). */
function sanitizeHtml(html) {
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll('script, iframe, object, embed, style, link, meta').forEach(el => el.remove());
  template.content.querySelectorAll('*').forEach(el => {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on')) el.removeAttribute(attr.name);
      if ((name === 'href' || name === 'src') && /^\s*javascript:/i.test(attr.value)) {
        el.removeAttribute(attr.name);
      }
    }
  });
  return template.innerHTML;
}
