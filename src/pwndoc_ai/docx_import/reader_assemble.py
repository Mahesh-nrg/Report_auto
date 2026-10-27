"""Final step of the reader: keys, PoC blocks, screenshots and their flags.

Findings get ``f001``... in order of first appearance; screenshots get ``i001``... in document
order. Every screenshot keeps one ``image`` block in its finding's PoC, at the place it had in
the document (pictures found elsewhere in the finding, or after its table, go at the end).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from . import limits
from .captions import caption_key
from .dedupe import Merged
from .findings import Figure, RawFinding
from .html import Para, group_blocks, render
from .images import ImageFacts
from .model import CompletenessIssue, ImageBlob, ImportedFinding, ImportedImage, PocBlock
from .reader_media import Media, MediaEntry

MISSING = MediaEntry(
    target="",
    facts=ImageFacts(hashlib.sha256(b"").hexdigest(), "", 0, 0, 0, ("unsupported",)),
    data=b"",
)
FLAG_ISSUES = (
    ("placeholder", "image_placeholder"),
    ("unsupported", "image_unsupported"),
    ("too_large", "image_too_large"),
    ("uncaptioned", "image_uncaptioned"),
    ("unmatched", "image_unmatched"),
)


@dataclass(frozen=True)
class Built:
    findings: tuple[ImportedFinding, ...]
    images: tuple[ImportedImage, ...]
    blobs: tuple[ImageBlob, ...]
    finding_issues: tuple[CompletenessIssue, ...]
    image_issues: tuple[CompletenessIssue, ...]


@dataclass(frozen=True)
class _Placed:
    finding: int  # index of the finding
    slot: int  # index of the Figure in that finding's PoC items
    figure: Figure


def finding_key(index: int) -> str:
    return f"f{index + 1:03d}"


def image_key(index: int) -> str:
    return f"i{index + 1:03d}"


def _entry(media: Media, rid: str) -> MediaEntry:
    """The picture behind ``rid``; one the package cannot give (unknown, external or damaged
    relationship) is still listed, flagged ``unsupported``, never silently dropped."""
    return media.entry(rid) or MISSING


def _placed(merged: Sequence[Merged]) -> list[_Placed]:
    found = [
        _Placed(f, slot, item)
        for f, entry in enumerate(merged)
        for slot, item in enumerate(entry.finding.poc)
        if isinstance(item, Figure)
    ]
    return sorted(found, key=lambda p: (p.figure.order, p.finding, p.slot))


class _Poc:
    """Builds PoC blocks within the block and character caps."""

    def __init__(self) -> None:
        self.blocks: list[PocBlock] = []
        self.paras: list[Para] = []
        self.chars = 0
        self.cut = False

    def _room(self, size: int) -> bool:
        if len(self.blocks) >= limits.MAX_POC_BLOCKS or self.chars + size > limits.MAX_POC_CHARS:
            self.cut = True
            return False
        return True

    def flush(self) -> None:
        rendered = render(self.paras)
        self.paras = []
        self.cut = self.cut or rendered.truncated
        for html in group_blocks(rendered.chunks, limits.MAX_RICH_CHARS):
            if self._room(len(html)):
                self.blocks.append(PocBlock(kind="html", html=html))
                self.chars += len(html)

    def image(self, key: str) -> None:
        self.flush()
        if self._room(0):
            self.blocks.append(PocBlock(kind="image", image_key=key))


def _poc(finding: RawFinding, keys: dict[int, str]) -> tuple[tuple[PocBlock, ...], bool]:
    poc = _Poc()
    for slot, item in enumerate(finding.poc):
        if isinstance(item, Para):
            poc.paras.append(item)
        elif slot in keys:
            poc.image(keys[slot])
    poc.flush()
    return tuple(poc.blocks), poc.cut


def _flags(entry: Merged, cut: bool) -> tuple[str, ...]:
    flags = []
    if not entry.has_detail:
        flags.append("summary_only")
    if cut or entry.finding.truncated:
        flags.append("truncated")
    if entry.copies > 1:
        flags.append("merged_duplicate")
    if entry.disagree:
        flags.append("copies_disagree")
    return tuple(flags)


def _finding(index: int, entry: Merged, keys: dict[int, str]) -> tuple[ImportedFinding, tuple]:
    raw = entry.finding
    poc, cut = _poc(raw, keys)
    origin = "merged" if entry.copies > 1 else raw.origin
    finding = ImportedFinding(
        key=finding_key(index),
        title=raw.title,
        source_id=raw.source_id,
        severity=raw.severity,
        cvss_vector=raw.cvss_vector,
        cvss_score=raw.cvss_score,
        cwe=raw.cwe,
        urls=raw.urls,
        parameter=raw.parameter,
        location=raw.location,
        description=raw.description,
        observation=raw.observation,
        poc=poc,
        remediation=raw.remediation,
        references=raw.references,
        origin=origin,
        flags=_flags(entry, cut),
    )
    fields = tuple(dict.fromkeys((*raw.truncated, *(("poc",) if cut else ()))))
    issue = CompletenessIssue("text_truncated", finding_key=finding.key, fields=fields)
    return finding, ((issue,) if fields else ())


def _image(key: str, placed: _Placed, media: Media, duplicate: bool) -> ImportedImage:
    facts = _entry(media, placed.figure.rid).facts
    flags = list(facts.flags)
    if not placed.figure.captioned or not placed.figure.caption:
        flags.append("uncaptioned")
    if placed.figure.unmatched:
        flags.append("unmatched")
    if duplicate:
        flags.append("duplicate_caption")
    usable = facts.usable and "placeholder" not in flags
    owner = finding_key(placed.finding)
    return ImportedImage(
        key=key,
        sha256=facts.sha256,
        media_type=facts.media_type if facts.media_type in limits.IMAGE_TYPES else "",
        size=min(facts.size, limits.MAX_TOTAL_IMAGE_BYTES),
        width=facts.width,
        height=facts.height,
        caption=placed.figure.caption,
        finding_key=owner,
        source_finding_key=owner,
        include=usable,
        order=placed.figure.order,
        flags=tuple(dict.fromkeys(flags)),
    )


def _images(placed: list[_Placed], media: Media) -> tuple[list[ImportedImage], list[ImageBlob]]:
    images: list[ImportedImage] = []
    blobs: list[ImageBlob] = []
    captions: dict[tuple[int, str], str] = {}
    for index, item in enumerate(placed):
        key = image_key(index)
        sha = media.sha_of(item.figure.rid)
        caption = (item.finding, caption_key(item.figure.caption))
        duplicate = bool(caption[1]) and captions.get(caption, sha) != sha
        captions.setdefault(caption, sha)
        images.append(_image(key, item, media, duplicate))
        blobs.append(ImageBlob(key=key, data=_entry(media, item.figure.rid).data))
    return images, blobs


def _image_issues(images: Sequence[ImportedImage]) -> tuple[CompletenessIssue, ...]:
    return tuple(
        CompletenessIssue(code, image_key=image.key, finding_key=image.finding_key)
        for image in images
        for flag, code in FLAG_ISSUES
        if flag in image.flags
    )


def assemble(merged: Sequence[Merged], media: Media) -> Built:
    placed = _placed(merged)
    keys: dict[int, dict[int, str]] = {}
    for index, item in enumerate(placed):
        keys.setdefault(item.finding, {})[item.slot] = image_key(index)
    findings: list[ImportedFinding] = []
    issues: list[CompletenessIssue] = []
    for index, entry in enumerate(merged):
        finding, extra = _finding(index, entry, keys.get(index, {}))
        findings.append(finding)
        issues.extend(extra)
    images, blobs = _images(placed, media)
    return Built(tuple(findings), tuple(images), tuple(blobs), tuple(issues), _image_issues(images))
