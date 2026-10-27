/* pwndoc-ai: follows PwnDoc's client-side routes (a single-page app) and reacts to them. */
import { refreshStatus, renderDock } from './ai-dock.js';
import { closeDrawer } from './ai-shell.js';
import { S, freshUpd, resetClientState } from './ai-state.js';
import { isTerminal } from './ai-util.js';

const AUDIT_RE = /^\/audits\/([0-9a-f]{24})(?:\/findings\/([0-9a-f]{24}))?(?:\/|$)/i;

export function parseRoute() {
  const p = location.pathname;
  if (p.startsWith('/login')) return { kind: 'login' };
  const m = AUDIT_RE.exec(p);
  if (m) return { kind: 'audit', auditId: m[1], findingId: m[2] || null };
  return { kind: 'other', settings: p.startsWith('/settings') };
}

const sameRoute = (a, b) =>
  a.kind === b.kind && a.auditId === b.auditId && a.findingId === b.findingId && !!a.settings === !!b.settings;

const updActive = () => !!S.upd.starting || !!(S.upd.job && !isTerminal(S.upd.job.status));
const finActive = () => !!S.fin.starting || !!(S.fin.job && !isTerminal(S.fin.job.status));

/** Close the drawers (and drop the idle state) that belong to an audit the user left. */
function leaveAudit(next) {
  const U = S.upd;
  const away = next.kind !== 'audit' || next.auditId !== U.auditId;
  if (away) {
    if (S.open === 'update' && !updActive()) closeDrawer();
    if (!updActive()) S.upd = freshUpd();
  } else if (!U.job) {
    U.scope = next.findingId ? 'one' : 'all';
  }
  if ((next.kind !== 'audit' || next.auditId !== S.fin.auditId) && S.open === 'finish' && !finActive()) closeDrawer();
  if (S.open === 'download' && next.kind !== 'audit') closeDrawer();
  if (S.open === 'history' && (next.kind !== 'audit' || next.auditId !== S.hist.auditId)) closeDrawer();
}

function onRoute() {
  const next = parseRoute();
  const prev = S.route;
  if (sameRoute(prev, next)) return;
  if (next.kind === 'login') {  // nothing of the previous session may survive a logout
    closeDrawer({ silent: true });
    resetClientState();
    S.route = next;
    renderDock();
    return;
  }
  S.route = next;
  if (!S.status || !S.status.authenticated || prev.kind === 'login') refreshStatus();
  leaveAudit(next);
  renderDock();
}

/** PwnDoc navigates with history.pushState: tell ourselves when it does. */
export function watchRoutes() {
  for (const m of ['pushState', 'replaceState']) {
    const orig = history[m];
    history[m] = function patched() {
      const r = orig.apply(this, arguments);
      window.dispatchEvent(new Event('pwndoc-ai:nav'));
      return r;
    };
  }
  window.addEventListener('pwndoc-ai:nav', onRoute);
  window.addEventListener('popstate', onRoute);
  if (window.navigation && window.navigation.addEventListener) {
    window.navigation.addEventListener('navigatesuccess', onRoute);
  }
}
