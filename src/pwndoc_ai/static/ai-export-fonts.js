/* pwndoc-ai: the document-wide font pickers of the "Export & fonts" drawer: the document default
 * and one picker per block type found in the document, plus "Reset all fonts". The pickers keep
 * their DOM for one analysed document; a plan that changed elsewhere (apply to all, reset, a
 * reload after a stale save) is pushed into them without firing their change handlers. */
import { btn, h } from './ai-dom.js';
import { FontPicker } from './ai-font-picker.js';
import { BLOCK_TYPE_LABELS, emptyPlan, presentTypes, withSpec } from './ai-export-util.js';
import { cur } from './ai-export-store.js';
import { setPlan } from './ai-export-run.js';

const CATEGORY = { code: 'mono' };

function specOf(plan, where) {
  if (where.scope === 'default') return plan.default;
  return plan.by_block_type[where.type] || { family: null, size: null };
}

export class TypePanel {
  constructor() {
    this.box = h('div', { class: 'ai-stack-tight' });
    this.forDetail = null;
    this.forCatalog = null;
    this.entries = [];
  }

  picker(E, where, keys, label, inherit) {
    const spec = specOf(E.plan, where);
    const picker = new FontPicker({
      fk: keys[0], sizeFk: keys[1], label, catalog: E.catalog, family: spec.family, size: spec.size,
      inheritLabel: inherit, sizeInheritLabel: 'Keep the size', onChange: (v) => setPlan(withSpec(cur().plan, where, v)),
    });
    picker.box.classList.add(CATEGORY[where.type] ? 'ai-fp-mono' : 'ai-fp-plain');
    this.entries.push({ where, picker });
    return picker.box;
  }

  build(E) {
    this.forDetail = E.detail;
    this.forCatalog = E.catalog;
    this.entries = [];
    const types = presentTypes(E.detail.sections);
    const reset = btn('ai-ghost ai-sm', 'Reset all fonts', () => setPlan(emptyPlan()), { 'data-fk': 'export-reset' });
    this.box.replaceChildren(
      h('h4', { class: 'ai-label' }, 'Whole document'),
      this.picker(E, { scope: 'default' }, ['export-default-font', 'export-default-size'], 'Document default', 'Keep the document’s fonts'),
      h('p', { class: 'ai-quiet' }, 'The default font applies to every block except code; the default size applies to body text, lists, tables and quotes.'),
      h('details', { class: 'ai-exp-types' }, h('summary', {}, 'By block type (' + types.length + ')'),
        h('div', { class: 'ai-stack-tight' }, types.map((type) =>
          this.picker(E, { scope: 'type', type }, ['export-type-font-' + type, 'export-type-size-' + type], BLOCK_TYPE_LABELS[type], 'Inherit')))),
      h('div', { class: 'ai-row' }, reset));
  }

  update(E) {
    if (!E.detail || !E.plan) { this.box.replaceChildren(); this.forDetail = null; return; }
    if (E.detail !== this.forDetail || E.catalog !== this.forCatalog) { this.build(E); return; }
    const active = this.box.getRootNode().activeElement;
    for (const { where, picker } of this.entries) {
      if (picker.box.contains(active)) continue;  // never overwrite what is being typed
      const spec = specOf(E.plan, where);
      const v = picker.value();
      if (v.family !== spec.family || v.size !== spec.size) picker.set(spec.family, spec.size);
    }
  }
}
