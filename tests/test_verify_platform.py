"""`novacrm verify-platform` against a scripted Rocketlane: it runs, records and cleans up."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import TEAM

from novacrm import verify as verify_module
from novacrm.adapters.rocketlane import RocketlaneProjects


def _rocketlane(tmp: Path) -> tuple[RocketlaneProjects, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    tasks = [{"taskId": i, "taskName": f"task {i}"} for i in range(1, 16)]
    tasks[7] = {
        "taskId": 8,
        "taskName": "Customer data verification sign-off",
        "assignees": {"members": [], "placeholders": [{"placeholderId": 12}]},
    }
    tasks[12] = {
        "taskId": 13,
        "taskName": "Go-live readiness check",
        "dependencies": [{"taskId": 8}],
    }
    phases = [
        {"phaseId": i, "phaseName": n}
        for i, n in enumerate(["Kickoff", "Data Migration", "Configuration", "Go-Live"])
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path.removeprefix("/api/1.0")
        if req.method == "POST" and path == "/projects":
            ref = json.loads(req.content)["externalReferenceId"]
            return httpx.Response(201, json={"projectId": 5000 + len(seen), "ref": ref})
        if req.method == "GET" and path.startswith("/projects/"):
            return httpx.Response(
                200,
                json={
                    "projectId": 1,
                    "sources": [
                        {
                            "templateId": 201,
                            "templateName": "Enterprise Onboarding - 30 day"
                            if "enterprise" in str(seen)
                            else "Growth",
                        }
                    ],
                    "plannedDurationInDays": 30,
                },
            )
        if path == "/phases":
            return httpx.Response(200, json={"data": phases})
        if path == "/tasks":
            return httpx.Response(200, json={"data": tasks})
        if path == "/projects":  # list or lookup by reference
            ref = req.url.params.get("externalReferenceId.eq")
            data = [{"projectId": 1, "externalReferenceId": ref}] if ref else []
            return httpx.Response(200, json={"data": data, "pagination": {"hasMore": False}})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RocketlaneProjects("k", client=client), seen


def test_verify_platform_records_results_and_archives_its_test_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify_module, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(verify_module, "RESULTS", tmp_path / "checks.json")
    api, seen = _rocketlane(tmp_path)
    report = verify_module.verify(api, TEAM, "me@example.com", delete=False)

    results = json.loads((tmp_path / "checks.json").read_text())
    assert results["measured"]["enterprise.planned_duration_days"] == 30
    assert any(
        c["name"] == "enterprise: sign-off owned by the customer" and c["ok"]
        for c in results["checks"]
    )
    archived = [r for r in seen if r.url.path.endswith("/archive")]
    assert len(archived) == 2 and not any(r.method == "DELETE" for r in seen)
    assert (tmp_path / "fixtures" / "enterprise-tasks.json").exists()
    assert report.checks
