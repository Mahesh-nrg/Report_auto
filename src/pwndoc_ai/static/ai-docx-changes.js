/* pwndoc-ai: the result of a refine run: summary, downloads and the paged review of every changed
 * paragraph with the changed words highlighted. All document text is drawn as text nodes. */
import { api } from './ai-api.js';
import { btn, chip, h, note, plural, section } from './ai-dom.js';
import { copyButtons, downloadInfo } from './ai-docx-download.js';
import { docxLayoutBlock } from './ai-docx-layout.js';
import { CHANGES_PAGE, normChanges, wordDiff } from './ai-docx-util.js';
import { cur, filePath, live, patch } from './ai-docx-store.js';
import { POLL_TIMEOUT_MS } from './ai-util.js';

const CHANGE_CHIP = { applied: ['ok', 'Applied'], rejected: ['warn', 'Rejected'], unchanged: ['mute', 'No change'] };
export const MODE_LABEL = { light: 'Light polish', deep: 'Deep refine' };

/** Load the first page of the review (reset) or the next one. */
export async function loadChanges(reset) {
  const D = cur();
  const id = D.selectedId;
  if (!id || D.changes.loading) return;
  const gen = D.gen;
  const base = reset || D.changes.fileId !== id ? [] : D.changes.items;
  patch({ changes: { fileId: id, items: base, total: reset ? 0 : D.changes.total, loading: true, error: '' } });
  try {
    const raw = await api(filePath(id, '/changes?offset=' + base.length + '&limit=' + CHANGES_PAGE), { timeoutMs: POLL_TIMEOUT_MS });
    if (!live(gen, id)) return;
    const page = normChanges(raw);
    const items = [...base, ...page.changes];
    // An empty page ends the list even when the gateway's total says more: no endless "Load more".
    patch({ changes: { fileId: id, items, total: page.changes.length ? page.total : items.length, loading: false, error: '' } });
  } catch (e) {
    if (live(gen, id)) patch({ changes: { ...cur().changes, loading: false, error: e.message } });
  }
}

const mark = (seg) => (seg.changed ? h('mark', { class: 'ai-hl' }, seg.text) : seg.text);

function side(cls, label, segments) {
  return h('div', { class: cls }, h('span', {}, label), h('p', { class: 'ai-doc-text' }, segments.map(mark)));
}

function changeRow(c) {
  const [tone, label] = CHANGE_CHIP[c.status];
  const head = h('div', { class: 'ai-item-head' },
    h('span', { class: 'ai-item-title' }, c.section || 'Document'), chip(tone, label));
  const reason = c.reason ? h('p', { class: 'ai-item-meta' }, c.reason) : null;
  if (c.status === 'unchanged') return h('li', { class: 'ai-item ai-change', 'data-status': c.status }, head, reason, h('p', { class: 'ai-doc-text' }, c.after || c.before));
  const diff = wordDiff(c.before, c.after);
  return h('li', { class: 'ai-item ai-change', 'data-status': c.status }, head, reason,
    h('div', { class: 'ai-diff-cols' },
      side('ai-before', 'Before', diff.before),
      side('ai-after', c.status === 'rejected' ? 'Proposed, not applied' : 'After', diff.after)));
}

export function changesView(D) {
  const c = D.changes;
  const more = c.items.length < c.total;
  const kids = [];
  if (c.error) kids.push(note('bad', c.error));
  if (!c.items.length && c.loading) kids.push(h('p', { class: 'ai-quiet' }, 'Loading changes…'));
  else if (!c.items.length && !c.error) kids.push(h('p', { class: 'ai-quiet' }, 'No paragraph was changed.'));
  else if (c.items.length) {
    kids.push(h('p', { class: 'ai-quiet' }, 'Showing ' + c.items.length + ' of ' + c.total + '. Words that changed are highlighted.'));
    kids.push(h('ul', { class: 'ai-items', 'aria-label': 'Changes' }, c.items.map(changeRow)));
  }
  if (more || c.error) {
    kids.push(btn('ai-ghost', c.loading ? 'Loading…' : 'Load more', () => loadChanges(false), { disabled: c.loading ? true : null, 'data-fk': 'docx-more' }));
  }
  return section('Review changes', kids);
}

function summaryCard(D) {
  const file = D.detail.file;
  const st = D.status && D.status.state === 'done' ? D.status : file;
  const run = D.detail.lastRun;
  const mode = (run && run.mode) || file.mode;
  return h('div', { class: 'ai-progress' },
    h('div', { class: 'ai-progress-top' }, h('strong', { class: 'ai-big' }, 'Refinement finished'),
      mode ? chip('accent', MODE_LABEL[mode]) : null),
    h('div', { class: 'ai-row' }, chip('ok', st.changed + ' changed'), chip('warn', st.rejected + ' rejected'), chip('mute', st.protected + ' protected')),
    run && run.failedChunks ? note('warn', plural(run.failedChunks, 'part') + ' could not be processed and keep the original text.') : null,
    run && run.models.length ? h('p', { class: 'ai-item-meta' }, 'Model: ' + run.models.join(', ')) : null);
}

/** Everything shown for a finished document: summary, the four downloads, the review. */
export function resultView(D) {
  return h('div', { class: 'ai-stack' }, summaryCard(D),
    section('Download', h('div', { class: 'ai-row' }, copyButtons(D, D.detail.file, false)), downloadInfo(D)),
    docxLayoutBlock(D),
    changesView(D));
}
