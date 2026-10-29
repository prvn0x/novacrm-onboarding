"""Gemini adapter (Generative Language API, `generateContent`) behind the `LLM` port.

JSON calls use `responseMimeType: application/json` with a `responseSchema`, at temperature 0.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..ports import TransientError
from .http import request

SYSTEM = "gemini"
API = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiLLM:
    def __init__(
        self, api_key: str, model: str = "gemini-3.5-flash-lite", client: httpx.Client | None = None
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._client.headers.update({"x-goog-api-key": api_key})
        self._url = f"{API}/{model}:generateContent"

    def _generate(self, system: str, user: str, config: dict[str, Any]) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, **config},
        }
        data = request(self._client, SYSTEM, "POST", self._url, json=body).json()
        try:
            return str(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError) as exc:
            raise TransientError(SYSTEM, f"no text in the response ({exc})") from exc

    def complete_json(self, task: str, system: str, user: str, schema: dict[str, Any]) -> Any:
        config = {"responseMimeType": "application/json", "responseSchema": _gemini_schema(schema)}
        text = self._generate(system, user, config)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise TransientError(SYSTEM, f"invalid JSON from the model ({exc})") from exc

    def complete_text(self, task: str, system: str, user: str) -> str:
        return self._generate(system, user, {"maxOutputTokens": 120})


def _gemini_schema(schema: Any) -> Any:
    """Gemini's schema dialect spells types in capitals (OBJECT, STRING, ...)."""
    if isinstance(schema, dict):
        return {
            k: (v.upper() if k == "type" and isinstance(v, str) else _gemini_schema(v))
            for k, v in schema.items()
        }
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema
