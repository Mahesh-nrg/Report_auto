/* pwndoc-ai: pure helpers of the "Layout check" (no DOM): what the gateway's check routes send,
 * the short sentences shown for it and the summary of a finished download. Importable from node,
 * so it is unit tested there. Every string from the gateway is clipped and only ever drawn as
 * text. */
export const CHECK_TIMEOUT_MS = 120000;  // the report is built first, then inspected
export const MAX_PHRASES = 6;
const MAX_TEXT = 200;
const MAX_ITEMS = 50;
const SEVERITIES = new Set(['error', 'warning', 'info']);
const FIXED_NOTE = 'These are fixed automatically in the download.';
const OFF_NOTE = 'Tidy layout is off, so these stay in the download.';
const NUMBER = /^\d{1,6}$/;

const clip = (v) => (typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f-\u009f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT) : '');
const whole = (v, fallback) => (Number.isInteger(v) && v >= 0 ? v : fallback);
const list = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === 'object').slice(0, MAX_ITEMS) : []);
const plural = (n, word) => n + ' ' + (n === 1 ? word : word.endsWith('x') ? word + 'es' : word + 's');

function normIssue(raw) {
  return Object.freeze({
    code: clip(raw.code), severity: SEVERITIES.has(raw.severity) ? raw.severity : 'info', message: clip(raw.message),
    fixable: raw.fixable === true, count: Math.max(1, whole(raw.count, 1)), where: clip(raw.where),
  });
}

function normFont(raw) {
  return Object.freeze({ font: clip(raw.font), resolved: clip(raw.resolved), compatible: raw.compatible === true });
}

/** The answer of a /check route; null when it is not an object. Never keeps unknown fields. */
export function normCheck(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const tidied = raw.tidied && typeof raw.tidied === 'object' ? raw.tidied : {};
  return Object.freeze({
    available: raw.available !== false, note: clip(raw.note),
    issues: Object.freeze(list(raw.issues).map(normIssue)), fonts: Object.freeze(list(raw.fonts).map(normFont)),
    removedBlank: whole(tidied.removed_blank_pages, 0),
  });
}

/** The things worth telling: the messages of the errors and warnings (at most MAX_PHRASES). */
export function layoutPhrases(check) {
  if (!check) return [];
  const found = check.issues.filter((i) => i.severity !== 'info' && i.message).map((i) => i.message);
  if (!found.length && check.removedBlank > 0) found.push(plural(check.removedBlank, 'blank page') + ' would appear');
  if (found.length <= MAX_PHRASES) return found;
  return [...found.slice(0, MAX_PHRASES - 1), 'and ' + (found.length - MAX_PHRASES + 1) + ' more'];
}

/** The families the PDF would show in a wider substitute font. */
export const incompatibleFonts = (check) => (check ? check.fonts.filter((f) => !f.compatible && f.font) : []);

export const tidyNote = (tidyOn) => (tidyOn ? FIXED_NOTE : OFF_NOTE);

/** One sentence per check: Looks good, or what was found and whether the download fixes it. */
export function layoutHeadline(check, tidyOn) {
  const phrases = layoutPhrases(check);
  if (!phrases.length) return 'Looks good';
  return phrases.join(', ') + '. ' + tidyNote(tidyOn);
}

/**
 * The font warning. A listed font is shown in a substitute: not installed at all, or only
 * approximated by an installed one (Aptos in Carlito), so the sentence claims neither. The install
 * command is for administrators only (AI Settings > Health); the words here are the same for
 * everybody except for who to ask.
 */
export function fontWarning(check, admin) {
  const fonts = incompatibleFonts(check);
  if (!fonts.length) return '';
  const names = fonts.map((f) => f.font);
  const verb = names.length === 1 ? ' is' : ' are';
  const who = admin ? 'The install command is under AI Settings, Health, PDF fonts.' : 'Ask an administrator to install the missing fonts.';
  return names.join(', ') + verb + ' shown in a substitute font in the PDF, so page breaks can differ from Word. ' + who;
}

/** The query suffix of a download: nothing when tidy is on (the default), ``tidy=0`` when off. */
export const tidyParam = (tidyOn) => (tidyOn ? '' : 'tidy=0');

/**
 * What a finished download did, from its response headers (``get(name)`` -> string or null):
 * ASCII digits only are believed. '' when there is nothing worth saying.
 */
export function downloadSummary(get, tidyOn) {
  if (!tidyOn) return 'Downloaded without the layout tidy.';
  const num = (name) => { const v = get(name); return typeof v === 'string' && NUMBER.test(v) ? Number(v) : 0; };
  const parts = [];
  if (num('X-Pdf-Removed-Blank')) parts.push('removed ' + plural(num('X-Pdf-Removed-Blank'), 'blank page'));
  if (get('X-Pdf-Toc-Updated') === '1') parts.push('table of contents page numbers updated');
  if (num('X-Layout-Fixes')) parts.push(plural(num('X-Layout-Fixes'), 'layout fix') + ' applied');
  if (!parts.length) return '';
  const text = parts.join(', ');
  return text.charAt(0).toUpperCase() + text.slice(1) + '.';
}
