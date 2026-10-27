/* pwndoc-ai: the embedder line of the knowledge base. Everything that is embedded (your documents
 * and internet search results) goes through ONE small Ollama that the gateway starts only when an
 * embedding is needed and stops when it has been idle. This panel only describes and prepares it. */
import { api } from './ai-api.js';
import { btn, chip, h, keepFocusKey, note } from './ai-dom.js';
import { POLL_TIMEOUT_MS, UPLOAD_TIMEOUT_MS } from './ai-util.js';

const MAX_TEXT = 300;
const NOT_THERE = 404;  // a gateway without the embedder routes
const WATCH_MS = 3000;      // while the model downloads in the background, ask again this often
const MAX_WATCHES = 400;    // about 20 minutes
const STATES = new Set(['idle', 'preparing', 'ready', 'error']);
const OLLAMA_TEXT = 'Ollama starts only when something needs embedding (indexing a file, or turning internet search '
  + 'results into context) and stops itself when it has been idle. All embedding goes through this one embedder.';
const BUILTIN_TEXT = 'The built-in model gives the same vectors as Ollama all-minilm, so the existing index keeps '
  + 'working and nothing has to be indexed again if you switch.';

const words = (v) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT) : '');

/** The gateway's embedder status as a validated plain object. */
export function normEmbedder(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  return {
    backend: r.backend === 'ollama' ? 'ollama' : 'onnx', model: words(r.model), space: words(r.space),
    installed: r.installed === true, running: r.running === true, modelPresent: r.model_present === true,
    detail: words(r.detail), warning: words(r.warning), state: STATES.has(r.state) ? r.state : 'idle',
  };
}

export class EmbedderStatus {
  /** @param {{say: (text: string) => void}} hooks */
  constructor(hooks) {
    this.hooks = hooks;
    this.info = null;
    this.state = 'loading';  // loading | ready | missing | failed
    this.error = '';
    this.busy = false;
    this.watches = 0;
    this.timer = null;
    this.disposed = false;
    this.box = h('div', { class: 'ai-embedder', tabindex: '-1' });
    this.draw();
  }

  alive() { return !this.disposed && this.box.isConnected; }

  async refresh() {
    try {
      const raw = await api('/rag/embedder', { timeoutMs: POLL_TIMEOUT_MS });
      if (!this.alive()) return;
      this.info = normEmbedder(raw);
      this.state = 'ready';
      this.error = '';
    } catch (e) {
      if (!this.alive()) return;
      this.state = e.status === NOT_THERE ? 'missing' : 'failed';
      this.error = e.message;
    }
    this.afterStatus();
  }

  /** The gateway downloads the model in the background: keep asking until it is done. */
  afterStatus() {
    clearTimeout(this.timer);
    this.timer = null;
    const going = this.state === 'ready' && this.info.state === 'preparing';
    if (going && this.watches < MAX_WATCHES) {
      this.watches += 1;
      this.timer = setTimeout(() => this.refresh(), WATCH_MS);
    }
    if (!going && this.watches) {
      this.watches = 0;
      if (this.info && this.info.modelPresent) this.hooks.say('The embedder is ready.');
    }
    this.draw();
  }

  async prepare() {
    if (this.busy) return;
    this.busy = true;
    this.error = '';
    this.draw('emb-prepare');
    try {
      const raw = await api('/rag/embedder/prepare', { method: 'POST', timeoutMs: UPLOAD_TIMEOUT_MS });
      if (!this.alive()) return;
      this.info = normEmbedder(raw);
    } catch (e) {
      if (!this.alive()) return;
      this.error = e.message;
    }
    this.busy = false;
    this.afterStatus();
    if (this.alive() && !this.needsPrepare()) this.box.focus({ preventScroll: true });
  }

  working() { return this.busy || (!!this.info && this.info.state === 'preparing'); }

  needsPrepare() { return !!this.info && this.info.backend === 'ollama' && this.info.installed && !this.info.modelPresent; }

  chips(i) {
    if (i.backend !== 'ollama') return [];
    return [
      i.installed ? chip('ok', 'Installed') : chip('warn', 'Not installed'),
      i.running ? chip('accent', 'Running') : chip('mute', 'Stopped'),
      i.modelPresent ? chip('ok', 'Model ready') : chip('warn', 'Model missing'),
    ];
  }

  body() {
    if (this.state === 'loading') return [h('p', { class: 'ai-quiet' }, 'Checking the embedder…')];
    if (this.state === 'missing') return [h('p', { class: 'ai-quiet' }, 'Embedder status is not available on this gateway.')];
    if (this.state === 'failed') return [h('p', { class: 'ai-quiet' }, 'Could not read the embedder status: ' + this.error)];
    const i = this.info;
    const label = i.backend === 'ollama' ? 'Ollama · ' + (i.model || 'model') : 'Built-in model (ONNX)' + (i.model ? ' · ' + i.model : '');
    return [
      h('p', {}, h('strong', {}, 'Embeddings: '), label),
      h('p', { class: 'ai-quiet' }, i.backend === 'ollama' ? OLLAMA_TEXT : BUILTIN_TEXT),
      this.chips(i).length ? h('div', { class: 'ai-row' }, this.chips(i)) : null,
      i.detail ? h('p', { class: 'ai-item-meta' }, i.detail) : null,
      i.warning ? note('warn', i.warning) : null,
      this.working() ? h('p', { class: 'ai-quiet' }, 'Preparing the embedder: the model is downloaded once and this can take a few minutes.') : null,
      this.error ? note('bad', this.error) : null,
      i.state === 'error' && !this.working() && !this.error ? note('bad', i.detail || 'The embedder could not be prepared.') : null,
      this.needsPrepare()
        ? btn('ai-ghost ai-sm', this.working() ? 'Preparing…' : 'Prepare embedder', () => this.prepare(), { 'data-fk': 'emb-prepare', 'aria-label': 'Prepare embedder', disabled: this.working() ? true : null })
        : null,
    ];
  }

  draw(focus) {
    if (this.disposed) return;
    keepFocusKey(this.box, () => this.box.replaceChildren(...this.body().filter(Boolean)));
    if (focus) this.box.querySelector('[data-fk="' + focus + '"]')?.focus({ preventScroll: true });
  }

  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    this.timer = null;
  }
}
