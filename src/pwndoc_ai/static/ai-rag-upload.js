/* pwndoc-ai: the upload queue of the knowledge base. Files go to the gateway one request each, two
 * at a time. Uploads only STORE a file; indexing is a separate, explicit step. Items are frozen
 * objects that are replaced on every change, never edited in place. */
import { uploadApi } from './ai-api.js';
import { checkFile } from './ai-rag-files.js';

const CONCURRENCY = 2;
const MAX_REASON_CHARS = 160;
const MAX_REJECTED_ROWS = 20;  // more refused files than this are only counted
const STORED = new Set(['stored', 'exists', 'duplicate']);
const FINAL = new Set(['stored', 'exists', 'duplicate', 'failed', 'rejected']);

export const isFinal = (item) => FINAL.has(item.state);
export const isActive = (item) => item.state === 'queued' || item.state === 'uploading';

/** A server message for a person: one line, no URLs or file system paths, bounded. */
export function shortReason(error) {
  const text = String((error && error.message) || 'Upload failed')
    .replace(/https?:\/\/\S+/gi, '…').replace(/(^|\s)\/(?:[\w.-]+\/)*[\w.-]+/g, '$1…').replace(/\s+/g, ' ').trim();
  return text.length > MAX_REASON_CHARS ? text.slice(0, MAX_REASON_CHARS - 1) + '…' : text;
}

/** The name of the stored file a duplicate repeats, when the gateway says which. */
const twinName = (done) => (done.status === 'duplicate' && typeof done.duplicate_of === 'string' ? shortReason({ message: done.duplicate_of }) : '');

const fileKey = (f) => f.name + '|' + f.size + '|' + f.lastModified;
const freeze = (item) => Object.freeze(item);

export class UploadQueue {
  /** @param {{getMax: () => number, onChange: () => void, onDrained: (items: object[]) => void}} hooks */
  constructor(hooks) {
    this.hooks = hooks;
    this.items = [];
    this.seq = 0;
    this.working = false;
    this.disposed = false;
    this.aborts = new Map();
  }

  /** Queue files. Returns { queued, rejected, hidden, repeated } for the caller to report. */
  add(files) {
    const known = new Set(this.items.map((i) => i.key));
    const max = this.hooks.getMax();
    const fresh = [];
    const count = { queued: 0, rejected: 0, hidden: 0, repeated: 0 };
    for (const file of files) {
      const key = fileKey(file);
      if (known.has(key)) { count.repeated += 1; continue; }
      known.add(key);
      const reason = checkFile(file, max);
      if (reason) count.rejected += 1; else count.queued += 1;
      if (reason && count.rejected > MAX_REJECTED_ROWS) { count.hidden += 1; continue; }
      this.seq += 1;
      fresh.push(freeze({
        id: this.seq, key, file, name: file.name, size: file.size, loaded: 0, total: 0, twin: '',
        state: reason ? 'rejected' : 'queued', reason,
      }));
    }
    this.items = [...this.items, ...fresh];
    if (count.queued) this.working = true;
    this.hooks.onChange();
    this.pump();
    return count;
  }

  patch(id, fields) {
    this.items = this.items.map((i) => (i.id === id ? freeze({ ...i, ...fields }) : i));
    this.hooks.onChange();
  }

  progress(id, loaded, total) {
    const item = this.items.find((i) => i.id === id);
    if (item && item.state === 'uploading') this.patch(id, { loaded, total });
  }

  pump() {
    if (this.disposed) return;
    let running = this.items.filter((i) => i.state === 'uploading').length;
    for (const item of this.items) {
      if (running >= CONCURRENCY) break;
      if (item.state !== 'queued') continue;
      running += 1;
      this.patch(item.id, { state: 'uploading', loaded: 0, total: 0, reason: '' });
      this.send(item);
    }
  }

  async send(item) {
    const control = new AbortController();
    this.aborts.set(item.id, control);
    const form = new FormData();
    form.append('file', item.file, item.name);
    try {
      const done = await uploadApi('/rag/documents', form, {
        signal: control.signal, onProgress: (l, t) => this.progress(item.id, l, t),
      });
      this.settle(item.id, STORED.has(done && done.status)
        ? { state: done.status, twin: twinName(done) } : { state: 'failed', reason: 'The gateway sent an unexpected answer.' });
    } catch (e) {
      this.settle(item.id, { state: 'failed', reason: e.aborted ? 'Cancelled.' : shortReason(e) });
    }
  }

  settle(id, fields) {
    this.aborts.delete(id);
    if (this.disposed) return;
    this.patch(id, fields);
    this.pump();
    if (this.working && !this.items.some(isActive)) {
      this.working = false;
      this.hooks.onDrained(this.items);
    }
  }

  /** Remove a waiting file, or abort the one being sent (it then shows as cancelled, with Retry). */
  cancel(id) {
    const item = this.items.find((i) => i.id === id);
    if (!item) return;
    if (item.state === 'queued') {
      this.items = this.items.filter((i) => i.id !== id);
      this.hooks.onChange();
    } else if (item.state === 'uploading' && this.aborts.has(id)) this.aborts.get(id).abort();
  }

  retry(id) {
    const item = this.items.find((i) => i.id === id);
    if (!item || item.state !== 'failed') return;
    this.working = true;
    this.patch(id, { state: 'queued', reason: '', loaded: 0, total: 0 });
    this.pump();
  }

  clearFinished() {
    this.items = this.items.filter((i) => !isFinal(i));
    this.hooks.onChange();
  }

  /** Stop everything (the panel is gone): in-flight requests are aborted, nothing new starts. */
  dispose() {
    this.disposed = true;
    for (const control of this.aborts.values()) control.abort();
    this.aborts.clear();
  }
}
