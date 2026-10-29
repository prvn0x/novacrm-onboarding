"""The domain: plan tiers, the deal record, and the facts each step produces.

Nothing here talks to a vendor. `TIER_POLICIES` is the only place that maps a tier to a template,
a duration and a CSM model (design D4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class Tier(StrEnum):
    ENTERPRISE = "enterprise"
    GROWTH = "growth"


class CsmModel(StrEnum):
    DEDICATED = "dedicated"
    POOLED = "pooled"


@dataclass(frozen=True, slots=True)
class TierPolicy:
    tier: Tier
    label: str
    duration_days: int
    csm_model: CsmModel


TIER_POLICIES: dict[Tier, TierPolicy] = {
    Tier.ENTERPRISE: TierPolicy(Tier.ENTERPRISE, "Enterprise", 30, CsmModel.DEDICATED),
    Tier.GROWTH: TierPolicy(Tier.GROWTH, "Growth", 14, CsmModel.POOLED),
}


# The plan both templates share (design §9). The sign-off task is owned by the customer.
PHASES = ("Kickoff", "Data Migration", "Configuration", "Go-Live")
SIGNOFF_TASK = "Customer data verification sign-off"
TEMPLATE_TASK_COUNT = 15


def project_start(received: date) -> date:
    """The next business day after the email arrived (assumption A5)."""
    start = received + timedelta(days=1)
    while start.weekday() >= 5:  # Saturday, Sunday
        start += timedelta(days=1)
    return start


# A Salesforce record id is 15 or 18 base-62 characters; opportunity ids start with "006".
_OPPORTUNITY_LINK = re.compile(
    r"https://[A-Za-z0-9.-]+\.(?:salesforce|force)\.com/[^\s<>\"']*?"
    r"(?<![A-Za-z0-9])(006[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?)(?![A-Za-z0-9])"
)


_CHECKSUM_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def salesforce_id_18(record_id: str) -> str:
    """The 18-character form of a 15- or 18-character Salesforce id (design D2).

    The 15-character form is case-sensitive; the 3-character suffix encodes, for each 5-character
    chunk, which characters are upper case. Both forms of one record map to the same value here.
    """
    if len(record_id) == 18:
        return record_id[:15] + record_id[15:].upper()
    if len(record_id) != 15:
        raise ValueError(f"not a Salesforce id: {record_id!r}")
    suffix = ""
    for chunk in (record_id[0:5], record_id[5:10], record_id[10:15]):
        bits = sum(1 << i for i, ch in enumerate(chunk) if "A" <= ch <= "Z")
        suffix += _CHECKSUM_ALPHABET[bits]
    return record_id + suffix


def opportunity_id_from_link(link: str) -> str | None:
    """The 18-character opportunity id in a Salesforce link, or None."""
    match = _OPPORTUNITY_LINK.search(link)
    return salesforce_id_18(match.group(1)) if match else None


class DealNotification(BaseModel):
    """The validated facts of one deal email. Every value was found in the email (design D2)."""

    customer_name: str = Field(min_length=1)
    contact_email: EmailStr
    contact_name: str | None = None
    opportunity_link: str
    opportunity_id: str
    ae_name: str
    ae_email: EmailStr
    close_date: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class InboundEmail:
    message_id: str
    thread_id: str
    sender: str  # the bare address, lower-cased
    subject: str
    body: str
    received_at: datetime
    thread_text: str  # every message in the thread, oldest first, for re-reading after a reply


class DealState(StrEnum):
    RECEIVED = "RECEIVED"
    REJECTED = "REJECTED"
    NEEDS_INFO = "NEEDS_INFO"
    DUPLICATE = "DUPLICATE"
    AWAITING_TIER = "AWAITING_TIER"
    TIER_CONFIRMED = "TIER_CONFIRMED"
    PROJECT_CREATED = "PROJECT_CREATED"
    CHANNEL_CREATED = "CHANNEL_CREATED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLOSED = "CLOSED"


class ReviewReason(StrEnum):
    """Why a deal needs a person; it decides which exits are allowed (design §6.1)."""

    AE_UNREACHABLE = "ae_unreachable"
    AE_UNSURE = "ae_unsure"
    TIER_UNCLEAR = "tier_unclear"
    MISSING_FIELDS = "missing_fields"
    UNPARSEABLE = "unparseable"
    REJECTED_BY_ROCKETLANE = "rejected_by_rocketlane"
    REJECTED_BY_SLACK = "rejected_by_slack"
    OUTAGE = "outage"
    TEMPLATE_MISMATCH = "template_mismatch"
    POSSIBLE_DUPLICATE = "possible_duplicate"  # a project for this customer we did not make
    INTERNAL_ERROR = "internal_error"  # a bug: the deal is parked so the others keep moving


TIER_REASONS = frozenset(
    {ReviewReason.AE_UNREACHABLE, ReviewReason.AE_UNSURE, ReviewReason.TIER_UNCLEAR}
)

TERMINAL_STATES = frozenset(
    {DealState.REJECTED, DealState.DUPLICATE, DealState.CHANNEL_CREATED, DealState.CLOSED}
)


class CallStatus(StrEnum):
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Turn:
    speaker: Literal["ae", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class CallResult:
    call_id: str
    status: CallStatus
    turns: tuple[Turn, ...] = ()
    detail: str = ""  # the platform's own reason, e.g. "customer-did-not-answer"


class TierOutcome(StrEnum):
    CONFIRMED = "confirmed"
    UNSURE = "unsure"  # the AE does not know: a person must decide, no more calls
    UNCLEAR = "unclear"  # an answer we cannot trust: one more call
    UNREACHABLE = "unreachable"  # no answer, busy, voicemail, failed: retry


@dataclass(frozen=True, slots=True)
class TierDecision:
    outcome: TierOutcome
    tier: Tier | None
    reason: str
    evidence: str = ""


@dataclass(slots=True)
class Deal:
    """One onboarding, from the first email to the Slack channel. Saved after every step."""

    key: str  # D1, D2, ...; the opportunity id links the emails of one deal
    thread_id: str  # the thread replies go to (the deal's most recent complete one)
    state: DealState
    state_since: datetime
    received_at: datetime
    ae_email: str
    subject: str = ""
    thread_text: str = ""
    message_ids: list[str] = field(default_factory=list)
    notification: DealNotification | None = None
    tier: Tier | None = None
    tier_decided_by: str = ""  # "ae-call" or the name of the person who resolved it
    # the voice step (D3, D5): intent saved before each call, the call id right after
    call_attempts: int = 0
    unclear_calls: int = 0
    call_started_at: datetime | None = None
    pending_call_id: str | None = None
    abandoned_call_id: str | None = None  # gave up waiting for it; re-read before calling again
    next_call_at: datetime | None = None
    # the inbox step (D2)
    parse_failures: int = 0
    clarifications_sent: int = 0
    partial_customer: str = ""  # from an incomplete email, to match a complete resend (D5)
    partial_opportunity: str = ""  # likewise; a valid link in an otherwise incomplete email
    # the project step (D4, D5)
    reference_checked: bool = False  # step 4 saw no project with this opportunity's reference
    not_duplicate_by: str = ""  # a person said a same-name project is not this onboarding
    project_id: int | None = None
    project_url: str = ""
    csm_email: str = ""
    go_live: str = ""  # ISO date: the project's due date as Rocketlane scheduled it
    kickoff_task_id: int | None = None
    # the channel step (D5, D8)
    channel_id: str | None = None
    channel_name: str = ""
    welcome_ts: str | None = None
    # outages (D6) and people (§6.1)
    pending_since: datetime | None = None
    next_retry_at: datetime | None = None
    outage_notified: bool = False
    review_reason: ReviewReason | None = None
    review_detail: str = ""
    resume_state: DealState | None = None
    merged_into: str | None = None
