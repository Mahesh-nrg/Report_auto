/* pwndoc-ai: the indexing run of the knowledge base: normalising the gateway's status and drawing
 * the progress card (overall bar, current file, per-file chips, new versus reused chunks). */
import { chip, h, note, plural } from './ai-dom.js';
import { fmtDuration, stampMs } from './ai-rag-files.js';
import { MAX_LISTED, objects } from './ai-util.js';

const STATES = new Set(['idle', 'running', 'done', 'error', 'cancelled']);
const ITEM_STATES = new Set(['pending', 'indexed', 'skipped', 'duplicate', 'failed']);
const MAX_TEXT = 300;
const MAX_RUN_MS = 30 * 24 * 3600 * 1000;  // a longer span means the two clocks disagree
const ITEM_CHIP = {
  indexed: ['ok', 'Indexed'], skipped: ['mute', 'Already indexed'], duplicate: ['warn', 'Duplicate'], failed: ['bad', 'Failed'],
};
const TITLE = { running: 'Indexing…', done: 'Indexing finished', cancelled: 'Indexing cancelled', error: 'Indexing stopped' };

const count = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);
const words = (v) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT) : '');

function normItem(raw) {
  return {
    name: words(raw.name), status: ITEM_STATES.has(raw.status) ? raw.status : 'pending',
    chunksNew: count(raw.chunks_new), chunksReused: count(raw.chunks_reused), error: words(raw.error),
  };
}

/** The gateway's index status as a plain, validated object (anything unexpected becomes idle/empty). */
export function normStatus(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const items = objects(r.items).map(normItem);
  const processed = items.filter((i) => i.status !== 'pending').length;
  return {
    state: STATES.has(r.state) ? r.state : 'idle', total: count(r.total) || items.length,
    done: Number.isInteger(r.done) && r.done >= 0 ? r.done : processed, current: words(r.current), items,
    started: r.started, ended: r.ended, message: words(r.error) || words(r.message) || words(r.detail),
  };
}

export function tally(items) {
  const by = (s) => items.filter((i) => i.status === s).length;
  const sum = (key) => items.reduce((n, i) => n + i[key], 0);
  return {
    indexed: by('indexed'), skipped: by('skipped'), duplicate: by('duplicate'), failed: by('failed'),
    pending: by('pending'), chunksNew: sum('chunksNew'), chunksReused: sum('chunksReused'),
  };
}

/** Milliseconds the run took (or has taken), or NaN when the gateway's timestamps are unusable. */
export function elapsedMs(status, now) {
  const start = stampMs(status.started);
  const end = status.state === 'running' ? now : stampMs(status.ended);
  const span = end - start;
  return span >= 0 && span <= MAX_RUN_MS ? span : Number.NaN;
}

const chunkText = (n, r) => plural(n, 'new chunk') + ', ' + r + ' reused';

function itemRow(item) {
  const [tone, label] = ITEM_CHIP[item.status];
  const detail = item.status === 'failed' ? item.error
    : item.status === 'indexed' || item.status === 'skipped' ? chunkText(item.chunksNew, item.chunksReused) : '';
  return h('li', { class: 'ai-run-item' },
    h('span', { class: 'ai-doc-name' }, item.name, detail ? h('small', { class: item.status === 'failed' ? 'ai-item-error' : '' }, detail) : null),
    chip(tone, label));
}

function summaryChips(t) {
  const out = [];
  if (t.indexed) out.push(chip('ok', t.indexed + ' indexed'));
  if (t.skipped) out.push(chip('mute', t.skipped + ' already indexed'));
  if (t.duplicate) out.push(chip('warn', plural(t.duplicate, 'duplicate')));
  if (t.failed) out.push(chip('bad', t.failed + ' failed'));
  return out;
}

function bar(done, total) {
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const fill = h('i');
  fill.style.transform = 'scaleX(' + pct / 100 + ')';
  return h('div', {
    class: 'ai-bar', role: 'progressbar', 'aria-label': 'Indexing progress', 'aria-valuemin': '0', 'aria-valuemax': '100',
    'aria-valuenow': String(pct), 'aria-valuetext': total ? done + ' of ' + total + ' files' : 'Starting',
  }, fill);
}

/** The progress card for a status that is not idle (the caller hides it for idle). */
export function runCard(status, elapsed) {
  const t = tally(status.items);
  const shown = status.items.filter((i) => i.status !== 'pending').reverse();
  const more = shown.length - MAX_LISTED;
  const span = Number.isNaN(elapsed) ? '' : ' · ' + fmtDuration(elapsed);
  return h('div', { class: 'ai-progress ai-run' },
    h('div', { class: 'ai-progress-top' },
      h('strong', { class: 'ai-big' }, TITLE[status.state] || 'Indexing'),
      h('span', { class: 'ai-quiet' }, status.total ? status.done + ' of ' + status.total + span : 'Starting…')),
    bar(status.done, status.total),
    status.state === 'running' && status.current ? h('p', { class: 'ai-item-meta' }, 'Now: ' + status.current) : null,
    h('div', { class: 'ai-row' }, summaryChips(t),
      t.indexed + t.skipped ? h('small', { class: 'ai-quiet' }, chunkText(t.chunksNew, t.chunksReused)) : null),
    status.state === 'error' ? note('bad', status.message || 'The run stopped because of an error.') : null,
    t.pending && status.state !== 'running' ? h('p', { class: 'ai-quiet' }, plural(t.pending, 'file') + ' not processed.') : null,
    shown.length ? h('ul', { class: 'ai-items ai-run-items', 'aria-label': 'Indexed files' }, shown.slice(0, MAX_LISTED).map(itemRow)) : null,
    more > 0 ? h('p', { class: 'ai-quiet' }, 'and ' + plural(more, 'earlier file')) : null);
}

/** Words for the screen reader when a run ends (the visible chips say the same in other words). */
export function endedText(status) {
  const t = tally(status.items);
  const head = { done: 'The run is finished.', cancelled: 'The run was cancelled.', error: 'The run stopped.' }[status.state] || 'The run ended.';
  return head + ' Files indexed: ' + t.indexed + '. Already indexed: ' + t.skipped + '. Duplicates: ' + t.duplicate + '. Failed: ' + t.failed + '.';
}
