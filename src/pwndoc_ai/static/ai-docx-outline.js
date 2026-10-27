/* pwndoc-ai: the setup of a refine run: scope (whole document or picked sections from the heading
 * outline), depth (light or deep) and the client names to hide. The form keeps its own DOM between
 * redraws, so typing and checking boxes is never interrupted by a poll; every change is written to
 * S.docx.setup (replaced, not edited). */
import { btn, choice, h, note, plural, section } from './ai-dom.js';
import { cur, patch } from './ai-docx-store.js';
import { defaultPicked, descendants, parseTerms, pickedTotals } from './ai-docx-util.js';

const KILO = 1000;
const fmtChars = (n) => (n >= KILO ? (n / KILO).toFixed(1) + 'k' : String(n)) + ' characters';

const protectedText = (n) => (n === 1 ? '1 paragraph is protected' : n + ' paragraphs are protected');
const DEEP_WARNING = 'The AI never adds hosts, credentials, payloads or screenshots, but it can add references and detail from research on the web and in your books. Research uses short queries built from section headings. Check every new fact.';

export class SetupForm {
  constructor(detail) {
    this.outline = detail.outline;
    this.stats = detail.stats;
    this.boxes = new Map();
    const setup = cur().setup;
    this.summary = h('p', { class: 'ai-quiet', role: 'status', 'aria-live': 'polite' });
    this.picker = this.buildPicker();
    this.picker.hidden = setup.scope !== 'pick';
    this.deepNote = note('warn', DEEP_WARNING);
    this.deepNote.hidden = setup.mode !== 'deep';
    this.termsError = h('small', { class: 'ai-item-error' });
    this.box = h('div', { class: 'ai-stack' }, this.scopeSection(setup), this.depthSection(setup), this.privacySection(setup));
    this.sync();
  }

  set(fields) { patch({ setup: { ...cur().setup, ...fields } }); }

  scopeSection(setup) {
    return section('What to refine',
      h('div', { class: 'ai-choices', role: 'radiogroup', 'aria-label': 'Scope' },
        choice('radio', 'docx-scope', 'Whole document', setup.scope === 'all', () => this.setScope('all'), 'docx-scope-all',
          'Every paragraph that can be edited safely.'),
        choice('radio', 'docx-scope', 'Pick sections', setup.scope === 'pick', () => this.setScope('pick'), 'docx-scope-pick',
          'Choose headings from the outline. Cover pages and legal text can be skipped.')),
      this.picker,
      h('p', { class: 'ai-quiet' }, protectedText(this.stats.protected) + ' (images, fields, tables of contents, code, headers and footers). They are never sent to the AI and stay exactly as they are.'));
  }

  depthSection(setup) {
    return section('How deep',
      h('div', { class: 'ai-choices', role: 'radiogroup', 'aria-label': 'Depth' },
        choice('radio', 'docx-mode', 'Light polish', setup.mode === 'light', () => this.setMode('light'), 'docx-mode-light',
          'Grammar and clarity only, adds no facts.'),
        choice('radio', 'docx-mode', 'Deep refine', setup.mode === 'deep', () => this.setMode('deep'), 'docx-mode-deep',
          'Researches web and your books, strengthens content, may add references and detail. Check new facts.')),
      this.deepNote);
  }

  privacySection(setup) {
    const input = h('input', {
      class: 'ai-input', type: 'text', autocomplete: 'off', value: setup.terms, 'data-fk': 'docx-terms',
      oninput: () => this.setTerms(input),
    });
    return section('Privacy', h('label', { class: 'ai-field' },
      h('span', { class: 'ai-field-label' }, 'Client or company names to hide from the AI'), input,
      h('small', {}, 'Comma separated. The AI sees fictional stand-ins (a made-up company, fictional domains and IP addresses) and the real values are put back afterwards. Domains, IP addresses and e-mails are swapped as well; paths and payloads are sent as they are.')),
    this.termsError);
  }

  buildPicker() {
    const bar = h('div', { class: 'ai-picker-bar' },
      btn('ai-link', 'Select all', () => this.pick(defaultPicked(this.outline)), { 'data-fk': 'docx-all' }),
      btn('ai-link', 'Select none', () => this.pick(new Set()), { 'data-fk': 'docx-none' }));
    const list = h('ul', { class: 'ai-list ai-outline', 'aria-label': 'Sections to refine' }, this.outline.map((s) => this.sectionRow(s)));
    return h('div', { class: 'ai-picker' }, bar, list, h('div', { class: 'ai-picker-bar' }, this.summary));
  }

  sectionRow(s) {
    const checkbox = h('input', {
      type: 'checkbox', disabled: s.units === 0 ? true : null, 'data-fk': 'docx-sec-' + s.id,
      onchange: () => this.toggle(s.id, checkbox.checked),
    });
    this.boxes.set(s.id, checkbox);
    const detail = s.units ? plural(s.units, 'paragraph') + ' · ' + fmtChars(s.chars) : 'Nothing to refine';
    const label = h('label', { class: 'ai-pick' }, checkbox, h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }),
      h('span', { class: 'ai-choice-text' }, s.title, h('small', {}, detail)));
    label.style.setProperty('padding-inline-start', Math.max(0, s.level - 1) * 14 + 'px');
    return h('li', {}, label);
  }

  setScope(scope) { this.picker.hidden = scope !== 'pick'; this.set({ scope }); }

  setMode(mode) { this.deepNote.hidden = mode !== 'deep'; this.set({ mode }); }

  setTerms(input) {
    this.termsError.textContent = parseTerms(input.value).error;
    input.setAttribute('aria-invalid', this.termsError.textContent ? 'true' : 'false');
    this.set({ terms: input.value });
  }

  /** A heading takes its subsections with it; a subsection alone leaves its parent as it is. */
  toggle(id, on) {
    const next = new Set(cur().setup.picked);
    for (const target of [id, ...descendants(this.outline, id)]) {
      const refinable = this.outline.find((s) => s.id === target).units > 0;
      if (on && refinable) next.add(target); else if (!on) next.delete(target);
    }
    this.pick(next);
  }

  pick(picked) {
    this.set({ picked });
    this.sync();
  }

  /** Put the boxes and the summary in line with the picked set. */
  sync() {
    const picked = cur().setup.picked;
    for (const [id, box] of this.boxes) box.checked = picked.has(id);
    const t = pickedTotals(this.outline, picked);
    this.summary.textContent = plural(t.sections, 'section') + ' selected · ' + plural(t.units, 'paragraph') + ' · ' + fmtChars(t.chars);
  }
}
