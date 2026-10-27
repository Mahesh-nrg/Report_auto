/* pwndoc-ai: the gateway calls of the "Import a DOCX" drawer: upload and analyse, follow a running
 * step, load and save the preview, create, cancel, undo and delete. The steps run on the gateway;
 * closing the drawer only stops the polling, and reopening it follows the import again (its id
 * waits in sessionStorage). Every answer is checked against `gen` and the open import id first. */
import { api, fetchApi, uploadApi } from './ai-api.js';
import { checkDocxPick } from './ai-docx-util.js';
import { cur, hooks, live, patch, readImport, storeImport } from './ai-import-state.js';
import { isImportId, isRunning, normList, normPlan, normStatus } from './ai-import-util.js';
import { shortReason } from './ai-rag-upload.js';
import { S, ui } from './ai-state.js';
import { POLL_MS, POLL_TIMEOUT_MS, pollFailure } from './ai-util.js';
import { llmMissing } from './ai-widgets.js';

const BASE = '/docx/import/imports';
const UNEXPECTED = 'The gateway sent an unexpected answer.';
const GONE = 'That import no longer exists on the gateway.';
const RELOADED = 'The preview was reloaded because it changed on the gateway. Check it and try again.';
export const importPath = (id, tail) => BASE + '/' + encodeURIComponent(id) + (tail || '');

let pollSeq = 0;
let pollTimer = null;
let upload = null;  // AbortController of the upload in flight
const imageUrls = new Map();  // "<import id>/<key>" -> Promise of a blob URL ('' when it failed)

export function stopPolling() {
  pollSeq += 1;
  clearTimeout(pollTimer);
  pollTimer = null;
}

function stopAll() {
  stopPolling();
  if (upload) upload.abort();
  revokeImages();
}
hooks.stop = stopAll;

export async function loadList() {
  const gen = cur().gen;
  try {
    const list = normList(await api(BASE, { timeoutMs: POLL_TIMEOUT_MS }));
    if (live(gen)) patch({ list, listError: '' });
  } catch (e) {
    if (live(gen)) patch({ listError: 'The recent imports could not be loaded: ' + e.message, list: cur().list || normList(null) });
  }
}

/** Follow the open import while one of its steps runs. */
export function startPolling() {
  stopPolling();
  if (S.open === 'import' && cur().id) poll(pollSeq, cur().gen, cur().id);
}

async function poll(seq, gen, id) {
  try {
    const status = normStatus(await api(importPath(id), { timeoutMs: POLL_TIMEOUT_MS }));
    if (seq !== pollSeq || !live(gen, id)) return;
    if (!status) throw new Error(UNEXPECTED);
    patch({ status, pollFails: 0, pollError: '' });
    if (isRunning(status.state)) pollTimer = setTimeout(() => poll(seq, gen, id), POLL_MS);
    else ended(status);
  } catch (e) {
    if (seq !== pollSeq || !live(gen, id)) return;
    const verdict = pollFailure(cur(), e);
    if (verdict.gone) { importGone(); return; }
    patch({ pollFails: verdict.fails || 0, ...(verdict.stop ? { pollError: verdict.message } : {}) });
    if (!verdict.stop) pollTimer = setTimeout(() => poll(seq, gen, id), POLL_MS);
  }
}

const END_TEXT = {
  ready: 'The report was read. Check the preview.', created: 'Created in PwnDoc.', undone: 'The import was undone.',
  analysis_failed: 'The report could not be read.', create_failed: 'Creating in PwnDoc failed.',
  undo_failed: 'The undo did not finish.', cancelled: 'The step was cancelled.',
};

function ended(status) {
  storeImport(status.state === 'ready' || status.state === 'created' ? { id: status.id } : null);
  ui.announce(END_TEXT[status.state] || '');
  if (status.hasPreview && (status.state === 'ready' || !cur().plan)) loadPreview(status.id);
  loadList();
}

function importGone() {
  storeImport(null);
  patch({ id: null, status: null, plan: null, confirm: '', actionError: GONE });
  loadList();
}

/** Open an import (from the list, after a reload, or right after starting one). */
export async function openImport(id) {
  if (!isImportId(id)) return;
  stopPolling();
  const gen = cur().gen;
  patch({ id, status: null, plan: null, planError: '', confirm: '', actionError: '', notice: '', pollError: '', dirty: false, acceptBlanks: false, confirmDuplicate: false });
  try {
    const status = normStatus(await api(importPath(id), { timeoutMs: POLL_TIMEOUT_MS }));
    if (!live(gen, id)) return;
    if (!status) throw new Error(UNEXPECTED);
    patch({ status });
    if (isRunning(status.state)) { storeImport({ id }); startPolling(); return; }
    storeImport(status.state === 'ready' || status.state === 'created' ? { id } : null);
    if (status.hasPreview) loadPreview(id);
  } catch (e) {
    if (!live(gen, id)) return;
    if (e.status === 404) importGone(); else patch({ actionError: e.message });
  }
}

export function resumeImport() {
  const saved = readImport();
  if (saved && isImportId(saved.id) && !cur().id) openImport(saved.id);
  else if (cur().id && cur().status && isRunning(cur().status.state)) startPolling();
}

export async function loadPreview(id) {
  const gen = cur().gen;
  try {
    const plan = normPlan(await api(importPath(id, '/preview'), { timeoutMs: POLL_TIMEOUT_MS }));
    if (!live(gen, id)) return;
    if (!plan) throw new Error(UNEXPECTED);
    patch({ plan, planError: '', dirty: false });
  } catch (e) {
    if (live(gen, id)) patch({ planError: 'The preview could not be loaded: ' + e.message });
  }
}

/** Why an upload failed; a full store points to where the imported reports are kept. */
function uploadReason(e) {
  const text = shortReason(e);
  return e && e.status === 413 ? text + ' Imported reports are kept in Refine a DOCX › My documents; delete old ones there.' : text;
}

/** Pick a file: check it, upload it through the DOCX file store, then start the analysis. */
export async function pickFile(files) {
  const problem = checkDocxPick(files);
  if (problem) { patch({ actionError: problem }); return; }
  if (cur().uploading || cur().starting) return;
  stopPolling();
  const gen = cur().gen;
  const file = files[0];
  upload = new AbortController();
  patch({ id: null, status: null, plan: null, confirm: '', actionError: '', notice: '', uploading: { name: file.name, pct: 0 } });
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const done = await uploadApi('/docx/files', form, {
      signal: upload.signal,
      onProgress: (l, t) => { if (live(gen) && t > 0) patch({ uploading: { name: file.name, pct: Math.min(100, Math.floor((l / t) * 100)) } }); },
    });
    if (!live(gen)) return;
    patch({ uploading: null });
    if (!done || !isImportId(done.id)) throw new Error(UNEXPECTED);
    await analyse(done.id);
  } catch (e) {
    if (live(gen)) patch({ uploading: null, actionError: e.aborted ? 'Upload cancelled.' : uploadReason(e) });
  } finally {
    upload = null;
  }
}

/** Start the analysis of an uploaded DOCX (also "Analyse again" after a failure). */
export async function analyse(fileId) {
  const gen = cur().gen;
  patch({ starting: true, actionError: '' });
  try {
    const status = normStatus(await api(BASE, { method: 'POST', body: { file_id: fileId, use_llm: cur().useLlm && !llmMissing() } }));
    if (!live(gen)) return;
    if (!status) throw new Error(UNEXPECTED);
    storeImport({ id: status.id });
    patch({ starting: false, id: status.id, status, plan: null, dirty: false, acceptBlanks: false, confirmDuplicate: false });
    ui.announce('Reading the report.');
    startPolling();
    loadList();
  } catch (e) {
    if (live(gen)) patch({ starting: false, actionError: e.message });
  }
}

/** Save the changed preview fields. A 409 "stale" reloads the preview. */
export async function saveEdits(body) {
  const { gen, id } = cur();
  patch({ saving: true, actionError: '', notice: '' });
  try {
    const plan = normPlan(await api(importPath(id, '/preview'), { method: 'PUT', body }));
    if (!live(gen, id)) return;
    if (!plan) throw new Error(UNEXPECTED);
    patch({ saving: false, plan, dirty: false, notice: 'Changes saved.' });
    ui.announce('Changes saved.');
  } catch (e) {
    if (!live(gen, id)) return;
    patch({ saving: false, actionError: e.status === 409 ? RELOADED : e.message });
    if (e.status === 409) loadPreview(id);
  }
}

/** Ask the gateway to create everything in PwnDoc (after the confirm). */
export async function startCreate() {
  const { gen, id, plan, acceptBlanks, confirmDuplicate } = cur();
  if (!plan || cur().posting) return;
  patch({ posting: true, actionError: '' });
  const body = { revision: plan.revision, accept_blanks: acceptBlanks, confirm_duplicate: confirmDuplicate };
  try {
    const status = normStatus(await api(importPath(id, '/create'), { method: 'POST', body }));
    if (!live(gen, id)) return;
    if (!status) throw new Error(UNEXPECTED);
    storeImport({ id });
    patch({ posting: false, confirm: '', status });
    ui.announce('Creating in PwnDoc.');
    startPolling();
  } catch (e) {
    if (!live(gen, id)) return;
    patch({ posting: false, confirm: '', actionError: e.message });
    if (e.status === 409 || e.status === 400) loadPreview(id);
  }
}

/** One POST or DELETE on the open import; true when the gateway accepted it. */
async function action(tail, method, started) {
  const { gen, id } = cur();
  patch({ posting: true, confirm: '', actionError: '' });
  try {
    const raw = await api(importPath(id, tail), { method, body: method === 'POST' ? {} : undefined });
    if (!live(gen, id)) return false;
    patch({ posting: false });
    if (started) started(normStatus(raw));
    return true;
  } catch (e) {
    if (live(gen, id)) patch({ posting: false, actionError: e.message });
    return false;
  }
}

export const cancelStep = () => action('/cancel', 'POST', (status) => { if (status) patch({ status }); startPolling(); });

export const startUndo = () => action('/undo', 'POST', (status) => {
  if (status) patch({ status });
  storeImport({ id: cur().id });
  ui.announce('Undoing the import.');
  startPolling();
});

/** Delete the local import (preview, screenshots, journal). PwnDoc is never touched. */
export async function discardImport() {
  const name = cur().status ? cur().status.fileName : 'the import';
  if (!(await action('', 'DELETE'))) return;
  storeImport(null);
  revokeImages();
  patch({ id: null, status: null, plan: null, notice: 'Deleted the import of ' + name + '.' });
  ui.announce('Deleted the import of ' + name + '.');
  loadList();
}

async function fetchImage(id, key) {
  try {
    const res = await fetchApi(importPath(id, '/images/' + encodeURIComponent(key)), {}, POLL_TIMEOUT_MS);
    const type = res.headers.get('Content-Type') || '';
    if (!res.ok || !/^image\/(png|jpeg|gif)$/.test(type)) return '';
    return URL.createObjectURL(await res.blob());
  } catch (e) {
    return '';  // the screenshot shows as its caption only
  }
}

/** A blob URL of one extracted screenshot (fetched with the CSRF header), or '' when it fails.
 * The promise is cached, so the PoC preview and the Screenshots list share one request. */
export function imageUrl(id, key) {
  const slot = id + '/' + key;
  if (!imageUrls.has(slot)) {
    const pending = fetchImage(id, key).then((url) => {
      if (!url && imageUrls.get(slot) === pending) imageUrls.delete(slot);  // a later view retries
      return url;
    });
    imageUrls.set(slot, pending);
  }
  return imageUrls.get(slot);
}

/** Revoke every blob URL, including those whose fetch is still running. */
export function revokeImages() {
  for (const pending of imageUrls.values()) pending.then((url) => { if (url) URL.revokeObjectURL(url); });
  imageUrls.clear();
}
