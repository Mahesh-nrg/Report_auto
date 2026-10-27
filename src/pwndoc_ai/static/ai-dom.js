/* pwndoc-ai: tiny DOM helpers. Data is only ever rendered through text nodes (never innerHTML). */
const SVGNS = 'http://www.w3.org/2000/svg';
// Event properties h() accepts, and only as functions. Anything else named on* is dropped so a
// string from data can never become an inline handler attribute.
const EVENT_PROPS = new Set(['onclick', 'onchange', 'oninput', 'onkeydown', 'onsubmit', 'onfocus', 'onblur']);

export function add(el, kid) {
  if (kid == null || kid === false) return;
  if (Array.isArray(kid)) kid.forEach((k) => add(el, k));
  else el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
}

const PROP_SETTERS = {
  class: (el, v) => { el.className = v; },
  text: (el, v) => { el.textContent = v; },
  value: (el, v) => { el.value = v; },
  checked: (el, v) => { el.checked = v; },
};

function setProp(el, k, v) {
  if (PROP_SETTERS[k]) PROP_SETTERS[k](el, v);
  else if (/^on/i.test(k)) {
    if (EVENT_PROPS.has(k) && typeof v === 'function') el.addEventListener(k.slice(2), v);
  } else el.setAttribute(k, v === true ? '' : String(v));
}

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v != null && v !== false) setProp(el, k, v);
  }
  add(el, kids);
  return el;
}

const ICONS = {
  close: ['M6 6l12 12', 'M18 6L6 18'],
  chev: ['M8 10l4 4 4-4'],
  spark: ['M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z', 'M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z'],
  sliders: ['M4 7h9', 'M17 7h3', 'M4 17h3', 'M11 17h9', 'M15 5v4', 'M9 15v4'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  trash: ['M5 7h14', 'M10 7V5h4v2', 'M7 7l1 12h8l1-12'],
  upload: ['M12 16V5', 'M7.5 9.5L12 5l4.5 4.5', 'M5 19h14'],
  doc: ['M7 3h7l4 4v14H7z', 'M14 3v4h4', 'M10 12h5', 'M10 16h5'],
  download: ['M12 4v11', 'M7.5 10.5L12 15l4.5-4.5', 'M5 19h14'],
  type: ['M5 7V5h14v2', 'M12 5v14', 'M9 19h6'],
  history: ['M4 12a8 8 0 1 0 2.6-5.9', 'M4 4v4.5h4.5', 'M12 8v4.2l3 1.8'],
};

export function icon(name) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('class', 'ai-icon');
  s.setAttribute('aria-hidden', 'true');
  for (const d of ICONS[name]) {
    const p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('d', d);
    s.append(p);
  }
  return s;
}

export const btn = (cls, label, onclick, extra) =>
  h('button', Object.assign({ type: 'button', class: 'ai-btn ' + cls, onclick }, extra), label);

/* Notes carry no ARIA role: the drawer shell announces the notes of each render through its
 * persistent live regions, so a re-render does not re-announce unchanged text. */
export const note = (tone, ...kids) => h('div', { class: 'ai-note ai-' + tone }, kids);

export function choice(type, name, label, checked, onchange, fk, hint) {
  return h('label', { class: 'ai-choice' },
    h('input', { type, name, checked, onchange, 'data-fk': fk }),
    h('span', { class: 'ai-choice-box', 'aria-hidden': 'true' }, type === 'checkbox' ? icon('check') : null),
    h('span', { class: 'ai-choice-text' }, label, hint ? h('small', {}, hint) : null));
}

/** A switch around an existing checkbox control. */
export const switchRow = (ctl, label, hint) =>
  h('label', { class: 'ai-switch' }, ctl,
    h('span', { class: 'ai-track', 'aria-hidden': 'true' }),
    h('span', { class: 'ai-choice-text' }, label, hint ? h('small', {}, hint) : null));

export const toggle = (label, hint, checked, onchange, fk) =>
  switchRow(h('input', { type: 'checkbox', role: 'switch', checked, onchange, 'data-fk': fk }), label, hint);

export const section = (label, ...kids) => h('section', { class: 'ai-section' }, h('h3', { class: 'ai-label' }, label), kids);
export const chip = (tone, ...kids) => h('span', { class: 'ai-chip ai-chip-' + tone }, kids);
export const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');
export const field = (label, control, hint) =>
  h('label', { class: 'ai-field' }, h('span', { class: 'ai-field-label' }, label), control, hint ? h('small', {}, hint) : null);

/** Focus the element carrying data-fk=key inside a root (no selector escaping needed). */
export function focusKey(rootNode, key) {
  if (!key) return false;
  const target = Array.from(rootNode.querySelectorAll('[data-fk]')).find((n) => n.getAttribute('data-fk') === key);
  if (target) target.focus({ preventScroll: true });
  return !!target;
}

/**
 * Run a redraw of a self-managed block and put focus back on the control (data-fk) that had it,
 * when that control still exists afterwards.
 */
export function keepFocusKey(rootNode, redraw) {
  const scope = rootNode.getRootNode();
  const active = scope && scope.activeElement;
  const key = active && rootNode.contains(active) ? active.getAttribute('data-fk') : '';
  redraw();
  if (key) focusKey(rootNode, key);
}
