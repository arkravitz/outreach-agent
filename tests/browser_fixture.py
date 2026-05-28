"""Explicit offline browser QA host. All model/Google behavior stays synthetic.
Run as: python tests/browser_fixture.py --state /tmp/... --port 8766.
Never use this helper to connect real accounts or to start a real worker.
"""

import argparse
import json
from pathlib import Path
import httpx
from conftest import Model
from pipeline.models import Campaign, Seed, Evidence
from pipeline.runtime import build as original_build
from pipeline.settings import SettingsStore
import pipeline.web_api as module
from pipeline.web import create_server


def fixture_build(state):
    app = original_build(state)

    def handler(request):
        from pipeline.providers import INSTRUCTIONS

        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-local"}]})
        if request.url.path == "/v1beta/interactions":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "steps": [
                        {
                            "type": "google_search_result",
                            "result": [
                                {
                                    "search_suggestions": '<div id="google-search-suggestions">Google search suggestions</div>'
                                }
                            ],
                        },
                        {
                            "type": "model_output",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Synthetic grounded answer.",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://example.org/source",
                                            "title": "Synthetic source",
                                            "start_index": 0,
                                            "end_index": 25,
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                    "usage": {
                        "total_input_tokens": 100,
                        "total_output_tokens": 50,
                        "total_thought_tokens": 10,
                    },
                },
            )
        request_data = json.loads(request.content)
        system = request_data["messages"][0]["content"]
        task = next(task for task, instructions in INSTRUCTIONS.items() if system.startswith(instructions))
        payload = json.loads(request_data["messages"][1]["content"])
        result = Model().complete(task, payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    app.model.client = httpx.Client(transport=httpx.MockTransport(handler))
    return app


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--state", required=True)
    args.add_argument("--port", type=int, default=8766)
    config = args.parse_args()
    SettingsStore(config.state).update(
        {"provider": "ollama", "default_model": "fixture-local", "requests_per_minute": 120}
    )
    app = fixture_build(config.state)
    for file in Path("config").glob("*.json"):
        campaign = Campaign.model_validate_json(file.read_text())
        campaign.prospecting_goal = "PAID_FIT"
        campaign.auto_draft = False
        campaign.sender_name = "Synthetic QA Sender"
        campaign.sender_email = "qa@example.com"
        campaign.postal_address = "123 Synthetic Example St, Example City, CA 90001"
        app.configure(campaign)
    seed = Seed(
        company="Example Agency",
        domain="example.org",
        email="sam@example.org",
        name="Sam",
        role="Owner",
        source_url="https://example.org/contact",
        verification="published",
        evidence=[
            Evidence(
                url="https://example.org/services",
                quote="We process customer documents.",
                text="Example Agency. We process customer documents. Sam is the owner. sam@example.org.",
            )
        ],
    )
    app.prepare("ai_automation", seed)
    # Presence-only synthetic token enables approval UI. No provider transport can use it.
    Path(config.state, "google-token.json").write_text('{"fixture_only":true}')
    import pipeline.research as research_module

    research_module.search = lambda *args, **kwargs: ["https://discovery.example/services"]
    discovery_seed = seed.model_copy(deep=True)
    discovery_seed.domain = "discovery.example"
    discovery_seed.email = "sam@discovery.example"
    discovery_seed.source_url = "https://discovery.example/contact"
    discovery_seed.region_reviewed = False
    discovery_seed.evidence = [
        Evidence(
            url="https://discovery.example/services",
            quote="We process customer documents.",
            text="Example Agency. We process customer documents. Sam is the owner. sam@discovery.example.",
        )
    ]
    research_module.LeadResearcher.research_company = lambda self, url: discovery_seed
    relationship = app.campaign("ai_engineering")
    relationship.prospecting_goal = "RELATIONSHIPS"
    app.configure(relationship)
    learner = discovery_seed.model_copy(deep=True)
    learner.email, learner.domain, learner.name = "sam@learning.example", "learning.example", "Sam Learner"
    app.prepare(relationship.id, learner)
    app.store.execute("""insert into discovery_candidates(campaign_id,domain,url,status,reason,updated_at)
        values ('ai_engineering','missing.example','https://missing.example','NEEDS_CONTACT','No suitable published email found','2026-10-18T00:00:00+00:00')""")
    module.build = fixture_build
    server = create_server(config.state, config.port)
    original_post = server.api.post

    def fixture_post(path, data, origin, session):
        if path == "/api/fixture/confirm-sends":
            from pipeline.models import now

            # Simulate provider-confirmed history in the isolated fixture; no mail transport runs.
            app.store.execute(
                "update messages set status='SENT',sent_at=? where id in "
                "(select id from messages where status='APPROVED' order by id limit ?)",
                (now().isoformat(), int(data.get("limit", 100000))),
            )
            return {"ok": True}
        return original_post(path, data, origin, session)

    server.api.post = fixture_post
    # Batch sending exercises the real approval route while keeping process launch inert.
    server.api.worker.start = lambda: {"running": True, "fixture_only": True}
    print(f"Offline browser fixture at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
