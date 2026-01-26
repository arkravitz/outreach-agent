"""Supported Gmail REST operations and incremental, tracked-thread-only synchronization."""

import base64
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from .models import now
import re
from .outbound import SafeRetry, AmbiguousSend

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class Gmail:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def mime(message):
        email = EmailMessage()
        email["From"] = formataddr((message["from_name"], message["from"]))
        email["To"] = message["to"]
        email["Subject"] = message["subject"]
        email["Message-ID"] = message["rfc_id"]
        if message.get("reply_to"):
            email["In-Reply-To"] = message["reply_to"]
            email["References"] = message["reply_to"]
        email.set_content(message["body"])
        result = {"raw": base64.urlsafe_b64encode(email.as_bytes()).decode()}
        if message.get("thread_id"):
            result["threadId"] = message["thread_id"]
        return result

    def get(self, path, **params):
        response = self.client.get(BASE + path, params=params)
        response.raise_for_status()
        return response.json()

    def send(self, message):
        # NO automatic transport retries for POST. 5xx may follow an accepted send.
        try:
            response = self.client.post(BASE + "/messages/send", json=self.mime(message))
        except Exception as exc:
            raise AmbiguousSend(type(exc).__name__) from exc
        reasons = set()
        if response.status_code == 403:
            try:
                reasons = {item.get("reason") for item in response.json()["error"].get("errors", [])}
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        if response.status_code == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
            retry_after = response.headers.get("Retry-After", "")
            delay = None
            try:
                delay = max(0, int(retry_after))
            except ValueError:
                try:
                    delay = max(0, (parsedate_to_datetime(retry_after) - now()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
            raise SafeRetry("Gmail rate limit", retry_after_seconds=delay)
        if response.status_code >= 400:
            raise AmbiguousSend(f"Gmail HTTP {response.status_code}; requires review")
        return response.json()

    def draft(self, message):
        response = self.client.post(BASE + "/drafts", json={"message": self.mime(message)})
        response.raise_for_status()
        return response.json()

    def find_sent(self, rfc_id):
        data = self.get("/messages", q="in:sent rfc822msgid:" + rfc_id.strip("<>"), maxResults=2)
        found = data.get("messages", [])
        if len(found) > 1:
            raise ValueError("Multiple provider messages share Message-ID; inspect manually")
        return found[0] if found else None

    def threads(self, thread_id):
        return self.get("/threads/" + thread_id, format="full").get("messages", [])


def decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def payload_text(payload):
    if payload.get("mimeType") in {"text/plain", "message/delivery-status"} and payload.get("body", {}).get(
        "data"
    ):
        return decode(payload["body"]["data"])[:100000]
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        from .research import Text

        parser = Text()
        parser.feed(decode(payload["body"]["data"]))
        return " ".join(parser.parts)[:100000]
    parts = payload.get("parts", [])
    plain = [p for p in parts if p.get("mimeType") in {"text/plain", "message/delivery-status"}]
    texts = [payload_text(p) for p in (plain or parts)]
    return "\n".join(t for t in texts if t)[:100000]


def parse_message(message):
    if {"SENT", "DRAFT"} & set(message.get("labelIds", [])):
        return None
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body = payload_text(payload)
    # Treat as bounce only for actual DSN MIME structure and permanent failure indication.
    types = {p.get("mimeType") for p in payload.get("parts", [])}
    bounce = (
        "multipart/report" == payload.get("mimeType")
        and "message/delivery-status" in types
        and bool(re.search(r"(?i)\b(5\.\d\.\d|550|permanent|address not found)\b", body))
    )
    reply_to = headers.get("in-reply-to", "")
    if not reply_to:
        reply_to = next(iter(re.findall(r"<[^<>\s]+>", headers.get("references", ""))[::-1]), "")
    if bounce and not reply_to:
        original_id = re.search(r"(?im)^Original-Message-ID:\s*(<[^<>\s]+>)", body)
        if original_id:
            reply_to = original_id.group(1)
        for part in payload.get("parts", []):
            if part.get("mimeType") == "message/rfc822":
                for original in [part, *part.get("parts", [])]:
                    for header in original.get("headers", []):
                        if header["name"].lower() == "message-id":
                            reply_to = header["value"]
    if not body:
        body = "[Non-text message: manual review required]"
    return {
        "id": message["id"],
        "thread_id": message.get("threadId"),
        "sender": headers.get("from", ""),
        "body": body,
        "in_reply_to": reply_to,
        "bounce": bounce,
        "rfc_id": headers.get("message-id", ""),
    }


class GmailSync:
    def __init__(self, app, gmail):
        self.app = app
        self.gmail = gmail

    def consume(self, message):
        event = parse_message(message)
        if event:
            self.app.ingest(event)

    def inspect_candidate(self, message, tracked):
        if message.get("threadId") in tracked:
            self.consume(self.gmail.get("/messages/" + message["id"], format="full"))
            return
        # Metadata only for unrelated mail. Read a body only after matching a known RFC reference.
        meta = self.gmail.get(
            "/messages/" + message["id"],
            format="metadata",
            metadataHeaders=["From", "In-Reply-To", "References", "Content-Type"],
        )
        if {"SENT", "DRAFT"} & set(meta.get("labelIds", [])):
            return
        headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
        references = re.findall(
            r"<[^<>\s]+>", headers.get("in-reply-to", "") + " " + headers.get("references", "")
        )
        relevant = any(
            self.app.store.one(
                "select id from messages where rfc_id=? and status in ('SENT','SENDING','UNKNOWN')", (ref,)
            )
            for ref in references
        )
        if relevant:
            full = self.gmail.get("/messages/" + message["id"], format="full")
            event = parse_message(full)
            if event:
                self.app.ingest(event)

    def has_outreach(self):
        return bool(
            self.app.store.one("select id from messages where status in ('SENT','SENDING','UNKNOWN') limit 1")
        )

    def recover(self):
        if not self.has_outreach():
            return
        # Capture BEFORE recovery so arrivals during recovery remain in the next history window.
        checkpoint = self.gmail.get("/profile")["historyId"]
        for row in self.app.store.all("select thread_id from contacts where thread_id is not null"):
            for message in self.gmail.threads(row["thread_id"]):
                self.consume(message)
        # Expired history may include replies Gmail assigned a fresh thread. Scan only a bounded
        # recovery window of metadata; never read unrelated bodies or silently truncate recovery.
        if self.has_outreach():
            tracked = {
                r["thread_id"]
                for r in self.app.store.all("select thread_id from contacts where thread_id is not null")
            }
            token = None
            for _ in range(10):
                params = {"q": "newer_than:30d -in:sent -in:drafts", "maxResults": 100}
                if token:
                    params["pageToken"] = token
                data = self.gmail.get("/messages", **params)
                for message in data.get("messages", []):
                    if not self.app.store.one("select id from inbound where id=?", (message["id"],)):
                        self.inspect_candidate(message, tracked)
                token = data.get("nextPageToken")
                if not token:
                    break
            if token:
                raise RuntimeError(
                    "Gmail recovery exceeds 1000 messages; manual sync required, checkpoint preserved"
                )
        self.checkpoint(checkpoint)

    def checkpoint(self, value):
        self.app.store.execute(
            "insert into sync_state values ('gmail_history',?) on conflict(key) do update set value=excluded.value",
            (str(value),),
        )

    def poll(self):
        if not self.has_outreach():
            return
        row = self.app.store.one("select value from sync_state where key='gmail_history'")
        if not row:
            return self.recover()
        tracked = {
            r["thread_id"]
            for r in self.app.store.all("select thread_id from contacts where thread_id is not null")
        }
        token = None
        checkpoint = row["value"]
        while True:
            params = {"startHistoryId": row["value"], "historyTypes": "messageAdded", "maxResults": 100}
            if token:
                params["pageToken"] = token
            try:
                data = self.gmail.get("/history", **params)
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                    return self.recover()
                raise
            for change in data.get("history", []):
                for item in change.get("messagesAdded", []):
                    message = item["message"]
                    if not self.app.store.one("select id from inbound where id=?", (message["id"],)):
                        self.inspect_candidate(message, tracked)
            checkpoint = data.get("historyId", checkpoint)
            token = data.get("nextPageToken")
            if not token:
                break
        # Advance only after every page has been durably ingested. Repeats dedupe by Gmail ID.
        self.checkpoint(checkpoint)
