import json
import httpx
from pipeline.providers import LanguageModel
import pytest
from conftest import SKILL_IS_STUB


def test_drafting_and_review_receive_only_task_relevant_context(setup, seed):
    app, model, _, campaign = setup
    campaign.approved_facts = ["I build AI tools.", "I teach practical software engineering."]
    app.configure(campaign)
    app.prepare(campaign.id, seed)
    qualify = next(p for t, p in model.calls if t == "qualify")
    write = next(p for t, p in model.calls if t == "write")
    ground = next(p for t, p in model.calls if t == "ground")
    for payload in [qualify, write, ground]:
        assert "daily_limit" not in payload["campaign"]
        assert "calendar_id" not in payload["campaign"]
        assert "fetched_at" not in str(payload["recipient"])
        assert "legal_basis" not in payload["recipient"]
        assert payload["untrusted_data"]
    assert "approved_facts" not in qualify["campaign"]
    assert write["campaign"]["approved_facts"]
    assert "instructions" not in ground and "campaign_hash" not in ground


@pytest.mark.skipif(SKILL_IS_STUB, reason="requires the installed no-ai-slop skill")
def test_editor_profile_uses_installed_rules_without_interactive_handbook(setup, seed):
    app, model, _, _ = setup
    app.prepare("automation", seed)
    edit = next(p for t, p in model.calls if t == "edit")
    original = (app.writer.skill_dir / "SKILL.md").read_text() + (
        app.writer.skill_dir / "eval.md"
    ).read_text()
    assert len(edit["skill"] + edit["evaluation"]) < len(original) * 0.5
    assert "Protect the specific fact" in edit["skill"]
    assert "Final read" in edit["evaluation"]
    assert "What to ask for" not in edit["skill"]
    assert "synergy" not in edit["skill"] or "Words to cut" in edit["skill"]


def test_response_token_caps_match_small_json_tasks():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        models={"qualify": "local", "write": "local", "ground": "local"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    for task in ["qualify", "write", "ground"]:
        model.complete(task, {"test": "value"})
    assert [r["max_tokens"] for r in requests] == [384, 768, 512]
    assert len(requests[0]["messages"][0]["content"]) < 500
    assert '": "' not in requests[0]["messages"][1]["content"]


def test_research_excerpts_keep_contact_at_end_of_long_page():
    from pipeline.research import LeadResearcher

    body = (
        "Example Agency. "
        + ("navigation " * 1500)
        + " We process customer documents. Contact Sam Owner sam@example.org"
    )

    class Model:
        def complete(self, task, payload):
            assert "sam@example.org" in payload["text"]
            assert "We process customer documents." in payload["text"]
            assert len(payload["text"]) < 5000
            return dict(
                company="Example Agency",
                name="Sam",
                role="Owner",
                email="sam@example.org",
                quote="We process customer documents.",
            )

    seed = LeadResearcher(None, Model()).extract([("https://example.org", body)])
    assert seed.email == "sam@example.org"
    assert seed.evidence[0].text == body


def test_research_excerpts_prefer_distinct_usable_addresses():
    from pipeline.prompt_context import research_excerpts

    body = (
        "Example Agency Founder Jane owner services provide develop workflow "
        + ("privacy@example.org " + "navigation " * 100) * 9
        + " Sam, sales@example.org. Enquiries."
    )
    excerpt = research_excerpts(body)
    assert "sales@example.org" in excerpt and "Sam" in excerpt


def test_truncated_json_gets_bounded_larger_retry_and_counts_both_calls(tmp_path):
    from pipeline.db import Store

    requests = []

    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "length", "message": {"content": '{"score":'}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 384},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"score":0.6,"confidence":0.8,"reason":"Relevant"}'},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
        )

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        models={"qualify": "local"},
        store=Store(tmp_path / "pipeline.db"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert model.complete("qualify", {})["score"] == 0.6
    assert [r["max_tokens"] for r in requests] == [384, 768]
    assert model.store.one("select count(*) n from model_usage")["n"] == 2


def test_per_task_output_cap_can_be_overridden():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    model = LanguageModel(
        base_url="http://127.0.0.1:11434/v1",
        models={"qualify": "local"},
        task_output_tokens={"qualify": 2048},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    model.complete("qualify", {})
    assert requests[0]["max_tokens"] == 2048


def test_editor_accepts_list_of_audit_notes_without_redrafting(setup, seed):
    app, model, _, _ = setup
    original = model.complete

    def notes(task, payload):
        result = original(task, payload)
        if task == "edit":
            result["changes"] = ["Kept the source fact.", "Shortened the request."]
        return result

    model.complete = notes
    contact = app.prepare("automation", seed)
    assert contact["state"] == "READY_FOR_OUTREACH"
    assert len([t for t, p in model.calls if t == "write"]) == 1
