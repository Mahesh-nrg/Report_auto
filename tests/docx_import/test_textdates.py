"""Date grounding: an LLM date is accepted only when the header excerpt writes that date."""

from __future__ import annotations

from datetime import date

import pytest
from plan_fakes import FakeLlm, FakeSettings, report, text_tools  # noqa: F401 - autouse fixture

from pwndoc_ai.docx_import import fallback
from pwndoc_ai.docx_import.model import HeaderFields
from pwndoc_ai.docx_import.textdates import dates_in


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Start 2026-03-05", {date(2026, 3, 5)}),
        ("05.03.2026", {date(2026, 3, 5), date(2026, 5, 3)}),
        ("31/12/2025", {date(2025, 12, 31)}),
        ("5th of March, 2026", {date(2026, 3, 5)}),
        ("March 5, 2026 and Sept 1 2025", {date(2026, 3, 5), date(2025, 9, 1)}),
        ("Version 3 of 2025, page 12", set()),
        ("2026-02-30 and 99.99.2026 and 12345-01-01", set()),
    ],
)  # fmt: skip
def test_dates_in(text, expected):
    assert dates_in(text) == expected


def dates_report(header_text: str):
    blank = HeaderFields(company="Acme", company_candidates=("Acme",))
    return report(header=blank, header_text=header_text, findings=())


def answered(header_text: str, start: str, end: str) -> HeaderFields:
    llm = FakeLlm(lambda system, user, tool: {"date_start": start, "date_end": end})
    outcome = fallback.fill_gaps(dates_report(header_text), llm=llm, settings=FakeSettings())
    return outcome.report.header


def test_recombined_numbers_are_not_a_date():
    head = answered(
        "Acme\nStart Date | End Date\n05.03.2026 | 06.03.2026", "2026-05-06", "2026-06-05"
    )
    assert (head.date_start, head.date_end) != ("2026-05-06", "2026-06-05")
    assert head.date_start != "2026-05-06"


def test_numbers_that_are_not_dates_ground_nothing():
    head = answered(
        "Acme\nVersion 3 of 2025, page 12\nundefined | undefined", "2025-03-12", "2025-12-12"
    )
    assert (head.date_start, head.date_end) == ("", "")


def test_a_printed_date_is_accepted():
    head = answered(
        "Acme\nStart Date | End Date\n05.03.2026 | 06.03.2026", "2026-03-05", "2026-03-06"
    )
    assert (head.date_start, head.date_end) == ("2026-03-05", "2026-03-06")
