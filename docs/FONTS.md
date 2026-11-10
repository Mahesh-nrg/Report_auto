# Fonts: catalog, installer and the PDF renderer

This document covers the curated font catalog (`src/pwndoc_ai/font_catalog.py`), the installer
(`scripts/install_fonts.sh`), how the LibreOffice renderer resolves fonts, and the admin health
check. Shapes and JSON keys are defined in `font_catalog_model.py` and docs/EXPORT_DESIGN.md (f).

## What is installed where

| Layer | What | Where |
|---|---|---|
| apt (root) | Carlito, Caladea, Liberation, Croscore (Arimo/Tinos/Cousine), DejaVu, Lato, Open Sans, Roboto, Inter, Noto Sans/Serif, Source Sans 3, Ubuntu, IBM Plex, EB Garamond, Fira Code, JetBrains Mono, Inconsolata, URW base35 (P052, URW Gothic) | system font directories |
| pinned downloads | Lexend (9 static TTFs, googlefonts/lexend@7894f02b), Lexend Deca and Gelasio (variable TTFs, google/fonts@e7db6ca6), Selawik (optional, `--with-selawik`) | `${PWNDOC_AI_FONTS_DIR:-<repo>/data/fonts}`, or `/usr/local/share/fonts/pwndoc-ai` with `--system` |

Every download is checked against a sha256 written in the script. A mismatch deletes the file and
the script exits with status 3. The OFL licence text is installed next to each family.

Proprietary fonts (Calibri, Aptos, Cambria, Arial, Times New Roman, Segoe UI, Georgia, Consolas
and so on) are never installed. They resolve to an open stand-in, and the UI shows
"substituted by X". Stand-ins marked *metric* keep line and page breaks identical:

| Requested | Stand-ins, first installed wins | Metric twin |
|---|---|---|
| Calibri, Calibri Light | Carlito, Liberation Sans | Carlito |
| Aptos (Display, Narrow) | Carlito, Liberation Sans | none (close match) |
| Cambria | Caladea, Liberation Serif | Caladea |
| Arial, Helvetica | Liberation Sans, Arimo | both |
| Times New Roman | Liberation Serif, Tinos | both |
| Courier New | Liberation Mono, Cousine | both |
| Segoe UI | Selawik, Open Sans, Noto Sans, Liberation Sans | Selawik |
| Georgia | Gelasio, Noto Serif, Liberation Serif | Gelasio |
| Consolas | Inconsolata, Liberation Mono | none |
| Palatino Linotype, Book Antiqua | P052, Liberation Serif | P052 |
| Century Gothic | URW Gothic, Liberation Sans | none |
| Garamond | EB Garamond, Liberation Serif | none |
| Trebuchet MS | Open Sans, Liberation Sans | none |
| Verdana, Tahoma | Liberation Sans (renderer) | none |

Verdana and Tahoma: the catalog lists DejaVu Sans as their closest design, but the renderer never
names DejaVu in an alias rule. It pushes Liberation in front of DejaVu on purpose; see
`pdf_fonts.py`.

## Installing

```sh
sudo scripts/install_fonts.sh --dry-run   # print what would happen, change nothing
sudo scripts/install_fonts.sh --yes       # apt packages + downloads + fc-cache
scripts/install_fonts.sh --no-apt         # without root: downloads only, into data/fonts
```

The options are `--system`, `--dry-run`, `--no-apt`, `--with-selawik` and `--yes`.
- apt runs only as root, and only with `--yes` or after an interactive confirmation.
- Packages unknown to apt are skipped with a warning.
- Running the script twice changes nothing.
- Afterwards, restart the gateway. The installed font set is cached per process
  (`pdf_fonts.installed_families` and `font_catalog.font_catalog`, TTL 300 s); calling
  `font_catalog.clear_caches()` together with `pdf_fonts.installed_families.cache_clear()` also
  works.

## How the renderer resolves a font

For each conversion, `pdf_fonts.write_fontconfig(work)` writes a private `fonts.conf`:
1. It includes `/etc/fonts/fonts.conf`.
2. It adds `<dir>` for the project fonts directory (`$PWNDOC_AI_FONTS_DIR`, or
   `$PWNDOC_AI_DATA_DIR/fonts`, default `data/fonts`) when that directory exists, so LibreOffice
   and fc-match see the downloaded fonts without root.
3. It writes one alias rule per requested family. Each rule names only the candidates that are
   actually installed.
4. It adds the rule that puts Liberation in front of DejaVu.

`font_catalog.font_catalog(data_dir)` asks fontconfig under that same configuration (fc-list,
then fc-match per family). It has a 15 s deadline and a TTL cache. `GET
/__ai/api/docx/export/fonts` returns the result: per family `installed`, `substituted` (with
`resolved` and `layout_safe`) or `missing`. It never contains host paths or install commands.
Family names reach fc-match only after `valid_family`, with a fixed argv and no shell.

## Health check

`FONTS_CATALOG` (admin `/health`) is WARN when open catalog families are not installed and
fontconfig replaces them. It lists them together with `sudo scripts/install_fonts.sh`. A family
fontconfig knows nothing about is not reported. The older `PDF_FONTS` check (Calibri, Aptos and
Cambria twins) is unchanged.

## Licences

- The downloaded and apt fonts are under SIL OFL 1.1, Apache 2.0, the Ubuntu Font Licence or the
  Bitstream Vera licence. Each catalog row carries its licence.
- PDF import: the optional `pdf2docx` engine depends on **PyMuPDF, which is AGPL-3.0** (or an
  Artifex commercial licence). It is an optional extra, runs in a separate worker process, and
  the default `structure` engine does not need it. Check that its licence fits your distribution
  before you enable it. This is not legal advice.

## Installed on the reference host (2026-10-26)

`sudo scripts/install_fonts.sh --system --yes` (Kali 2026.3, fontconfig 2.17.1, LibreOffice
26.8.0.3) installed 11 apt packages (fonts-croscore, fonts-open-sans, fonts-roboto-unhinted,
fonts-inter, fonts-noto-core, fonts-adobe-sourcesans3, fonts-ubuntu, fonts-ibm-plex,
fonts-ebgaramond, fonts-jetbrains-mono, fonts-inconsolata) and downloaded 14 files (Lexend x9,
Lexend Deca, Gelasio and their OFL texts, all sha256-verified) into `/usr/local/share/fonts/pwndoc-ai`;
`--system` was used so that nothing was written under `data/`. A second run reported "already
present 14" and installed nothing.

All 40 catalog families render through the repo's isolated renderer config (checked with the
embedded fonts of the PDF, `tests/test_export_integration.py`): 24 open families are *installed*
and embed as themselves, the 16 proprietary names embed as their stand-in (Calibri/Calibri
Light/Aptos -> Carlito, Cambria -> Caladea, Arial/Verdana/Tahoma -> Liberation Sans, Times New
Roman -> Liberation Serif, Courier New -> Liberation Mono, Segoe UI/Trebuchet MS -> Open Sans,
Georgia -> Gelasio, Consolas -> Inconsolata, Palatino Linotype -> P052, Century Gothic -> URW
Gothic, Garamond -> EB Garamond). The system `fc-match` (outside the renderer) answers Arial ->
Arimo and Times New Roman -> Tinos (also metric twins) and Segoe UI -> Cantarell; only the
renderer's own fontconfig matters for output.

Two glyph-level fallbacks remain visible in PDFs and are expected: U+2011 (non-breaking hyphen)
is not in Carlito and is drawn from FreeSerif or Noto Serif, and list bullets whose numbering
definition names Symbol may be drawn from DejaVu Sans. Numbering definitions are not retyped.

## Optional PDF engine licence

`pip install 'pwndoc-ai[pdfimport]'` installs pdf2docx 0.5.13 (MIT) and PyMuPDF 1.28.2, which is
**dual licensed AGPL-3.0 or Artifex commercial**, plus opencv-python-headless (Apache-2.0),
fonttools and others. Running PyMuPDF behind a network service you offer to others can trigger
the AGPL source-offer obligations; keep the extra off unless that is acceptable. The structure
engine (mutool, pdftotext, pypdf) needs no extra and is the default of *auto*.
