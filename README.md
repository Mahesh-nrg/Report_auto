# pwndoc-ai

An AI report assistant for [PwnDoc](https://github.com/pwndoc/pwndoc). It adds
**✨ Update with AI**, **📄 Finish report**, **⬇ Download report** and **History** buttons to the
audit pages of a stock, unmodified PwnDoc and an **⚙ AI Settings** panel (LLM endpoint, key, model,
fallbacks, privacy and a health check) for administrators. A **Refine a DOCX** button on every page
lets any signed-in user upload an existing Word report and have its text polished in place.

PwnDoc itself runs unchanged in Docker. A small gateway (FastAPI, bound to `0.0.0.0` so the whole
LAN can use it) sits in front of it, proxies every request, and injects the AI controls into the
PwnDoc pages. It talks to PwnDoc through its
[REST API](https://pwndoc.github.io/pwndoc/api/) using the permissions of the user who is signed in.
No PwnDoc credentials are stored.

> No PwnDoc fork, no rebuilt front end, no report data in this repository.

## Quick start

> ### ⚠ Change the default password
> `start_all.sh` creates the PwnDoc administrator **`admin` / `Admin@12345`**. This default is public
> (it is in this repository), so treat it as a placeholder:
>
> - **Best:** choose your own before the first run:
>   `PWNDOC_ADMIN_USER=alice PWNDOC_ADMIN_PASSWORD='Str0ng-Passw0rd' ./start_all.sh`
>   (at least 8 characters with an upper-case letter, a lower-case letter and a digit).
> - **Or:** log in once and change it in PwnDoc (user menu, top right > **Profile** > new password).
>   Do this before giving the address to anyone, and never leave the default on a network you do not
>   fully trust.
>
> The script can only set the password on a **fresh** PwnDoc database. If PwnDoc already has users it
> leaves them alone, checks whether the configured login works, and warns if it does not.

### Requirements

| What | Why |
|---|---|
| Linux or macOS host with `bash`, `git`, `curl`, `python3` (3.10+) | run the scripts and the gateway |
| Docker Engine with the **compose plugin** (`docker compose version`) and a running daemon | runs PwnDoc and MongoDB |
| Internet access on first run | clone PwnDoc, pull its images, `pip install` |
| An API key for an OpenAI-compatible LLM (or a local Ollama) | needed only when you use **Update with AI** |

### Install and run

```bash
# 1. Get the code
git clone https://github.com/Mahesh-nrg/Report_auto.git
cd Report_auto

# 2. (Recommended) pick your own admin password, then start everything
export PWNDOC_ADMIN_PASSWORD='Str0ng-Passw0rd'     # skip to use the default Admin@12345
./start_all.sh
```

The first run takes a few minutes (image download). The script then:

1. checks the prerequisites above;
2. clones upstream PwnDoc (pinned release) and starts it with Docker, listening on `127.0.0.1` only;
3. waits until PwnDoc answers and creates the administrator on a fresh install;
4. creates `.venv`, installs the gateway and starts it on `0.0.0.0` over HTTPS;
5. prints a summary with the **port to open**, the **address** and the **login**, for example:

```text
 1) PORT TO OPEN (firewall / security group), TCP, inbound from your LAN:
      8080/tcp   AI gateway + PwnDoc UI   <- the only port users need
 2) ADDRESS TO GIVE USERS:   https://<this-machine-ip>:8080
 3) LOGIN TO GIVE USERS:     Username: admin    Password: <your password>
```

Open only the **gateway port** to your LAN (default `8080`; the script picks the next free port and
tells you if it is busy). PwnDoc's own port (`8443`) stays on `127.0.0.1`, so do not open it.
Example with ufw: `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp`.

### First-time setup in the browser

1. Open the printed address. The certificate is self-signed, so the browser warns once: choose
   **Advanced > Continue**.
2. Log in with the printed username and password, then **change the password** if you kept the
   default (see the warning above).
3. As an administrator, click the purple **⚙ AI Settings** button, enter the LLM **base URL**, **API
   key** and **model**, and press **Test connection**, then **Save**.
4. Open any audit and click **✨ Update with AI**. Other PwnDoc users log in with their own accounts
   (an administrator creates them in PwnDoc's user management).

### Day-to-day commands

```bash
./start_all.sh              # start (or re-run safely; it never recreates existing data)
./start_all.sh status       # gateway process and PwnDoc containers
./start_all.sh stop         # stop gateway and PwnDoc; all data is kept
./start_all.sh --help       # every option
tail -f logs/gateway.log    # gateway log
```

Report data lives in the PwnDoc Docker volumes; gateway settings, backups and the RAG index live in
`./data` (see [Configuration](#configuration)). `stop` removes neither. To update, run
`git pull && ./start_all.sh`.

| Variable | Default | Purpose |
|---|---|---|
| `PWNDOC_ADMIN_USER` / `PWNDOC_ADMIN_PASSWORD` | `admin` / `Admin@12345` | First-run administrator (upper, lower, digit, 8+ chars) |
| `GATEWAY_PORT` | `8080` (next free port if busy) | The one port users need; open it in your firewall |
| `PWNDOC_PORT` | `8443` (next free port if busy) | PwnDoc's own port, kept on `127.0.0.1` |
| `GATEWAY_MODE` | `https` | `http` serves plain HTTP (trusted networks only) |
| `PWNDOC_PUBLIC` | `0` | `1` publishes PwnDoc's port on all interfaces too |
| `WITH_RAG` | `0` | `1` installs the local-documents extra (large download) |

### Troubleshooting

| Symptom | Fix |
|---|---|
| `port 8080 is busy, using 8081` | Normal. Open the port shown in the summary, or set `GATEWAY_PORT` |
| `docker: command not found` / daemon not running | Install Docker and start it (`sudo systemctl start docker`) |
| Image pull fails with `docker-credential-desktop not found` | Handled automatically with an anonymous Docker config |
| Warning that the admin login does not work | PwnDoc already had users: use the password you set earlier, or reset it in PwnDoc |
| Browser says the connection is not private | Expected with the self-signed certificate; continue once |
| Need to see what went wrong | `tail -n 100 logs/gateway.log` and `docker compose -p pwndoc logs` in `./pwndoc` |

### Manual install (without `start_all.sh`)

```bash
# 1. Fetch and start upstream PwnDoc (clones a pinned release into ./pwndoc, prebuilt images)
./scripts/setup_pwndoc.sh

# 2. Install and start the gateway, reachable from your LAN
python -m venv .venv && . .venv/bin/activate
pip install -e '.[rag,https]'        # extras are optional, see below
# optional: '.[pdfimport]' adds the pdf2docx PDF engine (PyMuPDF: AGPL-3.0), WITH_PDFIMPORT=1 in start_all.sh
pwndoc-ai serve --host 0.0.0.0 --port 8080 --https
```

Then open `https://<this-machine-ip>:8080`. On a brand-new PwnDoc, its first-run page asks you to
create the administrator yourself.

`serve` refuses to listen on a non-loopback address over plain HTTP unless you pass
`--insecure-http` (or set `PWNDOC_AI_ALLOW_HTTP=1`), because logins and the API key would then
cross the network in clear text. Use that only on a network you trust.

Already have PwnDoc running elsewhere? Set `PWNDOC_URL` to its address (see
[Configuration](#configuration)). Run `pwndoc-ai --help` for all options.

### Docker

```bash
cp .env.example .env        # set LLM_*, and PWNDOC_URL if PwnDoc is not on this Docker host
docker compose up -d --build        # gateway on :8080
# EXTRAS=rag,docx,https docker compose up -d --build   # include optional features
```

The compose file only starts the gateway. By default it reaches PwnDoc on the Docker host through
`https://host.docker.internal:8443`; inside a container `localhost` is the container itself, so do
not use it for `PWNDOC_URL`. The stock self-signed PwnDoc certificate needs
`PWNDOC_VERIFY_TLS=false` there (already set in `.env.example`). The compose service acknowledges
plain HTTP with `PWNDOC_AI_ALLOW_HTTP=1`; to serve HTTPS instead, build with `EXTRAS=https` and
override the command with `["pwndoc-ai", "serve", "--host", "0.0.0.0", "--port", "8080", "--https"]`.
Settings, backups and the RAG index live in the `gateway-data` volume.

Note that upstream PwnDoc publishes its own port (`APP_PORT`, default `8443`) on all interfaces, so
it remains reachable from the LAN next to the gateway. `setup_pwndoc.sh` explains how to keep it
private.

## Using it

The button works on whichever page you are on and picks up the audit from the URL automatically.

1. Click **✨ Update with AI** on an audit (or on a single finding).
2. Choose the scope (this finding, selected findings or the whole audit), the fields to update, the
   mode (**Improve existing text** or **Only fill empty fields**) and whether to apply automatically
   (off by default). Internet search and local documents are global options an administrator sets in
   AI Settings.
3. The AI runs as a background job and shows a **preview** of every proposed change.
4. Select the changes you want and **Apply**.
5. Open pages normally refresh by themselves; use **Reload page** if they do not.

The AI rewrites title, description, observation, PoC, scope, remediation and references, and fills
the audit's custom fields (CWE, CVSS score, severity, ...). It may only reuse screenshots
(`<img>` tags) that already exist in the finding: they are always preserved, and the model cannot add
new or external images. Output is sanitised to a small set of formatting tags before it reaches
PwnDoc.

Safety behaviour on apply:

- Right before the first write of a job, the current findings are saved to
  `<data dir>/backups/<audit id>/<UTC timestamp>_<job id>.json`.
- If someone edited a finding in PwnDoc after the preview was made, that finding is **not**
  overwritten; it is marked failed with "changed in PwnDoc since the proposal" and you can re-run it.
- PwnDoc sessions are short-lived. If yours expires during an automatic run, remaining proposals
  stay available to apply manually after you sign in again.
- Each user can run one job at a time, and the gateway runs at most four jobs at once.

### Finish report (one click, Word file at the end)

When the testers have finished adding screenshots and URLs to an audit, click **📄 Finish report**
and confirm. The gateway then, using the signed-in user's own PwnDoc session:

1. checks the audit can be exported (its audit type must have a template for the audit's language,
   otherwise it stops *before* spending any LLM budget and tells you what to fix);
2. reads every finding of the audit, researches each one (web search and local documents, as set in
   AI Settings), rewrites **every** field and fills the custom fields (CWE, CVSS, severity, ...),
   keeping the screenshots, and saves the result to PwnDoc (the originals are backed up first, see
   above);
3. builds the Word report once to prove it works, then **downloads it automatically**.

Keep the tab open while it runs: PwnDoc rotates its short-lived session token and the open page is
what keeps the gateway supplied with the newest one. A finding that fails (for example the model's
safety filter blocks it) keeps its original text; the report is still produced and the finding is
marked failed in the list. If the Word export itself fails, the rewritten findings are already saved
and the panel says so. If the session expires before everything is saved, no report is produced.

### Download report (Word or PDF, any time)

**⬇ Download report** is available on every audit page. It builds the report *right now* from the
audit as it is saved in PwnDoc, with the audit's own template, and gives you:

- **Word (.docx)**: exactly what PwnDoc produces.
- **PDF**: the same document rendered by headless LibreOffice.

Nothing is stored on the gateway and nothing expires: the audit lives in PwnDoc's database, so you
can download the current report whenever you need it, as often as you like. No AI is involved.

PDF needs LibreOffice on the gateway host (Word does not):

```bash
sudo apt install libreoffice-writer-nogui      # Debian / Ubuntu / Kali
```

Each conversion runs in its own throw-away profile and temp directory with a 3 minute limit, and at
most two run at once (a third request waits up to 5 s, then gets a "busy, try again" answer). Before
LibreOffice opens a report it is checked: oversized archives and documents that link to external
images, templates or OLE objects are refused, so a hostile document cannot make the host fetch URLs
or read local files while converting. The download endpoint, like every other gateway action, only
accepts requests that carry the page's own `X-Requested-With` header.

Set `PWNDOC_AI_SOFFICE=/path/to/soffice` if LibreOffice is not on `PATH`. Without it the PDF button
explains what to install and the Word download keeps working.

### Layout check and tidy (blank pages, orphan headings, fonts, table of contents)

A PwnDoc template report often comes out with layout defects: hard page breaks in the middle of
runs of empty paragraphs (blank pages that hold only the header and footer), a heading alone at the
bottom of a page or with nothing under it, and runs of empty paragraphs used instead of spacing. The
download drawer therefore has a **Layout check** block. It runs when the drawer opens, shows
**Looks good** or a short list ("2 blank pages would appear, 12 stray empty paragraphs, 1 empty
section") and the note *These are fixed automatically in the download.* The switch **Tidy layout
(recommended)** is on by default; switched off, the download carries `tidy=0` and is the file
PwnDoc built, byte for byte. After a PDF download the drawer says what was done ("Removed 4 blank
pages, table of contents page numbers updated"). The same block is in the **Refine a DOCX** result
view, for the clean file. A failing check never blocks a download.

What the tidy does, and what it never does:

- **Empty paragraphs.** A run of empty paragraphs is cut to at most one; none is kept next to a page
  break, at the start of a page or section, or at the end of the document.
- **Page breaks.** A hard page break that would create an empty page is removed (consecutive breaks,
  a break straight after a break or a section break, at the very start or end of the document, or
  before a paragraph that already starts a new page); a break inside an otherwise empty paragraph
  becomes "page break before" on the next paragraph when the page looks the same.
- **Empty sections.** A heading whose section holds only empty paragraphs and breaks is removed with
  them. Never the first heading, and never a heading with text, a table, an image, a field or a
  referenced bookmark.
- **Headings stay with what follows.** Headings and captions get "keep with next", so a heading is
  not left alone at the bottom of a page.
- **Safety limits.** The tidy never changes text and never removes images, tables, fields,
  bookmarks, hyperlinks, footnotes, comments, content controls or the section properties that carry
  headers, footers, page size and orientation. Parts it does not edit stay byte-identical, tidying
  twice changes nothing, and a document that already checks clean is returned as it is. A document
  with tracked changes is never tidied, and neither are the **Original** and **Word (tracked
  changes)** downloads; the stored files are never touched (the clean copy is tidied when it is
  downloaded). The Word file is treated as untrusted input (zip and XML limits, no entity
  expansion).

The PDF renderer is improved for both kinds of report:

- **Fonts.** Word documents ask for Calibri, Aptos and Cambria, which a Linux host does not have;
  LibreOffice then uses DejaVu Sans, about 15 percent wider, and every line break and page number
  moves. The renderer maps those fonts to the metric-compatible Carlito and Caladea (and Segoe UI,
  Tahoma, Verdana to Liberation Sans; Consolas, Courier New to Liberation Mono) when they are
  installed, and works without them. Install them once on the gateway host:

  ```bash
  sudo apt install fonts-crosextra-carlito fonts-crosextra-caladea
  # or the whole curated set (Lexend, Open Sans, Roboto, Inter, Noto, Liberation/Arimo...):
  sudo scripts/install_fonts.sh --yes    # --dry-run first to see what it does; see docs/FONTS.md
  ```

  A missing font is reported: in the Layout check ("Calibri is not installed; the PDF uses a
  substitute"; the install command is shown to administrators only, in **AI Settings > Health**,
  check **PDF fonts**, which warns but never errors).
- **Blank pages.** A page of the produced PDF with no text, image or drawing beyond the running
  header and footer is removed (never the first page; the last only when blank).
- **Table of contents.** LibreOffice does not update the page numbers of a Word table of contents. The
  renderer finds the page of each heading in the PDF, patches the cached numbers in the document
  (never the entries' text) and renders again, at most twice in all.

Opt out per download with `tidy=0` (`GET /__ai/api/audits/{id}/report?format=pdf&tidy=0`, or the
switch). `GET /__ai/api/audits/{id}/report/check` and `GET /__ai/api/docx/files/{id}/check` return the
findings without downloading. Downloads carry `X-Layout-Fixes` and, for PDF, `X-Pdf-Pages`,
`X-Pdf-Removed-Blank` and `X-Pdf-Toc-Updated`.

### Refine a DOCX (polish an existing Word report in place)

**Refine a DOCX** is on every page for any signed-in user (it is not tied to an audit and not
administrator-only). Upload a Word report that **already exists**, whether or not PwnDoc produced it.
The PwnDoc template is never run. The gateway goes through the document, improves the **text in
place**, and you download the result as Word or PDF.

1. Click **Refine a DOCX**, then drop a `.docx` on the drop zone (or click it, or press Enter or
   Space on it). Exactly one file, up to 50 MB. The upload shows real progress and the file is then
   open with its heading outline.
2. Choose **what to refine**: the **whole document**, or **Pick sections**. The outline lists the
   headings with the number of paragraphs and characters in each, all ticked by default; ticking a
   heading ticks the sections under it, and **Select all / Select none** are there too. Use it to
   skip a cover page or legal text.
3. Choose **how deep**:
   - **Light polish**: copy-editing only. Grammar, clarity, consistent terminology and tone. It adds
     **no facts**.
   - **Deep refine**: also researches (the web and your local books, through the same sources and
     settings as the other AI features; short queries are built from section headings) and
     strengthens the content: technical clarity, CWE and OWASP references where a vulnerability is
     discussed, clearer remediation and proof-of-concept steps. It may add a few new paragraphs. It
     never invents evidence such as hosts, credentials, payloads or screenshots, but **check every
     new fact**: this mode can add detail.
4. Optionally list **client or company names to hide from the AI** (comma separated). The model
   sees believable fictional stand-ins instead (a made-up company, fictional domains and IP
   addresses) and the real values are put back afterwards, together with the domains, IP addresses
   and e-mails that masking always covers. Paths and payloads are sent as they are. Masking reduces
   what the provider learns; it is not anonymisation (see Security notes).
5. **Start refining.** The panel shows a progress bar, the section being worked on and the counts
   (changed, rejected, protected). **Cancel run** stops it. You can close the panel or reload the
   page: the run keeps going on the gateway, and reopening the panel follows it again.

When it finishes you get a summary, **Review changes** (every changed paragraph before and after,
with the changed words highlighted, loaded 50 at a time) and four downloads:

- **Word (clean)**: the refined text, with every change accepted.
- **Word (tracked changes)**: the same edit as real Word revisions (insertions and deletions), so a
  reviewer can accept or reject each one in Word.
- **PDF**: the clean file rendered by LibreOffice, with the same requirement and limits as
  **Download report** above, including the layout tidy and the font handling (PDF is only offered for the clean and the original file; if LibreOffice
  is missing, or the document links to external content, or the converter is busy, the panel says so
  and Word keeps working).
- **Original**: the file you uploaded.

How the text is edited: the model proposes new text per paragraph, and only the **words that changed**
are edited. Unchanged words keep their original runs and formatting; styles, numbering, sections,
tables, images, headers, footers and every other part of the file are left as they were. A paragraph
the engine cannot edit safely is **protected**: it is never sent to the model, stays byte-identical
and is counted in the summary. Protected are paragraphs with images or drawings, fields (including
tables of contents and page numbers), footnote or endnote references, comments, equations, content
controls or symbols, table-of-contents styles, code-style paragraphs and empty paragraphs. Only
the main document body is processed: text in headers, footers, footnotes, endnotes and comments is
not refined and not counted, so check those by hand. Table cells are normal paragraphs and are edited. Every proposed paragraph is checked
too: in light mode every IP address, URL, e-mail, CVE or CWE id, number and code-like token of the
original must still be there and the length must stay in a sane range; a paragraph that fails is
shown as **Rejected** with the reason and left as it was. Deep mode keeps every number of the
original too and adds a number only when the research text states it or it is part of a CWE, CVE or
OWASP reference; it links only to trusted reference sites (cwe.mitre.org, nvd.nist.gov, owasp.org
and similar), never to a link that merely appears in the research. A paragraph added in deep mode
at the very end of the document or of a table cell is tracked so that rejecting all changes in Word
gives back the original; after a heading or a section break at that position none is added.

**Limits of one run.** A run may make at most 400 model calls (about 2.4 MB of text) and, in deep
mode, research at most 150 sections; a larger document is refined in parts by picking sections. A
document may have at most 25,000 paragraphs and a 16 MB main part. The word diff is bounded in cost:
very long, repetitive paragraphs are marked as one larger change instead of word by word, and
**Cancel** also works while the result is being built. For PDF export LibreOffice runs with a memory
limit (3 GB address space, `PWNDOC_AI_SOFFICE_MEMORY_MB`, 0 = none) and a CPU limit, pictures above
80 megapixels (400 in total) are refused, and a produced PDF is kept in memory for repeated downloads.
Documents are stored under the PwnDoc account id (not the user name), so a re-issued user name does
not see the old account's files; files of a deleted PwnDoc account stay on disk until removed with
`DocxStore.purge` (there is no automatic expiry).

**Files are kept until you delete them.** The uploaded file and every refined copy stay on the
gateway host, in your own list under **My documents**, until you press the trash icon and confirm
(that removes the original and the refined copies). Only the user who uploaded a file can see or
download it. Because these are client reports, treat the gateway host's storage like the backups
described under Security notes, and delete documents you no longer need. Quotas: 25 files and 1 GB
per user and 5 GB for the whole gateway; an upload over the quota is refused with a message to
delete some files. The list shows how much of it you use.

Limits to know about: `.doc` files are not supported (save as `.docx` first); documents with macros
(`vbaProject.bin`), zip bombs and malformed or absurdly structured files are refused with a short
message; one refine runs per user at a time; the document text is untrusted input to the model, so
the usual prompt-injection caveats apply and a model can only change the paragraphs it was given,
as plain text. An LLM must be configured in AI Settings, and the feature needs `python-docx`:
`pip install "pwndoc-ai[docx]"` (the drawer says so when it is missing).

### Import a DOCX (turn a finished Word report into a PwnDoc audit)

**Import a DOCX** is on every page for any signed-in user when the gateway has the DOCX extra
installed (`pip install "pwndoc-ai[docx]"`). It reads an existing, prefilled report that was made
from the **active PwnDoc template** and creates, with **your own PwnDoc session**, a company (when
it does not exist yet), an audit and all its findings with their screenshots. Nothing is written to
PwnDoc until you confirm, and one click undoes the import.

1. Click **Import a DOCX**, then **Choose a .docx report** (or drop the file on it). The upload uses
   the same file store, size cap (50 MB) and checks as **Refine a DOCX**, so the report is kept in
   **Refine a DOCX › My documents** until you delete it there (it counts toward that store's quota,
   and deleting an import does not remove it). **Use AI to fill missing fields** is on by default
   when an LLM is configured; without one, only the document reader is used.
2. The gateway reads the report (a progress bar shows the step; **Cancel** stops it). You can close
   the panel or reload the page; reopening it follows the import again.
3. Check the **preview**. Nothing in it has reached PwnDoc yet.
   - **Completeness** lists what is missing or doubtful, each marked **Required**, **Check** or
     **Note** (in words, not only by colour), with **Go to field** to jump to the control that fixes
     it. Required items (no company, no audit name, a finding without a title, a bad date) block the
     create. The other items can be fixed, or accepted with **Create with the blanks listed above**.
   - **Audit**: company, audit name, language, audit type, template, start and end date, scope and an
     optional contact. The **audit name** defaults to the file name, because the report does not carry
     one. The **company** is the name the cover, the page header, the application table and the
     disclaimer agree on; it is never taken from the report title or the body text. A company name
     left over from the template in the body text (such as “Alfred AI”) is reported, not used; when
     the cover itself names that template company, it is kept but flagged so you check it. A chip
     says whether PwnDoc already has that company (it is reused) or it will be created.
   - **Findings**: every finding once. The template lists each finding twice (the summary table and
     the detail table); the two copies are merged, keeping the richest one, and marked **Listed twice,
     merged**. A finding that is only in the summary table is marked so. Every field can be edited.
     Rich fields (description, observation, proof of concept, remediation) are plain text here: a blank
     line starts a paragraph, lines starting with `- ` or `1. ` make a list, `**bold**` and `` `code` ``
     keep their marks. Lines like `[Screenshot i001]` place a screenshot in the proof of concept; a
     live preview shows the result.
   - **Screenshots**: each picture of the report with its caption. It goes into the proof of concept
     of the finding it is attached to, at the place the figure caption showed; a picture without a
     caption is put after the nearest finding and marked, never dropped silently. Change its caption,
     attach it to another finding, or leave it out. Pictures PwnDoc cannot show (EMF, SVG, WEBP, TIFF)
     and placeholders are listed and left out.
   - **Save changes** sends only what you changed. If the preview changed in another tab, it is
     reloaded and you are told.
4. **Create in PwnDoc** asks first (“Create audit “…” with N findings in PwnDoc?”, with **Go back**
   focused). If PwnDoc already has an audit of that name for that company you must also tick **Create
   another audit with the same name**; an existing audit is never overwritten.
5. When it is done you get **Open the audit** and the counts. **Undo this import** deletes only what
   this import created (the audit with its findings, the contact and the company if it created them,
   and its screenshots) and never anything someone else now depends on:
   - if findings were added to the audit by hand after the import, the undo refuses and deletes
     nothing (remove them first, or delete the audit in PwnDoc);
   - the contact, the company and the screenshots are deleted only when no other audit, contact or
     library entry uses them (a second import of the same report reuses them). Only an administrator
     can see every audit, so for other users the company and the contact stay, and PwnDoc lets only
     administrators delete images; the panel says what stayed.
   If a create fails halfway, everything it had created is removed again (with the same rules).

**Delete this import** removes the preview, the extracted screenshots and the undo journal from the
gateway; PwnDoc is not touched, so an audit that was created stays and can then no longer be undone
from here. **Recent imports** lists your last imports (at most 20; each user sees only their own).

What is read and what is not: the cover, page header, application, scope and date tables, and the
summary and detail tables of the findings (title, severity, CVSS score, CWE/CVE, URLs, parameter,
location, description, proof of concept, remediation, references). The template has no contacts,
tester names or audit type, so those come from you or from PwnDoc's own defaults (your most recent
audit). The literal `undefined` that PwnDoc prints for empty values is read as blank.

**Masking.** The document reader runs on the gateway, without the AI model. When AI fallback is on,
only the fields the reader could not fill go to the model: the header call sees the cover and header
text with the real client names (reading them is its job); every call about finding content goes
through the same masking as the other features (client names, contact names, scope domains, IP
addresses and e-mails become fictional stand-ins and are put back afterwards), and an answer that adds facts that are
not in the document is rejected. Fields the AI filled are marked **AI filled** and listed in
Completeness.

Limits: 200 findings and 200 screenshots per import, 8 MB per screenshot (64 MB in all), 20 imports
and 512 MB per user, one running step per user. All imported text is escaped and kept to a small set
of HTML tags (paragraphs, line breaks, lists, bold, italic, code); nothing from the file is ever
passed through as raw HTML.

### Export & fonts (fonts per section, from a DOCX or a PDF)

**Export & fonts** in the dock (any signed-in user, on every page) changes the fonts and sizes of a
Word or PDF document without touching its text:

1. Drop a `.docx` (up to 50 MB) or a `.pdf` (up to 100 MB). A PDF is converted to Word first. Pick the
   **PDF conversion engine**: *Auto* (the structure rebuild, then the layout-faithful engine when the
   rebuild is measurably poor), *Structure rebuild* (headings, lists and tables from mutool and
   pdftotext; always available when those tools are installed) or *Layout-faithful (pdf2docx)*.
   pdf2docx is optional (`pip install 'pwndoc-ai[pdfimport]'`) and uses **PyMuPDF, which is
   licensed under the AGPL-3.0**; the drawer says so before you choose it. A scanned PDF (no text
   layer) is refused with a clear notice: OCR is not installed.
2. **Detect sections with AI** (on by default) labels every paragraph and table (title, heading
   levels, body, list, table, code, caption, header/footer…) and groups them into sections. Client
   names you list are masked before anything reaches the model; switched off (or with no LLM) a
   rule-based detector does the same from style names, sizes and bold text. The AI never changes
   text.
3. Choose fonts: per section (click or arrow through the section tree, each with its block-type
   chips), per block type, or for the whole document; **Apply to all sections** makes the chosen
   section's font the document default, **Reset all fonts** starts over. Each dropdown says whether
   the gateway has the font *installed*, renders it *substituted by* a twin (Calibri by Carlito,
   Aptos by Carlito, Georgia by Gelasio…) or is *missing*; **Other…** takes any font name. The live
   preview uses your browser's fonts and says when the server only has a stand-in. Choices are
   saved on the gateway as you go.
4. **Download DOCX** or **Download PDF** (the same tidy and renderer as the report download).

**Default fonts of downloaded reports** (administrators, AI Settings → *Document fonts*): a font and
size for body text, headings and code, applied to the Word and PDF reports downloaded from PwnDoc and
to the clean copy of a refined DOCX (never to an original upload). Leave a choice on *Keep* to use the
template's font. Missing fonts are listed in the Health check with the install command
(`sudo scripts/install_fonts.sh`); see [docs/FONTS.md](docs/FONTS.md).

## More report features

All of these work on the audit you have open and use your own PwnDoc session, so PwnDoc keeps
enforcing who may see and change what.

### Report check, review and summary (in **📄 Finish report**)

- **Report check.** When the drawer opens, the gateway lints the audit without changing it and shows
  error, warning and note counts plus a collapsible issue list. It never blocks **Finish**. It flags,
  for example: no findings or no template, empty description, remediation or PoC, a PoC with no
  screenshot, an invalid CVSS vector, a severity field that disagrees with the vector, placeholder
  text (lorem ipsum, TODO, TBD), duplicate titles, empty custom fields and missing references. Set a
  LanguageTool server URL in AI Settings to add spelling notes (text is sent to that server).
- **Review changes before saving.** Turn this on and the button becomes **Finish and review
  changes**. It opens **Update with AI** with every finding and field selected (improve mode, no auto
  apply) and starts it, so you read each diff and apply what you want. No report is built until you
  download it.
- **Draft executive summary.** The model writes the prose and a remediation roadmap; the finding
  counts by severity are computed by the gateway, not by the model. The draft is written into the
  audit's executive-summary field (the first rich-text field whose label or section name contains
  "executive", "summary" or "management") and always shown in the drawer. A summary that is already
  there is **never replaced silently**: the drawer says so and offers **Replace the existing
  summary** (with a confirmation; the old text is saved under `<data dir>/summary-backups/`). The
  field is re-read right before writing, so edits to other fields of that section survive. The
  summary contains no links. If the audit has no such field, or saving fails, copy the text from
  the drawer. Reload the page to see a written summary in the editor.
- **Save findings to the library.** Adds the generic wording of the audit's findings (title,
  description, remediation, references, CVSS) to PwnDoc's vulnerability library in the audit's
  language. The library is readable by every PwnDoc user, so the observation is never saved and a
  finding whose text still names the client, a host, an IP address or an e-mail is skipped with a
  reason. Titles already in the library (in any language), or repeated in the batch, are skipped
  and listed with the reason.
- **Limits.** Summary, report check, library, history and restore are limited per user (one at a
  time and a few per minute); a request over the limit gets HTTP 429. The spelling pass stops
  after 20 seconds and its result is reused until the findings change.

### History and restore (**History** button)

Every update and finish run is recorded (when, who, status, findings applied or failed, models used,
tokens, whether a report was built) in `<data dir>/ai-log.jsonl`, rotated at about 5 MB. The log has
no finding text. The same drawer lists the backups taken before AI changes, newest first. **Restore**
asks for confirmation, first saves the *current* findings as a new backup (so a restore can itself be
undone), then writes the old text back to the findings that still exist in the audit. Fields that
were empty in the backup are emptied again, so text the AI added to them is removed. A restore is
refused while an AI job is running on the audit. If nothing could be written (for example you only
have read access) no new backup is kept; the newest 100 backups per audit are kept. Findings
that were deleted from the audit since the backup are reported as skipped. A run that was still going when the
gateway restarted is shown as *Interrupted*.

### Health check (administrators, in AI Settings)

The **Health** section checks the setup the report features rely on: a language exists, audit types
and templates exist, every audit type has a template for each language, the CWE, CVSS score,
severity and URL custom fields exist, the LLM is configured, local documents are indexed, LibreOffice
is installed, whether the PDF fonts are metric-compatible (**PDF fonts**: a warning that names
`fonts-crosextra-carlito fonts-crosextra-caladea` when Calibri, Aptos or Cambria would be replaced
by a wider font; it cannot be fixed automatically), and whether web search is on. **Fix automatically** repairs what is safe and can be
repeated: it creates a default English language when there is none, copies an existing template to
languages that lack one, and creates the missing AI custom fields (`CWE`, `CVSS Score`, `Severity`,
`Vulnerable URL`). It never deletes anything and never creates an audit type or template for you.

### How findings are rewritten

- **Model fallback chain.** The main model is tried first, then each fallback in order, when a model
  is filtered, rate-limited, unavailable or returns something unusable. Set the fallbacks in AI
  Settings (one per line) or `llm_fallback_models`. Nothing is added by default. A suggested order
  for an Anthropic-style provider is `claude-sonnet-5`, `claude-opus-4-8`, `claude-opus-4-7`,
  `claude-opus-4-6`, `claude-opus-4-5`, `claude-haiku-5-5`. The model that answered is stored with
  the proposal, shown on any finding not answered by the primary model, and kept in History. If
  every model fails, the finding fails with all the reasons.
- **Masking** (on by default, `mask_sensitive`). The model sees realistic stand-ins, not the
  client's values, so it can still follow the pattern of a finding:
  - client and company names and the audit name become a fictional company of the same shape
    (`Brandarmy Inc` becomes something like `Lumenfold Inc`; legal suffix, word count and casing
    kept);
  - domains and host names keep their sub-domain labels and get a fictional registrable domain
    under a reserved zone (`app.brandarmy.com` becomes something like `app.lumenfold.example`); all
    hosts of one real domain share one fictional domain;
  - IPv4 addresses keep their class and `/24` topology (private stays private, public addresses
    move to non-routable test ranges such as `198.18.0.0/15`), CIDR masks and ranges are kept, and
    IPv6 addresses move to `2001:db8::/32`; loopback, `0.0.0.0`, link-local, multicast and version
    strings are left alone;
  - e-mail addresses keep a role local part (`security@`, `admin@`) and get the fictional domain.

  The stand-ins are chosen per job with a random key (the same value gets the same stand-in in
  every finding of that job, another job gets different ones) and live in memory only. The answer
  is converted back to the real values before anything is saved; an answer that still contains a
  stand-in, or an address from the test ranges, is rejected instead of written. The same applies
  to research and library text.

  **Sent as they are, on purpose:** URL schemes, ports, paths, API routes, query parameter names,
  request bodies, headers, cookies, tokens and PoC payloads (SQLi/XSS strings, commands). Only the
  client-identifying part of a URL (its host) changes. A password, API key or session token inside
  a payload is **not** masked unless it equals a client term; use `redact_field_labels` or a local
  model for such evidence. See the limits under [Security notes](#security-notes).
- **CVSS from the vector.** The model only returns a `CVSS:3.1/...` base vector. The gateway
  validates it, computes the score and severity itself, stores the canonical vector in the finding's
  CVSS field, and fills the custom fields named exactly like the base score (`CVSS Score`,
  `CVSS v3.1 Base Score`, `Base Score`) or the severity (`Severity`, `CVSS Severity`, `Risk
  Rating`) when they are input, select or radio fields (a select field only when the computed word
  is one of its options). Temporal or environmental scores, `Severity justification` and a bare
  `CVSS` field are left alone. In *fill missing* mode a vector the tester already set is never
  replaced and is used to compute the score. Without any valid vector (from the model or on the
  finding) the score and severity fields are not written.
- **Citations.** Each proposal lists its research sources (web hosts first, then the names of local
  documents that were used) as small text under the finding.
- **Scanned PDFs.** A PDF with almost no extractable text (under 80 non-blank characters per page on
  average) is flagged "little text found" in AI Settings. If `pdftoppm` (poppler-utils) and
  `tesseract` are installed, OCR runs on it when it is indexed (at most 400 pages and 10 minutes) and replaces
  the extracted text when it finds more. Without them the flag is the only effect.
- **Research cache.** Web and document research for the same finding title is reused for
  `research_cache_days` (default 14, `0` turns it off), stored under `<data dir>/research/`. The cache
  key includes a fingerprint of the document index, so uploading, replacing or deleting a document
  makes the next run research again.
- **Library reference.** With `use_library` on, the wording of a matching library entry (same title,
  close match, in the audit's language) is shown to the model as untrusted reference. The gateway
  reads it from PwnDoc's per-language library endpoint, because the plain list carries titles only.
  The command line `refine` does the same when the source is an audit.

### Webhook

Set a Slack, Teams or generic incoming-webhook URL and the gateway sends a short message
(`{"text": "..."}`: audit name, who ran it, status, findings applied and failed, whether the report
was built) when a **Finish report** run ends. Update runs do not notify. A failed notification is
logged (host only, never the URL) and never affects the run.

The webhook URL is a secret, handled like the API key: it is stored in `config.json` (`0600`), the
settings API returns only a mask (`********` plus `notify_webhook_url_set`), and AI Settings shows
an empty field with a "URL stored" hint. Leave the field blank to keep the stored URL. Tick "Remove the stored webhook" and save to delete it. The URL must
not point at a link-local or cloud-metadata address (any spelling, including decimal, hex or octal
IPv4 forms and IPv4-mapped IPv6; host names are resolved and refused if any address is blocked). It
is checked when you save it and again each time a message is sent. Loopback and ordinary private
addresses are allowed.

### Findings order and normalisation

Reports list findings Critical -> High -> Medium -> Low -> Informational, highest CVSS 3.1 score
first, numbered 001.. (contract: `docs/FINDING_ORDER.md`). In the Finish drawer, **Check findings
order** shows the order, the severity derived from the score (a different stored label is shown
as a mismatch), estimated scores and the verification. **Fill empty fields with AI** asks the
model only for the empty fields (reviewed in Update with AI); **Apply normalisation** stores CVSS
vector and score, severity, CWE/CVE or `N/A`, `N/A` fills and the order, after an automatic
backup (Restore undoes it). The Settings switch **Normalise findings before download** (default
on) does the same before a Word/PDF download; the response carries `X-Findings-Verified` and
`X-Findings-Normalised`. PwnDoc cannot renumber stored identifiers; outputs use the new numbers.

## Interactive CLI (JSON in, JSON out, upload)

The same engine is available without the web UI:

```bash
pwndoc-ai refine
```

The wizard asks, step by step, where the findings come from (**a PwnDoc audit** you pick from a
numbered list, or **an existing JSON file**), where to write the polished JSON, which fields and mode
to use, and finally whether (and to which audit) to upload the result. Runs are resumable: findings
already present in the output file are skipped. Input files in UTF-8, UTF-16 or cp1252 are accepted;
output is UTF-8 with private (`0600`) permissions.

Every prompt can also be given as a flag, so a run can be fully non-interactive:

```bash
pwndoc-ai refine --source file --input findings.json --output findings_refined.json \
  --fields description,remediation --mode fill_missing --no-web --no-rag \
  --upload --target-audit <audit-id> --username alice -y
```

| Command | Purpose |
|---|---|
| `pwndoc-ai serve [--host] [--port] [--https] [--insecure-http]` | Run the LAN gateway |
| `pwndoc-ai refine [...]` | The wizard above (`--source {audit,file}`, `--audit`, `--input`, `--output`, `--fields`, `--mode {improve,fill_missing}`, `--web/--no-web`, `--rag/--no-rag`, `--upload/--no-upload`, `--target-audit`, `--username`, `--totp`, `-y`) |
| `pwndoc-ai upload --file F [--audit ID]` | Upload a refined JSON file; findings whose `_id` is not in the audit are skipped and reported |
| `pwndoc-ai config [--set KEY=VALUE]` | Show or change the settings shared with the web UI (`--set llm_api_key=-` prompts for the key so it stays out of shell history) |
| `pwndoc-ai export --audit ID --out report.docx` | Download an audit's DOCX report |

Global flags: `-v/--verbose`, `--data-dir`. The PwnDoc password is read from `PWNDOC_PASSWORD` or a
prompt, never from a flag. Exit codes: `0` ok, `1` error, `2` input closed, `3` some findings failed
or an upload was partial, `130` interrupted. When the source is an audit, the pulled findings are
saved to `<data dir>/exports/audit_<id>_findings.json` (they are your untouched originals), and a
`tokens_used_<timestamp>.json` usage report is written next to the output file. The CLI upload path
does not create a backup.

## Configuration

Settings are edited in **⚙ AI Settings** and saved to `<data dir>/config.json` (mode `0600`). A
value supplied through an environment variable always wins, is shown as the effective value, and is
never written to disk.

| Setting | Env var | Default | Notes |
|---|---|---|---|
| `pwndoc_url` | `PWNDOC_URL` | `https://localhost:8443` | PwnDoc front end the gateway proxies to. Not editable from the browser |
| `verify_tls` | `PWNDOC_VERIFY_TLS` | `true` | Verify PwnDoc's certificate. A loopback upstream (`localhost`, `127.0.0.1`) is always accepted; for a remote host with the stock self-signed certificate set `false`. Not editable from the browser |
| `llm_base_url` | `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible API. Changing it from the UI clears the stored key so it cannot be sent to the new host |
| `llm_api_key` | `LLM_API_KEY` | _empty_ | Never returned to the browser |
| `llm_model` | `LLM_MODEL` | `gpt-4o-mini` | |
| `temperature` | | `0.2` | 0 to 2 |
| `max_output_tokens` | | `16384` | 256 to 1,000,000; a model's own maximum always applies and the gateway lowers a too-large value to it automatically |
| `use_web_search` | | `false` | DuckDuckGo lookup for CWE/OWASP context. Off by default: finding titles would be sent to a search engine |
| `web_results` | | `3` | Pages fetched per finding (0 to 10) |
| `use_rag` | | `false` | Use your uploaded local documents |
| `embed_backend` | `PWNDOC_AI_EMBEDDER` | `auto` | Which embedder turns text into vectors: `auto` (Ollama when it is installed or reachable, else the built-in ONNX model), `ollama` or `onnx`. Switching never re-indexes your documents, see [Knowledge base](#knowledge-base-and-embeddings) |
| `ollama_model` | `PWNDOC_AI_OLLAMA_MODEL` | `all-minilm` | Embedding model. `all-minilm` gives the same vectors as the built-in model |
| `ollama_idle_seconds` | `PWNDOC_AI_OLLAMA_IDLE` | `300` | Seconds without an embedding before the gateway stops its own Ollama (30 to 3600) |
| `ollama_url` | `PWNDOC_AI_OLLAMA_URL` | _empty_ | Empty: the gateway starts and stops its own Ollama on a free loopback port. A URL: use that server as it is and never stop it (the SSRF guard applies; loopback is allowed) |
| `redact_field_labels` | | _empty_ | Custom-field labels left out of the prompt and never modified |
| `extra_instructions` | | _empty_ | House style, language or tone appended to the prompt |
| `llm_fallback_models` | | _empty_ | Models tried in order after `llm_model` fails. One per line in the UI |
| `mask_sensitive` | | `true` | Show the model fictional stand-ins for client names, domains, IPs and e-mails (endpoints and payloads are sent as they are) and restore the real values in its answer |
| `use_library` | | `true` | Show matching vulnerability-library wording to the model as reference |
| `research_cache_days` | | `14` | Reuse research per finding title for this many days (0 to 365; 0 = off) |
| `notify_webhook_url` | | _empty_ | Incoming webhook told when a Finish report run ends. A secret: masked in the UI and the API |
| `languagetool_url` | | _empty_ | LanguageTool server for spelling notes in the report check |
| data directory | `PWNDOC_AI_DATA_DIR` | `data` (relative to the working directory) | `config.json`, `backups/`, `exports/`, `rag/`, `research/`, `ai-log.jsonl`, `tls/` |

`pwndoc_url` and `verify_tls` are locked in the UI on purpose: anyone able to change them could
redirect every user's session cookie. Change them with `pwndoc-ai config --set pwndoc_url=...` or
the environment.

### Provider examples

| Provider | Base URL | Model example |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | any model slug from the OpenRouter catalogue |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1` (API key can be any non-empty text) |

The engine asks for structured output through function calling. If a provider rejects tools it
retries once without them and parses a JSON reply instead; newer models that require
`max_completion_tokens` are retried accordingly.

### Context sources

- **Internet search** (off by default): DuckDuckGo results are fetched and reduced to extracted page
  text. Only public hosts are contacted; private, loopback and link-local addresses are refused,
  connections are pinned to the validated address, and responses are capped at about 1.5 MB. The
  pages are split into passages that are **embedded and ranked against the finding**, so the model
  gets the passages that are about the finding instead of the first few thousand characters of
  every page. This goes through the same embedder as your documents (next section); without Ollama
  the built-in ONNX model (same vectors) does the ranking, and it also takes over for a finding
  whose Ollama fails or is too slow. A page's passages are cached once and
  reused whatever the other results of a search are. The text is labelled with the host it
  really came from (after redirects). Only if no embedder of any kind works is the old truncation
  (about 5,600 characters per page) used instead and the run marked as degraded; the
  document search and the web ranking share one deadline and one cool-down, so a broken embedder
  costs one timeout, not one per finding. A report run never fails because of embedding.
- **Local documents (RAG)**: your own books, standards and notes, see
  [Knowledge base and embeddings](#knowledge-base-and-embeddings). Bring your own material; none is
  shipped.

The combined context is capped at 12,000 characters per finding and is passed to the model as
untrusted reference text.

### Knowledge base and embeddings

**One embedding pipeline.** Everything that is embedded, whether internet search results or your
local books, goes through ONE small embedder: `all-minilm` (about 45 MB) served by Ollama in server
mode. The gateway **starts `ollama serve` only when an embedding is actually needed and stops it
after `ollama_idle_seconds` without use** (default 300 s). It listens on a free loopback port of its
own (never 11434), runs with a scrubbed environment (no gateway secrets, Ollama cloud off) and is
stopped with its model runner when it idles or when the gateway exits. Listing, deleting and
skipping files that are already indexed never start it. An Ollama you set in `ollama_url` is used as
it is and never stopped.

**Add files (AI Settings, Knowledge base).** Install the `rag` extra (`pip install 'pwndoc-ai[rag]'`),
enable `use_rag`, then drag PDFs, books and notes onto the drop zone (folders work too), or click it
or press Enter to choose files. PDF, DOCX, Markdown, text and JSON are accepted; other types and files
over the size limit are refused in the browser with a reason. Files upload one request each, two at a
time, with a progress bar per file, and end as **Stored**, **Already stored** (same name, same
content) or **Duplicate** (same content under another name, nothing added). **Uploading never
indexes.** Closing the panel while files are uploading stops those uploads (Retry puts a file back).

**Index once, incrementally.** Press **Index new files** to index what is stored but not yet
indexed (**Re-check all** looks at every stored file). The run happens on the gateway as one
background job with live progress, the current file, a chip per file (Indexed, Already indexed,
Duplicate, Failed) and how many chunks were new versus reused. **Cancel indexing** stops it between
files and between embedding batches. Closing the panel or reloading the page does not stop it:
reopening shows the run where it is. A failing file never stops the run; a broken embedder stops it
with a short message and the list stays as it was. Nothing is embedded twice:

- **File level.** The same content under the same name is skipped without being parsed or embedded.
  The same content under another name is a duplicate: no chunks are added.
- **Chunk level.** A chunk is identified by its source and the SHA-256 of its text. Re-indexing a
  changed file embeds only the chunks that are not already stored for it, and removes the file's
  stale chunks only after the new ones were stored. Chunks indexed by earlier versions stay as they
  are while their file is unchanged.

**Switching embedders never re-embeds.** Chunks are stored per *vector space*, not per embedder.
Ollama `all-minilm` and ChromaDB's built-in ONNX `all-MiniLM-L6-v2` produce identical vectors (the
space `minilm-l6-v2`), so both use the same collection (`documents`): an index built with one is
searched with the other, you can flip `embed_backend` freely, and an existing index of tens of
thousands of chunks is reused as it is. Only a model with a different space (for example
`ollama_model = nomic-embed-text`) gets a collection of its own (`documents-<model>`), and then the
files have to be indexed again once. A model name with a tag or other punctuation
(`nomic-embed-text:v1.5`) also carries a short hash of the exact name in its space, so two models
whose names only differ in punctuation (`bge_m3`, `bge-m3`) never share a collection or cached
vectors. Only models of the default Ollama registry can be used; an Ollama URL outside your local
network is flagged next to the embedder line when masking is off.

**Install Ollama (project-local, no service).** Nothing is installed system-wide and no service is
created. Unpack the Linux release archive from <https://ollama.com/download> so that the binary is
`<project>/.ollama/bin/ollama` (the gateway looks there first, then for `ollama` on the `PATH`;
`PWNDOC_AI_OLLAMA` names another binary). The model files live in `<project>/.ollama/models`. Then
open AI Settings and press **Prepare embedder**: it starts the server on a private port, downloads
`all-minilm` once (the one time that needs internet) and stops again. The embedder line above the
drop zone shows what is going on: backend and model, "starts only when something needs embedding and
stops itself when idle", and chips for Installed, Running or Stopped, and Model ready or missing.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PWNDOC_AI_EMBEDDER` | `auto` | `auto`, `ollama` or `onnx` (wins over `embed_backend`) |
| `PWNDOC_AI_OLLAMA` | `<project>/.ollama/bin/ollama`, then `ollama` on the `PATH` | the binary to start |
| `PWNDOC_AI_OLLAMA_URL` | _empty_ | an external server to use instead of starting our own (never stopped) |
| `PWNDOC_AI_OLLAMA_MODEL` | `all-minilm` | embedding model |
| `PWNDOC_AI_OLLAMA_IDLE` | `300` | idle seconds before our server is stopped (30 to 3600) |

**When Ollama is not there.** `auto` falls back to the built-in ONNX embedder (no PyTorch): same
vectors, so indexing and search keep working and nothing has to be re-indexed; the embedder line says
"built-in model". If the embedder fails at the moment it is needed (Ollama forced but not installed,
model missing, server will not start), indexing stops with a short message and keeps what was
stored; a report run still finishes: the web text is still ranked by the built-in embedder (unranked
and marked degraded only if that fails too), without local documents for that finding.

**Resources.** The embedder uses no process and no memory except while indexing, or while a report
run ranks web passages or searches the documents. One `all-minilm` process takes a few hundred MB of
RAM and runs on the CPU. Indexing tens of thousands of chunks from scratch takes hours on a small
host, which is why the incremental rules above matter: you do it once, and later runs only embed
what is new. Scanned PDFs are OCR'd during indexing when `pdftoppm` and `tesseract` are installed;
the OCR tools run under memory, file-size and CPU limits and their temporary folder is watched
while they run.

**Limits while reading a file.** A `.txt` or `.md` file may be 25 MB, a `.json` file 10 MB, a PDF
5,000 pages, a `.docx` body 40 MB of XML, and the text kept of any document is capped at roughly what 25,000 chunks hold; the
check happens before the whole file is held in memory. A file that cannot be indexed (no readable
text, too large, a duplicate of another file) is remembered with the hash of its bytes: the list
shows why (**Duplicate**, **Not indexed** with the reason) instead of "Pending", it is not read or
OCR'd again until its bytes change, and **Re-check all** tries every file again.

## Security notes

- Serve over **HTTPS** (`pwndoc-ai serve --https`, needs the `https` extra) or run only on a network
  you trust. Over plain HTTP, PwnDoc passwords and session cookies cross the LAN unencrypted, and the
  gateway has to relax the cookie `Secure` flag in that mode. Behind your own TLS terminator, set
  `FORWARDED_ALLOW_IPS` so the gateway knows the browser is on HTTPS.
- "Administrator" means a PwnDoc user with the `admin` role. AI Settings, model listing and document
  upload are administrator-only, enforced server-side. Any signed-in PwnDoc user can run AI jobs on
  the shared LLM key, so a rogue account can spend your budget. `GET /__ai/api/status` is
  unauthenticated and only reveals the version and whether an LLM is configured.
- **Refine a DOCX** keeps uploaded reports and their refined copies on the gateway host until the
  user deletes them: they are client data, so keep the gateway's storage on an encrypted,
  access-controlled volume. Files are visible only to the user who uploaded them, every route needs
  the `X-Requested-With` header, and the per-user and global quotas bound the disk use. The
  document's text is sent to the configured LLM provider (client names you list are swapped for
  fictional stand-ins first, like domains, IPs and e-mails, with the masking limits below) and, in deep mode, short queries built
  from section headings go to web search and the local document index. The DOCX is untrusted: the
  gateway refuses macros, zip bombs, XML entity tricks and unsafe archive paths, tells the model that
  the document is data and never instructions, and applies model output only to the paragraphs it
  sent, only as plain text.
- Finding text is sent to the configured LLM provider. Use `redact_field_labels` for sensitive
  custom fields (they are omitted from the prompt; title, description, observation, PoC, scope,
  remediation and references are always sent), or a local model (Ollama) for confidential work.
- Masking reduces what the provider learns but is not anonymisation. It matches the client and
  company names of the audit, the client's domains and host names, IPs and e-mails, and swaps them
  for fictional stand-ins. It does not catch a name split by formatting (`Ac<b>me</b>`) or other
  people's names. Endpoints, paths, query parameters, headers, cookies, tokens and payloads are
  sent as they are: **a secret inside a payload is not masked unless it equals a client term**.
  Public reference hosts (OWASP, MITRE, NVD) and hosts that are not the client's are left as
  written. Text inside `alt` and `title` attributes is masked like any other text. A term is
  masked everywhere it occurs, so a short name such as "Mark" also masks the word "mark". Before
  web search the client values are removed from the finding title (not replaced by stand-ins); a
  title that is nothing but client data is not searched. Names written as HTML entities
  (`Johnson &amp; Johnson`) are masked. If the model's answer still contains a stand-in after
  restoring, or an address from the fictional test ranges, that finding fails instead of being
  written. The command line `refine` masks too (client names only when the source is an audit).
- The webhook URL, the LanguageTool URL and the model fallback list are administrator-only
  settings. The webhook URL is a secret (masked in the settings API). Both URLs refuse link-local
  and cloud-metadata addresses in every spelling, resolve host names before deciding, and are
  checked again when they are used, not only when saved. The spelling check sends
  finding text (not the PoC) to the LanguageTool server you name **without masking**, so run it on
  your own network; the Health section warns when it looks like a public server.
- **Fix automatically** (Health) replaces the whole audit-type list in PwnDoc, because PwnDoc has
  no per-type call. The current list is saved to `<data dir>/backups/_setup/` first, re-read
  afterwards and put back if it does not match; only one fix runs at a time.
- Restoring a backup writes client data back into PwnDoc as the signed-in user and can be undone
  from the safety backup it creates. Backups and the run log live in the data directory.
- Review the preview before applying. Model output is a draft, and web or RAG content can contain
  prompt-injection attempts that the sanitiser and the preview step reduce but cannot eliminate.
- Backups, exports and the settings file are created with private permissions, but they contain
  client data: keep the data directory on an encrypted, access-controlled volume.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and limitations.

## Development

```bash
pip install -e '.[dev,rag,docx,https,ui]'
pytest --cov=pwndoc_ai
ruff check . && ruff format --check .
```

The browser tests (`tests/e2e`, plus `python tests/ui/smoke_ui.py`) need Playwright and a Chromium
binary; they use `/usr/bin/chromium` unless `CHROMIUM` points elsewhere, and are skipped when it is
missing, unless `PWNDOC_AI_REQUIRE_BROWSER=1` is set (use that in CI), which turns a missing
browser into a failure. `tests/e2e/support.py` holds the fake PwnDoc and LLM, the fixtures and the
page helpers; the tests are in `test_http_security_e2e.py`, `test_update_flow_e2e.py`,
`test_race_guards_e2e.py` and `test_report_tools_e2e.py`. The Knowledge base tests (`test_kb_*.py`)
run the real UI against `fake_gateway.py`, a fake of the document, indexing and embedder routes
whose index run advances only when the test says so (`test_kb_files_node.py` needs only `node`;
`test_kb_real_gateway_e2e.py` uses the real gateway and indexes one tiny file with the built-in
model when it is cached). The Refine a DOCX tests (`test_docx_upload_e2e.py`,
`test_docx_run_e2e.py`, `test_docx_review_e2e.py`) run the real UI against the same fake gateway
with `fake_docx.py`, an in-memory fake of the `/docx` routes whose runs advance only when the test
says so; `test_docx_util_node.py` needs only `node`. Every test starts its own gateway and data directory. The front end
is plain ES modules (`src/pwndoc_ai/static/ai-*.js`, loaded by `inject.js`); check a change with
`node --check` on each file.

## Credits

Built around [PwnDoc](https://github.com/pwndoc/pwndoc) (AGPL-3.0, separate project; not bundled
here) and its [REST API](https://pwndoc.github.io/pwndoc/api/). Check upstream for its own
AI-related features before deploying this alongside it.

## License

[MIT](LICENSE)
