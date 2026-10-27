/* pwndoc-ai: the "Document fonts" group of AI Settings: default font and size for body text,
 * headings and code, applied to the Word and PDF reports downloaded from PwnDoc (and the clean
 * copy of a refined DOCX). Shown only when the gateway's settings carry these keys. */
import { api } from './ai-api.js';
import { h, note, section } from './ai-dom.js';
import { FontPicker } from './ai-font-picker.js';
import { cssFor, normCatalog, substitutionNotes } from './ai-export-util.js';

const ROLES = [
  { key: 'default_font', fk: 'settings-default-font', label: 'Body text', category: 'sans' },
  { key: 'heading_font', fk: 'settings-heading-font', label: 'Headings', category: 'sans' },
  { key: 'mono_font', fk: 'settings-mono-font', label: 'Code', category: 'mono' },
];
const SAMPLE = {
  heading: '3.2 Stored cross-site scripting',
  body: 'The profile name is stored and shown to every visitor without encoding. An attacker can run script in the browser of any user who views the page.',
  mono: 'GET /profile?name=<script>alert(1)</script>',
};
const FONTS_PATH = '/docx/export/fonts';

/** True when the gateway knows the document-font settings. */
export const hasFontSettings = (d) => !!d && ROLES.every((r) => r.key in d && r.key + '_size' in d);

export class SettingsFonts {
  /** @param {object} d settings  @param {(apply: () => void) => void} rebase runs a non-edit change */
  constructor(d, rebase) {
    this.rebase = rebase || ((apply) => apply());
    this.catalog = normCatalog(null);
    this.pickers = ROLES.map((role) => ({
      role,
      picker: new FontPicker({
        fk: role.fk, sizeFk: role.fk + '-size', label: role.label, catalog: this.catalog, lenient: true,
        family: d[role.key] || null, size: d[role.key + '_size'] > 0 ? d[role.key + '_size'] : null,
        inheritLabel: 'Keep the template’s font', sizeInheritLabel: 'Keep the size',
        onChange: () => this.drawSample(),
      }),
    }));
    this.sample = h('div', { class: 'ai-font-sample', 'data-fk': 'settings-font-sample', 'aria-label': 'Font sample', role: 'group' });
    this.notes = h('div', { class: 'ai-stack-tight', 'aria-live': 'polite' });
    this.box = section('Document fonts',
      h('p', { class: 'ai-quiet' }, 'Default fonts and sizes for the Word and PDF reports downloaded from PwnDoc. Leave a choice on “Keep” to use the template’s own font. Original uploads are never changed.'),
      ...this.pickers.map(({ picker }) => picker.box), this.sample, this.notes);
    this.drawSample();
    this.load();
  }

  async load() {
    try {
      const catalog = normCatalog(await api(FONTS_PATH));
      this.rebase(() => {
        this.catalog = catalog;
        for (const { picker } of this.pickers) picker.setCatalog(catalog);
      });
      if (!this.catalog.checked) this.notes.replaceChildren(note('warn', 'The gateway could not check its installed fonts (fontconfig is missing).'));
    } catch (e) {
      this.notes.replaceChildren(note('warn', 'The font list could not be loaded (' + e.message + '). You can still type a font name with “Other…”.'));
    }
    this.drawSample();
  }

  line(kind, picker, category) {
    const v = picker.value();
    const el = h('p', { class: 'ai-font-sample-' + kind }, SAMPLE[kind]);
    if (v.family) el.style.fontFamily = cssFor(this.catalog, v.family, category);
    if (v.size) el.style.fontSize = v.size + 'pt';
    return el;
  }

  drawSample() {
    const [body, heading, mono] = this.pickers.map((p) => p.picker);
    this.sample.replaceChildren(this.line('heading', heading, 'sans'), this.line('body', body, 'sans'), this.line('mono', mono, 'mono'));
    const families = this.pickers.map((p) => p.picker.value().family);
    const warn = substitutionNotes(this.catalog, families);
    if (this.catalog.fonts.length) this.notes.replaceChildren(...warn.map((t) => note('warn', t)));
  }

  /** The settings patch (family '' = off, size 0 = off). */
  patch() {
    const out = {};
    for (const { role, picker } of this.pickers) {
      const v = picker.value();
      out[role.key] = v.familyOk ? v.family || '' : picker.other.value;  // an invalid name is sent and refused by the server
      out[role.key + '_size'] = v.sizeOk ? v.size || 0 : Number(picker.custom.value);
    }
    return out;
  }

  /** key -> control, merged into the form's fields (snapshot and flagInvalid). */
  fields() {
    const out = {};
    for (const { role, picker } of this.pickers) {
      const [select, other, size, custom] = picker.controls();
      Object.assign(out, { [role.key]: select, [role.key + '_other']: other, [role.key + '_size']: size, [role.key + '_size_custom']: custom });
    }
    return out;
  }
}
