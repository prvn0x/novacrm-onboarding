"""Write docs/API_REQUESTS.md: every request the real adapters send, captured from the adapters
themselves against a recording transport (no network), so the document cannot drift from the code.

    uv run python scripts/api_examples.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

from novacrm.adapters.gemini import GeminiLLM
from novacrm.adapters.rocketlane import RocketlaneProjects
from novacrm.adapters.slack import SlackWorkspace
from novacrm.adapters.vapi import VapiCaller
from novacrm.agents.extraction import EXTRACTION_SCHEMA
from novacrm.ports import ProjectSpec, TaskSpec

OUT = Path("docs/API_REQUESTS.md")
OPP = "006Ka00000AbCdEIAV"


class Recorder:
    def __init__(self, reply: Callable[[httpx.Request], Any]) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(200, json=self.reply(req))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _render(title: str, req: httpx.Request, note: str = "") -> str:
    lines = [f"### {title}", ""]
    if note:
        lines += [note, ""]
    query = f"?{req.url.query.decode()}" if req.url.query else ""
    lines += ["```http", f"{req.method} {req.url.scheme}://{req.url.host}{req.url.path}{query}"]
    for name in ("api-key", "authorization", "x-goog-api-key"):
        if name in req.headers:
            lines.append(f"{name}: <{name.upper().replace('-', '_')}>")
    lines.append("```")
    if req.content:
        kind = req.headers.get("content-type", "")
        if kind.startswith("application/json"):
            body: Any = json.loads(req.content)
        else:
            body = dict(parse_qsl(req.content.decode()))
            if "metadata" in body:
                body["metadata"] = json.loads(body["metadata"])
        lines += ["", "```json", json.dumps(body, indent=2, ensure_ascii=False), "```"]
    return "\n".join(lines) + "\n"


def rocketlane() -> list[str]:
    def reply(req: httpx.Request) -> Any:
        path = req.url.path
        if req.method == "POST" and path.endswith("/projects"):
            return {"projectId": 5001}
        if req.method == "POST" and path.endswith("/phases"):
            return {"phaseId": 7}
        if req.method == "POST" and path.endswith("/tasks"):
            return {"taskId": 9001}
        if "/projects/" in path:
            return {"projectId": 5001, "projectName": "x"}
        return {"data": [], "pagination": {"hasMore": False}}

    rec = Recorder(reply)
    api = RocketlaneProjects("<ROCKETLANE_API_KEY>", client=rec.client())
    base = {
        "name": "Acme Corp - Enterprise onboarding",
        "customer_name": "Acme Corp",
        "contact_email": "john.doe@acme.com",
        "owner_email": "priya@novacrm.example",
        "csm_email": "asha@novacrm.example",
        "start_date": date(2026, 10, 29),
        "external_reference_id": OPP,
    }
    api.find_by_reference(OPP)
    api.find_by_name("Acme Corp")
    api.create_project(
        ProjectSpec(**base, template_id=201, pm_placeholder_id=11, customer_placeholder_id=12)  # type: ignore[arg-type]
    )
    api.create_project(ProjectSpec(**base, due_date=date(2026, 11, 28)))  # type: ignore[arg-type]
    api.list_phases(5001)
    api.list_tasks(5001)
    api.create_phase(5001, "Kickoff", date(2026, 10, 29), date(2026, 11, 3))
    api.create_task(
        TaskSpec(
            5001,
            7,
            "Kickoff call with John Doe",
            "AE: Rahul Mehta. Notes: pipeline reports.",
            "asha@novacrm.example",
            date(2026, 11, 1),
            date(2026, 10, 30),
        )
    )
    api.add_dependency(9013, 9008)
    titles = [
        ("Step 4: is there a project for this opportunity? (archived ones included)", ""),
        ("Step 4: is there a project for this customer made by hand?", ""),
        (
            "Step 7, template mode: create the project from the template",
            "Rocketlane computes the due date from the template, so none is sent.",
        ),
        ("(read back after create)", ""),
        ("Step 7, api mode: create an empty project with explicit dates", ""),
        ("(read back after create)", ""),
        ("Step 8: list the phases (`projectId` is a required parameter)", ""),
        (
            "Step 8: list the tasks, with their phase, assignees and dependencies",
            "These are optional fields in v1.3 and must be requested.",
        ),
        ("api mode: create a phase", ""),
        (
            "Step 7: create the deal's kickoff task (both modes; api mode creates all 15 this way)",
            "",
        ),
        (
            "api mode: go-live readiness depends on the sign-off",
            "The path task starts after the listed tasks are complete.",
        ),
    ]
    return [_render(t, r, n) for (t, n), r in zip(titles, rec.requests, strict=True)]


def slack() -> list[str]:
    def reply(req: httpx.Request) -> Any:
        return {
            "ok": True,
            "channel": {"id": "C0001"},
            "user": {"id": "U0002"},
            "ts": "1.0",
            "user_id": "UBOT",
            "messages": [],
            "channels": [],
        }

    rec = Recorder(reply)
    chat = SlackWorkspace("<SLACK_BOT_TOKEN>", client=rec.client())
    chat.create_channel("onb-acme-corp", f"NovaCRM onboarding for Salesforce opportunity {OPP}")
    chat.set_topic("C0001", "Enterprise onboarding · 30 days (29 Oct → go-live 28 Nov) · ...")
    chat.invite("C0001", ["asha@novacrm.example"])
    chat.has_posted("C0001", OPP)
    chat.post("C0001", "We're thrilled to ...\n\nHi John, I'm Asha Rao, ...", marker=OPP)
    titles = [
        "Create the channel",
        "Mark it with the opportunity (its purpose)",
        "Set the topic",
        "Find the CSM's Slack user",
        "Invite the CSM and the AE",
        "Was this deal's welcome already posted? (after a crash)",
        "Post the welcome, marked with the opportunity",
    ]
    note = "Slack Web API calls are form-encoded. Production adds `conversations.inviteShared`."
    return [
        _render(t, r, note if i == 0 else "")
        for i, (t, r) in enumerate(zip(titles, rec.requests, strict=True))
    ]


def vapi() -> list[str]:
    rec = Recorder(
        lambda req: {"id": "call_123", "status": "ended", "endedReason": "assistant-ended-call"}
    )
    voice = VapiCaller(
        "<VAPI_API_KEY>", "<VAPI_ASSISTANT_ID>", "<VAPI_PHONE_NUMBER_ID>", client=rec.client()
    )
    voice.place_call(
        "+919800000001", {"customer_name": "Acme Corp", "ae_first_name": "Rahul", "deal": "D1"}
    )
    voice.get_result("call_123")
    return [
        _render(
            "Step 5: phone the AE",
            rec.requests[0],
            "The assistant itself is defined in "
            "config/vapi_assistant.json (`novacrm setup-voice`).",
        ),
        _render(
            "Step 5: read the finished call (status, endedReason, transcript)", rec.requests[1]
        ),
    ]


def gemini() -> list[str]:
    rec = Recorder(lambda req: {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})
    llm = GeminiLLM("<GEMINI_API_KEY>", client=rec.client())
    llm.complete_json(
        "extract",
        "You extract facts from an email ...",
        "<email>\n...\n</email>",
        EXTRACTION_SCHEMA,
    )
    return [_render("Step 3: extract the deal fields (schema-bound)", rec.requests[0])]


def main() -> None:
    parts = [
        "# API requests\n",
        "Every request the system sends, generated from the adapters by "
        "`scripts/api_examples.py`. Keys are placeholders; values are the sample deal "
        "(Acme Corp, opportunity `" + OPP + "`). Gmail goes through Google's client library "
        "(query `in:inbox -label:novacrm-processed newer_than:7d -from:me`, then label and reply "
        "in the thread).\n",
        "## Rocketlane\n",
        *rocketlane(),
        "## Slack\n",
        *slack(),
        "## Vapi\n",
        *vapi(),
        "## Gemini\n",
        *gemini(),
    ]
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
