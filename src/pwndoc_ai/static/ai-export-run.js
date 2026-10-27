/* pwndoc-ai: the requests of the "Export & fonts" drawer: catalog, engines and file list, analysis
 * start / status polling / cancel, the analysed detail, the debounced plan save (PUT with rev,
 * 409 stale -> reload), downloads and delete. */
import { api, fetchApi } from './ai-api.js';
import { dispositionName, failureText, saveBlob } from './ai-download.js';
import {
  PLAN_SAVE_MS, downloadFailureText, isRunning, normCatalog, normEngines, normFile, normFiles, normPlan, normQuota,
  normSections, normStatus, parseTerms, planBody, samePlan,
} from './ai-export-util.js';
import { BASE, clearedSelection, cur, filePath, live, patch } from './ai-export-store.js';
import { S, storeExport, timers, ui } from './ai-state.js';
import { DOWNLOAD_TIMEOUT_MS, POLL_MS, POLL_TIMEOUT_MS, pollFailure } from './ai-util.js';

export async function loadCatalog() {
  const gen = cur().gen;
  const [fonts, engines] = await Promise.allSettled([api(BASE + '/fonts'), api(BASE + '/engines')]);
  if (!live(gen)) return;
  patch({
    catalog: normCatalog(fonts.status === 'fulfilled' ? fonts.value : null),
    engines: engines.status === 'fulfilled' ? normEngines(engines.value) : { info: {}, ocr: false },
    notice: fonts.status === 'rejected' ? 'The font list could not be loaded: ' + fonts.reason.message : cur().notice,
  });
}

export async function loadList() {
  const gen = cur().gen;
  try {
    const data = await api(BASE + '/files');
    if (live(gen)) patch({ files: normFiles(data), quota: normQuota(data), listError: '' });
  } catch (e) {
    if (live(gen)) patch({ files: cur().files || [], listError: 'The file list could not be loaded: ' + e.message });
  }
}

export function stopPolling() {
  clearTimeout(timers.export);
  timers.export = null;
}

/** A finished upload becomes the selected file (state "uploaded"). */
export function adoptUpload(raw) {
  const file = normFile(raw);
  if (!file) return;
  const files = [file, ...(cur().files || []).filter((f) => f.id !== file.id)];
  patch({ ...clearedSelection(), files, selectedId: file.id });
  loadList();
}

export function selectFile(id) {
  stopPolling();
  patch({ ...clearedSelection(), selectedId: id });
  const file = (cur().files || []).find((f) => f.id === id);
  if (!file || file.state === 'ready') loadDetail(id);
  if (!file || isRunning(file.state)) pollSoon(id, 0);
}

export async function loadDetail(id) {
  const gen = cur().gen;
  try {
    const raw = await api(filePath(id));
    if (!live(gen, id)) return;
    const detail = {
      file: normFile(raw.file), report: raw.report || null, sections: normSections(raw.sections),
      fontsUsed: Array.isArray(raw.fonts_used) ? raw.fonts_used : [],
    };
    const sections = detail.sections.sections;
    const focus = sections.some((s) => s.id === cur().focusSection) ? cur().focusSection : (sections[0] || {}).id || '';
    patch({ detail, plan: normPlan(raw.plan), planRev: Number(raw.plan_rev) || 0, detailError: '', focusSection: focus });
  } catch (e) {
    if (!live(gen, id)) return;
    if (e.status === 409) patch({ detail: null, detailError: '' });  // not analysed yet: the setup shows
    else patch({ detailError: e.status === 404 ? 'This file is no longer available.' : e.message });
  }
}

function pollSoon(id, delay) {
  stopPolling();
  timers.export = setTimeout(() => poll(id), delay === undefined ? POLL_MS : delay);
}

async function poll(id) {
  const gen = cur().gen;
  timers.export = null;
  try {
    const status = normStatus(await api(filePath(id, '/status'), { timeoutMs: POLL_TIMEOUT_MS }));
    if (!live(gen, id)) return;
    patch({ status, pollFails: 0, error: '' });
    if (isRunning(status.state)) { pollSoon(id); return; }
    storeExport(null);
    loadList();
    if (status.state === 'ready') { ui.announce('Sections detected.'); loadDetail(id); }
  } catch (e) {
    if (!live(gen, id)) return;
    const verdict = pollFailure(cur(), e);
    if (verdict.stop) { storeExport(null); patch({ error: verdict.message, status: null }); return; }
    patch({ pollFails: verdict.fails });
    pollSoon(id);
  }
}

export async function startAnalyse() {
  const E = cur();
  const id = E.selectedId;
  const terms = parseTerms(E.setup.terms);
  if (!id || E.starting || terms.error) return;
  const gen = E.gen;
  patch({ starting: true, error: '', detailError: '' });
  try {
    await api(filePath(id, '/analyse'), { method: 'POST', body: { engine: E.setup.engine, use_ai: E.setup.useAi, client_terms: terms.terms } });
    if (!live(gen, id)) return;
    storeExport({ id });
    patch({ starting: false, status: { state: 'analysing', done: 0, total: 0, step: '', error: '' }, detail: null });
    pollSoon(id, 0);
  } catch (e) {
    if (live(gen, id)) patch({ starting: false, error: e.message });
  }
}

export async function cancelAnalyse() {
  const E = cur();
  const id = E.selectedId;
  if (!id) return;
  try {
    await api(filePath(id, '/cancel'), { method: 'POST', body: {} });
    pollSoon(id, 0);
  } catch (e) {
    if (live(E.gen, id)) patch({ error: e.message });
  }
}

/* ---------- the plan ---------- */

/** A new plan from the pickers: drawn at once, saved after PLAN_SAVE_MS of quiet. */
export function setPlan(plan) {
  if (cur().plan && samePlan(plan, cur().plan)) return;  // e.g. "Other…" chosen, nothing typed yet
  patch({ plan, saveError: '' });
  clearTimeout(timers.exportSave);
  timers.exportSave = setTimeout(() => { timers.exportSave = null; savePlan(); }, PLAN_SAVE_MS);
}

export async function savePlan() {
  const E = cur();
  const id = E.selectedId;
  if (!id || !E.plan) return true;
  const gen = E.gen;
  const sent = E.plan;
  patch({ saving: true });
  try {
    const raw = await api(filePath(id, '/plan'), { method: 'PUT', body: { plan: planBody(sent), rev: E.planRev } });
    if (!live(gen, id)) return false;
    const changedMeanwhile = cur().plan !== sent;  // keep newer edits; the next save sends them
    patch({ saving: false, planRev: Number(raw.plan_rev) || 0, plan: changedMeanwhile ? cur().plan : normPlan(raw.plan) });
    return true;
  } catch (e) {
    if (!live(gen, id)) return false;
    if (e.status === 409) {
      patch({ saving: false, saveError: 'The font plan changed in another tab. It was reloaded; check it and try again.' });
      loadDetail(id);
    } else patch({ saving: false, saveError: e.message });
    return false;
  }
}

/** Save a pending edit now (before a download). */
async function flushPlan() {
  if (timers.exportSave === null) return true;
  clearTimeout(timers.exportSave);
  timers.exportSave = null;
  return savePlan();
}

export async function download(format) {
  const E = cur();
  const id = E.selectedId;
  if (!id || E.dl.busy) return;
  const gen = E.gen;
  patch({ dl: { busy: format, error: '', info: '' } });
  let error = '';
  let info = '';
  try {
    if (!(await flushPlan())) error = 'Save the font choices first.';
    else {
      const res = await fetchApi(filePath(id, '/download?format=' + format + '&tidy=1'), {}, DOWNLOAD_TIMEOUT_MS);
      if (res.ok) {
        saveBlob(await res.blob(), dispositionName(res, 'document.' + format));
        info = downloadInfo(res, format);
      } else error = downloadFailureText(res.status, format) || await failureText(res);
    }
  } catch (e) {
    error = e.message || 'Download failed.';
  }
  if (!live(gen, id)) return;
  patch({ dl: { busy: '', error, info } });
  if (info) ui.announce(info);
}

function downloadInfo(res, format) {
  const changed = res.headers.get('X-Typography') === '1';
  const subs = Number(res.headers.get('X-Font-Substitutes') || 0);
  const parts = [(format === 'pdf' ? 'PDF' : 'Word file') + ' downloaded' + (changed ? ' with your fonts.' : '; no font had to change.')];
  if (subs > 0) parts.push(subs + (subs === 1 ? ' font was' : ' fonts were') + ' substituted by the server.');
  return parts.join(' ');
}

export async function deleteFile(id) {
  const E = cur();
  const gen = E.gen;
  try {
    await api(filePath(id), { method: 'DELETE' });
    if (!live(gen)) return;
    if (cur().selectedId === id) { stopPolling(); storeExport(null); patch(clearedSelection()); }
    patch({ confirmId: '', files: (cur().files || []).filter((f) => f.id !== id), notice: 'File deleted.' });
    loadList();
  } catch (e) {
    if (live(gen)) patch({ confirmId: '', error: e.message });
  }
}

/** The drawer closed: polling and a pending save stop (the save is sent now). */
export function closeExport() {
  stopPolling();
  if (timers.exportSave !== null && S.exp.selectedId) flushPlan();
}
