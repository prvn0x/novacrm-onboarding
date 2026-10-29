"""Gmail adapter: the CS inbox, read by label (design D7), replies in the same thread.

New mail = inbox messages from the last 7 days WITHOUT the `novacrm/processed` label, excluding
mail the inbox sent itself. Read/unread is ignored on purpose: the inbox is shared.
Auth is OAuth for an installed app (`novacrm gmail-auth` once); the token lives in `var/`.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from ..domain import InboundEmail
from ..ports import PermanentError, TransientError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
LABEL = "novacrm/processed"
QUERY = f"in:inbox -label:{LABEL.replace('/', '-')} newer_than:7d -from:me"


def authorize(credentials_file: str, token_file: str) -> Any:
    """Load the saved token, refreshing it, or run the browser consent flow once."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = Path(token_file)
    creds = (
        Credentials.from_authorized_user_file(str(token), SCOPES)  # type: ignore[no-untyped-call]
        if token.exists()
        else None
    )
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(
            credentials_file, SCOPES
        ).run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    token.chmod(0o600)
    return creds


class GmailInbox:
    def __init__(self, credentials_file: str, token_file: str = "var/gmail-token.json") -> None:
        from googleapiclient.discovery import build

        self._api = build(
            "gmail",
            "v1",
            credentials=authorize(credentials_file, token_file),
            cache_discovery=False,
        )
        self._label_id = self._ensure_label()

    def _run(self, call: Any) -> Any:
        from googleapiclient.errors import HttpError

        try:
            return call.execute(num_retries=2)
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            kind = TransientError if status == 429 or status >= 500 else PermanentError
            raise kind("gmail", f"{status} {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise TransientError("gmail", str(exc)) from exc

    def _ensure_label(self) -> str:
        labels = self._run(self._api.users().labels().list(userId="me")).get("labels", [])
        for label in labels:
            if label["name"] == LABEL:
                return str(label["id"])
        body = {"name": LABEL, "labelListVisibility": "labelShow"}
        return str(self._run(self._api.users().labels().create(userId="me", body=body))["id"])

    def fetch_new(self) -> list[InboundEmail]:
        listing = self._run(self._api.users().messages().list(userId="me", q=QUERY, maxResults=25))
        emails = []
        for ref in reversed(listing.get("messages", [])):  # oldest first
            message = self._run(self._api.users().messages().get(userId="me", id=ref["id"]))
            if self._label_id in message.get("labelIds", []):
                continue
            thread = self._run(self._api.users().threads().get(userId="me", id=message["threadId"]))
            headers = _headers(message)
            emails.append(
                InboundEmail(
                    message_id=message["id"],
                    thread_id=message["threadId"],
                    sender=parseaddr(headers.get("from", ""))[1].lower(),
                    subject=headers.get("subject", ""),
                    body=_text(message["payload"]),
                    received_at=datetime.fromtimestamp(int(message["internalDate"]) / 1000, UTC),
                    # The agent's own replies are not part of what the AE wrote.
                    thread_text="\n\n---\n\n".join(
                        f"Subject: {_headers(m).get('subject', '')}\n\n{_text(m['payload'])}"
                        for m in thread.get("messages", [])
                        if "SENT" not in m.get("labelIds", [])
                    ),
                )
            )
        return emails

    def mark_processed(self, message_id: str) -> None:
        body = {"addLabelIds": [self._label_id]}
        self._run(self._api.users().messages().modify(userId="me", id=message_id, body=body))

    def reply(self, thread_id: str, to: str, subject: str, body: str) -> None:
        thread = self._run(self._api.users().threads().get(userId="me", id=thread_id))
        last = _headers(thread["messages"][-1])
        mime = EmailMessage()
        mime["To"] = to
        mime["Subject"] = subject
        if last.get("message-id"):
            mime["In-Reply-To"] = last["message-id"]
            mime["References"] = (last.get("references", "") + " " + last["message-id"]).strip()
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        send = {"raw": raw, "threadId": thread_id}
        self._run(self._api.users().messages().send(userId="me", body=send))


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _text(payload: dict[str, Any]) -> str:
    """The text/plain body (the first one found, depth-first)."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _text(part)
        if text:
            return text
    return ""
