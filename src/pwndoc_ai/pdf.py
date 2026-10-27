"""Word -> PDF with headless LibreOffice (``soffice``).

LibreOffice is only needed for the PDF download. It runs as a child process with its own
throw-away profile and temp directory per conversion, a hard timeout (the whole process group
is killed), a scrubbed environment (no gateway secrets), and at most ``MAX_PARALLEL`` runs at once.
Before LibreOffice sees a document it is checked: it must be a zip of sane size, every
relationship part is parsed as XML (no DOCTYPE or entity declarations, UTF-8 only) and may not link
to external resources (images, templates, OLE objects) except plain http, https or mailto
hyperlinks, and no XML part (wherever it sits in the package) may hold an INCLUDEPICTURE,
INCLUDETEXT, LINK or DDE field or a VML ``src``/``href`` that names a URL. Attributes that a
lenient reader could resolve differently (the same name twice, or in another namespace) are
refused. When the host
allows it ``unshare -rn`` additionally takes the network away from LibreOffice, so even a
document that slips through cannot make the host fetch a URL.

``convert_to_pdf_report`` adds the layout work around the conversion: the document is tidied
first (``docx_layout.tidy_docx``, when that package is importable), rendered with a generated
fontconfig file that maps Calibri, Aptos and Cambria to metric-compatible fonts (``pdf_fonts``),
the table-of-contents page numbers are corrected with one more render when they are wrong
(``pdf_toc``), and pages that hold nothing but the running header and footer are removed from the
result (``pdf_pages``). At most two renders share one slot and one overall deadline.
"""

from __future__ import annotations

import functools
import io
import logging
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import pdf_fonts, pdf_pages, pdf_toc

if TYPE_CHECKING:
    from .docx_layout import LayoutIssue
    from .pdf_fonts import FontIssue

log = logging.getLogger(__name__)

SOFFICE_ENV = "PWNDOC_AI_SOFFICE"
CANDIDATES = ("soffice", "libreoffice")
CONVERT_TIMEOUT = 180.0
MAX_DOCX_BYTES = 200 * 1024 * 1024
MAX_UNCOMPRESSED = 500 * 1024 * 1024  # decompression-bomb guard on the zip's real size
MAX_RELS_BYTES = 5 * 1024 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024  # one word/*.xml part that is scanned for field codes
MAX_IMAGE_PIXELS = 80_000_000  # one embedded picture (a 9000 x 9000 scan)
MAX_TOTAL_PIXELS = 400_000_000  # all pictures of a document together (about 0.5 GB decoded)
IMAGE_HEAD_BYTES = 256 * 1024  # how much of a JPEG is searched for its size (EXIF can be large)
SOFFICE_MEMORY_MB = 3072  # address-space limit of the converter; 0 switches it off
MEMORY_ENV = "PWNDOC_AI_SOFFICE_MEMORY_MB"
CPU_SLACK = 60.0  # CPU seconds allowed beyond the wall-clock timeout (LibreOffice is threaded)
UNSHARE_PROBE_TIMEOUT = 10.0
SLOT_WAIT = 5.0  # how long a request waits for a free conversion slot before a 503
REAP_WAIT = 5.0
HYPERLINK_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink",
    }
)
LINK_SCHEMES = frozenset({"http", "https", "mailto"})
SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f]+")
DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
ENCODING_DECL = re.compile(rb"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
BLOCKED_FIELDS = frozenset({"INCLUDEPICTURE", "INCLUDETEXT", "LINK", "DDE", "DDEAUTO", "IMPORT"})
LEADING_DOTS = re.compile(r"^(?:\./|/)+")
FIELD_NAME = re.compile(r"\s*([A-Za-z]+)")
URL_ATTRIBUTES = frozenset({"src", "href"})
XML_SNIFF = 16
MAX_PARALLEL = 2
STDERR_TAIL = 400
SECOND_RENDER_MIN = 5.0  # seconds that must be left for the TOC re-render
SECOND_RENDER_FACTOR = 1.5  # ... and at least this many times as long as the first render took
FIRST_RENDER_RESERVE = 0.8  # share of the deadline the font lookups may never eat into
UNAVAILABLE = (
    "PDF export needs LibreOffice on the gateway host: "
    "sudo apt install libreoffice-writer-nogui (or set PWNDOC_AI_SOFFICE to its soffice path)"
)

_slots = threading.BoundedSemaphore(MAX_PARALLEL)


class PdfUnavailable(RuntimeError):
    """LibreOffice is not installed (maps to HTTP 501)."""


class PdfBusy(RuntimeError):
    """All conversion slots stayed busy for ``SLOT_WAIT`` seconds (maps to HTTP 503)."""


class PdfError(RuntimeError):
    """Conversion failed (maps to HTTP 502)."""


def find_soffice() -> str | None:
    configured = os.environ.get(SOFFICE_ENV, "").strip()
    if configured and os.path.isfile(configured) and os.access(configured, os.X_OK):
        return configured
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


# -- document check -----------------------------------------------------------------------------
REFUSED_EXTERNAL = "the report links to external resources, not converted"
REFUSED_FIELD = "the report contains a field that pulls in external content, not converted"
REFUSED_INVALID = "the report is not a valid Word document"
REFUSED_LARGE = "the report is too large to convert to PDF"
REFUSED_IMAGES = "the report has images that are too large to convert to PDF"


def _local(name: str) -> str:
    return name.rpartition("}")[2].lower()


def _attributes(element: ET.Element, *, plain_only: bool) -> dict[str, str]:
    """Attributes by lower-cased local name; refuses anything LibreOffice could read differently.

    LibreOffice looks attributes up by exact name, a naive dict would let ``x:Type`` shadow
    ``Type``. So a repeated local name is refused, and with ``plain_only`` so is any namespaced one.
    """
    found: dict[str, str] = {}
    for key, value in element.attrib.items():
        name = _local(key)
        if name in found or (plain_only and key.startswith("{")):
            raise PdfError(REFUSED_INVALID)
        found[name] = value
    return found


def _part_name(entry: zipfile.ZipInfo) -> str:
    """Lower-case name with forward slashes: how a lenient reader would look the part up."""
    return LEADING_DOTS.sub("", entry.filename.replace("\\", "/")).lower()


def _read_part(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
    if entry.file_size > limit:
        raise PdfError(REFUSED_LARGE)
    with archive.open(entry) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PdfError(REFUSED_LARGE)
    return data


def _parse_xml(data: bytes) -> str:
    """The part's text, after proving it is plain UTF-8 XML with no DOCTYPE/ENTITY declaration."""
    declared = ENCODING_DECL.match(data[:200])
    if declared and declared.group(1).strip().lower() not in (b"utf-8", b"utf8"):
        raise PdfError(REFUSED_INVALID)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PdfError(REFUSED_INVALID) from None
    if "\x00" in text or DECLARATION.search(data):
        raise PdfError(REFUSED_INVALID)
    return text


def _is_external_target(target: str, mode: str) -> bool:
    if mode not in ("", "internal"):
        return True
    cleaned = CONTROL_CHARS.sub("", target)
    return bool(SCHEME.match(cleaned)) or cleaned.startswith(("\\\\", "//"))


def _check_relationship(attributes: dict[str, str]) -> None:
    target = attributes.get("target", "")
    if not _is_external_target(target, attributes.get("targetmode", "").strip().lower()):
        return
    scheme = SCHEME.match(CONTROL_CHARS.sub("", target))
    allowed = (
        attributes.get("type", "").strip() in HYPERLINK_TYPES
        and scheme is not None
        and scheme.group(1).lower() in LINK_SCHEMES
    )
    if not allowed:
        raise PdfError(REFUSED_EXTERNAL)


def _check_rels(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> None:
    text = _parse_xml(_read_part(archive, entry, MAX_RELS_BYTES))
    try:
        root = ET.fromstring(text)  # noqa: S314 - DOCTYPE/ENTITY refused above
    except ET.ParseError:
        raise PdfError(REFUSED_INVALID) from None
    for element in root.iter():
        if _local(element.tag) == "relationship":
            _check_relationship(_attributes(element, plain_only=True))


def _field_name(instruction: str) -> str:
    match = FIELD_NAME.match(instruction)
    return match.group(1).upper() if match else ""


def _field_char(open_fields: list[list[object]], kind: str) -> str | None:
    """Track begin/separate/end of complex fields; the instruction text when one ends."""
    if kind == "begin":
        open_fields.append(["", True])  # [instruction so far, still in the instruction part]
    elif kind == "separate" and open_fields:
        open_fields[-1][1] = False
    elif kind == "end" and open_fields:
        return str(open_fields.pop()[0])
    return None


def _field_step(open_fields: list[list[object]], event: str, element: ET.Element) -> str | None:
    """A finished field instruction if this parse event completes one, else None."""
    name = _local(element.tag)
    if event == "start" and name == "fldchar":
        return _field_char(
            open_fields, _attributes(element, plain_only=False).get("fldchartype", "")
        )
    if event != "end":
        return None
    if name == "fldsimple":
        return _attributes(element, plain_only=False).get("instr", "")
    if name == "instrtext" and open_fields and open_fields[-1][1]:
        open_fields[-1][0] = str(open_fields[-1][0]) + (element.text or "")
    return None


def _field_instructions(text: str) -> Iterator[str]:
    """Every field instruction in a WordprocessingML part (complex and simple fields)."""
    open_fields: list[list[object]] = []
    for event, element in ET.iterparse(  # noqa: S314 - DOCTYPE/ENTITY refused above
        io.StringIO(text), events=("start", "end")
    ):
        finished = _field_step(open_fields, event, element)
        if finished is not None:
            yield finished
        if event == "end":
            element.clear()  # keep memory flat on large parts
    yield from (str(f[0]) for f in reversed(open_fields))  # unterminated fields still count


def _is_url_reference(value: str) -> bool:
    """A VML ``src``/``href`` that names something outside the package (mailto is harmless)."""
    cleaned = CONTROL_CHARS.sub("", value)
    scheme = SCHEME.match(cleaned)
    if scheme:
        return scheme.group(1).lower() != "mailto"
    return cleaned.startswith(("\\\\", "//"))


def _check_references(text: str) -> None:
    """Refuse a ``src``/``href`` (VML ``imagedata``, ``o:href``, ``r:href``) that names a URL."""
    for _, element in ET.iterparse(io.StringIO(text), events=("start",)):  # noqa: S314
        for key, value in element.attrib.items():
            if _local(key) in URL_ATTRIBUTES and _is_url_reference(value):
                raise PdfError(REFUSED_EXTERNAL)


def _check_fields(text: str, lowered: bytes) -> None:
    try:
        if b"instr" in lowered:
            for instruction in _field_instructions(text):
                if _field_name(instruction) in BLOCKED_FIELDS:
                    raise PdfError(REFUSED_FIELD)
        if b"src" in lowered or b"href" in lowered:
            _check_references(text)
    except ET.ParseError:
        raise PdfError(REFUSED_INVALID) from None


def _looks_like_xml(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bool:
    """Whether a part starts like XML in any encoding (so media and other binaries are skipped)."""
    if entry.file_size == 0:
        return False
    with archive.open(entry) as handle:
        head = handle.read(XML_SNIFF)
    head = head.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
    return head.startswith((b"<", b"\xff\xfe", b"\xfe\xff", b"\x00<", b"\x00\x00\xfe\xff"))


def _check_xml_part(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> None:
    data = _read_part(archive, entry, MAX_PART_BYTES)
    text = _parse_xml(data)
    _check_fields(text, data.lower())


# -- image sizes ----------------------------------------------------------------------------------
PNG_MAGIC, GIF_MAGICS, JPEG_MAGIC = b"\x89PNG\r\n\x1a\n", (b"GIF87a", b"GIF89a"), b"\xff\xd8\xff"
BMP_DIB_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})
JPEG_NO_LENGTH = frozenset({0x01, *range(0xD0, 0xDA)})  # TEM, RSTn, SOI, EOI
JPEG_NOT_FRAME = frozenset({0xC4, 0xC8, 0xCC})  # DHT, JPG, DAC share the SOFn range


def _png_size(head: bytes) -> tuple[int, int] | None:
    if len(head) >= 24 and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])
    return None


def _gif_size(head: bytes) -> tuple[int, int] | None:
    return struct.unpack("<HH", head[6:10]) if len(head) >= 10 else None


def _bmp_size(head: bytes) -> tuple[int, int] | None:
    if len(head) >= 22 and struct.unpack("<I", head[14:18])[0] == 12:
        return struct.unpack("<HH", head[18:22])
    if len(head) >= 26:
        width, height = struct.unpack("<ii", head[18:26])
        return abs(width), abs(height)
    return None


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    """Walk the marker segments up to the first frame header (SOFn), which carries the size."""
    position = 2
    while position + 4 <= len(head):
        if head[position] != 0xFF:
            return None
        marker = head[position + 1]
        if marker == 0xFF:  # fill byte
            position += 1
            continue
        if marker in JPEG_NO_LENGTH:
            position += 2
            continue
        length = struct.unpack(">H", head[position + 2 : position + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in JPEG_NOT_FRAME:
            if position + 9 > len(head):
                return None
            height, width = struct.unpack(">HH", head[position + 5 : position + 9])
            return width, height
        position += 2 + length
    return None


def _image_size(head: bytes, name: str) -> tuple[int, int] | None | bool:
    """(width, height) of a recognised picture, None when it is one but its size cannot be read,
    False when the bytes are not a picture this check knows."""
    if head.startswith(PNG_MAGIC):
        return _png_size(head)
    if head.startswith(GIF_MAGICS):
        return _gif_size(head)
    if head.startswith(JPEG_MAGIC):
        return _jpeg_size(head)
    known_dib = len(head) >= 18 and struct.unpack("<I", head[14:18])[0] in BMP_DIB_SIZES
    if head.startswith(b"BM") and (known_dib or name.endswith(".bmp")):
        return _bmp_size(head)
    return False


def _pixels(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> int:
    """Pixels of the picture in ``entry`` (0 when it is not one). Refuses an unreadable header."""
    if entry.file_size == 0:
        return 0
    with archive.open(entry) as handle:
        head = handle.read(IMAGE_HEAD_BYTES)
    size = _image_size(head, _part_name(entry))
    if size is False:
        return 0
    if size is None:
        raise PdfError(REFUSED_IMAGES)
    width, height = size
    if width * height > MAX_IMAGE_PIXELS:
        raise PdfError(REFUSED_IMAGES)
    return width * height


def _check_archive(archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED:
        raise PdfError(REFUSED_LARGE)
    pixels = 0
    for entry in entries:
        if _part_name(entry).endswith(".rels"):
            _check_rels(archive, entry)
        elif _looks_like_xml(archive, entry):
            _check_xml_part(archive, entry)  # the main part may be named and placed anywhere
        else:
            pixels += _pixels(archive, entry)
            if pixels > MAX_TOTAL_PIXELS:
                raise PdfError(REFUSED_IMAGES)


def check_document(docx: bytes) -> None:
    """Refuse anything that is not a plain, self-contained .docx before LibreOffice parses it."""
    try:
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            _check_archive(archive)
    except zipfile.BadZipFile:
        raise PdfError(REFUSED_INVALID) from None


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Only called while the leader is still unreaped, so its process-group id cannot be reused."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.communicate(timeout=REAP_WAIT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run(command: list[str], work: Path, timeout: float) -> int:
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "LANG": "C.UTF-8"}
    fonts = work / pdf_fonts.FONTS_CONF_NAME
    if fonts.is_file():  # the per-conversion fontconfig file (metric-compatible font aliases)
        environment["FONTCONFIG_FILE"] = str(fonts)
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=work,
            start_new_session=True,  # so a timeout can kill soffice.bin as well
        )
    except OSError as exc:
        raise PdfError(f"could not start the PDF converter: {exc.strerror}") from exc
    try:
        _, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise PdfError("PDF conversion timed out") from None
    except BaseException:  # e.g. the worker thread is interrupted: never leave soffice running
        _kill_group(process)
        raise
    if process.returncode != 0:
        log.warning("soffice exited %s: %s", process.returncode, stderr[-STDERR_TAIL:])
    return process.returncode


@functools.lru_cache(maxsize=1)
def _network_isolation() -> list[str]:
    """Command prefix that runs a program without any network (``unshare -rn``), else ``[]``.

    Probed once: user namespaces may be disabled (containers, hardened kernels). Without the
    prefix the conversion still works, only the extra network cut-off is missing.
    """
    unshare = shutil.which("unshare")
    if not unshare:
        log.warning("PDF export: 'unshare' not found, LibreOffice keeps network access")
        return []
    try:
        probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [unshare, "-rn", "true"],
            capture_output=True,
            timeout=UNSHARE_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("PDF export: network isolation probe failed (%s)", type(exc).__name__)
        return []
    if probe.returncode != 0:
        log.warning("PDF export: network namespaces are unavailable, no network isolation")
        return []
    return [unshare, "-rn"]


def memory_limit_mb() -> int:
    """The converter's address-space limit in MB (``PWNDOC_AI_SOFFICE_MEMORY_MB``; 0 = none)."""
    raw = os.environ.get(MEMORY_ENV, "").strip()
    if raw.isdecimal() and len(raw) <= 7:
        return int(raw)
    return SOFFICE_MEMORY_MB


def _limit_prefix(timeout: float) -> list[str]:
    """``sh -c 'ulimit ...; exec "$@"'``: caps the memory and CPU time of the program that follows
    (inherited by LibreOffice's children). Done in a shell, not ``preexec_fn``, which is unsafe in
    a threaded server. A limit the host refuses is skipped, not fatal."""
    lines = []
    memory = memory_limit_mb()
    if memory > 0:
        lines.append(f"ulimit -v {memory * 1024} 2>/dev/null")
    lines.append(f"ulimit -t {math.ceil(timeout + CPU_SLACK)} 2>/dev/null")
    return ["/bin/sh", "-c", "; ".join([*lines, 'exec "$@"']), "sh"]


def _soffice_command(
    binary: str, work: Path, out: Path, source: Path, timeout: float = CONVERT_TIMEOUT
) -> list[str]:
    return [
        *_network_isolation(),
        *_limit_prefix(timeout),
        binary, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
        f"-env:UserInstallation={(work / 'profile').as_uri()}",
        "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(out), str(source),
    ]  # fmt: skip


def _convert_in(work: Path, docx: bytes, binary: str, timeout: float, round_dir: str = "") -> bytes:
    """One render. ``round_dir`` gives a second render its own input/output folder in ``work``
    (the private profile and the fontconfig file stay shared)."""
    base = work / round_dir if round_dir else work
    source, out = base / "report.docx", base / "out"
    try:
        base.mkdir(exist_ok=True)
        source.write_bytes(docx)
        out.mkdir()
    except OSError as exc:
        log.warning("PDF export: could not prepare the work directory (%s)", exc.strerror)
        raise PdfError("could not prepare the PDF conversion") from exc
    code = _run(_soffice_command(binary, work, out, source, timeout), work, timeout)
    result = out / "report.pdf"
    if code != 0 or not result.is_file():
        raise PdfError("PDF conversion failed")
    data = result.read_bytes()
    if not data.startswith(b"%PDF"):
        raise PdfError("PDF conversion produced no PDF")
    return data


# -- the layout-aware conversion ------------------------------------------------------------------
@dataclass(frozen=True)
class PdfResult:
    """The PDF and what was done to get it. ``pages`` is 0 when the page count is unknown."""

    data: bytes
    pages: int = 0
    removed_blank_pages: tuple[int, ...] = ()
    toc_updated: bool = False
    font_warnings: tuple[FontIssue, ...] = ()
    tidy_fixes: tuple[LayoutIssue, ...] = ()


def _tidy_failed_issue() -> tuple[LayoutIssue, ...]:
    try:
        from .docx_layout import LayoutIssue
    except ImportError:
        return ()
    message = "the layout tidy step failed; the document was rendered as it is"
    return (LayoutIssue(code="tidy_failed", severity="warning", message=message, fixable=False),)


def _tidy(docx: bytes) -> tuple[bytes, tuple[LayoutIssue, ...]]:
    """The tidied document and the fixes made; the input and a note when tidy cannot be used.

    A tidy problem never fails the conversion: only the exception type is logged.
    """
    try:
        from .docx_layout import tidy_docx
    except ImportError:
        return docx, ()
    try:
        result = tidy_docx(docx)
        if result.data is not docx and result.data != docx:
            check_document(result.data)
        return result.data, tuple(result.fixes)
    except Exception as exc:  # noqa: BLE001 - tidy is best effort, the render must still happen
        log.warning("PDF export: layout tidy failed (%s)", type(exc).__name__)
        return docx, _tidy_failed_issue()


def _font_warnings(work: Path, docx: bytes, deadline: float) -> tuple[FontIssue, ...]:
    """Write the generated fontconfig into ``work`` and list the fonts that will be substituted
    (``fc-match`` lookups stop at ``deadline``, a ``time.monotonic()`` value)."""
    try:
        conf = pdf_fonts.write_fontconfig(work)
        return pdf_fonts.font_warnings(docx, matcher=pdf_fonts.fc_matcher(conf, deadline))
    except Exception as exc:  # noqa: BLE001 - fonts are an improvement, never a failure
        log.warning("PDF export: font setup failed (%s)", type(exc).__name__)
        return ()


def _toc_render(
    work: Path,
    docx: bytes,
    first: bytes,
    blanks: tuple[int, ...],
    budget: tuple[str, float, float],
    texts: Sequence[str | None] | None = None,
) -> bytes | None:
    """A second render whose TOC numbers are right, or None when none is needed or possible.

    ``budget`` is (soffice, seconds the first render took, deadline on the monotonic clock);
    ``texts`` are the page texts of ``first`` when the caller has read them already.
    """
    binary, first_seconds, deadline = budget
    entries = pdf_toc.read_toc(docx)
    if not entries:
        return None
    located = pdf_toc.locate_headings(first, entries, texts=texts, deadline=deadline)
    pages = pdf_toc.remap_pages(located, blanks)
    patched = pdf_toc.apply_toc_pages(docx, pages)
    if patched is None:
        return None
    left = deadline - time.monotonic()
    if left < max(SECOND_RENDER_MIN, SECOND_RENDER_FACTOR * first_seconds):
        log.info("PDF export: no time left for the TOC re-render")
        return None
    try:
        check_document(patched)
        return _convert_in(work, patched, binary, left, "second")
    except PdfError as exc:
        log.warning("PDF export: the TOC re-render failed (%s), keeping the first", exc)
        return None


def _drop_blanks(
    first: bytes, second: bytes | None, blanks: tuple[int, ...], deadline: float
) -> tuple[bytes, tuple[int, ...]]:
    """The final PDF without its blank pages. The pages found in the first render are reused
    (the scan is slow): for the second render only when it has the same number of pages."""
    if second is None:
        return pdf_pages.remove_blank_pages(first, blanks=blanks, deadline=deadline)
    if pdf_pages.page_count(second) == pdf_pages.page_count(first):
        return pdf_pages.remove_blank_pages(second, blanks=blanks, deadline=deadline)
    return pdf_pages.remove_blank_pages(second, deadline=deadline)


def _render_report(
    work: Path, docx: bytes, binary: str, timeout: float, fixes: tuple[LayoutIssue, ...]
) -> PdfResult:
    deadline = time.monotonic() + timeout
    warnings = _font_warnings(work, docx, deadline - FIRST_RENDER_RESERVE * timeout)
    started = time.monotonic()
    left = deadline - started
    if left <= 0:
        raise PdfError("PDF conversion timed out")
    first = _convert_in(work, docx, binary, left)
    took = time.monotonic() - started
    texts = pdf_pages.extract_texts(first, deadline)  # read once, shared by the scans below
    blanks = pdf_pages.find_blank_pages(first, deadline=deadline, texts=texts)
    second = _toc_render(work, docx, first, blanks, (binary, took, deadline), texts)
    final, removed = _drop_blanks(first, second, blanks, deadline)
    return PdfResult(
        data=final,
        pages=pdf_pages.page_count(final),
        removed_blank_pages=removed,
        toc_updated=second is not None,
        font_warnings=warnings,
        tidy_fixes=fixes,
    )


def convert_to_pdf_report(
    docx: bytes, *, timeout: float = CONVERT_TIMEOUT, soffice: str | None = None, tidy: bool = True
) -> PdfResult:
    """Render ``docx`` to PDF and say what was done (tidy fixes, TOC update, removed blank pages,
    fonts that are substituted). One conversion slot and one ``timeout`` cover every render."""
    binary = soffice or find_soffice()
    if not binary:
        raise PdfUnavailable(UNAVAILABLE)
    if len(docx) > MAX_DOCX_BYTES:
        raise PdfError(REFUSED_LARGE)
    check_document(docx)
    prepared, fixes = _tidy(docx) if tidy else (docx, ())
    if not _slots.acquire(timeout=SLOT_WAIT):
        raise PdfBusy("the PDF converter is busy; try again in a few seconds")
    try:
        try:
            work = Path(tempfile.mkdtemp(prefix="pwndoc-pdf-"))  # mode 0700
        except OSError as exc:
            log.warning("PDF export: no temporary directory (%s)", exc.strerror)
            raise PdfError("could not prepare the PDF conversion") from exc
        try:
            return _render_report(work, prepared, binary, timeout, fixes)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        _slots.release()


def convert_to_pdf(
    docx: bytes, *, timeout: float = CONVERT_TIMEOUT, soffice: str | None = None, tidy: bool = True
) -> bytes:
    """The PDF bytes of ``convert_to_pdf_report``."""
    return convert_to_pdf_report(docx, timeout=timeout, soffice=soffice, tidy=tidy).data
