/* pwndoc-ai: the drag-and-drop zone of the knowledge base and the list of uploads under it. */
import { btn, chip, focusKey, h, icon, keepFocusKey, plural } from './ai-dom.js';
import { ALLOWED_EXT, collectDropped, fmtBytes } from './ai-rag-files.js';
import { UploadQueue, isActive, isFinal } from './ai-rag-upload.js';
import { MAX_LISTED } from './ai-util.js';

const DRAW_MS = 80;  // progress events are coalesced into one redraw per interval
const TYPES_TEXT = 'PDF, DOCX, Markdown, text or JSON';
const HAS_FILES = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
const STATE_CHIP = {
  queued: ['mute', 'Waiting'], stored: ['ok', 'Stored'], exists: ['mute', 'Already stored'],
  duplicate: ['warn', 'Duplicate'], failed: ['bad', 'Failed'], rejected: ['warn', 'Not added'],
};
const STATE_NOTE = {
  stored: 'Stored. Not indexed yet.',
  exists: 'Already in the knowledge base with the same content.',
  duplicate: 'Same content as a file already stored; nothing was added.',
};

const percent = (item) => (item.total > 0 ? Math.min(100, Math.floor((item.loaded / item.total) * 100)) : 0);
const sending = (item) => item.state === 'uploading' && item.total > 0 && item.loaded >= item.total;

function stateChip(item) {
  if (item.state !== 'uploading') return chip(...STATE_CHIP[item.state]);
  return chip('accent', h('i', { class: 'ai-pulse' }), sending(item) ? 'Storing…' : 'Uploading ' + percent(item) + '%');
}

function bar(label, pct) {
  const fill = h('i');
  fill.style.transform = 'scaleX(' + pct / 100 + ')';
  return h('div', {
    class: 'ai-bar', role: 'progressbar', 'aria-label': label, 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct),
  }, fill);
}

/** What to list: everything in order, or (for a huge drop) the active files first, then the latest results. */
function visible(items) {
  if (items.length <= MAX_LISTED) return items;
  return [...items.filter((i) => !isFinal(i)), ...items.filter(isFinal).reverse()].slice(0, MAX_LISTED);
}

export class DropZone {
  /** @param {{getMax: () => number, onStored: () => void}} hooks */
  constructor(hooks) {
    this.hooks = hooks;
    this.timer = null;
    this.disposed = false;
    this.queue = new UploadQueue({
      getMax: hooks.getMax, onChange: () => this.schedule(), onDrained: (items) => this.drained(items),
    });
    this.input = h('input', { type: 'file', multiple: true, hidden: true, accept: ALLOWED_EXT.join(','), 'aria-hidden': 'true', tabindex: '-1' });
    this.hint = h('small', { id: 'ai-drop-hint' });
    this.zone = h('div', {
      class: 'ai-drop', role: 'button', tabindex: '0', 'data-fk': 'rag-drop', 'aria-describedby': 'ai-drop-hint',
      'aria-label': 'Add documents: drop files or folders here, or press Enter to choose files',
    }, icon('upload'), h('span', { class: 'ai-drop-text' }, h('strong', {}, 'Drop books, PDFs and notes here'), this.hint));
    this.summary = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.list = h('ul', { class: 'ai-ups', 'aria-label': 'Uploads' });
    this.overall = h('div', { class: 'ai-up-overall' });
    this.clear = btn('ai-ghost ai-sm', 'Clear finished', () => this.clearDone(), { 'data-fk': 'up-clear' });
    this.box = h('div', { class: 'ai-rag' }, this.zone, this.input, this.summary, this.overall, this.list, this.clear);
    this.guard = (e) => { if (HAS_FILES(e) && !e.composedPath().includes(this.zone)) e.preventDefault(); };
    this.wire();
    this.setLimit(hooks.getMax());
    this.draw();
  }

  setLimit(max) {
    this.hint.textContent = 'Or click to choose files. ' + TYPES_TEXT + ', up to ' + fmtBytes(max) + ' each. Uploading only stores the files.';
  }

  wire() {
    const z = this.zone;
    z.addEventListener('click', () => this.input.click());
    z.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.input.click(); }
    });
    this.input.addEventListener('change', () => {
      const files = Array.from(this.input.files || []);
      this.input.value = '';
      this.take({ files, unreadable: 0, truncated: false });
    });
    z.addEventListener('dragenter', (e) => this.over(e, true));
    z.addEventListener('dragover', (e) => this.over(e, true));
    z.addEventListener('dragleave', (e) => { if (!z.contains(e.relatedTarget)) z.classList.remove('ai-drop-over'); });
    z.addEventListener('drop', (e) => this.dropped(e));
    // A file that misses the zone must not navigate the page away from the report.
    window.addEventListener('dragover', this.guard);
    window.addEventListener('drop', this.guard);
  }

  over(e, on) {
    if (!HAS_FILES(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    this.zone.classList.toggle('ai-drop-over', on);
  }

  async dropped(e) {
    if (!HAS_FILES(e)) return;
    e.preventDefault();
    this.zone.classList.remove('ai-drop-over');
    const got = await collectDropped(e.dataTransfer);  // the entries are read before its first await
    if (!this.disposed) this.take(got);
  }

  take({ files, unreadable, truncated }) {
    const n = this.queue.add(files);
    const parts = [];
    if (n.queued) parts.push(plural(n.queued, 'file') + ' added to the upload queue.');
    if (n.rejected) parts.push(plural(n.rejected, 'file') + ' not added (see the list' + (n.hidden ? '; ' + n.hidden + ' more are not listed' : '') + ').');
    if (n.repeated) parts.push(plural(n.repeated, 'file') + ' already in the list.');
    if (unreadable) parts.push(plural(unreadable, 'item') + ' in the dropped folders could not be read.');
    if (truncated) parts.push('Only the first files of the drop were taken.');
    this.summary.textContent = parts.join(' ') || 'No files found in the drop.';
  }

  drained(items) {
    const n = (s) => items.filter((i) => i.state === s).length;
    const parts = [];
    if (n('stored')) parts.push(plural(n('stored'), 'file') + ' stored');
    if (n('exists')) parts.push(n('exists') + ' already stored');
    if (n('duplicate')) parts.push(plural(n('duplicate'), 'duplicate'));
    if (n('failed')) parts.push(n('failed') + ' failed');
    this.summary.textContent = (parts.join(', ') || 'Nothing was stored') + '.'
      + (n('stored') ? ' Press Index new files to make them searchable.' : '');
    this.hooks.onStored();
  }

  clearDone() {
    this.queue.clearFinished();
    this.draw();
    focusKey(this.box, 'rag-drop');
  }

  schedule() {
    if (this.timer || this.disposed) return;
    this.timer = setTimeout(() => { this.timer = null; this.draw(); }, DRAW_MS);
  }

  row(item) {
    const meta = item.reason || (item.state === 'duplicate' && item.twin ? 'Same content as ' + item.twin + '; nothing was added.' : STATE_NOTE[item.state]) || '';
    const danger = item.state === 'failed' || item.state === 'rejected';
    return h('li', { class: 'ai-up', 'data-state': item.state },
      h('div', { class: 'ai-up-head' },
        h('span', { class: 'ai-doc-name' }, item.name, h('small', {}, fmtBytes(item.size))),
        stateChip(item), this.action(item)),
      item.state === 'uploading' ? bar('Upload progress of ' + item.name, sending(item) ? 100 : percent(item)) : null,
      meta ? h('p', { class: danger ? 'ai-item-error' : 'ai-item-meta' }, meta) : null);
  }

  action(item) {
    const key = 'up-' + item.id;
    if (item.state === 'failed') return btn('ai-ghost ai-sm', 'Retry', () => this.queue.retry(item.id), { 'data-fk': key, 'aria-label': 'Retry ' + item.name });
    if (item.state === 'queued') return btn('ai-ghost ai-sm', 'Remove', () => this.queue.cancel(item.id), { 'data-fk': key, 'aria-label': 'Remove ' + item.name + ' from the queue' });
    if (item.state === 'uploading') return btn('ai-ghost ai-sm', 'Cancel', () => this.queue.cancel(item.id), { 'data-fk': key, 'aria-label': 'Cancel upload of ' + item.name });
    return null;
  }

  draw() {
    if (this.disposed) return;
    const items = this.queue.items;
    keepFocusKey(this.box, () => {
      this.list.replaceChildren(...visible(items).map((i) => this.row(i)));
      const hidden = items.length - Math.min(items.length, MAX_LISTED);
      if (hidden > 0) this.list.append(h('li', { class: 'ai-quiet' }, 'and ' + hidden + ' more'));
      const done = items.filter(isFinal).length;
      const active = items.some(isActive);
      this.overall.replaceChildren(...(active && items.length > 1
        ? [h('p', { class: 'ai-quiet' }, done + ' of ' + items.length + ' files done'), bar('Overall upload progress', Math.round((done / items.length) * 100))] : []));
      this.clear.hidden = !items.some(isFinal);
    });
    this.list.hidden = items.length === 0;
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    this.timer = null;
    this.queue.dispose();
    window.removeEventListener('dragover', this.guard);
    window.removeEventListener('drop', this.guard);
  }
}
