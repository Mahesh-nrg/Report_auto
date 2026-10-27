"""Build the clean and the tracked-changes documents from the accepted edits."""

from __future__ import annotations

import copy
import threading
from collections.abc import Sequence

from lxml import etree

from . import diff_edit
from .diff_edit import IdSource, add_paragraph_after, apply_paragraph_edit
from .diff_plan import DiffBudget, Edit
from .inspection import LoadedDocument
from .package import write_package
from .xmlutil import w


class OutputsCancelled(Exception):
    """The cancel event was set while the documents were being built."""


class _Planner:
    """Plans each (old, new) text pair once for the whole build (the clean and the tracked copy
    share it), within one diff budget, and notices a cancel between paragraphs."""

    def __init__(self, cancel: threading.Event | None) -> None:
        self._cancel = cancel
        self._budget = DiffBudget()
        self._plans: dict[tuple[str, str], list[Edit]] = {}

    def check(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise OutputsCancelled

    def __call__(self, old: str, new: str) -> list[Edit]:
        self.check()
        key = (old, new)
        if key not in self._plans:
            self._plans[key] = diff_edit.plan_edits(old, new, self._budget)
        return self._plans[key]


def _serialise(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _index(unit_id: str) -> int:
    return int(unit_id[1:])


def _apply(
    loaded: LoadedDocument,
    edits: Sequence[tuple[str, str]],
    additions: Sequence[tuple[str, str]],
    *,
    track: bool,
    author: str,
    timestamp: str,
    planner: _Planner,
) -> bytes:
    root = copy.deepcopy(loaded.root)
    paragraphs = list(root.iter(w("p")))
    ids = IdSource.above(root)
    marks = {"track": track, "author": author, "timestamp": timestamp, "id_source": ids}
    # Additions first: they copy formatting from the paragraph as it was in the original.
    last_added: dict[int, etree._Element] = {}
    for after_id, text in additions:
        index = _index(after_id)
        if not loaded.inspection.units[index].addable:
            continue  # the chunk judge refuses these; never write one that rejects unclean
        heading = loaded.inspection.units[index].heading_level is not None
        last_added[index] = add_paragraph_after(
            paragraphs[index], text, plain_body=heading, after=last_added.get(index), **marks
        )
    for unit_id, text in edits:
        planner.check()
        apply_paragraph_edit(paragraphs[_index(unit_id)], text, planner=planner, **marks)
    return _serialise(root)


def build_outputs(
    loaded: LoadedDocument,
    edits: Sequence[tuple[str, str]],
    additions: Sequence[tuple[str, str]],
    *,
    author: str,
    timestamp: str,
    cancel: threading.Event | None = None,
) -> tuple[bytes, bytes]:
    """(clean, tracked) DOCX bytes. Only the main document part is rewritten; every other member
    is copied unchanged. With nothing to apply both are the original bytes. Raises
    OutputsCancelled when ``cancel`` is set while building."""
    if not edits and not additions:
        return loaded.package.data, loaded.package.data
    planner = _Planner(cancel)
    out = []
    for track in (False, True):
        xml = _apply(
            loaded, edits, additions, track=track, author=author, timestamp=timestamp,
            planner=planner,
        )  # fmt: skip
        out.append(write_package(loaded.package, {loaded.package.main_part: xml}))
    return out[0], out[1]
