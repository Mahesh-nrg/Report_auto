/* pwndoc-ai: Shadow DOM host, drawer shell (open, close, focus, render), live regions. */
import { focusKey, h, icon } from './ai-dom.js';
import { S, ui } from './ai-state.js';

export const host = h('div', { id: 'pwndoc-ai-host' });
host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483000;visibility:hidden';
export const root = host.attachShadow({ mode: 'open' });
const styleEl = h('style');
export const dockMount = h('div');
const layer = h('div');
// Live regions live next to the layer, never inside content that is replaced on every render.
const politeEl = h('div', { class: 'ai-sr', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
const notesEl = h('div', { class: 'ai-sr', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
const alertEl = h('div', { class: 'ai-sr', role: 'alert', 'aria-live': 'assertive', 'aria-atomic': 'true' });
root.append(styleEl, dockMount, layer, politeEl, notesEl, alertEl);

const VIEWS = new Map();  // drawer kind -> { prepare(), render(), guard?(), dispose?() }
export const registerView = (kind, spec) => VIEWS.set(kind, spec);

/* ---------- styles and theme ---------- */
export async function loadStyles() {
  const link = document.querySelector('link[href*="inject.css"]');
  const urls = [link && link.href, new URL('inject.css', import.meta.url).href, '/__ai/static/inject.css'].filter(Boolean);
  for (const u of urls) {
    try {
      const r = await fetch(u, { credentials: 'same-origin' });
      if (r.ok) { styleEl.textContent = await r.text(); return; }
    } catch (e) { /* try the next location */ }
  }
  console.warn('pwndoc-ai: could not load inject.css'); // eslint-disable-line no-console
}

export function syncTheme() {
  const b = document.body;
  const dark = b.classList.contains('body--dark') ||
    (!b.classList.contains('body--light') && window.matchMedia('(prefers-color-scheme: dark)').matches);
  host.classList.toggle('ai-dark', dark);
}

/* ---------- live regions ---------- */
const spoken = { polite: '', notes: '', alert: '' };
function say(el, slot, text) {
  if (text === spoken[slot]) return;
  spoken[slot] = text;
  el.textContent = text;
}
export const announce = (text) => say(politeEl, 'polite', text || '');

const noteText = (tone) => Array.from(layer.querySelectorAll('.ai-note.ai-' + tone)).map((n) => n.textContent.trim()).join(' ');
function announceNotes() {
  say(alertEl, 'alert', noteText('bad'));
  say(notesEl, 'notes', [noteText('ok'), noteText('warn')].filter(Boolean).join(' '));
}

/* ---------- drawer ---------- */
let drawerEl = null;
let bodyEl = null;
let footEl = null;
let titleEl = null;
let returnKey = '';

const FOCUSABLE = 'button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';
const focusables = () => Array.from(drawerEl.querySelectorAll(FOCUSABLE)).filter((n) => n.getClientRects().length > 0);
const activeKey = () => {
  const a = root.activeElement;
  return (a && a.getAttribute && a.getAttribute('data-fk')) || '';
};

/** Re-render while keeping scroll position and the focused control (matched by data-fk). */
function keepFocus(fn, wantKey) {
  const key = wantKey || activeKey();
  const scroller = layer.querySelector('.ai-body');
  const top = scroller ? scroller.scrollTop : 0;
  fn();
  const sc = layer.querySelector('.ai-body');
  if (sc) sc.scrollTop = top;
  if (key && key !== activeKey()) focusKey(layer, key);
}

export function openDrawer(kind) {
  const spec = VIEWS.get(kind);
  if (!spec) return;
  if (S.open) closeDrawer({ silent: true });
  else returnKey = activeKey();
  S.open = kind;
  titleEl = h('h2', { id: 'ai-title', class: 'ai-title' });
  bodyEl = h('div', { class: 'ai-body' });
  footEl = h('div', { class: 'ai-foot' });
  drawerEl = h('aside', { class: 'ai-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'ai-title' },
    h('header', { class: 'ai-head' },
      h('div', {}, h('p', { class: 'ai-kicker' }, kind === 'settings' ? 'Administration' : 'AI report assistant'), titleEl),
      h('button', { type: 'button', class: 'ai-x', 'aria-label': 'Close', onclick: requestClose }, icon('close'))),
    bodyEl, footEl);
  const backdrop = h('div', { class: 'ai-backdrop', onclick: requestClose });
  layer.replaceChildren(backdrop, drawerEl);
  host.style.visibility = 'visible';
  requestAnimationFrame(() => { backdrop.classList.add('ai-open'); drawerEl.classList.add('ai-open'); });
  spec.prepare();
  const first = focusables()[0] || drawerEl.querySelector('.ai-x');
  if (first) first.focus({ preventScroll: true });
}

export function closeDrawer(opts) {
  const spec = VIEWS.get(S.open);
  S.open = null;
  if (spec && spec.dispose) spec.dispose();
  layer.replaceChildren();
  drawerEl = bodyEl = footEl = titleEl = null;
  if (opts && opts.silent) return;
  say(alertEl, 'alert', '');
  say(notesEl, 'notes', '');
  say(politeEl, 'polite', '');
  const key = returnKey;
  returnKey = '';
  if (focusKey(root, key)) return;
  const first = root.querySelector('.ai-dock button');
  if (first) first.focus({ preventScroll: true });
}

/** Close from the user's side (Escape, backdrop, X): a view with unsaved work may ask first. */
function requestClose() {
  const spec = VIEWS.get(S.open);
  if (spec && spec.guard && spec.guard()) return;
  closeDrawer();
}

/**
 * Redraw the open drawer. `kinds` (a kind or a list of kinds) names the drawers the caller's data
 * belongs to: a background update for another drawer is ignored, so e.g. a job poll can never
 * rebuild the Settings form someone is typing in. `wantKey` moves focus to that data-fk control.
 */
export function renderBody(kinds, wantKey) {
  if (!S.open || !bodyEl) return;
  if (kinds && ![].concat(kinds).includes(S.open)) return;
  keepFocus(() => {
    const view = VIEWS.get(S.open).render();
    titleEl.textContent = view.title;
    // A view that keeps its own DOM between renders (Settings) is left in place.
    if (view.body !== bodyEl.firstChild) {
      bodyEl.replaceChildren(view.body);
      footEl.replaceChildren(...view.foot);
      footEl.hidden = view.foot.length === 0;
    }
    if (view.announce) announce(view.announce);
  }, wantKey);
  announceNotes();
}

document.addEventListener('keydown', (e) => {
  if (!S.open || !drawerEl) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); requestClose(); return; }
  if (e.key !== 'Tab') return;
  const f = focusables();
  if (!f.length) return;
  const active = root.activeElement;
  const inside = drawerEl.contains(active);
  if (e.shiftKey && (active === f[0] || !inside)) { e.preventDefault(); f[f.length - 1].focus(); }
  else if (!e.shiftKey && (active === f[f.length - 1] || !inside)) { e.preventDefault(); f[0].focus(); }
}, true);

Object.assign(ui, { renderBody, openDrawer, closeDrawer, announce });
