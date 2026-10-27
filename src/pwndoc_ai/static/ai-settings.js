/* pwndoc-ai: AI Settings drawer (administrators). The form is built once per open and then left
 * alone, so background work (job polls, status refreshes) can never wipe what is being typed. */
import { api } from './ai-api.js';
import { healthSection } from './ai-health.js';
import { btn, field, focusKey, h, note, section, switchRow } from './ai-dom.js';
import { ragPanel } from './ai-rag.js';
import { embedderFields, embedderPatch } from './ai-rag-settings.js';
import { refreshStatus } from './ai-dock.js';
import { SettingsFonts, hasFontSettings } from './ai-settings-fonts.js';
import { S, isAdmin, ui } from './ai-state.js';
import { MASK } from './ai-util.js';

const render = () => ui.renderBody('settings');

/* [tokens, label]: 1M is a context-window size; the gateway lowers it to each model's real output limit. */
const TOKEN_PRESETS = [
  [1024, '1,024'], [2048, '2,048'], [4096, '4,096'], [8192, '8,192'], [16384, '16k (16,384)'],
  [32768, '32k (32,768)'], [65536, '64k (65,536)'], [128000, '128k (128,000)'],
  [1000000, '1M (each model’s own maximum)'],
];
const CUSTOM = 'custom';
const OTHER = '__other__';
const TOKENS_HINT = 'The most one answer may contain. A model’s own limit always applies: if you pick more than a model allows, the gateway lowers it to that model’s maximum by itself (1M simply means “as much as the model allows”).';

const FALLBACK_PLACEHOLDER = 'One model id per line, tried in order.\nUse the exact ids your provider lists (Fetch models).';
const LOCKED_HINT = 'Read-only here: whoever can change it can redirect every user’s session. '
  + 'Set it with the command line (pwndoc-ai config --set KEY=VALUE) or the environment.';

/* The two secrets behave alike: never sent back, a blank input keeps the stored value. */
const SECRETS = {
  llm_api_key: {
    label: 'API key', flag: 'llm_api_key_set',
    placeholder: (set) => (set ? 'Key stored. Leave blank to keep it.' : 'Paste your API key'),
    hint: (set) => (set ? 'A key is saved on the gateway and is never sent back to the browser. Pointing at a different endpoint clears it.' : 'Stored on the gateway only. Not set yet.'),
  },
  notify_webhook_url: {
    label: 'Webhook URL', flag: 'notify_webhook_url_set', clearLabel: 'Remove the stored webhook',
    placeholder: (set) => (set ? 'URL stored. Leave blank to keep it.' : 'https://hooks.example.com/…'),
    hint: (set) => (set
      ? 'A webhook is saved on the gateway and is never sent back to the browser. Told when a Finish report run ends; the message names the audit and the user.'
      : 'Told when a Finish report run ends (Slack, Teams or any incoming webhook). The message names the audit and the user. Not set yet.'),
  },
};

/* A text/number input with a dropdown of known values beside it. The <label> names ONLY the input
 * (for=id): a dropdown inside the label would put every option into the input's accessible name. */
function comboField(label, input, select, hint) {
  input.id = 'ai-f-' + input.name;
  return h('div', { class: 'ai-field' },
    h('label', { class: 'ai-field-label', for: input.id }, label), input, select, hint ? h('small', {}, hint) : null);
}

const lines = (el) => el.value.split('\n').map((x) => x.trim()).filter(Boolean);
const num = (el) => (el.value.trim() === '' ? undefined : Number(el.value));  // blank = leave unchanged

class SettingsForm {
  constructor(d) {
    this.d = d;
    this.f = {};
    this.secrets = [];
    this.held = new Set();
    this.saveButtons = [];
    this.rag = null;
    this.modelList = h('datalist', { id: 'ai-models' });
    this.models = [];
    this.typing = false;
    this.model = null;
    this.fallbackPick = null;
    this.fonts = null;
    this.msg = h('div', { 'aria-live': 'polite' });
    this.guardBox = h('div');
    this.saveBtn = btn('ai-primary', 'Save settings', () => this.saveClicked(), { 'data-fk': 'save' });
    this.saveButtons.push(this.saveBtn);
    this.body = h('div', {}, h('div', { class: 'ai-stack' }, this.msg, this.guardBox, ...this.sections()), this.modelList);
    this.view = {
      title: 'AI Settings', body: this.body,
      foot: [h('span', { class: 'ai-foot-info' }, 'Changes apply to every user of this gateway.'), this.saveBtn],
    };
    const known = S.set.models;  // a list fetched earlier for this endpoint is offered again
    if (known && known.base === d.llm_base_url) this.applyModels(known.names);
    this.baseline = this.snapshot();
  }

  /* ---- controls ---- */
  input(key, type, value, extra) {
    this.f[key] = h('input', Object.assign({ type, class: 'ai-input', value: value == null ? '' : value, name: key, 'data-fk': 'set-' + key }, extra));
    return this.f[key];
  }
  check(key, checked) {
    this.f[key] = h('input', { type: 'checkbox', checked, role: 'switch', 'data-fk': 'set-' + key });
    return this.f[key];
  }
  area(key, rows, value, placeholder) {
    const el = h('textarea', { class: 'ai-input', rows: String(rows), name: key, placeholder, spellcheck: 'false', 'data-fk': 'set-' + key });
    el.value = value;
    this.f[key] = el;
    return el;
  }
  secret(key) {
    const spec = SECRETS[key];
    const set = !!this.d[spec.flag];
    const ctl = this.input(key, 'password', '', { autocomplete: 'new-password', placeholder: spec.placeholder(set) });
    const labelEl = field(spec.label, ctl, spec.hint(set));
    const entry = { key, ctl, hintEl: labelEl.querySelector('small'), spec };
    this.secrets.push(entry);
    if (!spec.clearLabel) return labelEl;
    const box = this.check(key + '_clear', false);  // a blank input keeps the secret; this removes it
    entry.clearRow = switchRow(box, spec.clearLabel, 'Takes effect when you save. Nothing is sent to the webhook until a new URL is set.');
    entry.clearRow.hidden = !set;
    return h('div', { class: 'ai-stack' }, labelEl, entry.clearRow);
  }

  /* ---- values ---- */
  patch() {
    const f = this.f;
    return {
      llm_base_url: f.llm_base_url.value.trim(), llm_api_key: f.llm_api_key.value.trim() || MASK,
      llm_model: f.llm_model.value.trim(), temperature: num(f.temperature), max_output_tokens: num(f.max_output_tokens),
      use_web_search: f.use_web_search.checked, web_results: num(f.web_results), use_rag: f.use_rag.checked,
      redact_field_labels: lines(f.redact_field_labels), extra_instructions: f.extra_instructions.value,
      llm_fallback_models: lines(f.llm_fallback_models), mask_sensitive: f.mask_sensitive.checked,
      use_library: f.use_library.checked, normalise_before_download: f.normalise_before_download.checked, notify_webhook_url: f.notify_webhook_url.value.trim() || MASK,
      ...(f.notify_webhook_url_clear.checked ? { notify_webhook_url_clear: true } : {}),
      languagetool_url: f.languagetool_url.value.trim(), research_cache_days: num(f.research_cache_days),
      ...embedderPatch(f), ...(this.fonts ? this.fonts.patch() : {}),
    };
  }
  snapshot() {
    return JSON.stringify(Object.entries(this.f).map(([k, el]) => [k, el.type === 'checkbox' ? el.checked : el.value]));
  }
  isDirty() { return this.snapshot() !== this.baseline; }

  /* ---- feedback ---- */
  show(tone, text) {
    this.msg.replaceChildren(text ? note(tone, text) : '');
    if (text) this.msg.scrollIntoView({ block: 'nearest' });
  }
  flagInvalid(text) {
    for (const [key, el] of Object.entries(this.f)) {
      const hit = !!text && new RegExp('\\b' + key + '\\b').test(text);  // whole setting names only
      if (hit) { el.hidden = false; el.setAttribute('aria-invalid', 'true'); }
      else el.removeAttribute('aria-invalid');
    }
  }
  setBusy(on) {
    if (on) this.held = new Set(this.saveButtons.filter((b) => b.disabled));  // keep what was already off
    for (const b of this.saveButtons) b.disabled = on || this.held.has(b);
  }
  ask() {  // Escape / backdrop with unsaved edits
    const keep = () => { this.guardBox.replaceChildren(); focusKey(this.body, 'save'); };
    this.guardBox.replaceChildren(note('warn',
      h('p', { class: 'ai-confirm-text' }, 'You have unsaved changes. Close and discard them?'),
      h('div', { class: 'ai-row' },
        btn('ai-primary ai-sm', 'Discard changes', () => ui.closeDrawer(), { 'data-fk': 'set-discard' }),
        btn('ai-ghost ai-sm', 'Keep editing', keep, { 'data-fk': 'set-keep' }))));
    this.guardBox.scrollIntoView({ block: 'nearest' });
    focusKey(this.guardBox, 'set-keep');
  }

  /* ---- saving ---- */
  afterSave(data) {
    for (const { key, ctl, hintEl, spec, clearRow } of this.secrets) {
      const set = !!data[spec.flag];
      ctl.value = '';
      ctl.placeholder = spec.placeholder(set);
      if (clearRow) { clearRow.hidden = !set; this.f[key + '_clear'].checked = false; }
      if (hintEl) hintEl.textContent = spec.hint(set);
    }
    this.baseline = this.snapshot();
  }
  async save(quiet) {
    this.flagInvalid('');
    try {
      const resp = await api('/settings', { method: 'PUT', body: this.patch() });
      S.set.data = resp.settings || resp;
      this.afterSave(S.set.data);
      if (this.rag) this.rag.onSettingsSaved();
      await refreshStatus();
      if (!quiet) this.show('ok', 'Settings saved.');
      return true;
    } catch (e) {
      this.show('bad', e.message);
      this.flagInvalid(e.message);
      return false;
    }
  }
  async saveClicked() {
    this.setBusy(true);
    await this.save(false);
    this.setBusy(false);
  }
  async saveAndTest() {
    this.setBusy(true); this.show('ok', 'Saving and testing…');
    if (await this.save(true)) {
      try {
        const r = await api('/settings/test', { method: 'POST' });
        const ok = !(r && (r.ok === false || r.success === false));
        this.show(ok ? 'ok' : 'bad', (r && (r.message || r.reply || r.detail || r.error)) || (ok ? 'Connection OK.' : 'Test failed.'));
      } catch (e) { this.show('bad', e.message); }
    }
    this.setBusy(false);
  }
  async fetchModels() {
    this.setBusy(true); this.show('ok', 'Saving and fetching models…');
    if (await this.save(true)) {
      try {
        const r = await api('/models');
        const names = (Array.isArray(r) ? r : r.models || []).map((m) => (typeof m === 'string' ? m : m.id)).filter(Boolean);
        S.set.models = { base: this.f.llm_base_url.value.trim(), names };
        this.applyModels(names);
        this.show(names.length ? 'ok' : 'warn', names.length ? names.length + ' models found. Choose one from the Model list.' : 'The provider returned no models.');
      } catch (e) { this.show('bad', e.message); }
    }
    this.setBusy(false);
  }

  /* ---- model dropdowns (filled by Fetch models) ---- */
  applyModels(names) {
    this.models = [...new Set(names)].sort((a, b) => a.localeCompare(b));
    this.modelList.replaceChildren(...this.models.map((n) => h('option', { value: n })));
    this.syncModelSelect();
    this.refreshFallbackPicker();
  }
  syncModelSelect() {
    const { input, select } = this.model;
    if (!this.models.length) { select.hidden = true; input.hidden = false; return; }
    const current = input.value.trim();
    const unknown = current !== '' && !this.models.includes(current);
    select.replaceChildren(
      h('option', { value: '' }, 'Choose a model…'),
      ...(unknown ? [h('option', { value: current }, current + ' (not in the fetched list)')] : []),
      ...this.models.map((m) => h('option', { value: m }, m)),
      h('option', { value: OTHER }, 'Other (type a model id)…'));
    select.value = this.typing ? OTHER : current;
    select.hidden = false;
    input.hidden = !this.typing;
  }
  refreshFallbackPicker() {
    const pick = this.fallbackPick;
    if (!pick) return;
    const used = new Set([this.f.llm_model.value.trim(), ...lines(this.f.llm_fallback_models)]);
    const free = this.models.filter((m) => !used.has(m));
    pick.replaceChildren(
      h('option', { value: '' }, free.length ? 'Add a fallback model…' : 'All fetched models are already used'),
      ...free.map((m) => h('option', { value: m }, m)));
    pick.value = '';
    pick.hidden = !this.models.length;
    pick.disabled = !free.length;
  }
  addFallback(name) {
    if (!name) return;
    const area = this.f.llm_fallback_models;
    area.value = [...lines(area), name].join('\n');
    this.refreshFallbackPicker();
  }

  /** The drawer closed: stop whatever the panels still run (uploads, polling). */
  dispose() { if (this.rag) this.rag.dispose(); }

  /* ---- sections ---- */
  sections() {
    return [
      this.modelSection(), this.researchSection(), this.knowledgeSection(), this.privacySection(), this.fallbackSection(),
      this.maskingSection(), this.cacheSection(), this.integrationsSection(), this.styleSection(), this.typographySection(),
      this.connectionSection(), healthSection(),
    ];
  }
  modelSection() {
    const d = this.d;
    const fetchBtn = btn('ai-ghost ai-sm', 'Fetch models', () => this.fetchModels());
    const testBtn = btn('ai-ghost ai-sm', 'Test connection', () => this.saveAndTest());
    this.saveButtons.push(fetchBtn, testBtn);
    return section('Language model',
      field('Base URL', this.input('llm_base_url', 'url', d.llm_base_url, { placeholder: 'https://api.openai.com/v1', autocomplete: 'off' }),
        'Any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, LM Studio, vLLM…'),
      this.secret('llm_api_key'),
      this.modelField(d),
      h('div', { class: 'ai-row' }, fetchBtn, testBtn, h('small', { class: 'ai-quiet' }, 'Both save the values above first.')),
      h('div', { class: 'ai-two' },
        field('Temperature', this.input('temperature', 'number', d.temperature, { min: '0', max: '2', step: '0.1' })),
        this.tokensField(d.max_output_tokens)));
  }
  modelField(d) {
    const input = this.input('llm_model', 'text', d.llm_model, { list: 'ai-models', autocomplete: 'off', spellcheck: 'false' });
    input.classList.add('ai-combo-input');
    const select = h('select', { class: 'ai-input ai-combo-select', hidden: true, 'aria-label': 'Model: choose from the fetched list', 'data-fk': 'set-llm_model-list' });
    this.model = { input, select };
    select.addEventListener('change', () => {
      this.typing = select.value === OTHER;
      if (this.typing) { input.hidden = false; input.focus(); return; }
      if (select.value) input.value = select.value;
      input.hidden = true;
      this.refreshFallbackPicker();
    });
    input.addEventListener('input', () => { this.typing = true; this.refreshFallbackPicker(); });
    return comboField('Model', input, select, null);
  }
  tokensField(value) {
    const input = this.input('max_output_tokens', 'number', value, { min: '256', max: '1000000', step: '1' });
    input.classList.add('ai-combo-input');
    const select = h('select', { class: 'ai-input ai-combo-select', 'aria-label': 'Token limit: choose a size', 'data-fk': 'set-max_output_tokens-preset' },
      ...TOKEN_PRESETS.map(([n, label]) => h('option', { value: String(n) }, label)),
      h('option', { value: CUSTOM }, 'Custom…'));
    const preset = TOKEN_PRESETS.find(([n]) => n === Number(input.value));
    select.value = preset ? String(preset[0]) : CUSTOM;
    input.hidden = !!preset;
    select.addEventListener('change', () => {
      if (select.value === CUSTOM) { input.hidden = false; input.focus(); return; }
      input.value = select.value;
      input.hidden = true;
    });
    return comboField('Max output tokens', input, select, TOKENS_HINT);
  }
  researchSection() {
    const d = this.d;
    return section('Research context',
      switchRow(this.check('use_web_search', !!d.use_web_search), 'Web search (DuckDuckGo)', 'Looks up CWE, OWASP and remediation guidance for each finding.'),
      field('Pages per finding', this.input('web_results', 'number', d.web_results, { min: '0', max: '10', step: '1' })));
  }
  knowledgeSection() {
    const d = this.d;
    this.rag = ragPanel();
    return section('Knowledge base',
      switchRow(this.check('use_rag', !!d.use_rag), 'Local documents (RAG)', 'Use your own books, notes and standards as extra context.'),
      this.rag.box, embedderFields(this, d));
  }
  privacySection() {
    return section('Privacy',
      field('Hide these custom fields from the model',
        this.area('redact_field_labels', 3, (this.d.redact_field_labels || []).join('\n'), 'One label per line, e.g. Vulnerable URL'),
        'Values of fields with these labels are never sent to the LLM and are never changed by it.'));
  }
  fallbackSection() {
    this.fallbackPick = h('select', { class: 'ai-input', hidden: true, 'aria-label': 'Add a fallback model from the fetched list', 'data-fk': 'set-llm_fallback_models-add' });
    this.fallbackPick.addEventListener('change', () => this.addFallback(this.fallbackPick.value));
    const area = this.area('llm_fallback_models', 4, (this.d.llm_fallback_models || []).join('\n'), FALLBACK_PLACEHOLDER);
    area.addEventListener('input', () => this.refreshFallbackPicker());
    this.refreshFallbackPicker();
    return section('Model fallbacks',
      field('Fallback models', area,
        'Tried in order when the main model is filtered, rate-limited or unavailable. The model that answered is shown on each finding. After Fetch models, pick more from the list below.'),
      this.fallbackPick);
  }
  maskingSection() {
    return section('Masking',
      switchRow(this.check('mask_sensitive', this.d.mask_sensitive !== false), 'Mask client names, domains, IPs and e-mails',
        'The model sees believable stand-ins instead: a fictional company name, fictional domains (sub-domains such as app or api are kept) and fictional IP addresses of the same kind. Its answer is converted back to the real values before anything is saved. Endpoints, paths, query parameters and PoC payloads are sent as they are, so a password, token or cookie inside a payload is NOT masked unless it equals a client name.'));
  }
  cacheSection() {
    const d = this.d;
    return section('Reference and cache',
      switchRow(this.check('use_library', d.use_library !== false), 'Use the vulnerability library', 'Shows the wording of matching library entries to the model as reference.'),
      switchRow(this.check('normalise_before_download', d.normalise_before_download !== false), 'Normalise findings before download',
        'Before a Word/PDF download, findings get a CVSS 3.1 score and vector (estimated when missing), the severity derived from it, CWE/CVE or N/A and N/A in empty fields, and are moved into Critical-to-Informational order. Written behind the automatic backup (Restore can undo it). Off: the report is exported as stored and only the verification is reported.'),
      field('Research cache (days)', this.input('research_cache_days', 'number', d.research_cache_days == null ? 14 : d.research_cache_days, { min: '0', max: '365', step: '1' }),
        'Reuse web and document research for the same finding title. 0 turns the cache off; leave blank to keep the current value.'));
  }
  integrationsSection() {
    return section('Integrations',
      this.secret('notify_webhook_url'),
      field('LanguageTool URL', this.input('languagetool_url', 'url', this.d.languagetool_url, { placeholder: 'http://localhost:8010', autocomplete: 'off' }),
        'Optional spelling check in the report check. The text of every finding is sent to this server WITHOUT masking, so use a LanguageTool server you host yourself, never a public one. Link-local and cloud-metadata addresses are refused.'));
  }
  /** Default fonts of downloaded reports (only when the gateway knows these settings). */
  typographySection() {
    if (!hasFontSettings(this.d)) return null;
    // The catalog arrives later and may re-label the current choice: that is not an edit.
    this.fonts = new SettingsFonts(this.d, (apply) => { const clean = !this.isDirty(); apply(); if (clean) this.baseline = this.snapshot(); });
    Object.assign(this.f, this.fonts.fields());
    return this.fonts.box;
  }
  styleSection() {
    return section('Style', field('Extra instructions',
      this.area('extra_instructions', 4, this.d.extra_instructions || '', 'House style, language, tone, severity conventions…'), null));
  }
  connectionSection() {
    const locked = (label, value) => field(label,
      h('input', { class: 'ai-input', readonly: true, 'aria-readonly': 'true', value, 'data-fk': 'locked-' + label }), LOCKED_HINT);
    return section('PwnDoc connection',
      locked('PwnDoc URL', this.d.pwndoc_url || ''),
      locked('Verify TLS certificate', this.d.verify_tls ? 'On' : 'Off'));
  }
}

export async function prepareSettings() {
  const D = S.set;
  Object.assign(D, { error: '', built: null });
  if (!isAdmin()) { render(); return; }
  D.loading = true;
  render();
  try {
    const d = await api('/settings');
    D.data = d.settings || d;
  } catch (e) {
    D.error = e.message;
  }
  D.loading = false;
  render();
}

export function viewSettings() {
  const D = S.set;
  if (!isAdmin()) return { title: 'AI Settings', body: note('warn', 'Only PwnDoc administrators can change AI settings.'), foot: [] };
  if (D.loading || !D.data) {
    return { title: 'AI Settings', body: D.error ? note('bad', D.error) : h('p', { class: 'ai-quiet' }, 'Loading settings…'), foot: [] };
  }
  if (!D.built) D.built = new SettingsForm(D.data);
  return D.built.view;
}

/** Escape / backdrop / X: with unsaved edits the form asks first (returns true to stop the close). */
export function settingsGuard() {
  const form = S.set.built;
  if (!form || !form.isDirty()) return false;
  form.ask();
  return true;
}

/** The drawer closed or was replaced: the settings panels stop their uploads and timers. */
export function disposeSettings() {
  if (S.set.built) S.set.built.dispose();
}
