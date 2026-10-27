"""Field and bookmark references: which bookmarks are used, and which table of contents
paragraphs can be removed without breaking the field structure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from lxml import etree

from . import budget
from .errors import DocxLayoutError
from .ooxml import local, w

TOKEN = re.compile(r'"([^"]*)"|(\S+)')
REF_FIELDS = frozenset({"REF", "PAGEREF", "NOTEREF"})
MAX_REF_LENGTH = 400
MAX_FIELD_DEPTH = 64  # open fields at once; a deeper document is refused as too complex
CHECK_EVERY = 2048  # elements between two looks at the time budget


@dataclass
class _Field:
    instruction: str = ""
    in_result: bool = False
    toc: bool = False


@dataclass
class _Para:
    depth: int = 0
    balanced: bool = True
    in_toc: bool = False
    refs: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FieldScan:
    """Results of one pass over the body fields."""

    outside_refs: frozenset[str]
    toc_refs: Mapping[str, tuple[etree._Element, ...]]
    toc_removable: Mapping[etree._Element, frozenset[str]]
    has_toc: bool

    def referenced(self, name: str) -> bool:
        return name in self.outside_refs or name in self.toc_refs


def _tokens(instruction: str) -> list[str]:
    return [a or b for a, b in TOKEN.findall(instruction[:MAX_REF_LENGTH])]


def instruction_targets(instruction: str) -> tuple[str, list[str]]:
    """(field name in upper case, bookmark names the field points to)."""
    tokens = _tokens(instruction.strip())
    if not tokens:
        return "", []
    name = tokens[0].upper()
    if name in REF_FIELDS and len(tokens) > 1:
        return name, [tokens[1]]
    if name == "HYPERLINK":
        for index, token in enumerate(tokens[:-1]):
            if token.lower() == "\\l":
                return name, [tokens[index + 1]]
    return name, []


class _Scanner:
    def __init__(self) -> None:
        self.fields: list[_Field] = []
        self.paras: list[tuple[etree._Element, _Para]] = []
        self.outside: set[str] = set()
        self.records: dict[etree._Element, _Para] = {}
        self.has_toc = False
        self.open_toc = 0  # open fields that are a TOC (counted, not searched for)

    def in_toc(self) -> bool:
        return self.open_toc > 0

    def start_paragraph(self, paragraph: etree._Element) -> None:
        record = _Para(in_toc=self.in_toc())
        self.paras.append((paragraph, record))
        self.records[paragraph] = record

    def end_paragraph(self) -> None:
        _, record = self.paras.pop()
        record.balanced = record.balanced and record.depth == 0

    def add_ref(self, name: str) -> None:
        if self.in_toc() and self.paras:
            self.paras[-1][1].refs.add(name)
            self.paras[-1][1].in_toc = True
        else:
            self.outside.add(name)

    def finish_instruction(self, current: _Field, *, open_field: bool = True) -> None:
        current.in_result = True
        name, targets = instruction_targets(current.instruction)
        if name == "TOC":
            current.toc = True
            self.has_toc = True
            self.open_toc += 1 if open_field else 0
            if self.paras:
                self.paras[-1][1].in_toc = True
        for target in targets:
            self.add_ref(target)

    def field_char(self, kind: str) -> None:
        record = self.paras[-1][1] if self.paras else None
        if kind == "begin":
            if len(self.fields) >= MAX_FIELD_DEPTH:
                raise DocxLayoutError(budget.TOO_COMPLEX)
            self.fields.append(_Field())
            if record is not None:
                record.depth += 1
        elif kind == "separate" and self.fields and not self.fields[-1].in_result:
            self.finish_instruction(self.fields[-1])
        elif kind == "end" and self.fields:
            done = self.fields.pop()
            if done.toc:
                self.open_toc -= 1
            if not done.in_result:
                self.finish_instruction(done, open_field=False)
            if record is not None:
                record.depth -= 1
                record.balanced = record.balanced and record.depth >= 0

    def instr_text(self, text: str) -> None:
        if self.fields and not self.fields[-1].in_result:
            self.fields[-1].instruction += text


def _walk(scanner: _Scanner, body: etree._Element) -> None:
    for count, (event, element) in enumerate(etree.iterwalk(body, events=("start", "end"))):
        if count % CHECK_EVERY == 0:
            budget.check()
        name = local(element)
        if name == "p":
            scanner.start_paragraph(element) if event == "start" else scanner.end_paragraph()
        elif event != "start":
            continue
        elif name == "fldChar":
            scanner.field_char(element.get(w("fldCharType")) or "")
        elif name == "instrText":
            scanner.instr_text(element.text or "")
        elif name == "hyperlink" and element.get(w("anchor")):
            scanner.add_ref(element.get(w("anchor")))
        elif name == "fldSimple":
            _, targets = instruction_targets(element.get(w("instr")) or "")
            for target in targets:
                scanner.add_ref(target)


def scan_fields(body: etree._Element) -> FieldScan:
    scanner = _Scanner()
    _walk(scanner, body)
    toc_refs: dict[str, list[etree._Element]] = {}
    removable: dict[etree._Element, frozenset[str]] = {}
    for paragraph, record in scanner.records.items():
        if record.in_toc and record.refs:
            for name in record.refs:
                toc_refs.setdefault(name, []).append(paragraph)
            if record.balanced:
                removable[paragraph] = frozenset(record.refs)
    return FieldScan(
        frozenset(scanner.outside),
        {name: tuple(items) for name, items in toc_refs.items()},
        removable,
        scanner.has_toc,
    )
