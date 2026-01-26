"""Persistent, diverse company discovery; each pass obeys the shared cost limits."""

from datetime import datetime, timedelta
import json
import os
import uuid
from urllib.parse import urlsplit
from .models import now
from .settings import SettingsStore
from .limits import RateLimiter
from .prompt_context import prospect_campaign, feedback_context
from .outbound import SafeRetry

LOCATIONS = (
    "California",
    "Texas",
    "New York",
    "Florida",
    "Illinois",
    "Washington",
    "Colorado",
    "Massachusetts",
    "North Carolina",
    "Georgia",
    "Ohio",
    "Oregon",
    "Pennsylvania",
    "Arizona",
    "Michigan",
    "Minnesota",
    "Virginia",
    "Tennessee",
    "Wisconsin",
    "Maryland",
    "Utah",
    "Missouri",
    "Indiana",
    "New Jersey",
)
ANGLES = (
    "owner founder team services",
    "new services growing team",
    "operations workflow client onboarding",
    "training workshop technical help",
    "integration software product launch",
    "small independent business contact",
)


def next_query(app, campaign):
    """Reserve the next exploration slot transactionally, even when results repeat."""
    key = "discovery_cursor:" + campaign.id
    with app.store.tx() as db:
        row = db.execute("select value from sync_state where key=?", (key,)).fetchone()
        cursor = int(row[0]) if row else 0
        db.execute(
            "insert into sync_state values (?,?) on conflict(key) do update set value=excluded.value",
            (key, str(cursor + 1)),
        )
    bases = list(campaign.discovery_queries)
    good = app.store.all(
        """select co.name from prospect_feedback f join contacts c on c.id=f.contact_id
        join companies co on co.id=c.company_id where f.verdict='GOOD' and c.campaign_id=?
        order by f.updated_at desc limit 3""",
        (campaign.id,),
    )
    bases += [f"US companies similar to {r['name']} {' '.join(campaign.keywords[:3])}" for r in good]
    if not bases:
        raise ValueError("Add discovery queries in campaign settings first")
    base = bases[cursor % len(bases)]
    offset = cursor // len(bases)
    query = f"{base} {LOCATIONS[offset % len(LOCATIONS)]} {ANGLES[(offset // len(LOCATIONS)) % len(ANGLES)]}"
    # Recent known domains reduce wasted top results; local dedupe is authoritative.
    domains = app.store.all(
        """select domain from (
        select domain,updated_at from discovery_candidates where campaign_id=? and next_at>?
        union all select domain,updated_at from contacts where campaign_id=?
        ) group by domain order by max(updated_at) desc limit 20""",
        (campaign.id, now().isoformat(), campaign.id),
    )
    query += "".join(" -site:" + r["domain"] for r in domains)
    return query


def discover(app, campaign_id):
    from . import research

    campaign = app.campaign(campaign_id)
    settings = SettingsStore(app.store.path.parent).load()
    key = "discovery_lease:" + campaign_id
    owner = uuid.uuid4().hex
    with app.store.tx() as db:
        lease = db.execute("select * from executions where id=?", (key,)).fetchone()
        if (
            lease
            and lease["status"] == "RUNNING"
            and lease["next_at"]
            and datetime.fromisoformat(lease["next_at"]) > now()
        ):
            raise SafeRetry("Discovery already running for this campaign")
        db.execute(
            """insert into executions(id,step,status,result,next_at,updated_at)
            values (?,'discovery','RUNNING',?,?,?) on conflict(id) do update set status='RUNNING',
            result=excluded.result,next_at=excluded.next_at,updated_at=excluded.updated_at""",
            (key, owner, (now() + timedelta(minutes=15)).isoformat(), now().isoformat()),
        )
    report = dict(
        campaign_id=campaign_id,
        status="RUNNING",
        started_at=now().isoformat(),
        queries=0,
        candidates=0,
        duplicates=0,
        skipped=0,
        qualified=0,
        contacts=0,
        retained=0,
        target=settings.discovery_limit,
        pool_target=campaign.pool_target,
        provider=settings.search_provider,
        query_details=[],
        stop_reason="",
    )
    results, seen = [], set()
    researcher = research.LeadResearcher(
        research.PublicWeb(app.store),
        app.model,
        prospect_campaign(campaign),
        feedback_context(
            app.store.all(
                "select verdict,note from prospect_feedback f join contacts c on c.id=f.contact_id where c.campaign_id=? order by f.updated_at desc limit 6",
                (campaign_id,),
            )
        ),
    )

    def contact_search(domain):
        if report["queries"] >= settings.discovery_search_limit:
            return []
        RateLimiter(app.store).take("search", limit=settings.search_requests_per_minute)
        query = f"site:{domain} owner founder team leadership contact email"
        report["queries"] += 1
        app.store.execute(
            "insert into discovery_queries(campaign_id,query,created_at) values (?,?,?)",
            (campaign_id, query, now().isoformat()),
        )
        return research.search(
            query,
            limit=3,
            api_key=os.environ.get("BRAVE_SEARCH_API_KEY", settings.brave_api_key),
            provider=settings.search_provider,
        )

    researcher.contact_search = contact_search

    def save():
        app.store.execute(
            "insert into sync_state values (?,?) on conflict(key) do update set value=excluded.value",
            ("discovery_report:" + campaign_id, json.dumps(report)),
        )

    try:
        save()
        pool = app.store.one(
            "select count(*) n from contacts where campaign_id=? and state not in ('REJECTED','SUPPRESSED','LOST','BOUNCED')",
            (campaign_id,),
        )["n"]
        if pool >= campaign.pool_target:
            report["stop_reason"] = "Pool goal reached"
        else:
            # Increase breadth on duplicate-only batches, while keeping each click bounded.
            for _ in range(settings.discovery_search_limit):
                if report["queries"] >= settings.discovery_search_limit:
                    break
                if (
                    report["contacts"] >= settings.discovery_limit
                    or pool + report["contacts"] >= campaign.pool_target
                ):
                    report["stop_reason"] = "Pass or pool target reached"
                    break
                RateLimiter(app.store).take("search", limit=settings.search_requests_per_minute)
                query = next_query(app, campaign)
                qid = app.store.execute(
                    "insert into discovery_queries(campaign_id,query,created_at) values (?,?,?)",
                    (campaign_id, query, now().isoformat()),
                )
                detail = dict(query=query, candidates=0, contacts=0, duplicates=0)
                report["query_details"].append(detail)
                urls = research.search(
                    query,
                    limit=10,
                    api_key=os.environ.get("BRAVE_SEARCH_API_KEY", settings.brave_api_key),
                    provider=settings.search_provider,
                )
                report["queries"] += 1
                due = app.store.all(
                    "select url from discovery_candidates where campaign_id=? and contact_id is null and next_at<=? order by next_at limit 5",
                    (campaign_id, now().isoformat()),
                )
                urls = [r["url"] for r in due] + urls
                for url in urls:
                    if (
                        report["contacts"] >= settings.discovery_limit
                        or report["candidates"] >= settings.discovery_company_limit
                    ):
                        break
                    parts = urlsplit(url)
                    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                        continue
                    domain = parts.hostname.lower().removeprefix("www.")
                    if domain in seen:
                        continue
                    seen.add(domain)
                    candidate = app.store.one(
                        "select * from discovery_candidates where campaign_id=? and domain=?",
                        (campaign_id, domain),
                    )
                    known = app.store.one("select id from companies where domain=?", (domain,))
                    if known or (
                        candidate
                        and candidate["next_at"]
                        and datetime.fromisoformat(candidate["next_at"]) > now()
                    ):
                        report["duplicates"] += 1
                        detail["duplicates"] += 1
                        continue
                    if domain in campaign.blocked_domains:
                        report["skipped"] += 1
                        continue
                    if domain in {
                        "linkedin.com",
                        "facebook.com",
                        "adapt.io",
                        "zoominfo.com",
                        "apollo.io",
                        "yelp.com",
                    }:
                        report["skipped"] += 1
                        continue
                    app.store.execute(
                        """insert into discovery_candidates(campaign_id,domain,url,query,updated_at)
                        values (?,?,?,?,?) on conflict(campaign_id,domain) do update set url=excluded.url,
                        query=excluded.query,updated_at=excluded.updated_at""",
                        (campaign_id, domain, url, query, now().isoformat()),
                    )
                    report["candidates"] += 1
                    detail["candidates"] += 1
                    status, reason, contact_id = "NEEDS_RESEARCH", "", None
                    try:
                        seed = researcher.research_company(url)
                        existing = app.store.one(
                            "select id from contacts where email=? or domain=?", (seed.email, seed.domain)
                        )
                        if existing:
                            contact_id, status = existing["id"], "DUPLICATE"
                            report["duplicates"] += 1
                            detail["duplicates"] += 1
                        else:
                            contact = app.prepare(campaign_id, seed)
                            contact_id, status = contact["id"], "CONTACT_FOUND"
                            report["contacts"] += 1
                            detail["contacts"] += 1
                            if contact["state"] in {"QUALIFIED", "READY_FOR_OUTREACH"}:
                                report["qualified"] += 1
                            if contact["state"] == "NEEDS_RESEARCH":
                                report["retained"] += 1
                            results.append(
                                dict(id=contact_id, state=contact["state"], source=seed.source_url)
                            )
                    except SafeRetry as exc:
                        status, reason = "DEFERRED", str(exc)
                        raise
                    except (ValueError, research.httpx.HTTPError) as exc:
                        report["skipped"] += 1
                        reason = (
                            str(exc)[:200]
                            if isinstance(exc, ValueError)
                            else "Source retrieval failed; retry later"
                        )
                        status = (
                            "NEEDS_CONTACT"
                            if "published" in reason.lower() or "suitable" in reason.lower()
                            else "SOURCE_UNAVAILABLE"
                        )
                        app.store.audit(
                            "research.skipped", domain, dict(error=type(exc).__name__, reason=reason)
                        )
                    finally:
                        # Short cooldown on transient failures; longer on exhausted email research.
                        delay = 7 if status in {"NEEDS_CONTACT", "CONTACT_FOUND", "DUPLICATE"} else 1
                        app.store.execute(
                            """update discovery_candidates set status=?,reason=?,contact_id=?,
                            attempts=attempts+1,next_at=?,updated_at=? where campaign_id=? and domain=?""",
                            (
                                status,
                                reason,
                                contact_id,
                                (
                                    now()
                                    + (
                                        timedelta(minutes=5)
                                        if status == "DEFERRED"
                                        else timedelta(days=delay)
                                    )
                                ).isoformat(),
                                now().isoformat(),
                                campaign_id,
                                domain,
                            ),
                        )
                        save()
                app.store.execute(
                    "update discovery_queries set candidates=?,contacts=?,duplicates=? where id=?",
                    (detail["candidates"], detail["contacts"], detail["duplicates"], qid),
                )
                if report["candidates"] >= settings.discovery_company_limit:
                    report["stop_reason"] = "Company research budget reached"
                    break
        report["status"] = "DONE"
        if not report["stop_reason"]:
            report["stop_reason"] = "Search budget reached; next pass uses new queries"
        return results
    except SafeRetry as exc:
        report["status"], report["stop_reason"] = "PAUSED", str(exc)
        due = now() + timedelta(minutes=1)
        if "daily" in str(exc).lower():
            due = (now() + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        app.store.enqueue(
            "discover-retry:" + campaign_id + ":" + due.strftime("%Y%m%d%H%M"),
            "discover",
            {"campaign_id": campaign_id},
            due_at=due,
        )
        return results
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = (
            str(exc)[:240] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        )
        raise
    finally:
        report["finished_at"] = now().isoformat()
        save()
        app.store.execute(
            "update executions set status='DONE',next_at=NULL where id=? and result=?", (key, owner)
        )
