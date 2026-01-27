"""Per-provider rolling request budgets shared across worker restarts."""

from datetime import timedelta
from .models import now
from .outbound import SafeRetry


class RateLimiter:
    def __init__(self, store):
        self.store = store

    def take(self, provider, limit=30, seconds=60):
        with self.store.tx() as db:
            cutoff = (now() - timedelta(seconds=seconds)).isoformat()
            db.execute("delete from provider_events where provider=? and at<?", (provider, cutoff))
            count = db.execute(
                "select count(*) from provider_events where provider=?", (provider,)
            ).fetchone()[0]
            if count >= limit:
                raise SafeRetry(f"{provider} request budget exhausted")
            db.execute("insert into provider_events(provider,at) values (?,?)", (provider, now().isoformat()))
