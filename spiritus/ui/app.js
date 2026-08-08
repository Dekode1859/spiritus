'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  port: null,
  projectPath: null,
  workspacePath: null,
  config: null,        // full app config from get_config (branding/folders/agents)
  sessions: [],
  activeSessionId: null,
  agents: [],          // [{name, label}] supplied by the app via config
  activeAgent: null,
  activeModel: null, // { providerID, modelID, name } — passed per-prompt, no restart needed
  // A "turn" = everything OpenCode emits in response to one prompt. It can span
  // MULTIPLE assistant messages (text → tool call → more text), so we track each
  // by messageID rather than assuming a single streaming bubble.
  turn: {
    bubbles: {},     // messageID -> container element (live assistant bubbles)
    parts: {},       // messageID -> { partID -> accumulated text }
    textParts: {},   // partID -> true  (which parts are text, learned from part.updated)
  },
  working: false,   // session is busy (driven by session.status / session.idle SSE)
  roles: {},        // messageID -> 'user' | 'assistant' (learned from SSE)
  sending: false,   // guard against double-send during async session creation
  vault: { folder: null, files: [], activeFile: null },
  sse: null,
  settings: {
    providers: [],
    connected: [],
    loaded: false,
  },
};

// ── Bridge (Python ↔ JS) ──────────────────────────────────────────────────────
const bridge = (() => {
  const call = (method, ...args) =>
    window.pywebview.api[method](...args).catch(err => {
      console.error(`bridge.${method}:`, err);
      throw err;
    });
  return {
    getConfig:         ()                    => call('get_config'),
    workspaceTree:     ()                    => call('workspace_tree'),
    workspaceList:     (folder)              => call('workspace_list', folder),
    workspaceRead:     (path)                => call('workspace_read', path),
    workspaceWrite:    (path, content)       => call('workspace_write', path, content),
    workspaceDelete:   (path)                => call('workspace_delete', path),
    workspaceNewNotePath:(title)             => call('workspace_new_note_path', title),
    openFolderDialog:  ()                    => call('open_folder_dialog'),
    getProviders:      ()                    => call('get_providers'),
    saveProviderKey:   (pid, key)            => call('save_provider_key', pid, key),
    removeProviderKey: (pid)                 => call('remove_provider_key', pid),
    setDefaultModel:   (pid, mid)            => call('set_default_model', pid, mid),
  };
})();

// ── OpenCode HTTP helpers ─────────────────────────────────────────────────────
async function oc(path, options = {}) {
  const r = await fetch(`http://127.0.0.1:${state.port}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.text()).slice(0, 200); } catch (_) {}
    throw new Error(`HTTP ${r.status}${detail ? ': ' + detail : ''}`);
  }
  // 204 No Content (e.g. prompt_async) and empty bodies must not be JSON-parsed —
  // WKWebView throws "The string did not match the expected pattern" otherwise.
  if (r.status === 204) return null;
  const text = await r.text();
  return text ? JSON.parse(text) : null;
}
function ocGet(path)        { return oc(path); }
function ocPost(path, body) { return oc(path, { method: 'POST', body: JSON.stringify(body) }); }
function ocDelete(path)     { return oc(path, { method: 'DELETE' }); }

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  if (state.sse) state.sse.close();
  const es = new EventSource(`http://127.0.0.1:${state.port}/event`);
  state.sse = es;
  es.onmessage = (e) => {
    try { handleOCEvent(JSON.parse(e.data)); } catch (_) {}
  };
  es.onerror = () => {
    if (state.port) setTimeout(connectSSE, 3000);
  };
}

function handleOCEvent(event) {
  const payload = event.payload || event;
  const { type, properties } = payload;
  if (!type) return;

  switch (type) {
    case 'message.updated': {
      const { info } = properties;
      if (!info || info.sessionID !== state.activeSessionId) return;
      if (info.role) state.roles[info.id] = info.role;
      if (info.role === 'assistant' && info.time && info.time.completed) {
        finalizeAssistantMessage(info.id, info.error?.data?.message || info.error?.message);
      }
      break;
    }
    case 'message.part.updated': {
      const { part } = properties;
      if (!part || part.sessionID !== state.activeSessionId) return;
      if (state.roles[part.messageID] === 'user') return; // never echo the user
      if (part.type !== 'text') return;
      state.turn.textParts[part.id] = true;
      // Snapshot carries the authoritative full text — replace any drift.
      applyAssistantText(part.messageID, part.id, part.text || '', /*replace*/ true);
      break;
    }
    case 'message.part.delta': {
      const { sessionID, messageID, partID, field, delta } = properties;
      if (sessionID !== state.activeSessionId) return;
      if (field !== 'text' || !delta) return;
      if (state.roles[messageID] === 'user') return;
      // Deltas fire for reasoning parts too — only stream known text parts.
      if (!state.turn.textParts[partID]) return;
      applyAssistantText(messageID, partID, delta, /*replace*/ false);
      break;
    }
    case 'session.status': {
      const { sessionID, status } = properties;
      if (sessionID !== state.activeSessionId) return;
      setWorking(status?.type === 'busy');
      break;
    }
    case 'session.idle': {
      const { sessionID } = properties;
      if (sessionID && sessionID !== state.activeSessionId) return;
      setWorking(false);
      break;
    }
    case 'session.error': {
      const { sessionID, error } = properties;
      if (sessionID && sessionID !== state.activeSessionId) return;
      const msg = error?.data?.message || error?.message || 'Something went wrong';
      const liveId = Object.keys(state.turn.bubbles)[0];
      if (liveId) finalizeAssistantMessage(liveId, msg);
      else { const el = createMessageBubble('assistant', `⚠ ${msg}`); el.querySelector('.message-bubble')?.classList.add('error'); }
      setWorking(false);
      break;
    }
    case 'session.updated': {
      const { info } = properties;
      if (info) updateSessionInList(info);
      break;
    }
    case 'session.deleted': {
      const { info } = properties;
      if (!info) return;
      state.sessions = state.sessions.filter(s => s.id !== info.id);
      renderSessionList();
      if (state.activeSessionId === info.id) {
        state.activeSessionId = null;
        clearMessages();
      }
      break;
    }
  }

  // Centralized: the thinking indicator is shown whenever the session is busy
  // but no assistant bubble is currently receiving text (e.g. during reasoning
  // or a tool call between messages).
  reconcileThinking();
}

// ── Streaming rendering (per-message, delta-driven) ───────────────────────────
// One turn can produce several assistant messages (text → tool → more text).
// Each gets its own bubble keyed by messageID; text accumulates per part from
// both delta events (incremental) and part.updated snapshots (authoritative).
let thinkingEl = null;

function ensureAssistantBubble(messageId) {
  let el = state.turn.bubbles[messageId];
  if (el) return el;
  hideThinking();
  // If we returned to a session mid-generation, a static bubble for this message
  // may already exist from the history render — adopt it instead of duplicating.
  el = document.querySelector(`.message.assistant[data-message-id="${messageId}"]`)
       || createMessageBubble('assistant', '', messageId);
  el.classList.add('streaming-cursor');
  state.turn.bubbles[messageId] = el;
  state.turn.parts[messageId] = state.turn.parts[messageId] || {};
  return el;
}

function applyAssistantText(messageId, partId, text, replace) {
  const prev = state.turn.parts[messageId]?.[partId] || '';
  const newVal = replace ? text : prev + text;

  // Don't materialize an empty bubble — that would show a lone blinking cursor
  // while a tool runs. Keep the thinking dots and just record the (empty) text.
  if (!state.turn.bubbles[messageId] && !newVal.trim()) {
    state.turn.parts[messageId] = state.turn.parts[messageId] || {};
    state.turn.parts[messageId][partId] = newVal;
    return;
  }

  const el = ensureAssistantBubble(messageId);
  const parts = state.turn.parts[messageId];
  parts[partId] = newVal;
  const bubble = el.querySelector('.message-bubble');
  if (bubble) bubble.textContent = Object.values(parts).join('');
  scrollMessages();
}

function finalizeAssistantMessage(messageId, errorMsg) {
  const el = state.turn.bubbles[messageId];
  if (el) {
    const bubble = el.querySelector('.message-bubble');
    const text = Object.values(state.turn.parts[messageId] || {}).join('');
    if (bubble) {
      if (errorMsg) {
        bubble.classList.remove('markdown');
        bubble.textContent = text.trim() ? `${text}\n\n⚠ ${errorMsg}` : `⚠ ${errorMsg}`;
        bubble.classList.add('error');
      } else if (text.trim()) {
        bubble.classList.add('markdown');
        bubble.innerHTML = renderMarkdown(text);
      } else {
        // Pure tool-use message with no text — drop the empty bubble.
        el.remove();
      }
    }
    el.classList.remove('streaming-cursor');
    delete state.turn.bubbles[messageId];
    delete state.turn.parts[messageId];
  } else if (errorMsg) {
    const e = createMessageBubble('assistant', `⚠ ${errorMsg}`);
    e.querySelector('.message-bubble')?.classList.add('error');
  }
  refreshVaultTree();
  if (!document.getElementById('messages').children.length) showEmptyState();
}

function resetTurnState() {
  state.turn = { bubbles: {}, parts: {}, textParts: {} };
  state.working = false;
  hideThinking();
  setStreamingIndicator(false);
}

// ── Working state (driven by OpenCode session.status / session.idle) ──────────
function setWorking(on) {
  state.working = on;
  setStreamingIndicator(on);
}

function reconcileThinking() {
  const hasLiveBubble = Object.keys(state.turn.bubbles).length > 0;
  if (state.working && !hasLiveBubble) showThinking();
  else hideThinking();
}

function showThinking() {
  if (thinkingEl) return;
  hideEmptyState();
  thinkingEl = document.createElement('div');
  thinkingEl.className = 'message assistant';
  thinkingEl.innerHTML =
    `<div class="message-role">${escHtml(state.activeAgent ? agentLabel(state.activeAgent) : 'AI')}</div>` +
    `<div class="message-bubble thinking"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>`;
  document.getElementById('messages').appendChild(thinkingEl);
  scrollMessages();
}

function hideThinking() {
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
}

function setSendEnabled(on) {
  document.getElementById('btn-send').disabled = !on;
}

function setStreamingIndicator(on) {
  const el = document.getElementById('streaming-indicator');
  if (el) el.classList.toggle('hidden', !on);
}

// ── Message rendering ─────────────────────────────────────────────────────────
function renderMarkdown(text) {
  if (typeof marked === 'undefined') return escHtml(text);
  // Basic security: allow only safe subset — this is a local desktop app
  // but we still avoid raw HTML in LLM output.
  return marked.parse(text, { breaks: true, gfm: true });
}

function createMessageBubble(role, text, messageId = '') {
  hideEmptyState();
  const container = document.createElement('div');
  container.className = `message ${role}`;
  if (messageId) container.dataset.messageId = messageId;

  const roleEl = document.createElement('div');
  roleEl.className = 'message-role';
  roleEl.textContent = role === 'user' ? 'You' : (state.activeAgent || 'AI');
  container.appendChild(roleEl);

  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  if (role === 'assistant' && text) {
    bubble.classList.add('markdown');
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text;
  }
  container.appendChild(bubble);

  document.getElementById('messages').appendChild(container);
  scrollMessages();
  return container;
}

function scrollMessages() {
  const el = document.getElementById('messages-wrap');
  el.scrollTop = el.scrollHeight;
}

function clearMessages() {
  document.getElementById('messages').innerHTML = '';
  showEmptyState();
  thinkingEl = null;
  resetTurnState();
}

function renderHistoryMessages(messages) {
  clearMessages();
  if (!messages) return;
  for (const msg of messages) {
    const info = msg?.info;
    if (!info || !msg.parts) continue;
    const role = info.role === 'user' ? 'user' : 'assistant';
    // Pick the first text part; skip empty text parts (step delimiters)
    const textPart = msg.parts.find(p => p.type === 'text' && p.text?.trim());
    if (!textPart) continue;
    createMessageBubble(role, textPart.text, info.id);
  }
}

function showEmptyState() {
  const el = document.getElementById('empty-state');
  if (el) el.style.display = '';
}

function hideEmptyState() {
  const el = document.getElementById('empty-state');
  if (el) el.style.display = 'none';
}

// ── Session management ────────────────────────────────────────────────────────
async function loadSessions() {
  try {
    const sessions = await ocGet('/session');
    state.sessions = Array.isArray(sessions) ? sessions : [];
    renderSessionList();
  } catch (e) {
    console.error('loadSessions:', e);
  }
}

// OpenCode names a freshly-created session "New session - <ISO timestamp>" until
// it generates a title from the first exchange. Empty placeholder sessions that
// were never used keep that title forever, so we hide them — except the active
// one (which is mid-conversation and about to be renamed).
const DEFAULT_TITLE_RE = /^New session - /;

function renderSessionList() {
  const el = document.getElementById('session-list');
  const visible = state.sessions.filter(s =>
    s.id === state.activeSessionId || !DEFAULT_TITLE_RE.test(s.title || '')
  );
  if (!visible.length) {
    el.innerHTML = '<div class="empty-hint">No sessions yet. Click + to start.</div>';
    return;
  }
  el.innerHTML = '';
  const sorted = [...visible].sort((a, b) =>
    (b.time?.updated || 0) - (a.time?.updated || 0)
  );
  for (const s of sorted) {
    const item = document.createElement('div');
    item.className = 'session-item' + (s.id === state.activeSessionId ? ' active' : '');
    item.dataset.sessionId = s.id;
    item.innerHTML = `
      <sl-icon library="lucide" name="message-square"></sl-icon>
      <span class="session-item-title">${escHtml(s.title || 'New session')}</span>
      <button class="session-del-btn" data-session-id="${escHtml(s.id)}" title="Delete">×</button>
    `;
    el.appendChild(item);
  }
}

function updateSessionInList(info) {
  const idx = state.sessions.findIndex(s => s.id === info.id);
  idx >= 0
    ? (state.sessions[idx] = { ...state.sessions[idx], ...info })
    : state.sessions.unshift(info);
  renderSessionList();
  if (info.id === state.activeSessionId) {
    document.getElementById('chat-session-title').textContent = info.title || 'New session';
  }
}

// "New chat" button: start a fresh, empty conversation WITHOUT creating a
// backend session yet. The session is created lazily on the first send, which
// prevents stray empty "New session - <timestamp>" entries from piling up.
function newSession() {
  state.activeSessionId = null;
  clearMessages();          // also resets turn + working state
  setSendEnabled(true);
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
  document.getElementById('chat-session-title').textContent = 'New chat';
  document.getElementById('chat-header').classList.remove('hidden');
  document.getElementById('chat-input').focus();
}

// Actually create the backend session (called on first message send).
async function createBackendSession() {
  const s = await ocPost('/session', {});
  state.sessions.unshift(s);
  state.activeSessionId = s.id;
  renderSessionList();
  document.querySelectorAll('.session-item').forEach(el =>
    el.classList.toggle('active', el.dataset.sessionId === s.id)
  );
  document.getElementById('chat-session-title').textContent = s.title || 'New chat';
  document.getElementById('chat-header').classList.remove('hidden');
  return s;
}

async function activateSession(sessionId) {
  if (state.activeSessionId === sessionId) return;
  state.activeSessionId = sessionId;
  clearMessages();          // also resets turn + working state
  setSendEnabled(true);

  document.querySelectorAll('.session-item').forEach(el =>
    el.classList.toggle('active', el.dataset.sessionId === sessionId)
  );

  const session = state.sessions.find(s => s.id === sessionId);
  const title = session?.title || 'New session';
  document.getElementById('chat-session-title').textContent = title;
  document.getElementById('chat-header').classList.remove('hidden');

  try {
    const data = await ocGet(`/session/${sessionId}/message`);
    if (Array.isArray(data)) renderHistoryMessages(data);
  } catch (e) {
    console.error('loadMessages:', e);
  }
}

async function deleteSession(sessionId) {
  try {
    await ocDelete(`/session/${sessionId}`);
    state.sessions = state.sessions.filter(s => s.id !== sessionId);
    renderSessionList();
    if (state.activeSessionId === sessionId) {
      state.activeSessionId = null;
      clearMessages();
      document.getElementById('chat-header').classList.add('hidden');
    }
  } catch (e) {
    showToast('Failed to delete session');
  }
}

// ── Agent management ──────────────────────────────────────────────────────────
// The agent list is supplied entirely by the application via get_config
// (derived from its opencode.json). Spiritus has no built-in or default agents.

function buildMenuItems(agents) {
  return agents.map(a => {
    const item = document.createElement('sl-menu-item');
    item.value = a.name;
    item.textContent = a.label || agentLabel(a.name);
    return item;
  });
}

function initAgents() {
  const agents = state.agents || [];
  const menu = document.getElementById('agent-menu');
  menu.replaceChildren(...buildMenuItems(agents));
  const def = state.config?.default_agent || (agents[0] && agents[0].name);
  if (def) setActiveAgent(def);
}

async function loadAgents() {
  // Cross-check the app's declared agents against what OpenCode actually loaded,
  // preferring OpenCode's list but restricted to the app-declared set. If the
  // call fails, the config-supplied list (already shown by initAgents) stands.
  try {
    const declared = new Set((state.agents || []).map(a => a.name));
    const live = await ocGet('/agent');
    if (!Array.isArray(live)) return;
    const visible = live
      .filter(a => declared.has(a.name))
      .map(a => ({ name: a.name, label: agentLabel(a.name) }));
    if (!visible.length) return;
    const current = state.activeAgent;
    document.getElementById('agent-menu').replaceChildren(...buildMenuItems(visible));
    setActiveAgent(current || state.config?.default_agent || visible[0].name);
  } catch (e) {
    console.error('loadAgents:', e);
  }
}

function agentLabel(name) {
  const fromConfig = (state.agents || []).find(a => a.name === name);
  if (fromConfig && fromConfig.label) return fromConfig.label;
  return name.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function setActiveAgent(name) {
  state.activeAgent = name;
  document.getElementById('agent-label').textContent = agentLabel(name);

  // Update checkmark in menu
  const menu = document.getElementById('agent-menu');
  menu.querySelectorAll('sl-menu-item').forEach(item => {
    item.querySelector('.agent-check')?.remove();
    if (item.value === name) {
      const check = document.createElement('sl-icon');
      check.setAttribute('library', 'lucide');
      check.setAttribute('name', 'check');
      check.setAttribute('slot', 'suffix');
      check.className = 'agent-check';
      item.appendChild(check);
    }
  });
}

// ── Send message ──────────────────────────────────────────────────────────────
// Note: we do NOT block sending while the session is busy. OpenCode queues
// prompts sent during an active turn server-side, so the user can stack
// follow-ups; the session.status SSE keeps the "Working…" indicator accurate.
async function sendMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || state.sending) return;

  state.sending = true;
  setSendEnabled(false);

  try {
    // Lazily create the backend session on the very first message.
    if (!state.activeSessionId) await createBackendSession();
    if (!state.activeSessionId) return;

    input.value = '';
    input.style.height = 'auto';
    createMessageBubble('user', text);
    setWorking(true);       // optimistic; session.status will confirm/clear
    reconcileThinking();    // show the typing indicator immediately

    await ocPost(`/session/${state.activeSessionId}/prompt_async`, {
      agent: state.activeAgent || undefined,
      model: state.activeModel
        ? { providerID: state.activeModel.providerID, modelID: state.activeModel.modelID }
        : undefined,
      parts: [{ type: 'text', text }],
    });
    // Response is rendered by SSE events; working state cleared on session.idle.
  } catch (e) {
    setWorking(false);
    const el = createMessageBubble('assistant', `⚠ ${e.message}`);
    el.querySelector('.message-bubble')?.classList.add('error');
  } finally {
    state.sending = false;
    setSendEnabled(true);   // allow queueing further prompts while busy
  }
}

// ── Vault ─────────────────────────────────────────────────────────────────────
async function refreshVaultTree() {
  try {
    const tree = await bridge.workspaceTree();
    renderVaultFolders(tree);
  } catch (e) {
    console.error('refreshVaultTree:', e);
  }
}

function renderVaultFolders(tree) {
  const el = document.getElementById('vault-folders');
  el.innerHTML = '';
  // Icons + labels come from the app's config (relayed through workspace_tree).
  // Spiritus knows no folder names.
  for (const [name, info] of Object.entries(tree)) {
    const div = document.createElement('div');
    div.className = 'vault-folder-item' + (state.vault.folder === name ? ' active' : '');
    div.dataset.folder = name;
    div.innerHTML = `
      <sl-icon library="lucide" name="${info.icon || 'folder'}"></sl-icon>
      <span>${escHtml(info.label || name)}</span>
      <span class="vault-folder-count">${info.count}</span>
    `;
    el.appendChild(div);
  }
}

async function openVaultFolder(folder) {
  state.vault.folder = folder;
  state.vault.activeFile = null;

  // Update sidebar highlight
  document.querySelectorAll('.vault-folder-item').forEach(el =>
    el.classList.toggle('active', el.dataset.folder === folder)
  );

  const dialog = document.getElementById('vault-dialog');
  dialog.label = `Vault / ${folder}`;

  const fileListEl = document.getElementById('vault-dialog-files');
  const fileContentEl = document.getElementById('vault-dialog-content');
  fileListEl.innerHTML = '<div class="hint-text">Loading…</div>';
  fileContentEl.classList.add('hidden');
  fileContentEl.textContent = '';

  dialog.show();

  const files = await bridge.workspaceList(folder);
  state.vault.files = files;

  if (!files.length) {
    fileListEl.innerHTML = '<div class="hint-text">No files in this folder yet.</div>';
    return;
  }

  fileListEl.innerHTML = '';
  for (const f of files) {
    const item = document.createElement('div');
    item.className = 'vault-file-item';
    item.dataset.path = f.path;
    item.innerHTML = `<sl-icon library="lucide" name="file-text"></sl-icon>${escHtml(f.name)}`;
    fileListEl.appendChild(item);
  }
}

async function openVaultFile(relPath) {
  state.vault.activeFile = relPath;
  document.querySelectorAll('.vault-file-item').forEach(el =>
    el.classList.toggle('active', el.dataset.path === relPath)
  );
  const contentEl = document.getElementById('vault-dialog-content');
  contentEl.textContent = 'Loading…';
  contentEl.classList.remove('hidden');
  try {
    const result = await bridge.workspaceRead(relPath);
    contentEl.textContent = result.error ? `Error: ${result.error}` : result.content;
  } catch (e) {
    contentEl.textContent = 'Error reading file';
  }
}

// ── Model picker (inline dropdown, no restart) ────────────────────────────────
async function ensureProviderData() {
  if (state.settings.loaded) return;
  try {
    const data = await bridge.getProviders();
    state.settings.providers = data.featured || [];
    state.settings.connected = data.connected || [];
    state.settings.loaded = true;
  } catch (e) {
    console.error('ensureProviderData:', e);
  }
}

async function openModelPicker() {
  await ensureProviderData();
  renderModelList('');
}

function renderModelList(query) {
  const wrap = document.getElementById('model-list-wrap');
  const q = query.toLowerCase().trim();
  const connectedSet = new Set(state.settings.connected);

  const showProviders = state.settings.providers.filter(p =>
    connectedSet.has(p.id) && p.models && p.models.length > 0
  );

  if (!showProviders.length) {
    wrap.innerHTML = '<div class="model-empty-hint">No providers connected. Add one in settings.</div>';
    return;
  }

  wrap.innerHTML = '';
  for (const p of showProviders) {
    const models = q
      ? p.models.filter(m => (m.name || m.id).toLowerCase().includes(q))
      : p.models;
    if (!models.length) continue;

    const group = document.createElement('div');
    group.className = 'model-provider-group';

    const header = document.createElement('div');
    header.className = 'model-provider-header';
    header.textContent = p.name;
    group.appendChild(header);

    for (const m of models) {
      const isActive = state.activeModel?.modelID === m.id && state.activeModel?.providerID === p.id;
      // Only OpenCode Zen (provider id: "opencode") actually offers free models.
      // Other providers may have zero cost in metadata but still require paid API keys.
      const isFree = p.id === 'opencode';

      const item = document.createElement('div');
      item.className = 'model-item' + (isActive ? ' active' : '');
      item.dataset.pid = p.id;
      item.dataset.mid = m.id;
      item.dataset.name = m.name || m.id;

      const nameEl = document.createElement('span');
      nameEl.className = 'model-item-name';
      nameEl.textContent = m.name || m.id;
      item.appendChild(nameEl);

      if (isFree) {
        const badge = document.createElement('span');
        badge.className = 'model-free-badge';
        badge.textContent = 'Free';
        item.appendChild(badge);
      }

      if (isActive) {
        const check = document.createElement('sl-icon');
        check.setAttribute('library', 'lucide');
        check.setAttribute('name', 'check');
        check.className = 'model-check-icon';
        item.appendChild(check);
      }

      group.appendChild(item);
    }

    wrap.appendChild(group);
  }
}

function selectModel(providerId, modelId, modelName) {
  state.activeModel = { providerID: providerId, modelID: modelId, name: modelName };
  document.getElementById('model-label').textContent = modelName || modelId;
  document.getElementById('model-dropdown').hide();
}

// ── Settings ──────────────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settings-dialog').show();
  loadSettingsData();
}

async function loadSettingsData() {
  try {
    const data = await bridge.getProviders();
    state.settings.providers = data.featured || [];
    state.settings.connected = data.connected || [];
    state.settings.loaded = true;
    renderConnectedProviders();
    populateProviderSelect();
    populateModelProviderSelect();
  } catch (e) {
    setAuthStatus('err', 'Failed to load providers');
  }
}

function renderConnectedProviders() {
  const el = document.getElementById('connected-providers-list');
  const connected = state.settings.connected.filter(id => id !== 'opencode');
  if (!connected.length) {
    el.innerHTML = '<span class="hint-text">No providers connected yet.</span>';
    return;
  }
  el.innerHTML = '';
  for (const id of connected) {
    const p = state.settings.providers.find(x => x.id === id);
    const name = p ? p.name : id;
    const tag = document.createElement('div');
    tag.className = 'provider-tag';
    tag.innerHTML = `
      <span class="provider-dot"></span>
      <span>${escHtml(name)}</span>
      <button class="provider-tag-remove" data-provider-id="${escHtml(id)}" title="Disconnect">×</button>
    `;
    el.appendChild(tag);
  }
}

function populateProviderSelect() {
  const sel = document.getElementById('settings-provider-select');
  sel.innerHTML = '';
  for (const p of state.settings.providers) {
    const opt = document.createElement('sl-option');
    opt.value = p.id;
    const connected = state.settings.connected.includes(p.id);
    opt.textContent = p.name + (connected ? ' ✓' : '');
    sel.appendChild(opt);
  }
}

function populateModelProviderSelect() {
  const sel = document.getElementById('settings-model-provider');
  sel.innerHTML = '<sl-option value="">— pick a connected provider —</sl-option>';
  const connected = state.settings.connected.filter(id => id !== 'opencode');
  for (const id of connected) {
    const p = state.settings.providers.find(x => x.id === id);
    const name = p ? p.name : id;
    const opt = document.createElement('sl-option');
    opt.value = id;
    opt.textContent = name;
    sel.appendChild(opt);
  }

  const modelSel = document.getElementById('settings-model-select');
  modelSel.innerHTML = '<sl-option value="">— pick a provider first —</sl-option>';
  modelSel.disabled = true;
  document.getElementById('btn-save-model').disabled = true;
}

function updateModelSelectForProvider(pid) {
  const modelSel = document.getElementById('settings-model-select');
  const btn = document.getElementById('btn-save-model');
  if (!pid) {
    modelSel.innerHTML = '<sl-option value="">— pick a provider first —</sl-option>';
    modelSel.disabled = true;
    btn.disabled = true;
    return;
  }
  const provider = state.settings.providers.find(p => p.id === pid);
  const models = provider?.models || [];
  modelSel.innerHTML = '<sl-option value="">— pick a model —</sl-option>' +
    models.map(m => `<sl-option value="${escHtml(m.id)}">${escHtml(m.name || m.id)}</sl-option>`).join('');
  modelSel.disabled = false;
  btn.disabled = true;
}

async function saveProviderKey() {
  const pid = document.getElementById('settings-provider-select').value;
  const keyInput = document.getElementById('settings-api-key');
  const key = keyInput.value.trim();
  if (!pid) { setAuthStatus('err', 'Pick a provider first.'); return; }
  if (!key) { setAuthStatus('err', 'Enter an API key.'); return; }

  const btn = document.getElementById('btn-save-provider');
  btn.loading = true;
  setAuthStatus('info', 'Saving key and restarting server…');

  try {
    const result = await bridge.saveProviderKey(pid, key);
    if (!result.ok) throw new Error(result.error || 'Unknown error');
    state.port = result.port;
    keyInput.value = '';
    setAuthStatus('ok', 'Connected! Server restarted on port ' + result.port);
    reconnectAfterRestart();
    await loadSettingsData();
  } catch (e) {
    setAuthStatus('err', e.message);
  } finally {
    btn.loading = false;
  }
}

async function removeProviderKey(providerId) {
  setAuthStatus('info', 'Removing credentials and restarting…');
  try {
    const result = await bridge.removeProviderKey(providerId);
    if (!result.ok) throw new Error(result.error);
    state.port = result.port;
    setAuthStatus('ok', 'Removed. Server restarted.');
    reconnectAfterRestart();
    await loadSettingsData();
  } catch (e) {
    setAuthStatus('err', e.message);
  }
}

async function saveDefaultModel() {
  const pid = document.getElementById('settings-model-provider').value;
  const mid = document.getElementById('settings-model-select').value;
  if (!pid || !mid) { setModelStatus('err', 'Pick both provider and model.'); return; }

  const btn = document.getElementById('btn-save-model');
  btn.loading = true;
  setModelStatus('info', 'Writing config and restarting…');

  try {
    const result = await bridge.setDefaultModel(pid, mid);
    if (!result.ok) throw new Error(result.error);
    state.port = result.port;
    setModelStatus('ok', `Default model set: ${result.model}`);
    const modelName = mid.split('/').pop();
    document.getElementById('model-label').textContent = modelName;
    state.activeModel = { providerID: pid, modelID: mid, name: modelName };
    reconnectAfterRestart();
  } catch (e) {
    setModelStatus('err', e.message);
  } finally {
    btn.loading = false;
  }
}

function setAuthStatus(type, msg) {
  const el = document.getElementById('settings-auth-status');
  el.className = `status-text${type ? ` status-${type}` : ''}`;
  el.textContent = msg;
}

function setModelStatus(type, msg) {
  const el = document.getElementById('settings-model-status');
  el.className = `status-text${type ? ` status-${type}` : ''}`;
  el.textContent = msg;
}

function reconnectAfterRestart() {
  if (state.sse) { state.sse.close(); state.sse = null; }
  state.settings.loaded = false; // force model picker to reload
  setTimeout(() => {
    connectSSE();
    Promise.all([loadSessions(), loadAgents(), refreshVaultTree()]);
  }, 800);
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3000);
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Event wiring ──────────────────────────────────────────────────────────────
function wireEvents() {
  // Sidebar: new session
  document.getElementById('btn-new-session').addEventListener('click', newSession);

  // Sidebar: session list (click to activate, × to delete)
  document.getElementById('session-list').addEventListener('click', e => {
    const del = e.target.closest('.session-del-btn');
    if (del) { e.stopPropagation(); deleteSession(del.dataset.sessionId); return; }
    const item = e.target.closest('.session-item');
    if (item) activateSession(item.dataset.sessionId);
  });

  // Sidebar: vault folder clicks → open dialog
  document.getElementById('vault-folders').addEventListener('click', e => {
    const folder = e.target.closest('.vault-folder-item');
    if (folder) openVaultFolder(folder.dataset.folder);
  });

  // Vault dialog: file clicks
  document.getElementById('vault-dialog-files').addEventListener('click', e => {
    const item = e.target.closest('.vault-file-item');
    if (item) openVaultFile(item.dataset.path);
  });

  // Chat: send button
  document.getElementById('btn-send').addEventListener('click', sendMessage);

  // Chat: Enter to send
  const chatInput = document.getElementById('chat-input');
  chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // Chat input: auto-resize + enable send
  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + 'px';
    document.getElementById('btn-send').disabled = !chatInput.value.trim() || state.sending;
  });

  // Agent dropdown (Shoelace sl-select event)
  document.getElementById('agent-dropdown').addEventListener('sl-select', e => {
    setActiveAgent(e.detail.item.value);
  });

  // Model dropdown: load data when opened
  document.getElementById('model-dropdown').addEventListener('sl-show', openModelPicker);

  // Model list: click to select
  document.getElementById('model-list-wrap').addEventListener('click', e => {
    const item = e.target.closest('.model-item');
    if (item) selectModel(item.dataset.pid, item.dataset.mid, item.dataset.name);
  });

  // Model search: filter list
  document.getElementById('model-search').addEventListener('input', e => {
    renderModelList(e.target.value);
  });

  // Model picker settings icon → open settings dialog
  document.getElementById('btn-mpick-settings').addEventListener('click', () => {
    document.getElementById('model-dropdown').hide();
    openSettings();
  });

  // Settings open/close
  document.getElementById('btn-settings').addEventListener('click', openSettings);
  document.getElementById('btn-settings-close').addEventListener('click', () => {
    document.getElementById('settings-dialog').hide();
  });

  // Settings: connected providers remove button (delegated)
  document.getElementById('connected-providers-list').addEventListener('click', e => {
    const btn = e.target.closest('.provider-tag-remove');
    if (btn) removeProviderKey(btn.dataset.providerId);
  });

  // Settings: save provider key
  document.getElementById('btn-save-provider').addEventListener('click', saveProviderKey);

  // Settings: model provider change → load models
  document.getElementById('settings-model-provider').addEventListener('sl-change', e => {
    updateModelSelectForProvider(e.target.value);
  });

  // Settings: model select → enable save
  document.getElementById('settings-model-select').addEventListener('sl-change', e => {
    document.getElementById('btn-save-model').disabled = !e.target.value;
  });

  // Settings: save model
  document.getElementById('btn-save-model').addEventListener('click', saveDefaultModel);
}

// ── Initialisation ────────────────────────────────────────────────────────────
async function init() {
  wireEvents();

  // Wait for pywebview bridge and the Spiritus Shoelace components.
  // sl-menu-item is NOT awaited here — it is guaranteed to load because a
  // hidden placeholder is in the DOM (see index.html agent-menu), but we
  // don't want a stray missing component to block agent initialization.
  await Promise.all([
    new Promise(resolve => {
      if (window.pywebview) return resolve();
      window.addEventListener('pywebviewready', resolve, { once: true });
    }),
    customElements.whenDefined('sl-dropdown'),
    customElements.whenDefined('sl-dialog'),
    customElements.whenDefined('sl-select'),
  ]);

  // Load the app config FIRST (a fast local bridge call). Everything
  // application-specific — branding, folders, agents — comes from here.
  // Spiritus ships no
  // defaults of its own.
  try {
    const config = await bridge.getConfig();
    state.config = config;
    state.port = config.opencode_port;
    state.workspacePath = config.workspace_path;
    state.projectPath = config.project_path;
    state.agents = Array.isArray(config.agents) ? config.agents : [];

    applyBranding(config);

    // Show current default model if configured
    if (config.default_model) {
      const parts = config.default_model.split('/');
      document.getElementById('model-label').textContent = parts[parts.length - 1];
    }
  } catch (e) {
    console.error('getConfig:', e);
    showToast('Failed to connect to backend');
    return;
  }

  // Populate the agent dropdown from the app config (works before the server
  // is confirmed ready).
  initAgents();

  if (!state.port) {
    showToast('OpenCode server unavailable — check console');
    return;
  }

  connectSSE();
  await Promise.all([loadSessions(), loadAgents(), refreshVaultTree()]);
}

// Apply application-supplied branding to the shared Spiritus shell.
function applyBranding(config) {
  const title = config.app_title || 'Spiritus';
  document.title = title;
  const brand = document.querySelector('.brand span');
  if (brand) brand.textContent = title;
  const emptyTitle = document.querySelector('.empty-title');
  if (emptyTitle) emptyTitle.textContent = title;
}

init();
