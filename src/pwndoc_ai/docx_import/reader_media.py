"""Bytes of the pictures a document places, read through its relationships with size caps.

Only internal targets of ``image`` relationships that exist in the validated package are read;
external links are never fetched. A picture over ``MAX_IMAGE_BYTES`` is hashed while streaming
and its bytes are not kept; once the import holds ``MAX_TOTAL_IMAGE_BYTES`` further pictures are
flagged ``too_large`` the same way.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, replace

from . import limits
from .images import ImageFacts, facts_for
from .reader_xml import Relationship, check

SNIFF_BYTES = 64 * 1024
STREAM_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class MediaEntry:
    target: str
    facts: ImageFacts
    data: bytes  # b"" when the picture is too large to keep


class Media:
    """Loads and caches pictures by relationship id."""

    def __init__(self, package, rels: dict[str, Relationship]) -> None:
        self._package = package
        self._rels = rels
        self._cache: dict[str, MediaEntry | None] = {}
        self._kept = 0
        with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
            self._sizes = {info.filename: info.file_size for info in archive.infolist()}

    def entry(self, rid: str) -> MediaEntry | None:
        """The picture behind ``rid``, or ``None`` for an unknown, external or non-image one."""
        rel = self._rels.get(rid)
        if rel is None or rel.kind != "image" or not rel.target:
            return None
        if rel.target not in self._cache:
            self._cache[rel.target] = self._load(rel.target)
        return self._cache[rel.target]

    def sha_of(self, rid: str) -> str:
        entry = self.entry(rid)
        return entry.facts.sha256 if entry is not None else ""

    def _load(self, target: str) -> MediaEntry | None:
        size = self._sizes.get(target)
        if size is None:
            return None
        if size > limits.MAX_IMAGE_BYTES or self._kept + size > limits.MAX_TOTAL_IMAGE_BYTES:
            head, digest = self._stream(target)
            facts = facts_for(head, size=size, digest=digest)
            if "too_large" not in facts.flags:
                facts = replace(facts, flags=(*facts.flags, "too_large"))
            return MediaEntry(target, facts, b"")
        try:
            data = self._package.read(target, limits.MAX_IMAGE_BYTES)
        except ValueError:  # DocxError: damaged member
            return None
        self._kept += len(data)
        return MediaEntry(target, facts_for(data, size=len(data)), data)

    def _stream(self, target: str) -> tuple[bytes, str]:
        digest = hashlib.sha256()
        head = b""
        try:
            with (
                zipfile.ZipFile(io.BytesIO(self._package.data)) as archive,
                archive.open(target) as member,
            ):
                while block := member.read(STREAM_BLOCK):
                    check()
                    if len(head) < SNIFF_BYTES:
                        head += block[: SNIFF_BYTES - len(head)]
                    digest.update(block)
        except (zipfile.BadZipFile, KeyError, ValueError, OSError, EOFError, NotImplementedError):
            return b"", hashlib.sha256(b"").hexdigest()
        return head, digest.hexdigest()
