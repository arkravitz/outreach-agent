import httpx
import pytest
from pipeline.providers import LanguageModel
from pipeline.db import Store
from pipeline.outbound import SafeRetry


def model(tmp_path, handler, **kwargs):
    return LanguageModel(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="fake-key",
        models={"research": "gemini-3.8-flash"},
        store=Store(tmp_path / "pipeline.db"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_google_grounded_research_uses_native_tool_and_shared_budget(tmp_path):
    import json

    def handler(request):
        assert request.url.path == "/v1beta/interactions"
        assert request.headers["x-goog-api-key"] == "fake-key"
        body = json.loads(request.content)
        assert body["tools"] == [{"type": "google_search"}]
        assert body["store"] is False
        assert body["generation_config"]["thinking_level"] == "low"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": "Example",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.org",
                                        "title": "Example",
                                        "start_index": 0,
                                        "end_index": 7,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "usage": {"total_input_tokens": 20, "total_output_tokens": 10, "total_thought_tokens": 5},
            },
        )

    m = model(tmp_path, handler, daily_limit=1)
    result = m.grounded_research("Research document intake agencies")
    assert result["steps"][0]["content"][0]["annotations"][0]["url"] == "https://example.org"
    usage = m.store.one("select * from model_usage")
    assert usage["task"] == "google_search" and usage["status"] == "COMPLETED"
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 15
    with pytest.raises(SafeRetry, match="daily"):
        m.grounded_research("Another query")


def test_google_grounding_http_error_redacts_key(tmp_path):
    m = model(tmp_path, lambda r: httpx.Response(403, json={"error": {"message": "fake-key"}}))
    with pytest.raises(ValueError, match="403") as exc:
        m.grounded_research("Find a company")
    assert "fake-key" not in str(exc.value)
    assert m.store.one("select status from model_usage")["status"] == "FAILED"


def test_grounded_viewer_escapes_text_and_preserves_suggestions():
    from pipeline.google_research import render_research

    html = render_research(
        {
            "steps": [
                {
                    "type": "google_search_result",
                    "result": [{"search_suggestions": "<div>Google suggestions</div>"}],
                },
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "text",
                            "text": "<script>bad()</script>",
                            "annotations": [
                                {"type": "url_citation", "url": "javascript:bad()", "title": "bad"}
                            ],
                        }
                    ],
                },
            ]
        }
    )
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert "<div>Google suggestions</div>" in html


def test_grounded_research_view_expires_without_entering_prospect_records(tmp_path, monkeypatch):
    from pipeline.web_api import WebAPI
    import pipeline.web_api as module

    monkeypatch.setattr(
        LanguageModel,
        "grounded_research",
        lambda self, query: {
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "Operator answer"}]}]
        },
    )
    api = WebAPI(tmp_path)
    result = api.post("/api/google-research", {"query": "A question"}, "", "")
    key = result["viewer_url"].rsplit("/", 1)[-1]
    assert "Operator answer" in api.research_view(key)
    assert not api.store.all("select * from contacts")
    assert not api.store.all("select * from research_cache")
    assert not api.store.all("select * from executions")
    deadline = api.research_views[key][0]
    monkeypatch.setattr(module.time, "monotonic", lambda: deadline + 1)
    assert api.research_view(key) is None


@pytest.mark.parametrize(
    "response",
    [
        {"status": "failed", "steps": []},
        {"status": "completed", "steps": []},
    ],
)
def test_google_failed_answer_keeps_reported_billable_usage(tmp_path, response):
    response["usage"] = {"total_input_tokens": 20, "total_output_tokens": 10, "total_thought_tokens": 5}
    m = model(tmp_path, lambda r: httpx.Response(200, json=response))
    with pytest.raises(ValueError):
        m.grounded_research("A question")
    row = m.store.one("select * from model_usage")
    assert row["status"] == "FAILED" and row["input_tokens"] == 20 and row["output_tokens"] == 15
