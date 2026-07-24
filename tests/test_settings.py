import json
import os
import pytest
from pipeline.settings import SettingsStore, RuntimeSettings
from pipeline.runtime import build


def test_settings_persist_and_never_echo_credentials(tmp_path):
    config = SettingsStore(tmp_path)
    config.update(
        {
            "provider": "gemini",
            "api_key": "private-key",
            "default_model": "gemini-test",
            "brave_api_key": "private-search",
        }
    )
    reloaded = SettingsStore(tmp_path)
    safe = reloaded.public()
    assert safe["api_key_present"] and safe["brave_api_key_present"]
    assert "private-key" not in json.dumps(safe)
    assert "private-search" not in json.dumps(safe)
    assert reloaded.load().api_key == "private-key"
    assert os.stat(config.path).st_mode & 0o777 == 0o600
    reloaded.update({"api_key": "", "default_model": "different-model"})
    assert reloaded.load().api_key == "private-key"  # blank form preserves a saved secret


def test_runtime_uses_saved_local_provider_without_api_key(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("PIPELINE_MODEL_") or name in {"PIPELINE_LLM_BASE_URL", "PIPELINE_LLM_API_KEY"}:
            monkeypatch.delenv(name)
    SettingsStore(tmp_path).update({"provider": "ollama", "default_model": "local-test"})
    app = build(tmp_path)
    assert app.model.base_url == "http://127.0.0.1:11434/v1"
    assert app.model.models["write"] == "local-test"
    assert not app.model.api_key


def test_provider_endpoint_rejects_embedded_secrets_and_private_remote_hosts():
    with pytest.raises(ValueError):
        RuntimeSettings(provider="custom", base_url="https://user:password@host.example/v1")
    with pytest.raises(ValueError):
        RuntimeSettings(provider="custom", base_url="http://192.168.1.5:8000/v1")


def test_local_preset_cannot_select_known_cloud_proxy_models():
    with pytest.raises(ValueError, match="cloud"):
        RuntimeSettings(provider="ollama", default_model="gemma4:cloud")


def test_profile_exclusions_are_private_and_used_by_runtime(tmp_path):
    config = SettingsStore(tmp_path)
    config.update({"profile_excluded_terms": ["restricted origin"]})
    assert "restricted origin" not in json.dumps(config.public())
    assert config.public()["profile_preferences_present"]
    assert build(tmp_path).writer.excluded_terms == ["restricted origin"]


def test_throughput_defaults_support_a_real_prospect_batch():
    config = RuntimeSettings()
    assert config.daily_model_limit == 1000
    assert config.requests_per_minute == 60
    assert config.discovery_limit == 50
    assert config.search_requests_per_minute == 20
    assert config.web_requests_per_minute == 240
    assert config.domain_requests_per_minute == 20
    assert config.discovery_search_limit == 12
    assert config.discovery_company_limit == 100


def test_raising_throughput_wakes_budget_deferred_work(setup):
    from pipeline.models import now
    from datetime import timedelta

    app, _, _, _ = setup
    settings = SettingsStore(app.store.path.parent)
    settings.update({"daily_model_limit": 100})
    app.store.enqueue("deferred-draft", "prepare", {"contact_id": 999}, due_at=now() + timedelta(days=1))
    app.store.execute("update jobs set error='Model daily call budget reached' where id='deferred-draft'")
    settings.update({"daily_model_limit": 1000})
    row = app.store.one("select due_at from jobs where id='deferred-draft'")
    assert row["due_at"] <= now().isoformat()
