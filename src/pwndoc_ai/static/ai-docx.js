/* pwndoc-ai: "Refine a DOCX report" drawer: upload an existing Word report, let the gateway refine
 * its text in place, review the changes and download the result (Word, tracked changes, PDF).
 *
 * The drawer keeps its own DOM while it is open (like AI Settings): typing a client name, checking
 * a section or dragging a file is never interrupted by a status poll. Each block below is redrawn
 * only when the slice of S.docx it shows was replaced (identity check). */
import { btn, h, keepFocusKey, note, plural, section } from './ai-dom.js';
import { resultView } from './ai-docx-changes.js';
import { adoptUpload, loadList } from './ai-docx-files.js';
import { listKeys, listView } from './ai-docx-list.js';
import { SetupForm } from './ai-docx-outline.js';
import { cancelRun, progressView, selectFile, startRefine, stopPolling } from './ai-docx-run.js';
import { cur, effectiveState, hooks, patch } from './ai-docx-store.js';
import { DocxDrop } from './ai-docx-upload.js';
import { isFileId, parseTerms, pickedTotals } from './ai-docx-util.js';
import { fmtBytes } from './ai-rag-files.js';
import { root } from './ai-shell.js';
import { S, storeDocx, ui } from './ai-state.js';
import { llmMissing, notConfigured } from './ai-widgets.js';

const TITLE = 'Refine a DOCX report';
const UNAVAILABLE = 'DOCX refinement is not installed on the gateway. An administrator can add it with: pip install "pwndoc-ai[docx]", then restart the gateway.';
const START_LABEL = { uploaded: 'Start refining', done: 'Start refining', error: 'Try again', cancelled: 'Try again' };

const showsSetup = (D, state) => !!D.detail && state !== 'running' && (D.showSetup === null ? state !== 'done' : D.showSetup);

function canStart(D) {
  const setup = D.setup;
  if (!setup || D.starting || llmMissing() || parseTerms(setup.terms).error) return false;
  return setup.scope === 'pick' ? pickedTotals(D.detail.outline, setup.picked).units > 0 : D.detail.stats.refinable > 0;
}

/** What the footer offers: nothing, Cancel (running), Start (setup shown) or Refine again (result shown). */
function footSpec(D) {
  const state = effectiveState(D);
  if (!D.detail) return { key: '', nodes: [] };
  if (D.starting) return { key: 'starting', nodes: [btn('ai-primary', 'Starting…', startRefine, { disabled: true, 'data-fk': 'docx-start' })] };
  if (state === 'running') return { key: 'cancel', nodes: [btn('ai-ghost', 'Cancel run', cancelRun, { 'data-fk': 'docx-cancel' })] };
  if (showsSetup(D, state)) {
    const label = D.starting ? 'Starting…' : START_LABEL[state] || 'Start refining';
    const disabled = !canStart(D);
    return { key: 'start|' + label + '|' + disabled, nodes: [btn('ai-primary', label, startRefine, { disabled: disabled ? true : null, 'data-fk': 'docx-start' })] };
  }
  if (state === 'done') return { key: 'again', nodes: [btn('ai-ghost', 'Refine again', () => patch({ showSetup: true }), { 'data-fk': 'docx-again' })] };
  return { key: '', nodes: [] };
}

/** A container whose content is rebuilt only when one of the values it depends on changed. */
class Block {
  constructor(el, keys, build) { this.el = el; this.keys = keys; this.build = build; this.last = null; }

  /** Returns true when it redrew. */
  update(D) {
    const next = this.keys(D);
    if (this.last && next.length === this.last.length && next.every((v, i) => v === this.last[i])) return false;
    this.last = next;
    keepFocusKey(this.el, () => this.el.replaceChildren(...[].concat(this.build(D)).filter(Boolean)));
    return true;
  }
}

function headView(D) {
  if (!D.detail) return h('p', { class: 'ai-quiet' }, D.detailError ? '' : 'Loading the document…');
  const { file, stats } = D.detail;
  return [h('h3', { class: 'ai-big ai-card-title' }, file.name),
    h('p', { class: 'ai-quiet' }, [fmtBytes(file.size), plural(stats.sections, 'section'), plural(stats.refinable, 'paragraph') + ' can be refined',
      plural(stats.tables, 'table'), plural(stats.images, 'image')].join(' · '))];
}

function runView(D, state) {
  if (state === 'running') return progressView(D);
  const message = D.status && D.status.error;
  if (state === 'error') return note('bad', 'The last run failed' + (message ? ': ' + message : '.'));
  return state === 'cancelled' ? note('warn', 'The last run was cancelled.') : null;
}

class DocxPanel {
  constructor() {
    this.drop = new DocxDrop({ onUploaded: (answer) => { stopPolling(); adoptUpload(answer); } });
    this.form = null;
    this.formFor = null;
    this.notes = h('div', { class: 'ai-notes' });
    this.status = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.foot = h('div', { class: 'ai-foot-slot' });
    this.footKey = '';
    const [head, setup, run, result, list] = [h('div'), h('div'), h('div'), h('div'), h('div')];
    this.card = h('section', { class: 'ai-stack ai-card', 'aria-label': 'Selected document' }, head, setup, run, result);
    this.blocks = this.buildBlocks({ head, setup, run, result, list });
    this.main = h('div', { class: 'ai-stack' },
      h('div', { class: 'ai-stack-tight' },
        h('p', { class: 'ai-lead' }, 'Refine an existing Word report in place.'),
        h('p', { class: 'ai-quiet' }, 'Upload any .docx report (it does not have to come from PwnDoc). The AI edits the text paragraph by paragraph and keeps your layout, images, tables and styles. Download the result as Word, Word with tracked changes, or PDF. Uploaded and refined files stay on the gateway until you delete them.')),
      this.notes, this.drop.box, this.card, section('My documents', this.status, list));
    this.body = h('div', { class: 'ai-stack' }, this.main);
    this.unavailable = S.status && S.status.docx_refine === false;
    if (this.unavailable) this.body.replaceChildren(note('warn', UNAVAILABLE));
  }

  buildBlocks({ head, setup, run, result, list }) {
    const state = effectiveState;
    return [
      new Block(head, (D) => [D.detail, D.detailError], headView),
      new Block(setup, (D) => [showsSetup(D, state(D)), D.detail && D.detail.outline, llmMissing()], (D) => this.setupView(D, state(D))),
      new Block(run, (D) => [state(D), D.status, D.starting], (D) => runView(D, state(D))),
      new Block(result, (D) => [state(D) === 'done' && !!D.detail, D.detail, D.changes, D.dl], (D) => (state(D) === 'done' && D.detail ? resultView(D) : null)),
      new Block(list, listKeys, listView),
    ];
  }

  setupView(D, state) {
    if (!showsSetup(D, state)) return null;
    if (this.formFor !== D.detail.outline) { this.form = new SetupForm(D.detail); this.formFor = D.detail.outline; }
    return [llmMissing() ? notConfigured() : null, this.form.box];
  }

  drawNotes(D) {
    const texts = [D.listError, D.actionError, D.pollError, D.dl.error, D.detailError].filter(Boolean);
    const key = texts.join('\n');
    if (key === this.notesKey) return false;
    this.notesKey = key;
    this.notes.replaceChildren(...texts.map((t) => note('bad', t)));
    return true;
  }

  drawFoot(D) {
    const spec = footSpec(D);
    if (spec.key === this.footKey) return;
    this.footKey = spec.key;
    const hadFocus = this.foot.contains(root.activeElement);
    this.foot.replaceChildren(...spec.nodes);
    const first = this.foot.querySelector('button');
    if (hadFocus && first) first.focus({ preventScroll: true });
  }

  draw() {
    if (this.unavailable) return;
    const D = cur();
    const changed = this.drawNotes(D);
    this.card.hidden = !D.selectedId;
    this.status.textContent = D.notice;
    for (const block of this.blocks) block.update(D);
    this.drawFoot(D);
    if (changed) ui.renderBody('docx');  // the shell announces notes that came or went
  }

  dispose() {
    this.drop.dispose();
  }
}

let panel = null;

export function prepareDocx() {
  panel = new DocxPanel();
  hooks.redraw = () => { if (panel) panel.draw(); };
  panel.draw();
  ui.renderBody('docx');
  if (panel.unavailable) return;
  loadList();
  if (cur().selectedId) selectFile(cur().selectedId);
}

export function viewDocx() {
  return { title: TITLE, body: panel.body, foot: [panel.foot] };
}

/** The drawer closed: polling stops and an upload in flight is aborted. The run itself keeps going. */
export function disposeDocx() {
  stopPolling();
  hooks.redraw = () => {};
  if (panel) panel.dispose();
  panel = null;
}

/** A run that was going when the page reloaded: the drawer picks it up when it is opened. */
export function resumeDocx(saved) {
  if (!isFileId(saved.id)) { storeDocx(null); return; }
  S.docx = { ...S.docx, selectedId: saved.id.toLowerCase() };
}
