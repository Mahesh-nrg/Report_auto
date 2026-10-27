/* pwndoc-ai: "Finish report" drawer: one-click rewrite of every finding, then the Word report. */
import { api } from './ai-api.js';
import { downloadAuditReport, downloadButtons } from './ai-download.js';
import { btn, h, note, toggle } from './ai-dom.js';
import { S, freshFin, freshTools, freshUpd, storeFin, timers, ui } from './ai-state.js';
import { orderSection } from './ai-order.js';
import { loadQa, qaSection, toolsSection } from './ai-tools.js';
import { loadFindings, startJob } from './ai-update.js';
import {
  POLL_MS, POLL_TIMEOUT_MS, STATUS_LABEL, countFinished, isTerminal, normJob, pollFailure, sleep,
} from './ai-util.js';
import { itemMeta, llmMissing, notConfigured, primaryModel, progressCard, statusChip } from './ai-widgets.js';

const render = (focusKey) => ui.renderBody('finish', focusKey);
const runActive = () => !!(S.fin.job && !isTerminal(S.fin.job.status));
const AUTO_DOWNLOAD_WAIT_TRIES = 20;
const AUTO_DOWNLOAD_WAIT_MS = 300;

export function prepareFinish() {
  const F = S.fin;
  if (F.job && F.auditId !== S.route.auditId && isTerminal(F.job.status)) S.fin = freshFin();
  if (!S.fin.job) S.fin.auditId = S.route.auditId;
  else if (!isTerminal(S.fin.job.status)) {  // e.g. polling gave up while the panel was shut
    Object.assign(S.fin, { pollError: '', pollFails: 0 });
    finPoll();
  }
  if (S.tools.auditId !== S.route.auditId) S.tools = freshTools(S.route.auditId);
  render();
  if (S.tools.qa.status !== 'loading' && !runActive()) loadQa();  // fresh on every open
}

async function startReportJob(kind) {
  const F = S.fin;
  const auditId = S.route.auditId;  // the audit this click was made on, even if the user navigates
  Object.assign(F, { starting: true, error: '' });
  render();
  try {
    const resp = await api('/audits/' + encodeURIComponent(auditId) + '/' + kind, { method: 'POST' });
    if (S.fin !== F) { storeFin({ id: resp.job_id, auditId }); return; }  // state reset while posting
    Object.assign(F, { job: normJob({ id: resp.job_id, status: 'running' }), auditId, sig: '', pollFails: 0, downloaded: false });
    storeFin({ id: F.job.id, auditId });
    ui.renderDock();
    finPoll();
  } catch (e) {
    F.error = e.status === 409
      ? 'You already have an AI run in progress (in “Update with AI” or another tab). Let it finish or cancel it first.'
      : e.message;
  }
  F.starting = false;
  render();
}

const startFinish = () => (S.review ? startReview() : startReportJob('finish'));

// "Review changes before saving": the same rewrite as Finish, but through the Update drawer
// (all findings, all fields, improve, no auto-apply) so the user reads every diff first.
async function startReview() {
  const F = S.fin;
  const auditId = S.route.auditId;
  if (S.upd.job && !isTerminal(S.upd.job.status)) {
    F.error = 'An “Update with AI” run is already in progress. Let it finish or cancel it first.';
    render();
    return;
  }
  Object.assign(F, { starting: true, error: '' });
  render();
  S.upd = freshUpd();
  S.upd.auditId = auditId;
  const V = S.upd;
  await loadFindings();
  F.starting = false;
  if (S.open !== 'finish' || S.route.auditId !== auditId || S.upd !== V) return;  // the user moved on
  if (V.error || !V.findings || !V.findings.length) {
    F.error = V.error || 'This audit has no findings to review.';
    V.error = '';
    render();
    return;
  }
  ui.openDrawer('update');
  startJob();
}

function finPollFailed(F, e) {
  const verdict = pollFailure(F, e);
  F.pollFails = verdict.fails || 0;
  if (verdict.gone) { F.job = null; F.error = verdict.message; storeFin(null); ui.renderDock(); }
  else if (verdict.stop) F.pollError = verdict.message;
  if (verdict.stop) render();
  return verdict.stop;
}

// The Word file is fetched once per run; a download that is already busy is waited for, never skipped.
async function autoDownload(F) {
  F.downloaded = true;
  for (let i = 0; i < AUTO_DOWNLOAD_WAIT_TRIES && S.dl.busy; i++) await sleep(AUTO_DOWNLOAD_WAIT_MS);
  const result = await downloadAuditReport(F.auditId, 'docx', { auto: true });
  if (result === 'busy') F.downloaded = false;
}

async function finPoll() {
  clearTimeout(timers.fin);
  const F = S.fin;
  if (!F.job) return;
  try {
    const j = normJob(await api('/jobs/' + encodeURIComponent(F.job.id), { timeoutMs: POLL_TIMEOUT_MS }));
    if (S.fin !== F) return;
    const recovered = !!F.pollError;
    Object.assign(F, { pollFails: 0, pollError: '' });  // errors raised by other actions stay
    const sig = JSON.stringify(j);
    if (sig !== F.sig) { F.sig = sig; F.job = j; render(); ui.renderDock(); }
    else if (recovered) render();
  } catch (e) {
    if (S.fin !== F || finPollFailed(F, e)) return;
  }
  if (F.job && !isTerminal(F.job.status)) { timers.fin = setTimeout(finPoll, POLL_MS); return; }
  storeFin(null);
  if (F.job && F.job.report && !F.downloaded) await autoDownload(F);
  if (S.tools.auditId === F.auditId) loadQa();  // the run changed the findings: check them again
  ui.renderDock();
  render();
}

async function cancelFinish() {
  const F = S.fin;
  F.error = '';
  try {
    await api('/jobs/' + encodeURIComponent(F.job.id) + '/cancel', { method: 'POST' });
  } catch (e) {
    F.error = e.message;
    render();
  }
  finPoll();
}

function resetFinish() {
  clearTimeout(timers.fin);
  storeFin(null);
  S.fin = freshFin();
  S.fin.auditId = S.route.auditId;
  ui.renderDock();
  render();
}

export function viewFinish() {
  const F = S.fin;
  const body = h('div', { class: 'ai-stack' });
  const foot = [];
  if (F.error) body.append(note('bad', F.error));
  if (F.pollError) body.append(note('bad', F.pollError));
  if (S.dl.error) body.append(note('bad', S.dl.error));
  let announce = '';
  if (!F.job) finishIntro(body, foot);
  else announce = finishRun(body, foot);
  return { title: 'Finish report', body, foot, announce };
}

function finishIntro(body, foot) {
  const F = S.fin;
  if (llmMissing()) body.append(notConfigured());
  body.append(
    h('p', { class: 'ai-lead' }, 'Complete this audit’s report in one step.'),
    h('ul', { class: 'ai-steps' },
      h('li', {}, 'Every finding in this audit is researched (web and your local documents) and rewritten: description, observation, PoC steps, scope, remediation, references, and custom fields such as CVSS, CWE and severity.'),
      h('li', {}, 'Your screenshots and URLs are kept. The new text replaces what is in PwnDoc now; a JSON backup of the originals is saved on the server first.'),
      h('li', {}, 'When it finishes, the Word report is built from the audit’s template and downloads automatically.')),
    qaSection(),
    orderSection(() => render()),
    toggle('Review changes before saving', 'Opens Update with AI with every finding and field selected, so you read each change and apply it yourself. No report is built until you download it.',
      S.review, (e) => { S.review = e.target.checked; render(); }, 'finish-review'),
    note('warn', 'Keep this tab open while it runs. Large audits can take several minutes.'),
    toolsSection());
  const label = F.starting ? 'Starting…' : S.review ? 'Finish and review changes' : 'Finish and download report';
  foot.push(btn('ai-primary', label, startFinish, { disabled: !F.starting && !llmMissing() ? null : true, 'data-fk': 'finish-start' }));
}

function readyNote(job, failed) {
  const lost = failed.length ? failed.length + ' finding' + (failed.length === 1 ? '' : 's') + ' could not be refined and keep their original text. ' : '';
  return note('ok', h('strong', {}, 'Your report is ready. '), lost,
    'The Word file downloads automatically; use the buttons below for Word or PDF any time.');
}

function finishLine(job, terminal, current, finished, total) {
  if (current) return 'Working on: ' + current.title;
  if (terminal) return job.report ? 'Saved to PwnDoc and exported.' : 'Run finished.';
  return total && finished === total ? 'Building the Word report…' : 'Waiting for the model…';
}

function finishFoot(job, terminal, finished, foot) {
  const F = S.fin;
  if (!terminal) foot.push(btn('ai-ghost', 'Cancel run', cancelFinish, { 'data-fk': 'finish-cancel' }));
  if (job.report) foot.push(...downloadButtons(F.auditId, 'docx'));
  if (terminal && finished > 0) foot.push(btn('ai-ghost', 'Reload page', () => location.reload(), { 'data-fk': 'finish-reload' }));
  if (terminal) foot.push(btn('ai-ghost', job.report ? 'New run' : 'Try again', resetFinish, { 'data-fk': 'finish-new' }));
}

/** Fills the run view and returns the sentence for the live region. */
function finishRun(body, foot) {
  const F = S.fin;
  const job = F.job;
  const terminal = isTerminal(job.status);
  const total = job.items.length;
  const finished = countFinished(job.items);
  const current = job.items.find((i) => i.status === 'running');
  const failed = job.items.filter((i) => i.status === 'failed' || i.status === 'error');
  if (F.auditId !== S.route.auditId) body.append(note('warn', 'This run belongs to a different audit.'));
  if (job.error) body.append(note('bad', job.error));
  if (job.report) body.append(readyNote(job, failed));
  const line = finishLine(job, terminal, current, finished, total);
  const heading = terminal ? (job.report ? 'Report ready' : (STATUS_LABEL[job.status] || 'Finished')) : 'Finishing report';
  body.append(progressCard({ heading, finished, total, line }));
  const primary = primaryModel(job.items);
  body.append(h('ul', { class: 'ai-items' }, job.items.map((it) => h('li', { class: 'ai-item' },
    h('div', { class: 'ai-item-head' }, h('span', { class: 'ai-item-title' }, it.title), statusChip(it)),
    it.error ? h('p', { class: 'ai-item-error' }, it.error) : null,
    itemMeta(it, primary)))));
  if (terminal) body.append(qaSection(), toolsSection());
  finishFoot(job, terminal, finished, foot);
  return heading + '. ' + line;
}

/** Resume a finish run found in sessionStorage after a page load. */
export function resumeFinish(saved) {
  S.fin.job = normJob({ id: saved.id, status: 'running' });
  S.fin.auditId = saved.auditId;
  ui.renderDock();
  finPoll();
}
