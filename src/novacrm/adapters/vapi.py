"""Vapi adapter: place an outbound phone call with our assistant, then read the finished call.

`POST /call` with the assistant, the phone number and the AE's number; `GET /call/{id}` until
`status` is `ended`. The `endedReason` decides whether the AE was reached; the transcript comes
from the call's messages (`user` is the AE, `bot`/`assistant` is our assistant).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..domain import CallResult, CallStatus, Turn
from .http import request

SYSTEM = "vapi"
API = "https://api.vapi.ai"

_NOT_REACHED: dict[str, CallStatus] = {
    "customer-did-not-answer": CallStatus.NO_ANSWER,
    "call.forwarding.no-answer": CallStatus.NO_ANSWER,
    "silence-timed-out": CallStatus.NO_ANSWER,
    "customer-busy": CallStatus.BUSY,
    "call.forwarding.operator-busy": CallStatus.BUSY,
    "voicemail": CallStatus.VOICEMAIL,
}


class VapiCaller:
    def __init__(
        self,
        api_key: str,
        assistant_id: str,
        phone_number_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=20.0)
        self._client.headers.update({"Authorization": f"Bearer {api_key}"})
        self._assistant = assistant_id
        self._number = phone_number_id

    def check(self) -> None:
        """Read-only: the assistant and the phone number exist."""
        self.check_assistant()
        request(self._client, SYSTEM, "GET", f"{API}/phone-number/{self._number}")

    def check_assistant(self) -> None:
        request(self._client, SYSTEM, "GET", f"{API}/assistant/{self._assistant}")

    def place_call(self, phone: str, variables: dict[str, str]) -> str:
        body = {
            "assistantId": self._assistant,
            "phoneNumberId": self._number,
            "customer": {"number": phone},
            "assistantOverrides": {"variableValues": variables},
            "metadata": {"deal": variables.get("deal", "")},
        }
        response = request(self._client, SYSTEM, "POST", f"{API}/call", json=body)
        return str(response.json()["id"])

    def get_result(self, call_id: str) -> CallResult | None:
        call: dict[str, Any] = request(self._client, SYSTEM, "GET", f"{API}/call/{call_id}").json()
        if call.get("status") != "ended":
            return None
        reason = str(call.get("endedReason", ""))
        turns = _turns(call)
        if reason in _NOT_REACHED:
            return CallResult(call_id, _NOT_REACHED[reason], turns, reason)
        if not any(t.speaker == "ae" for t in turns):
            status = (
                CallStatus.FAILED
                if "error" in reason or "failed" in reason
                else CallStatus.NO_ANSWER
            )
            return CallResult(call_id, status, turns, reason)
        return CallResult(call_id, CallStatus.ANSWERED, turns, reason)


def _turns(call: dict[str, Any]) -> tuple[Turn, ...]:
    messages = (call.get("artifact") or {}).get("messages") or call.get("messages") or []
    turns = []
    for message in messages:
        role, text = message.get("role"), (message.get("message") or "").strip()
        if not text:
            continue
        if role == "user":
            turns.append(Turn("ae", text))
        elif role in ("bot", "assistant"):
            turns.append(Turn("assistant", text))
    return tuple(turns)
