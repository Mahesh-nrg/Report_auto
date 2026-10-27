/* pwndoc-ai: the incremental indexing run of the knowledge base: start, live progress, cancel.
 * The run lives on the gateway, so closing the panel or reloading the page never loses it: a new
 * panel asks for the status first and, when a run is going, shows it and keeps polling. */
import { api } from './ai-api.js';
import { btn, h, keepFocusKey, note, plural } from './ai-dom.js';
import { elapsedMs, endedText, normStatus, runCard } from './ai-rag-run.js';
import { POLL_MS, POLL_TIMEOUT_MS, UPLOAD_TIMEOUT_MS, pollFailure } from './ai-util.js';

const MAX_NAMES = 5000;  // the gateway's limit on a named list; more than that means "every stored file"
const NOT_THERE = 404;  // a gateway without the index routes: nothing to show, nothing to poll

export class IndexRun {
  /**
   * @param {{pendingNames: () => string[], fileCount: () => number, onEnded: () => void,
   *          onChange: () => void, say: (text: string) => void}} hooks
   */
  constructor(hooks) {
    this.hooks = hooks;
    this.status = normStatus(null);
    this.error = '';
    this.starting = false;
    this.watching = false;
    this.fails = 0;
    this.timer = null;
    this.disposed = false;
    this.firstSeen = 0;
    this.card = h('div');
    this.bar = h('div', { class: 'ai-row' });
    this.box = h('div', { class: 'ai-rag' }, this.bar, this.card);
    this.draw();
  }

  get running() { return this.status.state === 'running'; }

  alive() { return !this.disposed && this.box.isConnected; }

  /** Ask the gateway where the run stands (on open, after a reload, after a refused start). */
  async resume() {
    try {
      this.apply(await api('/rag/index/status', { timeoutMs: POLL_TIMEOUT_MS }));
      this.error = '';
    } catch (e) {
      if (e.status !== NOT_THERE) this.error = e.message;
    }
    this.draw();
  }

  apply(raw) {
    const next = normStatus(raw);
    const wasRunning = this.running;
    if (next.state === 'running' && !wasRunning) this.firstSeen = Date.now();
    if (next.state === 'running') this.watching = true;
    this.status = next;
    if (wasRunning !== this.running || this.running) this.hooks.onChange();
    clearTimeout(this.timer);
    this.timer = null;
    if (this.running) this.timer = setTimeout(() => this.poll(), POLL_MS);
    else if (this.watching) this.ended();
  }

  ended() {
    this.watching = false;
    this.fails = 0;
    this.hooks.say(endedText(this.status));
    this.hooks.onEnded();
  }

  async poll() {
    this.timer = null;
    if (!this.alive()) return;
    try {
      const raw = await api('/rag/index/status', { timeoutMs: POLL_TIMEOUT_MS });
      if (!this.alive()) return;
      this.fails = 0;
      this.error = '';
      this.apply(raw);
    } catch (e) {
      if (!this.alive()) return;
      this.pollFailed(e);
    }
    this.draw();
  }

  pollFailed(e) {
    const verdict = pollFailure({ pollFails: this.fails }, e);
    this.fails = verdict.fails || this.fails;
    if (verdict.stop) { this.error = verdict.message; this.watching = false; return; }
    this.timer = setTimeout(() => this.poll(), POLL_MS);
  }

  /** Index the named files, or (no names) every stored file; already indexed ones are skipped. */
  async begin(names) {
    if (this.starting || this.running) return;
    this.starting = true;
    this.error = '';
    this.draw();
    try {
      await api('/rag/index', { method: 'POST', body: names && names.length <= MAX_NAMES ? { names } : undefined, timeoutMs: UPLOAD_TIMEOUT_MS });
      this.watching = true;
      this.hooks.say('Indexing started.');
      await this.resume();
    } catch (e) {
      if (e.status === 409) {
        this.hooks.say('A run is already going; showing its progress.');
        await this.resume();
      } else this.error = e.message;
    }
    this.starting = false;
    this.draw();
  }

  async cancel() {
    try {
      this.apply(await api('/rag/index/cancel', { method: 'POST' }));
      this.error = '';
    } catch (e) {
      this.error = e.message;
    }
    this.draw();
  }

  /** Redraw the buttons and the card; also called by the panel when the file list changed. */
  draw() {
    if (this.disposed) return;
    keepFocusKey(this.box, () => {
      this.bar.replaceChildren(...this.controls());
      const elapsed = elapsedMs(this.status, Date.now());
      const span = Number.isNaN(elapsed) && this.running ? Date.now() - this.firstSeen : elapsed;
      const parts = [];
      if (this.error) parts.push(note('bad', this.error));
      if (this.status.state !== 'idle') parts.push(runCard(this.status, span));
      this.card.replaceChildren(...parts);
    });
  }

  controls() {
    const names = this.hooks.pendingNames();
    const off = this.starting || this.running;
    return [
      btn('ai-primary ai-sm', 'Index new files', () => this.begin(this.hooks.pendingNames()),
        { 'data-fk': 'idx-new', disabled: off || !names.length ? true : null }),
      btn('ai-ghost ai-sm', 'Re-check all', () => this.begin(null),
        { 'data-fk': 'idx-all', disabled: off || !this.hooks.fileCount() ? true : null }),
      this.running ? btn('ai-ghost ai-sm', 'Cancel indexing', () => this.cancel(), { 'data-fk': 'idx-cancel' }) : null,
      this.running && this.error ? btn('ai-ghost ai-sm', 'Check again', () => this.resume(), { 'data-fk': 'idx-again' }) : null,
      h('small', { class: 'ai-quiet' }, this.hint(names.length)),
    ].filter(Boolean);
  }

  hint(waiting) {
    if (this.running) return 'The run goes on if you close this panel.';
    return waiting ? plural(waiting, 'file') + ' waiting to be indexed'
      : 'Nothing is waiting. Files that are already indexed are never embedded twice.';
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    this.timer = null;
  }
}
