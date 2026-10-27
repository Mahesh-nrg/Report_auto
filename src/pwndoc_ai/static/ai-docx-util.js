/* pwndoc-ai: pure helpers of the "Refine a DOCX" drawer (no DOM): picking a file, validating and
 * normalising what the gateway sends, section picking, the refine request body and the word-level
 * diff behind the highlighted review. Importable from node, so it is unit tested there. */
import { extOf, fmtBytes, stampMs } from './ai-rag-files.js';
import { objects } from './ai-util.js';

export const MAX_DOCX_BYTES = 50 * 1024 * 1024;  // the gateway's cap for one upload
export const CHANGES_PAGE = 50;                  // review rows asked for per request
export const MAX_TERMS = 50;
export const MAX_TERM_CHARS = 100;
const MAX_NAME_CHARS = 200;
const MAX_TITLE_CHARS = 300;
const MAX_NOTE_CHARS = 300;
export const MAX_SHOWN_TEXT = 4000;              // longest paragraph text drawn in the review
const DIFF_MAX_CELLS = 250000;                   // beyond this the middle is simply marked changed
const FRONT_MATTER = 'Text before the first heading';

const ID_RE = /^[0-9a-f]{32}$/i;
const SECTION_RE = /^s\d{1,6}$/;
const FILE_STATES = new Set(['uploaded', 'running', 'done', 'error', 'cancelled', 'damaged']);
const CHANGE_STATES = new Set(['applied', 'rejected', 'unchanged']);
const MODES = new Set(['light', 'deep']);
const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const JUST_NOW_MS = 45 * SECOND;

export const isFileId = (v) => typeof v === 'string' && ID_RE.test(v);
const count = (v) => (Number.isInteger(v) && v >= 0 ? v : 0);
const words = (v, max) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, max || MAX_NOTE_CHARS) : '');

/* ---------- picking a file ---------- */

/** Why the picked files cannot be uploaded, in words for the person ('' when they can). */
export function checkDocxPick(files, maxBytes) {
  const limit = maxBytes === undefined ? MAX_DOCX_BYTES : maxBytes;
  if (!files || files.length === 0) return 'Choose a Word (.docx) file.';
  if (files.length > 1) return 'Choose one .docx file at a time.';
  const file = files[0];
  const name = String((file && file.name) || '');
  if (!name || name.length > MAX_NAME_CHARS) return 'The file name is too long or missing.';
  const ext = extOf(name);
  if (ext === '.doc') return 'Older .doc files are not supported. Save the report as .docx in Word first.';
  if (ext !== '.docx') return 'Only Word (.docx) documents are supported.';
  if (!(file.size > 0)) return 'The file is empty.';
  if (Number.isFinite(limit) && file.size > limit) return 'The file is larger than ' + fmtBytes(limit) + '.';
  return '';
}

/** The comma or line separated client names: trimmed, de-duplicated, bounded. */
export function parseTerms(text) {
  const seen = new Set();
  const terms = [];
  for (const piece of String(text || '').split(/[,\n]/)) {
    const term = piece.replace(/\s+/g, ' ').trim();
    if (!term || seen.has(term.toLowerCase())) continue;
    seen.add(term.toLowerCase());
    terms.push(term);
  }
  if (terms.length > MAX_TERMS) return { terms, error: 'Give at most ' + MAX_TERMS + ' names.' };
  if (terms.some((t) => t.length > MAX_TERM_CHARS)) return { terms, error: 'Each name can have at most ' + MAX_TERM_CHARS + ' characters.' };
  return { terms, error: '' };
}

/* ---------- what the gateway sends ---------- */

export function normFile(raw) {
  if (!raw || typeof raw !== 'object' || !isFileId(raw.id)) return null;
  const name = words(raw.name, MAX_NAME_CHARS);
  if (!name) return null;
  const created = stampMs(raw.created);
  return Object.freeze({
    id: raw.id.toLowerCase(), name, size: Number.isFinite(raw.size) && raw.size >= 0 ? raw.size : 0,
    created: Number.isNaN(created) ? 0 : created,
    state: FILE_STATES.has(raw.state) ? raw.state : 'uploaded', mode: MODES.has(raw.mode) ? raw.mode : '',
    changed: count(raw.changed), rejected: count(raw.rejected), protected: count(raw.protected),
  });
}

export function normFiles(raw) {
  const list = Array.isArray(raw) ? raw : (raw && raw.files) || [];
  return Array.isArray(list) ? list.map(normFile).filter(Boolean) : [];
}

export function normQuota(raw) {
  const q = raw && typeof raw === 'object' ? raw : null;
  if (!q || !Number.isFinite(q.used_bytes) || !Number.isFinite(q.max_bytes)) return null;
  return Object.freeze({ usedBytes: q.used_bytes, maxBytes: q.max_bytes, files: count(q.files), maxFiles: count(q.max_files) });
}

function normSection(raw) {
  if (typeof raw.id !== 'string' || !SECTION_RE.test(raw.id)) return null;
  const title = words(raw.title, MAX_TITLE_CHARS) || (raw.id === 's0' ? FRONT_MATTER : '(untitled section)');
  return Object.freeze({ id: raw.id, title, level: Math.min(count(raw.level), 9), units: count(raw.units), chars: count(raw.chars) });
}

export const normOutline = (raw) => objects(raw).map(normSection).filter(Boolean);

export function normStats(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  return Object.freeze({
    paragraphs: count(s.paragraphs), refinable: count(s.refinable), protected: count(s.protected),
    tables: count(s.tables), images: count(s.images), sections: count(s.sections), words: count(s.words),
  });
}

export function normStatus(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  return Object.freeze({
    state: FILE_STATES.has(s.state) ? s.state : 'uploaded', total: count(s.total), done: count(s.done),
    changed: count(s.changed), rejected: count(s.rejected), protected: count(s.protected),
    current: words(s.current), error: words(s.error), started: s.started == null ? null : s.started,
    ended: s.ended == null ? null : s.ended,
  });
}

/** A short summary of the last run of a document (field names follow the engine's run stats). */
export function normLastRun(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const names = Array.isArray(raw.models) ? raw.models : Array.isArray(raw.model_names) ? raw.model_names : [];
  const models = names.map((m) => words(m, 80)).filter(Boolean).slice(0, 5);
  return Object.freeze({
    mode: MODES.has(raw.mode) ? raw.mode : '', changed: count(raw.changed), unchanged: count(raw.unchanged),
    rejected: count(raw.rejected), protected: count(raw.protected), chunks: count(raw.chunks),
    failedChunks: count(raw.failed_chunks), models,
  });
}

const PDF_FAILURES = {
  501: 'PDF export is not available: LibreOffice is not installed on the gateway. Word downloads still work.',
  422: 'This document links to external content or has very large images, so it cannot be converted to PDF. Word downloads still work.',
  503: 'The PDF converter is busy. Try again in a moment.',
  502: 'The PDF could not be produced. Word downloads still work.',
};

/** The readable text for a failed download; '' when the gateway's own (short, safe) message should be used. */
export function downloadFailureText(status, format) {
  if (format === 'pdf' && PDF_FAILURES[status]) return PDF_FAILURES[status];
  if (status === 409) return 'Nothing has been refined yet. Run the refinement first.';
  return '';
}

const text = (v) => (typeof v === 'string' ? v.slice(0, MAX_SHOWN_TEXT) : '');

function normChange(raw) {
  return Object.freeze({
    unitId: words(raw.unit_id, 40), section: words(raw.section, MAX_TITLE_CHARS), before: text(raw.before),
    after: text(raw.after), status: CHANGE_STATES.has(raw.status) ? raw.status : 'unchanged', reason: words(raw.reason),
  });
}

export function normChanges(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const changes = objects(r.changes).map(normChange);
  return { total: count(r.total) || changes.length, changes };
}

export function fmtAge(ms, now) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  const diff = now - ms;
  const plural = (n, unit) => n + ' ' + unit + (n === 1 ? '' : 's') + ' ago';
  if (diff < JUST_NOW_MS) return 'just now';
  if (diff < HOUR) return Math.max(1, Math.round(diff / MINUTE)) + ' min ago';
  if (diff < DAY) return Math.round(diff / HOUR) + ' h ago';
  return plural(Math.floor(diff / DAY), 'day');
}

/* ---------- picking sections ---------- */

export const defaultPicked = (outline) => new Set(outline.filter((s) => s.units > 0).map((s) => s.id));

/** The sections nested under a heading: the ones after it with a deeper level. */
export function descendants(outline, id) {
  const at = outline.findIndex((s) => s.id === id);
  if (at < 0 || outline[at].level < 1) return [];
  const out = [];
  for (let i = at + 1; i < outline.length && outline[i].level > outline[at].level; i++) out.push(outline[i].id);
  return out;
}

export function pickedTotals(outline, picked) {
  const chosen = outline.filter((s) => picked.has(s.id));
  return {
    sections: chosen.length, units: chosen.reduce((n, s) => n + s.units, 0), chars: chosen.reduce((n, s) => n + s.chars, 0),
  };
}

/** The JSON body of POST /docx/files/{id}/refine for the setup the person made. */
export function refineBody(setup, outline) {
  return {
    mode: setup.mode === 'deep' ? 'deep' : 'light',
    section_ids: setup.scope === 'pick' ? outline.filter((s) => setup.picked.has(s.id)).map((s) => s.id) : null,
    client_terms: parseTerms(setup.terms).terms,
  };
}

/* ---------- word-level diff ---------- */

const tokens = (s) => String(s || '').match(/\s+|\S+/g) || [];
const isSpace = (t) => /^\s+$/.test(t);

/** Which tokens of two arrays are outside their longest common subsequence: [flagsA, flagsB]. */
function lcsFlags(a, b) {
  const n = a.length;
  const m = b.length;
  const width = m + 1;
  const dp = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = a[i] === b[j] ? dp[(i + 1) * width + j + 1] + 1 : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1]);
    }
  }
  const flagsA = new Array(n).fill(true);
  const flagsB = new Array(m).fill(true);
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { flagsA[i++] = false; flagsB[j++] = false; }
    else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) i++;
    else j++;
  }
  return [flagsA, flagsB];
}

/** A space between two changed words belongs to the change, so "old words" is one highlight. */
function bridgeSpaces(toks, flags) {
  return flags.map((f, i) => f || (i > 0 && i < toks.length - 1 && isSpace(toks[i]) && flags[i - 1] && flags[i + 1]));
}

function segments(toks, flags) {
  const out = [];
  toks.forEach((t, i) => {
    const last = out[out.length - 1];
    if (last && last.changed === flags[i]) out[out.length - 1] = { text: last.text + t, changed: last.changed };
    else out.push({ text: t, changed: flags[i] });
  });
  return out;
}

/**
 * Highlight what changed between two paragraphs. Returns { before, after }: lists of
 * { text, changed } whose texts join back to the inputs. Pure insertions and deletions show up
 * on one side only. A middle part too large to compare exactly is marked changed as a whole.
 */
export function wordDiff(before, after) {
  const a = tokens(before);
  const b = tokens(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let endA = a.length;
  let endB = b.length;
  while (endA > head && endB > head && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const midA = a.slice(head, endA);
  const midB = b.slice(head, endB);
  const [midFlagsA, midFlagsB] = midA.length * midB.length > DIFF_MAX_CELLS
    ? [midA.map(() => true), midB.map(() => true)] : lcsFlags(midA, midB);
  const flagsA = [...a.slice(0, head).map(() => false), ...midFlagsA, ...a.slice(endA).map(() => false)];
  const flagsB = [...b.slice(0, head).map(() => false), ...midFlagsB, ...b.slice(endB).map(() => false)];
  return { before: segments(a, bridgeSpaces(a, flagsA)), after: segments(b, bridgeSpaces(b, flagsB)) };
}
