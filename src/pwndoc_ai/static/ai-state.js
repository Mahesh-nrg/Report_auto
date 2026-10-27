/* pwndoc-ai: the one client-side state object, its constructors and the late-bound UI hooks.
 *
 * S is a single deliberately mutable holder (every module reads the same object). Each slice
 * (upd, fin, tools, hist, ...) is created by a fresh*() constructor and replaced wholesale on
 * reset; nested collections are replaced, not edited in place, where the slice is rebuilt. */
import { DOCX_KEY, EXPORT_KEY, FIELDS, FIN_KEY, JOB_KEY } from './ai-util.js';

export const freshUpd = () => ({
  auditId: null, findings: null, findingsFor: null, loadingFindings: false, scope: 'all', selected: new Set(),
  fields: new Set(FIELDS.map((f) => f[0])), mode: 'improve', auto: false,
  job: null, sig: '', error: '', pollError: '', note: '', warn: '', starting: false, applying: false,
  seen: new Set(), accept: new Set(), expanded: new Set(), pollFails: 0, appliedAny: false,
});
export const freshFin = () => ({
  auditId: null, job: null, sig: '', error: '', pollError: '', starting: false, pollFails: 0, downloaded: false,
});
const idle = () => ({ busy: false, error: '', data: null });
// Report tools of the Finish drawer (QA pass, executive summary, library); reset per audit.
export const freshTools = (auditId) => ({
  auditId, qa: { status: 'idle', issues: [], counts: null, error: '', open: false }, summary: idle(), library: idle(),
});
export const freshHist = (auditId) => ({
  auditId, loading: false, loadError: '', error: '', runs: [], backups: [], confirm: '', restoring: '', result: null,
});
// "Refine a DOCX": `gen` tells an answer that arrives after a reset (logout) that it is stale. Every
// sub-slice (files, detail, status, setup, changes, dl) is replaced, never edited, so a redraw can
// tell what changed by identity.
let docxGen = 0;
export const freshDocx = () => ({
  gen: ++docxGen, files: null, quota: null, listError: '', selectedId: null, detail: null, detailError: '',
  status: null, starting: false, pollFails: 0, pollError: '', actionError: '', confirmId: '', deletingId: '',
  notice: '', showSetup: null, setup: null, changes: { fileId: '', items: [], total: 0, loading: false, error: '' },
  dl: { busy: '', error: '' },
});
// "Export & fonts": the same replace-never-edit rule as the DOCX slice (see ai-export-store.js).
let exportGen = 0;
export const freshExport = () => ({
  gen: ++exportGen, catalog: null, engines: null, files: null, quota: null, listError: '', selectedId: null,
  detail: null, detailError: '', status: null, starting: false, pollFails: 0, error: '', notice: '',
  setup: { engine: 'auto', useAi: true, terms: '' }, plan: null, planRev: 0, saving: false, saveError: '',
  focusSection: '', confirmId: '', dl: { busy: '', error: '', info: '' },
});
const freshSettings = () => ({ data: null, loading: false, error: '', built: null });

const initialState = () => ({
  status: null, open: null, upd: freshUpd(), fin: freshFin(), dl: { busy: '', error: '' },
  set: freshSettings(), review: false, tools: freshTools(null), hist: freshHist(null), docx: freshDocx(), exp: freshExport(),
});

export const S = Object.assign({ route: { kind: 'other' } }, initialState());

/** Timers of the two polling loops, kept outside S so a reset can always cancel them. */
export const timers = { poll: null, fin: null, docx: null, export: null, exportSave: null };

/** The in-flight upload of the DOCX drawer, so a reset (logout) can abort it. */
export const aborts = { docxUpload: null, exportUpload: null };

export const isAdmin = () => !!(S.status && S.status.authenticated && (S.status.is_admin === true ||
  (S.status.is_admin === undefined && String(S.status.role).toLowerCase() === 'admin')));

function storeValue(key, value) {
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch (e) { /* storage unavailable: the run just cannot be resumed after a reload */ }
}
export const storeJob = (v) => storeValue(JOB_KEY, v);
export const storeFin = (v) => storeValue(FIN_KEY, v);
export const storeDocx = (v) => storeValue(DOCX_KEY, v);
export const storeExport = (v) => storeValue(EXPORT_KEY, v);
export function readStored(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (e) { return null; }
}

/** Forget everything about the signed-in user (logout, or the login page showing up). */
export function resetClientState() {
  clearTimeout(timers.poll);
  clearTimeout(timers.fin);
  clearTimeout(timers.docx);
  clearTimeout(timers.export);
  clearTimeout(timers.exportSave);
  if (aborts.docxUpload) aborts.docxUpload.abort();
  if (aborts.exportUpload) aborts.exportUpload.abort();
  storeJob(null);
  storeFin(null);
  storeDocx(null);
  storeExport(null);
  if (S.set.built) S.set.built.dispose();  // a signed-out user's uploads and polling stop with the session
  Object.assign(S, initialState());
}

/* Late-bound hooks, filled in by the drawer shell and the dock. Feature modules call these, so
 * none of them has to import the shell (no import cycles). */
export const ui = {
  renderBody: () => {}, renderDock: () => {}, openDrawer: () => {}, closeDrawer: () => {},
  announce: () => {},
};
