/* pwndoc-ai: the state slice of the "Import a DOCX" drawer (S.imp) and its sessionStorage key.
 *
 * S.imp is replaced, never edited: every change goes through patch(), which also derives the
 * drawer step (S.imp.step) and asks the open drawer to redraw. `gen` grows on every reset, so an
 * answer that arrives after a logout or a user switch is recognised as stale and dropped. */
import { S } from './ai-state.js';
import { stepOf } from './ai-import-util.js';

export const IMPORT_KEY = 'pwndoc-ai:import';

let impGen = 0;
export const freshImp = () => ({
  gen: ++impGen, user: '', list: null, listError: '', id: null, status: null, plan: null, planError: '',
  uploading: null, starting: false, saving: false, posting: false, confirm: '', actionError: '', notice: '',
  pollFails: 0, pollError: '', acceptBlanks: false, confirmDuplicate: false, dirty: false, useLlm: true,
  step: 'pick',
});

S.imp = freshImp();

/** Filled in by the open drawer (redraw) and the run module (stop); no-ops otherwise. */
export const hooks = { redraw: () => {}, stop: () => {} };

export const cur = () => S.imp;

export function patch(fields) {
  const next = { ...S.imp, ...fields };
  S.imp = { ...next, step: stepOf(next) };
  hooks.redraw();
}

/** An answer is still wanted: no reset since `gen`, and (when given) the same import is open. */
export const live = (gen, id) => S.imp.gen === gen && (id === undefined || S.imp.id === id);

export function storeImport(value) {
  try {
    if (value) sessionStorage.setItem(IMPORT_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(IMPORT_KEY);
  } catch (e) { /* storage unavailable: the import just is not reopened after a reload */ }
}

export function readImport() {
  try { return JSON.parse(sessionStorage.getItem(IMPORT_KEY) || 'null'); } catch (e) { return null; }
}

/** Forget everything (logout, another user signed in): polling and uploads stop too. */
export function resetImport() {
  hooks.stop();
  storeImport(null);
  S.imp = freshImp();
}

/** Called when the dock is drawn: a signed-out or different user gets a fresh slice. */
export function syncImportUser(status) {
  const user = status && status.authenticated ? String(status.username || '') : '';
  if (!user) { if (S.imp.user || S.imp.id) resetImport(); return; }
  if (S.imp.user && S.imp.user !== user) resetImport();
  if (S.imp.user !== user) S.imp = { ...S.imp, user };
}
