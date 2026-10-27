/* pwndoc-ai: "Update with AI" dock + AI Settings, injected into the stock PwnDoc SPA.
 *
 * This file is only the loader. The UI is a set of small ES modules next to it (ai-*.js), pulled in
 * with one dynamic import so the page only has to carry <script src="/__ai/static/inject.js">.
 * All UI lives in a Shadow DOM so Quasar CSS cannot interfere. Data from the gateway (finding
 * titles, diffs) is only ever rendered via text nodes. */
(function () {
  'use strict';
  if (window.__pwndocAiLoaded) return;
  window.__pwndocAiLoaded = true;

  const own = document.currentScript && document.currentScript.src;
  const base = own ? new URL('./', own).href : '/__ai/static/';
  import(base + 'ai-boot.js').catch((e) => {
    window.__pwndocAiLoaded = false;  // a later reload of the script may try again
    console.warn('pwndoc-ai: the UI could not be loaded', e); // eslint-disable-line no-console
  });
})();
