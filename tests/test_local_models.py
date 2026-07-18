import json
import httpx
import pytest
from pipeline.local_models import install_model
from pipeline.settings import RuntimeSettings


def test_model_install_is_local_explicit_and_sets_context():
    seen = []

    def handler(request):
        data = json.loads(request.content)
        seen.append(request.url.path)
        if request.url.path.endswith("/pull"):
            assert data["model"] == "qwen3.5:4b"
            return httpx.Response(
                200, text='{"status":"downloading","completed":50,"total":100}\n{"status":"success"}\n'
            )
        assert data["from"] == "qwen3.5:4b" and data["parameters"]["num_ctx"] == 16384
        return httpx.Response(200, json={"status": "success"})

    progress = []
    result = install_model(
        RuntimeSettings(provider="ollama"),
        "qwen3.5:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        on_progress=progress.append,
    )
    assert result["model"] == "pipeline-qwen-4b"
    assert progress[0]["percent"] == 50
    assert seen == ["/api/pull", "/api/create"]


def test_cloud_endpoint_cannot_be_used_as_model_downloader():
    with pytest.raises(ValueError, match="local"):
        install_model(RuntimeSettings(provider="gemini"), "qwen3.5:4b")
