"""Explicit local Ollama model download/context setup; no shell or cloud fallback."""

import json
import time
import httpx
from .settings import is_local


def install_model(settings, model, client=None, on_progress=None):
    if settings.provider != "ollama" or not is_local(settings.base_url):
        raise ValueError("Model installation requires the local Ollama preset")
    if model not in {"qwen3.5:4b", "qwen3.5:9b"}:
        raise ValueError("Choose the 4B or 9B local model preset")
    root = settings.base_url.removesuffix("/v1")
    client = client or httpx.Client(timeout=httpx.Timeout(120, connect=5), trust_env=False)
    deadline = time.monotonic() + 1800
    complete = False
    with client.stream("POST", root + "/api/pull", json={"model": model, "stream": True}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise ValueError("Model download exceeded 30 minutes. Check Ollama and try again.")
            if not line:
                continue
            update = json.loads(line)
            if update.get("error"):
                raise ValueError("Ollama could not download this model; inspect the Ollama app")
            total, completed = update.get("total", 0), update.get("completed", 0)
            progress = {
                "status": update.get("status", "Downloading"),
                "percent": round(completed / total * 100) if total else None,
                "completed_bytes": completed,
                "total_bytes": total,
            }
            if on_progress:
                on_progress(progress)
            complete = update.get("status") == "success"
    if not complete:
        raise ValueError("Ollama did not confirm a completed model download")
    alias = "pipeline-qwen-" + model.rsplit(":", 1)[1]
    response = client.post(
        root + "/api/create",
        json={"model": alias, "from": model, "parameters": {"num_ctx": 16384}, "stream": False},
    )
    response.raise_for_status()
    if response.json().get("error"):
        raise ValueError("Model downloaded but its Pipeline context profile could not be created")
    return {
        "model": alias,
        "source_model": model,
        "context_tokens": 16384,
        "message": "Local weights ready. Test the connection and review actual writing quality.",
    }
