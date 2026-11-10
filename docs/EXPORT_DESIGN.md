# Export tab, fonts, typography and PDF import: design contract

Status: contract for the parallel builders (B-FONTS, B-TYPO, B-SECTIONS, B-PDF, B-ROUTES, B-UI).
The designer files listed in (a) already exist, are tested, and are the single source of truth
for data shapes. A builder may extend a designer file only additively (new names; never change
the meaning, JSON keys or validation of existing names). When this text and a designer module
disagree, the module wins and the builder tells the integrator.

## 0. Settled decisions

1. One URL prefix for everything new: `/__ai/api/docx/export`. It inherits the existing
   `no-store` headers (`limits.NO_STORE_PREFIX = "/__ai/api/docx"`), so `limits.py` is not
   edited. The router uses `dependencies=[Depends(require_csrf_header)]`, like server_docx.py, so
   GET requests also need `X-Requested-With: pwndoc-ai`. In the e2e fake the export dispatch
   goes BEFORE the `DOCX_PREFIX` dispatch.
2. The font catalog route is `GET /__ai/api/docx/export/fonts`. Any signed-in user can call it.
   Settings uses the same route. It never contains host paths or install commands. Those appear
   only in the admin-only `/health`.
3. Fonts are installed in layers.
   - `scripts/install_fonts.sh`, run as root, installs the apt packages and the pinned, hashed
     downloads into a project-local fonts directory.
   - The directory is `<data_dir>/fonts`, overridable with `PWNDOC_AI_FONTS_DIR`.
   - The renderer's generated fontconfig adds `<dir>` for that directory, so both LibreOffice
     and fc-match see the fonts, with or without root.
   - Proprietary names (Calibri, Aptos, Segoe UI and so on) are never installed. They resolve
     through alias chains, and the UI shows "substituted by X".
4. The typography step for PwnDoc audit downloads comes BEFORE tidy and BEFORE the PDF renderer:
   `export_docx -> is_docx -> typography (admin defaults) -> tidy / render_pdf`. See (e).
5. Sections are detected by the AI for every block, with masking. A deterministic heuristic
   serves as the fallback and repairs coverage. The AI returns only ids and closed-enum labels
   and never returns or changes text.
6. There are two PDF engines.
   - `structure` uses only the installed tools (mutool, pdftotext, pypdf) and is always
     preferred.
   - `pdf2docx` is the optional extra `pdfimport`. Its dependency PyMuPDF is AGPL-3.0, so it
     runs in a separate worker process only.
   - `auto` runs the structure engine first and falls back to pdf2docx on the measured criteria
     in (g).
   - A scanned PDF is refused with a clear message, because OCR is not installed.
7. Errors are fixed sentences from `export_errors.py` (codes to messages). Logs carry the
   exception type only, never text, paths or names.
8. Uploads, both DOCX and PDF, live in a dedicated store `<data_dir>/export`. It is separate from
   DocxStore, and the source PDF is kept so it can be converted again with another engine.
   `docx_store.py` is not edited.
9. The block-type set is closed, shared and defined once in `block_types.py`.

## (a) Module map: one owner per file

P = `src/pwndoc_ai`. Designer files (exist, tested):

| File | Public API |
|---|---|
| P/export_errors.py | `ExportFailure(code=None)` (`.code`, `.status`, `str()` is the fixed sentence); subclasses `ExportInputError(code, field=)` 400, `ExportNotFound` 404, `ExportConflict` 409, `ExportCancelled` 409, `ExportLimitError` 413, `ExportUnsupported` 415, `ExportRefused` 422, `ExportUnavailable` 501, `ExportUpstreamError` 502 |
| P/export_json.py | strict JSON helpers: `fail`, `record`, `mapping`, `text`, `required`, `flag`, … (raise `ExportInputError` with a schema path) |
| P/block_types.py | `BLOCK_TYPES`, `HEADING_TYPES`, `DEFAULT_FAMILY_TYPES`, `DEFAULT_SIZE_TYPES`, `BLOCK_TYPE_LABELS`, `heading_type(level)`, `heading_level(type)` |
| P/font_catalog_model.py | `valid_family`, `clean_family`, `parse_size` (strict), `clamp_size` (lenient, Settings), `half_points`, `squash`, `strip_pdf_font_name`, `valid_css_stack`, `custom_css`, `FontEntry`, `FontStatus`, `FontCatalog`, constants `SIZE_MIN=6`, `SIZE_MAX=72`, `SIZE_STEP=0.5`, `SIZE_PRESETS`, `CATEGORIES`, `ROLES`, `STATUSES` |
| P/docx_typography/{__init__,errors,model}.py | `FontSpec`, `SectionStyle`, `TypographyPlan` (`resolve`, `digest`, `check_sections`, `families`, `is_empty`), `plan_from_defaults(...)`, `BlockAssignment`, `check_assignments`, `FontUse`, `TypographyResult` (`headers()`), `TypographyError` |
| P/docx_sections/{__init__,errors,model}.py | `Block`, `BlockLabel`, `Section`, `DocumentSections` (`build`, `assignments`, `section_ids`), `build_sections`, `label_tool(name)`, `parse_label_answer(value, sent_ids)`, `merge_labels(blocks, primary, fallback)`, `SectionsError`, `LabelAnswerRejected`, limits |
| P/pdf_import/{__init__,errors,model}.py | `ENGINES`, `EngineInfo`, `PdfFacts`, `ConversionMetrics`, `ConversionReport`, `engine_choice`, `verdict_for`, `structure_weakness`, `keep_pdf2docx`, thresholds; `PdfRefused`, `PdfEngineUnavailable`, `PdfConversionFailed`, `SCANNED_MESSAGE` |

Builder files (exact signatures; owners as given):

**B-FONTS**
- `P/font_catalog.py`
  - `CATALOG: tuple[FontEntry, ...]` (see (f)).
  - `ALIAS_CHAINS: dict[str, tuple[str, ...]]`: proprietary name to its ordered candidates.
  - `METRIC_TWINS: dict[str, frozenset[str]]`: casefolded name to its metric twins.
  - `fonts_dir(data_dir: Path, env: Mapping[str, str] = os.environ) -> Path`.
  - `catalog_status(installed: frozenset[str], resolve: Callable[[str], str | None], *, checked: bool = True) -> FontCatalog`. Pure.
  - `font_catalog(data_dir: Path, *, ttl: float = 300.0) -> FontCatalog`. Runs fontconfig with
    the render config, has a deadline of 15 s, and is cached for the TTL keyed on the installed
    set.
  - `status_for(family: str, catalog: FontCatalog) -> FontStatus`. This covers custom "Other…"
    names: `custom=True`, with the CSS from `custom_css`.
  - `clear_caches() -> None`.
- `P/pdf_fonts.py`, additive changes:
  - `build_fontconfig(installed, fonts_dir: Path | None = None)` emits `<dir>` after the
    include.
  - `installed_families(conf: Path | None = None)` keeps the old zero-argument behaviour.
  - `write_fontconfig(work, installed=None, fonts_dir=None)`.
  - `FALLBACK_CHAINS` is extended with font_catalog.ALIAS_CHAINS, imported lazily or duplicated
    in data only.
  - Every existing test in tests/test_pdf_fonts.py passes unchanged. The chains for Segoe UI,
    Verdana and Tahoma may add candidates only BEFORE "Liberation Sans" when those candidates
    are NOT in the `NO_CROSEXTRA` fixture set. DejaVu Sans is never a candidate there.
- `P/font_probe.py`: `resolve_family(family, conf: Path | None = None)` (additive argument).
- `P/font_health.py` (new, so health.py does not grow): `check_font_catalog(installed,
  resolver) -> Check` with id `FONTS_CATALOG`. In health.py the only change is ONE added line in
  the `run_checks` builders tuple. The existing `PDF_FONTS` text stays as it is.
- `scripts/install_fonts.sh` and docs/FONTS.md.

**B-TYPO** (`P/docx_typography/`)
- `apply.py`: `apply_plan(data: bytes, plan: TypographyPlan, assignments: Mapping[str,
  BlockAssignment] | None = None, *, seconds: float | None = None) -> TypographyResult`.
  - Raises `TypographyError`.
  - An empty plan returns the input bytes unchanged.
  - With `assignments=None` (audit downloads), the block types are inferred by
    `infer_types(...)` from the style or outline level: heading n, Title, Caption, TOC n, List*,
    the Code style or a mono run font, in-table, or body. The section is `None`.
- `blocks.py`: `body_blocks(root) -> list[tuple[str, Element]]`. This is the SAME block
  enumeration as docx_sections (see (h)). B-TYPO owns it, and B-SECTIONS imports it from
  `docx_typography.blocks`. Both packages must agree on ids.
- `budget.py`, `limits.py`, and the internal helpers `styles_edit.py`, `theme_edit.py`,
  `runs_edit.py` and `verify.py`.
- `fonts_used(data: bytes) -> tuple[FontUse, ...]`, which feeds the "fonts in document" UI.

**B-SECTIONS** (`P/docx_sections/`)
- `reader.py`: `read_blocks(data: bytes) -> tuple[Block, ...]`. It uses
  `docx_typography.blocks.body_blocks`. It does not raise on a document that has only tables;
  it raises `SectionsError("no_text")` only when there are zero blocks with text.
- `heuristic.py`: `heuristic_labels(blocks) -> tuple[BlockLabel, ...]`. Pure and total (it
  labels every block).
- `prompts.py`: `SYSTEM_PROMPT`, `batch_user(lines) -> str`.
- `classify.py`: `detect_sections(data: bytes, *, llm: Any | None, masker: Any, model: str = "",
  cancelled: Callable[[], bool] = lambda: False, progress: Callable[[int, int], None] = ...) ->
  DocumentSections`.
- `cache.py`: `SectionsCache(root: Path)` with `get(doc_sha256, key) -> DocumentSections |
  None` and `put(...)`. The key is `sha256(docx) + ai_on + model + SECTIONS_VERSION`.

**B-PDF** (`P/pdf_import/`)
- `precheck.py`: `precheck_pdf(path: Path, *, deadline: float) -> PdfFacts`. Raises
  `PdfRefused`/`ExportLimitError`. It runs inside the worker AND as a quick in-process check at
  upload. The in-process run is bounded decode only, with a 15 s deadline.
- `engines.py`: `engines_available() -> tuple[EngineInfo, ...]`. It uses `shutil.which` and
  `find_spec` and never imports pdf2docx.
- `convert.py`: `convert_pdf(src: Path, work: Path, engine: str = "auto", *, timeout: float =
  ...) -> tuple[bytes, ConversionReport]`. It runs worker subprocesses and applies the auto
  rules.
- `worker.py`: executed as `python -I <path-to-worker.py> --engine E in.pdf out.docx
  report.json`.
- `structure.py`, `pdf2docx_engine.py`, `textlayer.py`, `metrics.py`, `limits.py`.
- pyproject.toml: the extra `pdfimport` only.

**B-ROUTES**
- `P/server_export.py`: `router`, `ExportBodyLimit(DocxBodyLimit)`.
- `P/export_store.py`: `ExportStore(root)`, `ExportRecord`.
- `P/export_jobs.py`: the analysis runner, at most 2 running in total and 1 per user.
- `P/export_service.py`: `ExportService(store, *, llm_factory, settings, data_dir)`.
- `P/typography_service.py`:
  - `settings_plan(settings) -> TypographyPlan`.
  - `apply_defaults(data: bytes, settings) -> tuple[bytes, dict[str, str]]`. It never raises.
    On failure it returns the original bytes and `{"X-Typography": "0"}`.
- `P/config_fonts.py`: the normalisers.
- Additions to config.py, gateway.py, server.py and server_docx.py, as given in (c), (d) and
  (e).

**B-UI**: the files listed in (j).

## (b) Data model (JSON)

All `from_json` implementations reject unknown keys, wrong types, out-of-range values and
non-closed enums. `to_json` followed by `from_json` is the identity.

**FontSpec**: `{"family": str|null, "size": number|null}`.
- `family` goes through `clean_family`: at most 100 characters, no `:,\="';<>{}()[]`, no leading
  `-`, printable.
- `size` is between 6 and 72 in steps of 0.5 (`parse_size`).
- null means inherit.

**TypographyPlan**:
```json
{"version": 1,
 "default": {"family": "Lexend", "size": 11},
 "by_block_type": {"heading_1": {"family": null, "size": 20}, "code": {"family": "Consolas", "size": 9}},
 "by_section": {"s3": {"all": {"family": "Georgia", "size": null},
                       "by_block_type": {"body": {"family": null, "size": 10.5}}}}}
```
Keys of `by_block_type` come from `BLOCK_TYPES`. Section ids match `s[1-9]\d{0,4}` and must
exist in the analysed document (`check_sections`). The digest is 16 hex characters.

**BlockType** (closed): title, heading_1 to heading_4, body, list, table, code, caption, quote,
toc, header, footer, footnote, figure, other.

**Block**: `{id "b00000", kind paragraph|table, text (excerpt at most 300 characters), style,
level, length, bold, size, list, mono, font}`.

**BlockLabel**: `{id, type, source ai|heuristic|style}`.

**Section**: `{id, title, level, heading_block, block_ids[], type_counts{type:n}}`.

**DocumentSections**: `{version 1, doc_sha256, method ai|heuristic|mixed, model, warnings[],
blocks[], labels[], sections[]}`. Blocks before the first heading go into section `s1`
("Start of document").

**FontStatus** (catalog entry as sent): `{family, category sans|serif|mono|display, roles[],
css, proprietary, licence, status installed|substituted|missing, resolved, layout_safe,
custom}`.

**FontCatalog**: `{fonts[], sizes[presets], min_size 6, max_size 72, step 0.5, checked}`.

**ConversionReport**: `{engine_requested, engine_used, reason, verdict text|mixed|scanned,
scanned_share, facts{pages,size,tagged}, metrics{engine, pages, lines, headings[4], title,
tables, images, lists, dropped_running_lines, text_loss, residual_tabular, max_columns, seconds},
fonts_seen[], warnings[]}`.

**EngineInfo**: `{name, available, licence, note ""|not_installed|tools_missing}`.

**TypographyResult.headers()**: `X-Typography: 0|1`, `X-Typography-Runs`,
`X-Typography-Styles` (digits only).

**ExportRecord** (B-ROUTES, `meta.json`, strict): `{id uuid4hex, name, kind docx|pdf, size,
created, state uploaded|converting|analysing|ready|error|cancelled, engine, error_code, progress
{done,total}, plan_rev int}`.
- Files in the record folder: `source.docx|source.pdf`, `converted.docx` (PDF only),
  `report.json`, `sections.json`, `plan.json`.
- Folder layout: `<data_dir>/export/<owner_hash>/<id>/`, directories 0700, files 0600.

## (c) HTTP API (prefix `/__ai/api/docx/export`, CSRF header on every method)

| Method and path | Who | Throttle | Answer |
|---|---|---|---|
| GET `/fonts` | user | `export_read` | `FontCatalog.to_json()` |
| GET `/engines` | user | `export_read` | `{"engines":[EngineInfo…], "ocr": false}` |
| GET `/files` | user | `export_read` | `{"files":[item…], "quota":{used,max_bytes,files,max_files}}` |
| POST `/files` (multipart `file`, query `engine=auto`) | user | `export_upload` | 201 `item`. 413 for size or quota, 415 for type, 422 for refused PDF or DOCX |
| POST `/files/{id}/analyse` body `{"engine":"auto","use_ai":true,"client_terms":[str…≤50]}` | user | `export_run` | 202 `{"started":true}`. 409 when running or busy |
| GET `/files/{id}/status` | user | `export_status` | `{state, done, total, step converting\|analysing\|"", error}` |
| POST `/files/{id}/cancel` | user | `export_run` | 200 `{"cancelled":true}` |
| GET `/files/{id}` | user | `export_read` | `{file:item, report:ConversionReport\|null, sections:DocumentSections, plan:TypographyPlan, plan_rev, fonts_used:[FontUse]}`. 409 `not_ready` |
| PUT `/files/{id}/plan` body `{"plan":TypographyPlan, "rev":int}` | user | `export_run` | 200 `{plan, plan_rev}`. 400 for an invalid plan, 409 `stale` |
| GET `/files/{id}/download?format=docx\|pdf&tidy=1` | user | `export_download` | file. Headers: Content-Disposition, `X-Typography*`, `layout_headers`, `X-Font-Substitutes` (count) |
| GET `/files/{id}/check` | user | `export_download` | `check_payload` shape on the typographed DOCX (fonts list with substitutions) |
| DELETE `/files/{id}` | user | `export_upload` | 200 `{"deleted":true}` |

- Ids must match `docx_store.FILE_ID`. Anything else gets the same 404 as "not found".
- The owner is `gw.docx_owner(identity)`. Another user's id gets a 404.
- Throttles go into `gateway._default_limits`:
  - `export_upload` (1, 20/min, total 2)
  - `export_read` (2, 120)
  - `export_status` (4, 300)
  - `export_run` (1, 30)
  - `export_download` (2, 30, total 4)
- Upload body limit: `ExportBodyLimit` allows `MAX_EXPORT_DOCX_BYTES` (50 MiB) or
  `MAX_EXPORT_PDF_BYTES` (100 MiB), plus `MULTIPART_OVERHEAD`, on `POST
  /__ai/api/docx/export/files` only. server.py swaps its single `add_middleware(DocxBodyLimit)`
  line for `ExportBodyLimit`.
- Quotas per user: 10 files and 1 GiB.
- The sink accepts `PK\x03\x04` followed by `precheck_docx`, or `%PDF` followed by
  `precheck_pdf`. It refuses a mismatch between the suffix and the magic bytes.
- Downloads: the PDF cache key is `gw.docx_pdf_cache.key(typographed_docx, "export|tidy")`.
  The bytes already include the plan, so the key changes when the plan changes.
- `/status` gains `"export_tab": docx_installed()` and `"pdf_import": <structure tools present>`.
  `"pdf2docx"` is a boolean.
- Admin: `GET/PUT /__ai/api/settings` (existing) carries the new keys from (d). `/health` lists
  `FONTS_CATALOG`.

## (d) Settings keys (config.py plus P/config_fonts.py)

| Key | Type and default | Normaliser | Env |
|---|---|---|---|
| `default_font` | str "" | `config_fonts.font_name`: strip; "" means off; else `clean_family`. It raises `SettingsError("default_font is not a valid font name")` | `PWNDOC_AI_DEFAULT_FONT` |
| `default_font_size` | float 0.0 | `config_fonts.font_size`: `clamp_size` (0 is off; else clamped to 6-72 and rounded to 0.5). Bool, NaN and text raise `SettingsError("default_font_size must be a number")` | `PWNDOC_AI_DEFAULT_FONT_SIZE` |
| `heading_font` / `heading_font_size` | same | same | `PWNDOC_AI_HEADING_FONT(_SIZE)` |
| `mono_font` / `mono_font_size` | same | same | `PWNDOC_AI_MONO_FONT(_SIZE)` |

- All env variables are in `LENIENT_ENV_FIELDS`.
- Error messages contain the key name, for `flagInvalid`.
- On load the normalisers are lenient: an unknown but syntactically valid family is kept.
- The plan is `plan_from_defaults(...)`: heading applies to title and heading_1 to heading_4,
  and mono applies to code.

## (e) PwnDoc audit downloads

- The pipeline in `server.audit_report` is:
  1. `data = client.export_docx`
  2. `is_docx`
  3. `data, typo_headers = typography_service.apply_defaults(data, gw.store.settings)` (in the
     threadpool)
  4. the existing `layout_limited` block (Word: `tidy_for_download`; PDF: `render_pdf`, whose
     own tidy runs inside)
- Why typography comes first:
  - tidy measures fonts and widths, so it must see the final fonts.
  - The renderer must receive the final bytes.
  - The font warnings then describe the actual output.
- `typo_headers` is merged into the response headers.
- Refine downloads (`server_docx.download_file`) apply the defaults only when `kind == "clean"`.
  They never apply them to `original` (user rule) or `tracked` (revision markup).
- Cache: the audit route has no cache. The refine PDF cache key is computed from the
  typographed bytes, so a settings change produces a new key automatically. Do not compute the
  key before typography runs.
- The layout check routes (`server_features` audit check, `server_docx` check) run the same
  `apply_defaults` before `check_payload`, so their font warnings match the download.
- An empty plan does nothing: the bytes are identical and `X-Typography: 0`.

## (f) Fonts

**Catalog** (B-FONTS fills `CATALOG`; families, roles and CSS stacks are as in the recon table):

- Microsoft names, each with its stand-in:
  - Calibri (Carlito, metric)
  - Aptos (Carlito, close)
  - Cambria (Caladea, metric)
  - Arial (Liberation Sans, metric)
  - Times New Roman (Liberation Serif, metric)
  - Courier New (Liberation Mono, metric)
  - Segoe UI (Selawik, then Open Sans, then Noto Sans)
  - Verdana (DejaVu Sans)
  - Tahoma (DejaVu Sans)
  - Georgia (Gelasio, metric once downloaded)
  - Consolas (Inconsolata)
  - Palatino Linotype (P052)
  - Century Gothic (URW Gothic)
  - Garamond (EB Garamond)
- Free families: Carlito, Caladea, Liberation Sans/Serif/Mono, Lexend, Lexend Deca, Open Sans,
  Roboto, Lato, Inter, Noto Sans, Noto Serif, Source Sans 3, Ubuntu, IBM Plex Sans, DejaVu
  Sans/Serif/Sans Mono, EB Garamond, Fira Code, JetBrains Mono, Inconsolata.
- Excluded: CJK, symbol fonts, Comic Sans, TeX/Latin Modern.

**Status rule**:
- `installed`: the family is in the installed set.
- `substituted`: the render-config fc-match gives another family. `resolved` names it, and
  `layout_safe` is true when the result is in `METRIC_TWINS`.
- `missing`: fc-match gives nothing, or `checked=false`.

**Alias chains**: the first installed candidate wins. Liberation is the last resort, and DejaVu
is never first except for Verdana and Tahoma.

| Font | Chain |
|---|---|
| Calibri, Calibri Light, Aptos* | Carlito, Liberation Sans |
| Cambria | Caladea, Liberation Serif |
| Segoe UI | Selawik, Open Sans, Noto Sans, Liberation Sans |
| Verdana, Tahoma | DejaVu Sans, Liberation Sans |
| Georgia | Gelasio, Noto Serif, Liberation Serif |
| Consolas | Inconsolata, Liberation Mono |
| Garamond | EB Garamond, Liberation Serif |
| Century Gothic | URW Gothic, Liberation Sans |
| Palatino Linotype, Book Antiqua | P052, Liberation Serif |
| Trebuchet MS | Open Sans, Liberation Sans |
| Arial, Helvetica | Liberation Sans, Arimo |
| Times New Roman | Liberation Serif, Tinos |
| Courier New | Liberation Mono, Cousine |

**Installer** `scripts/install_fonts.sh [--system] [--dry-run] [--no-apt] [--with-selawik]`:
- It starts with `set -euo pipefail`. It needs root for apt; without root it only downloads.
- Step 1, apt: install the packages from the recon list that are not already installed
  (dpkg-query). Packages missing from apt-cache produce a warning and are skipped.
- Step 2, downloads: `curl -fsSL` from the pinned URLs into a temp directory, then a sha256
  check. On a mismatch it deletes the file and exits non-zero. Then it installs into
  `${PWNDOC_AI_FONTS_DIR:-<repo>/data/fonts}`, or into `/usr/local/share/fonts/pwndoc-ai` with
  `--system`, and copies OFL.txt alongside.
  - Lexend: the 9 static TTFs at googlefonts/lexend@7894f02b.
  - Lexend Deca and Gelasio: the variable fonts at google/fonts@e7db6ca6.
  - Selawik: optional.
- Step 3, `fc-cache -f <dir>`.
- Running it twice changes nothing. `--dry-run` prints the actions and changes nothing (the
  tests use it).
- The hashes are in the recon report and are copied verbatim into the script.

**Health**: `FONTS_CATALOG` is WARN when catalog fonts are missing. Its detail lists the
families and `sudo scripts/install_fonts.sh`, and is shown to admins only. `PDF_FONTS` is
unchanged. After installing, restart the gateway, or `font_catalog.clear_caches()` together with
`installed_families.cache_clear()`.

## (g) PDF import

- **Order**:
  1. Upload precheck, in process and bounded.
  2. Worker: precheck again, then the text-layer check on the first 50 pages.
  3. The engine runs.
  4. The output is rechecked: python-docx reopens it, `precheck_docx` passes, and
     `pdf.check_document` passes. The output must be at most 200 MB with at most 60 images.
  5. Section detection, then typography.
- **Precheck limits** (`pdf_import/limits.py`):
  - at most 100 MiB and at most 400 pages (`len(reader.pages)`, never `/Count`)
  - MediaBox at most 14400 pt on each side
  - bounded zlib decode: at most 16 MiB per page and 128 MiB in total
  - images: at most 80 Mpx each and 400 Mpx in total
  - action chains followed to depth 8; a 15 s deadline
- **Refused**: encrypted (all of them), JavaScript, Launch, GoToR/GoToE, SubmitForm/ImportData,
  XFA, EmbeddedFiles, /AF, file attachments, RichMedia, Movie, Sound, Rendition.
- **Subprocess**:
  - The worker runs as `python -I .../pdf_import/worker.py` under `unshare -rn` when available,
    with `ulimit -v` set to 512 MB for structure and 3 GB for pdf2docx, `ulimit -t 120`, a
    wall timeout of 180 s, and `start_new_session` plus killpg.
  - Environment: `PATH`, `HOME=work`, `LANG=C.UTF-8`, and `OMP_NUM_THREADS`,
    `OPENBLAS_NUM_THREADS` and `OPENCV_FOR_THREADS_NUM` all set to 1.
  - pdf2docx runs with `multi_processing=False` and its logging silenced.
  - Each tool call (mutool, pdftotext, pdftoppm) has its own timeout. There are at most 12
    image crops per page and 60 per document, done by a pool of 4.
  - Exit codes map as follows: timeout gives `PdfConversionFailed("timeout")`, a signal or
    OOM gives `"crashed"`.
- **Text layer**: a page counts as text when it has at least 25 alphanumeric characters. It is
  image-only when images cover at least 50% of it. A share of 0.8 or more is `scanned`, which
  gives `PdfRefused("scanned")` and runs no engine. A share above 0.1 is `mixed`, which gives a
  warning.
- **Auto**:
  - Run `structure` first. Then `structure_weakness(metrics)` checks:
    - text_loss above 0.03 (measured)
    - residual tabular above 0.08 (proposed)
    - lines per page below 8 with more than 1 image per page (proposed)
    - more than 2 columns
  - If one of these holds, or structure raised, and pdf2docx is available, run pdf2docx and use
    `keep_pdf2docx` to choose.
  - If pdf2docx is unavailable, keep the structure result with the warning
    `fallback_unavailable`.
  - An explicit engine choice is final. A missing pdf2docx gives `PdfEngineUnavailable("pdf2docx")`
    (501).
- **Text loss**: NFKC normalisation, ligatures normalised, tokens of 2 or more characters,
  measured against plain `pdftotext`, with the dropped running lines added back.
- **pdf2docx output**: fonts are normalised by `strip_pdf_font_name`, and the running-line
  filter is applied before sections.

## (h) Section detection

- **Blocks**: one block per top-level `w:body` child that is a `w:p` or a `w:tbl`. Paragraphs in
  an `w:sdt` content control are unwrapped, and `sectPr` is skipped. Ids are `b%05d` in document
  order, over the same enumeration as `docx_typography.blocks.body_blocks`.
- **LLM**: used only when `use_ai` is set, `settings.llm_configured` is true, and the LLM is
  built through the gateway's late-bound `llm_factory`.
- **Batches**: at most `BATCH_BLOCKS=120` blocks and `BATCH_CHARS=12000` characters. At most
  `MAX_LLM_BLOCKS=4000` blocks go to the model; the rest get the heuristic and the warning
  `truncated`.
- Each line is a JSON object from `Block.llm_line(masked_excerpt)`. The excerpt is at most 160
  characters, masked with `Masker.mask_plain`, where the masker comes from `client_terms` plus
  `settings.mask_sensitive` (same pattern as `docx_jobs.masker_factory_for`). It is escaped with
  `<`/`>` inside `<document>` tags, and the system prompt treats it as untrusted data.
- **Tool**: `label_tool(prompts.TOOL_NAME)`. Answers may be a tuple or a dict.
- **Validation**:
  - `parse_label_answer` rejects the whole batch for: a shape other than the expected one, more
    entries than ids sent, an id not sent, a duplicate id, or a type outside the enum.
  - Any `masker.leftovers(args)` also rejects the batch.
  - A rejected batch is labelled by the heuristic, with the warning `answer_rejected`.
  - Missing ids are filled from the heuristic, with the warning `partial_coverage`.
  - `merge_labels` guarantees that every block is labelled exactly once.
- **Auth error**: stop calling the LLM and add the warning `llm_key`.
- **Heuristic**:
  - Style names: Title, Heading n, TOC n, Caption, List*, Quote, Code.
  - Then the outline level.
  - Then size and bold clustering for unstyled documents (pdf2docx output), using the PDF
    rebuild rules: heading when size is at least 1.12 × body, or bold with size at least
    0.95 × body and at most 90 characters.
  - Then mono font gives code, `^(Figure|Fig.|Table|Listing) N` gives caption, `\.{4,}\s*\d+$`
    gives toc, a table gives table, a drawing alone gives figure, and anything else is body.
- **Cache**: `<record>/sections.json` plus a `SectionsCache` keyed by the document hash, AI
  on/off and the model.

## (i) Typography OOXML rules

- **Precedence** is per property, the first value set wins: `by_section[s].by_block_type[t]`,
  then `by_section[s].all` (types in DEFAULT_*_TYPES), then `by_block_type[t]`, then `default`
  (types in DEFAULT_*_TYPES), then the document's own value. See `TypographyPlan.resolve`.
- **Sizes**: the size in pt goes into `w:sz` and `w:szCs` as half-points (`half_points`).
- **Family**: set `w:ascii`, `w:hAnsi` and `w:cs`.
  - Remove `w:asciiTheme`, `w:hAnsiTheme` and `w:cstheme` on the edited rFonts.
  - Keep `w:eastAsia` and `w:eastAsiaTheme`.
  - Do not touch `w:hint`.
- **Where edits go**:
  1. Document-wide default (both default family and size set, no section plan): edit
     `docDefaults/rPrDefault` and the paragraph styles Normal, Title, Heading n, Caption, TOC n,
     List*, the Table styles and Code, mapped to the block type they represent.
  2. Every run (`w:r/w:rPr`), plus the paragraph mark `w:pPr/w:rPr`, inside the targeted blocks
     wherever an explicit `rFonts`, `sz` or theme attribute would override the style. Edit it
     when the resolved spec differs, and add `w:rFonts`/`w:sz` when the run has none but the
     style-level route cannot express a section-specific change.
  3. Header, footer, footnote and endnote parts are typed `header`, `footer` and `footnote`
     (resolved without a section).
- **Never touch**:
  - text (`w:t`, `w:instrText`, fields), `w:tbl` structure, `w:drawing`/`w:pict`, bookmarks
  - `w:ins`/`w:del`/`*Change` subtrees
  - `w:sym` runs
  - runs whose font is a symbol font (Symbol, Wingdings*, Webdings, Marlett, Cambria Math, MT
    Extra). These are counted in `symbol_runs_kept`.
  - the theme part. It is edited only when the default family is set and no section plan
    exists: then the minor and major `a:latin typeface` are set.
- **Bounds**: a budget of 20 s by default and MAX_ELEMENTS from docx_engine.limits. Packages
  above 64 MiB are refused as `too_large`.
- **Output**: written with `write_package`, never by adding new parts. A document without a
  styles part gets run-level edits only.
- **Verify**: python-docx reopens the output, and the paragraph text sequence must be identical,
  otherwise `TypographyError("verify_failed")`.
- **Idempotence**: applying the plan twice gives the same bytes as applying it once.

## (j) UI (B-UI)

**Files**:
- `ai-export.js`: the drawer (`prepareExport`, `viewExport`, `disposeExport`).
- `ai-export-store.js`
- `ai-export-upload.js`
- `ai-export-run.js`: polling.
- `ai-export-sections.js`
- `ai-export-fonts.js`: pickers.
- `ai-export-util.js`: pure.
- `ai-font-picker.js`: shared by Settings and Export.
- `ai-settings.js`: `typographySection()`.
- Single-line additions to `ai-dock.js`, `ai-boot.js`, `ai-state.js` (`S.exp`, `freshExport`,
  `timers.export`, `aborts.exportUpload`), `ai-dom.js` (icon `type`), `ai-util.js`
  (`EXPORT_KEY`), and `inject.css` (appended `/* export */` section).

**Selectors** (`data-fk` / aria-label):
- dock button: `dock-export`, label "Export & fonts"
- drop zone: `export-drop`, "Choose or drop a DOCX or PDF"
- engine select: `export-engine`, "PDF conversion engine"
- AI switch: `export-use-ai`, "Detect sections with AI"
- client terms: `export-terms`
- start: `export-analyse`
- cancel: `export-cancel`
- progress: `export-progress` (role progressbar)
- scanned/no-text notice: `export-notice`
- conversion report: `export-report`
- sections tree: `export-sections` (role tree; items `export-section-<sid>`, with chips
  `.ai-chip` per block type)
- per-section font: `export-font-<sid>`, size: `export-size-<sid>`
- per-type font: `export-type-font-<type>`, size: `export-type-size-<type>`
- document default: `export-default-font` / `export-default-size`
- apply to all: `export-apply-all`
- preview: `export-preview` (`style.fontFamily` = the catalog css, `style.fontSize` = n pt)
- substitution notice: `export-subst-note` ("The server renders X as Y")
- downloads: `export-download-docx`, `export-download-pdf`
- delete: `export-delete`

**Settings**:
- `settings-default-font` (select with an "Other…" option) and `settings-default-font-other`
  (text input)
- `settings-default-font-size` (presets plus custom)
- `settings-heading-font*` and `settings-mono-font*`
- `settings-font-sample` (live sample)
- badges: `.ai-chip-ok` "installed", `.ai-chip-warn` "substituted by X", `.ai-chip-bad`
  "missing"

**States**: idle, uploading, converting, analysing, ready, error, unavailable (`status.export_tab
=== false`, "pip install 'pwndoc-ai[docx]'").
- When the user picks pdf2docx and it is unavailable, show its message, which includes the AGPL
  note.
- The plan is saved with PUT, debounced (600 ms), with `rev`. A 409 `stale` triggers a reload.
- No innerHTML anywhere. Font stacks come only from the catalog `css` or from `custom_css`.

## (k) Dependencies

- `pyproject.toml`: `pdfimport = ["pdf2docx==0.5.13", "pymupdf>=1.26.7,<2", "python-docx>=1",
  "pypdf>=5.1"]`, with a comment that PyMuPDF is AGPL-3.0 or Artifex commercial and that
  pymupdf 2.x needs Python 3.11 or newer.
- The structure engine needs only the `docx` extra plus mutool and pdftotext.
- `start_all.sh`: `WITH_PDF_IMPORT=0|1` appends `pdfimport` to the extras and probes `import
  pdf2docx, fitz`. `WITH_FONTS=1` runs `sudo scripts/install_fonts.sh`, and only when that flag
  is set. The integrator makes these edits.
- **When pieces are absent**:
  - no python-docx: `export_tab=false`, and the routes answer 501 `docx`.
  - no mutool or pdftotext: a PDF upload answers 501 `pdf_tools`, DOCX still works.
  - no pdf2docx: `auto` falls back to structure only; an explicit choice answers 501.
  - no LibreOffice: PDF download answers 501 `renderer`.
  - no LLM: the heuristic runs, with the warning `llm_unavailable`.
- No module imports pdf2docx or fitz at import time. Only `worker.py` does, inside a function.
- The licence is flagged in the README, in docs/FONTS.md, in the 501 text and in the final
  report. This is not legal advice.

## (l) Test plan (each builder: tmp_path, no network, no real LLM or PwnDoc; real samples skip when absent)

The test helper module names must be unique across the flat test namespace. Never name a test
file `test_model.py`.

- **Designer** (done, 142 tests): `tests/docx_typography/test_typography_model.py`,
  `tests/docx_sections/test_sections_model.py`, `tests/pdf_import/test_pdf_import_model.py`,
  `tests/test_font_catalog_model.py`.
- **B-FONTS**:
  - status rules with an injected installed set and resolver
  - alias chains emit installed candidates only
  - all of `test_pdf_fonts.py` still passes
  - `<dir>` emitted
  - health WARN text
  - `bash -n` and `--dry-run` of the installer, plus a hash-mismatch path using a local
    `file://` fake
- **B-TYPO**:
  - each rule in (i)
  - theme attributes removed, eastAsia kept, symbol runs kept, tracked changes untouched
  - idempotence, text identical, budget exceeded
  - samples demo, brand and brandarmy: all run-level Calibri becomes the target and text is
    identical
- **B-SECTIONS**:
  - block ids match those of typography
  - heuristic totality
  - fake LLM: good answer, rejected answer, partial, auth error, leftovers
  - masking: no client term in prompts
  - cache
- **B-PDF**:
  - precheck on the hostile PDFs built with `tests/pdf_helpers.make_pdf` plus raw objects
  - text-layer verdicts
  - auto selection with fake metrics
  - worker timeout and kill with a fake slow worker
  - real structure conversion on demo.pdf (skip when absent): headings ≥ 13 and loss < 3%
  - pdf2docx tests importorskip
- **B-ROUTES**:
  - every route: status codes, CSRF, owner isolation, SAFE_ID/FILE_ID 404, throttles 429,
    quotas, body limit, no-store headers
  - settings clamps and env overrides
  - audit Word and PDF downloads get `X-Typography: 1` with defaults set and identical bytes
    without
  - refine `original` and `tracked` are never changed
- **B-UI**:
  - node check of pure utils
  - fake_export contract test against the real routes (key sets)
  - Playwright e2e: upload, sections, picker, downloads, and the settings font controls
  - `node --check` on every new JS file
