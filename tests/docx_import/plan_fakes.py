"""Builders and fakes shared by the B-PLAN tests (plan, completeness, fallback, maskflow).

``text_tools`` makes the reader's ``docx_import.html`` available: the real module when it is
installed, otherwise a small stand-in with the same allow-list contract (§h), so these tests
exercise the plan code against the contract and not against a particular reader build.
"""

from __future__ import annotations

import html as std_html
import importlib
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import pytest

from pwndoc_ai.docx_import import model
from pwndoc_ai.docx_import.model import (
    AuditTypeOption,
    Choice,
    HeaderFields,
    ImportedFinding,
    ImportedImage,
    ImportedReport,
    NameCheck,
    PlanDefaults,
    PocBlock,
    PwnDocOptions,
    SourceStats,
)
from pwndoc_ai.llm import Usage

IMPORT_ID = "0123456789ab4def8123456789abcdef"
SHA = "d" * 64
TEMPLATE = "c" * 24
OTHER_TEMPLATE = "e" * 24
COMPANY_ID = "b" * 24

ALLOWED = ("p", "br", "ul", "ol", "li", "strong", "em", "code", "pre")
BLOCKS = ("p", "ul", "ol", "li", "pre")
DROP_CONTENT = ("script", "style", "template", "iframe", "object")


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in DROP_CONTENT:
            self.skip += 1
        elif tag in ALLOWED and not self.skip:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
        elif tag in ALLOWED and tag != "br" and not self.skip:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(std_html.escape(data, quote=True))


def _stand_in_clean(value: str) -> str:
    parser = _Cleaner()
    parser.feed(value or "")
    parser.close()
    out = "".join(parser.out)
    return out if _stand_in_plain(out).strip() else ""


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _stand_in_plain(value: str) -> str:
    parser = _Plain()
    parser.feed(value or "")
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    return "\n".join(line for line in lines if line)


def _stand_in_module() -> types.ModuleType:
    module = types.ModuleType("pwndoc_ai.docx_import.html")
    module.clean_html = _stand_in_clean  # type: ignore[attr-defined]
    module.plain_text = _stand_in_plain  # type: ignore[attr-defined]
    return module


@pytest.fixture(autouse=True)
def text_tools(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The reader's html module, or the contract stand-in when it is not built yet."""
    try:
        return importlib.import_module("pwndoc_ai.docx_import.html")
    except ImportError:
        module = _stand_in_module()
        package = sys.modules["pwndoc_ai.docx_import"]
        monkeypatch.setitem(sys.modules, "pwndoc_ai.docx_import.html", module)
        monkeypatch.setattr(package, "html", module, raising=False)
        return module


# -- builders -----------------------------------------------------------------------------------


def finding(key: str = "f001", **values: Any) -> ImportedFinding:
    base: dict[str, Any] = {
        "title": "Stored XSS in search",
        "source_id": key[1:],
        "severity": "High",
        "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
        "cvss_score": "8.8",
        "description": "<p>The search page reflects input.</p>",
        "poc": (PocBlock("html", html="<p>Steps to Reproduce:</p>"),),
        "remediation": "<p>Encode output.</p>",
    }
    base.update(values)
    return ImportedFinding(key=key, **base)


def image(key: str = "i001", finding_key: str = "f001", **values: Any) -> ImportedImage:
    base: dict[str, Any] = {
        "sha256": "a" * 64,
        "media_type": "image/png",
        "size": 5000,
        "width": 800,
        "height": 600,
        "caption": "Login page",
        "finding_key": finding_key,
        "source_finding_key": finding_key,
        "order": int(key[1:]),
    }
    base.update(values)
    return ImportedImage(key=key, **base)


def header(**values: Any) -> HeaderFields:
    base: dict[str, Any] = {
        "company": "Brandarmy",
        "company_votes": 4,
        "company_candidates": ("Brandarmy",),
        "scope": ("https://brandarmy.com", "https://wink.ing/"),
        "date_start": "2026-03-05",
        "date_end": "2026-03-06",
        "test_type": "Grey Box Penetration Testing",
    }
    base.update(values)
    return HeaderFields(**base)


def report(**values: Any) -> ImportedReport:
    base: dict[str, Any] = {
        "header": header(),
        "findings": (finding("f001"), finding("f002", title="Weak password policy")),
        "images": (image("i001", "f001"),),
        "stats": SourceStats(summary_rows=2, detail_tables=2, merged=2),
        "header_text": "Vulnerability Assessment and Penetration Testing Report - Brandarmy",
    }
    base.update(values)
    return ImportedReport(**base)


def options() -> PwnDocOptions:
    return PwnDocOptions(
        languages=(Choice("en", "English"), Choice("fr", "French")),
        audit_types=(AuditTypeOption("Web", ("en", "fr")), AuditTypeOption("Mobile", ("fr",))),
        templates=(Choice(TEMPLATE, "Final_1"), Choice(OTHER_TEMPLATE, "Other")),
    )


def defaults() -> PlanDefaults:
    return PlanDefaults(language="en", audit_type="Web", template_id=TEMPLATE)


def build(rep: ImportedReport | None = None, **kwargs: Any) -> model.ImportPlan:
    from pwndoc_ai.docx_import import plan

    params: dict[str, Any] = {
        "import_id": IMPORT_ID,
        "file_name": "Brandarmy_VAPT_Report.docx",
        "file_sha256": SHA,
        "options": options(),
        "defaults": defaults(),
        "names": NameCheck(),
    }
    params.update(kwargs)
    return plan.build_plan(rep or report(), **params)


def codes(issues: Any) -> list[str]:
    return [issue.code for issue in issues]


# -- LLM fakes ----------------------------------------------------------------------------------


@dataclass
class FakeLlm:
    """``handler(system, user, tool) -> dict`` builds the answer; may raise LlmError."""

    handler: Callable[[str, str, dict], Any]
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    last_model: str = "fake-model"

    @property
    def prompts(self) -> list[str]:
        return [user for _, user, _ in self.calls]

    def call_tool(self, system: str, user: str, tool: dict) -> tuple[Any, Usage]:
        self.calls.append((system, user, tool))
        return self.handler(system, user, tool), Usage(1, 1)


@dataclass(frozen=True)
class FakeSettings:
    mask_sensitive: bool = True
    extra_instructions: str = ""
    max_output_tokens: int = 4096
