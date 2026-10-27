/* pwndoc-ai: download the audit's report (Word or PDF), built on demand by the gateway. */
import { api, detailText, fetchApi } from './ai-api.js';
import { btn, h, note } from './ai-dom.js';
import {
  failedLayout, freshLayout, layoutAnnouncement, layoutBlock, readyLayout,
} from './ai-layout.js';
import { CHECK_TIMEOUT_MS, downloadSummary, normCheck, tidyParam } from './ai-layout-util.js';
import { root } from './ai-shell.js';
import { S, isAdmin, ui } from './ai-state.js';
import { DOWNLOAD_TIMEOUT_MS } from './ai-util.js';

const FORMAT_LABEL = { docx: 'Word', pdf: 'PDF' };
const renderDl = () => ui.renderBody(['download', 'finish']);
// S.dl also holds: tidy (the "Tidy layout" switch, on unless false), layout (the check of the
// audit's report) and info (what the last download did). A reset replaces S.dl, so all three go.
const tidyOn = () => S.dl.tidy !== false;

export function dispositionName(res, fallback) {
  const cd = res.headers.get('Content-Disposition') || '';
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  if (star) { try { return decodeURIComponent(star[1]); } catch (e) { /* fall back to the quoted name */ } }
  const quoted = /filename="([^"]+)"/i.exec(cd);
  return quoted ? quoted[1] : fallback;
}

export async function failureText(res) {
  let detail = '';
  try { detail = detailText(await res.json()); } catch (e) { /* not JSON */ }
  return detail || 'Download failed (' + res.status + ').';
}

export function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name, style: 'display:none' });
  root.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/**
 * The report is built on demand from the audit in PwnDoc, so it can be downloaded at any time.
 * Resolves 'ok', 'error' (S.dl.error says why) or 'busy' (another download is running).
 */
export async function downloadAuditReport(auditId, format, opts) {
  const D = S.dl;
  if (D.busy) return 'busy';
  D.busy = format; D.error = ''; D.info = '';
  renderDl();
  try {
    const query = ['format=' + format, tidyParam(tidyOn())].filter(Boolean).join('&');
    const path = '/audits/' + encodeURIComponent(auditId) + '/report?' + query;
    const res = await fetchApi(path, {}, DOWNLOAD_TIMEOUT_MS);
    if (!res.ok) throw new Error(await failureText(res));
    saveBlob(await res.blob(), dispositionName(res, 'report.' + format));
    D.info = downloadSummary((name) => res.headers.get(name), tidyOn());  // a note: the shell announces it
    return 'ok';
  } catch (e) {
    const why = e.message || 'Download failed.';
    D.error = opts && opts.auto ? 'The report was built, but the automatic download failed: ' + why + ' Use the download buttons.' : why;
    return 'error';
  } finally {
    D.busy = '';
    renderDl();
  }
}

export const downloadButtons = (auditId, primary) => ['docx', 'pdf'].map((format) =>
  btn(format === primary ? 'ai-primary' : 'ai-ghost',
    S.dl.busy === format ? 'Preparing…' : 'Download ' + FORMAT_LABEL[format],
    () => downloadAuditReport(auditId, format),
    { disabled: S.dl.busy ? true : null, 'data-fk': 'download-' + format }));

/** Ask the gateway what the layout of this audit's report looks like; a failure never blocks a download. */
export async function loadLayout(auditId) {
  const D = S.dl;
  const mine = freshLayout();
  D.layout = mine;
  renderDl();
  try {
    const check = normCheck(await api('/audits/' + encodeURIComponent(auditId) + '/report/check', { timeoutMs: CHECK_TIMEOUT_MS }));
    if (S.dl.layout !== mine) return;
    S.dl.layout = check ? readyLayout(check) : failedLayout();
  } catch (e) {
    if (S.dl.layout !== mine) return;
    S.dl.layout = failedLayout();
  }
  const spoken = layoutAnnouncement(S.dl.layout, tidyOn(), true);
  if (spoken) ui.announce(spoken);
  renderDl();
}

export function prepareDownload() {
  S.dl.error = '';
  S.dl.info = '';
  ui.renderBody();
  if (S.route.auditId) loadLayout(S.route.auditId);
}

function layoutHandlers(auditId) {
  return {
    onTidy: (checked) => { S.dl.tidy = checked; renderDl(); },
    onRetry: () => loadLayout(auditId),
  };
}

export function viewDownload() {
  const body = h('div', { class: 'ai-stack' });
  if (S.dl.error) body.append(note('bad', S.dl.error));
  body.append(
    h('p', { class: 'ai-lead' }, 'Download this audit’s report.'),
    h('p', { class: 'ai-quiet' }, 'Built right now from the audit as it is saved in PwnDoc, using its report template. Download it as often as you like.'),
    h('p', { class: 'ai-quiet' }, 'Word is what PwnDoc produces. PDF is the same document rendered by LibreOffice.'),
    layoutBlock(S.dl.layout, tidyOn(), layoutHandlers(S.route.auditId), isAdmin()));
  if (S.dl.info) body.append(note('ok', S.dl.info));
  return { title: 'Download report', body, foot: downloadButtons(S.route.auditId, 'docx') };
}
