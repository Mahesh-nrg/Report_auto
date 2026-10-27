/* pwndoc-ai: the drop zone of the "Refine a DOCX" drawer: exactly one .docx, checked in the browser
 * first, sent with real upload progress. The zone, its status lines and the progress row keep
 * their DOM for as long as the drawer is open (persistent live regions). */
import { uploadApi } from './ai-api.js';
import { btn, h, icon } from './ai-dom.js';
import { MAX_DOCX_BYTES, checkDocxPick } from './ai-docx-util.js';
import { shortReason } from './ai-rag-upload.js';
import { fmtBytes } from './ai-rag-files.js';
import { aborts } from './ai-state.js';

const HAS_FILES = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export class DocxDrop {
  /** @param {{onUploaded: (answer: object) => void}} hooks */
  constructor(hooks) {
    this.hooks = hooks;
    this.busy = false;
    this.disposed = false;
    this.input = h('input', { type: 'file', hidden: true, accept: '.docx,' + DOCX_TYPE, 'aria-hidden': 'true', tabindex: '-1' });
    this.hint = h('small', { id: 'ai-docx-hint' },
      'Or click to choose a file. One Word (.docx) report, up to ' + fmtBytes(MAX_DOCX_BYTES) + '. It stays on the gateway until you delete it.');
    this.zone = h('div', {
      class: 'ai-drop', role: 'button', tabindex: '0', 'data-fk': 'docx-drop', 'aria-describedby': 'ai-docx-hint',
      'aria-label': 'Add a Word report: drop a .docx file here, or press Enter to choose a file',
    }, icon('upload'), h('span', { class: 'ai-drop-text' }, h('strong', {}, 'Drop an existing DOCX report here'), this.hint));
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
    z.addEventListener('dragenter', (e) => this.over(e));
    z.addEventListener('dragover', (e) => this.over(e));
    z.addEventListener('dragleave', (e) => { if (!z.contains(e.relatedTarget)) z.classList.remove('ai-drop-over'); });
    z.addEventListener('drop', (e) => this.dropped(e));
    // A file that misses the zone must not navigate the page away from the report.
    window.addEventListener('dragover', this.guard);
    window.addEventListener('drop', this.guard);
  }

  over(e) {
    if (!HAS_FILES(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    this.zone.classList.add('ai-drop-over');
  }

  dropped(e) {
    if (!HAS_FILES(e)) return;
    e.preventDefault();
    this.zone.classList.remove('ai-drop-over');
    this.take(Array.from(e.dataTransfer.files || []));
  }

  say(text, error) {
    this.msg.textContent = error ? '' : text;
    this.err.textContent = error ? text : '';
  }

  take(files) {
    if (this.busy) { this.say('An upload is already running. Wait for it or cancel it first.', true); return; }
    const problem = checkDocxPick(files);
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
        btn('ai-ghost ai-sm', 'Cancel', () => this.cancel(), { 'data-fk': 'docx-up-cancel', 'aria-label': 'Cancel upload of ' + file.name })),
      h('div', {
        class: 'ai-bar', role: 'progressbar', 'aria-label': 'Upload progress', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(sent ? 100 : pct),
      }, fill));
    this.progress.hidden = false;
  }

  cancel() {
    if (aborts.docxUpload) aborts.docxUpload.abort();
  }

  async send(file) {
    this.busy = true;
    this.say('', false);
    const control = new AbortController();
    aborts.docxUpload = control;
    this.showProgress(file, 0, 0);
    const form = new FormData();
    form.append('file', file, file.name);
    try {
      const done = await uploadApi('/docx/files', form, {
        signal: control.signal, onProgress: (l, t) => { if (!this.disposed) this.showProgress(file, l, t); },
      });
      if (!this.disposed) {
        this.hooks.onUploaded(done);
        this.say('Uploaded ' + file.name + '. Choose what to refine below.', false);
      }
    } catch (e) {
      if (!this.disposed) this.say(e.aborted ? 'Upload cancelled.' : shortReason(e), true);
    } finally {
      if (aborts.docxUpload === control) aborts.docxUpload = null;
      this.busy = false;
      this.progress.hidden = true;
      this.progress.replaceChildren();
    }
  }

  /** The drawer closed: an upload in flight is aborted and the page's drag guards are removed. */
  dispose() {
    this.disposed = true;
    this.cancel();
    window.removeEventListener('dragover', this.guard);
    window.removeEventListener('drop', this.guard);
  }
}
