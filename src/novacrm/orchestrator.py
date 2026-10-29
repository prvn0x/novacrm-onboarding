"""The orchestrator: a small, fixed state machine (design D1, §6.1).

One poll reads new email, then advances every open deal as far as it can go without waiting.
It also holds the three human exits from HUMAN_REVIEW (`resolve`, `retry`, `close`), each of
which is checked against the review reason and written to the audit log with the person's name.
"""

from __future__ import annotations

from .agents.base import Services
from .agents.comms import CommunicationAgent
from .agents.intake import IntakeAgent
from .domain import TIER_REASONS, Deal, DealState, ReviewReason, Tier
from .ports import IntegrationError

AGENT = "orchestrator"
_MAX_STEPS = 10  # per deal per poll; a deal that keeps moving is handled again next poll


class ReviewError(Exception):
    """A human command that the deal's review reason does not allow."""


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.intake = IntakeAgent(services)
        self.comms = CommunicationAgent(services)

    def poll(self) -> None:
        s = self.s
        try:
            emails = s.inbox.fetch_new()
        except IntegrationError as exc:  # Gmail down: still advance the deals we have
            s.store.audit(s.clock.now(), "-", AGENT, "inbox_unavailable", reason=str(exc))
            emails = []
        for email in emails:
            try:
                self.intake.receive(email)
                s.inbox.mark_processed(email.message_id)  # after the deal is saved (D7)
            except IntegrationError as exc:  # seen-before check makes the next poll safe
                s.store.audit(s.clock.now(), "-", AGENT, "inbox_unavailable", reason=str(exc))
            except Exception as exc:  # a bug on one email must not stop the others
                s.store.audit(
                    s.clock.now(),
                    "-",
                    AGENT,
                    "receive_failed",
                    input={"message": email.message_id},
                    reason=repr(exc),
                )
        for deal in s.store.open_deals():
            try:
                self.advance(deal)
            except Exception as exc:  # one broken deal must not stop the others
                fresh = s.store.get(deal.key) or deal
                if fresh.state is not DealState.HUMAN_REVIEW:
                    s.escalate(
                        fresh,
                        ReviewReason.INTERNAL_ERROR,
                        AGENT,
                        f"{type(exc).__name__}: {exc}",
                    )

    def advance(self, deal: Deal) -> None:
        s = self.s
        for _ in range(_MAX_STEPS):
            before = (deal.state, deal.call_attempts, deal.project_id, deal.channel_id)
            if deal.state is DealState.RECEIVED:
                self.intake.parse(deal)
            elif deal.state is DealState.NEEDS_INFO:
                if s.clock.now() - deal.state_since >= s.policy.needs_info_timeout:
                    s.escalate(
                        deal,
                        ReviewReason.MISSING_FIELDS,
                        AGENT,
                        "the AE has not replied with the missing fields in 24 hours",
                        resume=DealState.RECEIVED,
                    )
            elif deal.state is DealState.AWAITING_TIER:
                self.intake.confirm_tier(deal)
            elif deal.state is DealState.TIER_CONFIRMED:
                self.intake.create_project(deal)
            elif deal.state is DealState.PROJECT_CREATED:
                self.comms.open_channel(deal)
            after = (deal.state, deal.call_attempts, deal.project_id, deal.channel_id)
            if after == before:
                return

    # ---- the human exits from HUMAN_REVIEW (§6.1) ----------------------------------------------
    def _reviewed(self, key: str) -> Deal:
        deal = self.s.store.get(key)
        if deal is None:
            raise ReviewError(f"no deal {key}")
        if deal.state is not DealState.HUMAN_REVIEW:
            raise ReviewError(f"{key} is {deal.state}, not waiting for a person")
        return deal

    def resolve(self, key: str, tier: Tier, by: str) -> Deal:
        """A person sets the tier. Only for tier reasons, and only with every field valid."""
        deal = self._reviewed(key)
        if deal.review_reason not in TIER_REASONS:
            raise ReviewError(
                f"{key} is waiting for '{deal.review_reason}', not for a tier: use retry or close"
            )
        if deal.notification is None:
            raise ReviewError(f"{key} has no valid deal fields; a tier cannot skip them")
        deal.tier = tier
        deal.tier_decided_by = by
        self.s.audit(
            deal,
            "human",
            "resolve",
            input={"by": by, "reason": deal.review_reason},
            output={"tier": tier},
            decision="tier set by a person",
            reason=f"{by} confirmed {tier.value}",
        )
        self._leave_review(deal)
        self.s.move(deal, DealState.TIER_CONFIRMED, "human", f"tier {tier.value} set by {by}")
        self.advance(deal)
        return deal

    def retry(self, key: str, by: str, *, not_duplicate: bool = False) -> Deal:
        """Resume from the saved step, after a person fixed the cause."""
        deal = self._reviewed(key)
        reason = deal.review_reason
        if not_duplicate and reason is not ReviewReason.POSSIBLE_DUPLICATE:
            raise ReviewError("--not-duplicate only answers a possible duplicate")
        if reason is ReviewReason.POSSIBLE_DUPLICATE and not not_duplicate:
            raise ReviewError("say whether it is a duplicate: `close`, or `retry --not-duplicate`")
        resume = deal.resume_state or DealState.RECEIVED
        if reason in TIER_REASONS:
            # "Try the AE again": a fresh set of calls, still under the same rules.
            deal.call_attempts = deal.unclear_calls = 0
            deal.next_call_at = self.s.clock.now()
            resume = DealState.AWAITING_TIER
        if reason in (ReviewReason.MISSING_FIELDS, ReviewReason.UNPARSEABLE):
            deal.notification = None
            deal.clarifications_sent = 0
        if not_duplicate:
            deal.not_duplicate_by = by
        self.s.audit(
            deal,
            "human",
            "retry",
            input={"by": by, "reason": reason},
            output={"resume": resume},
            decision="resume",
            reason=f"{by} asked to resume" + (" (not a duplicate)" if not_duplicate else ""),
        )
        self._leave_review(deal)
        self.s.move(deal, resume, "human", f"resumed by {by}")
        self.advance(deal)
        return deal

    def close(self, key: str, by: str, why: str) -> Deal:
        deal = self._reviewed(key)
        self.s.audit(
            deal,
            "human",
            "close",
            input={"by": by, "reason": deal.review_reason},
            decision="closed",
            reason=why,
        )
        self._leave_review(deal)
        self.s.move(deal, DealState.CLOSED, "human", f"closed by {by}: {why}")
        return deal

    def _leave_review(self, deal: Deal) -> None:
        deal.review_reason = None
        deal.review_detail = ""
        deal.resume_state = None
