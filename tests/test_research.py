import httpx
import pytest
from pipeline.research import PublicWeb, LeadResearcher
from pipeline.providers import LanguageModel


def test_public_web_rejects_local_and_non_https_urls():
    web = PublicWeb()
    for url in ["http://example.org", "https://127.0.0.1/admin", "https://localhost", "file:///etc/passwd"]:
        with pytest.raises(ValueError):
            web.validate(url)


def test_research_extracts_only_published_contact_and_verified_quote(monkeypatch):
    class Web:
        def fetch(self, url):
            return "Example Agency. We process customer documents. Contact Sam at sam@example.org."

    class Model:
        def complete(self, task, payload):
            return {
                "company": "Example Agency",
                "name": "Sam",
                "role": "",
                "quote": "We process customer documents.",
                "email": "sam@example.org",
            }

    result = LeadResearcher(Web(), Model()).research("https://example.org")
    assert result.email == "sam@example.org"
    assert result.verification == "published"
    assert not result.region_reviewed
    assert result.evidence[0].quote in result.evidence[0].text


def test_research_rejects_invented_email():
    class Web:
        def fetch(self, url):
            return "We process customer documents."

    class Model:
        def complete(self, task, payload):
            return {
                "company": "Example",
                "name": "CEO",
                "quote": "We process customer documents.",
                "email": "guessed@example.org",
            }

    with pytest.raises(ValueError, match="published"):
        LeadResearcher(Web(), Model()).research("https://example.org")


def test_llm_adapter_uses_actual_editor_skill_as_system_instruction():
    import json

    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert "Preserve personal voice" in payload["messages"][0]["content"]
        assert "Check edited draft" in payload["messages"][0]["content"]
        assert payload["model"] == "editor-model"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"body":"Edited","changes":"Cut filler","checks_passed":true}'}}
                ]
            },
        )

    model = LanguageModel(
        base_url="https://llm.example/v1",
        api_key="test",
        models={"edit": "editor-model"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = model.complete(
        "edit", {"skill": "Preserve personal voice", "evaluation": "Check edited draft", "draft": "Draft"}
    )
    assert result["checks_passed"] is True
    assert len(seen) == 1


def test_research_redirect_cannot_bypass_robots_policy(monkeypatch):
    from urllib.parse import urlsplit

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nDisallow: /restricted", headers={"content-type": "text/plain"}
            )
        if request.url.path == "/contact":
            return httpx.Response(302, headers={"location": "/restricted"})
        raise AssertionError("Restricted URL must never be fetched")

    web = PublicWeb(client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(web, "validate", lambda url: urlsplit(url))
    with pytest.raises(ValueError, match="robots"):
        web.fetch("https://example.org/contact")


def test_free_exa_search_parses_sse_without_credentials():
    import json
    from pipeline.research import search

    def handler(request):
        assert request.url == "https://mcp.exa.ai/mcp"
        assert not request.headers.get("Authorization")
        body = json.loads(request.content)
        assert body["params"]["name"] == "web_search_exa"
        result = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Title: Example\nURL: https://example.org/contact\nHighlights: other https://untrusted.example\nURL: http://unsafe.example",
                    }
                ]
            },
        }
        return httpx.Response(
            200,
            text="event: message\ndata: " + json.dumps(result) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    assert search("example", client=httpx.Client(transport=httpx.MockTransport(handler))) == [
        "https://example.org/contact"
    ]


def test_free_search_limit_never_falls_back_to_paid():
    from pipeline.research import search

    with pytest.raises(ValueError, match="free.*limit"):
        search("example", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))


def test_company_research_finds_contact_email_and_keeps_business_evidence():
    class Web:
        def fetch(self, url):
            if url == "https://example.org/services":
                return "Example Agency. We process customer documents for clients."
            if url == "https://example.org/contact":
                return "Example Agency. Contact Sam, Owner, at sam@example.org."
            raise ValueError("Page unavailable")

    class Model:
        def complete(self, task, payload):
            assert "sam@example.org" in str(payload)
            return {
                "company": "Example Agency",
                "name": "Sam",
                "role": "Owner",
                "quote": "We process customer documents for clients.",
                "email": "sam@example.org",
            }

    seed = LeadResearcher(Web(), Model()).research_company("https://example.org/services")
    assert seed.source_url == "https://example.org/contact"
    assert seed.evidence[0].url == "https://example.org/services"
    assert any("sam@example.org" in e.quote and e.url == "https://example.org/contact" for e in seed.evidence)
    assert not seed.region_reviewed


def test_company_research_without_published_email_never_calls_model():
    class Web:
        def fetch(self, url):
            return "Example Agency. We process customer documents for clients."

    class Model:
        def complete(self, *args):
            raise AssertionError("Do not spend model tokens guessing unpublished email addresses")

    with pytest.raises(ValueError, match="published"):
        LeadResearcher(Web(), Model()).research_company("https://example.org")


def test_unresolvable_company_is_a_handled_source_error(monkeypatch):
    import socket

    def unresolved(*args, **kwargs):
        raise socket.gaierror("Name not found")

    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    with pytest.raises(ValueError, match="resolve"):
        PublicWeb.validate("https://stale.example/contact")


def test_research_accepts_unknown_names_returned_as_null():
    class Web:
        def fetch(self, url):
            return "Example Agency. We process customer documents. info@example.org"

    class Model:
        def complete(self, *args):
            return {
                "company": "Example Agency",
                "name": None,
                "role": None,
                "quote": "We process customer documents.",
                "email": "info@example.org",
            }

    seed = LeadResearcher(Web(), Model()).research("https://example.org")
    assert seed.name == "" and seed.role == "" and seed.email == "info@example.org"


def test_company_research_follows_real_links_even_after_finding_generic_email():
    seen = []

    class Web:
        def fetch(self, url):
            seen.append(url)
            return {
                "https://example.org": "Example Agency. info@example.org\n[Company page: https://example.org/our-people]",
                "https://example.org/our-people": "Example Agency. Sam Owner. sam@example.org. We process customer documents.",
            }.get(url, "Example Agency. We process customer documents.")

    class Model:
        def complete(self, task, payload):
            assert "sam@example.org" in str(payload)
            return dict(
                company="Example Agency",
                email="sam@example.org",
                name="Sam",
                role="Owner",
                quote="We process customer documents.",
            )

    result = LeadResearcher(Web(), Model()).research_company("https://example.org")
    assert result.email == "sam@example.org" and "https://example.org/our-people" in seen


def test_company_research_rejects_hiring_inbox_even_when_model_selects_it():
    class Web:
        def fetch(self, url):
            return "Example Agency. We process customer documents. hiring@example.org"

    class Model:
        def complete(self, *args):
            return dict(
                company="Example Agency",
                email="hiring@example.org",
                name="",
                role="",
                quote="We process customer documents.",
            )

    with pytest.raises(ValueError, match="suitable|published"):
        LeadResearcher(Web(), Model()).research_company("https://example.org")


def test_public_web_retains_same_host_company_links_in_cached_text(tmp_path, monkeypatch):
    from pipeline.db import Store

    web = PublicWeb(Store(tmp_path / "test.db"))
    monkeypatch.setattr(
        web, "validate", lambda url: __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(url)
    )
    monkeypatch.setattr(
        web,
        "download",
        lambda url, **kwargs: (
            ""
            if url.endswith("robots.txt")
            else '<p>Example Agency.</p><a href="/our-people">Our team</a><a href="https://other.example/contact">Other site</a>'
        ),
    )
    text = web.fetch("https://example.org")
    assert "[Company page: https://example.org/our-people]" in text
    assert "[Company page: https://other.example/contact]" not in text


def test_optional_page_budget_does_not_discard_usable_research():
    from pipeline.outbound import SafeRetry

    class Web:
        def fetch(self, url):
            if url == "https://example.org":
                return "Example Agency. We process customer documents. sam@example.org"
            raise SafeRetry("public_web request budget exhausted")

    class Model:
        def complete(self, *args):
            return dict(
                company="Example Agency",
                email="sam@example.org",
                name="",
                role="",
                quote="We process customer documents.",
            )

    assert LeadResearcher(Web(), Model()).research_company("https://example.org").email == "sam@example.org"


def test_missing_contact_can_use_targeted_same_company_search():
    queries = []

    class Web:
        def fetch(self, url):
            return "Example Agency. We process customer documents." + (
                " sam@example.org" if url.endswith("/leadership") else ""
            )

    class Model:
        def complete(self, *args):
            return dict(
                company="Example Agency",
                email="sam@example.org",
                name="",
                role="",
                quote="We process customer documents.",
            )

    def targeted(domain):
        queries.append(domain)
        return ["https://example.org/leadership", "https://unrelated.example/contact"]

    assert (
        LeadResearcher(Web(), Model(), contact_search=targeted).research_company("https://example.org").email
        == "sam@example.org"
    )
    assert queries == ["example.org"]
