"""Safe access to the OPC (zip) package of a DOCX and writing it back with one part replaced."""

from __future__ import annotations

import io
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass

from . import limits
from .errors import DocxError
from .xmlutil import REL_NS, parse_xml

DEFAULT_MAIN_PART = "word/document.xml"
OFFICE_DOCUMENT = "/officeDocument"
MACRO_PART = re.compile(r"vbaproject|vbadata", re.IGNORECASE)
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
UNSAFE = "The file contains unsafe parts."
COPY_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Package:
    """A validated package. ``data`` is the original bytes; parts are read on demand."""

    data: bytes
    main_part: str
    names: tuple[str, ...]

    def read(self, name: str, limit: int) -> bytes:
        return _read(self.data, name, limit)


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise DocxError("The file is not a valid Word document.") from None


def _read(data: bytes, name: str, limit: int) -> bytes:
    try:
        with _open(data) as archive, archive.open(name) as member:
            payload = member.read(limit + 1)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, EOFError, NotImplementedError):
        raise DocxError("The file is damaged or is not a valid Word document.") from None
    if len(payload) > limit:
        raise DocxError("The document has a part that is too large to process.")
    return payload


def _check_name(name: str) -> None:
    parts = name.split("/")
    unsafe = (
        not name
        or len(name) > limits.MAX_NAME_LENGTH
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or DRIVE_PREFIX.match(name) is not None
        or any(part == ".." for part in parts)
    )
    if unsafe:
        raise DocxError(UNSAFE)


def _check_infos(archive: zipfile.ZipFile) -> list[str]:
    infos = archive.infolist()
    if len(infos) > limits.MAX_MEMBERS:
        raise DocxError("The file has too many parts.")
    names: list[str] = []
    total = 0
    for info in infos:
        _check_name(info.filename)
        if MACRO_PART.search(info.filename):
            raise DocxError("Documents with macros are not supported.")
        if info.flag_bits & 0x1:
            raise DocxError("Encrypted documents are not supported.")
        total += info.file_size
        if total > limits.MAX_UNCOMPRESSED_BYTES:
            raise DocxError("The document is too large once unpacked.")
        names.append(info.filename)
    if len(set(names)) != len(names):
        raise DocxError(UNSAFE)
    return names


def _main_part(data: bytes, names: set[str]) -> str:
    """The main document part named by the package relationships (default word/document.xml)."""
    if "_rels/.rels" in names:
        rels = parse_xml(_read(data, "_rels/.rels", limits.MAX_SMALL_XML_BYTES))
        for rel in rels.iter(f"{{{REL_NS}}}Relationship"):
            kind = rel.get("Type", "")
            if kind.endswith(OFFICE_DOCUMENT) and rel.get("TargetMode") != "External":
                target = posixpath.normpath((rel.get("Target") or "").lstrip("/"))
                if target in names:
                    return target
    if DEFAULT_MAIN_PART in names:
        return DEFAULT_MAIN_PART
    raise DocxError("The file is not a Word document.")


def _check_main_ratio(archive: zipfile.ZipFile, main_part: str) -> None:
    """The main part is parsed whole, so it may not be a few kilobytes that expand to megabytes."""
    info = archive.getinfo(main_part)
    if info.file_size >= limits.RATIO_FROM_BYTES and info.file_size > limits.MAX_XML_RATIO * max(
        1, info.compress_size
    ):
        raise DocxError("The document is too large or complex.")


def _check_content_types(data: bytes, names: set[str]) -> None:
    if "[Content_Types].xml" not in names:
        raise DocxError("The file is not a Word document.")
    types = _read(data, "[Content_Types].xml", limits.MAX_SMALL_XML_BYTES)
    if b"macroenabled" in types.lower():
        raise DocxError("Documents with macros are not supported.")


def open_package(data: bytes) -> Package:
    """Validate the zip and return the package. Raises DocxError with a user-safe message."""
    if not isinstance(data, bytes | bytearray) or not data:
        raise DocxError("The file is empty.")
    if len(data) > limits.MAX_PACKAGE_BYTES:
        raise DocxError("The file is too large.")
    data = bytes(data)
    if not data.startswith(b"PK"):
        raise DocxError("The file is not a valid Word document.")
    with _open(data) as archive:
        names = _check_infos(archive)
    _check_content_types(data, set(names))
    main_part = _main_part(data, set(names))
    with _open(data) as archive:
        _check_main_ratio(archive, main_part)
    return Package(data, main_part, tuple(names))


def _copy_member(source: zipfile.ZipFile, sink: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    target = _clone_info(info)
    target.file_size = info.file_size  # lets the writer decide about zip64 up front
    with source.open(info) as reader, sink.open(target, "w") as writer:
        shutil.copyfileobj(reader, writer, COPY_BLOCK)


def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    target = zipfile.ZipInfo(info.filename, info.date_time)
    target.compress_type = info.compress_type
    target.external_attr = info.external_attr
    target.create_system = info.create_system
    return target


def write_package(package: Package, replacements: dict[str, bytes]) -> bytes:
    """The package with the given parts replaced; every other member is copied unchanged
    (same name, timestamp and compression), in the original order."""
    out = io.BytesIO()
    try:
        with _open(package.data) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as sink:
            for info in source.infolist():
                if info.filename in replacements:
                    sink.writestr(_clone_info(info), replacements[info.filename])
                else:
                    _copy_member(source, sink, info)
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError):
        raise DocxError("The file is damaged or is not a valid Word document.") from None
    return out.getvalue()
