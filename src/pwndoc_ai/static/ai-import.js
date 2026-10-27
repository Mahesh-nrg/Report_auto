/* pwndoc-ai: "Import a DOCX into PwnDoc" drawer. Upload a prefilled report made from the active
 * PwnDoc template; the gateway reads it into an editable preview; nothing is written to PwnDoc
 * until "Create in PwnDoc" is confirmed; "Undo this import" deletes exactly what it created.
 *
 * Like the Refine drawer it keeps its own DOM while open: each block is redrawn only when the
 * part of S.imp it shows was replaced, so a status poll never interrupts typing. */
import { btn, chip, choice, focusKey, h, icon, keepFocusKey, note, section } from './ai-dom.js';
import { PreviewForm } from './ai-import-form.js';
import {
  analyse, cancelStep, discardImport, imageUrl, loadList, openImport, pickFile, resumeImport, saveEdits,
  startCreate, startUndo, stopPolling, revokeImages,
} from './ai-import-run.js';
import { cur, hooks, patch, storeImport } from './ai-import-state.js';
import {
  LEVEL_LABEL, LEVEL_TONE, fieldNames, PHASE_LABEL, STATE_LABEL, STATE_TONE, confirmText, createGate, createSummary,
  isRunning, issueTarget, keptOtherText, keptText, plural, undoCounts, warnings,
} from './ai-import-util.js';
import { MAX_DOCX_BYTES } from './ai-docx-util.js';
import { fmtBytes } from './ai-rag-files.js';
import { root } from './ai-shell.js';
import { ui } from './ai-state.js';
import { fmtTime } from './ai-util.js';
import { llmMissing } from './ai-widgets.js';

const TITLE = 'Import a DOCX into PwnDoc';
const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const HAS_FILES = (e) => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
const LEVELS = ['blocker', 'warning', 'info'];
const COUNT_TEXT = { blocker: (n) => n + ' required', warning: (n) => n + ' to check', info: (n) => plural(n, 'note') };
const STEP_FOCUS = { pick: 'import-pick', confirm: 'import-create-no', analysing: 'import-cancel', creating: 'import-cancel', undoing: 'import-step-progress' };

/** Focus a control by data-fk and bring it into view (focusKey alone does not scroll). */
function focusTo(scope, key) {
  if (!focusKey(scope, key)) return false;
  const el = root.activeElement;
  if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  return true;
}

/** A button that stays focusable while it cannot be used (aria-disabled), so focus is never lost. */
function gatedBtn(cls, label, onclick, fk, off, extra) {
  return btn(cls, label, () => { if (!off) onclick(); }, { 'data-fk': fk, 'aria-disabled': off ? 'true' : null, ...(extra || {}) });
}

class Block {
  constructor(keys, build) { this.el = h('div', { class: 'ai-stack' }); this.keys = keys; this.build = build; this.last = null; }

  update(I) {
    const next = this.keys(I);
    if (this.last && next.length === this.last.length && next.every((v, i) => v === this.last[i])) return;
    this.last = next;
    keepFocusKey(this.el, () => this.el.replaceChildren(...[].concat(this.build(I)).filter(Boolean)));
    this.el.hidden = !this.el.firstChild;
  }
}

/* ---------- pick: the drop zone and the AI switch ---------- */

class PickArea {
  constructor() {
    this.input = h('input', { type: 'file', hidden: true, accept: '.docx,' + DOCX_TYPE, 'aria-hidden': 'true', tabindex: '-1' });
    this.zone = h('button', { type: 'button', class: 'ai-drop ai-import-drop', 'data-fk': 'import-pick', 'aria-describedby': 'ai-imp-hint' },
      icon('upload'), h('span', { class: 'ai-drop-text' }, h('strong', {}, 'Choose a .docx report')));
    this.llm = choice('checkbox', 'import-llm', 'Use AI to fill missing fields', true, (e) => patch({ useLlm: e.target.checked }), 'import-use-llm',
      'Only for fields the reader could not fill. Client names, domains and IP addresses in finding text are swapped for fictional stand-ins before they reach the model.');
    this.box = h('div', { class: 'ai-rag' }, this.zone,
      h('small', { id: 'ai-imp-hint', class: 'ai-quiet' }, 'Or drop it here. One Word report made from the active PwnDoc template, up to '
        + fmtBytes(MAX_DOCX_BYTES) + '. The report is kept in Refine a DOCX › My documents until you delete it there.'),
      this.input, this.llm);
    this.guard = (e) => { if (HAS_FILES(e) && !e.composedPath().includes(this.zone)) e.preventDefault(); };
    this.wire();
  }

  wire() {
    this.zone.addEventListener('click', () => this.input.click());
    this.input.addEventListener('change', () => { const files = Array.from(this.input.files || []); this.input.value = ''; pickFile(files); });
    const over = (e) => { if (!HAS_FILES(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; this.zone.classList.add('ai-drop-over'); };
    this.zone.addEventListener('dragenter', over);
    this.zone.addEventListener('dragover', over);
    this.zone.addEventListener('dragleave', () => this.zone.classList.remove('ai-drop-over'));
    this.zone.addEventListener('drop', (e) => {
      if (!HAS_FILES(e)) return;
      e.preventDefault();
      this.zone.classList.remove('ai-drop-over');
      pickFile(Array.from(e.dataTransfer.files || []));
    });
    window.addEventListener('dragover', this.guard);
    window.addEventListener('drop', this.guard);
  }

  update(I) {
    const box = this.llm.querySelector('input');
    const missing = llmMissing();
    box.disabled = missing;
    box.checked = !missing && I.useLlm;
    this.box.hidden = I.step !== 'pick';
  }

  dispose() {
    window.removeEventListener('dragover', this.guard);
    window.removeEventListener('drop', this.guard);
  }
}

/* ---------- progress (persistent, so its live text is announced) ---------- */

class ProgressBox {
  constructor() {
    this.heading = h('strong', { class: 'ai-big', tabindex: '-1', 'data-fk': 'import-step-progress' });
    this.count = h('span', { class: 'ai-quiet' });
    this.fill = h('i');
    this.bar = h('div', { class: 'ai-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, this.fill);
    this.line = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.cancel = btn('ai-ghost ai-sm', 'Cancel', () => this.onCancel(), { 'data-fk': 'import-cancel' });
    this.box = h('div', { class: 'ai-progress' }, h('div', { class: 'ai-progress-top' }, this.heading, this.count), this.bar, this.line, h('div', { class: 'ai-row' }, this.cancel));
  }

  onCancel() {
    if (cur().uploading) hooks.stop(); else cancelStep();
  }

  update(I) {
    const active = I.step === 'analysing' || I.step === 'creating' || I.step === 'undoing';
    this.box.hidden = !active;
    if (!active) return;
    const st = I.status;
    const label = I.uploading ? PHASE_LABEL.analyse : PHASE_LABEL[st && st.phase] || PHASE_LABEL[{ creating: 'create', undoing: 'undo' }[I.step] || 'analyse'];
    const pct = I.uploading ? I.uploading.pct : st && st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : 0;
    this.heading.textContent = label + '…';
    this.count.textContent = I.uploading || !st || !st.total ? '' : st.done + ' of ' + st.total;
    this.bar.setAttribute('aria-label', label);
    this.bar.setAttribute('aria-valuenow', String(pct));
    this.fill.style.transform = 'scaleX(' + pct / 100 + ')';
    this.line.textContent = I.uploading ? 'Uploading ' + I.uploading.name + ': ' + I.uploading.pct + '%' : (st && st.current) || 'Starting…';
    this.cancel.hidden = I.step === 'undoing';
    this.cancel.disabled = I.posting;
  }
}

/* ---------- preview pieces ---------- */

function issueContext(plan, issue) {
  const fi = plan.findings.findIndex((f) => f.key === issue.finding_key);
  const ii = plan.images.findIndex((i) => i.key === issue.image_key);
  const parts = [];
  if (fi >= 0) parts.push('Finding ' + (fi + 1) + (plan.findings[fi].title ? ': ' + plan.findings[fi].title : ''));
  if (ii >= 0) parts.push('Screenshot ' + (ii + 1));
  if (issue.fields.length) parts.push((issue.code === 'fields_blank' ? 'Blank: ' : 'About: ') + fieldNames(issue.fields));
  const gaps = issue.code === 'id_gaps' ? strs(plan.stats && plan.stats.id_gaps) : [];
  if (gaps.length) parts.push('Missing: ' + gaps.join(', '));
  return parts.join(' · ');
}

const strs = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);

function issueRow(plan, issue) {
  const target = issueTarget(issue);
  const context = issueContext(plan, issue);
  return h('li', { class: 'ai-issue', 'data-level': issue.level },
    chip(LEVEL_TONE[issue.level], LEVEL_LABEL[issue.level]),
    h('span', { class: 'ai-issue-text' }, issue.message, context ? h('small', {}, context) : null),
    target ? btn('ai-link ai-issue-go', 'Go to field', () => focusTo(root, target),
      { 'aria-label': 'Go to field: ' + issue.message + (context ? ' (' + context + ')' : '') }) : null);
}

function completenessView(plan) {
  const sorted = LEVELS.flatMap((level) => plan.issues.filter((i) => i.level === level));
  const counts = LEVELS.map((l) => [l, plan.issues.filter((i) => i.level === l).length]).filter(([, n]) => n);
  return h('section', { class: 'ai-section ai-card', 'aria-label': 'Completeness' },
    h('h3', { class: 'ai-label' }, 'Completeness'),
    h('p', { class: 'ai-quiet' }, counts.length ? counts.map(([l, n]) => COUNT_TEXT[l](n)).join(' · ') + '. Nothing is written to PwnDoc until you create.' : 'Nothing is missing.'),
    sorted.length ? h('ul', { class: 'ai-issues ai-imp-issues' }, sorted.map((i) => issueRow(plan, i))) : null);
}

function previewHead(plan) {
  const merged = plan.findings.filter((f) => f.origin === 'merged').length;
  const facts = [plural(plan.findings.length, 'finding'), merged ? merged + ' listed twice and merged' : '', plural(plan.images.length, 'screenshot')].filter(Boolean);
  return [h('h3', { class: 'ai-big ai-card-title', tabindex: '-1', 'data-fk': 'import-step-preview' }, plan.file_name),
    h('p', { class: 'ai-quiet' }, facts.join(' · ')),
    plan.previous_import ? note('warn', 'This file was already imported into PwnDoc before. Creating it again makes a second audit.') : null];
}

function gateView(I) {
  const plan = I.plan;
  if (!plan) return null;
  const dup = plan.duplicate_audit ? [note('warn', plan.issues.find((i) => i.code === 'duplicate_audit')?.message || 'An audit with this name already exists for this company.'),
    choice('checkbox', 'import-dup', 'Create another audit with the same name', I.confirmDuplicate, (e) => patch({ confirmDuplicate: e.target.checked }), 'import-confirm-duplicate')] : [];
  const blanks = warnings(plan).length ? [choice('checkbox', 'import-blanks', 'Create with the blanks listed above', I.acceptBlanks,
    (e) => patch({ acceptBlanks: e.target.checked }), 'import-accept-blanks', 'You can still fill them in PwnDoc later.')] : [];
  return dup.length || blanks.length ? h('div', { class: 'ai-stack-tight' }, dup, blanks) : null;
}

function confirmView(I) {
  if (I.step !== 'confirm' || !I.plan) return null;
  const s = createSummary(I.plan);
  return h('section', { class: 'ai-card ai-stack-tight', 'aria-label': 'Confirm' },
    h('p', { class: 'ai-lead ai-confirm-text' }, confirmText(s)),
    h('ul', { class: 'ai-plain-list' },
      h('li', {}, s.newCompany ? 'A new company “' + s.company + '” is created.' : 'The existing company “' + s.company + '” is reused.'),
      h('li', {}, plural(s.images, 'screenshot') + (s.images === 1 ? ' is' : ' are') + ' uploaded into the proofs of concept.'),
      h('li', {}, 'Nothing that already exists in PwnDoc is changed or overwritten.')),
    h('div', { class: 'ai-row' },
      btn('ai-ghost', 'Go back', () => patch({ confirm: '' }), { 'data-fk': 'import-create-no' }),
      btn('ai-primary', 'Yes, create', startCreate, { 'data-fk': 'import-create-yes', disabled: I.posting ? true : null })));
}

/* ---------- results ---------- */

function undoConfirm(I, label) {
  if (I.confirm !== 'undo') return btn('ai-ghost', label, () => patch({ confirm: 'undo' }), { 'data-fk': 'import-undo' });
  return h('div', { class: 'ai-card ai-stack-tight' },
    h('p', { class: 'ai-confirm-text' }, 'Delete the audit and everything this import created?'),
    h('div', { class: 'ai-row' },
      btn('ai-ghost', 'Keep it', () => patch({ confirm: '' }), { 'data-fk': 'import-undo-no' }),
      btn('ai-primary', 'Yes, undo', startUndo, { 'data-fk': 'import-undo-yes' })));
}

function createdView(I) {
  const r = I.status.result;
  const head = h('h3', { class: 'ai-big', tabindex: '-1', 'data-fk': 'import-step-created' }, 'Created');
  if (!r) return [head, note('ok', 'Created in PwnDoc.')];
  return [head, note('ok', 'Created in PwnDoc.'),
    h('ul', { class: 'ai-plain-list' },
      h('li', {}, plural(r.findings, 'finding') + ' created.'),
      h('li', {}, r.companyCreated ? 'A new company was created.' : 'An existing company was reused.'),
      h('li', {}, plural(r.uploaded, 'screenshot') + ' uploaded' + (r.reused ? ', ' + r.reused + ' already in PwnDoc' : '') + (r.skipped ? ', ' + r.skipped + ' left out' : '') + '.')),
    h('div', { class: 'ai-row' },
      h('a', { class: 'ai-btn ai-primary', href: '/audits/' + r.auditId + '/general', 'data-fk': 'import-open-audit' }, 'Open the audit'),
      h('a', { class: 'ai-btn ai-ghost', href: '/data/companies' }, 'Companies in PwnDoc')),
    undoConfirm(I, 'Undo this import')];
}

function undoneView(I) {
  const c = undoCounts(I.status.undo);
  return [h('h3', { class: 'ai-big', tabindex: '-1', 'data-fk': 'import-step-undone' }, 'Undone'), note('ok', 'The import was undone.'),
    c.keptImages ? note('warn', keptText(c.keptImages)) : null,
    c.keptOther ? note('warn', keptOtherText(c.keptOther)) : null,
    c.failed ? note('bad', plural(c.failed, 'object') + ' could not be deleted. Remove them in PwnDoc.') : null];
}

const FAIL_TEXT = {
  analysis_failed: 'The report could not be read', cancelled: 'The step was cancelled', create_failed: 'Creating in PwnDoc failed',
  undo_failed: 'The undo did not finish', interrupted: 'The gateway restarted while this import was running',
};

function failedView(I) {
  const st = I.status;
  const c = undoCounts(st.undo);
  const out = [h('h3', { class: 'ai-big', tabindex: '-1', 'data-fk': 'import-step-failed' }, STATE_LABEL[st.state]),
    note(st.state === 'cancelled' || st.state === 'interrupted' ? 'warn' : 'bad', FAIL_TEXT[st.state] + (st.error ? ': ' + st.error : '.'))];
  if (st.state === 'create_failed' && st.undo) {
    out.push(note(c.failed ? 'bad' : 'ok', c.failed ? plural(c.failed, 'object') + ' could not be removed again.' : 'Everything this import had created was removed again.'));
  } else if (st.state === 'undo_failed' && c.failed) {
    out.push(note('bad', plural(c.failed, 'object') + ' could not be deleted.'));
  }
  if ((st.state === 'create_failed' || st.state === 'undo_failed') && st.undo) {
    if (c.keptImages) out.push(note('warn', keptText(c.keptImages)));
    if (c.keptOther) out.push(note('warn', keptOtherText(c.keptOther)));
  }
  const canUndo = st.state === 'undo_failed' || st.state === 'interrupted' || (st.state === 'create_failed' && c.failed > 0);
  if (canUndo) out.push(undoConfirm(I, st.state === 'undo_failed' ? 'Try the undo again' : 'Undo what is left'));
  if (st.fileId && (st.state === 'analysis_failed' || st.state === 'cancelled' || st.state === 'interrupted')) {
    out.push(h('div', { class: 'ai-row' }, btn('ai-ghost', 'Analyse again', () => analyse(st.fileId), { 'data-fk': 'import-again' })));
  }
  return out;
}

function resultView(I) {
  if (!I.status) return null;
  if (I.step === 'created') return createdView(I);
  if (I.step === 'undone') return undoneView(I);
  return I.step === 'failed' ? failedView(I) : null;
}

/* ---------- list and discard ---------- */

function listView(I) {
  if (!I.list) return h('p', { class: 'ai-quiet' }, I.listError ? '' : 'Loading…');
  if (!I.list.imports.length) return h('p', { class: 'ai-quiet' }, 'No imports yet.');
  return h('ul', { class: 'ai-docs ai-imp-list', 'aria-label': 'Recent imports' }, I.list.imports.map((st) =>
    h('li', { class: st.id === I.id ? 'ai-selected' : '' },
      h('span', { class: 'ai-doc-name' }, st.fileName, h('small', {}, st.started ? fmtTime(st.started) : '')),
      h('span', { class: 'ai-row' }, chip(STATE_TONE[st.state] || 'mute', STATE_LABEL[st.state]),
        btn('ai-ghost ai-sm', 'Open', () => openImport(st.id), { 'data-fk': 'import-open-' + st.id, 'aria-label': 'Open ' + st.fileName })))));
}

function discardView(I) {
  if (!I.id || !I.status || isRunning(I.status.state) || I.step === 'confirm') return null;
  const again = btn('ai-ghost ai-sm', 'Import another report', () => { storeImport(null); patch({ id: null, status: null, plan: null, confirm: '', actionError: '', notice: '' }); }, { 'data-fk': 'import-new' });
  if (I.confirm !== 'discard') return h('div', { class: 'ai-row' }, again, btn('ai-ghost ai-sm', 'Delete this import', () => patch({ confirm: 'discard' }), { 'data-fk': 'import-discard' }));
  const created = I.status.state === 'created' || I.status.state === 'undo_failed' || I.status.state === 'create_failed';
  return h('div', { class: 'ai-card ai-stack-tight' },
    h('p', { class: 'ai-confirm-text' }, created ? 'The audit stays in PwnDoc and this import can no longer undo it.' : 'Delete the preview and the extracted screenshots from the gateway?'),
    h('div', { class: 'ai-row' },
      btn('ai-ghost ai-sm', 'Keep', () => patch({ confirm: '' }), { 'data-fk': 'import-discard-no' }),
      btn('ai-primary ai-sm', 'Delete', discardImport, { 'data-fk': 'import-discard-yes' })));
}

/* ---------- the panel ---------- */

class ImportPanel {
  constructor() {
    this.pick = new PickArea();
    this.progress = new ProgressBox();
    this.notes = h('div', { class: 'ai-notes' });
    this.guardBox = h('div');
    this.status = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.form = null;
    this.formFor = null;
    this.preview = h('div', { class: 'ai-stack' });
    this.blocks = [
      new Block((I) => [I.step === 'preview' || I.step === 'confirm', I.plan], (I) => this.previewView(I)),
      new Block((I) => [I.step, I.plan, I.acceptBlanks, I.confirmDuplicate], (I) => (I.step === 'preview' ? gateView(I) : null)),
      new Block((I) => [I.step, I.plan, I.posting], confirmView),
      new Block((I) => [I.step, I.status, I.confirm], resultView),
      new Block((I) => [I.id, I.status, I.confirm, I.step], discardView),
    ];
    this.list = new Block((I) => [I.list, I.id, I.listError], listView);
    this.foot = h('div', { class: 'ai-foot-slot' });
    this.footKey = '';
    this.lastStep = '';
    this.warned = false;
    this.body = h('div', { class: 'ai-stack ai-imp-root' },
      h('div', { class: 'ai-stack-tight' }, h('p', { class: 'ai-lead' }, 'Turn a finished Word report into a PwnDoc audit.'),
        h('p', { class: 'ai-quiet' }, 'The gateway reads the company, dates, scope, findings and screenshots of a report made from the active PwnDoc template. You check and edit everything before anything is created, and one click undoes the import.')),
      this.notes, this.guardBox, this.pick.box, this.progress.box, ...this.blocks.map((b) => b.el), section('Recent imports', this.status, this.list.el));
  }

  previewView(I) {
    if ((I.step !== 'preview' && I.step !== 'confirm') || !I.plan) return null;
    if (this.formFor !== I.plan) {
      const id = I.plan.import_id;
      this.form = new PreviewForm(I.plan, { onChange: () => this.changed(), imageSrc: (key) => imageUrl(id, key) });
      this.formFor = I.plan;
    }
    const wrap = h('div', { class: 'ai-stack' }, previewHead(I.plan), completenessView(I.plan), this.form.audit, this.form.findings, this.form.images);
    return wrap;
  }

  changed() {
    const dirty = this.form ? this.form.isDirty() : false;
    this.warned = false;
    this.guardBox.replaceChildren();
    if (dirty !== cur().dirty) patch({ dirty, notice: '' });
  }

  drawNotes(I) {
    const texts = [I.actionError, I.planError, I.pollError, I.listError].filter(Boolean);
    const key = texts.join('\n');
    if (key === this.notesKey) return false;
    this.notesKey = key;
    this.notes.replaceChildren(...texts.map((t) => h('div', { class: 'ai-note ai-bad', role: 'alert' }, t)));
    return true;
  }

  footSpec(I) {
    if (I.step !== 'preview' || !I.plan) return { key: '', nodes: [] };
    const gate = createGate(I.plan, I);
    const save = () => { const body = this.form && this.form.edits(); if (body) saveEdits(body); else patch({ dirty: false }); };
    const create = () => patch({ confirm: 'create' });
    const key = [I.dirty, I.saving, I.posting, gate.reason].join('|');
    return { key, nodes: [h('span', { class: 'ai-foot-info ai-imp-gate', id: 'ai-imp-gate' }, gate.reason),
      gatedBtn('ai-ghost', I.saving ? 'Saving…' : 'Save changes', save, 'import-save', !I.dirty || I.saving),
      gatedBtn('ai-primary', 'Create in PwnDoc', create, 'import-create', !gate.ok || I.saving || I.posting,
        gate.reason ? { 'aria-describedby': 'ai-imp-gate' } : null)] };
  }

  drawFoot(I) {
    const spec = this.footSpec(I);
    if (spec.key === this.footKey) return;
    this.footKey = spec.key;
    const had = root.activeElement && this.foot.contains(root.activeElement) ? root.activeElement.getAttribute('data-fk') : '';
    this.foot.replaceChildren(...spec.nodes);
    if (had) focusKey(this.foot, had);
  }

  /** A new step: put focus somewhere sensible when the control that had it is gone. */
  focusStep(I) {
    const stepKey = I.step + (I.plan ? '+plan' : '');  // the preview step is only drawn once its plan is in
    if (stepKey === this.lastStep && I.confirm === this.lastConfirm) return;
    const back = this.lastStep.startsWith('confirm') && I.step === 'preview';
    const closed = this.lastConfirm === 'undo' ? 'import-undo' : this.lastConfirm === 'discard' ? 'import-discard' : '';
    this.lastStep = stepKey;
    this.lastConfirm = I.confirm;
    const active = root.activeElement;
    const lost = !active || !(this.body.contains(active) || this.foot.contains(active)) || active.getClientRects().length === 0;
    const opened = I.confirm === 'undo' ? 'import-undo-no' : I.confirm === 'discard' ? 'import-discard-no' : '';
    const want = opened || (back ? 'import-create' : '') || (lost && closed) || STEP_FOCUS[I.step];
    if (I.confirm || I.step === 'confirm' || lost) {
      if (!(want && (focusTo(this.body, want) || focusTo(this.foot, want)))) focusTo(this.body, 'import-step-' + I.step);
    }
  }

  draw() {
    const I = cur();
    const changed = this.drawNotes(I);
    this.status.textContent = I.notice;
    this.pick.update(I);
    this.progress.update(I);
    for (const block of this.blocks) block.update(I);
    if (I.step === 'confirm') this.blocks[0].el.hidden = true;
    else this.blocks[0].el.hidden = !this.blocks[0].el.firstChild;
    this.list.update(I);
    this.drawFoot(I);
    this.focusStep(I);
    if (changed) ui.renderBody('import');
  }

  /** Escape, the backdrop or X: a confirm closes first; unsaved edits ask once, visibly. */
  guard() {
    const I = cur();
    if (I.confirm) { patch({ confirm: '' }); return true; }
    if (I.dirty && !this.warned) {
      this.warned = true;
      this.askClose();
      return true;
    }
    return false;
  }

  /** The unsaved-changes question at the top of the drawer, with "Keep editing" focused. */
  askClose() {
    const keep = () => { this.warned = false; this.guardBox.replaceChildren(); focusTo(this.foot, 'import-save'); };
    this.guardBox.replaceChildren(note('warn',
      h('p', { class: 'ai-confirm-text' }, 'You have unsaved changes. Close and discard them?'),
      h('div', { class: 'ai-row' },
        btn('ai-ghost ai-sm', 'Keep editing', keep, { 'data-fk': 'import-keep-editing' }),
        btn('ai-primary ai-sm', 'Discard and close', () => ui.closeDrawer(), { 'data-fk': 'import-discard-close' }))));
    if (this.guardBox.scrollIntoView) this.guardBox.scrollIntoView({ block: 'nearest' });
    focusKey(this.guardBox, 'import-keep-editing');
  }

  dispose() {
    this.pick.dispose();
  }
}

let panel = null;

export function prepareImport() {
  panel = new ImportPanel();
  hooks.redraw = () => { if (panel) panel.draw(); };
  panel.draw();
  ui.renderBody('import');
  loadList();
  resumeImport();
}

export function viewImport() {
  return { title: TITLE, body: panel.body, foot: [panel.foot] };
}

export const importGuard = () => !!panel && panel.guard();

/** The drawer closed: polling stops; a step that runs keeps going on the gateway. */
export function disposeImport() {
  stopPolling();
  revokeImages();
  hooks.redraw = () => {};
  if (panel) panel.dispose();
  panel = null;
  if (cur().confirm || cur().dirty) patch({ confirm: '', dirty: false });
}
