"""Provenance-preserving public-web research, bounded fetches and free Exa or optional Brave discovery."""

from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import httpx
from .models import Evidence, Seed, now
from .outbound import SafeRetry
from .prompt_context import prospect_campaign, feedback_context, research_excerpts, suitable_business_email

USER_AGENT = "PipelineResearch/0.1"


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignore = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.ignore += 1
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value and not value.startswith(("mailto:", "javascript:", "tel:")):
                    self.links.append(value)
                if name == "href" and value and value.startswith("mailto:"):
                    self.parts.append(value[7:].split("?")[0])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.ignore = max(0, self.ignore - 1)

    def handle_data(self, data):
        if not self.ignore:
            self.parts.append(data)


class PublicWeb:
    def __init__(self, store=None, client=None):
        self.store = store
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
        )

    @staticmethod
    def validate(url):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise ValueError("Only public HTTPS URLs on port 443 are allowed")
        try:
            addresses = [ipaddress.ip_address(parts.hostname)]
        except ValueError:
            if parts.hostname in {"localhost", "metadata.google.internal"} or "." not in parts.hostname:
                raise ValueError("Private hostname blocked")
            try:
                addresses = [
                    ipaddress.ip_address(x[4][0])
                    for x in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
                ]
            except socket.gaierror:
                raise ValueError("Cannot resolve public source hostname") from None
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Non-public destination blocked")
        return parts

    def download(self, url, robots=None):
        origin = urlsplit(url).netloc
        for _ in range(4):
            self.validate(url)
            if robots is not None:
                if urlsplit(url).netloc != origin:
                    raise ValueError("Cross-host redirect requires independent source/robots review")
                if not robots.can_fetch(USER_AGENT, url):
                    raise ValueError("Redirect blocked by source robots.txt")
            with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                if not any(
                    t in response.headers.get("content-type", "") for t in ["text/", "application/xhtml"]
                ):
                    raise ValueError("Research only supports public text/HTML")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError("Research page exceeds 1 MB limit")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        raise ValueError("Too many redirects")

    def fetch(self, url):
        parts = self.validate(url)
        if self.store:
            cached = self.store.one("select * from research_cache where url=?", (url,))
            if cached and datetime.fromisoformat(cached["fetched_at"]) > now() - timedelta(days=7):
                return cached["text"]
        if self.store:
            from .limits import RateLimiter

            from .settings import SettingsStore

            limits = SettingsStore(self.store.path.parent).load()
            RateLimiter(self.store).take("public_web", limit=limits.web_requests_per_minute)
            RateLimiter(self.store).take(
                "web_domain:" + parts.hostname, limit=limits.domain_requests_per_minute
            )
        robots_url = f"https://{parts.netloc}/robots.txt"
        try:
            rules = self.download(robots_url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                rules = ""
            else:
                raise ValueError("Cannot establish source crawling policy") from exc
        robot = RobotFileParser()
        robot.parse(rules.splitlines())
        if not robot.can_fetch(USER_AGENT, url):
            raise ValueError("Source robots.txt prohibits retrieval")
        html = self.download(url, robots=robot)
        parser = Text()
        parser.feed(html)
        text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:50000]
        links = []
        for href in parser.links:
            linked = urljoin(url, href).split("#")[0].split("?")[0]
            target = urlsplit(linked)
            if (
                target.scheme == "https"
                and target.hostname == parts.hostname
                and any(
                    word in target.path.lower()
                    for word in (
                        "contact",
                        "team",
                        "people",
                        "about",
                        "service",
                        "solution",
                        "product",
                        "leadership",
                    )
                )
                and linked not in links
            ):
                links.append(linked)
        text += "\n" + "\n".join(f"[Company page: {link}]" for link in links[:20])
        if self.store:
            self.store.execute(
                "insert into research_cache values (?,?,?) on conflict(url) do update set text=excluded.text,fetched_at=excluded.fetched_at",
                (url, text, now().isoformat()),
            )
        return text


class LeadResearcher:
    def __init__(self, web, model, campaign=None, feedback=None, contact_search=None):
        self.web, self.model = web, model
        self.campaign, self.feedback = campaign, feedback or []
        self.contact_search = contact_search

    @staticmethod
    def published_emails(text):
        return {
            e.lower()
            for e in re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
        }

    def research(self, url):
        return self.extract([(url, self.web.fetch(url))])

    @staticmethod
    def suitable_email(email):
        return suitable_business_email(email)

    def research_company(self, url):
        pages = [(url, self.web.fetch(url))]
        parts = urlsplit(url)
        root = f"https://{parts.netloc}"
        linked = re.findall(r"\[Company page: (https://[^\]\s]+)\]", pages[0][1])
        # Use real links before bounded fallback paths; retain services/team evidence even
        # when the landing page already includes an address.
        linked.sort(
            key=lambda link: (
                0
                if any(
                    word in urlsplit(link).path.lower()
                    for word in ("team", "people", "contact", "leadership")
                )
                else 1
            )
        )
        pending = linked + [root + path for path in ("/contact", "/contact-us", "/about", "/services")]
        seen = {url}
        for next_url in pending:
            if len(seen) >= 6:
                break
            if next_url in seen or urlsplit(next_url).hostname != parts.hostname:
                continue
            seen.add(next_url)
            try:
                body = self.web.fetch(next_url)
            except SafeRetry:
                if any(self.suitable_email(e) for _, page in pages for e in self.published_emails(page)):
                    break
                raise
            except (ValueError, httpx.HTTPError):
                continue
            pages.append((next_url, body))
            # Actual links discovered on secondary pages can reveal the decision maker.
            for child in re.findall(r"\[Company page: (https://[^\]\s]+)\]", body):
                if child not in pending and child not in seen:
                    pending.append(child)
        if self.contact_search and not any(
            self.suitable_email(e) for _, body in pages for e in self.published_emails(body)
        ):
            for target in self.contact_search(parts.hostname)[:2]:
                if target in seen or urlsplit(target).hostname != parts.hostname:
                    continue
                try:
                    pages.append((target, self.web.fetch(target)))
                except (ValueError, httpx.HTTPError):
                    continue
        if not any(self.suitable_email(e) for _, body in pages for e in self.published_emails(body)):
            raise ValueError("No suitable published email found on the checked company pages")
        return self.extract(pages)

    def extract(self, pages):
        keywords = prospect_campaign(self.campaign).get("keywords", [])
        text = "\n\n".join(f"SOURCE: {url}\n{research_excerpts(body, keywords)}" for url, body in pages)
        result = self.model.complete(
            "research",
            {
                "source_url": pages[0][0],
                "text": text,
                "campaign": prospect_campaign(self.campaign),
                "operator_feedback": feedback_context(self.feedback),
                "untrusted_data": True,
            },
        )
        for field in ["email", "company", "quote", "name", "role"]:
            if result.get(field) is None:
                result[field] = ""
            if not isinstance(result[field], str):
                raise ValueError("Research fields must be text or empty when unknown")
        email = result["email"].lower()
        email_page = next(
            ((url, body) for url, body in pages if email and email in self.published_emails(body)), None
        )
        if not self.suitable_email(email):
            raise ValueError("No suitable published contact address selected; hiring/legal inboxes excluded")
        if email_page is None:
            raise ValueError("No valid published contact address; never guess")
        quote = result.get("quote", "")
        evidence_page = next(((url, body) for url, body in pages if quote in body), None)
        if evidence_page is None or len(quote) < 10:
            raise ValueError("Research quote missing from source")
        for field in ["name", "role"]:
            if result.get(field) and not any(
                result[field].casefold() in body.casefold() for _, body in pages
            ):
                result[field] = ""
        if not result.get("company") or not any(
            result["company"].casefold() in body.casefold() for _, body in pages
        ):
            raise ValueError("Company name is not grounded in the source")
        evidence = [Evidence(url=evidence_page[0], quote=quote, text=evidence_page[1])]
        # Preserve the source of the actual published address, not merely a business quote.
        if email_page[0] != evidence_page[0] or email not in quote.lower():
            start = email_page[1].lower().index(email)
            email_quote = email_page[1][max(0, start - 35) : start + len(email) + 35]
            evidence.append(Evidence(url=email_page[0], quote=email_quote, text=email_page[1]))
        for source, body in pages:
            if len(evidence) >= 4:
                break
            if source not in {e.url for e in evidence}:
                excerpt = body.split("[Company page:", 1)[0].strip()[:600]
                if len(excerpt) >= 10:
                    evidence.append(Evidence(url=source, quote=excerpt, text=body))
        return Seed(
            company=result["company"],
            domain=urlsplit(pages[0][0]).hostname.lower().removeprefix("www."),
            email=email,
            name=result.get("name", ""),
            role=result.get("role", ""),
            source_url=email_page[0],
            provenance="Published company contact; source policy and recipient review required",
            verification="published",
            evidence=evidence,
        )


def search(query, limit=5, client=None, api_key=None, provider="exa"):
    if provider == "exa":
        client = client or httpx.Client(timeout=30)
        response = client.post(
            "https://mcp.exa.ai/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "web_search_exa",
                    "arguments": {"query": query, "numResults": max(1, min(limit, 10))},
                },
            },
        )
        if response.status_code == 429:
            raise ValueError("Exa free search rate limit reached; wait and retry or import company URLs")
        response.raise_for_status()
        if "text/event-stream" in response.headers.get("content-type", ""):
            events = [
                json.loads(block.split("data:", 1)[1].strip())
                for block in response.text.replace("\r\n", "\n").split("\n\n")
                if "data:" in block
            ]
            data = next((event for event in events if event.get("id") == 1), {})
        else:
            data = response.json()
        result = data.get("result", {})
        if data.get("error") or result.get("isError"):
            raise ValueError(
                "Exa free search unavailable or rate limited; try later. No paid fallback was used"
            )
        if "content" not in result:
            raise ValueError("Exa returned an unexpected search response; try again later")
        urls = []
        for content in result.get("content", []):
            for url in re.findall(r"^URL: (https://[^\s]+)", content.get("text", ""), re.MULTILINE):
                if url not in urls:
                    urls.append(url)
        return urls[: max(1, min(limit, 10))]
    if provider != "brave":
        raise ValueError("Choose Exa or Brave search")
    key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        raise RuntimeError("Set BRAVE_SEARCH_API_KEY or import sourced prospect JSON with pipeline prepare")
    client = client or httpx.Client(timeout=15)
    response = client.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": min(limit, 10)},
    )
    response.raise_for_status()
    return [
        r["url"] for r in response.json().get("web", {}).get("results", []) if r["url"].startswith("https://")
    ]


def discover(app, campaign_id):
    from .discovery import discover as run

    return run(app, campaign_id)
