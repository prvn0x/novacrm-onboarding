"""The onboarding plan (config/onboarding_plan.yaml): phases and tasks, dated for one tier.

In `api` mode the Intake Agent creates exactly this plan through the Rocketlane API; in
`template` mode the two Rocketlane templates are built from it by hand (docs/ROCKETLANE_SETUP.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

from .domain import PHASES, SIGNOFF_TASK, TEMPLATE_TASK_COUNT, Tier


class PlanTask(BaseModel):
    name: str
    owner: Literal["pm", "customer"]
    enterprise: tuple[int, int]
    growth: tuple[int, int]
    description: str = ""
    depends_on: str | None = None

    def days(self, tier: Tier) -> tuple[int, int]:
        return self.enterprise if tier is Tier.ENTERPRISE else self.growth


class PlanPhase(BaseModel):
    name: str
    tasks: list[PlanTask]


class Plan(BaseModel):
    durations: dict[Tier, int]
    phases: list[PlanPhase]

    @model_validator(mode="after")
    def _matches_the_design(self) -> Plan:
        """The plan the agents check projects against (domain.py) and this file must agree."""
        if tuple(p.name for p in self.phases) != PHASES:
            raise ValueError(f"phases must be {PHASES}")
        tasks = [t for p in self.phases for t in p.tasks]
        if len(tasks) != TEMPLATE_TASK_COUNT:
            raise ValueError(f"the plan needs {TEMPLATE_TASK_COUNT} tasks, has {len(tasks)}")
        names = {t.name for t in tasks}
        if SIGNOFF_TASK not in names:
            raise ValueError(f"the plan needs the '{SIGNOFF_TASK}' task")
        for task in tasks:
            if task.depends_on and task.depends_on not in names:
                raise ValueError(f"{task.name} depends on an unknown task: {task.depends_on}")
            for tier in Tier:
                start, end = task.days(tier)
                if not 0 <= start <= end <= self.durations[tier]:
                    raise ValueError(f"{task.name}: days {start}-{end} outside the {tier} plan")
        return self


@dataclass(frozen=True, slots=True)
class DatedTask:
    phase: str
    name: str
    owner: Literal["pm", "customer"]
    start: date
    due: date
    description: str
    depends_on: str | None


@dataclass(frozen=True, slots=True)
class DatedPhase:
    name: str
    start: date
    due: date
    tasks: tuple[DatedTask, ...]


def load_plan(path: str | Path = "config/onboarding_plan.yaml") -> Plan:
    return Plan.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def schedule(plan: Plan, tier: Tier, start: date) -> list[DatedPhase]:
    """The plan for one tier, dated from the project start."""
    phases = []
    for phase in plan.phases:
        tasks = tuple(
            DatedTask(
                phase=phase.name,
                name=t.name,
                owner=t.owner,
                start=start + timedelta(days=t.days(tier)[0]),
                due=start + timedelta(days=t.days(tier)[1]),
                description=t.description,
                depends_on=t.depends_on,
            )
            for t in phase.tasks
        )
        phases.append(
            DatedPhase(phase.name, min(t.start for t in tasks), max(t.due for t in tasks), tasks)
        )
    return phases
