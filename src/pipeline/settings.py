"""UI-managed runtime settings shared with CLI and unattended workers."""

from typing import Literal, Annotated
import ipaddress
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .auth import save_token
from .prompt_context import TASK_OUTPUT_CAPS

PRESETS = {
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "default_model": "qwen3.5:9b"},
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3.8-flash",
    },
    "custom": {"base_url": "", "default_model": ""},
}
_LOCK = threading.RLock()


def is_local(url):
    return urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}


def valid_endpoint(url):
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Use a model API URL without embedded credentials, query or fragment")
    if parts.scheme != "https" and not (parts.scheme == "http" and is_local(url)):
        raise ValueError("Model endpoints require HTTPS, except local Ollama/LM Studio")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address and not address.is_global and not is_local(url):
        raise ValueError("Use a public endpoint or a loopback local model")
    return url.rstrip("/")


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "ollama"
    base_url: str = ""
    default_model: str = ""
    api_key: str = ""
    models: dict[str, str] = {}
    daily_model_limit: int = Field(default=1000, ge=0, le=10000)
    requests_per_minute: int = Field(default=60, ge=1, le=300)
    max_output_tokens: int = Field(default=4096, ge=512, le=16000)
    task_output_tokens: dict[str, Annotated[int, Field(ge=128, le=16000)]] = Field(
        default_factory=lambda: dict(TASK_OUTPUT_CAPS)
    )
    profile_excluded_terms: list[str] = Field(default_factory=list, max_length=50)
    allow_cloud_email: bool = False
    local_thinking: bool = False
    brave_api_key: str = ""
    search_provider: Literal["exa", "brave"] = "exa"
    discovery_limit: int = Field(default=50, ge=1, le=100)
    discovery_search_limit: int = Field(default=12, ge=1, le=50)
    discovery_company_limit: int = Field(default=100, ge=1, le=500)
    search_requests_per_minute: int = Field(default=20, ge=1, le=120)
    web_requests_per_minute: int = Field(default=240, ge=1, le=1000)
    domain_requests_per_minute: int = Field(default=20, ge=1, le=60)
    discovery_enabled: bool = False
    discovery_interval_hours: int = Field(default=4, ge=1, le=24)
    skill_dir: str = str(Path.home() / ".agents/skills/no-ai-slop")
    input_price_per_million: float = Field(default=0, ge=0, le=1000)
    output_price_per_million: float = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def validate_settings(self):
        if set(self.task_output_tokens) - TASK_OUTPUT_CAPS.keys():
            raise ValueError("Unknown task response limit")
        self.task_output_tokens = {**TASK_OUTPUT_CAPS, **self.task_output_tokens}
        if self.provider not in PRESETS:
            raise ValueError("Choose ollama, gemini or custom")
        self.base_url = valid_endpoint(self.base_url or PRESETS[self.provider]["base_url"])
        self.default_model = self.default_model or PRESETS[self.provider]["default_model"]
        if self.provider == "gemini" and self.base_url != PRESETS["gemini"]["base_url"]:
            raise ValueError(
                "Gemini preset uses the official AI Studio endpoint; choose custom for another provider"
            )
        if self.provider == "ollama" and any(
            "cloud" in m.lower() for m in [self.default_model, *self.models.values()]
        ):
            raise ValueError("The local preset requires downloaded weights, not an Ollama cloud proxy model")
        return self


class SettingsStore:
    def __init__(self, state):
        self.state = Path(state).resolve()
        self.path = self.state / "settings.json"

    def load(self):
        with _LOCK:
            if self.path.exists():
                return RuntimeSettings.model_validate_json(self.path.read_text())
            return RuntimeSettings()

    def update(self, changes):
        with _LOCK:
            old = self.load()
            data = old.model_dump()
            changes = dict(changes)
            if changes.get("provider", old.provider) != old.provider:
                for key in ["base_url", "default_model", "api_key", "models"]:
                    data[key] = {} if key == "models" else ""
            for secret in ["api_key", "brave_api_key"]:
                if changes.get(secret) == "":
                    changes.pop(secret)
            data.update(changes)
            result = RuntimeSettings.model_validate(data)
            save_token(self.path, result.model_dump_json(indent=2))
            throughput = [
                "daily_model_limit",
                "requests_per_minute",
                "search_requests_per_minute",
                "web_requests_per_minute",
                "domain_requests_per_minute",
            ]
            if self.state.joinpath("pipeline.db").exists() and any(
                getattr(result, key) > getattr(old, key) for key in throughput
            ):
                from .db import Store
                from .models import now

                store = Store(self.state / "pipeline.db")
                store.execute(
                    "update jobs set due_at=?,lease_until=NULL where status='PENDING' and (lower(coalesce(error,'')) like '%budget%' or id like 'discover-retry:%')",
                    (now().isoformat(),),
                )
            return self.public()

    def public(self):
        data = self.load().model_dump()
        for key in ["api_key", "brave_api_key"]:
            data[key + "_present"] = bool(data.pop(key))
        data["profile_preferences_present"] = bool(data.pop("profile_excluded_terms"))
        overrides = [
            name
            for name in os.environ
            if name.startswith("PIPELINE_MODEL_")
            or name
            in {"PIPELINE_LLM_BASE_URL", "PIPELINE_LLM_API_KEY", "BRAVE_SEARCH_API_KEY", "NO_AI_SLOP_DIR"}
        ]
        data["environment_overrides"] = sorted(overrides)
        return data
