import httpx
import pytest
from pipeline.providers import LanguageModel
from pipeline.db import Store
from pipeline.outbound import SafeRetry


def test_ollama_json_request_requires_no_api_key():
    def handler(request):
        assert not request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"score":0.9,"confidence":0.95,"reason":"Relevant"}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        api_key="",
        models={"qualify": "local"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert model.complete("qualify", {})["score"] == 0.9


def test_persistent_daily_budget_and_usage(tmp_path):
    store = Store(tmp_path / "pipeline.db")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"category":"QUESTION","confidence":0.95,"reason":"Question"}'}}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        models={"classify": "local"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        store=store,
        daily_limit=1,
    )
    model.complete("classify", {})
    with pytest.raises(SafeRetry, match="daily"):
        model.complete("classify", {})
    usage = store.one("select * from model_usage")
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 10


def test_cloud_reply_content_requires_explicit_data_permission(tmp_path):
    model = LanguageModel(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        models={"classify": "gemini-test"},
        allow_cloud_email=False,
    )
    with pytest.raises(ValueError, match="correspondence"):
        model.complete("classify", {"message": "Private customer text"})


def test_local_fast_mode_sends_no_thinking_hint():
    import json

    def handler(request):
        assert json.loads(request.content)["reasoning_effort"] == "none"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        models={"qualify": "local"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        reasoning_effort="none",
    )
    assert model.complete("qualify", {})["ok"]


def test_gemini_model_listing_requires_key_before_network():
    model = LanguageModel(base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key="")
    with pytest.raises(ValueError, match="AI Studio API key"):
        model.list_models()


def test_gemini_native_models_paginates_and_filters():
    def handler(request):
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "secret"
        assert "key" not in request.url.params
        if request.url.params.get("pageToken"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/gemini-4-argon", "supportedGenerationMethods": ["generateContent"]}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
                ],
                "nextPageToken": "next",
            },
        )

    model = LanguageModel(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert model.list_models() == ["gemini-3.8-flash", "gemini-4-argon"]


def test_model_listing_error_is_actionable_and_redacted():
    model = LanguageModel(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={"error": {"message": "secret"}})
            )
        ),
    )
    with pytest.raises(ValueError, match="403.*restrictions") as error:
        model.list_models()
    assert "secret" not in str(error.value)


def test_current_gemini_models_use_low_thinking_for_routine_json():
    import json

    def handler(request):
        assert json.loads(request.content)["reasoning_effort"] == "low"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    for name in ["gemini-3.8-flash", "gemini-4-argon"]:
        model = LanguageModel(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="fake",
            models={"write": name},
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert model.complete("write", {})["ok"]
