"""Deep mode research: one lookup per section, query free of client data, result untrusted."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from . import limits
from .maskio import MaskAdapter
from .model import Section
from .prompts import neutralise

log = logging.getLogger(__name__)
EXCERPT_SOURCE_CHARS = 1200  # text masked before the first 300 characters are taken


def _takes_two_arguments(provider: Callable[..., Any]) -> bool:
    try:
        parameters = list(inspect.signature(provider).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind is p.VAR_POSITIONAL for p in parameters):
        return True
    positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


class ResearchSource:
    """Looks up reference text for a section through the gateway's context provider."""

    def __init__(self, provider: Callable[..., Any] | None, adapter: MaskAdapter) -> None:
        self._provider = provider
        self._adapter = adapter
        self._cache: dict[str, str] = {}
        self.failed = 0

    def _query(self, section: Section, body: str) -> tuple[str, str] | None:
        """(title, excerpt) with client data removed (not replaced by stand-ins), or None when
        nothing safe is left."""
        redact = self._adapter.redact
        excerpt = " ".join(body.split())[:EXCERPT_SOURCE_CHARS]
        excerpt = redact(excerpt)[: limits.RESEARCH_QUERY_CHARS]
        if section.level == 0:
            return (excerpt, "") if excerpt else None
        title = redact(section.title)
        return (title, excerpt) if title else None

    def _masked(self, found: str) -> str:
        """Research text for the prompt; text that cannot be masked safely is dropped."""
        try:
            self._adapter.prepare(found)
            return neutralise(self._adapter.mask(found))[: limits.MAX_RESEARCH_CHARS]
        except ValueError as exc:  # e.g. it holds a stand-in already sent: never guess
            self.failed += 1
            log.warning("research text dropped (%s)", type(exc).__name__)
            return ""

    def _call(self, title: str, excerpt: str) -> Any:
        assert self._provider is not None
        if _takes_two_arguments(self._provider):
            return self._provider(title, excerpt)
        return self._provider(f"{title}\n{excerpt}" if excerpt else title)

    def for_section(self, section: Section, body: str) -> str:
        if self._provider is None:
            return ""
        if section.id in self._cache:
            return self._cache[section.id]
        text = ""
        query = self._query(section, body)
        if query is not None:
            try:
                found = str(self._call(*query) or "")
            except Exception as exc:  # noqa: BLE001 - a lookup failure must not stop the run
                self.failed += 1
                log.warning(
                    "research lookup failed (%s); continuing without it", type(exc).__name__
                )
            else:
                text = self._masked(found)
        self._cache[section.id] = text
        return text
