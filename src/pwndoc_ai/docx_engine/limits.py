"""Caps applied to untrusted documents and to model output. Read as ``limits.NAME`` at call time
(tests lower them with monkeypatch)."""

from __future__ import annotations

MAX_PACKAGE_BYTES = 64 * 1024 * 1024  # the whole .docx as received
MAX_MEMBERS = 4000
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024  # sum of declared member sizes
MAX_XML_BYTES = 16 * 1024 * 1024  # word/document.xml (parsed whole: about 20x in memory)
MAX_XML_RATIO = 100  # a main part of 1 MiB or more may not expand more than this (real: 10-30)
RATIO_FROM_BYTES = 1024 * 1024
MAX_SMALL_XML_BYTES = 8 * 1024 * 1024  # styles, rels, content types
MAX_NAME_LENGTH = 255
MAX_PARAGRAPHS = 25_000
MAX_ELEMENTS = 3_000_000

MAX_REFINE_PARAGRAPH_CHARS = 20_000  # a longer paragraph is protected ("too long")
MAX_OUTPUT_PARAGRAPH_CHARS = 40_000  # the model may not return more than this for one paragraph
MAX_ADDITION_CHARS = 3_000
MAX_SELECTED_CHARS = 3_000_000  # refinable text one run may send to the model
MAX_CHUNKS_PER_RUN = 400  # model calls one run may make (a long document is refined in parts)
MAX_RESEARCH_SECTIONS = 150  # deep mode: web lookups one run may make (one per section)
MAX_SECTION_TITLE = 200

CHUNK_CHARS = 6000
CHUNK_MAX_PARAGRAPHS = 80
MAX_ADDITIONS_PER_CHUNK = 3
RESEARCH_QUERY_CHARS = 300
MAX_RESEARCH_CHARS = 8000

# Cost bounds of the word diff, in inner-loop steps (about 0.1 microsecond each in the worst case).
DIFF_STEPS_PER_PARAGRAPH = 20_000_000  # above this a paragraph is diffed by sentence, then coarsely
DIFF_STEPS_PER_BUILD = 200_000_000  # all paragraphs of one build; when spent every edit is coarse

LIGHT_RATIO = (0.5, 1.6)
DEEP_RATIO = (0.5, 6.0)
