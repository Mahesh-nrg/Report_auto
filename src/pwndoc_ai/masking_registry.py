"""The in-memory registry of issued stand-ins (one per Masker, never persisted or logged).

Maps every issued dummy surface (case-folded) to the real value it stands for, remembers which
stand-ins already left the machine (``sent``), holds the reserved corpus tokens no dummy may equal
and compiles the single reverse alternation used by unmask and the leak scans.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace

from . import masking_text as mt
from .masking_model import MappingKind

_TOKEN = re.compile(r"[^\W_][\w.\-]{0,80}")
_SPLIT = re.compile(r"[.\-]+")


@dataclass(frozen=True)
class Entry:
    """What one stand-in surface restores to and how (``style``: name, host or exact)."""

    original: str
    kind: MappingKind
    style: str
    sent: bool = False

    def __repr__(self) -> str:  # never show the real value
        return f"Entry(kind={self.kind.value}, style={self.style}, sent={self.sent})"


class Registry:
    """Lock-guarded mapping of stand-in surfaces; every method is safe across threads."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._reserved: set[str] = set()
        self._names: set[str] = set()
        self._prefixes: set[tuple[int, int, int]] = set()
        self._ip_originals: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    # -- entries ---------------------------------------------------------------------------

    def add(self, dummy: str, original: str, kind: MappingKind, style: str, sent: bool) -> None:
        """Register ``dummy`` (first registration wins); ``sent`` marks it as externalised."""
        key = dummy.casefold()
        with self.lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = Entry(original, kind, style, sent)
                self._dirty = True
                if kind is MappingKind.IP:
                    self._ip_originals.add(_ip_text(original))
            elif sent and not entry.sent:
                self._entries[key] = replace(entry, sent=True)

    def add_name(self, name: str) -> None:
        """Remember a generated name word/body for the substring-free rule."""
        with self.lock:
            self._names.add(name.casefold())

    def get(self, matched: str) -> Entry | None:
        return self._entries.get(mt.canonical_dummy(matched))

    def forget(self, key: str) -> None:
        """Drop an unsent stand-in and reserve its surface so it is never issued again."""
        with self.lock:
            self._entries.pop(key, None)
            self._names.discard(key)
            self._reserved.add(key)
            self._dirty = True

    def items(self) -> tuple[tuple[str, Entry], ...]:
        with self.lock:
            return tuple(self._entries.items())

    def pattern(self) -> re.Pattern[str] | None:
        """The reverse alternation of every issued surface (recompiled only after a change)."""
        with self.lock:
            if self._dirty:
                self._pattern = mt.compile_unmask(self._entries)
                self._dirty = False
            return self._pattern

    # -- reserved corpus -------------------------------------------------------------------

    def reserve_text(self, text: str) -> int:
        """Add the words, hosts and IPv4 ``/24`` prefixes of ``text``; returns the token count."""
        count = 0
        with self.lock:
            for m in _TOKEN.finditer(text):
                token = m.group().casefold().strip(".-")
                self._reserved.add(token)
                self._reserved.update(p for p in _SPLIT.split(token) if p)
                count += 1
            for m in mt._IPV4_RE.finditer(text):
                self._prefixes.add(tuple(int(o) for o in m.group().split(".")[:3]))
        return count

    def is_reserved(self, candidate: str) -> bool:
        return candidate.casefold() in self._reserved

    def prefix_reserved(self, prefix: tuple[int, int, int]) -> bool:
        return prefix in self._prefixes

    def name_is_free(self, candidate: str, real_keys: Iterable[str]) -> bool:
        """A name is free when unused, unreserved, not real and substring-free of other names."""
        key = candidate.casefold()
        if key in self._reserved or key in self._entries or key in set(real_keys):
            return False
        return all(key not in other and other not in key for other in self._names)

    def known_original(self, value: str) -> bool:
        """True when ``value`` is a real value behind an issued IP stand-in."""
        with self.lock:
            return _ip_text(value) in self._ip_originals


def _ip_text(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value
