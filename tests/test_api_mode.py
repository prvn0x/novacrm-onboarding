"""Rocketlane `api` mode: the whole onboarding plan created through the phase and task APIs.

This is the fallback if the trial cannot apply templates through the API (design D4). One config
value switches it on: `rocketlane: {mode: api}` in config/team.yaml.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from conftest import OPP_18, TEAM, World

from novacrm.adapters.rocketlane import RocketlaneProjects, project_body
from novacrm.domain import PHASES, SIGNOFF_TASK, DealState
from novacrm.fakes import confirms
from novacrm.ports import ProjectSpec, TransientError


@pytest.fixture
def api_world() -> World:
    world = World()
    team = TEAM.model_copy(deep=True)
    team.rocketlane.mode = "api"
    team.rocketlane.templates = {}
    world.services.team = team
    return world


@pytest.mark.parametrize(("tier", "days"), [("enterprise", 30), ("growth", 14)])
def test_the_plan_is_built_through_the_api_and_verified(
    api_world: World, tier: str, days: int
) -> None:
    w = api_world
    w.voice.script = [confirms(tier)]
    w.send()
    w.poll()

    deal = w.deal()
    assert deal.state is DealState.CHANNEL_CREATED
    (spec,) = w.rocketlane.created
    assert spec.template_id is None and spec.due_date is not None
    assert (spec.due_date - spec.start_date).days == days
    assert w.rocketlane.phases_created == list(PHASES)
    tasks = [t for t in w.rocketlane.tasks.values() if t["project"] == deal.project_id]
    assert len(tasks) == 16  # the 15 plan tasks and the deal's kickoff task
    signoff = next(t for t in tasks if t["name"] == SIGNOFF_TASK)
    assert signoff["assignees"] == {"john.doe@acme.com"}
    readiness = next(t for t in tasks if t["name"] == "Go-live readiness check")
    signoff_id = next(i for i, t in w.rocketlane.tasks.items() if t is signoff)
    assert readiness["dependencies"] == {signoff_id}
    build = next(r for r in w.store.trail("D1") if r["action"] == "build_plan")
    assert build["output"] == {"phases_created": 4, "tasks_created": 15}


def test_a_lost_response_while_building_the_plan_creates_nothing_twice(api_world: World) -> None:
    w = api_world
    real = w.rocketlane.create_task
    calls = {"n": 0}

    def flaky(spec):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        task_id = real(spec)
        if calls["n"] == 7:
            raise TransientError("rocketlane", "read timeout")  # created, response lost
        return task_id

    w.rocketlane.create_task = flaky  # type: ignore[method-assign]
    w.voice.script = [confirms("enterprise")]
    w.send()
    w.poll()
    assert w.deal().state is DealState.TIER_CONFIRMED  # waiting to retry
    w.clock.advance(minutes=6)
    w.poll()
    assert w.deal().state is DealState.CHANNEL_CREATED
    names = [t["name"] for t in w.rocketlane.tasks.values()]
    assert len(names) == len(set(names)) == 16
    assert len(w.rocketlane.phases_created) == 4


def _spec(**extra: object) -> ProjectSpec:
    base = {
        "name": "Acme Corp - Enterprise onboarding",
        "customer_name": "Acme Corp",
        "contact_email": "john.doe@acme.com",
        "owner_email": "priya@novacrm.example",
        "csm_email": "asha@novacrm.example",
        "start_date": date(2026, 10, 29),
        "external_reference_id": OPP_18,
    }
    return ProjectSpec(**{**base, **extra})  # type: ignore[arg-type]


def test_the_api_mode_project_body_has_dates_and_no_template() -> None:
    body = project_body(_spec(due_date=date(2026, 11, 28)))
    assert body["dueDate"] == "2026-11-28" and "sources" not in body and "placeholders" not in body
    template = project_body(
        _spec(template_id=201, pm_placeholder_id=11, customer_placeholder_id=12)
    )
    assert "dueDate" not in template and template["sources"][0]["templateId"] == 201


def test_phase_and_dependency_requests_match_the_reference() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"phaseId": 7, "taskId": 9})

    api = RocketlaneProjects("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.create_phase(5001, "Kickoff", date(2026, 10, 29), date(2026, 11, 3)) == 7
    api.add_dependency(13, 8)
    phase, dep = seen
    assert phase.url.path == "/api/1.0/phases"
    assert json.loads(phase.content) == {
        "phaseName": "Kickoff",
        "project": {"projectId": 5001},
        "startDate": "2026-10-29",
        "dueDate": "2026-11-03",
    }
    assert dep.url.path == "/api/1.0/tasks/13/add-dependencies"
    assert json.loads(dep.content) == {"dependencies": [{"taskId": 8}]}


def test_api_mode_needs_no_template_ids_in_config() -> None:
    raw = TEAM.model_dump(mode="json")
    raw["rocketlane"] = {"mode": "api"}
    from novacrm.config import Team

    assert Team.model_validate(raw).rocketlane.templates == {}


def test_the_plan_file_matches_the_design() -> None:
    from novacrm.plan import load_plan

    plan = load_plan()
    assert [p.name for p in plan.phases] == list(PHASES)
    assert sum(len(p.tasks) for p in plan.phases) == 15
    assert plan.durations == {"enterprise": 30, "growth": 14}


def test_the_example_config_files_are_valid() -> None:
    import json
    from pathlib import Path

    from novacrm.config import load_team

    assert load_team("config/team.example.yaml").rocketlane.mode == "template"
    assistant = json.loads(Path("config/vapi_assistant.json").read_text())
    assert "{{customer_name}}" in assistant["firstMessage"]
    assert "Just to confirm: Enterprise" in assistant["model"]["messages"][0]["content"]
