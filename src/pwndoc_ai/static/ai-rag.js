/* pwndoc-ai: the Knowledge base panel inside AI Settings (administrators). It stitches together
 * the embedder line, the drag-and-drop uploads, the one-time incremental indexing run and the
 * list of stored documents. Uploading only stores files; indexing is its own explicit step, and
 * files that are already indexed are never embedded twice. */
import { api } from './ai-api.js';
import { btn, chip, focusKey, h, icon, keepFocusKey, note, plural } from './ai-dom.js';
import { DropZone } from './ai-rag-drop.js';
import { EmbedderStatus } from './ai-rag-embedder.js';
import { DEFAULT_MAX_UPLOAD_BYTES, fmtBytes } from './ai-rag-files.js';
import { IndexRun } from './ai-rag-index.js';
import { POLL_TIMEOUT_MS } from './ai-util.js';

const UNAVAILABLE = 'Local document search is not installed on the gateway. Run: pip install "pwndoc-ai[rag]"';
const EMPTY = 'No documents yet. Drop PDFs, books or notes above, then press Index new files.';

const STATES = new Set(['indexed', 'pending', 'duplicate', 'failed']);
const MAX_ERROR = 200;

/** Validated document rows (the list of an older gateway has no `indexed` flag: those are indexed).
 *  `state` says why a stored file has no chunks: waiting for a run (pending), or a run could not use it. */
export function normDocs(raw) {
  const list = Array.isArray(raw) ? raw : (raw && raw.documents) || [];
  return list.filter((d) => d && typeof d.name === 'string' && d.name).map((d) => {
    const indexed = d.indexed !== false;
    const state = indexed ? 'indexed' : (STATES.has(d.state) && d.state !== 'indexed' ? d.state : 'pending');
    return Object.freeze({
      name: d.name, size: Number.isFinite(d.size) && d.size >= 0 ? d.size : null,
      chunks: Number.isInteger(d.chunks) && d.chunks >= 0 ? d.chunks : 0,
      lowText: d.low_text === true, indexed, state,
      error: typeof d.error === 'string' ? d.error.replace(/\s+/g, ' ').trim().slice(0, MAX_ERROR) : '',
    });
  });
}

/** The chip of a document row and the note under its name for a file that is not indexed. */
export function stateView(doc) {
  if (doc.state === 'duplicate') return { kind: 'mute', label: 'Duplicate', note: 'same content as another file' };
  if (doc.state === 'failed') return { kind: 'bad', label: 'Not indexed', note: doc.error || 'could not be indexed' };
  return doc.indexed ? { kind: 'ok', label: 'Indexed', note: '' } : { kind: 'mute', label: 'Pending', note: '' };
}

const maxBytes = (raw) => (raw && Number.isFinite(raw.max_upload_bytes) && raw.max_upload_bytes > 0 ? raw.max_upload_bytes : 0);

class RagPanel {
  constructor() {
    this.docs = [];
    this.confirm = '';
    this.busy = false;
    this.disposed = false;
    this.maxBytes = DEFAULT_MAX_UPLOAD_BYTES;
    this.status = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });  // persistent live region
    const say = (text) => { this.status.textContent = text; };
    this.say = say;
    this.embedder = new EmbedderStatus({ say });
    this.drop = new DropZone({ getMax: () => this.maxBytes, onStored: () => this.refresh() });
    this.index = new IndexRun({
      say, pendingNames: () => this.docs.filter((d) => d.state === 'pending').map((d) => d.name),
      fileCount: () => this.docs.length, onChange: () => this.drawList(), onEnded: () => { this.refresh(); this.embedder.refresh(); },
    });
    this.summaryEl = h('p', { class: 'ai-quiet' });
    this.listEl = h('div');
    this.main = h('div', { class: 'ai-rag' }, this.embedder.box, this.drop.box, this.index.box, this.summaryEl, this.listEl, this.status);
    this.problem = h('div');
    this.box = h('div', { class: 'ai-rag ai-kb' }, this.problem, this.main);
  }

  /** Everything the panel shows is asked for at once, so a rebuilt panel is never half-informed. */
  start() {
    this.refresh();
    this.embedder.refresh();
    this.index.resume();
  }

  get locked() { return this.busy || this.index.running; }

  async refresh() {
    try {
      const raw = await api('/rag/documents', { timeoutMs: POLL_TIMEOUT_MS });
      if (this.disposed) return;
      if (!Array.isArray(raw) && raw && raw.available === false) { this.showUnavailable(); return; }
      this.problem.replaceChildren();
      this.main.hidden = false;
      this.docs = normDocs(raw);
      this.maxBytes = maxBytes(raw) || this.maxBytes;
      this.drop.setLimit(this.maxBytes);
    } catch (e) {
      if (this.disposed) return;
      this.problem.replaceChildren(note('bad', e.message));
    }
    this.draw();
  }

  showUnavailable() {
    this.main.hidden = true;
    this.problem.replaceChildren(note('warn', UNAVAILABLE));
  }

  ask(name, key) {
    this.confirm = name;
    this.draw();
    focusKey(this.box, key);
  }

  docRow(doc) {
    const view = stateView(doc);
    const detail = [doc.size == null ? '' : fmtBytes(doc.size), doc.indexed ? plural(doc.chunks, 'chunk') : '',
      doc.lowText ? 'little text found (scanned PDF?)' : '', view.note].filter(Boolean).join(' · ');
    const tail = this.confirm === doc.name
      ? h('span', { class: 'ai-row' },
        btn('ai-primary ai-sm', 'Delete', () => this.remove(doc.name), { 'data-fk': 'del-yes', 'aria-label': 'Confirm deleting ' + doc.name }),
        btn('ai-ghost ai-sm', 'Keep', () => this.ask('', 'del-' + doc.name), { 'data-fk': 'del-no' }))
      : h('button', {
        type: 'button', class: 'ai-x ai-x-sm', 'aria-label': 'Delete ' + doc.name, 'data-fk': 'del-' + doc.name,
        disabled: this.locked ? true : null, onclick: () => this.ask(doc.name, 'del-yes'),
      }, icon('trash'));
    return h('li', {}, h('span', { class: 'ai-doc-name' }, doc.name, h('small', {}, detail)),
      h('span', { class: 'ai-row' }, chip(view.kind, view.label), tail));
  }

  draw() {
    this.drawList();
    this.index.draw();
  }

  /** The stored documents; while a run is going, the files it has already handled show as indexed. */
  shown() {
    const done = new Map(this.index.status.items.filter((i) => i.status === 'indexed' || i.status === 'skipped').map((i) => [i.name, i]));
    const docs = this.index.running
      ? this.docs.map((d) => (done.has(d.name) && !d.indexed
        ? { ...d, indexed: true, state: 'indexed', chunks: done.get(d.name).chunksNew + done.get(d.name).chunksReused } : d))
      : this.docs;
    return [...docs].sort((a, b) => a.name.localeCompare(b.name));
  }

  drawList() {
    const docs = this.shown();
    const count = (state) => docs.filter((d) => d.state === state).length;
    const unusable = count('duplicate') + count('failed');
    const chunks = docs.reduce((n, d) => n + d.chunks, 0);
    this.summaryEl.textContent = docs.length
      ? plural(docs.length, 'file') + ' · ' + count('indexed') + ' indexed · ' + count('pending') + ' pending · '
        + (unusable ? unusable + ' not indexed · ' : '') + plural(chunks, 'chunk')
      : '';
    keepFocusKey(this.box, () => this.listEl.replaceChildren(docs.length
      ? h('ul', { class: 'ai-docs ai-kb-docs', 'aria-label': 'Documents' }, docs.map((d) => this.docRow(d)))
      : h('p', { class: 'ai-quiet' }, EMPTY)));
  }

  async remove(name) {
    this.confirm = '';
    this.busy = true;
    this.draw();
    this.say('Deleting ' + name + '…');
    try {
      await api('/rag/documents/' + encodeURIComponent(name), { method: 'DELETE' });
      this.say('Deleted ' + name + '.');
      await this.refresh();
    } catch (e) {
      this.say('Could not delete ' + name + ': ' + e.message);
    }
    this.busy = false;
    this.draw();
    focusKey(this.box, 'rag-drop');
  }

  /** The settings were saved: the backend or model may have changed. */
  onSettingsSaved() { this.embedder.refresh(); }

  /** The drawer closed: uploads are aborted and every timer is cleared. */
  dispose() {
    this.disposed = true;
    this.drop.dispose();
    this.index.dispose();
    this.embedder.dispose();
  }
}

/** Build the panel and start loading it. The caller keeps it to dispose of it when the drawer closes. */
export function ragPanel() {
  const panel = new RagPanel();
  panel.start();
  return panel;
}
