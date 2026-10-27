/* pwndoc-ai: opening a document, starting, following and cancelling a refine run. The run keeps
 * going on the gateway when the drawer is closed or the page reloads; only the polling stops, and
 * it starts again when the drawer is opened (the document's id waits in sessionStorage). */
import { api } from './ai-api.js';
import { chip, h, plural } from './ai-dom.js';
import { loadChanges } from './ai-docx-changes.js';
import { loadDocxLayout } from './ai-docx-layout.js';
import {
  UNEXPECTED, clearStored, fileGone, loadList, normDetail,
} from './ai-docx-files.js';
import {
  clearedSelection, cur, filePath, freshSetup, live, patch,
} from './ai-docx-store.js';
import { normStatus, refineBody } from './ai-docx-util.js';
import { S, storeDocx, timers, ui } from './ai-state.js';
import { POLL_MS, POLL_TIMEOUT_MS, pollFailure } from './ai-util.js';

const BUSY_TEXT = 'A document is already being refined (by you, perhaps in another tab). Wait for it to finish or cancel it, then start again.';
let pollSeq = 0;  // a poll answer that arrives after a newer start or a stop is ignored

export function stopPolling() {
  pollSeq += 1;
  clearTimeout(timers.docx);
  timers.docx = null;
}

/** Poll the open document's status now and then every POLL_MS while it runs and the drawer is open. */
export function startPolling() {
  stopPolling();
  if (S.open !== 'docx' || !cur().selectedId) return;
  poll(pollSeq);
}

function pollAgain(seq) {
  timers.docx = setTimeout(() => poll(seq), POLL_MS);
}

async function poll(seq) {
  const { gen, selectedId: id } = cur();
  try {
    const status = normStatus(await api(filePath(id, '/status'), { timeoutMs: POLL_TIMEOUT_MS }));
    if (seq !== pollSeq || !live(gen, id)) return;
    patch({ status, pollFails: 0, pollError: '' });
    if (status.state === 'running') pollAgain(seq);
    else ended(gen, id, status);
  } catch (e) {
    if (seq !== pollSeq || !live(gen, id)) return;
    pollFailed(e, id, seq);
  }
}

function pollFailed(e, id, seq) {
  const verdict = pollFailure(cur(), e);
  if (verdict.gone) { fileGone(id); return; }
  patch({ pollFails: verdict.fails || 0, ...(verdict.stop ? { pollError: verdict.message } : {}) });
  if (!verdict.stop) pollAgain(seq);
}

const END_TEXT = {
  done: (st) => 'Refinement finished. ' + plural(st.changed, 'paragraph') + ' changed.',
  error: () => 'Refinement failed.',
  cancelled: () => 'Refinement cancelled.',
};

/** A run reached a terminal state: refresh the list and the summary, open the review. */
async function ended(gen, id, status) {
  clearStored(id);
  ui.announce((END_TEXT[status.state] || END_TEXT.error)(status));
  loadList();
  try {
    const detail = normDetail(await api(filePath(id), { timeoutMs: POLL_TIMEOUT_MS }), cur().detail);
    if (live(gen, id) && detail) patch({ detail });
  } catch (e) {
    if (live(gen, id)) patch({ detailError: e.message });
  }
  if (live(gen, id) && status.state === 'done') { loadChanges(true); loadDocxLayout(id); }
}

/** Show what is known about a document, then follow it when it is running. */
export async function selectFile(id) {
  const { gen, selectedId, detail: known } = cur();
  const same = selectedId === id && !!known;
  stopPolling();
  patch(same ? { detailError: '', confirmId: '', pollError: '', pollFails: 0 }
    : { ...clearedSelection(), selectedId: id, confirmId: '', actionError: '', notice: '' });
  try {
    const [raw, rawStatus] = await Promise.all([
      api(filePath(id), { timeoutMs: POLL_TIMEOUT_MS }), api(filePath(id, '/status'), { timeoutMs: POLL_TIMEOUT_MS }),
    ]);
    if (!live(gen, id)) return;
    const detail = normDetail(raw, cur().detail);
    if (!detail) throw new Error(UNEXPECTED);
    const status = normStatus(rawStatus);
    patch({ detail, status, setup: cur().setup || freshSetup(detail.outline), detailError: '' });
    enter(status, id);
  } catch (e) {
    if (!live(gen, id)) return;
    if (e.status === 404) fileGone(id); else patch({ detailError: e.message });
  }
}

function enter(status, id) {
  if (status.state === 'running') { storeDocx({ id }); startPolling(); return; }
  clearStored(id);
  if (status.state !== 'done') return;
  if (cur().changes.fileId !== id) loadChanges(true);
  loadDocxLayout(id);
}

export async function startRefine() {
  const D = cur();
  const { gen, selectedId: id } = D;
  if (!D.detail || D.starting) return;
  patch({ starting: true, actionError: '' });
  try {
    await api(filePath(id, '/refine'), { method: 'POST', body: refineBody(D.setup, D.detail.outline) });
  } catch (e) {
    if (!live(gen)) return;
    patch({ starting: false, actionError: e.status === 409 ? BUSY_TEXT : e.message });
    if (e.status === 409) loadList();
    return;
  }
  if (!live(gen)) return;
  storeDocx({ id });
  if (cur().selectedId !== id) { patch({ starting: false }); loadList(); return; }
  patch({
    starting: false, status: normStatus({ state: 'running' }), showSetup: null, pollFails: 0, pollError: '',
    changes: { fileId: '', items: [], total: 0, loading: false, error: '' },
    dl: { ...cur().dl, layout: null, info: '' },  // the clean copy is about to change
  });
  ui.announce('Refining started.');
  startPolling();
  loadList();
}

/** Ask the gateway to stop. Whatever it answers, the run is followed on until it really ended. */
export async function cancelRun() {
  const { gen, selectedId: id } = cur();
  patch({ actionError: '' });
  try {
    await api(filePath(id, '/cancel'), { method: 'POST' });
  } catch (e) {
    if (live(gen, id)) patch({ actionError: e.message });
  }
  if (live(gen, id)) startPolling();
}

/** The progress card of a running document. */
export function progressView(D) {
  const st = D.status && D.status.state === 'running' ? D.status : normStatus({ state: 'running' });
  const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : 0;
  const fill = h('i');
  fill.style.transform = 'scaleX(' + pct / 100 + ')';
  return h('div', { class: 'ai-progress' },
    h('div', { class: 'ai-progress-top' }, h('strong', { class: 'ai-big' }, 'Refining…'),
      h('span', { class: 'ai-quiet' }, st.total ? st.done + ' of ' + plural(st.total, 'part') : 'Starting…')),
    h('div', {
      class: 'ai-bar', role: 'progressbar', 'aria-label': 'Refinement progress', 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': String(pct), 'aria-valuetext': st.total ? st.done + ' of ' + plural(st.total, 'part') : 'Starting',
    }, fill),
    st.current ? h('p', { class: 'ai-item-meta' }, 'Now: ' + st.current) : null,
    h('div', { class: 'ai-row' }, chip('ok', st.changed + ' changed'), chip('warn', st.rejected + ' rejected'), chip('mute', st.protected + ' protected')));
}
