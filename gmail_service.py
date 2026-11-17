import os
import base64
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import SERVICE_DOMAINS

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())


def _decode_body(data):
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(data + padding)
    return raw.decode("utf-8", errors="replace")


def _collect_bodies(payload):
    plain, html = [], []
    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = _decode_body(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for child in part.get("parts", []) or []:
            walk(child)
    walk(payload or {})
    if plain:
        return "\n\n".join(plain)
    if html:
        parser = _HTMLText()
        parser.feed("\n".join(html))
        return "\n".join(parser.parts)
    return "(This email has no readable text body.)"

def _redact_authentication_codes(text):
    # Mask likely one-time authentication codes before showing email previews.
    patterns = [
        r"(?i)(\b(?:verification|security|login|one[- ]time|authentication)\s+code\b\s*[:：-]?\s*)([A-Z0-9][A-Z0-9 -]{3,11})",
        r"(?i)(\bcode\s*(?:is|:|：)\s*)([A-Z0-9][A-Z0-9 -]{3,11})",
        r"(?<!\d)\d{4,8}(?!\d)",
    ]
    text = re.sub(patterns[0], r"\1[REDACTED]", text)
    text = re.sub(patterns[1], r"\1[REDACTED]", text)
    text = re.sub(patterns[2], "[REDACTED]", text)
    return text.strip()


def gmail_client():
    required = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing Gmail OAuth environment variables: " + ", ".join(missing))
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def search_latest_service_email(service_name: str, requested_address: str):
    if service_name not in SERVICE_DOMAINS:
        raise ValueError("Unsupported service.")
    domains = SERVICE_DOMAINS[service_name]
    sender_query = " OR ".join(f"from:{domain}" for domain in domains)
    query = f"to:{requested_address} ({sender_query})"
    client = gmail_client()
    try:
        result = client.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
        messages = result.get("messages", [])
        if not messages:
            return None

        msg = client.users().messages().get(
            userId="me", id=messages[0]["id"], format="full"
        ).execute()
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        internal_ms = int(msg.get("internalDate", "0"))
        received = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
        body = _redact_authentication_codes(
            _collect_bodies(msg.get("payload", {}))
        )
        return {
            "id": msg["id"],
            "from": f"{sender_name} <{sender_email}>" if sender_name else sender_email,
            "subject": headers.get("subject", "(no subject)"),
            "date": received.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "body": body[:2800],
            "truncated": len(body) > 2800,
            "gmail_url": f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
        }
    except HttpError as exc:
        raise RuntimeError(f"Gmail API error: {exc}") from exc
