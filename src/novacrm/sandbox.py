"""A whole NovaCRM world built from fakes: inbox, phone, Rocketlane, Slack, LLM and clock.

Used by the tests and by `novacrm demo`, which runs every scenario offline with no accounts.
The fake LLM behaves like a careful model: it reads the labelled lines AEs use in these sample
emails and the read-back in a transcript. Tests that need a misbehaving model replace a handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .agents.base import Services
from .config import Policy, Team
from .domain import Deal
from .fakes import (
    FakeClock,
    FakeInbox,
    FakeLLM,
    FakeRocketlane,
    FakeSlack,
    FakeTemplate,
    FakeVoice,
)
from .orchestrator import Orchestrator
from .plan import load_plan
from .store import Store

AE = "rahul.mehta@novacrm.example"
OTHER_AE = "sara.khan@novacrm.example"
OPP_15 = "006Ka00000AbCdE"
OPP_18 = "006Ka00000AbCdEIAV"
OPP_LINK = f"https://novacrm.lightning.force.com/lightning/r/Opportunity/{OPP_18}/view"
ENTERPRISE_TEMPLATE, GROWTH_TEMPLATE = 201, 202

TEAM = Team.model_validate(
    {
        "cs_manager": {"name": "Priya Sharma", "email": "priya@novacrm.example"},
        "account_executives": [
            {"name": "Rahul Mehta", "email": AE, "phone": "+919800000001"},
            {"name": "Sara Khan", "email": OTHER_AE, "phone": "+919800000002"},
        ],
        "dedicated_csms": [
            {"name": "Asha Rao", "email": "asha@novacrm.example"},
            {"name": "Vikram Iyer", "email": "vikram@novacrm.example"},
        ],
        "pooled_csm": {"name": "Growth CS Team", "email": "growth-cs@novacrm.example"},
        "rocketlane": {
            "templates": {
                "enterprise": {
                    "template_id": ENTERPRISE_TEMPLATE,
                    "pm_placeholder_id": 11,
                    "customer_placeholder_id": 12,
                    "planned_duration_days": 30,
                },
                "growth": {
                    "template_id": GROWTH_TEMPLATE,
                    "pm_placeholder_id": 21,
                    "customer_placeholder_id": 22,
                    "planned_duration_days": 14,
                },
            }
        },
    }
)
DEDICATED_CSMS = {"asha@novacrm.example", "vikram@novacrm.example"}
POOLED_CSM = "growth-cs@novacrm.example"


def deal_email(
    *,
    customer: str | None = "Acme Corp",
    contact: str | None = "John Doe <john.doe@acme.com>",
    opportunity: str | None = OPP_LINK,
    notes: str | None = "They are moving from spreadsheets and care most about pipeline reports.",
    signed: str = "Rahul",
    extra: str = "",
) -> str:
    lines = ["Hi CS team,", "", "Great news, we just closed a new deal!", ""]
    if customer is not None:
        lines.append(f"Customer: {customer}")
    if contact is not None:
        lines.append(f"Contact: {contact}")
    if opportunity is not None:
        lines.append(f"Opportunity: {opportunity}")
    if notes is not None:
        lines.append(f"Notes: {notes}")
    if extra:
        lines += ["", extra]
    lines += ["", "Thanks,", signed]
    return "\n".join(lines)


def _field(text: str, label: str) -> str | None:
    found = re.findall(rf"^{label}:[ \t]*(.+)$", text, re.M | re.I)
    return found[-1].strip() if found else None


def careful_extraction(user: str) -> dict[str, Any]:
    """What a good model returns for the emails above: only what is written, nothing guessed."""
    text = user
    contact = _field(text, "Contact")
    name = email = None
    if contact:
        m = re.match(r"(.*?)\s*<([^>]+)>", contact)
        name, email = (m.group(1) or None, m.group(2)) if m else (None, contact)
    signature = re.findall(r"Thanks,\s*\n\s*(\S[^\n]*)", text)
    is_deal = "closed" in text.lower()
    return {
        "is_deal_notification": is_deal,
        "customer_name": _field(text, "Customer"),
        "contact_name": name,
        "contact_email": email,
        "opportunity_link": _field(text, "Opportunity"),
        "ae_name": signature[-1].strip() if signature else None,
        "close_date": None,
        "notes": _field(text, "Notes"),
    }


def careful_transcript_reading(user: str) -> dict[str, Any]:
    """What a good model returns for a call: the read-back answer, or unsure, or unclear."""
    turns = [line.split(": ", 1) for line in user.splitlines() if ": " in line]
    ae_lines = [t for s, t in turns if s == "AE"]
    if any(re.search(r"not sure|don't know|check with", t, re.I) for t in ae_lines):
        quote = next(t for t in ae_lines if re.search(r"not sure|don't know|check with", t, re.I))
        return {"outcome": "unsure", "readback_confirmed": False, "evidence_quote": quote}
    for i in range(len(turns) - 1, -1, -1):
        speaker, text = turns[i]
        if speaker == "ASSISTANT" and "confirm" in text.lower():
            tier = "enterprise" if "enterprise" in text.lower() else "growth"
            reply = next((t for s, t in turns[i + 1 :] if s == "AE"), "")
            yes = reply.lower().startswith("yes")
            named = next((t for t in ae_lines if tier in t.lower()), "")
            if yes:
                return {"outcome": tier, "readback_confirmed": True, "evidence_quote": named}
            return {"outcome": "unclear", "readback_confirmed": False, "evidence_quote": reply}
    return {"outcome": "unclear", "readback_confirmed": False, "evidence_quote": ""}


@dataclass
class World:
    clock: FakeClock = field(default_factory=FakeClock)
    inbox: FakeInbox = field(default_factory=FakeInbox)
    voice: FakeVoice = field(default_factory=FakeVoice)
    slack: FakeSlack = field(default_factory=FakeSlack)
    llm: FakeLLM = field(
        default_factory=lambda: FakeLLM(
            {
                "extract": careful_extraction,
                "tier": careful_transcript_reading,
                "welcome_line": lambda _: (
                    "We're thrilled to help your team leave spreadsheets "
                    "behind and get your pipeline reports working from day one."
                ),
            }
        )
    )
    policy: Policy = field(default_factory=Policy)

    def __post_init__(self) -> None:
        self.rocketlane = FakeRocketlane(
            [
                FakeTemplate(ENTERPRISE_TEMPLATE, "Enterprise Onboarding - 30 day", 30),
                FakeTemplate(GROWTH_TEMPLATE, "Growth Onboarding - 14 day", 14),
            ],
            self.clock,
        )
        self.store = Store()
        self.services = Services(
            store=self.store,
            inbox=self.inbox,
            voice=self.voice,
            projects=self.rocketlane,
            chat=self.slack,
            llm=self.llm,
            clock=self.clock,
            team=TEAM,
            policy=self.policy,
            sleep=lambda _: None,
            plan=load_plan(),
        )
        self.orchestrator = Orchestrator(self.services)

    def send(
        self,
        body: str | None = None,
        *,
        sender: str = AE,
        subject: str = "Closed won: Acme Corp",
        thread_id: str | None = None,
    ) -> str:
        email = self.inbox.deliver(
            sender,
            subject,
            body if body is not None else deal_email(),
            received_at=self.clock.now(),
            thread_id=thread_id,
        )
        return email.thread_id

    def poll(self, times: int = 1, *, minutes_between: float = 0) -> None:
        for _ in range(times):
            self.orchestrator.poll()
            if minutes_between:
                self.clock.advance(minutes=minutes_between)

    def poll_through_calls(self, polls: int = 8) -> None:
        """Poll, moving the clock past the call spacing each time (15 minutes in production)."""
        for _ in range(polls):
            self.orchestrator.poll()
            self.clock.advance(minutes=16)

    def deal(self, key: str = "D1") -> Deal:
        deal = self.store.get(key)
        assert deal is not None, f"no deal {key}"
        return deal

    def actions(self, key: str = "D1") -> list[str]:
        return [row["action"] for row in self.store.trail(key)]

    def projects_for(self, ref: str = OPP_18) -> list[dict[str, Any]]:
        return [p for p in self.rocketlane.projects.values() if p["ref"] == ref]
