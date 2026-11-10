"""The masking boundary: header calls see real names, finding calls never do."""

from __future__ import annotations

import json

import pytest
from plan_fakes import FakeLlm, header, text_tools  # noqa: F401 - autouse fixture
from stand_in_masker import StandInMasker

from pwndoc_ai.docx_import import maskflow
from pwndoc_ai.docx_import.errors import FallbackRejected
from pwndoc_ai.docx_import.model import Contact, HeaderFields
from pwndoc_ai.llm import LlmError
from pwndoc_ai.masking import Masker, NullMasker

TOOL = {"type": "function", "function": {"name": "submit_refined_finding"}}
SECRETS = ("Brandarmy", "Alfred AI", "Wink Ltd", "Ann", "Smith", "ann@brandarmy.com", "10.1.2.3")
TEXT = (
    "Brandarmy and Wink Ltd share a host at 10.1.2.3; contact Ann Smith <ann@brandarmy.com> "
    "or see https://brandarmy.com/login. Template text names Alfred AI."
)


def full_header() -> HeaderFields:
    return header(
        company_candidates=("Brandarmy", "Wink Ltd"),
        leftover_names=("Alfred AI",),
        contacts=(Contact("ann@brandarmy.com", "Ann", "Smith"),),
    )


def echo(system: str, user: str, tool: dict) -> dict:
    return {"text": user}


def test_finding_masker_masks_every_client_term() -> None:
    masker = maskflow.finding_masker(enabled=True, header=full_header(), extra_terms=("Project X",))
    masked = masker.mask_plain(TEXT + " Project X")
    for secret in (*SECRETS, "Project X", "https://brandarmy.com"):
        assert secret not in masked
    assert masked.endswith(masker.mask_plain("Project X"))  # same stand-in in every call
    assert masker.unmask(masked) == TEXT + " Project X"


def test_null_masker_only_when_off_and_nothing_named() -> None:
    off = maskflow.finding_masker(enabled=False, header=HeaderFields(), extra_terms=())
    assert isinstance(off, NullMasker)
    named = maskflow.finding_masker(enabled=False, header=header(), extra_terms=())
    assert isinstance(named, Masker)
    on = maskflow.finding_masker(enabled=True, header=HeaderFields(), extra_terms=(" ",))
    assert isinstance(on, Masker)
    assert "10.1.2.3" not in on.mask_plain("host 10.1.2.3")


def test_call_masked_sends_no_client_data_and_restores_the_answer() -> None:
    masker = maskflow.finding_masker(enabled=True, header=full_header(), extra_terms=())
    llm = FakeLlm(echo)
    answer = maskflow.call_masked(llm, masker, "system", TEXT, TOOL)
    sent = llm.prompts[0]
    assert not any(secret in sent for secret in SECRETS)
    assert "brandarmy.com" not in sent
    assert answer == {"text": TEXT}


def test_mask_sensitive_opt_out_sends_text_unchanged() -> None:
    masker = maskflow.finding_masker(enabled=False, header=HeaderFields(), extra_terms=())
    llm = FakeLlm(echo)
    maskflow.call_masked(llm, masker, "system", "host 10.1.2.3", TOOL)
    assert llm.prompts == ["host 10.1.2.3"]


def test_a_legacy_placeholder_never_issued_rejects() -> None:
    masker = maskflow.finding_masker(enabled=True, header=full_header(), extra_terms=())
    answer = {"title": "[[CLIENT_9]] login"}
    with pytest.raises(FallbackRejected):
        maskflow.call_masked(FakeLlm(lambda *a: answer), masker, "s", "Brandarmy", TOOL)


@pytest.mark.parametrize(
    "answer",
    [
        {"title": "Lumenfold login"},  # a stand-in the masker could not restore
        {"nested": [{"title": "bypass on 198.18.0.9"}]},  # an address from a stand-in pool
    ],
)
def test_stand_ins_left_after_unmask_reject(answer: dict) -> None:
    masker = StandInMasker({"Brandarmy": "Lumenfold"}, sticky=("Lumenfold",))
    with pytest.raises(FallbackRejected):
        maskflow.call_masked(FakeLlm(lambda *a: answer), masker, "s", "Brandarmy", TOOL)


def test_a_stand_in_like_value_already_in_the_input_is_accepted() -> None:
    masker = StandInMasker({"Brandarmy": "Lumenfold"}, sticky=("Lumenfold",))
    answer = maskflow.call_masked(
        FakeLlm(lambda *a: {"t": "Lumenfold login"}), masker, "s", "Lumenfold login", TOOL
    )
    assert answer == {"t": "Lumenfold login"}


# -- items are masked before they are serialised -------------------------------------------------
def render(items) -> str:
    return "\n".join(json.dumps(item) for item in items)


def test_items_are_masked_value_by_value_so_escaped_newlines_cannot_hide_a_name() -> None:
    masker = maskflow.finding_masker(enabled=True, header=full_header(), extra_terms=())
    items = [{"key": "f001", "need": ["title"], "text": "first line\nBrandarmy\tWink Ltd"}]
    llm = FakeLlm(lambda *a: {"findings": []})
    maskflow.call_masked_items(llm, masker, "s", items, render, TOOL)
    sent = llm.prompts[0]
    assert "Brandarmy" not in sent and "Wink Ltd" not in sent
    assert json.loads(sent)["key"] == "f001" and json.loads(sent)["need"] == ["title"]


def test_masked_items_answer_is_restored() -> None:
    masker = maskflow.finding_masker(enabled=True, header=full_header(), extra_terms=())
    items = [{"key": "f001", "need": ["title"], "text": "Brandarmy login", "n": 3}]

    def echo_title(system: str, user: str, tool: dict) -> dict:
        sent = json.loads(user)
        assert sent["n"] == 3
        return {"findings": [{"key": sent["key"], "title": sent["text"]}]}

    llm = FakeLlm(echo_title)
    answer = maskflow.call_masked_items(llm, masker, "s", items, render, TOOL)
    assert answer == {"findings": [{"key": "f001", "title": "Brandarmy login"}]}


def test_items_that_cannot_be_masked_send_nothing() -> None:
    class Colliding(StandInMasker):
        def mask_json_plain(self, value):
            raise ValueError("stand-in collision")

    llm = FakeLlm(lambda *a: {})
    with pytest.raises(FallbackRejected):
        maskflow.call_masked_items(llm, Colliding({}), "s", [{"key": "k", "t": "x"}], render, TOOL)
    assert llm.calls == []


def test_prepare_registers_the_finding_corpus() -> None:
    masker = StandInMasker({"Brandarmy": "Lumenfold"})
    maskflow.prepare(masker, [{"key": "f001", "text": "Brandarmy"}])
    assert masker.reserved == [[{"key": "f001", "text": "Brandarmy"}]]


@pytest.mark.parametrize("answer", [None, [], "text", 3])
def test_non_dict_answers_reject(answer: object) -> None:
    masker = maskflow.finding_masker(enabled=True, header=header(), extra_terms=())
    with pytest.raises(FallbackRejected):
        maskflow.call_masked(FakeLlm(lambda *a: answer), masker, "s", "u", TOOL)
    with pytest.raises(FallbackRejected):
        maskflow.call_header(FakeLlm(lambda *a: answer), "s", "u", TOOL)


def test_too_deep_answers_reject() -> None:
    deep: dict = {}
    node = deep
    for _ in range(150):
        node["x"] = {}
        node = node["x"]
    masker = maskflow.finding_masker(enabled=True, header=header(), extra_terms=())
    with pytest.raises(FallbackRejected):
        maskflow.call_masked(FakeLlm(lambda *a: deep), masker, "s", "u", TOOL)


def test_llm_errors_propagate() -> None:
    def boom(*args: object) -> dict:
        raise LlmError("down")

    masker = maskflow.finding_masker(enabled=True, header=header(), extra_terms=())
    with pytest.raises(LlmError):
        maskflow.call_masked(FakeLlm(boom), masker, "s", "u", TOOL)


def test_header_call_sees_real_names() -> None:
    llm = FakeLlm(lambda *a: {"company": "Brandarmy"})
    answer = maskflow.call_header(llm, "system", TEXT, TOOL)
    assert answer == {"company": "Brandarmy"}
    assert all(secret in llm.prompts[0] for secret in SECRETS)


# -- closer review: scope hosts are client terms, and no client name means no finding call ----
def test_scope_hosts_are_masked_in_finding_text() -> None:
    scoped = header(scope=("https://www.brandarmy.com/app", "wink.ing", "10.9.8.7", "x"))
    assert maskflow.scope_hosts(scoped.scope) == ("brandarmy.com", "wink.ing")
    masker = maskflow.finding_masker(enabled=True, header=scoped, extra_terms=())
    masked = masker.mask_plain("seen on api.wink.ing, staging.wink.ing and brandarmy.com")
    assert "wink" not in masked and "brandarmy" not in masked


def test_scope_hosts_keep_the_registrable_part_of_a_subdomain() -> None:
    assert maskflow.scope_hosts(("https://app.client.test:8443/x",)) == (
        "app.client.test",
        "client.test",
    )


def test_has_client_name_needs_a_company_or_a_candidate() -> None:
    assert not maskflow.has_client_name(header(company="", company_candidates=()))
    assert maskflow.has_client_name(header(company="", company_candidates=("Acme",)))
    assert maskflow.has_client_name(header(company="Acme"))


def test_no_client_name_with_masking_on_makes_no_finding_call() -> None:
    from dataclasses import replace

    from plan_fakes import FakeSettings, codes, finding, report

    from pwndoc_ai.docx_import import fallback

    blank = header(company="", company_candidates=())
    rep = report(header=blank, header_text="", findings=(finding("f001", title="", severity=""),))
    llm = FakeLlm(lambda system, user, tool: {"findings": []})
    outcome = fallback.fill_gaps(rep, llm=llm, settings=FakeSettings(mask_sensitive=True))
    assert llm.calls == [] and "llm_skipped_unmasked" in codes(outcome.report.issues)
    opened = fallback.fill_gaps(
        replace(rep, header=header(company="Acme")), llm=llm, settings=FakeSettings()
    )
    assert llm.calls and "llm_skipped_unmasked" not in codes(opened.report.issues)
