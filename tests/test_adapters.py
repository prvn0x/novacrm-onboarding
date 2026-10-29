"""Request-shape tests for the real adapters, against a mock HTTP transport (no network).

Rocketlane gets the most: the trial account is scarce, and a payload bug there is costly.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from novacrm.adapters.http import request
from novacrm.adapters.rocketlane import RocketlaneProjects
from novacrm.adapters.vapi import VapiCaller
from novacrm.domain import CallStatus
from novacrm.ports import PermanentError, ProjectGone, ProjectSpec, TaskSpec, TransientError

PROJECT = {
    "projectId": 5001,
    "projectName": "Acme Corp - Enterprise onboarding",
    "externalReferenceId": "006Ka00000AbCdEIAV",
    "sources": [{"templateId": 201, "templateName": "Enterprise Onboarding - 30 day"}],
    "plannedDurationInDays": 30,
    "owner": {"emailId": "priya@novacrm.example"},
    "teamMembers": {
        "members": [{"emailId": "asha@novacrm.example"}],
        "customers": [{"emailId": "john.doe@acme.com"}],
    },
    "createdAt": 1793181600000,
}


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.responses.pop(0)


def _rocketlane(*responses: httpx.Response) -> tuple[RocketlaneProjects, Recorder]:
    recorder = Recorder(list(responses))
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return RocketlaneProjects("rl-test-key", client=client), recorder


def _json(req: httpx.Request) -> Any:
    return json.loads(req.content)


def test_create_project_sends_the_template_people_and_reference_and_reads_it_back() -> None:
    api, rec = _rocketlane(
        httpx.Response(201, json={"projectId": 5001}), httpx.Response(200, json=PROJECT)
    )
    spec = ProjectSpec(
        name="Acme Corp - Enterprise onboarding",
        customer_name="Acme Corp",
        contact_email="john.doe@acme.com",
        owner_email="priya@novacrm.example",
        csm_email="asha@novacrm.example",
        template_id=201,
        pm_placeholder_id=11,
        customer_placeholder_id=12,
        start_date=date(2026, 10, 29),
        external_reference_id="006Ka00000AbCdEIAV",
    )
    info = api.create_project(spec)

    post, get = rec.requests
    assert post.method == "POST" and post.url == "https://api.rocketlane.com/api/1.0/projects"
    assert post.headers["api-key"] == "rl-test-key"
    body = _json(post)
    assert body["sources"] == [{"templateId": 201, "startDate": "2026-10-29"}]
    assert body["startDate"] == "2026-10-29" and "dueDate" not in body  # Rocketlane computes it
    assert body["externalReferenceId"] == "006Ka00000AbCdEIAV"
    assert body["owner"] == {"emailId": "priya@novacrm.example"}
    assert body["customer"] == {"companyName": "Acme Corp"} and body["autoCreateCompany"] is True
    assert body["teamMembers"]["customers"] == [{"emailId": "john.doe@acme.com"}]
    assert body["placeholders"] == [
        {"placeholderId": 11, "user": {"emailId": "asha@novacrm.example"}},
        {"placeholderId": 12, "user": {"emailId": "john.doe@acme.com"}},
    ]
    assert get.url.path == "/api/1.0/projects/5001"
    assert "sources" in get.url.params["includeFields"]

    assert info.template_names == ("Enterprise Onboarding - 30 day",)
    assert info.planned_duration_days == 30
    assert {"asha@novacrm.example", "john.doe@acme.com"} <= info.member_emails


def test_the_duplicate_lookups_use_the_documented_filters() -> None:
    page = {"data": [PROJECT], "pagination": {"hasMore": False}}
    api, rec = _rocketlane(httpx.Response(200, json=page), httpx.Response(200, json=page))
    assert api.find_by_reference("006Ka00000AbCdEIAV") is not None
    assert len(api.find_by_name("Acme Corp")) == 1
    by_ref, by_name = (r.url.params for r in rec.requests)
    assert by_ref["externalReferenceId.eq"] == "006Ka00000AbCdEIAV"
    assert by_ref["includeArchive.eq"] == "true"
    assert by_name["projectName.cn"] == "Acme Corp"


def test_list_endpoints_follow_every_page() -> None:
    first = {
        "data": [{"taskId": 1, "taskName": "a"}],
        "pagination": {"hasMore": True, "nextPageToken": "t2"},
    }
    second = {
        "data": [
            {
                "taskId": 2,
                "taskName": "b",
                "assignees": {"members": [{"emailId": "john.doe@acme.com"}]},
            }
        ],
        "pagination": {"hasMore": False},
    }
    api, rec = _rocketlane(httpx.Response(200, json=first), httpx.Response(200, json=second))
    tasks = api.list_tasks(5001)
    assert [t.task_id for t in tasks] == [1, 2]
    assert tasks[1].assignee_emails == {"john.doe@acme.com"}
    assert rec.requests[0].url.params["projectId.eq"] == "5001"
    assert rec.requests[1].url.params["pageToken"] == "t2"


def test_phases_are_listed_with_the_required_project_parameter() -> None:
    body = {"data": [{"phaseId": 7, "phaseName": "Kickoff"}], "pagination": {"hasMore": False}}
    api, rec = _rocketlane(httpx.Response(200, json=body))
    (phase,) = api.list_phases(5001)
    assert (phase.phase_id, phase.name) == (7, "Kickoff")
    assert rec.requests[0].url.params["projectId"] == "5001"


def test_create_task_sends_project_phase_assignee_and_html_description() -> None:
    api, rec = _rocketlane(httpx.Response(201, json={"taskId": 9001}))
    task_id = api.create_task(
        TaskSpec(
            5001,
            7,
            "Kickoff call with John Doe",
            "Notes: <b>SSO</b> & reports",
            "asha@novacrm.example",
            date(2026, 11, 1),
        )
    )
    assert task_id == 9001
    body = _json(rec.requests[0])
    assert body["project"] == {"projectId": 5001} and body["phase"] == {"phaseId": 7}
    assert body["assignees"] == {"members": [{"emailId": "asha@novacrm.example"}]}
    assert body["dueDate"] == "2026-11-01"
    assert body["taskDescription"] == "<p>Notes: &lt;b&gt;SSO&lt;/b&gt; &amp; reports</p>"


def test_a_deleted_project_is_reported_as_gone() -> None:
    api, _ = _rocketlane(httpx.Response(404, json={"errors": [{"errorCode": "NOT_FOUND"}]}))
    with pytest.raises(ProjectGone):
        api.get_project(5001)


@pytest.mark.parametrize(
    ("status", "kind"), [(429, TransientError), (503, TransientError), (400, PermanentError)]
)
def test_http_statuses_map_to_retry_or_not(status: int, kind: type[Exception]) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    with pytest.raises(kind):
        request(client, "x", "GET", "https://example.test/")


def test_a_timeout_is_transient() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(TransientError):
        request(client, "x", "GET", "https://example.test/")


@pytest.mark.parametrize(
    ("reason", "messages", "status"),
    [
        ("customer-did-not-answer", [], CallStatus.NO_ANSWER),
        ("customer-busy", [], CallStatus.BUSY),
        ("voicemail", [{"role": "bot", "message": "Hi"}], CallStatus.VOICEMAIL),
        (
            "assistant-ended-call",
            [
                {"role": "bot", "message": "Enterprise or Growth?"},
                {"role": "user", "message": "Growth"},
            ],
            CallStatus.ANSWERED,
        ),
    ],
)
def test_vapi_ended_reasons_map_to_call_outcomes(
    reason: str, messages: list[dict[str, str]], status: CallStatus
) -> None:
    call = {
        "id": "c1",
        "status": "ended",
        "endedReason": reason,
        "artifact": {"messages": [{"role": "system", "message": "prompt"}, *messages]},
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=call)))
    result = VapiCaller("k", "a", "p", client=client).get_result("c1")
    assert result is not None and result.status is status
    assert all(t.text != "prompt" for t in result.turns)


def test_a_vapi_call_in_progress_has_no_result_yet() -> None:
    call = {"id": "c1", "status": "in-progress"}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=call)))
    assert VapiCaller("k", "a", "p", client=client).get_result("c1") is None


def test_an_unanswered_browser_call_ends_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # VOICE_MODE=web: a page nobody answers stops "ringing", as a phone would.
    from novacrm.adapters import webcall
    from novacrm.domain import CallStatus

    monkeypatch.setattr(webcall, "RING_SECONDS", 0.0)
    monkeypatch.setattr(webcall.webbrowser, "open", lambda url: True)
    caller = webcall.WebCaller("key", "assistant")
    monkeypatch.setattr(caller, "_serve", lambda: None)
    call_id = caller.place_call("+15550100", {"customer_name": "Acme Corp"})
    result = caller.get_result(call_id)
    assert result is not None and result.status is CallStatus.NO_ANSWER
