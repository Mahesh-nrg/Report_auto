/* pwndoc-ai: Health section of AI Settings (administrators): checks and one-click fixes. */
import { api } from './ai-api.js';
import { btn, chip, focusKey, h, note, section } from './ai-dom.js';
import { HEALTH_LABEL, HEALTH_TONE, objects } from './ai-util.js';

const INTRO = 'Checks that PwnDoc and this gateway are set up to produce reports. '
  + 'Fixes are safe to repeat and are applied together, after you confirm.';
const CONFIRM_TEXT = 'Apply every available fix? This replaces PwnDoc’s audit-type list '
  + '(a backup is saved first) and creates missing languages and custom fields.';

function normChecks(list) {
  return objects(list).map((c) => ({
    id: String(c.id || ''), label: String(c.label || c.id || ''), status: String(c.status || ''),
    detail: String(c.detail || ''), fixable: c.fixable === true,
  }));
}

function checkRow(c) {
  return h('li', { class: 'ai-item' },
    h('div', { class: 'ai-item-head' },
      h('span', { class: 'ai-item-title' }, c.label),
      c.fixable ? chip('accent', 'Fix available') : null,
      chip(HEALTH_TONE[c.status] || 'mute', HEALTH_LABEL[c.status] || c.status)),
    c.detail ? h('p', { class: 'ai-item-meta' }, c.detail) : null);
}

class HealthPanel {
  constructor() {
    this.checks = [];
    this.applied = null;
    this.confirm = false;
    this.busy = false;
    this.error = '';
    this.loading = true;
    this.box = h('div', { class: 'ai-rag' });
    this.status = h('p', { class: 'ai-sr', role: 'status', 'aria-live': 'polite' });  // persistent live region
    this.section = section('Health', h('p', { class: 'ai-quiet' }, INTRO), this.box, this.status);
  }

  say(text) { this.status.textContent = text; }

  fixControls() {
    if (this.confirm) {
      return note('warn', h('p', { class: 'ai-confirm-text' }, CONFIRM_TEXT),
        h('div', { class: 'ai-row' },
          btn('ai-primary ai-sm', 'Yes, apply the fixes', () => this.fixAll(), { 'data-fk': 'health-fix-yes' }),
          btn('ai-ghost ai-sm', 'Cancel', () => { this.confirm = false; this.draw('health-fix'); }, { 'data-fk': 'health-fix-no' })));
    }
    return btn('ai-ghost ai-sm', 'Fix automatically', () => { this.confirm = true; this.draw('health-fix-yes'); },
      { disabled: this.busy ? true : null, 'data-fk': 'health-fix' });
  }

  checkList() {
    const appliedNote = this.applied == null ? null
      : this.applied.length ? note('ok', h('strong', {}, 'Applied: '), this.applied.join('; '))
        : note('ok', 'Nothing needed fixing.');
    return [
      appliedNote,
      this.checks.length ? h('ul', { class: 'ai-items' }, this.checks.map(checkRow)) : h('p', { class: 'ai-quiet' }, 'No checks reported.'),
      h('div', { class: 'ai-row' }, this.checks.some((c) => c.fixable) ? this.fixControls() : null,
        btn('ai-ghost ai-sm', 'Re-check', () => this.check(), { disabled: this.busy ? true : null, 'data-fk': 'health-recheck' })),
    ].filter(Boolean);
  }

  draw(focus) {
    if (this.loading) this.box.replaceChildren(h('p', { class: 'ai-quiet' }, 'Checking…'));
    else if (this.error) {
      this.box.replaceChildren(note('bad', this.error),
        h('div', { class: 'ai-row' }, btn('ai-ghost ai-sm', 'Try again', () => this.check(), { 'data-fk': 'health-retry' })));
    } else this.box.replaceChildren(...this.checkList());
    focusKey(this.box, focus);
  }

  async check() {
    Object.assign(this, { loading: true, error: '', confirm: false, applied: null });
    this.draw();
    try {
      const d = (await api('/health')) || {};
      this.checks = normChecks(d.checks);
      const bad = this.checks.filter((c) => c.status !== 'ok').length;
      this.say(bad ? bad + ' health checks need attention.' : 'All health checks are fine.');
    } catch (e) {
      this.error = e.message;
    }
    this.loading = false;
    this.draw();
  }

  async fixAll() {
    Object.assign(this, { busy: true, confirm: false });
    this.draw();
    try {
      const r = (await api('/health/fix', { method: 'POST' })) || {};
      this.applied = Array.isArray(r.applied) ? r.applied.map(String) : [];
      this.checks = normChecks(r.checks);
      this.say(this.applied.length ? 'Applied ' + this.applied.length + ' fixes.' : 'Nothing needed fixing.');
    } catch (e) {
      this.error = e.message;
    }
    this.busy = false;
    this.draw('health-recheck');
  }
}

export function healthSection() {
  const panel = new HealthPanel();
  panel.check();
  return panel.section;
}
