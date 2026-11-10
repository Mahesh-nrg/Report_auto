# ParadigmIT VAPT: generators and the PwnDoc template installer

The `pd_*.py` / `build_word_template.py` files build the Word report (deliverable 1).
`install_pwndoc_template.sh`, `rollback_pwndoc_template.sh` and `pwndoc_installer.py` put the
PwnDoc template into the running PwnDoc.

## Install

```bash
# 1. Dry run (the default). Read-only checks run for real; every write is only printed.
scripts/paradigm/install_pwndoc_template.sh
# 2. Apply. Add --stop-services to stop the backend and frontend during the install and start them
#    again afterwards. MongoDB stays up. Stopping them is optional, because PwnDoc reads templates per request.
scripts/paradigm/install_pwndoc_template.sh --apply [--stop-services] \
    [--template /path/ParadigmIT_VAPT_PwnDoc_Template.docx] [--name 'ParadigmIT VAPT']
```

What `--apply` does, in this order:
1. **Preflight.** Checks that the three containers are running and that mongo answers a ping. Checks that `/app/report-templates` is bind-mounted from
   `pwndoc/backend/report-templates`. Checks that the template is a docx zip containing `word/document.xml`. Checks that the name is safe
   (`[A-Za-z0-9 _.-]`, max 64). Checks that no template, audit type or file already uses the name.
2. **Backup.** Writes a `mongodump --archive --gzip` of the `pwndoc` db and a sha256 listing of report-templates to
   `pwndoc/backend/backup/paradigm-<timestamp>/`, then checks that the archive is valid gzip and not empty.
3. **Install.** Copies the file to `report-templates/<name>.docx`. Inserts a `templates` doc `{name, ext:'docx', createdAt, updatedAt, __v:0}`.
   Inserts a NEW `audittypes` doc `<name>` with templates for locales `1` and `en`, sections `["Report starting", "highlights"]`,
   stage `default`, hidden `[]` (as in `v1`, plus the highlights section) and order = max+1. It inserts the missing `CUSTOM_FIELDS`:
   Application Name, Assessment Type and Target URL (general), Steps to Reproduce (finding, text), and the Text field of the
   new custom section 'Assessment Highlights' (`highlights`), which it also creates if missing.
   It installs `pwndoc_custom/report-filters-custom.js` (the `paradigmHTML`, `sortFindings`, `bandCount` and `inBand` filters the template needs) into the bind-mounted
   `pwndoc/backend/src/lib/custom/`. It refuses a foreign custom filter file and backs up the old file. Then it restarts the backend.
   The created ids are recorded in `paradigm-<timestamp>/created.json`.
4. **Verify.** Runs `find()` on both docs and checks the links. Runs `ls -la /app/report-templates` inside the backend. It also runs
   an offline render with the container's generator using `pwndoc_custom/run_render.sh`, without `--filters`, so it proves that the installed filter loads. It checks that the installed file defines `sortFindings`/`bandCount`, renders `rendered_samples/sshuffled.json` (19 findings stored in a shuffled order) and checks with `pwt_order_check.py` that the output is in CVSS v3.1 order with S. No. 001.. and band colours.
5. Prints the audit type name, the exact rollback command, and the full-restore command.

Existing templates, audit types and custom fields are never modified.
Values reach the mongo shell only as JSON literals in a script piped on stdin. No shell interpolation happens.

## Configuration

Edit the tables at the top of `pwndoc_installer.py`:
- `AUDIT_TYPE_DEFAULTS` and `LOCALES` set up the audit type.
- `CUSTOM_FIELDS` and `HIGHLIGHTS_SECTION` hold the fields and section the template reads. Rollback removes only what was created, and it restores or removes the filter file.

Environment overrides: `DOCKER` (the docker command, used by the tests' stub), `PYTHON`, `PWNDOC_BACKEND_DIR`,
`PWNDOC_MONGO`, `PWNDOC_BACKEND`, `PWNDOC_FRONTEND`.

## Update after the template changes

```bash
scripts/paradigm/install_pwndoc_template.sh --apply --update --template <new.docx>
```
Use `--stop-services` to stop the backend/frontend during the swap. This takes a backup first (mongodump, the installed `<name>.docx`
as `<name>.docx.orig`, and the installed filter file as `report-filters-custom.js.orig`). It then replaces the template file,
refreshes `src/lib/custom/report-filters-custom.js` (only when it differs; a foreign filter file is refused), sets `updatedAt` on
the existing template doc (ids unchanged), restarts the backend and verifies the render with the INSTALLED filter (see 4).
Everything is recorded in `paradigm-<timestamp>/updated.json`; `rollback_pwndoc_template.sh --apply --backup-dir <that dir>`
puts the previous template file and filter back and restarts the backend (it never deletes ids for an update record).
Without `--apply` it is a dry run.

## Roll back

```bash
scripts/paradigm/rollback_pwndoc_template.sh            # dry run: shows the ids and file it would delete
scripts/paradigm/rollback_pwndoc_template.sh --apply [--name 'ParadigmIT VAPT'] [--backup-dir pwndoc/backend/backup/paradigm-...]
```
For an install record the rollback deletes ONLY the ids in `created.json`, matching the audit type and template by both `_id` and name. It also deletes `<name>.docx`.
Running it twice is safe. As a last resort you can restore the whole DB with
`docker exec -i pwndoc-mongodb-1 mongorestore --drop --gzip --archive < .../pwndoc.archive.gz`.

## Tests

`.venv/bin/python -m pytest -q tests/paradigm_template/test_installer_script.py` runs against a docker stub, so nothing real is touched.

## Finding order (docs/FINDING_ORDER.md)

Every findings loop of the template is `{#findings | sortFindings}`: Critical -> Informational by CVSS v3.1 score (derived band,
not the typed label), highest first, ties by the stored identifier, S. No. = `{seq}` (001..). Counts use `{findings | bandCount:'High'}`.
The Word report and the import sample list their example findings in the same order (`pd_order.py`).

- `node tests/paradigm_template/js/finding_order_parity.test.js`: the JS filter against the 63 spec vectors.
- `pwt_samples.py OUT` also writes `sshuffled.json`; `pwt_order_check.py AUDIT.json RENDER.docx [RENDER.pdf]` checks a render
  (headings, register, severity colours, page-3 counts).
