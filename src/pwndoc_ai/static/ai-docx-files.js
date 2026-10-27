/* pwndoc-ai: the document list of the "Refine a DOCX" drawer: loading it, adopting a fresh upload
 * and deleting. Pure state and gateway calls; the drawing is in ai-docx-list.js. */
import { api } from './ai-api.js';
import {
  normFile, normFiles, normLastRun, normOutline, normQuota, normStats, normStatus,
} from './ai-docx-util.js';
import {
  clearedSelection, cur, filePath, freshChanges, freshSetup, live, patch,
} from './ai-docx-store.js';
import { readStored, storeDocx, ui } from './ai-state.js';
import { DOCX_KEY, POLL_TIMEOUT_MS } from './ai-util.js';

export const UNEXPECTED = 'The gateway sent an unexpected answer.';

export async function loadList() {
  const gen = cur().gen;
  try {
    const raw = await api('/docx/files', { timeoutMs: POLL_TIMEOUT_MS });
    if (!live(gen)) return;
    patch({ files: normFiles(raw), quota: normQuota(raw && raw.quota), listError: '' });
  } catch (e) {
    if (live(gen)) patch({ listError: e.message, files: cur().files || [] });
  }
}

/** A document's detail from the gateway's answer, or null when it is not one. The outline keeps its identity when unchanged. */
export function normDetail(raw, previous) {
  const file = normFile(raw);
  if (!file) return null;
  const outline = normOutline(raw.outline);
  const same = previous && previous.file.id === file.id && JSON.stringify(previous.outline) === JSON.stringify(outline);
  return Object.freeze({
    file, outline: same ? previous.outline : outline, stats: normStats(raw.stats), lastRun: normLastRun(raw.last_run || raw.run),
  });
}

/** Forget the stored run id when it is the one of this document. */
export function clearStored(id) {
  const saved = readStored(DOCX_KEY);
  if (saved && saved.id === id) storeDocx(null);
}

/** A document that was just uploaded becomes the open one, with its outline. */
export function adoptUpload(resp) {
  const detail = normDetail(resp, null);
  if (!detail) { patch({ actionError: UNEXPECTED }); return false; }
  patch({
    ...clearedSelection(), selectedId: detail.file.id, detail, status: normStatus({ state: 'uploaded' }),
    setup: freshSetup(detail.outline), changes: freshChanges(), actionError: '', detailError: '', confirmId: '', notice: '',
  });
  loadList();
  return true;
}

/** The open document is gone from the gateway (deleted elsewhere): say so and drop what points at it. */
export function fileGone(id) {
  clearStored(id);
  patch({ ...clearedSelection(), actionError: 'That document no longer exists on the gateway.', confirmId: '' });
  loadList();
}

export async function deleteFile(id) {
  const gen = cur().gen;
  const name = ((cur().files || []).find((f) => f.id === id) || {}).name || 'the document';
  patch({ confirmId: '', deletingId: id, actionError: '', notice: '' });
  try {
    await api(filePath(id), { method: 'DELETE' });
  } catch (e) {
    if (live(gen)) patch({ deletingId: '', actionError: 'Could not delete ' + name + ': ' + e.message });
    return;
  }
  if (!live(gen)) return;
  const wasOpen = cur().selectedId === id;
  clearStored(id);
  patch({
    deletingId: '', files: (cur().files || []).filter((f) => f.id !== id), notice: 'Deleted ' + name + '.',
    ...(wasOpen ? clearedSelection() : {}),
  });
  ui.announce('Deleted ' + name + '.');
  loadList();
}
