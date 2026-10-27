/* pwndoc-ai: constants and pure helpers (text, job normalisation). No DOM state lives here. */
export const API = '/__ai/api';
export const MASK = '********';
export const POLL_MS = 1500;
export const POLL_TIMEOUT_MS = 15000;      // one status poll
export const REQUEST_TIMEOUT_MS = 120000;  // ordinary API calls (an LLM draft can take a while)
export const UPLOAD_TIMEOUT_MS = 15 * 60 * 1000;  // indexing a scanned PDF can run for minutes
export const DOWNLOAD_TIMEOUT_MS = 5 * 60 * 1000;
const MAX_POLL_FAILS = 5;
export const MAX_LISTED = 100;  // longest issue / skipped list drawn at once
export const MAX_SOURCES = 6;
export const JOB_KEY = 'pwndoc-ai:job';
export const FIN_KEY = 'pwndoc-ai:finish';
export const DOCX_KEY = 'pwndoc-ai:docx';
export const EXPORT_KEY = 'pwndoc-ai:export';
const TERMINAL = new Set(['done', 'completed', 'complete', 'finished', 'cancelled', 'canceled', 'failed', 'error']);
const WORKING = new Set(['pending', 'queued', 'running']);
// The server writes an item when it is "proposed" (first apply) or "failed" (retry). Refine
// failures are "failed" too but carry no changes, so they never reach the apply list.
const APPLIABLE = new Set(['proposed', 'failed']);
export const FIELDS = [
  ['title', 'Title'], ['description', 'Description'], ['observation', 'Observation'],
  ['poc', 'Proof of concept'], ['scope', 'Scope'], ['remediation', 'Remediation'],
  ['references', 'References'], ['customFields', 'Custom fields'],
];
const FIELD_LABEL = Object.fromEntries(FIELDS);
export const STATUS_LABEL = {
  pending: 'Queued', queued: 'Queued', running: 'Working', proposed: 'Ready to review',
  applied: 'Applied', failed: 'Failed', error: 'Failed', skipped: 'No changes',
  unchanged: 'No changes', cancelled: 'Cancelled', canceled: 'Cancelled', done: 'Done',
  interrupted: 'Interrupted',
};
export const KIND_LABEL = { update: 'Update with AI', finish: 'Finish report' };
export const HEALTH_LABEL = { ok: 'OK', warn: 'Needs attention', error: 'Problem' };
export const HEALTH_TONE = { ok: 'ok', warn: 'warn', error: 'bad' };
export const SEVERITY_LABEL = { error: 'Error', warning: 'Warning', info: 'Note' };
export const SEVERITY_TONE = { error: 'bad', warning: 'warn', info: 'mute' };
const SESSION_EXPIRED_TEXT = 'Your PwnDoc session expired. Sign in again to continue.';

export const isTerminal = (s) => TERMINAL.has(String(s || '').toLowerCase());
export const shortId = (id) => (id ? String(id).slice(-6) : '');
export const fmtTokens = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n || 0));
export const objects = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === 'object') : []);
export const countFinished = (items) => items.filter((i) => !WORKING.has(i.status)).length;
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso || '') : d.toLocaleString();
}

/** Visible text of a value: HTML is reduced to text, arrays and objects are flattened. */
export function plain(v) {
  if (Array.isArray(v)) return v.map(plain).join('\n');
  if (v == null) return '';
  if (typeof v === 'object') return JSON.stringify(v, null, 1);
  let s = String(v);
  if (/<[a-z][\s\S]*>/i.test(s)) {
    s = s.replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, '').replace(/<img\b[^>]*>/gi, ' [image] ').replace(/<\/(p|div|li|h[1-6]|tr)>|<br\s*\/?>/gi, '\n');
    s = new DOMParser().parseFromString(s, 'text/html').body.textContent || '';
  }
  return s.replace(/\n{3,}/g, '\n\n').trim();
}

function normChanges(raw) {
  if (!raw) return [];
  const list = Array.isArray(raw)
    ? raw.map((c) => [c.field || c.label || c.name || '', c])
    : Object.entries(raw);
  return list
    .map(([field, c]) => {
      const isObj = c && typeof c === 'object' && !Array.isArray(c);
      const before = isObj ? (c.before !== undefined ? c.before : c.old) : '';
      const after = isObj ? (c.after !== undefined ? c.after : c.new) : c;
      return { field, label: FIELD_LABEL[field] || field, before: plain(before), after: plain(after) };
    })
    .filter((c) => c.before !== c.after);
}

function normItem(it) {
  let status = String(it.status || 'pending').toLowerCase();
  if (it.applied === true) status = 'applied';
  return {
    id: it.finding_id || it.id || it._id,
    title: it.title || '(untitled finding)',
    status, changes: normChanges(it.changes || it.diff), error: it.error || '', usage: it.usage || null,
    model: typeof it.model === 'string' ? it.model : '',
    sources: Array.isArray(it.sources) ? it.sources.filter((x) => typeof x === 'string' && x) : [],
  };
}

export function normJob(j) {
  const rawItems = j.items || j.findings || j.results || [];
  return {
    id: j.id || j.job_id, status: String(j.status || 'running').toLowerCase(), items: rawItems.map(normItem),
    usage: j.usage || null, error: j.error || '', auditId: j.audit_id || null, report: j.report === true,
    skipped: Array.isArray(j.skipped) ? j.skipped.map(String) : [],
  };
}

export const appliable = (it) => it.changes.length > 0 && APPLIABLE.has(it.status);

/** What a poll error means for a run: stop for good, or count a failure and retry. */
export function pollFailure(slice, e) {
  if (e.status === 404) return { stop: true, gone: true, message: 'This run is no longer available.' };
  if (e.status === 401) return { stop: true, message: SESSION_EXPIRED_TEXT };
  const fails = slice.pollFails + 1;
  if (fails > MAX_POLL_FAILS) {
    return { stop: true, fails, message: 'Lost connection to the gateway. Reopen this panel to resume.' };
  }
  return { stop: false, fails };
}
