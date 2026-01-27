"""Operator-only Google grounded answers, never an input to discovery or drafting."""

from html import escape
from urllib.parse import urlsplit

VIEWER_CSP = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data: https://www.google.com https://www.gstatic.com; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; sandbox allow-popups allow-popups-to-escape-sandbox allow-top-navigation-by-user-activation"


def render_research(data):
    blocks, suggestions = [], []
    searched = any(
        step.get("type") in {"google_search_call", "google_search_result"} for step in data.get("steps", [])
    )
    for step in data.get("steps", []):
        if step.get("type") == "google_search_result":
            for result in step.get("result", []):
                if result.get("search_suggestions"):
                    suggestions.append(result["search_suggestions"])
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []):
            if content.get("type") != "text":
                continue
            text = content.get("text", "")
            blocks.append('<p style="white-space:pre-wrap">' + escape(text) + "</p>")
            for citation in content.get("annotations", []):
                if citation.get("type") != "url_citation":
                    continue
                url = citation.get("url", "")
                parts = urlsplit(url)
                if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                    continue
                start, end = citation.get("start_index", 0), citation.get("end_index", 0)
                span = (
                    text[start:end]
                    if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
                    else ""
                )
                blocks.append(
                    "<p>"
                    + (escape(span) + " — " if span else "")
                    + '<a rel="noreferrer noopener" href="'
                    + escape(url, quote=True)
                    + '">'
                    + escape(citation.get("title") or url)
                    + "</a></p>"
                )
    # Suggestions are Google's own display fragment, unchanged. This top-level viewer
    # has an opaque sandbox origin, no scripts/forms/frames, and no app data or tracking.
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Google grounded research · Pipeline</title></head>'
        '<body style="font:17px/1.6 system-ui;max-width:850px;margin:40px auto;padding:0 24px">'
        "<h1>Google grounded research</h1><p>For your review. This answer does not enter automated discovery, lead records or email drafts.</p>"
        + ("" if searched else "<p>Gemini did not report a Google search for this answer.</p>")
        + "".join(blocks)
        + "".join(suggestions)
        + "</body></html>"
    )
