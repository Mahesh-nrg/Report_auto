/* pwndoc-ai: "Export & fonts" drawer. Upload a Word or PDF document (a PDF is converted to Word
 * first), let the AI detect its sections and block types, choose fonts and sizes per section and per
 * block type with a live preview, and download the result as Word or PDF.
 *
 * Like the DOCX drawer, it keeps its own DOM while open: each block below is redrawn only when the
 * slice of S.exp it shows was replaced (identity check), so a status poll never interrupts typing. */
import { btn, chip, h, keepFocusKey, note, plural, section, toggle } from './ai-dom.js';
import { TypePanel } from './ai-export-fonts.js';
import {
  adoptUpload, cancelAnalyse, closeExport, deleteFile, download, loadCatalog, loadList, selectFile, startAnalyse, stopPolling,
} from './ai-export-run.js';
import { SectionPanel, sectionTree } from './ai-export-sections.js';
import { cur, effectiveState, hooks, patch, selectedFile } from './ai-export-store.js';
import { ExportDrop } from './ai-export-upload.js';
import {
  ENGINES, ENGINE_LABEL, PDF2DOCX_NOTE, isFileId, isRunning, methodText, parseTerms, reportRows, reportWarnings,
  sectionWarningText,
} from './ai-export-util.js';
import { fmtBytes } from './ai-rag-files.js';
import { S, storeExport, ui } from './ai-state.js';

const TITLE = 'Export & fonts';
const UNAVAILABLE = 'The Export tab is not installed on the gateway. An administrator can add it with: pip install "pwndoc-ai[docx]", then restart the gateway.';
const STATE_LABEL = { uploaded: 'Uploaded', converting: 'Converting', analysing: 'Detecting sections', ready: 'Ready', error: 'Failed', cancelled: 'Cancelled' };
const STATE_TONE = { ready: 'ok', error: 'bad', cancelled: 'warn', converting: 'accent', analysing: 'accent', uploaded: 'mute' };

class Block {
  constructor(el, keys, build) { this.el = el; this.keys = keys; this.build = build; this.last = null; }

  update(E) {
    const next = this.keys(E);
    if (this.last && next.length === this.last.length && next.every((v, i) => v === this.last[i])) return;
    this.last = next;
    keepFocusKey(this.el, () => this.el.replaceChildren(...[].concat(this.build(E)).filter(Boolean)));
  }
}

const engineUnavailable = (E) => E.setup.engine === 'pdf2docx' && !!E.engines && !(E.engines.info.pdf2docx || {}).available;

function headView(E) {
  const file = selectedFile(E);
  if (!file) return null;
  const state = effectiveState(E);
  return [h('h3', { class: 'ai-big ai-card-title' }, file.name),
    h('p', { class: 'ai-quiet' }, (file.kind === 'pdf' ? 'PDF' : 'Word') + ' · ' + fmtBytes(file.size) + ' ',
      state ? chip(STATE_TONE[state] || 'mute', STATE_LABEL[state] || state) : null)];
}

function progressView(E) {
  const st = E.status || { done: 0, total: 0, step: '' };
  const step = st.step === 'converting' ? 'Converting the PDF to Word…' : 'Detecting sections…';
  const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
  const fill = h('i');
  fill.style.transform = 'scaleX(' + pct / 100 + ')';
  return h('div', { class: 'ai-progress' },
    h('div', { class: 'ai-progress-top' }, h('strong', {}, step), h('span', { class: 'ai-quiet' }, st.total ? st.done + ' of ' + st.total : 'Starting…')),
    h('div', {
      class: 'ai-bar', role: 'progressbar', 'aria-label': 'Analysis progress', 'data-fk': 'export-progress', 'aria-valuemin': '0',
      'aria-valuemax': '100', 'aria-valuenow': String(pct), 'aria-valuetext': step,
    }, fill),
    btn('ai-ghost ai-sm', 'Cancel', cancelAnalyse, { 'data-fk': 'export-cancel' }));
}

function runView(E) {
  const state = effectiveState(E);
  if (isRunning(state)) return progressView(E);
  const file = selectedFile(E);
  const message = (E.status && E.status.error) || (file && file.error) || '';
  if (state === 'error' && file && file.errorCode === 'scanned') {
    return h('div', { 'data-fk': 'export-notice' }, note('warn', message || 'This PDF has no text layer (scanned images). OCR is not installed on this server.'));
  }
  if (state === 'error') return note('bad', 'The analysis failed' + (message ? ': ' + message : '.'));
  if (state === 'cancelled') return note('warn', 'The analysis was cancelled.');
  return null;
}

function reportView(E) {
  const report = E.detail && E.detail.report;
  const rows = reportRows(report);
  if (!rows.length) return null;
  return h('section', { class: 'ai-stack-tight', 'data-fk': 'export-report', 'aria-label': 'Conversion report' },
    h('h4', { class: 'ai-label' }, 'PDF conversion'),
    h('dl', { class: 'ai-report' }, rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])),
    reportWarnings(report).map((t) => note('warn', t)));
}

function sectionsHead(E) {
  const d = E.detail.sections;
  return [h('p', { class: 'ai-quiet' }, methodText(d.method, d.model) + ' · ' + plural(d.sections.length, 'section') + ' · ' + plural(d.blocks.size, 'block')),
    ...d.warnings.map((w) => note(w === 'llm_key' ? 'bad' : 'warn', sectionWarningText(w)))];
}

function filesView(E) {
  if (E.files === null) return h('p', { class: 'ai-quiet' }, 'Loading…');
  if (!E.files.length) return h('p', { class: 'ai-quiet' }, 'No files yet.');
  return h('ul', { class: 'ai-plain-list ai-exp-files', 'aria-label': 'My export files' }, E.files.map((f) => h('li', {},
    btn('ai-link', f.name, () => selectFile(f.id), { 'data-fk': 'export-open-' + f.id, 'aria-current': f.id === E.selectedId ? 'true' : null }),
    h('small', { class: 'ai-quiet' }, ' ' + (f.kind === 'pdf' ? 'PDF' : 'Word') + ' · ' + fmtBytes(f.size) + ' · ' + (STATE_LABEL[f.state] || f.state)))));
}

function downloadView(E) {
  if (!E.detail || !E.plan) return null;
  const busy = E.dl.busy;
  const dl = (format, label) => btn(format === 'docx' ? 'ai-primary' : 'ai-ghost', busy === format ? 'Preparing…' : label,
    () => download(format), { 'data-fk': 'export-download-' + format, disabled: busy ? true : null });
  return [h('div', { class: 'ai-row' }, dl('docx', 'Download DOCX'), dl('pdf', 'Download PDF')),
    E.dl.info ? note('ok', E.dl.info) : null];
}

function deleteView(E) {
  if (!E.selectedId || isRunning(effectiveState(E))) return null;
  if (E.confirmId !== E.selectedId) {
    return btn('ai-ghost ai-sm', 'Delete this file', () => patch({ confirmId: E.selectedId }), { 'data-fk': 'export-delete' });
  }
  return note('warn', h('p', { class: 'ai-confirm-text' }, 'Delete this file and its converted copy from the gateway?'),
    h('div', { class: 'ai-row' },
      btn('ai-primary ai-sm', 'Delete', () => deleteFile(E.selectedId), { 'data-fk': 'export-delete-confirm' }),
      btn('ai-ghost ai-sm', 'Keep it', () => patch({ confirmId: '' }), { 'data-fk': 'export-delete-keep' })));
}

class ExportPanel {
  constructor() {
    this.drop = new ExportDrop({ onUploaded: (answer) => { stopPolling(); adoptUpload(answer); }, engine: () => cur().setup.engine });
    this.notes = h('div', { class: 'ai-notes' });
    this.saveState = h('span', { class: 'ai-foot-info', 'aria-live': 'polite' });
    this.engineNote = h('div');
    this.analyse = btn('ai-primary', 'Detect sections', startAnalyse, { 'data-fk': 'export-analyse' });
    this.sectionPanel = new SectionPanel();
    this.typePanel = new TypePanel();
    this.treeHost = h('div');
    const el = () => h('div');
    const [head, run, report, secHead, dls, del, files] = [el(), el(), el(), el(), el(), el(), el()];
    this.sectionsBox = section('Sections and fonts', secHead, this.treeHost, this.sectionPanel.box, this.typePanel.box);
    this.card = h('section', { class: 'ai-stack ai-card', 'aria-label': 'Selected file' }, head, run, this.analyse, report, this.sectionsBox, dls, del);
    this.blocks = [
      new Block(head, (E) => [selectedFile(E), effectiveState(E)], headView),
      new Block(run, (E) => [effectiveState(E), E.status, selectedFile(E)], runView),
      new Block(report, (E) => [E.detail], reportView),
      new Block(secHead, (E) => [E.detail], (E) => (E.detail ? sectionsHead(E) : null)),
      new Block(this.treeHost, (E) => [E.detail, E.focusSection, E.plan], (E) => (E.detail && E.plan ? sectionTree(E, this.treeHost) : null)),
      new Block(dls, (E) => [E.detail, E.plan, E.dl], downloadView),
      new Block(del, (E) => [E.selectedId, E.confirmId, effectiveState(E)], deleteView),
      new Block(files, (E) => [E.files, E.selectedId], filesView),
    ];
    this.body = h('div', { class: 'ai-stack' },
      h('div', { class: 'ai-stack-tight' }, h('p', { class: 'ai-lead' }, 'Change the fonts of a Word or PDF document, section by section.'),
        h('p', { class: 'ai-quiet' }, 'The AI labels every paragraph (title, headings, body, tables, code…) and groups them into sections. It never changes the text. Client names you list are masked before anything reaches the model.')),
      this.notes, this.setupView(), this.drop.box, this.card, section('My files', files));
    this.unavailable = !!(S.status && S.status.export_tab === false);
    if (this.unavailable) this.body.replaceChildren(note('warn', UNAVAILABLE));
  }

  setupView() {
    const E = cur();
    this.engine = h('select', { class: 'ai-input', 'data-fk': 'export-engine', 'aria-label': 'PDF conversion engine' });
    this.engine.addEventListener('change', () => patch({ setup: { ...cur().setup, engine: this.engine.value } }));
    this.fillEngines(E);
    const ai = toggle('Detect sections with AI', 'Off: rules only (style names, sizes, bold text).', E.setup.useAi,
      (e) => patch({ setup: { ...cur().setup, useAi: e.target.checked } }), 'export-use-ai');
    const terms = h('textarea', { class: 'ai-input', rows: '2', spellcheck: 'false', 'data-fk': 'export-terms', 'aria-label': 'Client names to mask', placeholder: 'One per line or comma separated' });
    terms.value = E.setup.terms;
    terms.addEventListener('input', () => patch({ setup: { ...cur().setup, terms: terms.value } }));
    return section('Before you start',
      h('label', { class: 'ai-field' }, h('span', { class: 'ai-field-label' }, 'PDF conversion engine'), this.engine,
        h('small', {}, 'Used only for PDF files.')), this.engineNote, ai,
      h('label', { class: 'ai-field' }, h('span', { class: 'ai-field-label' }, 'Client names to mask (optional)'), terms));
  }

  fillEngines(E) {
    const info = (E.engines && E.engines.info) || {};
    const label = (name) => ENGINE_LABEL[name] + (name !== 'auto' && info[name] && !info[name].available ? ' (not installed)' : '');
    this.engine.replaceChildren(...ENGINES.map((name) => h('option', { value: name }, label(name))));
    this.engine.value = E.setup.engine;
    this.enginesFor = E.engines;
  }

  drawSetup(E) {
    if (this.enginesFor !== E.engines) this.fillEngines(E);
    const unavailable = engineUnavailable(E);
    const key = E.setup.engine + '|' + unavailable;
    if (key !== this.engineKey) {
      this.engineKey = key;
      const text = unavailable ? 'The layout-faithful engine is not installed: pip install "pwndoc-ai[pdfimport]". ' + PDF2DOCX_NOTE
        : E.setup.engine === 'pdf2docx' ? PDF2DOCX_NOTE : '';
      this.engineNote.replaceChildren(text ? note(unavailable ? 'warn' : 'ok', text) : '');
    }
    const file = selectedFile(E);
    const state = effectiveState(E);
    this.analyse.hidden = !file || isRunning(state);
    this.analyse.textContent = E.starting ? 'Starting…' : state === 'ready' ? 'Detect sections again' : 'Detect sections';
    this.analyse.disabled = E.starting || !!parseTerms(E.setup.terms).error || (file && file.kind === 'pdf' && unavailable);
  }

  drawNotes(E) {
    const texts = [E.listError, E.error, E.detailError, E.saveError, E.dl.error, parseTerms(E.setup.terms).error].filter(Boolean);
    const key = texts.join('\n') + '|' + E.notice;
    if (key === this.notesKey) return false;
    this.notesKey = key;
    this.notes.replaceChildren(...texts.map((t) => note('bad', t)), ...(E.notice ? [h('p', { class: 'ai-quiet', role: 'status' }, E.notice)] : []));
    return true;
  }

  draw() {
    if (this.unavailable) return;
    const E = cur();
    const changed = this.drawNotes(E);
    this.drawSetup(E);
    this.card.hidden = !E.selectedId;
    this.sectionsBox.hidden = !(E.detail && E.plan);
    for (const block of this.blocks) block.update(E);
    if (E.detail && E.plan) this.sectionPanel.update(E);
    this.typePanel.update(E);
    this.saveState.textContent = E.saving ? 'Saving font choices…' : E.detail ? 'Font choices are saved on the gateway.' : '';
    if (changed) ui.renderBody('export');
  }

  dispose() { this.drop.dispose(); }
}

let panel = null;

export function prepareExport() {
  panel = new ExportPanel();
  hooks.redraw = () => { if (panel) panel.draw(); };
  panel.draw();
  ui.renderBody('export');
  if (panel.unavailable) return;
  loadCatalog();
  loadList();
  if (cur().selectedId) selectFile(cur().selectedId);
}

export function viewExport() {
  return { title: TITLE, body: panel.body, foot: [panel.saveState] };
}

export function disposeExport() {
  closeExport();
  hooks.redraw = () => {};
  if (panel) panel.dispose();
  panel = null;
}

/** An analysis that was running when the page reloaded: picked up when the drawer opens. */
export function resumeExport(saved) {
  if (!isFileId(saved.id)) { storeExport(null); return; }
  S.exp = { ...S.exp, selectedId: saved.id };
}
