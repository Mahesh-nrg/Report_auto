/* pwndoc-ai: one font + size picker, shared by AI Settings (document defaults) and the Export drawer.
 *
 * A dropdown of the gateway's font catalog (each option says installed / substituted by X /
 * missing), an "Other…" choice that reveals a free-text name (validated like the server does), and
 * a size dropdown of presets with a "Custom…" number box (6-72 pt, half-point steps). The picker
 * keeps its own DOM; `onChange({family, size})` runs only for a valid value. */
import { chip, h } from './ai-dom.js';
import {
  CUSTOM, OTHER, SIZE_MAX, SIZE_MIN, SIZE_PRESETS, cleanFamily, clampSize, findFont, fontBadge, fontGroups,
  fontOptionText, parseSize, sizeText, validFamily,
} from './ai-export-util.js';

const NAME_HINT = 'Letters, digits, spaces, dots, dashes; at most 100 characters; no punctuation such as : , ; " < > ( ).';
const SIZE_HINT = 'Between ' + SIZE_MIN + ' and ' + SIZE_MAX + ' pt, in steps of 0.5.';

export class FontPicker {
  /**
   * @param {{fk: string, sizeFk: string, label: string, catalog: object, family?: string|null,
   *   size?: number|null, inheritLabel: string, sizeInheritLabel: string, lenient?: boolean,
   *   onChange?: (v: {family: string|null, size: number|null}) => void}} opts
   */
  constructor(opts) {
    this.opts = opts;
    this.catalog = opts.catalog || { fonts: [], sizes: SIZE_PRESETS.slice(), checked: false };
    this.select = h('select', { class: 'ai-input', 'data-fk': opts.fk, 'aria-label': opts.label + ': font' });
    this.other = h('input', {
      type: 'text', class: 'ai-input', hidden: true, maxlength: '100', spellcheck: 'false', autocomplete: 'off',
      'data-fk': opts.fk + '-other', 'aria-label': opts.label + ': font name', placeholder: 'Font name, e.g. Gill Sans',
    });
    this.size = h('select', { class: 'ai-input ai-fp-size', 'data-fk': opts.sizeFk, 'aria-label': opts.label + ': size' });
    this.custom = h('input', {
      type: 'number', class: 'ai-input ai-fp-size', hidden: true, min: String(SIZE_MIN), max: String(SIZE_MAX), step: '0.5',
      'data-fk': opts.sizeFk + '-custom', 'aria-label': opts.label + ': size in points',
    });
    this.badge = h('span', { class: 'ai-fp-badge' });
    this.error = h('small', { class: 'ai-item-error', 'aria-live': 'polite' });
    this.box = h('div', { class: 'ai-fp', role: 'group', 'aria-label': opts.label },
      h('div', { class: 'ai-fp-row' }, this.select, this.size, this.badge),
      h('div', { class: 'ai-fp-row' }, this.other, this.custom), this.error);
    this.fill(opts.family || '', opts.size == null ? null : opts.size);
    this.wire();
  }

  fill(family, size) {
    const groups = fontGroups(this.catalog).map(([label, fonts]) =>
      h('optgroup', { label }, fonts.map((f) => h('option', { value: f.family }, fontOptionText(f)))));
    this.select.replaceChildren(h('option', { value: '' }, this.opts.inheritLabel), ...groups, h('option', { value: OTHER }, 'Other…'));
    const known = family && findFont(this.catalog, family);
    this.select.value = !family ? '' : known ? known.family : OTHER;
    this.other.value = family && !known ? family : '';
    this.other.hidden = this.select.value !== OTHER;
    const sizes = [...new Set([...this.catalog.sizes, ...SIZE_PRESETS])].sort((a, b) => a - b);
    this.size.replaceChildren(h('option', { value: '' }, this.opts.sizeInheritLabel),
      ...sizes.map((n) => h('option', { value: String(n) }, sizeText(n))), h('option', { value: CUSTOM }, 'Custom…'));
    const preset = size != null && sizes.includes(size);
    this.size.value = size == null ? '' : preset ? String(size) : CUSTOM;
    this.custom.value = size != null && !preset ? String(size) : '';
    this.custom.hidden = this.size.value !== CUSTOM;
    this.showBadge();
  }

  wire() {
    this.select.addEventListener('change', () => {
      this.other.hidden = this.select.value !== OTHER;
      if (!this.other.hidden) this.other.focus();
      this.changed();
    });
    this.other.addEventListener('input', () => this.changed());
    this.size.addEventListener('change', () => {
      this.custom.hidden = this.size.value !== CUSTOM;
      if (!this.custom.hidden) this.custom.focus();
      this.changed();
    });
    this.custom.addEventListener('input', () => this.changed());
  }

  /** The family as typed or chosen ('' = inherit) and whether it is valid. */
  familyValue() {
    if (this.select.value !== OTHER) return { family: this.select.value || null, ok: true };
    const typed = this.other.value;
    if (!typed.trim()) return { family: null, ok: true };
    return validFamily(typed) ? { family: cleanFamily(typed), ok: true } : { family: null, ok: false };
  }

  sizeValue() {
    if (this.size.value !== CUSTOM) return { size: this.size.value ? Number(this.size.value) : null, ok: true };
    if (!this.custom.value.trim()) return { size: null, ok: true };
    if (this.opts.lenient) {
      const n = clampSize(this.custom.value);
      return n === null ? { size: null, ok: false } : { size: n || null, ok: true };
    }
    const n = parseSize(this.custom.value);
    return n === null ? { size: null, ok: false } : { size: n, ok: true };
  }

  /** {family, size, ok}: ok is false while a typed value is invalid. */
  value() {
    const f = this.familyValue();
    const s = this.sizeValue();
    return { family: f.family, size: s.size, ok: f.ok && s.ok, familyOk: f.ok, sizeOk: s.ok };
  }

  showBadge() {
    const v = this.familyValue();
    const badge = v.ok ? fontBadge(this.catalog, v.family) : null;
    this.badge.replaceChildren(badge ? chip(badge.tone, badge.text) : '');
  }

  changed() {
    const v = this.value();
    this.other.toggleAttribute('aria-invalid', !v.familyOk);
    this.custom.toggleAttribute('aria-invalid', !v.sizeOk);
    this.error.textContent = [v.familyOk ? '' : NAME_HINT, v.sizeOk ? '' : SIZE_HINT].filter(Boolean).join(' ');
    this.showBadge();
    if (v.ok && this.opts.onChange) this.opts.onChange({ family: v.family, size: v.size });
  }

  /** Show another value without firing onChange (after apply-to-all, a reset or a reload). */
  set(family, size) {
    this.error.textContent = '';
    this.fill(family || '', size == null ? null : size);
  }

  setCatalog(catalog) {
    const v = this.value();
    this.catalog = catalog;
    this.fill(v.family || '', v.size);
  }

  /** The controls whose values make a settings snapshot (dirty check). */
  controls() { return [this.select, this.other, this.size, this.custom]; }
}
