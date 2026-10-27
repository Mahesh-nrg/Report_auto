/* pwndoc-ai: the editable preview of an import (audit header, findings, screenshots).
 *
 * The form is built once per preview revision and keeps its DOM while the user types; it only
 * reports whether something changed and, on save, the partial edit body (only the controls that
 * changed). Every value from the gateway is shown through text nodes or control values; rich text
 * is shown through the allow-list tree of ai-import-util.js, never as HTML. */
import { chip, field, h } from './ai-dom.js';
import {
  SEVERITIES, controlText, editableToPoc, fieldValue, headerValues, htmlTree,
} from './ai-import-util.js';
import { fmtBytes } from './ai-rag-files.js';

const FIELDS = [
  ['title', 'Title', 'line'], ['severity', 'Severity', 'severity'], ['cvss_vector', 'CVSS vector', 'line'],
  ['cvss_score', 'CVSS score', 'line'], ['cwe', 'CWE / CVE', 'line'], ['urls', 'Vulnerable URLs (one per line)', 'area'],
  ['parameter', 'Vulnerable parameter', 'line'], ['location', 'Vulnerable location', 'line'],
  ['description', 'Description', 'rich'], ['observation', 'Observation', 'rich'], ['poc', 'Proof of concept', 'poc'],
  ['remediation', 'Remediation', 'rich'], ['references', 'References (one per line)', 'area'],
];
const SHORT = new Set(['severity', 'cvss_vector', 'cvss_score', 'cwe']);
const HEADER_FK = {
  company: 'import-company', audit_name: 'import-audit-name', language: 'import-language', audit_type: 'import-audit-type',
  template_id: 'import-template', date_start: 'import-date-start', date_end: 'import-date-end', scope: 'import-scope',
  contact_email: 'import-contact-email', contact_first: 'import-contact-first', contact_last: 'import-contact-last',
};
const FLAG_TEXT = {
  summary_only: 'Only in the summary table', truncated: 'Shortened', merged_duplicate: 'Listed twice, merged', copies_disagree: 'Copies disagree',
  description_split: 'Description split by AI', placeholder: 'Looks like a placeholder', unsupported: 'Format not supported',
  too_large: 'Too large', uncaptioned: 'No caption', unmatched: 'Placed after the nearest finding', duplicate_caption: 'Repeated caption',
};
const TYPE_NAME = { 'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/gif': 'GIF' };
const RICH_HINT = 'A blank line starts a paragraph. Lines starting with "- " or "1. " make a list. **bold** and `code` keep their marks.';

const input = (fk, value, extra) => h('input', Object.assign({ class: 'ai-input', 'data-fk': fk, value: value || '' }, extra));
const area = (fk, value, rows) => h('textarea', { class: 'ai-input', 'data-fk': fk, rows: String(rows || 3), value: value || '' });

function select(fk, current, options, blank) {
  const values = options.map((o) => o.value);
  const extra = current && !values.includes(current) ? [{ value: current, label: current + ' (not in PwnDoc)' }] : [];
  return h('select', { class: 'ai-input', 'data-fk': fk },
    blank !== null ? h('option', { value: '' }, blank) : null,
    [...options, ...extra].map((o) => h('option', { value: o.value, selected: o.value === current ? true : null }, o.label)));
}

const checkbox = (fk, label, checked, extra) => h('label', { class: 'ai-choice' },
  h('input', Object.assign({ type: 'checkbox', 'data-fk': fk, checked }, extra)),
  h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }),
  h('span', { class: 'ai-choice-text' }, label));

/** Allow-list tree nodes as DOM (only the allow-listed tags exist in the tree). */
export function treeNodes(nodes) {
  return nodes.map((n) => {
    if (typeof n === 'string') return document.createTextNode(n);
    const el = document.createElement(n.tag);
    el.append(...treeNodes(n.kids));
    return el;
  });
}

export class PreviewForm {
  /** @param {object} plan normPlan() output; hooks: {onChange(), imageSrc(key) -> Promise<string>} */
  constructor(plan, hooks) {
    this.plan = plan;
    this.hooks = hooks;
    this.controls = new Map();  // data-fk -> { el, initial }
    this.imageKeys = plan.images.map((i) => i.key);
    this.audit = this.auditSection();
    this.findings = h('section', { class: 'ai-section', 'aria-label': 'Findings' },
      h('h3', { class: 'ai-label' }, 'Findings (' + plan.findings.length + ')'),
      plan.findings.map((f, i) => this.findingSet(f, i)));
    this.images = plan.images.length ? h('section', { class: 'ai-section', 'aria-label': 'Screenshots' },
      h('h3', { class: 'ai-label' }, 'Screenshots (' + plan.images.length + ')'),
      h('p', { class: 'ai-quiet' }, 'Every screenshot goes into the proof of concept of the finding it is attached to. Move its [Screenshot …] line in the proof of concept to change where it appears.'),
      plan.images.map((img, i) => this.imageSet(img, i))) : null;
  }

  track(fk, el, initial) {
    this.controls.set(fk, { el, initial });
    el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input', () => this.hooks.onChange());
    return el;
  }

  value(fk) {
    const c = this.controls.get(fk);
    return c.el.type === 'checkbox' ? c.el.checked : c.el.value;
  }

  changed(fk) {
    const c = this.controls.get(fk);
    return !!c && this.value(fk) !== c.initial;
  }

  isDirty() {
    return Array.from(this.controls.keys()).some((fk) => this.changed(fk));
  }

  /* ---------- the audit header ---------- */

  headerControl(name, el, initial) {
    return this.track(HEADER_FK[name], el, initial);
  }

  auditSection() {
    const { header, options, candidates } = this.plan;
    const contact = header.contact || { email: '', firstname: '', lastname: '' };
    const list = h('datalist', { id: 'ai-imp-companies' }, candidates.map((c) => h('option', { value: c })));
    const company = this.headerControl('company', input(HEADER_FK.company, header.company, { list: 'ai-imp-companies', autocomplete: 'off' }), header.company);
    const companyNote = this.plan.company_id ? chip('ok', 'Existing PwnDoc company: it will be reused') : chip('accent', 'New company: it will be created');
    this.typeHint = h('small', { class: 'ai-type-hint' });
    const grab = (name, el) => this.headerControl(name, el, el.value);
    const section = h('section', { class: 'ai-section ai-card', 'aria-label': 'Audit' },
      h('h3', { class: 'ai-label' }, 'Audit'),
      field('Company', [company, list], candidates.length > 1 ? 'Names found in the document: ' + candidates.join(', ') : 'Saving checks PwnDoc for a company of this name.'),
      h('div', { class: 'ai-row' }, companyNote),
      field('Audit name', grab('audit_name', input(HEADER_FK.audit_name, header.audit_name)), 'Taken from the file name; the report title is not used.'),
      h('div', { class: 'ai-two' },
        field('Language', grab('language', select(HEADER_FK.language, header.language, options.languages, 'Choose…'))),
        field('Audit type', grab('audit_type', select(HEADER_FK.audit_type, header.audit_type, options.audit_types.map((t) => ({ value: t.name, label: t.name })), 'Choose…')), this.typeHint)),
      field('Template', grab('template_id', select(HEADER_FK.template_id, header.template_id, options.templates, 'Choose…'))),
      h('div', { class: 'ai-two' },
        field('Start date', grab('date_start', input(HEADER_FK.date_start, header.date_start, { type: 'date' }))),
        field('End date', grab('date_end', input(HEADER_FK.date_end, header.date_end, { type: 'date' })))),
      field('Scope (one per line)', grab('scope', area(HEADER_FK.scope, header.scope.join('\n'), 3))),
      field('Contact email', grab('contact_email', input(HEADER_FK.contact_email, contact.email, { type: 'email', autocomplete: 'off' })), 'Optional. The template carries no contacts; a contact is created only with an email.'),
      h('div', { class: 'ai-two' },
        field('Contact first name', grab('contact_first', input(HEADER_FK.contact_first, contact.firstname))),
        field('Contact last name', grab('contact_last', input(HEADER_FK.contact_last, contact.lastname)))));
    for (const fk of [HEADER_FK.language, HEADER_FK.audit_type]) this.controls.get(fk).el.addEventListener('change', () => this.updateTypeHint());
    this.updateTypeHint();
    return section;
  }

  updateTypeHint() {
    const type = this.plan.options.audit_types.find((t) => t.name === this.value(HEADER_FK.audit_type));
    const lang = this.value(HEADER_FK.language);
    const missing = type && lang && type.locales.length && !type.locales.includes(lang);
    this.typeHint.textContent = missing ? 'This audit type has no template for this language.' : '';
  }

  /* ---------- findings ---------- */

  findingControl(f, name, kind) {
    const fk = 'f-' + f.key + '-' + name;
    const text = controlText(f, name);
    if (kind === 'severity') return this.track(fk, select(fk, f.severity, SEVERITIES.map((s) => ({ value: s, label: s })), 'Not set'), f.severity);
    if (kind === 'line') return this.track(fk, input(fk, text), text);
    const rows = kind === 'area' ? 3 : Math.min(12, Math.max(3, text.split('\n').length + 1));
    return this.track(fk, area(fk, text, rows), text);
  }

  findingMeta(f) {
    const chips = [];
    const o = (this.plan && this.plan.order && this.plan.order[f.key]) || null;
    if (o && o.severity) chips.push(chip('accent', o.severity + (o.score === null ? ' (no CVSS 3.1 score)' : ' ' + o.score.toFixed(1))));
    if (o && o.note) chips.push(chip('warn', 'Severity from CVSS 3.1: ' + o.note));
    if (f.source_id) chips.push(chip('mute', 'No. ' + f.source_id));
    if (f.origin === 'merged' || f.flags.includes('merged_duplicate')) chips.push(chip('accent', 'Listed twice, merged'));
    for (const flag of f.flags) if (flag !== 'merged_duplicate' && FLAG_TEXT[flag]) chips.push(chip('warn', FLAG_TEXT[flag]));
    if (f.llm_fields.length) chips.push(chip('warn', 'AI filled: ' + f.llm_fields.join(', ')));
    return chips.length ? h('div', { class: 'ai-row' }, chips) : null;
  }

  findingSet(f, index) {
    const o = (this.plan && this.plan.order && this.plan.order[f.key]) || null;
    const sno = o && o.seq ? o.seq : (f.include ? String(index + 1) : '-');
    const legend = h('legend', { class: 'ai-finding-legend' }, sno + '. ' + (f.title || '(no title)'));
    const include = this.track('f-' + f.key + '-include', checkbox('f-' + f.key + '-include', 'Include this finding', f.include).querySelector('input'), f.include);
    const rows = [];
    let pair = [];
    for (const [name, label, kind] of FIELDS) {
      const ctl = this.findingControl(f, name, kind);
      const hint = kind === 'rich' || kind === 'poc' ? (kind === 'poc' ? 'Lines like [Screenshot i001] place a screenshot. ' : '') + RICH_HINT : null;
      const box = field(label, ctl, hint);
      if (SHORT.has(name)) { pair.push(box); if (pair.length === 2) { rows.push(h('div', { class: 'ai-two' }, pair)); pair = []; } continue; }
      rows.push(box);
      if (kind === 'poc') rows.push(this.pocPreview(f, ctl));
    }
    this.controls.get('f-' + f.key + '-title').el.addEventListener('input', (e) => {
      legend.textContent = (index + 1) + '. ' + (e.target.value.trim() || '(no title)');
    });
    return h('fieldset', { class: 'ai-finding', 'aria-label': 'Finding ' + (index + 1) },
      legend, this.findingMeta(f), include.closest('label'), rows);
  }

  /** A live, sanitised preview of the PoC: allow-list text and the screenshots it places. */
  pocPreview(f, ctl) {
    const box = h('div', { class: 'ai-rich ai-poc-preview', role: 'group', 'aria-label': 'PoC preview' });
    const draw = () => box.replaceChildren(...editableToPoc(ctl.value, this.imageKeys).flatMap((b) => this.pocBlock(b)));
    ctl.addEventListener('input', draw);
    draw();
    return h('div', { class: 'ai-field' }, h('span', { class: 'ai-field-label', 'aria-hidden': 'true' }, 'Preview'), box);
  }

  pocBlock(block) {
    if (block.kind === 'html') return treeNodes(htmlTree(block.html));
    const img = this.plan.images.find((i) => i.key === block.image_key);
    return [h('figure', { class: 'ai-figure' }, this.thumb(img), h('figcaption', {}, img.caption || 'Screenshot ' + img.key))];
  }

  thumb(img) {
    if (!img.usable) return h('span', { class: 'ai-quiet' }, 'This screenshot cannot be shown or uploaded.');
    const el = h('img', { alt: img.caption || 'Screenshot ' + img.key, class: 'ai-thumb', decoding: 'async' });
    this.hooks.imageSrc(img.key).then((url) => { if (url) el.src = url; });
    return el;
  }

  /* ---------- screenshots ---------- */

  imageSet(img, index) {
    const fk = (name) => 'img-' + img.key + '-' + name;
    const caption = this.track(fk('caption'), input(fk('caption'), img.caption), img.caption);
    const choices = this.plan.findings.map((f, i) => ({ value: f.key, label: (i + 1) + '. ' + (f.title || '(no title)') }));
    const target = this.track(fk('finding'), select(fk('finding'), img.finding_key, choices, 'Not attached'), img.finding_key);
    const include = checkbox(fk('include'), 'Include this screenshot', img.include, { disabled: img.usable ? null : true });
    this.track(fk('include'), include.querySelector('input'), img.include);
    const facts = [TYPE_NAME[img.media_type] || 'Unknown format', img.width && img.height ? img.width + '×' + img.height : '', img.size ? fmtBytes(img.size) : ''].filter(Boolean);
    return h('fieldset', { class: 'ai-finding ai-shot', 'aria-label': 'Screenshot ' + (index + 1) },
      h('legend', { class: 'ai-finding-legend' }, 'Screenshot ' + (index + 1)),
      h('div', { class: 'ai-shot-body' }, this.thumb(img),
        h('div', { class: 'ai-stack-tight' }, h('p', { class: 'ai-quiet' }, facts.join(' · ')),
          h('div', { class: 'ai-row' }, img.flags.filter((x) => FLAG_TEXT[x]).map((x) => chip('warn', FLAG_TEXT[x]))))),
      field('Caption', caption), field('Attach to finding', target), include);
  }

  /* ---------- the edit body ---------- */

  headerEdit() {
    const read = (name) => this.value(HEADER_FK[name]);
    const values = headerValues(Object.fromEntries(Object.keys(HEADER_FK).map((n) => [n, read(n)])));
    const out = {};
    for (const name of Object.keys(HEADER_FK)) {
      if (!this.changed(HEADER_FK[name])) continue;
      const key = name.startsWith('contact_') ? 'contact' : name;
      out[key] = values[key];
    }
    return Object.keys(out).length ? out : null;
  }

  findingEdits() {
    const out = [];
    for (const f of this.plan.findings) {
      const edit = {};
      for (const name of ['include', ...FIELDS.map((x) => x[0])]) {
        const fk = 'f-' + f.key + '-' + name;
        if (this.changed(fk)) edit[name] = fieldValue(name, this.value(fk), this.imageKeys);
      }
      if (Object.keys(edit).length) out.push({ key: f.key, ...edit });
    }
    return out;
  }

  imageEdits() {
    const out = [];
    for (const img of this.plan.images) {
      const fk = (name) => 'img-' + img.key + '-' + name;
      const edit = {};
      if (this.changed(fk('caption'))) edit.caption = this.value(fk('caption')).replace(/\s+/g, ' ').trim();
      if (this.changed(fk('finding'))) edit.finding_key = this.value(fk('finding'));
      if (this.changed(fk('include'))) edit.include = this.value(fk('include'));
      if (Object.keys(edit).length) out.push({ key: img.key, ...edit });
    }
    return out;
  }

  /** The PUT /preview body with only what changed, or null when nothing did. */
  edits() {
    const header = this.headerEdit();
    const findings = this.findingEdits();
    const images = this.imageEdits();
    if (!header && !findings.length && !images.length) return null;
    return Object.assign({ revision: this.plan.revision }, header ? { header } : {},
      findings.length ? { findings } : {}, images.length ? { images } : {});
  }
}
