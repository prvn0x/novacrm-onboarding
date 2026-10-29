"""Slack Web API adapter (bot token).

Scopes: channels:manage, channels:read, channels:join, channels:history, chat:write, users:read,
users:read.email. Calls are form-encoded, which every Web API method accepts. In production the
customer is invited with Slack Connect (`conversations.inviteShared`), which needs a paid
workspace; see the design doc, D8.
"""

from __future__ import annotations

import json
from functools import cached_property
from typing import Any

import httpx

from ..ports import ChannelInfo, NameTaken, PermanentError, TransientError
from .http import request

SYSTEM = "slack"
API = "https://slack.com/api/"
MARKER_EVENT = "novacrm_welcome"
_TRANSIENT = {
    "ratelimited",
    "internal_error",
    "fatal_error",
    "service_unavailable",
    "request_timeout",
}
_HARMLESS_INVITE = {"already_in_channel", "cant_invite_self"}


class SlackWorkspace:
    def __init__(self, bot_token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=20.0)
        self._client.headers.update({"Authorization": f"Bearer {bot_token}"})

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        form = {k: _form(v) for k, v in params.items() if v is not None}
        body: dict[str, Any] = request(self._client, SYSTEM, "POST", API + method, data=form).json()
        if not body.get("ok"):
            error = str(body.get("error", "unknown_error"))
            if error == "name_taken":
                raise NameTaken(str(params.get("name", "")))
            kind = TransientError if error in _TRANSIENT else PermanentError
            raise kind(SYSTEM, f"{method}: {error}")
        return body

    @cached_property
    def _bot_user(self) -> str:
        return str(self._call("auth.test")["user_id"])

    def create_channel(self, name: str, purpose: str) -> str:
        channel_id = str(self._call("conversations.create", name=name)["channel"]["id"])
        self.set_purpose(channel_id, purpose)
        return channel_id

    def set_purpose(self, channel_id: str, purpose: str) -> None:
        self._call("conversations.setPurpose", channel=channel_id, purpose=purpose)

    def find_channel(self, name: str) -> ChannelInfo | None:
        cursor = ""
        while True:
            body = self._call(
                "conversations.list",
                types="public_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor or None,
            )
            for channel in body.get("channels", []):
                if channel.get("name") == name:
                    return ChannelInfo(
                        channel["id"],
                        channel.get("creator") == self._bot_user,
                        (channel.get("purpose") or {}).get("value", ""),
                    )
            cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                return None

    def set_topic(self, channel_id: str, topic: str) -> None:
        self._call("conversations.setTopic", channel=channel_id, topic=topic[:250])

    def invite(self, channel_id: str, emails: list[str]) -> None:
        users = []
        for email in emails:
            try:
                users.append(self._call("users.lookupByEmail", email=email)["user"]["id"])
            except PermanentError as exc:
                if "users_not_found" not in str(exc):
                    raise
        if not users:
            return
        try:
            self._call("conversations.invite", channel=channel_id, users=",".join(users))
        except PermanentError as exc:
            if not any(e in str(exc) for e in _HARMLESS_INVITE):
                raise

    def post(self, channel: str, text: str, marker: str = "") -> str:
        metadata = (
            {"event_type": MARKER_EVENT, "event_payload": {"marker": marker}} if marker else None
        )
        body = self._call(
            "chat.postMessage",
            channel=self._resolve(channel),
            text=text,
            mrkdwn=True,
            metadata=metadata,
        )
        return str(body["ts"])

    def has_posted(self, channel_id: str, marker: str) -> bool:
        """Only our marked welcome counts: Slack's own join and topic messages do not."""
        body = self._call(
            "conversations.history", channel=channel_id, limit=100, include_all_metadata=True
        )
        for message in body.get("messages", []):
            meta = message.get("metadata") or {}
            if (
                meta.get("event_type") == MARKER_EVENT
                and (meta.get("event_payload") or {}).get("marker") == marker
            ):
                return True
        return False

    def _resolve(self, channel: str) -> str:
        """A channel id, or a channel name (the escalation channel) the bot joins first."""
        if channel[:1] in "CG" and channel[1:].isalnum() and channel.isupper():
            return channel
        found = self.find_channel(channel.lstrip("#"))
        if found is None:
            raise PermanentError(SYSTEM, f"channel #{channel} not found; create it first")
        self._call("conversations.join", channel=found.channel_id)
        return found.channel_id


def _form(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)
