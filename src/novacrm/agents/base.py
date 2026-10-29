"""What every agent shares: the systems it may use, and the three ways it changes a deal.

`move` changes state, `park` handles an outage (D6) and `escalate` hands a deal to a person
(§6.1). All three save the deal and write the audit row in one place, so no step can change a deal
without leaving a record of why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Policy, Team
from ..domain import Deal, DealState, ReviewReason
from ..plan import Plan
from ..ports import LLM, ChatWorkspace, Clock, Inbox, ProjectStore, VoiceCaller
from ..store import Store


@dataclass
class Services:
    store: Store
    inbox: Inbox
    voice: VoiceCaller
    projects: ProjectStore
    chat: ChatWorkspace
    llm: LLM
    clock: Clock
    team: Team
    policy: Policy
    sleep: Callable[[float], None]
    plan: Plan

    def audit(
        self,
        deal: Deal,
        agent: str,
        action: str,
        *,
        input: Any = None,
        output: Any = None,
        decision: str = "",
        reason: str = "",
    ) -> None:
        self.store.audit(
            self.clock.now(),
            deal.key,
            agent,
            action,
            input=input,
            output=output,
            decision=decision,
            reason=reason,
        )

    def move(self, deal: Deal, state: DealState, agent: str, reason: str) -> None:
        old = deal.state
        deal.state = state
        deal.state_since = self.clock.now()
        deal.pending_since = None  # any outage wait ends when the deal moves on
        deal.next_retry_at = None
        deal.outage_notified = False
        self.store.save(deal)
        self.audit(
            deal,
            agent,
            "state",
            input={"from": old},
            output={"to": state},
            decision=state.value,
            reason=reason,
        )

    def waiting(self, deal: Deal) -> bool:
        """True while a deal is waiting out an outage between retries (D6)."""
        return deal.next_retry_at is not None and self.clock.now() < deal.next_retry_at

    def park(self, deal: Deal, agent: str, error: Exception) -> None:
        """An outage: the deal keeps its state and is retried on later polls (at most every
        5 minutes). The CS Manager is told once after 1 hour; after 24 hours a person decides."""
        now = self.clock.now()
        if deal.pending_since is None:
            deal.pending_since = now
        deal.next_retry_at = now + self.policy.outage_retry_every
        waited = now - deal.pending_since
        self.store.save(deal)
        self.audit(
            deal,
            agent,
            "wait_for_outage",
            decision="retry later",
            reason=f"{error}; waiting {waited}",
        )
        if waited >= self.policy.outage_give_up_after:
            self.escalate(
                deal, ReviewReason.OUTAGE, agent, f"still failing after {waited}: {error}"
            )
        elif waited >= self.policy.outage_notice_after and not deal.outage_notified:
            deal.outage_notified = True
            self.store.save(deal)
            self.notify(
                f"{error} - deal {deal.key} has been waiting since {deal.pending_since:%H:%M} UTC "
                "and will resume on its own. No action needed yet."
            )

    def escalate(
        self,
        deal: Deal,
        reason: ReviewReason,
        agent: str,
        detail: str,
        *,
        resume: DealState | None = None,
    ) -> None:
        """Hand the deal to a person. `resume` is where `novacrm retry` restarts it."""
        deal.resume_state = resume or deal.state
        deal.review_reason = reason
        deal.review_detail = detail
        self.move(deal, DealState.HUMAN_REVIEW, agent, f"{reason}: {detail}")
        customer = deal.notification.customer_name if deal.notification else deal.subject
        self.notify(
            f":warning: Deal *{deal.key}* ({customer}) needs a person: *{reason}*.\n{detail}\n"
            f"See `novacrm trace {deal.key}`; then `novacrm resolve`, `retry` or `close`."
        )

    def notify(self, text: str) -> None:
        """Best effort: a Slack outage must not block the deal that is being escalated."""
        try:
            self.chat.post(self.team.escalation_channel, text)
        except Exception as exc:
            self.store.audit(
                self.clock.now(), "-", "orchestrator", "notify_failed", reason=str(exc)
            )
