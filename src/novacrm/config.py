"""Configuration: the team directory (`config/team.yaml`) and the runtime settings (`.env`).

The team file holds people and Rocketlane ids, so the real one is git-ignored and
`config/team.example.yaml` documents its shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, EmailStr, model_validator

from .domain import Tier


class Person(BaseModel):
    name: str
    email: EmailStr


class AccountExecutive(Person):
    phone: str  # E.164, e.g. +919876543210


class TemplateRef(BaseModel):
    template_id: int
    pm_placeholder_id: int  # "Project Manager" -> the CSM
    customer_placeholder_id: int  # "Customer data owner" -> the customer contact
    planned_duration_days: int  # as Rocketlane reports it for this template (measured, §14)


class RocketlaneConfig(BaseModel):
    # "template": apply the Rocketlane template through the API (design D4).
    # "api": create every phase and task from config/onboarding_plan.yaml through the API
    #        (the fallback if the trial cannot apply templates through the API).
    mode: Literal["template", "api"] = "template"
    templates: dict[Tier, TemplateRef] = {}

    @model_validator(mode="after")
    def _one_template_per_tier(self) -> RocketlaneConfig:
        """Refuse a config that could mix templates up (design D4)."""
        if self.mode == "api":
            return self
        if set(self.templates) != set(Tier):
            raise ValueError("rocketlane.templates needs exactly one entry per tier")
        ids = [t.template_id for t in self.templates.values()]
        if len(set(ids)) != len(ids):
            raise ValueError("two tiers point at the same Rocketlane template")
        return self


class Team(BaseModel):
    cs_manager: Person
    escalation_channel: str = "cs-onboarding-escalations"
    account_executives: list[AccountExecutive]
    dedicated_csms: list[Person]
    pooled_csm: Person
    rocketlane: RocketlaneConfig

    def ae_by_email(self, email: str) -> AccountExecutive | None:
        email = email.lower()
        return next((a for a in self.account_executives if a.email.lower() == email), None)

    def csm_for(self, tier: Tier, deal_number: int) -> Person:
        """Enterprise: the next dedicated CSM in the list (assumption A7). Growth: the pool."""
        if tier is Tier.ENTERPRISE:
            return self.dedicated_csms[deal_number % len(self.dedicated_csms)]
        return self.pooled_csm


def load_team(path: str | Path) -> Team:
    return Team.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Policy:
    """The timing rules of design D3, D6 and §6.1. Defaults are the production values."""

    max_calls: int = 3  # per deal, whatever the outcome
    max_unclear_calls: int = 2  # an unclear answer gets one more call
    call_spacing: timedelta = timedelta(minutes=15)
    call_result_timeout: timedelta = timedelta(minutes=10)  # the assistant hangs up after 2
    max_parse_failures: int = 3
    max_clarifications: int = 2
    needs_info_timeout: timedelta = timedelta(hours=24)
    outage_notice_after: timedelta = timedelta(hours=1)
    outage_give_up_after: timedelta = timedelta(hours=24)
    outage_retry_every: timedelta = timedelta(minutes=5)
    retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0)
    # Rocketlane copies a template's tasks in the background (seen live, 28 Oct): re-read after
    # these waits before calling a short task list a mismatch.
    template_copy_waits: tuple[float, ...] = (5.0, 10.0, 15.0)


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: str = "var/novacrm.db"
    team_path: str = "config/team.yaml"
    poll_seconds: int = 30
    policy: Policy = field(default_factory=Policy)

    @classmethod
    def from_env(cls) -> Settings:
        spacing = int(os.environ.get("CALL_SPACING_SECONDS", "900"))
        outage_retry = int(os.environ.get("OUTAGE_RETRY_SECONDS", "300"))
        return cls(
            db_path=os.environ.get("NOVACRM_DB", "var/novacrm.db"),
            team_path=os.environ.get("NOVACRM_TEAM", "config/team.yaml"),
            poll_seconds=int(os.environ.get("POLL_SECONDS", "30")),
            policy=Policy(
                call_spacing=timedelta(seconds=spacing),
                outage_retry_every=timedelta(seconds=outage_retry),
            ),
        )


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set; add it to .env (see .env.example)")
    return value
