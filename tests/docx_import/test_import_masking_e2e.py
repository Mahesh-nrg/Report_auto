"""Importer fallback end to end: finding calls carry no client value, even next to a newline/tab.

The header call sees real names by design (docs/IMPORTER_DESIGN.md §i); every finding call goes
through the masking layer with realistic stand-ins and must not leak the client's name, scope
hosts, addresses or e-mails, including values glued to a JSON-escaped ``\\n`` or ``\\t``.
"""

from __future__ import annotations

import importlib.util
import re

import pytest
from plan_fakes import (
    FakeLlm,
    FakeSettings,
    finding,
    header,
    report,
    text_tools,  # noqa: F401 - autouse fixture
)

from pwndoc_ai.docx_import import fallback
from pwndoc_ai.docx_import.model import PocBlock
from pwndoc_ai.masking import Masker

NEW_MASKER = importlib.util.find_spec("pwndoc_ai.masking_text") is not None and hasattr(
    Masker, "assert_no_dummies"
)
pytestmark = pytest.mark.skipif(
    not NEW_MASKER, reason="realistic-dummy Masker (M-CORE: masking_text.py) is not built yet"
)
LEAKS = re.compile(r"brandarmy|wink\.ing|10\.1\.2\.3|52\.14\.88\.21|ops@|alfred ai", re.IGNORECASE)
PAYLOAD = "' OR '1'='1' -- "


def glued_report():
    target = finding(
        "f001",
        title="",
        severity="",
        description=(
            "<p>Login at 10.1.2.3 accepts unlimited attempts.</p>"
            "<p>Brandarmy\tadmin panel on app.brandarmy.com.</p>"
        ),
        poc=(PocBlock("html", html=f"<p>Brandarmy login sent {PAYLOAD} to 52.14.88.21</p>"),),
        remediation="<p>Rate-limit ops@brandarmy.com and vpn.wink.ing.</p>",
    )
    return report(
        header=header(leftover_names=("Alfred AI",)),
        findings=(target,),
        images=(),
    )


def test_finding_calls_never_carry_a_client_value_even_next_to_escapes():
    llm = FakeLlm(lambda system, user, tool: {})
    fallback.fill_gaps(glued_report(), llm=llm, settings=FakeSettings())
    finding_calls = [user for user in llm.prompts if '"f001"' in user]
    assert finding_calls, "no finding call was made"
    for user in finding_calls:
        assert not LEAKS.search(user), LEAKS.search(user).group()
        assert PAYLOAD.strip() in user  # the payload reaches the model as written


def test_finding_call_system_prompt_uses_the_stand_in_wording():
    llm = FakeLlm(lambda system, user, tool: {})
    fallback.fill_gaps(glued_report(), llm=llm, settings=FakeSettings())
    systems = [system for system, user, _ in llm.calls if '"f001"' in user]
    assert systems and not any("[[CLIENT" in s or "withheld" in s for s in systems)
