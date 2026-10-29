"""`novacrm verify-platform`: the day-1 platform checks (design doc §14), run against the trial.

For each tier it creates one clearly named test project the way the agent would, reads it back,
and records what Rocketlane actually returns: whether the template applied, the planned duration
(the value config/team.yaml needs), the phases and tasks, how the sign-off task's customer
assignment comes back, and whether the go-live dependency exists. It then archives the test
project and checks that the archived project is still found by reference. Raw responses are saved
under var/fixtures/ (git-ignored) so they can become test fixtures, and the results go to
var/platform-checks.json. Checks that only the Rocketlane UI can show (automation targets,
notifications) are printed as a manual checklist.

Changes made in the trial: one test project per tier, archived at the end (or deleted with
--delete). Nothing else is written.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .adapters.rocketlane import RocketlaneProjects, project_body
from .config import Team
from .domain import PHASES, SIGNOFF_TASK, TEMPLATE_TASK_COUNT, Tier
from .ports import ProjectSpec

FIXTURES = Path("var/fixtures")
RESULTS = Path("var/platform-checks.json")
MANUAL = [
    "Automation rule 1 (task overdue 1 day -> notify Project Manager) is on both templates",
    "Automation rule 2 (task overdue 4 days -> notify Project Owner) is on both templates",
    "'Notify' offers the Project Manager placeholder as a recipient (else: the task assignee)",
    "A task given a due date in the past fires rule 1 and rule 2 (check the run history)",
    "Go-live readiness cannot be started while the sign-off task is open (dependency enforced?)",
    "Vapi: one test call to your own number connects (VOICE_MODE=phone), else use VOICE_MODE=web",
]


@dataclass
class Check:
    name: str
    ok: bool | None  # None = could not tell
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    measured: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))
        mark = {True: "PASS", False: "FAIL", None: "????"}[ok]
        print(f"  [{mark}] {name}: {detail}")


def _save(name: str, data: Any) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(json.dumps(data, indent=2, default=str), "utf-8")


def verify(api: RocketlaneProjects, team: Team, contact_email: str, delete: bool) -> Report:
    report = Report()
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    listing = api.raw("GET", "/projects", params={"pageSize": 1})
    _save("projects-list", listing)
    report.add("API key works", True, "GET /projects answered")

    for tier in Tier:
        ref = f"novacrm-verify-{tier.value}-{stamp}"
        csm = team.csm_for(tier, 0)
        start = date.today() + timedelta(days=1)
        template = team.rocketlane.templates.get(tier)
        spec = ProjectSpec(
            name=f"NovaCRM platform check - {tier.value} - {stamp}",
            customer_name="NovaCRM Platform Check",
            contact_email=contact_email,
            owner_email=team.cs_manager.email,
            csm_email=csm.email,
            start_date=start,
            external_reference_id=ref,
            template_id=template.template_id if template else None,
            pm_placeholder_id=template.pm_placeholder_id if template else None,
            customer_placeholder_id=template.customer_placeholder_id if template else None,
            due_date=None if template else start + timedelta(days=30),
        )
        print(f"\n{tier.value}: creating test project {spec.name!r}")
        created = api.raw("POST", "/projects", json=project_body(spec))
        _save(f"{tier.value}-create-response", created)
        pid = int(created["projectId"])
        project = api.raw("GET", f"/projects/{pid}", params={"includeAllFields": "true"})
        _save(f"{tier.value}-project", project)
        phases = api.raw("GET", "/phases", params={"projectId": pid, "pageSize": 100})
        _save(f"{tier.value}-phases", phases)
        tasks: Any = {}
        for wait in (0.0, 5.0, 10.0, 15.0):  # a template's tasks are copied in the background
            time.sleep(wait)
            tasks = api.raw(
                "GET",
                "/tasks",
                params={
                    "projectId.eq": pid,
                    "includeFields": "phase,assignees,dependencies",
                    "pageSize": 100,
                },
            )
            if len((tasks or {}).get("data", [])) >= TEMPLATE_TASK_COUNT or not template:
                break
        report.measured[f"{tier.value}.task_copy_wait_seconds"] = wait
        _save(f"{tier.value}-tasks", tasks)

        sources = project.get("sources") or []
        names = [s.get("templateName", "") for s in sources]
        if template:
            report.add(
                f"{tier.value}: template applied through the API",
                bool(sources),
                f"sources = {names}" if sources else "no sources on the project",
            )
            report.add(
                f"{tier.value}: template name says {tier.value}",
                any(tier.value in n.lower() for n in names),
                str(names),
            )
        duration = project.get("plannedDurationInDays")
        report.measured[f"{tier.value}.planned_duration_days"] = duration
        report.add(
            f"{tier.value}: planned duration",
            None if duration is None else True,
            f"plannedDurationInDays = {duration} (put this in config/team.yaml)"
            if duration is not None
            else f"field missing; keys: {sorted(project)[:25]}",
        )
        phase_names = [p.get("phaseName") for p in (phases or {}).get("data", [])]
        report.add(
            f"{tier.value}: phases",
            sorted(map(str, phase_names)) == sorted(PHASES),
            str(phase_names),
        )
        task_list = (tasks or {}).get("data", [])
        report.add(
            f"{tier.value}: task count",
            len(task_list) == TEMPLATE_TASK_COUNT if template else None,
            f"{len(task_list)} tasks"
            + ("" if template else " (api mode builds them on a real run)"),
        )
        signoff = next((t for t in task_list if t.get("taskName") == SIGNOFF_TASK), None)
        if signoff is not None:
            assignees = signoff.get("assignees") or {}
            report.measured[f"{tier.value}.signoff_assignees"] = assignees
            members = [m.get("emailId") for m in assignees.get("members") or []]
            holders = [p.get("placeholderId") for p in assignees.get("placeholders") or []]
            report.add(
                f"{tier.value}: sign-off owned by the customer",
                contact_email in members
                or (template is not None and template.customer_placeholder_id in holders),
                f"members={members} placeholders={holders}",
            )
            readiness = next(
                (t for t in task_list if t.get("taskName") == "Go-live readiness check"), None
            )
            deps = [d.get("taskId") for d in (readiness or {}).get("dependencies") or []]
            report.add(
                f"{tier.value}: go-live depends on the sign-off",
                signoff.get("taskId") in deps,
                f"dependencies of 'Go-live readiness check' = {deps}",
            )
        elif template:
            report.add(f"{tier.value}: sign-off task", False, f"no '{SIGNOFF_TASK}' task")

        api.raw("POST", f"/projects/{pid}/archive")
        found = None
        for wait in (0.0, 5.0, 10.0):  # the archive may take a moment to show in a search
            time.sleep(wait)
            found = api.find_by_reference(ref)
            if found is not None:
                break
        report.add(
            f"{tier.value}: archived project still found by reference",
            found is not None,
            "found with includeArchive" if found else "NOT found once archived",
        )
        if delete:
            api.raw("DELETE", f"/projects/{pid}")
            print(f"  deleted test project {pid}")

    print("\nCheck these in the Rocketlane and Vapi UIs, and note the result in §14:")
    for item in MANUAL:
        print(f"  [ ] {item}")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "ran_at": stamp,
                "mode": team.rocketlane.mode,
                "checks": [c.__dict__ for c in report.checks],
                "measured": report.measured,
                "manual": MANUAL,
            },
            indent=2,
            default=str,
        ),
        "utf-8",
    )
    print(f"\nResults in {RESULTS}; raw responses in {FIXTURES}/")
    return report
