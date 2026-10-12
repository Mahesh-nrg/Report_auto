import json
import os
import stat

import pytest

from pwndoc_ai.config import MASK, ConfigStore, Settings, SettingsError, apply_patch


def test_defaults_are_generic():
    s = Settings()
    assert s.llm_api_key == ""
    assert not s.llm_configured
    assert s.redact_field_labels == ()


def test_public_view_masks_secret():
    s = apply_patch(Settings(), {"llm_api_key": "sk-secret"})
    view = s.public()
    assert view["llm_api_key"] == MASK
    assert view["llm_api_key_set"] is True
    assert "sk-secret" not in json.dumps(view)


def test_masked_secret_roundtrip_keeps_stored_value():
    s = apply_patch(Settings(), {"llm_api_key": "sk-secret"})
    s2 = apply_patch(s, {"llm_api_key": MASK, "llm_model": "x"})
    assert s2.llm_api_key == "sk-secret"
    assert s2.llm_model == "x"


@pytest.mark.parametrize(
    "patch",
    [
        {"llm_base_url": "ftp://nope"},
        {"pwndoc_url": "not a url"},
        {"temperature": 9},
        {"web_results": -1},
        {"bogus": 1},
    ],
)
def test_invalid_patches_rejected(patch):
    with pytest.raises(SettingsError):
        apply_patch(Settings(), patch)


def test_labels_parsed_from_text_and_deduped():
    s = apply_patch(Settings(), {"redact_field_labels": "URL, Host\nurl\n\nHost"})
    assert s.redact_field_labels == ("URL", "Host", "url")


def test_patch_does_not_mutate_original():
    original = Settings()
    apply_patch(original, {"llm_model": "other"})
    assert original.llm_model == "gpt-4o-mini"


def test_store_persists_with_private_permissions(tmp_path):
    store = ConfigStore(tmp_path, env={})
    store.update({"llm_api_key": "sk-1", "llm_model": "m"})
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    again = ConfigStore(tmp_path, env={})
    assert again.settings.llm_api_key == "sk-1"
    assert again.settings.llm_model == "m"


def test_env_overrides_file_and_is_not_written_back(tmp_path):
    store = ConfigStore(tmp_path, env={"LLM_API_KEY": "from-env"})
    assert store.settings.llm_api_key == "from-env"
    store.update({"llm_model": "m2"})
    on_disk = json.loads(store.path.read_text())
    assert on_disk["llm_api_key"] == ""
    assert on_disk["llm_model"] == "m2"


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    assert ConfigStore(tmp_path, env={}).settings == Settings()


# -- env overlay, quarantine, key safety ---------------------------------------------------------
def test_env_value_stays_effective_after_ui_update_and_is_not_persisted(tmp_path):
    store = ConfigStore(tmp_path, env={"LLM_MODEL": "env-model"})
    updated = store.update({"llm_model": "ui-model", "temperature": 0.5})
    assert updated.llm_model == "env-model" and updated.temperature == 0.5
    on_disk = json.loads(store.path.read_text())
    assert on_disk["llm_model"] == "ui-model"  # the file keeps the user's own choice
    assert ConfigStore(tmp_path, env={}).settings.llm_model == "ui-model"


def test_env_never_leaks_into_file_even_for_non_secrets(tmp_path):
    store = ConfigStore(tmp_path, env={"PWNDOC_URL": "https://env.example:8443"})
    store.update({"temperature": 0.7})
    assert json.loads(store.path.read_text())["pwndoc_url"] == Settings().pwndoc_url


def test_corrupt_config_is_quarantined_not_silently_overwritten(tmp_path, caplog):
    (tmp_path / "config.json").write_text('{"llm_api_key": "sk-keep", "mystery_key": 1}')
    store = ConfigStore(tmp_path, env={})
    assert store.settings == Settings()
    assert (tmp_path / "config.json.bad").read_text().startswith('{"llm_api_key": "sk-keep"')
    assert not (tmp_path / "config.json").exists()
    assert "invalid" in caplog.text


def test_changing_llm_url_drops_stored_key_unless_new_key_given():
    base = apply_patch(Settings(), {"llm_api_key": "sk-1"})
    moved = apply_patch(base, {"llm_base_url": "https://evil.example/v1"})
    assert moved.llm_api_key == ""
    kept = apply_patch(base, {"llm_base_url": "https://ok.example/v1", "llm_api_key": "sk-2"})
    assert kept.llm_api_key == "sk-2"


def test_loading_file_with_custom_url_and_key_keeps_the_key(tmp_path):
    ConfigStore(tmp_path, env={}).update(
        {"llm_base_url": "https://llm.example/v1", "llm_api_key": "sk-1"}
    )
    assert (
        ConfigStore(tmp_path, env={"LLM_BASE_URL": "https://other.example/v1"}).settings.llm_api_key
        == "sk-1"
    )


def test_env_key_cannot_be_redirected_to_another_llm_host_from_the_ui(tmp_path):
    store = ConfigStore(tmp_path, env={"LLM_API_KEY": "from-env"})
    with pytest.raises(SettingsError, match="environment"):
        store.update({"llm_base_url": "https://evil.example/v1"})
    assert store.settings.llm_base_url == Settings().llm_base_url


def test_effective_verify_tls_exempts_only_loopback():
    from pwndoc_ai.config import effective_verify_tls

    assert effective_verify_tls(Settings(pwndoc_url="https://localhost:8443")) is False
    assert effective_verify_tls(Settings(pwndoc_url="https://127.0.0.1:8443")) is False
    assert effective_verify_tls(Settings(pwndoc_url="https://pwndoc.corp:8443")) is True
    assert (
        effective_verify_tls(Settings(pwndoc_url="https://pwndoc.corp", verify_tls=False)) is False
    )
