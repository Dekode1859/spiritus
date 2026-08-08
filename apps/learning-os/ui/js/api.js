/**
 * Bridge client — the single place the UI talks to the Python backend.
 * Every backend capability gets one named method here; views never fetch().
 */
'use strict';

async function postBridge(method, args = []) {
  const response = await fetch(`/api/bridge/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ args }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${method}`);
  }
  return payload;
}

export async function uploadFiles(files) {
  const form = new FormData();
  for (const file of files) form.append('files', file, file.name);
  const response = await fetch('/api/upload/lexicon_import_files', {
    method: 'POST',
    body: form,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || 'Upload failed');
  }
  return payload;
}

export async function healthCheck() {
  try {
    const response = await fetch('/api/health');
    return response.ok;
  } catch {
    return false;
  }
}

export const api = {
  getConfig: () => postBridge('get_config'),
  overview: () => postBridge('lexicon_overview'),
  listSources: () => postBridge('lexicon_list_sources'),
  getSource: (sourceId) => postBridge('lexicon_get_source', [sourceId]),
  importUrl: (url) => postBridge('lexicon_import_url', [url]),
  reprocessSource: (sourceId) => postBridge('lexicon_reprocess_source', [sourceId]),
  wikiIndex: () => postBridge('lexicon_wiki_index'),
  wikiPage: (path) => postBridge('lexicon_wiki_page', [path]),
  knowledgeStatus: () => postBridge('lexicon_knowledge_status'),
  rebuildAll: () => postBridge('lexicon_rebuild_all'),
  rebuildSource: (sourceId) => postBridge('lexicon_rebuild_source', [sourceId]),
  retryFailed: () => postBridge('lexicon_retry_failed'),
  enrich: () => postBridge('lexicon_enrich'),
  reconcile: () => postBridge('lexicon_reconcile'),
  previewDelete: (sourceId) => postBridge('lexicon_preview_delete', [sourceId]),
  deleteSource: (sourceId) => postBridge('lexicon_delete_source', [sourceId]),
  openExternal: (url) => postBridge('open_external', [url]),
};
