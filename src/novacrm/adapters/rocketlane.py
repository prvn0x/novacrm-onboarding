"""Rocketlane REST API v1 adapter (developer.rocketlane.com, API v1.3 reference).

Create from a template (`sources`), look up by `externalReferenceId` or project name, read the
project back with its sources and planned duration, list phases and tasks, create one task.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from ..ports import (
    PermanentError,
    PhaseInfo,
    ProjectGone,
    ProjectInfo,
    ProjectSpec,
    TaskInfo,
    TaskSpec,
)
from .http import request

SYSTEM = "rocketlane"
BASE_URL = "https://api.rocketlane.com/api/1.0"
_PROJECT_FIELDS = "sources,plannedDuration,externalReferenceId"


class RocketlaneProjects:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        app_url: str = "https://app.rocketlane.com",
        client: httpx.Client | None = None,
    ) -> None:
        # Creating from a template took over 20 s on the trial (28 Oct), and the project was created
        # anyway: a long read timeout, and a lookup by reference before any retry (intake.py).
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, read=120.0))
        self._client.headers.update({"api-key": api_key, "accept": "application/json"})
        self._base = base_url.rstrip("/")
        self._app = app_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        return request(self._client, SYSTEM, "GET", self._base + path, params=params).json()

    def _list(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every page of a list endpoint."""
        items: list[dict[str, Any]] = []
        params = {**params, "pageSize": 100}
        while True:
            body = self._get(path, params)
            items += body.get("data", [])
            page = body.get("pagination") or {}
            if not page.get("hasMore") or not page.get("nextPageToken"):
                return items
            params = {**params, "pageToken": page["nextPageToken"]}

    def _info(self, p: dict[str, Any]) -> ProjectInfo:
        sources = p.get("sources") or []
        team = p.get("teamMembers") or {}
        people = [*(team.get("members") or []), *(team.get("customers") or [])]
        owner = p.get("owner") or {}
        emails = {m.get("emailId", "") for m in [*people, owner] if m.get("emailId")}
        created = p.get("createdAt")
        return ProjectInfo(
            project_id=int(p["projectId"]),
            name=p.get("projectName", ""),
            external_reference_id=p.get("externalReferenceId") or "",
            template_ids=tuple(int(s["templateId"]) for s in sources if "templateId" in s),
            template_names=tuple(s.get("templateName", "") for s in sources),
            planned_duration_days=p.get("plannedDurationInDays"),
            member_emails=frozenset(emails),
            url=f"{self._app}/projects/{p['projectId']}",
            created_at=datetime.fromtimestamp(created / 1000, UTC) if created else None,
            due_date=date.fromisoformat(p["dueDate"]) if p.get("dueDate") else None,
        )

    # ---- used only by `novacrm verify-platform` (raw responses, and cleaning up its test projects)
    def raw(self, method: str, path: str, **kwargs: Any) -> Any:
        response = request(self._client, SYSTEM, method, self._base + path, **kwargs)
        return response.json() if response.content else None

    # ---- ProjectStore ------------------------------------------------------------------------
    def find_by_reference(self, external_reference_id: str) -> ProjectInfo | None:
        found = self._list(
            "/projects",
            {
                "externalReferenceId.eq": external_reference_id,
                "includeArchive.eq": "true",
                "includeFields": _PROJECT_FIELDS,
            },
        )
        # Checked here too, so an ignored filter can never turn every deal into a duplicate.
        ours = [p for p in found if p.get("externalReferenceId") == external_reference_id]
        return self._info(ours[0]) if ours else None

    def find_by_name(self, text: str) -> list[ProjectInfo]:
        found = self._list("/projects", {"projectName.cn": text, "includeFields": _PROJECT_FIELDS})
        wanted = text.casefold()
        return [self._info(p) for p in found if wanted in p.get("projectName", "").casefold()]

    def create_project(self, spec: ProjectSpec) -> ProjectInfo:
        created = request(
            self._client, SYSTEM, "POST", self._base + "/projects", json=project_body(spec)
        ).json()
        return self.get_project(int(created["projectId"]))

    def get_project(self, project_id: int) -> ProjectInfo:
        try:
            body = self._get(f"/projects/{project_id}", {"includeFields": _PROJECT_FIELDS})
        except PermanentError as exc:
            if " 404 " in f" {exc} ":
                raise ProjectGone(SYSTEM, f"project {project_id} not found") from exc
            raise
        return self._info(body)

    def list_phases(self, project_id: int) -> list[PhaseInfo]:
        phases = self._list("/phases", {"projectId": project_id})
        return [PhaseInfo(int(p["phaseId"]), p.get("phaseName", "")) for p in phases]

    def list_tasks(self, project_id: int) -> list[TaskInfo]:
        # phase and assignees are optional fields in v1.3: ask for them explicitly.
        tasks = self._list(
            "/tasks",
            {"projectId.eq": project_id, "includeFields": "phase,assignees,dependencies"},
        )
        infos = []
        for t in tasks:
            assignees = t.get("assignees") or {}
            infos.append(
                TaskInfo(
                    task_id=int(t["taskId"]),
                    name=t.get("taskName", ""),
                    phase_id=(t.get("phase") or {}).get("phaseId"),
                    assignee_emails=frozenset(
                        m["emailId"] for m in assignees.get("members") or [] if m.get("emailId")
                    ),
                    placeholder_ids=frozenset(
                        int(p["placeholderId"])
                        for p in assignees.get("placeholders") or []
                        if p.get("placeholderId") is not None
                    ),
                    dependency_ids=frozenset(
                        int(d["taskId"]) for d in t.get("dependencies") or [] if d.get("taskId")
                    ),
                )
            )
        return infos

    def create_task(self, spec: TaskSpec) -> int:
        body = {
            "taskName": spec.name,
            "taskDescription": f"<p>{_html(spec.description)}</p>",
            "project": {"projectId": spec.project_id},
            "phase": {"phaseId": spec.phase_id},
            "dueDate": spec.due_date.isoformat(),
            "assignees": {"members": [{"emailId": spec.assignee_email}]},
            "type": "TASK",
        }
        if spec.start_date:
            body["startDate"] = spec.start_date.isoformat()
        created = request(self._client, SYSTEM, "POST", self._base + "/tasks", json=body).json()
        return int(created["taskId"])

    def create_phase(self, project_id: int, name: str, start: date, due: date) -> int:
        body = {
            "phaseName": name,
            "project": {"projectId": project_id},
            "startDate": start.isoformat(),
            "dueDate": due.isoformat(),
        }
        created = request(self._client, SYSTEM, "POST", self._base + "/phases", json=body).json()
        return int(created["phaseId"])

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        body = {"dependencies": [{"taskId": depends_on_task_id}]}
        url = f"{self._base}/tasks/{task_id}/add-dependencies"
        request(self._client, SYSTEM, "POST", url, json=body)


def project_body(spec: ProjectSpec) -> dict[str, Any]:
    """The create-project request (design D4), for either mode."""
    body: dict[str, Any] = {
        "projectName": spec.name,
        "owner": {"emailId": spec.owner_email},
        "customer": {"companyName": spec.customer_name},
        "autoCreateCompany": True,
        "startDate": spec.start_date.isoformat(),
        "externalReferenceId": spec.external_reference_id,
        "visibility": "MEMBERS",
        "teamMembers": {
            "members": [{"emailId": spec.csm_email}],
            "customers": [{"emailId": spec.contact_email}],
        },
    }
    if spec.template_id is not None:  # template mode: Rocketlane computes the due date
        body["sources"] = [
            {"templateId": spec.template_id, "startDate": spec.start_date.isoformat()}
        ]
        body["placeholders"] = [
            {"placeholderId": spec.pm_placeholder_id, "user": {"emailId": spec.csm_email}},
            {
                "placeholderId": spec.customer_placeholder_id,
                "user": {"emailId": spec.contact_email},
            },
        ]
    else:  # api mode: an empty project with explicit dates; phases and tasks follow
        assert spec.due_date is not None
        body["dueDate"] = spec.due_date.isoformat()
    return body


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
