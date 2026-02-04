"""Standalone runtime wiring. Read-only CLI commands do not need credentials."""

import os
from pathlib import Path
from .db import Store
from .providers import LanguageModel
from .service import Pipeline


class LazyGmail:
    def __init__(self, token_file):
        self.token_file = token_file
        self._provider = None

    def __getattr__(self, name):
        if self._provider is None:
            from .auth import google_client
            from .gmail import Gmail

            if not Path(self.token_file).exists():
                raise RuntimeError("Google OAuth token missing. Run pipeline auth --client-file PATH")
            self._provider = Gmail(google_client(self.token_file))
        return getattr(self._provider, name)


def build(state=None):
    state = Path(state or os.environ.get("PIPELINE_STATE", "state")).resolve()
    from .limits import RateLimiter

    store = Store(state / "pipeline.db")
    from .settings import SettingsStore
    from .providers import INSTRUCTIONS

    settings = SettingsStore(state).load()
    model = LanguageModel(
        base_url=os.environ.get("PIPELINE_LLM_BASE_URL", settings.base_url),
        api_key=os.environ.get("PIPELINE_LLM_API_KEY", settings.api_key),
        models={
            task: os.environ.get(
                "PIPELINE_MODEL_" + task.upper(), settings.models.get(task, settings.default_model)
            )
            for task in INSTRUCTIONS
        },
        limiter=RateLimiter(store),
        store=store,
        daily_limit=settings.daily_model_limit,
        requests_per_minute=settings.requests_per_minute,
        allow_cloud_email=settings.allow_cloud_email,
        max_output_tokens=settings.max_output_tokens,
        task_output_tokens=settings.task_output_tokens,
        reasoning_effort="none" if settings.provider == "ollama" and not settings.local_thinking else None,
        local_only=settings.provider == "ollama",
    )
    return Pipeline(
        store,
        model,
        LazyGmail(os.environ.get("PIPELINE_GOOGLE_TOKEN", str(state / "google-token.json"))),
        profile_excluded_terms=settings.profile_excluded_terms,
        skill_dir=os.environ.get("NO_AI_SLOP_DIR", settings.skill_dir),
    )
