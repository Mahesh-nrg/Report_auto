# Import DOCX into PwnDoc: design contract

This contract is for the five parallel builders: B-READER, B-PLAN, B-WRITER, B-ROUTES and B-UI. Code against the signatures below. If one does not work, ask the integrator rather than inventing a variant.

The data types already exist; import them, do not redefine them.

| Module | What it holds |
|---|---|
| `src/pwndoc_ai/docx_import/model.py` | Report and preview types |
| `records.py` | Requests, journal, results, status, PwnDoc options |
| `limits.py` | Caps, id patterns, enums and the issue table |
| `jsoncheck.py` | Strict JSON validators |
| `errors.py` | User-safe exceptions |

Tests for them are in `tests/docx_import/test_model.py`.

## 0. Settled decisions (read first)

**Flow**

1. The user uploads the file through the existing `POST /__ai/api/docx/files` (docx_store precheck, quota and owner isolation). The importer then works from that `file_id`. There is no second upload path and no body-limit exception.
2. **Analyse** (job): reader, then PwnDoc lookups, then optional LLM fallback, then completeness. The result is an `ImportPlan` (the preview), stored server-side.
3. **Edit**: the UI sends small partial `PlanEdits` (one finding at a time) with the plan `revision`. The server cleans and validates, bumps `revision` and returns the whole plan.
4. **Create** (job, separate explicit step) through `PwnDocClient` with the user's own cookie. Every created id is journaled the moment PwnDoc returns it.
5. **Undo** (job) deletes exactly the journaled ids. It never deletes anything else.

**Router and identity**

- Router prefix: `/__ai/api/docx/import`. It must sit under `/__ai/api/docx` so the existing no-store prefix and the 1 MiB JSON cap apply.
- Every route uses `dependencies=[Depends(require_csrf_header)]`, `UserDep` and a throttle. No route is admin-only.
- Owner key: `gw.docx_owner(identity)`. Another owner's import is a 404 `"import not found"`.

**Reading and LLM**

- The reader is deterministic and label-driven. The LLM only fills fields the reader left blank, and only when `use_llm` is set and an LLM is configured. Without an LLM the analyse still succeeds, with the info issue `llm_unavailable`.
- Every LLM tool schema uses `"name": prompts.TOOL_NAME`.
- Get the LLM only through the `llm_factory` passed from `Gateway.__init__` (`lambda settings: LlmClient(settings)`). Never import `LlmClient` in `docx_import/*`; tests patch `gateway.LlmClient`.
- The audit name defaults to the file name without `.docx`. The document title is never trusted for the company or the audit name.
- The company comes from a majority vote of the header, the application table, the cover text box (`mc:Choice` only) and the disclaimer. It never comes from body prose. `TEMPLATE_LEFTOVER_NAMES = {"alfred ai"}` found in the body gives the warning `leftover_name`.

**Findings**

- Deduplicate by source id or by folded title (`re.sub(r"[^a-z0-9]", "", norm(t).lower())`). Keep the richest copy (most non-blank fields, then most characters). Fill its blanks from the other copy and union the URLs in order.
- A summary row with no detail table becomes a stub with the flag `summary_only`.
- The detail "Description" cell holds description and observation together. It all goes into `description`. The LLM may split it, but only as an exact partition (see §8).
- Creation order: included findings sorted by severity rank (Critical first, blank last), then CVSS score descending, then `source_id` ascending, then `key`. PwnDoc numbers findings IDX-001 onwards in creation order.

**Images**

- All screenshots go into `poc` and nowhere else; Final_1 renders images only there.
- An image is uploaded only after the audit exists, with `auditId`.
- An image is journaled only if `GET /api/images/:id` returns `auditId` equal to the new audit. PwnDoc deduplicates images by their bytes, so a reused row must never be deleted.
- Accepted types are sniffed from magic bytes: PNG, JPEG and GIF. WEBP, EMF, WMF, SVG and TIFF are `unsupported`.
- Images are not re-encoded because Pillow is not installed. Anything over `MAX_IMAGE_BYTES` is flagged `too_large` and excluded.
- Placeholder images (1x1 px, or under 200 bytes) are listed, flagged and excluded by default.

**Company, audit and undo**

- PwnDoc's `POST /api/audits` cannot set the company, dates, scope or template. The writer always follows it with `PUT /api/audits/:id/general`, which carries `company:{_id}` (never `{name}`, which loses the company on a duplicate name), `scope` as a list of strings, ISO dates and an explicit `template`.
- Duplicate audit: the analyse and every header edit compute `duplicate_audit`. `POST create` re-checks synchronously and answers 409 `duplicate_audit` unless `confirm_duplicate` is true. The writer checks once more inside the job. The importer never overwrites anything.
- An import creates in PwnDoc at most once (409 `already_created`). After an undo the import stays `undone` and cannot be created again; analyse the file again for a new import.
- `previous_import` warns when the same file (sha256) already reached `created` for this owner.
- Undo first reads each journaled audit. When it holds a finding the import did not create (written by hand later), the undo refuses with 409-style `ImportConflict("changed_since_import")` and deletes nothing (the step ends `undo_failed` with that sentence; the journal is unchanged, so the undo can be retried after the extra findings are removed). The writer's own rollback skips this check.
- Undo order: audit (its findings go with it), then the client, then the company, then the images. When an audit delete fails, the client, company and images are all `failed` (kept for a retry).
- The client, the company and the images may be shared after the import: a later import reuses a contact by e-mail and an image by its bytes, and anyone can pick the company for another audit. Each is deleted only when `undo_usage.scan_usage` could read all of PwnDoc (the caller is an administrator: `/api/users/me` role `admin`, at most `MAX_SCAN_AUDITS` audits) and found its 24-hex id in no other audit (`GET /api/audits/:id`), no other contact's company link (`GET /api/clients`) and no vulnerability library entry (`GET /api/vulnerabilities` and `/api/vulnerabilities/<locale>`). Otherwise the outcome is `kept`. For a non-administrator the company and the contact are therefore always `kept` (PwnDoc lists only the caller's own audits to them), and so are images (only an administrator may delete them; a 403 also gives `kept`). An image whose row now belongs to another audit is `kept`. A 404 gives the outcome `gone`.

## (a) Module map and public signatures

Every file has exactly one owner. Cross-owner calls are listed here. Internal helpers inside one owner's files are that builder's choice.

Shared modules, owned by the designer (builders do not edit them; ask the integrator):
`docx_import/__init__.py`, `model.py`, `records.py`, `limits.py`, `jsoncheck.py`, `errors.py`.

Read caps as `limits.NAME` at call time (`from . import limits`) so tests can monkeypatch them.

### B-READER (no LLM, no network, no PwnDoc)

Files: `docx_import/{reader,labels,header_fields,findings,dedupe,html,images,captions}.py` and `tests/docx_import/test_{reader,labels,header,findings,dedupe,html,images,captions}*.py`.

- `reader.read_report(data: bytes, *, file_name: str) -> ImportedReport`
  - Opens the file with `docx_engine.package.open_package` and parses with `docx_engine.xmlutil.parse_xml` (safe parser and caps). Raises `DocumentUnreadable` (`unreadable`, `not_template` or `no_findings`) or `ImportLimitError` (`too_large`, `too_many_findings` or `too_many_images`).
  - lxml is imported inside this module tree. Nothing outside `reader*` and its helpers imports lxml at module level.
  - It returns, in document order:
    - `findings`, with keys `f001`, `f002` and so on;
    - `images`, with keys `i001` and so on, `order` set to the document position and `finding_key == source_finding_key`;
    - `blobs`, the image bytes keyed by image key, one blob per distinct image key;
    - `issues`, holding only the codes the reader owns: `id_gaps`, `duplicates_merged`, `figures_deduped`, `image_*`, `leftover_name`, `company_conflict`, `severity_table_mismatch`, `contacts_absent` and `text_truncated`;
    - `stats`;
    - `header_text`, at most `MAX_HEADER_TEXT_CHARS` of cover, header and front-table text, and never finding text.
  - Unmatched images go after the nearest preceding finding in document order: an `image` block at the end of its `poc`, with `finding_key` set and the flag `unmatched`. This holds for every picture after the first detail table, a Heading 1 or a later section in between included. Pictures before the first detail table (cover logos) are not screenshots.
  - A picture in a one-line detail cell (title, location, CVSS, ...) is kept: it goes at the end of the finding's PoC like a picture of a rich cell, and its caption is not read as the cell value.
  - A picture whose relationship the package cannot give (unknown, external or damaged) is still listed, with no bytes, the flag `unsupported` and `include` false; nothing is fetched.
  - Word list items are numbered per list (`numId`) and level. A numbered list that continues after a picture (or a typed `5.` list) is rendered as `<p>5. ...</p>` paragraphs with its literal numbers, never as a fresh `<ol>` that would restart at 1.
  - Dedupe compares printed ids as numbers (`001` is `1`). Differing locations and parameters of the copies are joined (`a, b`); copies that disagree on severity, CWE or CVSS get the flag `copies_disagree` (warning `copies_disagree`).
  - Treat `undefined`, `N/A`, `-`, `null` and empty as blank (`labels.BLANK_RE`).
  - Dates: the table `DD.MM.YYYY` comes first, converted to ISO. The disclaimer is used only as a fallback, and an ambiguous disclaimer date is left blank.
- `labels.norm(text: str) -> str`: NFKC, nbsp to space, soft hyphen and ZWSP removed, smart quotes and dashes mapped, whitespace collapsed.
- `labels.fold_title(text: str) -> str`: alphanumeric lower-case fold.
- `labels.name_key(text: str) -> str`: `norm(text).casefold()`. The writer uses it to match company and audit names case-insensitively.
- `labels.is_blank(text: str) -> bool`.
- `html.clean_html(value: str) -> str`: re-parses untrusted HTML (from UI edits or the LLM) with `html.parser` and returns allow-list HTML (§h). It returns `""` when no text remains and never raises on hostile input.
- `html.plain_text(value: str) -> str`: allow-list HTML to plain text, with paragraphs joined by `\n`. Used for completeness, fact checks and LLM input.
- `html.image_tag(image_id: str, caption: str) -> str`: returns exactly `<img src="{id}" alt="{html.escape(caption, quote=True)}">`. Raises `ValueError` unless `limits.OBJECT_ID.fullmatch(image_id)`.
- `images.sniff(data: bytes) -> tuple[str, int, int]`: `(media_type or "", width, height)` read from the bytes (PNG IHDR, JPEG SOFn, GIF header). Never trust the extension or `[Content_Types].xml`.
- `images.data_url(data: bytes) -> str`: `data:<type>;base64,<b64>`. Raises `ImportInputError("image_not_usable")` if `sniff` fails or the size is over the cap.
- `captions.strip_caption(text: str) -> str | None`: the caption text without a leading `Fig`, `Figure N:`, `Figure N -` or `N.` prefix. Returns `None` when the paragraph is not a caption (`CAPTION_RE`).

### B-PLAN (preview, completeness, LLM fallback, masking)

Files: `docx_import/{plan,completeness,fallback,fallback_prompts,maskflow}.py` and `tests/docx_import/test_{plan,completeness,fallback,maskflow}*.py`.

- `plan.default_audit_name(file_name: str) -> str`: the base name without `.docx`, with whitespace collapsed and control characters dropped, capped at `MAX_NAME_CHARS`. Returns `"Imported report"` when that leaves nothing.
- `plan.build_plan(report: ImportedReport, *, import_id: str, file_name: str, file_sha256: str, options: PwnDocOptions, defaults: PlanDefaults, names: NameCheck, previous_import: str = "") -> ImportPlan`
  - `revision` is 1.
  - The header is built from `report.header` and `defaults`, with `audit_name = default_audit_name(file_name)`.
  - The contact is `report.header.contacts[0]` or `None`.
  - Issues come from `completeness.assess`.
- `plan.apply_edits(plan: ImportPlan, edits: PlanEdits, *, names: NameCheck | None = None) -> ImportPlan`
  - Raises `ImportConflict("stale")` when `edits.revision != plan.revision`. Raises `ImportInputError("unknown_key")` for an unknown finding or image key, and `ImportInputError("image_not_usable")` when an edit includes an image whose `usable` is false.
  - Rich fields and `html` PoC blocks go through `html.clean_html`. An html block that becomes empty is dropped.
  - An image moved to another finding (or to `""`) loses its `image` blocks everywhere else. The writer appends it at the end of the new finding's PoC.
  - When `names` is given it sets `company_id` and `duplicate_audit`.
  - Issues are recomputed and `revision` goes up by 1. The result must pass `model.check_references`.
- `completeness.assess(plan: ImportPlan) -> tuple[CompletenessIssue, ...]`
  - Derives every code from plan data: header blanks and dates; per finding `title_missing`, `severity_missing`, `cvss_missing` and `fields_blank` (with `fields`); `summary_only` and `text_truncated` from finding flags; `llm_fields`; image flags; `duplicate_audit`, `previous_import` and `company_existing` / `company_new` from `company_id`; and the source codes from `stats` and `source_header`.
  - It keeps existing issues whose code is in `STICKY = {"llm_unavailable", "llm_failed", "llm_skipped_unmasked"}`.
  - `cvss_missing` is raised when the vector is blank or does not parse (`cvss.parse_vector`); an edited vector that does not parse is refused (`ImportInputError`, field `findings[i].cvss_vector`) and a valid one is stored canonical.
  - Output is deterministic: header issues first, then findings in order, then images.
- `completeness.blockers(issues) -> tuple[CompletenessIssue, ...]` (level `blocker`).
- `completeness.warnings(issues) -> tuple[CompletenessIssue, ...]` (level `warning`). A create needs no blockers, and needs `accept_blanks=true` when there are warnings.
- `fallback.fill_gaps(report: ImportedReport, *, llm: Any, settings: Any, extra_terms: Sequence[str] = (), cancelled: Callable[[], bool] = lambda: False) -> FallbackOutcome`
  - `FallbackOutcome` is a frozen dataclass in `fallback.py`: `report: ImportedReport`, `calls: int`, `rejected: int`, `failed: bool` and `key_rejected: bool`.
  - A rejected answer is counted and keeps the blank. On `LlmError` it stops and sets `failed=True` (issue `llm_failed`). On `LlmAuthError` it also sets `key_rejected=True`.
  - It uses `settings.mask_sensitive`, `settings.extra_instructions` and `settings.max_output_tokens`.
  - Fail closed: with `mask_sensitive` on and no client name known after the header step (`maskflow.has_client_name`: no company and no candidate), no finding call is made and the info issue `llm_skipped_unmasked` is added.
- `fallback_prompts.header_messages(header_text: str, known: HeaderFields) -> tuple[str, str]` and `HEADER_TOOL`.
- `fallback_prompts.finding_messages(items: Sequence[dict]) -> tuple[str, str]` and `FINDING_TOOL`.
- `maskflow.finding_masker(*, enabled: bool, header: HeaderFields, extra_terms: Sequence[str]) -> Masker | NullMasker`
  - Terms: `CLIENT` for `header.company` and every `company_candidates` and `leftover_names` entry; `CLIENT` for each scope host name (`www.` dropped) and its last two labels (`maskflow.scope_hosts`), so the client's bare host names are recognised as client hosts and go through the domain stand-in map; `CLIENT` for each contact name and email; `TERM` for the extras.
  - The model sees realistic stand-ins (docs/MASKING_DESIGN.md): a fictional company for the client name, a fictional registrable domain under a reserved zone for each scope domain (sub-domain labels kept), IPs that keep their class and `/24` topology, e-mails with the fictional domain. Endpoints, paths, query names and payloads are sent as they are; a secret inside a payload is not masked unless it equals a client term. Values are masked before JSON serialisation so a name next to a `\n` or `\t` is still found.
  - It returns `NullMasker()` only when `enabled` is false and there are no terms (the same rule as `docx_jobs.masker_factory_for`).
- `maskflow.call_masked(llm, masker, system: str, user: str, tool: dict) -> dict`: masks with `mask_plain`, calls `llm.call_tool`, unmasks, then raises `FallbackRejected` when a stand-in (or an address from the fictional test ranges) is left in the answer, on a legacy `[[KIND_n]]` stray, or on a non-dict answer.
- `maskflow.call_header(llm, system: str, user: str, tool: dict) -> dict`: the only unmasked call (§i).

### B-WRITER (PwnDoc writes, journal, undo)

Files: `src/pwndoc_ai/pwndoc_client.py` (add methods only), `docx_import/{writer,lookups,journal,undo}.py`, `tests/test_pwndoc_client_import.py` and `tests/docx_import/test_{writer,lookups,journal,undo}*.py`.

`lookups.py` is added to the list given to B-WRITER.

Common types used below:
- `OpenClient = Callable[[], ContextManager[PwnDocClient]]`. The route passes `lambda: gw.client(gw.session_cookies(identity.username) or identity.cookies)`, so a rotated token is used for each phase.
- `Progress = Callable[[str, int, int], None]`, called as `(fixed step label, done, total)`.

Functions:
- `lookups.load_options(client: PwnDocClient) -> tuple[PwnDocOptions, PlanDefaults]`
  - Reads languages, audit types (with their template locales), templates and `list_audits()`.
  - Defaults come from the newest audit by `createdAt` (`get_audit_general` gives its template id). Otherwise the first language, the first audit type, and that type's template for the language. Templates are listed with that default first.
  - Values are capped by `MAX_OPTIONS`, and anything that fails the model patterns is skipped.
- `lookups.check_names(client: PwnDocClient, company: str, audit_name: str) -> NameCheck`
  - Matches the company by `labels.name_key` against `list_companies()`.
  - `duplicate_audit` is true when `list_audits()` has the same `name_key(name)` and the same company id. When the company is new, it compares names among audits with no company.
- `writer.creation_order(findings: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]`: included findings only, in the order from §0.
- `writer.poc_html(finding: ImportedFinding, images: Sequence[ImportedImage], image_ids: Mapping[str, str]) -> str`
  - Concatenates the html blocks. An image block becomes `html.image_tag(id, caption)` placed between complete blocks, but only when the image is included, its `finding_key` is this finding and it has an uploaded id.
  - Then it appends images whose `finding_key` is this finding but that have no block here, in `order`.
- `writer.finding_payload(finding, *, poc: str, custom_fields: Sequence[dict], language: str) -> dict`: builds the body for §d.
- `writer.create_import(plan: ImportPlan, *, open_client: OpenClient, read_image: Callable[[str], bytes], journal: JournalWriter, progress: Progress, cancelled: Callable[[], bool], confirm_duplicate: bool) -> CreateOutcome`
  - `CreateOutcome` is a frozen dataclass in `writer.py`: `result: ImportResult | None`, `error: ImportFailure | None` and `rollback: UndoResult | None`.
  - It never raises for PwnDoc or cancel problems. On any failure after the first write it rolls back with `undo.undo_import` and returns the error and the rollback.
  - The sequence is in §d.
- `journal.JournalWriter(path: Path, import_id: str)`, with:
  - `.record(kind: str, object_id: str, audit_id: str = "") -> None`, an atomic 0600 write on every call, made before the next PwnDoc call;
  - `.set_state(state: str) -> None`;
  - `.current -> Journal`.
- `journal.read_journal(path: Path) -> Journal | None`: `None` when the file is absent, and `ImportInputError` when it is corrupt (strict `Journal.from_json`).
- `journal.write_journal(path: Path, journal: Journal) -> None`: `tempfile` plus `os.replace`, mode 0600.
- `undo.undo_import(journal: Journal, *, open_client: OpenClient, progress: Progress, check_changes: bool = True) -> UndoResult`
  - Follows the order and the shared-object rules in §0. Findings get the outcome `deleted` when their audit delete succeeded, and `failed` otherwise. No per-finding calls are made.
  - `check_changes` (every user undo) raises `ImportConflict("changed_since_import")` before any delete when a journaled audit holds findings that are not journaled; the writer's rollback passes False.
  - Status mapping: 404 is `gone`; 403 on an image is `kept`; an object something else may use is `kept`; 401, 5xx and connection errors are `failed`.
  - It touches only ids in `journal.objects`.

### B-ROUTES (service, jobs, storage, HTTP, wiring)

Files: `src/pwndoc_ai/server_import.py`, `docx_import/{service,jobs,store}.py`, and additions only in `src/pwndoc_ai/server.py` and `gateway.py`. Tests: `tests/test_import_routes.py`, `tests/test_import_security.py` and `tests/import_fakes.py`.

`store.py` is added to the list given to B-ROUTES.

- `store.ImportStore(root: Path)` with root `<data_dir>/docx_import`, holding:
  - `owner_dir(owner)`, using `docx_store.owner_hash(owner)`;
  - `create(owner, status) -> None`, which raises `ImportConflict("too_many")` or `ImportLimitError("quota")`;
  - `status(owner, id) -> ImportStatus`, which raises `ImportNotFound`;
  - `save_status`, `list(owner) -> list[ImportStatus]` (newest first), `read_plan` / `save_plan`, `save_images(owner, id, blobs)`, `read_image(owner, id, key) -> bytes`, `journal_path(owner, id) -> Path`, `delete(owner, id)`, `find_created_by_sha(owner, sha) -> str`, and `mark_interrupted()` (called at start-up).
  - Every read goes through the strict `from_json`. Files are 0600 and directories 0700. Write each file atomically under `uploads.lock_for(dir)`.
- `service.ImportService(store, docx_store, *, llm_factory, settings: Callable[[], Settings], runlog)`, with:
  - `start_analysis(identity, owner, req: AnalyseRequest, open_client) -> ImportStatus`
  - `get_status(owner, id) -> ImportStatus`
  - `list(owner) -> list[ImportStatus]`
  - `preview(owner, id) -> ImportPlan`
  - `edit(owner, id, edits: PlanEdits, open_client) -> ImportPlan`
  - `image(owner, id, key) -> tuple[bytes, str]`
  - `start_create(identity, owner, id, req: CreateRequest, open_client) -> ImportStatus`
  - `cancel(owner, id) -> ImportStatus`
  - `start_undo(identity, owner, id, open_client) -> ImportStatus`
  - `delete(owner, id) -> None`
  - `close() -> None`
- `jobs.ImportJobs`: one daemon thread per job, with the caps `MAX_RUNNING_PER_USER` (1, shared by analyse, create and undo) and `MAX_RUNNING_TOTAL` (2), a cancel `threading.Event`, and the status persisted before work starts and in a `finally`. It is shaped like `docx_jobs.DocxRefineJobs`.
- `Gateway.__init__` adds `self.docx_import = ImportService(...)` with `llm_factory=lambda settings: LlmClient(settings)`. Add `close_docx_import()` and call it from `server._lifespan` next to `close_docx()`.
- `server.py` includes the router and adds `"docx_import": docx_installed()` to `/status`.
- Error mapping: an `ImportFailure` becomes `HTTPException(exc.status, str(exc))`. `PwnDocError` goes through `pwndoc_http_error`. `EngineUnavailable` becomes 501.

### B-UI

Files:
- `src/pwndoc_ai/static/ai-import.js` (view), `ai-import-form.js` (preview form), `ai-import-run.js` (jobs and polling), `ai-import-state.js` (state slice and storage key), `ai-import-util.js` (pure helpers, no DOM).
- Additions to `ai-dock.js`, `ai-boot.js` (one import line and one `registerView` line; **added to B-UI's list**), `inject.css` (append at the end) and `inject.js` (none expected).
- `tests/e2e/fake_import.py`, `test_import_e2e.py`, `test_import_contract.py`, `import_check.mjs` and `test_import_util_node.py`.
- The mount additions in `tests/e2e/fake_gateway.py`.
- The README.md and docs/ARCHITECTURE.md sections.

The state slice lives in `ai-import-state.js`, not `ai-state.js`. Its sessionStorage key is `'pwndoc-ai:import'`, defined there and not in `ai-util.js`. Reuse the existing `upload` glyph, so `ai-dom.js` is not touched.

## (b) Data model (implemented)

`ImportedReport` is the reader's output (internal):

| Field | Type |
|---|---|
| `header` | `HeaderFields` |
| `findings` | `tuple[ImportedFinding]` |
| `images` | `tuple[ImportedImage]` |
| `blobs` | `tuple[ImageBlob(key, data)]` |
| `issues` | `tuple[CompletenessIssue]` |
| `stats` | `SourceStats` |
| `header_text` | `str` |

`HeaderFields`:

| Field | Notes |
|---|---|
| `company` | |
| `company_votes` | |
| `company_candidates` | |
| `scope` | |
| `date_start`, `date_end` | ISO or `""` |
| `test_type` | |
| `leftover_names` | |
| `contacts` | `tuple[Contact(email, firstname, lastname)]`; empty for this template |
| `severity_table` | `((sev, n), ...)` |

`ImportedFinding`:

| Field | Notes |
|---|---|
| `key` | `f\d{3,4}` |
| `source_id` | `\d{1,6}` or `""` |
| `title` | ≤300, one line |
| `severity` | `SEVERITIES` or `""` |
| `cvss_vector` | `CVSS:3.x/...` or `""` |
| `cvss_score` | `"8.8"` or `""` |
| `cwe` | |
| `urls` | tuple |
| `parameter` | |
| `location` | |
| `description`, `observation`, `remediation` | allow-list HTML |
| `poc` | `tuple[PocBlock(kind="html", html) \| PocBlock(kind="image", image_key)]` |
| `references` | tuple of plain strings |
| `include` | |
| `origin` | `detail`, `summary` or `merged` |
| `llm_fields` | subset of `title severity description observation` |
| `flags` | `summary_only truncated merged_duplicate copies_disagree description_split` |

`ImportedImage`:

| Field | Notes |
|---|---|
| `key` | `i\d{3,4}` |
| `sha256` | |
| `media_type` | `IMAGE_TYPES` or `""` |
| `size`, `width`, `height` | |
| `caption` | prefix stripped |
| `finding_key` | where it goes, editable; `""` means detached and not uploaded |
| `source_finding_key` | |
| `include` | |
| `order` | |
| `flags` | `placeholder unsupported too_large uncaptioned unmatched duplicate_caption` |
| `usable` | property: a type is known and there is no `unsupported` or `too_large` flag |

Rule: `include` can be true only when `usable`.

`CompletenessIssue(code, finding_key, image_key, fields)`: `level` and `message` are derived from `limits.ISSUES[code]`, a fixed sentence.

`ImportPlan` (the preview):

| Field | Type or notes |
|---|---|
| `import_id` | |
| `revision` | |
| `file_name` | |
| `file_sha256` | |
| `header` | `AuditHeader` |
| `source_header` | `HeaderFields` |
| `findings` | |
| `images` | |
| `issues` | |
| `stats` | |
| `options` | `PwnDocOptions` |
| `company_id` | |
| `duplicate_audit` | |
| `previous_import` | |

`AuditHeader` fields: `company`, `audit_name`, `language` (locale), `audit_type` (name), `template_id`, `date_start`, `date_end`, `scope`, and `contact: Contact | None`.

Defined in `records.py` and re-exported by `model.py`:

| Type | Fields or notes |
|---|---|
| `PwnDocOptions` | `languages: Choice(value=locale, label)`, `audit_types: AuditTypeOption(name, locales)`, `templates: Choice(value=id, label=name)` |
| `PlanDefaults` | `language`, `audit_type`, `template_id` |
| `NameCheck` | `company_id`, `duplicate_audit` |
| `AnalyseRequest` | `file_id`, `use_llm=True` |
| `CreateRequest` | `revision`, `accept_blanks=False`, `confirm_duplicate=False` |
| `CreatedObject` | `kind`, `id`, `at`, `audit_id` (set only for `image` and `finding`) |
| `Journal` | `version=1`, `import_id`, `state`, `updated`, `objects` |
| `ImportResult` | `audit_id`, `company_id`, `company_created`, `client_id`, `client_created`, `finding_ids`, `images_uploaded`, `images_reused`, `images_skipped` |
| `UndoItem` | `kind`, `id`, `outcome` |
| `UndoResult` | `items` |
| `ImportStatus` | see §c |

`PlanEdits` (`revision`, `header: FieldEdit | None`, `findings: tuple[FieldEdit]`, `images: tuple[FieldEdit]`) is in `model.py`. `FieldEdit.values` holds only the fields the request named; `.present` and `.get(name)` read them.

## (c) HTTP API

The base path is `/__ai/api/docx/import`. Every route:
- requires the header `X-Requested-With: pwndoc-ai`, on every method including GET;
- requires a signed-in PwnDoc user (401 `sign in to PwnDoc first` otherwise);
- is throttled per `identity.username`, answering 429 with `Retry-After`.

`{id}` must match `IMPORT_ID` and `{key}` must match `IMAGE_KEY`; anything else is a 404. Errors are `{"detail": "<fixed sentence>"}`. JSON bodies are capped at 1 MiB by the existing middleware (413).

| Method and path | Body | Answer | Errors | Throttle |
|---|---|---|---|---|
| `GET /imports` | | 200 `{"imports":[ImportStatus...], "limit": 20}`, own imports, newest first | | `import_read` |
| `POST /imports` | `{"file_id", "use_llm"?}` | 202 `ImportStatus` (`analysing`) | 400 invalid; 404 `file not found` (unknown or another user's docx); 409 `busy_user`, `busy_server`, `too_many`; 413 `quota`; 501 | `import_run` |
| `GET /imports/{id}` | | 200 `ImportStatus` | 404 | `import_status` |
| `GET /imports/{id}/preview` | | 200 `ImportPlan.to_json()` | 404; 409 `not_ready` (no preview) | `import_read` |
| `PUT /imports/{id}/preview` | `PlanEdits` | 200 the updated plan | 400 invalid, `unknown_key`, `image_not_usable`; 409 `stale`, `not_ready` (state is not `ready`), `running` | `import_edit` |
| `GET /imports/{id}/images/{key}` | | 200 raw bytes, `Content-Type` = `media_type`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'`, `Cache-Control: no-store` | 404 (unknown, or not `usable`) | `import_read` |
| `POST /imports/{id}/create` | `{"revision", "accept_blanks"?, "confirm_duplicate"?}` | 202 `ImportStatus` (`creating`) | 400 `blockers`, `blanks`; 409 `stale`, `not_ready`, `already_created`, `duplicate_audit`, `busy_*`; 401 or 403 from PwnDoc; 502 | `import_write` |
| `POST /imports/{id}/cancel` | `{}` | 200 `ImportStatus` | 409 `not_ready` when nothing is running or the run is an undo | `import_run` |
| `POST /imports/{id}/undo` | `{}` | 202 `ImportStatus` (`undoing`) | 409 `nothing_to_undo`, `running`, `busy_*` | `import_write` |
| `DELETE /imports/{id}` | | 204. Deletes only the local preview, images and journal; never touches PwnDoc | 409 `running` | `import_run` |

`ImportStatus` JSON keys, exactly:

| Key | Notes |
|---|---|
| `id` | |
| `file_id` | |
| `file_name` | |
| `state` | `IMPORT_STATES` |
| `phase` | `""`, `analyse`, `create` or `undo` |
| `total`, `done` | progress |
| `current` | a fixed step label |
| `error` | a fixed sentence or `""` |
| `started`, `ended` | ISO-Z timestamps or `""` |
| `revision` | |
| `has_preview` | |
| `result` | `ImportResult` JSON or `null` |
| `undo` | `UndoResult` JSON or `null` |

State transitions:

| From | To |
|---|---|
| `analysing` | `ready`, `analysis_failed` or `cancelled` |
| `ready` | (edits stay in `ready`) `creating` |
| `creating` | `created`, or `create_failed` (rolled back; `undo` holds the rollback items) |
| `created`, or `create_failed` with leftovers | `undoing` |
| `undoing` | `undone` or `undo_failed` |
| `undo_failed` | `undoing` (retry allowed) |
| any running state, after a restart | `interrupted` |

`interrupted` with a non-empty journal allows undo. Cancelling during `creating` rolls back.

Fixed step labels for `current`:
- Analyse: `Reading the document`, `Checking PwnDoc`, `Filling gaps with AI`, `Checking completeness`.
- Create: `Checking PwnDoc`, `Creating the company`, `Creating the contact`, `Creating the audit`, `Setting audit details`, `Uploading screenshots`, `Creating findings`, `Verifying`.
- Undo: `Deleting the audit`, `Deleting the contact`, `Deleting the company`, `Deleting screenshots`.

Throttles, added to `gateway._default_limits()`:

| Key | in_flight | per_minute |
|---|---|---|
| `import_read` | 2 | 120 |
| `import_status` | 4 | 300 |
| `import_run` | 1 | 30 |
| `import_edit` | 2 | 120 |
| `import_write` | 1 | 10 |

The run log uses kind `"docx_import"` with events `start` and `end`, and records the import id, phase, state and counts only.

## (d) PwnDocClient additions and the create sequence

Add these methods only; keep every existing method unchanged. Every id goes through `safe_id`. `_request` returns `datas`.

| Method | PwnDoc call | Returns |
|---|---|---|
| `list_companies() -> list[dict]` | GET `/api/companies` | `[{_id,name,shortName,logo}]` |
| `create_company(name: str) -> dict` | POST `/api/companies` `{"name"}` | 201 `{_id,name}`; 422 duplicate raises `PwnDocError(status=422)` |
| `delete_company(company_id: str) -> None` | DELETE `/api/companies/{id}` | |
| `list_clients() -> list[dict]` | GET `/api/clients` | |
| `create_client(payload: dict) -> dict` | POST `/api/clients` `{"email","firstname","lastname"}` | 201 `{_id,...}`. **Never send `company`**, which would upsert one invisibly. Link the client via `PUT /general`. |
| `delete_client(client_id: str) -> None` | DELETE `/api/clients/{id}` | |
| `create_audit(name: str, language: str, audit_type: str) -> str` | POST `/api/audits` `{"name","language","auditType"}` | `datas["audit"]["_id"]`; checked with `OBJECT_ID`, otherwise `PwnDocError` |
| `get_audit_general(audit_id: str) -> dict` | GET `/api/audits/{id}/general` | |
| `update_audit_general(audit_id: str, payload: dict) -> None` | PUT `/api/audits/{id}/general` | |
| `delete_audit(audit_id: str) -> None` | DELETE `/api/audits/{id}` | |
| `create_finding(audit_id: str, payload: dict) -> None` | POST `/api/audits/{id}/findings` | 200 message string, **no id** |
| `delete_finding(audit_id: str, finding_id: str) -> None` | DELETE `/api/audits/{aid}/findings/{fid}` | |
| `create_image(value: str, name: str, audit_id: str) -> str` | POST `/api/images/` `{"value","name","auditId"}`, JSON not multipart | 201 `datas["_id"]` |
| `get_image(image_id: str) -> dict` | GET `/api/images/{id}` | `{auditId,value,name}` |
| `delete_image(image_id: str) -> None` | DELETE `/api/images/{id}` | 403 for non-admin |

A non-JSON 500 (an Express 5 async rejection) is an opaque `PwnDocError(status=500)`; `_error_text` already tolerates it.

Writer sequence:

1. **Checks.** `check_names`, which fails with `duplicate_audit` unless confirmed. Re-validate locally that `plan.header.template_id` is in `options.templates` and that `language` is in `options.languages`.
2. **Company.** If `company_id` is set and still listed, reuse it and do not journal it. Otherwise `create_company(name)`: a 201 is journaled as `company`. A 422 means re-list, match by `name_key` and reuse without journaling.
3. **Contact** (only when `contact.email` is set). Reuse by a case-insensitive email match in `list_clients()` without journaling. Otherwise create it and journal `client`.
4. **Audit.** `create_audit(audit_name, language, audit_type)` and journal `audit` immediately.
5. **General.** `update_audit_general(id, {"company": {"_id": cid}, "template": tid, "scope": [..], "date_start"?, "date_end"?, "client"?})`. Omit blank dates; PwnDoc ignores falsy values anyway.
6. **Images**, sequentially, one per distinct sha256 among included, usable images whose `finding_key` points to an included finding.
   - `create_image(images.data_url(bytes), "<key>.<ext>", audit_id)`.
   - Then `get_image(id)`. If `auditId == audit_id` and the id is not already in the journal, journal `image` with `audit_id` and count it as uploaded. Otherwise count it as reused and never journal it.
   - Map every image key that shares that sha256 to the id.
7. **Findings**, sequentially, in `creation_order`. Never post them in parallel. The payload is:

   ```json
   {
     "title": "...",
     "description": "...",
     "observation": "...",
     "remediation": "...",
     "poc": "<poc_html>",
     "references": ["..."],
     "cvssv3": "<only if cvss.parse_vector(vector)>",
     "scope": "<p>url1</p><p>url2</p>",
     "status": 1,
     "customFields": [{"customField": {"...": "full field object without text"}, "text": "..."}]
   }
   ```

   Custom fields:
   - Take them from `list_custom_fields()` where `display in ("finding", "vulnerability")` and `displaySub == ""`.
   - Map them by the key `deburr(lower(label)).replace(/\s/g, "").replace(/[^\w]/g, "_")`:

     | Key | Value |
     |---|---|
     | `vulnerableurl` | `", ".join(urls)` |
     | `vulnerableparameter` | `parameter` |
     | `vulnerablelocation` | `location` |
     | `cwecve`, `cwe_cve` or `cwe` | `cwe` |
     | `cvssscore` | `cvss_score`, or the score computed from the vector when the score is blank |
     | `severity` | `severity`, or `cvss.severity_of(score)` when blank and a score exists |

   - Never send `priority` or `remediationComplexity`; out-of-range values cause a 500 after the finding is stored.
   - Fields not in the custom-field list are skipped.
8. **Verify.** `get_audit(audit_id)`. Take the finding ids in `identifier` order, journal each as `finding` with `audit_id`, and check the count. A wrong count is a failure, so roll back.
9. Set the journal state to `complete`. On any failure after step 2, call `undo.undo_import` and set the journal to `rolled_back` (everything was deleted, `kept` images excepted) or `partial`.

Map `PwnDocError` statuses: 401 is `ImportSessionExpired`, 403 is `ImportDenied`, and everything else is `ImportUpstreamError("pwndoc")`.

## (e) Undo journal and storage

```
<data_dir>/docx_import/<owner_hash(owner)>/<import_id>/
    status.json     ImportStatus
    plan.json       ImportPlan (absent until the analyse succeeds)
    journal.json    Journal (absent until the first PwnDoc write)
    images/<image_key>.bin   extracted bytes (0600)
```

- Directories are 0700 and files 0600. Every file is written atomically and read back with strict `from_json`; a corrupt file reads as `ImportNotFound`, except the journal (§a).
- Caps: `MAX_IMPORTS_PER_OWNER` (20) and `MAX_OWNER_IMPORT_BYTES` (512 MiB) per owner.

`journal.json` example:

```json
{"version": 1, "import_id": "<uuid4hex>", "state": "open|complete|rolled_back|partial|undone",
 "updated": "2026-10-23T10:00:00Z",
 "objects": [{"kind": "company", "id": "<24hex>", "at": "<ts>", "audit_id": ""},
             {"kind": "audit", "id": "<24hex>", "at": "<ts>", "audit_id": ""},
             {"kind": "image", "id": "<24hex>", "at": "<ts>", "audit_id": "<audit 24hex>"},
             {"kind": "finding", "id": "<24hex>", "at": "<ts>", "audit_id": "<audit 24hex>"}]}
```

- The journal holds ids only, never names.
- `DELETE /imports/{id}` removes the journal. The UI warns first: "The audit stays in PwnDoc and this import can no longer undo it."
- The importer never deletes the source DOCX in docx_store.

## (f) Preview JSON and the edit payload

`GET /preview` returns `ImportPlan.to_json()`: the keys listed in §b, with nested objects in their `to_json` shape. `source_header` and `stats` are read-only context.

The edit body (`PUT /preview`) is partial. Send only what changed: in practice the header, or one finding, or the images.

```json
{"revision": 3,
 "header": {"company": "Brandarmy", "audit_name": "Q1 web", "language": "en", "audit_type": "Alfred",
            "template_id": "<24hex>", "date_start": "2026-03-05", "date_end": "", "scope": ["https://a"],
            "contact": null},
 "findings": [{"key": "f001", "title": "...", "severity": "High", "cvss_vector": "", "cvss_score": "8.8",
               "cwe": "", "urls": ["..."], "parameter": "", "location": "", "description": "<p>..</p>",
               "observation": "", "remediation": "", "references": [], "include": true,
               "poc": [{"kind": "html", "html": "<p>..</p>"}, {"kind": "image", "image_key": "i001"}]}],
 "images": [{"key": "i001", "caption": "Login", "finding_key": "f001", "include": true}]}
```

- Every key inside `header`, a finding and an image entry is optional except `key`. Unknown keys are refused (400).
- When the server answers 409 `stale`, the UI reloads the preview.
- The create body is `{"revision": 3, "accept_blanks": true, "confirm_duplicate": false}`.

## (g) Limits (`docx_import/limits.py`)

| Limit | Value |
|---|---|
| Findings per import | 200 |
| Images per import | 200 |
| One image | 8 MiB |
| All images of an import | 64 MiB |
| Placeholder threshold | under 200 B, or 1x1 px |
| Company, audit name, audit type, contact name | 200 chars |
| Title | 300 |
| One-line fields (url, parameter, location, cwe) | 2000; longer is truncated and flagged `truncated` |
| One rich field or PoC html block | 50 000 |
| Whole PoC | 200 000 chars and 400 blocks |
| URLs per finding | 100 |
| References | 50 × 1000 |
| Scope | 100 × 500 |
| Caption | 300 |
| `header_text` | 8000 |
| Issues | 1000 |
| Options per list | 500 |
| Imports per owner | 20 |
| Storage per owner | 512 MiB |
| Running jobs | 1 per user, 2 in total |

The DOCX caps are those of `docx_store` and `docx_engine.limits`.

## (h) HTML allow-list and escaping

**Output tags.** Only `p`, `br`, `ul`, `ol`, `li`, `strong`, `em`, `code` and `pre`, with no attributes. `<img src="<24hex>" alt="<escaped caption>">` is emitted only by `html.image_tag` with an id PwnDoc returned, at top level, between closed blocks.

**Escaping and blocks**
- All text goes through `html.escape(text, quote=True)`. No raw XML, attribute or hyperlink target from the file ever reaches the output.
- Every `li` contains `<p>…</p>`, as TipTap emits it; PwnDoc's export drops bare `li` text.
- `<br>` appears only inside `<p>` or `<li><p>`.
- Never emit an empty `<code></code>` or `<p></p>`, and never emit `<mark>`.

**From the docx**
- Paragraphs become `<p>`. A `w:br` (textWrapping) becomes `<br>`, and `\n\n` starts a new paragraph.
- Word numbering (`numId` → `abstractNum` → `numFmt`) becomes `<ul>` for `bullet` and `<ol>` otherwise. Nesting follows `ilvl`.
- Typed markers (`1.`, `1)`, `-`, `•`, `*`) form a list when at least 2 contiguous items appear. Otherwise the paragraph stays a literal `<p>`.
- An all-bold short paragraph becomes `<p><strong>…</strong></p>`.
- A code block is recognised only with a language label line, and the heuristic is optional.
- A deduped figure removes its orphan "References:" or "-" text.

**Edits and LLM.** HTML from UI edits and from the LLM goes through `html.clean_html` and is never trusted. `sanitize.to_rich_text(value, allowed_images=…)` may be applied afterwards as a second check.

**Plain fields.** References are plain strings, not HTML. Title, location, parameter, URLs, CWE and score are single-line plain text; `\n` is collapsed and control characters are removed.

## (i) Masking boundary

| Call | Sees real names? | How |
|---|---|---|
| Reader, plan, completeness | n/a (local) | no LLM |
| Header fallback (company, dates, scope from `header_text`) | **Yes**: reading them is its job | `maskflow.call_header`, unmasked. Input is `header_text` only, never finding content. |
| Finding fallback (title, severity, description/observation split) | No (realistic stand-ins) | `maskflow.call_masked` with `finding_masker`. Client names, the scope domains/hosts, IPs and e-mails are swapped for fictional stand-ins; endpoints and payloads are sent as they are. With masking on and no client name known, no finding call is made (`llm_skipped_unmasked`). The answer is unmasked and rejected when a stand-in is left in it. |
| PwnDoc writes | Yes | the user's own cookie, no LLM |

**Rules that apply to every call**
- The masker is created per job and never persisted or logged.
- Prompts treat the document as untrusted data inside `<document>` tags. Items are JSON lines with `<` and `>` escaped, and the prompt tells the model to copy every name, host, address, URL and payload exactly as written (it is never told they are stand-ins).
- Answers are validated:
  - the answer is a dict, with only the keys that were sent;
  - `severity` is in `SEVERITIES` and follows from the printed CVSS score, or the finding text prints it as a labelled value (`Severity: High`, `Risk level - High`, `rated High`, `High risk`, `High severity`; a bare word such as "high privileges" is not one);
  - a title's tokens all appear in the finding text;
  - the description/observation split must satisfy `norm(plain(description_new) + plain(observation_new)) == norm(plain(description_old))`, an exact partition with nothing added;
  - a header company must appear in `header_text` (case-insensitive) and not be only in `TEMPLATE_LEFTOVER_NAMES`;
  - a date must equal a date that `header_text` writes out (`textdates.dates_in`: ISO, `DD.MM.YYYY`/`DD/MM/YYYY`/`DD-MM-YYYY` in both day-first and month-first readings, `5 March 2026`, `March 5, 2026`); numbers that merely appear somewhere never make a date.
- An LLM-filled field is listed in `finding.llm_fields`, which gives the warning `llm_fields`.
- Never log prompts, answers, names or document text. Logs carry exception types and counts only.

## (j) UI states and the selectors the e2e tests use

- **Dock button.** `<button>` with the visible text **`Import a DOCX`**, `data-fk="dock-import"` and the `upload` glyph. It is shown when `S.status.authenticated && S.status.docx_import`, on every page.
- **Drawer.** `registerView('import', …)` gives `role="dialog"` named by the heading **`Import a DOCX into PwnDoc`** (`#ai-title`).

The drawer moves through these steps (`S.imp.step`): `pick`, `analysing`, `preview`, `confirm`, `creating`, `created`, `undoing`, `undone` and `failed`.

| Element | Accessible name or role | data-fk |
|---|---|---|
| Drop zone and file input | button `Choose a .docx report` | `import-pick` |
| Use AI toggle | checkbox `Use AI to fill missing fields` | `import-use-llm` |
| Recent imports list | list `Recent imports`; each row has a button `Open <file name>` | `import-open-<id>` |
| Progress | `role="progressbar"`, label `Reading the report`, `Creating in PwnDoc` or `Undoing the import`; text `role="status"` | |
| Cancel | button `Cancel` | `import-cancel` |
| Audit section | `section` with aria-label `Audit` | |
| Header fields | labels `Company`, `Audit name`, `Language` (select), `Audit type` (select), `Template` (select), `Start date`, `End date` (input type=date), `Scope (one per line)` (textarea), `Contact email` | `import-company`, `import-audit-name`, `import-language`, `import-audit-type`, `import-template`, `import-date-start`, `import-date-end`, `import-scope`, `import-contact-email` |
| Completeness | section aria-label `Completeness`; list items show the level as a text chip (`Required`, `Check`, `Note`), not by colour alone | |
| Accept blanks | checkbox `Create with the blanks listed above` | `import-accept-blanks` |
| Duplicate warning | note text from the `duplicate_audit` issue; checkbox `Create another audit with the same name` | `import-confirm-duplicate` |
| Findings | section aria-label `Findings`; each finding is a `fieldset` with aria-label `Finding <n>` and a legend showing its title | |
| Finding fields | labels `Include this finding`, `Title`, `Severity` (select with an empty option `Not set`), `CVSS vector`, `CVSS score`, `CWE / CVE`, `Vulnerable URLs (one per line)`, `Vulnerable parameter`, `Vulnerable location`, `Description`, `Observation`, `Proof of concept`, `Remediation`, `References (one per line)` | `f-<key>-<field>`, e.g. `f-f001-title` |
| Screenshots | section aria-label `Screenshots`; each is a `fieldset` with aria-label `Screenshot <n>`; an `img` with alt = caption, loaded via the images route as a blob URL and omitted for unusable ones; labels `Caption`, `Attach to finding` (select; an option `Not attached`), `Include this screenshot` | `img-<key>-caption`, `img-<key>-finding`, `img-<key>-include` |
| Save | button `Save changes` (disabled when nothing changed); errors use `role="alert"` | `import-save` |
| Create | button `Create in PwnDoc` (disabled while there are unsaved changes or blockers) | `import-create` |
| Confirm | text `Create audit “<name>” with <N> findings in PwnDoc?`; buttons `Go back` (focused first) and `Yes, create` | `import-create-no`, `import-create-yes` |
| Created | note `Created in PwnDoc.`; link `Open the audit` (`href="/audits/<id>/general"`); button `Undo this import` | `import-undo` |
| Undo confirm | text `Delete the audit and everything this import created?`; buttons `Keep it` (focused first) and `Yes, undo` | `import-undo-no`, `import-undo-yes` |
| Undone | note `The import was undone.`; when there are `kept` images, `<N> screenshots stay in PwnDoc: another audit uses them, or only an administrator can delete images.`; when a company or contact is `kept`, `The company or contact this import created stays in PwnDoc: ...` (`keptOtherText`) | |
| Undo failed | the error sentence, `<N> object(s) could not be deleted.` when some failed, and the same kept notes as Undone | `import-undo` (`Try the undo again`) |
| Unsaved changes on close | a note at the top: `You have unsaved changes. Close and discard them?` with `Keep editing` (focused) and `Discard and close` | `import-keep-editing`, `import-discard-close` |
| Discard | button `Delete this import` with an inline confirm (`Keep`, `Delete`) | `import-discard` |

**Editable-text convention** (`ai-import-util.js`, pure, tested in node):
- `htmlToEditable(html)` and `editableToHtml(text)`: a blank line separates paragraphs, a single newline becomes `<br>`, lines starting with `- ` form a bullet list and lines starting with `1. ` form a numbered list. All text is escaped. The output uses only the allow-list.
- `pocToEditable(blocks)` and `editableToPoc(text, imageKeys)`: a line `[Screenshot i001]` is an image block. Unknown markers stay as text.
- The server cleans everything again.
- Server data is rendered as text nodes only, never `innerHTML`. The view keeps its own DOM between polls, so typing is never interrupted.

## (k) Test plan per owner

Test file basenames must be unique across the whole `tests/` tree, because `tests/docx_import` has no `__init__.py`. Use only `tmp_path`, no network, no real LLM and no real PwnDoc. Tests that read `/tmp/layout/*.docx` or `/tmp/refine_test/*.docx` must `skipif` the file is absent, and must never copy those files into the repo.

**Designer** (done): `tests/docx_import/test_model.py` covers round trips, unknown or missing keys, types, control characters, patterns, caps, cross references, issue levels and messages, and that errors do not echo values.

**B-READER**
- `labels`: normalisation, the fold that makes "XMLRPC" equal "XML-RPC", blank detection, label regexes.
- `header`: the company vote is Brandarmy (4 votes) with the leftover "Alfred AI" flagged; for demo it is Alfred AI; `undefined` dates are blank; demo dates are 2026-03-05 and 2026-03-06; scope is 2 items and 1 item.
- `findings` and `dedupe`: Brandarmy gives 10 unique findings with gaps 004, 008 and 009; `clean.docx` still pairs 006; a summary-only stub; the richest copy wins; the URL union keeps order.
- `html`: Word numbering becomes ul or ol with nesting; typed markers need at least 2 items; `w:br` becomes `<br>`; escaping of `<uuid>`, `&` and `"`; `clean_html` against hostile input (script, attributes, on* handlers, nested img, entities); every `li` has a `<p>`.
- `images` and `captions`: sniffing JPEG and GIF named `.png`; placeholder detection; the 3 duplicate figures in 005 are removed; an unmatched image goes after the nearest finding; captions lose their prefix.
- `reader`: zip and XML hostility (reuse the `docx_engine` fixtures), the tracked-changes file equals the clean one, and the caps (monkeypatch `limits`).

**B-PLAN**
- `plan`: the default audit name; `build_plan` from a hand-built `ImportedReport`; `apply_edits` for stale revisions, unknown keys, an image moved between findings, the HTML cleaned, revision+1 and `check_references` holding.
- `completeness`: each code, its level and `fields`; sticky codes are kept.
- `fallback`, using `FakeLlm`:
  - the header call contains the real company and finding calls never contain it (FakeLlm.prompts);
  - the split partition is enforced and an invented fact is rejected;
  - `LlmError` gives `llm_failed` and `LlmAuthError` stops;
  - `TOOL_NAME` is used.
- `maskflow`: an answer with a stand-in left in it is rejected; no client value reaches a finding call, even next to an escaped newline or tab (`tests/docx_import/test_import_masking_e2e.py`); the `mask_sensitive` opt-out is honoured.

**B-WRITER**
- `test_pwndoc_client_import.py`, using `httpx.MockTransport` with PwnDoc's envelope and statuses: 201 and 200 bodies, a 422 duplicate, a 404, a plain-text 500, and `safe_id` refusing a bad id.
- `writer`:
  - the call order;
  - `company:{_id}` and a `scope` list in `PUT /general`;
  - a sequential finding order by severity;
  - custom fields mapped and full objects sent;
  - `cvssv3` only when it parses;
  - the image reuse rule (another audit's `auditId` is not journaled);
  - `poc_html` has its `img` at top level with an escaped alt;
  - a failure at each step rolls back exactly the journaled ids;
  - a 401 midway gives `partial` or `rolled_back`;
  - cancel.
- `lookups`: defaults from the newest audit, case-insensitive company and duplicate matching.
- `journal`: atomic 0600 writes, strict reads, records survive a crash after each call.
- `undo`: the order, 403 gives `kept`, 404 gives `gone`, nothing outside the journal is touched.

**B-ROUTES**
- `test_import_routes.py`: the full flow upload → analyse → preview → edit → create → status → undo against `tests/import_fakes.py`, a stateful fake PwnDoc set via `rig.gw.transport = httpx.MockTransport(fake)`. Also: 409 cases, `previous_import`, restart giving `interrupted`, and the images route headers.
- `test_import_security.py`, copying `test_docx_security.py`:
  - every route returns 401 for an anonymous user and 403 without CSRF;
  - another user's import is a 404, admin included;
  - a malformed id is a 404;
  - throttles are per user, with 429 and `Retry-After`;
  - no document text, client names or paths appear in logs or error bodies;
  - an edit body over 1 MiB gives 413.

**B-UI**
- `test_import_util_node.py` with `import_check.mjs`: the editable round trips, escaping and the PoC markers.
- `test_import_e2e.py` against `fake_import.py`: the dock button, pick, analysing, the preview fields by the labels above, the completeness gate (Create disabled until blanks are accepted), the confirm with the safe answer focused, creation with a link, undo with the kept-images note, keyboard-only flow and focus return, and no use of sleeps.
- `test_import_contract.py`: the key sets of the fake equal the real JSON key sets for every route.

## Layout profiles (ParadigmIT VAPT layout)

`profile.py` holds the layout profiles; `read_report` detects one per document (two markers
among "PREPARED FOR", "ASSESSMENT WINDOW", "Detailed technical findings", the header
"PARADIGMIT CYBERSECURITY / VAPT", "Representative example") and activates it through a
`ContextVar` for the read. The `default` profile changes nothing. The `paradigm` profile:
reads the cover/scope cards (`profile_paradigm.py`: company = PREPARED FOR, window -> dates,
APPLICATION URL -> scope, TEST TYPE; the severity chips and "IDENTIFIED FINDINGS" feed the
existing `severity_table_mismatch` check), appends "Steps to Reproduce" to the PoC after the
evidence ("Steps to reproduce:" + ordered list), accepts a short plain line after a picture as
its caption, reads the register's leading URL and "Param:" lines, lets the detail copy's
location/parameter/URLs win the dedupe, joins wrapped URL lines, reads a blank severity from the
chip colour, and closes the finding region at the Disclaimer heading.

## Finding order, derived severity and verification (docs/FINDING_ORDER.md)

- `docx_import/ordering.py` implements FINDING_ORDER.md Part A on the plan: the CVSS v3.1 score
  comes from a valid `CVSS:3.1`/`3.0` vector (`pwndoc_ai.cvss`), else from the printed score; a
  `CVSS:4` vector is never scored. Its band (Critical/High/Medium/Low/Informational) is the
  severity. A finding without a score keeps its label and goes after the scored findings of that
  band (with the existing `cvss_missing` warning); ties keep the plan order. `order_rows` is
  checked against `tests/data/finding_order_cases.json`.
- `build_plan` and `apply_edits` store the findings in this order. Each edit recomputes it on the
  server from the stored plan. The client never sends an order.
- The writer creates the included findings in plan order, so PwnDoc's own last+1 numbering is the
  new S. No. (001 = most severe). The `severity` custom field gets the derived band, and
  `cvssscore` gets the v3.1 score (the vector wins over a different printed number). The stored
  label stays in the plan; when it disagrees with the band, the `severity_label_mismatch` warning
  is raised and the verification note says `old -> new`.
- `docx_import/verify.py` builds the verification report. Each finding gets `seq`, `score`,
  `severity`, `stored_severity`, `note` and checks `cvss_present`, `cvss_vector_valid`,
  `score_matches_vector` and `severity_matches_band` (`pass`/`fail`/`fixed`). List checks:
  `order_monotonic`, `numbering_contiguous`, `duplicate_titles`. `GET`/`PUT .../preview` add it as
  `verification`. `GET .../imports/{id}/verification` returns the report for the created audit
  (PwnDoc's stored order, `source: "audit"`) or, before create or after undo, for the plan
  (`source: "plan"`).
- The preview UI shows the new S. No. in each finding's legend, plus a chip with the derived
  severity and score and the `old -> new` note.
- Part B (`docx_import/normalize.py`, wired by the integrator): after the existing LLM fallback
  (AI first), each finding goes through the shared deterministic `finding_normalize`. Unscored
  findings get an estimated v3.1 vector (flag `cvss_estimated`) and are ordered by it, not put
  last in their band. The band comes from the score, CWE is `CWE-n` or `N/A`, Informational is
  0.0, and empty parameter/location/rich text become `N/A`. The plan keeps the extracted values
  (completeness still reports the gaps) but is ordered by the normalised key. `creation_order`
  writes the normalised findings in that order, and `verify_plan` reports on them. See
  FINDING_ORDER.md section C. The importer's LLM fallback is unchanged (still the four
  `LLM_FIELDS`); asking it for the other empty fields is not wired.
