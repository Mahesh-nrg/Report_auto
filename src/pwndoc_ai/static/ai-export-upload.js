/* pwndoc-ai: the drop zone of the "Export & fonts" drawer: one .docx or .pdf, checked in the
 * browser first, sent with real upload progress (and a Cancel). The zone keeps its DOM while the
 * drawer is open, so its live regions are persistent. */
import { uploadApi } from './ai-api.js';
import { btn, h, icon } from './ai-dom.js';
import { MAX_EXPORT_DOCX_BYTES, MAX_EXPORT_PDF_BYTES, checkExportPick } from './ai-export-util.js';
import { BASE } from './ai-export-store.js';
import { fmtBytes } from './ai-rag-files.js';
import { shortReason } from './ai-rag-upload.js';
import { aborts } from './ai-state.js';

const HAS_FILES = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
const ACCEPT = '.docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export class ExportDrop {
  /** @param {{onUploaded: (answer: object) => void, engine: () => string}} hooks */
  constructor(hooks) {
    this.hooks = hooks;
    this.busy = false;
    this.disposed = false;
    this.input = h('input', { type: 'file', hidden: true, accept: ACCEPT, 'aria-hidden': 'true', tabindex: '-1' });
    this.zone = h('div', {
      class: 'ai-drop', role: 'button', tabindex: '0', 'data-fk': 'export-drop', 'aria-describedby': 'ai-export-hint',
      'aria-label': 'Choose or drop a DOCX or PDF',
    }, icon('upload'), h('span', { class: 'ai-drop-text' }, h('strong', {}, 'Drop a Word or PDF document here'),
      h('small', { id: 'ai-export-hint' }, 'Or press Enter to choose one. Word up to ' + fmtBytes(MAX_EXPORT_DOCX_BYTES)
        + ', PDF up to ' + fmtBytes(MAX_EXPORT_PDF_BYTES) + '. A PDF is converted to Word first.')));
    this.msg = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.err = h('p', { class: 'ai-item-error', role: 'alert' });
    this.progress = h('div', { class: 'ai-up-overall' });
    this.progress.hidden = true;
    this.box = h('div', { class: 'ai-rag' }, this.zone, this.input, this.progress, this.msg, this.err);
    this.guard = (e) => { if (HAS_FILES(e) && !e.composedPath().includes(this.zone)) e.preventDefault(); };
    this.wire();
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
      this.take(files);
    });
    const over = (e) => {
      if (!HAS_FILES(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      z.classList.add('ai-drop-over');
    };
    z.addEventListener('dragenter', over);
    z.addEventListener('dragover', over);
    z.addEventListener('dragleave', (e) => { if (!z.contains(e.relatedTarget)) z.classList.remove('ai-drop-over'); });
    z.addEventListener('drop', (e) => {
      if (!HAS_FILES(e)) return;
      e.preventDefault();
      z.classList.remove('ai-drop-over');
      this.take(Array.from(e.dataTransfer.files || []));
    });
    window.addEventListener('dragover', this.guard);
    window.addEventListener('drop', this.guard);
  }

  say(text, error) {
    this.msg.textContent = error ? '' : text;
    this.err.textContent = error ? text : '';
  }

  take(files) {
    if (this.busy) { this.say('An upload is already running. Wait for it or cancel it first.', true); return; }
    const problem = checkExportPick(files);
    if (problem) { this.say(problem, true); return; }
    this.send(files[0]);
  }

  showProgress(file, loaded, total) {
    const pct = total > 0 ? Math.min(100, Math.floor((loaded / total) * 100)) : 0;
    const sent = total > 0 && loaded >= total;
    const fill = h('i');
    fill.style.transform = 'scaleX(' + pct / 100 + ')';
    this.progress.replaceChildren(
      h('div', { class: 'ai-up-head' },
        h('span', { class: 'ai-doc-name' }, file.name, h('small', {}, fmtBytes(file.size) + ' · ' + (sent ? 'Checking the document…' : 'Uploading ' + pct + '%'))),
        btn('ai-ghost ai-sm', 'Cancel', () => this.cancel(), { 'data-fk': 'export-up-cancel', 'aria-label': 'Cancel upload of ' + file.name })),
      h('div', { class: 'ai-bar', role: 'progressbar', 'aria-label': 'Upload progress', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(sent ? 100 : pct) }, fill));
    this.progress.hidden = false;
  }

  cancel() {
    if (aborts.exportUpload) aborts.exportUpload.abort();
  }

  async send(file) {
    this.busy = true;
    this.say('', false);
    const control = new AbortController();
    aborts.exportUpload = control;
    this.showProgress(file, 0, 0);
    const form = new FormData();
    form.append('file', file, file.name);
    try {
      const engine = encodeURIComponent(this.hooks.engine());
      const done = await uploadApi(BASE + '/files?engine=' + engine, form, {
        signal: control.signal, onProgress: (l, t) => { if (!this.disposed) this.showProgress(file, l, t); },
      });
      if (!this.disposed) {
        this.hooks.onUploaded(done);
        this.say('Uploaded ' + file.name + '. Press “Detect sections” to analyse it.', false);
      }
    } catch (e) {
      if (!this.disposed) this.say(e.aborted ? 'Upload cancelled.' : shortReason(e), true);
    } finally {
      if (aborts.exportUpload === control) aborts.exportUpload = null;
      this.busy = false;
      this.progress.hidden = true;
      this.progress.replaceChildren();
    }
  }

  dispose() {
    this.disposed = true;
    this.cancel();
    window.removeEventListener('dragover', this.guard);
    window.removeEventListener('drop', this.guard);
  }
}
