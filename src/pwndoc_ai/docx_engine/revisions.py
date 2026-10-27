"""Word revision marks (w:ins / w:del) and the id counter that keeps them unique."""

from __future__ import annotations

from lxml import etree

from .xmlutil import w

MAX_ID_DIGITS = 9  # ids with more digits are not ST_DecimalNumber values Word accepts (2**31 - 1)


def _plain_number(value: str) -> int | None:
    """The id as a number when it is plain ASCII digits within range, else None (ignored)."""
    if value.isascii() and value.isdecimal() and len(value) <= MAX_ID_DIGITS:
        return int(value)
    return None


class IdSource:
    """Hands out revision ids that are unique in the document: above every id already used."""

    def __init__(self, start: int = 1) -> None:
        self._next = start

    @classmethod
    def above(cls, root: etree._Element) -> IdSource:
        highest = 0
        key = w("id")
        for element in root.iter():
            value = element.get(key) if isinstance(element.tag, str) else None
            number = _plain_number(value) if value is not None else None
            if number is not None:
                highest = max(highest, number)
        return cls(highest + 1)

    def __call__(self) -> int:
        value = self._next
        self._next += 1
        return value


def revision(kind: str, id_source: IdSource, author: str, timestamp: str) -> etree._Element:
    """An empty ``w:ins`` or ``w:del`` carrying id, author and date."""
    element = etree.Element(w(kind))
    element.set(w("id"), str(id_source()))
    element.set(w("author"), author)
    element.set(w("date"), timestamp)
    return element
