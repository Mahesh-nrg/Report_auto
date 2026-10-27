/* pwndoc-ai: the embedder settings of the Knowledge base section (embed_backend, ollama_model,
 * ollama_idle_seconds, ollama_url). They are shown only when the gateway sends them, so a gateway
 * that predates them keeps saving its settings without unknown keys. */
import { field, h } from './ai-dom.js';

const BACKENDS = [
  ['auto', 'Automatic: Ollama when it is installed, otherwise the built-in model'],
  ['ollama', 'Ollama only'],
  ['onnx', 'Built-in model only'],
];
const KEYS = ['embed_backend', 'ollama_model', 'ollama_idle_seconds', 'ollama_url'];

export const hasEmbedderSettings = (data) => KEYS.every((k) => data && data[k] !== undefined);

function backendSelect(form, value) {
  const el = h('select', { class: 'ai-input', name: 'embed_backend', 'data-fk': 'set-embed_backend' },
    BACKENDS.map(([v, label]) => h('option', { value: v }, label)));
  el.value = BACKENDS.some(([v]) => v === value) ? value : 'auto';
  form.f.embed_backend = el;
  return el;
}

/** The four fields as one block, registered in the form so they take part in saving and in the unsaved-changes check. */
export function embedderFields(form, d) {
  if (!hasEmbedderSettings(d)) return null;
  return h('div', { class: 'ai-stack' },
    field('Embedding backend', backendSelect(form, d.embed_backend),
      'Switching never re-indexes your documents: the built-in model and Ollama all-minilm give the same vectors.'),
    h('div', { class: 'ai-two' },
      field('Ollama model', form.input('ollama_model', 'text', d.ollama_model, { autocomplete: 'off', spellcheck: 'false' }),
        'all-minilm reuses the index you already have. Any other model gets a separate index that must be built from scratch.'),
      field('Idle timeout (seconds)', form.input('ollama_idle_seconds', 'number', d.ollama_idle_seconds, { min: '30', max: '3600', step: '1' }),
        'Ollama stops itself after this long without an embedding. From 30 to 3600.')),
    field('Ollama server URL', form.input('ollama_url', 'url', d.ollama_url, { placeholder: 'Empty: the gateway starts its own', autocomplete: 'off' }),
      'Leave empty to let the gateway start and stop its own Ollama. A URL uses that server instead, and the gateway never stops it. Loopback addresses are fine.'));
}

/** The embedder part of a settings patch (empty when the gateway does not know these settings). */
export function embedderPatch(f) {
  if (!f.embed_backend) return {};
  const idle = f.ollama_idle_seconds.value.trim();
  return {
    embed_backend: f.embed_backend.value, ollama_model: f.ollama_model.value.trim(),
    ...(idle === '' ? {} : { ollama_idle_seconds: Number(idle) }), ollama_url: f.ollama_url.value.trim(),
  };
}
