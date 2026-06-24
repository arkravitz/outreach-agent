import pytest
from pipeline.limits import RateLimiter
from pipeline.outbound import SafeRetry


def test_provider_limit_is_persistent_across_runtime_restarts(setup):
    app, _, _, _ = setup
    RateLimiter(app.store).take("search", limit=2, seconds=60)
    RateLimiter(app.store).take("search", limit=2, seconds=60)
    with pytest.raises(SafeRetry, match="search"):
        RateLimiter(app.store).take("search", limit=2, seconds=60)
    RateLimiter(app.store).take("llm", limit=2, seconds=60)
