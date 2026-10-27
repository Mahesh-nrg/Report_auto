/* pwndoc-ai: the detected sections of the "Export & fonts" drawer: a keyboard-operable tree (one
 * item per section, block-type chips), and for the chosen section its font + size picker, a live
 * preview of its text in the chosen fonts and the server substitution notice.
 *
 * Every text from the document (titles, excerpts) is drawn with text nodes only. */
import { btn, chip, focusKey, h, note } from './ai-dom.js';
import { FontPicker } from './ai-font-picker.js';
import {
  BLOCK_TYPE_LABELS, applyToAll, cssFor, headingTypeOf, resolveSpec, sectionLabel, sectionSpec, sectionText,
  sizeText, substitutionNotes, withSpec,
} from './ai-export-util.js';
import { cur, patch } from './ai-export-store.js';
import { setPlan } from './ai-export-run.js';

const NAV = { ArrowDown: 1, ArrowUp: -1 };

function chips(section) {
  return h('span', { class: 'ai-chips' }, section.counts.map(([type, n]) =>
    chip(type.startsWith('heading') || type === 'title' ? 'accent' : 'mute', BLOCK_TYPE_LABELS[type] + ' ' + n)));
}

/** `host` is the persistent container of the tree: the tree itself is rebuilt by the redraw. */
function moveTo(host, sections, index) {
  const target = sections[Math.max(0, Math.min(sections.length - 1, index))];
  if (!target) return;
  patch({ focusSection: target.id });
  focusKey(host, 'export-section-' + target.id);
}

function onTreeKey(e, host, sections) {
  const at = sections.findIndex((s) => s.id === cur().focusSection);
  if (NAV[e.key]) moveTo(host, sections, at + NAV[e.key]);
  else if (e.key === 'Home') moveTo(host, sections, 0);
  else if (e.key === 'End') moveTo(host, sections, sections.length - 1);
  else return;
  e.preventDefault();
}

/** The tree of sections (role tree, roving tabindex, selection follows focus). */
export function sectionTree(E, host) {
  const sections = E.detail.sections.sections;
  const tree = h('ul', { class: 'ai-sec-tree', role: 'tree', 'aria-label': 'Detected sections', 'data-fk': 'export-sections' });
  const items = sections.map((s) => {
    const chosen = s.id === E.focusSection;
    const custom = sectionSpec(E.plan, s.id);
    const mark = custom.family || custom.size != null ? [custom.family, sizeText(custom.size)].filter(Boolean).join(', ') : '';
    return h('li', {
      role: 'treeitem', 'aria-level': String(Math.max(1, s.level)), 'aria-selected': chosen ? 'true' : 'false',
      tabindex: chosen ? '0' : '-1', 'data-fk': 'export-section-' + s.id, class: 'ai-sec-item' + (chosen ? ' ai-selected' : ''),
      style: '--depth:' + Math.max(0, s.level - 1),
      onclick: () => moveTo(host, sections, sections.indexOf(s)),
    }, h('span', { class: 'ai-sec-title' }, sectionLabel(s)), mark ? h('small', { class: 'ai-quiet' }, ' · ' + mark) : null, chips(s));
  });
  tree.append(...items);
  tree.addEventListener('keydown', (e) => onTreeKey(e, host, sections));
  return tree;
}

/** The chosen section: picker, preview and substitution notice. Keeps its DOM per section. */
export class SectionPanel {
  constructor() {
    this.box = h('div', { class: 'ai-stack-tight ai-sec-panel' });
    this.sid = '';
    this.picker = null;
    this.preview = h('div', { class: 'ai-font-sample', 'data-fk': 'export-preview', 'aria-label': 'Preview of the section in the chosen fonts', role: 'group' });
    this.subst = h('div', { 'data-fk': 'export-subst-note', 'aria-live': 'polite' });
  }

  build(E, section) {
    this.sid = section.id;
    const own = sectionSpec(E.plan, section.id);
    this.picker = new FontPicker({
      fk: 'export-font-' + section.id, sizeFk: 'export-size-' + section.id, label: 'Section “' + sectionLabel(section) + '”',
      catalog: E.catalog, family: own.family, size: own.size, inheritLabel: 'Document default', sizeInheritLabel: 'Default size',
      onChange: (v) => setPlan(withSpec(cur().plan, { scope: 'section', sid: section.id }, v)),
    });
    const apply = btn('ai-ghost ai-sm', 'Apply to all sections', () => setPlan(applyToAll(cur().plan, section.id)),
      { 'data-fk': 'export-apply-all', title: 'Make this section’s font the document default and drop the other section choices' });
    this.box.replaceChildren(h('h4', { class: 'ai-label' }, 'Fonts of this section'), this.picker.box, h('div', { class: 'ai-row' }, apply), this.preview, this.subst);
  }

  update(E) {
    const section = E.detail.sections.sections.find((s) => s.id === E.focusSection);
    if (!section) { this.box.replaceChildren(); this.sid = ''; return; }
    if (section.id !== this.sid || this.picker.catalog !== E.catalog) this.build(E, section);
    else if (!this.box.contains(this.box.getRootNode().activeElement)) {
      const own = sectionSpec(E.plan, section.id);
      const v = this.picker.value();
      if (v.family !== own.family || v.size !== own.size) this.picker.set(own.family, own.size);
    }
    this.drawPreview(E, section);
  }

  drawPreview(E, section) {
    const headType = headingTypeOf(section);
    const body = resolveSpec(E.plan, 'body', section.id);
    const head = headType ? resolveSpec(E.plan, headType, section.id) : null;
    const style = (el, s, category) => {
      if (s && s.family) el.style.fontFamily = cssFor(E.catalog, s.family, category);
      if (s && s.size != null) el.style.fontSize = s.size + 'pt';
      return el;
    };
    const text = sectionText(E.detail.sections, section) || 'This section has no text.';
    const lines = [h('p', { class: 'ai-font-sample-body' }, text)];
    if (head) lines.unshift(style(h('p', { class: 'ai-font-sample-heading' }, sectionLabel(section)), head, 'sans'));
    this.preview.replaceChildren(...lines);
    style(this.preview, body, 'sans');
    const notes = substitutionNotes(E.catalog, [body.family, head && head.family]);
    const hint = h('small', { class: 'ai-quiet' }, 'The preview uses the fonts of your browser; the downloaded PDF uses the server’s.');
    this.subst.replaceChildren(...notes.map((t) => note('warn', t)), ...(notes.length ? [hint] : []));
  }
}
