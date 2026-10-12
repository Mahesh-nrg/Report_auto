"""Builds the research context (internet + local documents) handed to the LLM."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from .config import Settings
from .rag import DocumentIndex
from .web_context import WebContext

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 12000
ContextProvider = Callable[[str], str]
RESEARCH_TAG = re.compile(r"<(/?)research", re.IGNORECASE)


def _neutralise(text: str) -> str:
    """Untrusted text must not be able to close the <research> block it is wrapped in."""
    return RESEARCH_TAG.sub(r"&lt;\1research", text)


def make_context_provider(settings: Settings, rag: DocumentIndex | None) -> ContextProvider:
    web = WebContext(settings.web_results) if settings.use_web_search else None
    use_rag = bool(rag) and settings.use_rag

    def provide(title: str) -> str:
        sections: list[str] = []
        if web is not None:
            try:
                found = web.for_finding(title)
            except Exception as exc:  # noqa: BLE001 - research is best-effort
                log.warning("web research failed for %r: %s", title, exc)
                found = ""
            if found:
                sections.append("INTERNET SEARCH RESULTS\n" + found)
        if use_rag and rag is not None:
            try:
                local = rag.query(title)
            except Exception as exc:  # noqa: BLE001
                log.warning("local document lookup failed for %r: %s", title, exc)
                local = ""
            if local:
                sections.append("LOCAL DOCUMENT EXCERPTS\n" + local)
        return _neutralise("\n\n".join(sections))[:MAX_CONTEXT_CHARS]

    return provide
