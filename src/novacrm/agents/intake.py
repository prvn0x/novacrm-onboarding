"""The Intake & Routing Agent: inbox → valid deal → confirmed tier → verified Rocketlane project.

Each public method is one step of the workflow (design §6) and leaves the deal in a saved state.
The LLM is used in exactly two places, both behind validation: reading the email (`extraction`)
and reading the call transcript (`tier`). Everything else here is plain code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import timedelta

from ..config import AccountExecutive, TemplateRef
from ..domain import (
    PHASES,
    SIGNOFF_TASK,
    TEMPLATE_TASK_COUNT,
    TIER_POLICIES,
    Deal,
    DealState,
    InboundEmail,
    ReviewReason,
    Tier,
    TierDecision,
    TierOutcome,
    opportunity_id_from_link,
    project_start,
)
from ..plan import schedule
from ..ports import (
    IntegrationError,
    PermanentError,
    ProjectGone,
    ProjectInfo,
    ProjectSpec,
    TaskSpec,
    TransientError,
)
from ..retry import with_retries
from . import tier as tier_reader
from .base import Services
from .extraction import Extraction, clarification_body, extract, grounded

AGENT = "intake"
KICKOFF_TASK = "Kickoff call with {contact}"
_READING_REVIEWS = frozenset({ReviewReason.MISSING_FIELDS, ReviewReason.UNPARSEABLE})


def _still_reading(deal: Deal) -> bool:
    """The deal has no complete email yet (waiting for the AE, or for a person about it)."""
    if deal.notification is not None:
        return False
    return deal.state in (DealState.RECEIVED, DealState.NEEDS_INFO) or (
        deal.state is DealState.HUMAN_REVIEW and deal.review_reason in _READING_REVIEWS
    )


@dataclass(frozen=True, slots=True)
class ProjectReady:
    """The handoff from the Intake Agent to the Communication Agent (design D1)."""

    deal: str
    tier: Tier
    csm_email: str
    contact_email: str
    project_url: str
    start: str


DURATION_TOLERANCE_DAYS = 3  # weekend drift of a working-day schedule


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _same_customer(a: str, b: str) -> bool:
    return bool(_letters(a)) and _letters(a) == _letters(b)


class IntakeAgent:
    def __init__(self, services: Services) -> None:
        self.s = services

    # ---- steps 1-2: the inbox --------------------------------------------------------------
    def receive(self, email: InboundEmail) -> None:
        """Turn one new inbox message into a new deal, or route it to the deal of its thread.

        A message moves an existing deal only if the deal is waiting for the AE's answer and the
        message is from that AE; anything else in the thread is noted and changes nothing (D7).
        """
        s = self.s
        if s.store.seen(email.message_id):
            return  # handled before a crash stopped it from being labelled
        existing = s.store.by_thread(email.thread_id)
        ae = s.team.ae_by_email(email.sender)
        if existing is not None:
            existing.message_ids.append(email.message_id)
            # The AE answering a question, or the AE sending a real deal into a thread that was
            # judged "not a deal" earlier, re-opens it; nothing else in a thread does.
            waiting = _still_reading(existing) or existing.state is DealState.REJECTED
            if waiting and ae is not None and email.sender == existing.ae_email:
                existing.thread_text = email.thread_text
                existing.thread_id = email.thread_id
                existing.notification = None  # re-read the thread, reply included
                if existing.state is DealState.HUMAN_REVIEW:
                    existing.review_reason, existing.review_detail = None, ""
                s.move(existing, DealState.RECEIVED, AGENT, "the AE wrote again in the thread")
            else:
                s.store.save(existing)
                s.audit(
                    existing,
                    AGENT,
                    "followup_ignored",
                    input={"from": email.sender, "subject": email.subject},
                    decision="no change",
                    reason=f"the deal is {existing.state}; only its AE's answer to a question "
                    "moves it",
                )
            s.store.remember(email.message_id, existing.key)
            return

        deal = self._new_deal(email, DealState.RECEIVED if ae else DealState.REJECTED)
        if ae is None:
            s.audit(
                deal,
                AGENT,
                "screen_sender",
                input={"from": email.sender, "subject": email.subject},
                decision="reject",
                reason="the sender is not in the AE directory; nothing is processed",
            )
        else:
            s.audit(
                deal,
                AGENT,
                "received",
                input={"from": email.sender, "subject": email.subject},
                decision="process",
                reason=f"new email from AE {ae.name}",
            )
        s.store.remember(email.message_id, deal.key)

    def _new_deal(self, email: InboundEmail, state: DealState) -> Deal:
        deal = Deal(
            key=self.s.store.new_key(),
            thread_id=email.thread_id,
            state=state,
            state_since=self.s.clock.now(),
            received_at=email.received_at,
            ae_email=email.sender,
            subject=email.subject,
            thread_text=email.thread_text,
            message_ids=[email.message_id],
        )
        self.s.store.save(deal)
        self.s.store.attach_thread(email.thread_id, deal.key)
        return deal

    # ---- steps 3-4: read, validate, de-duplicate ---------------------------------------------
    def parse(self, deal: Deal) -> None:
        s = self.s
        if s.waiting(deal):
            return
        ae = s.team.ae_by_email(deal.ae_email)
        assert ae is not None  # receive() only creates RECEIVED deals for known AEs
        if deal.notification is None:
            extraction = self._extract(deal, ae)
            if extraction is None:
                return
            if not extraction.is_deal:
                s.audit(
                    deal,
                    AGENT,
                    "extract",
                    output=extraction.raw,
                    decision="reject",
                    reason="not a deal notification and no deal field found",
                )
                s.move(deal, DealState.REJECTED, AGENT, "not a deal notification")
                return
            if extraction.notification is None:
                self._ask_for_missing(deal, ae, extraction)
                return
            deal.notification = extraction.notification
            s.store.save(deal)
            s.audit(
                deal,
                AGENT,
                "extract",
                input={"thread_chars": len(deal.thread_text)},
                output=extraction.notification.model_dump(),
                decision="valid",
                reason="every required field is present, well-formed and found in the email"
                + (f"; notes: {'; '.join(extraction.problems)}" if extraction.problems else ""),
            )
            if self._merge_with_other_emails(deal):
                return
        self._check_existing_projects(deal)

    def _extract(self, deal: Deal, ae: AccountExecutive) -> Extraction | None:
        s = self.s
        error: Exception | None = None
        for _ in range(2):  # one immediate retry (design D6)
            try:
                return extract(deal.thread_text, ae, s.llm)
            except Exception as exc:
                error = exc
        deal.parse_failures += 1
        s.store.save(deal)
        s.audit(
            deal,
            AGENT,
            "extract",
            decision="wait",
            reason=f"could not read the email ({error}); poll {deal.parse_failures}",
        )
        if deal.parse_failures >= s.policy.max_parse_failures:
            deal.parse_failures = 0  # `novacrm retry` gets a fresh set of attempts
            s.escalate(
                deal,
                ReviewReason.UNPARSEABLE,
                AGENT,
                f"the email could not be read after {s.policy.max_parse_failures} polls",
                resume=DealState.RECEIVED,
            )
        return None

    def _ask_for_missing(self, deal: Deal, ae: AccountExecutive, extraction: Extraction) -> None:
        s = self.s
        customer = extraction.raw.get("customer_name")
        if isinstance(customer, str) and grounded(customer, deal.thread_text):
            deal.partial_customer = customer.strip()
        link = extraction.raw.get("opportunity_link")
        if isinstance(link, str) and grounded(link, deal.thread_text):
            deal.partial_opportunity = opportunity_id_from_link(link) or ""
        detail = ", ".join(extraction.missing)
        if deal.clarifications_sent >= s.policy.max_clarifications:
            s.escalate(
                deal,
                ReviewReason.MISSING_FIELDS,
                AGENT,
                f"still missing after {deal.clarifications_sent} requests: {detail}",
                resume=DealState.RECEIVED,
            )
            return
        body = clarification_body(extraction, ae)
        try:
            s.inbox.reply(deal.thread_id, ae.email, f"Re: {deal.subject}", body)
        except TransientError as exc:
            s.park(deal, AGENT, exc)  # nothing was sent; the next poll asks again
            return
        deal.clarifications_sent += 1
        s.audit(
            deal,
            AGENT,
            "ask_ae",
            input={"missing": extraction.missing, "problems": extraction.problems},
            output={"reply": body},
            decision="ask, do not guess",
            reason=f"required fields missing or invalid: {detail}",
        )
        s.move(deal, DealState.NEEDS_INFO, AGENT, f"asked the AE for: {detail}")

    def _merge_with_other_emails(self, deal: Deal) -> bool:
        """One opportunity, one deal, whichever email arrived first (D5).

        A deal already past reading its email keeps going and this one is merged into it. An
        earlier deal still waiting for missing fields (same opportunity, or same AE and customer)
        is merged into this complete one instead, so the complete email is never held back.
        Returns True if this deal stopped here.
        """
        s = self.s
        assert deal.notification is not None
        n = deal.notification
        same_opportunity = [
            d
            for d in s.store.by_opportunity(n.opportunity_id)
            if d.key != deal.key and d.state not in (DealState.REJECTED, DealState.CLOSED)
        ]
        for other in same_opportunity:
            if _still_reading(other):
                continue
            deal.merged_into = other.key
            s.store.attach_thread(deal.thread_id, other.key)
            if other.state is DealState.CHANNEL_CREATED or other.state is DealState.DUPLICATE:
                self._tell_ae_duplicate(deal, other.project_url)
                s.move(deal, DealState.DUPLICATE, AGENT, f"already onboarded as {other.key}")
            else:
                s.move(
                    deal,
                    DealState.CLOSED,
                    AGENT,
                    f"merged into {other.key}, which is already {other.state}",
                )
            return True
        same_keys = {d.key for d in same_opportunity}
        waiting = [
            d
            for d in s.store.open_deals()
            if d.key != deal.key
            and _still_reading(d)
            and (
                d.key in same_keys
                or (
                    d.ae_email == deal.ae_email
                    and _same_customer(d.partial_customer, n.customer_name)
                )
            )
        ]
        for other in waiting:
            other.merged_into = deal.key
            s.store.attach_thread(other.thread_id, deal.key)
            s.move(
                other,
                DealState.CLOSED,
                AGENT,
                f"superseded by {deal.key}, a complete email for the same deal",
            )
        return False

    def _check_existing_projects(self, deal: Deal) -> None:
        """Step 4: before anyone is called, is this onboarding already in Rocketlane?"""
        s = self.s
        assert deal.notification is not None
        n = deal.notification
        delays = s.policy.retry_delays
        try:
            by_ref = with_retries(
                lambda: s.projects.find_by_reference(n.opportunity_id), delays, s.sleep
            )
            by_name = (
                []
                if by_ref or deal.not_duplicate_by
                else with_retries(lambda: s.projects.find_by_name(n.customer_name), delays, s.sleep)
            )
        except TransientError as exc:
            s.park(deal, AGENT, exc)
            return
        except PermanentError as exc:
            s.escalate(deal, ReviewReason.REJECTED_BY_ROCKETLANE, AGENT, str(exc))
            return
        if by_ref is not None:
            deal.project_id, deal.project_url = by_ref.project_id, by_ref.url
            self._tell_ae_duplicate(deal, by_ref.url)
            s.audit(
                deal,
                AGENT,
                "duplicate_check",
                input={"opportunity": n.opportunity_id},
                output={"project": by_ref.project_id},
                decision="duplicate",
                reason="a project for this opportunity already exists; no call made",
            )
            s.move(deal, DealState.DUPLICATE, AGENT, "project already exists")
            return
        if by_name:
            links = ", ".join(p.url for p in by_name)
            s.audit(
                deal,
                AGENT,
                "duplicate_check",
                input={"customer": n.customer_name},
                output={"projects": [p.project_id for p in by_name]},
                decision="possible duplicate",
                reason="a project for this customer exists without this opportunity's "
                "reference (made by hand?); no call made",
            )
            s.escalate(
                deal,
                ReviewReason.POSSIBLE_DUPLICATE,
                AGENT,
                f"{n.customer_name} already has: {links}. Close this deal if it is the "
                "same onboarding, or `novacrm retry --not-duplicate`.",
                resume=DealState.RECEIVED,
            )
            return
        deal.reference_checked = True
        deal.next_call_at = s.clock.now()
        s.audit(
            deal,
            AGENT,
            "duplicate_check",
            input={"opportunity": n.opportunity_id},
            decision="new",
            reason="no project for this opportunity or customer",
        )
        s.move(deal, DealState.AWAITING_TIER, AGENT, "ready to confirm the tier with the AE")

    def _tell_ae_duplicate(self, deal: Deal, url: str) -> None:
        s = self.s
        ae = s.team.ae_by_email(deal.ae_email)
        name = ae.name.split()[0] if ae else "there"
        body = (
            f"Hi {name},\n\nThis opportunity already has an onboarding project, so I have not "
            f"created another one:\n{url}\n\nNovaCRM Onboarding Assistant (automated)"
        )
        try:
            s.inbox.reply(deal.thread_id, deal.ae_email, f"Re: {deal.subject}", body)
        except IntegrationError as exc:
            s.audit(deal, AGENT, "reply_failed", reason=str(exc))

    # ---- step 5: the call ------------------------------------------------------------------
    def confirm_tier(self, deal: Deal) -> None:
        s = self.s
        policy = s.policy
        now = s.clock.now()
        if deal.pending_call_id:
            self._collect_call(deal)
            return
        if deal.next_call_at and now < deal.next_call_at:
            return
        if deal.abandoned_call_id and self._late_result_arrived(deal):
            return
        if deal.call_attempts >= policy.max_calls:
            # Only reachable after a crash between placing a call and saving its id.
            s.escalate(
                deal, ReviewReason.AE_UNREACHABLE, AGENT, "no confirmed answer after the last call"
            )
            return
        ae = s.team.ae_by_email(deal.ae_email)
        assert ae is not None and deal.notification is not None
        # Intent first (D5): the attempt is counted and spaced before the call is placed.
        deal.call_attempts += 1
        deal.call_started_at = now
        deal.next_call_at = now + policy.call_spacing
        s.store.save(deal)
        variables = {
            "customer_name": deal.notification.customer_name,
            "ae_first_name": ae.name.split()[0],
            "deal": deal.key,
        }
        try:
            call_id = s.voice.place_call(ae.phone, variables)
        except IntegrationError as exc:
            s.audit(
                deal,
                AGENT,
                "place_call",
                input={"to": ae.phone, "attempt": deal.call_attempts},
                decision="failed attempt",
                reason=str(exc),
            )
            self._after_unsuccessful_call(deal, ReviewReason.AE_UNREACHABLE, str(exc))
            return
        deal.pending_call_id = call_id
        s.store.save(deal)
        s.audit(
            deal,
            AGENT,
            "place_call",
            input={"to": ae.phone, "attempt": deal.call_attempts, "variables": variables},
            output={"call": call_id},
            decision="calling",
            reason=f"confirming the plan tier with {ae.name}",
        )

    def _collect_call(self, deal: Deal) -> None:
        s = self.s
        assert deal.pending_call_id is not None
        try:
            result = s.voice.get_result(deal.pending_call_id)
            problem = ""
        except TransientError as exc:
            result, problem = None, str(exc)
        except PermanentError as exc:
            result, problem = None, str(exc)
            deal.call_started_at = None  # no point waiting: give up on this call now
        if result is None:
            started = deal.call_started_at
            if started is not None and s.clock.now() - started < s.policy.call_result_timeout:
                return  # still ringing or in progress, or a brief outage
            detail = problem or "the voice platform never reported the call as ended"
            deal.abandoned_call_id = deal.pending_call_id
            deal.pending_call_id = None
            s.store.save(deal)
            s.audit(deal, AGENT, "call_result", decision="failed attempt", reason=detail)
            if deal.call_attempts >= s.policy.max_calls and deal.abandoned_call_id:
                return  # the last call: re-read it at the next call time before escalating
            self._after_unsuccessful_call(deal, ReviewReason.AE_UNREACHABLE, detail)
            return
        try:
            decision = tier_reader.decide(result, s.llm)
        except Exception as exc:
            decision = TierDecision(
                TierOutcome.UNCLEAR, None, f"could not interpret the transcript ({exc})"
            )
        deal.pending_call_id = None
        s.audit(
            deal,
            AGENT,
            "tier_decision",
            input={
                "call": result.call_id,
                "attempt": deal.call_attempts,
                "status": result.status,
                "transcript": [f"{t.speaker}: {t.text}" for t in result.turns],
            },
            output={
                "outcome": decision.outcome,
                "tier": decision.tier,
                "evidence": decision.evidence,
            },
            decision=decision.outcome,
            reason=decision.reason,
        )
        if decision.outcome is TierOutcome.CONFIRMED:
            assert decision.tier is not None
            deal.tier = decision.tier
            deal.tier_decided_by = "ae-call"
            s.move(deal, DealState.TIER_CONFIRMED, AGENT, decision.reason)
        elif decision.outcome is TierOutcome.UNSURE:
            s.escalate(deal, ReviewReason.AE_UNSURE, AGENT, decision.reason)
        elif decision.outcome is TierOutcome.UNCLEAR:
            deal.unclear_calls += 1
            if deal.unclear_calls >= s.policy.max_unclear_calls:
                s.escalate(
                    deal,
                    ReviewReason.TIER_UNCLEAR,
                    AGENT,
                    f"{deal.unclear_calls} unclear answers; last: {decision.reason}",
                )
            else:
                self._after_unsuccessful_call(deal, ReviewReason.TIER_UNCLEAR, decision.reason)
        else:
            self._after_unsuccessful_call(deal, ReviewReason.AE_UNREACHABLE, decision.reason)

    def _late_result_arrived(self, deal: Deal) -> bool:
        """Before calling again: did the call we stopped waiting for finish after all? If the AE
        confirmed on it, they should not be called a second time."""
        s = self.s
        call_id, deal.abandoned_call_id = deal.abandoned_call_id, None
        assert call_id is not None
        try:
            result = s.voice.get_result(call_id)
        except IntegrationError:
            result = None
        s.store.save(deal)
        if result is None:
            return False
        deal.pending_call_id = call_id
        deal.call_started_at = s.clock.now()
        self._collect_call(deal)
        return True

    def _after_unsuccessful_call(self, deal: Deal, reason: ReviewReason, detail: str) -> None:
        s = self.s
        if deal.call_attempts >= s.policy.max_calls:
            s.escalate(deal, reason, AGENT, f"{deal.call_attempts} calls; last: {detail}")
            return
        s.store.save(deal)
        s.audit(
            deal,
            AGENT,
            "schedule_call",
            output={"next_call_at": deal.next_call_at, "attempt": deal.call_attempts + 1},
            decision="retry",
            reason=detail,
        )

    # ---- steps 6-8: the project ------------------------------------------------------------
    def create_project(self, deal: Deal) -> ProjectReady | None:
        s = self.s
        assert deal.tier is not None and deal.notification is not None
        if s.waiting(deal):
            return None
        template = s.team.rocketlane.templates.get(deal.tier)  # None in api mode
        if not deal.csm_email:
            deal.csm_email = s.team.csm_for(deal.tier, int(deal.key.lstrip("D"))).email
            s.store.save(deal)
        try:
            info = self._project_for(deal, template)
            if info is None:
                return None
            if template is None:
                self._ensure_plan(deal, info)
            problems = self._verify(info, deal, template)
            if not problems:
                self._ensure_kickoff_task(deal, info)
        except TransientError as exc:
            s.park(deal, AGENT, exc)
            return None
        except PermanentError as exc:
            s.escalate(
                deal,
                ReviewReason.REJECTED_BY_ROCKETLANE,
                AGENT,
                str(exc),
                resume=DealState.TIER_CONFIRMED,
            )
            return None
        record = {"templates": info.template_names, "duration": info.planned_duration_days}
        if problems:
            s.audit(
                deal,
                AGENT,
                "verify_project",
                input={"project": info.project_id},
                output=record,
                decision="mismatch",
                reason="; ".join(problems),
            )
            s.escalate(
                deal,
                ReviewReason.TEMPLATE_MISMATCH,
                AGENT,
                f"project {info.url}: {'; '.join(problems)}. Fix or delete it, then "
                "`novacrm retry`.",
                resume=DealState.TIER_CONFIRMED,
            )
            return None
        label = TIER_POLICIES[deal.tier].label
        s.audit(
            deal,
            AGENT,
            "verify_project",
            input={"project": info.project_id},
            output=record,
            decision="verified",
            reason=f"{label} template; {info.planned_duration_days} days; 4 phases and "
            f"{TEMPLATE_TASK_COUNT} tasks; sign-off owned by the customer; CSM on the "
            "project",
        )
        # Slack states the go-live date: take Rocketlane's, which counts working days.
        deal.go_live = info.due_date.isoformat() if info.due_date else ""
        s.move(deal, DealState.PROJECT_CREATED, AGENT, f"project {info.url} verified")
        handoff = ProjectReady(
            deal.key,
            deal.tier,
            deal.csm_email,
            deal.notification.contact_email,
            deal.project_url,
            project_start(deal.received_at.date()).isoformat(),
        )
        s.audit(deal, AGENT, "handoff", output=handoff, decision="to the communication agent")
        return handoff

    def _project_for(self, deal: Deal, template: TemplateRef | None) -> ProjectInfo | None:
        """The project this deal owns: the saved one, an adopted one, or a new one (D5)."""
        s = self.s
        assert deal.notification is not None and deal.tier is not None
        n = deal.notification
        delays = s.policy.retry_delays
        if deal.project_id is not None:
            pid = deal.project_id
            try:
                return with_retries(lambda: s.projects.get_project(pid), delays, s.sleep)
            except ProjectGone:
                s.audit(
                    deal,
                    AGENT,
                    "project_gone",
                    input={"project": pid},
                    decision="create again",
                    reason="the saved project was deleted",
                )
                deal.project_id, deal.project_url = None, ""
                s.store.save(deal)

        found = with_retries(
            lambda: s.projects.find_by_reference(n.opportunity_id), delays, s.sleep
        )
        if found is not None:
            # Step 4 saw no project with this reference before the call, and a person making a
            # project by hand does not set it: a project carrying it now is our earlier create
            # whose response was lost (D5). No clocks are compared.
            deal.project_id, deal.project_url = found.project_id, found.url
            s.store.save(deal)
            s.audit(
                deal,
                AGENT,
                "adopt_project",
                output={"project": found.project_id},
                decision="adopt",
                reason="our earlier create landed but its response was lost",
            )
            return found
        if not deal.not_duplicate_by:
            by_name = with_retries(
                lambda: s.projects.find_by_name(n.customer_name), delays, s.sleep
            )
            if by_name:
                s.escalate(
                    deal,
                    ReviewReason.POSSIBLE_DUPLICATE,
                    AGENT,
                    f"a project for {n.customer_name} appeared while the tier was being "
                    f"confirmed: {', '.join(p.url for p in by_name)}",
                    resume=DealState.TIER_CONFIRMED,
                )
                return None

        start = project_start(deal.received_at.date())
        spec = ProjectSpec(
            name=f"{n.customer_name} - {TIER_POLICIES[deal.tier].label} onboarding",
            customer_name=n.customer_name,
            contact_email=n.contact_email,
            owner_email=s.team.cs_manager.email,
            csm_email=deal.csm_email,
            start_date=start,
            external_reference_id=n.opportunity_id,
        )
        if template is not None:  # template mode (D4)
            spec = replace(
                spec,
                template_id=template.template_id,
                pm_placeholder_id=template.pm_placeholder_id,
                customer_placeholder_id=template.customer_placeholder_id,
            )
        else:  # api mode: dates from the plan; phases and tasks follow in _ensure_plan
            spec = replace(spec, due_date=start + timedelta(days=s.plan.durations[deal.tier]))
        # Not retried here: if the response is lost, the next attempt's lookup above adopts it.
        created = s.projects.create_project(spec)
        deal.project_id, deal.project_url = created.project_id, created.url
        s.store.save(deal)
        s.audit(
            deal,
            AGENT,
            "create_project",
            input=spec,
            output={"project": created.project_id, "url": created.url},
            decision="created",
            reason=f"tier {deal.tier} confirmed by {deal.tier_decided_by}",
        )
        return created

    def _verify(self, info: ProjectInfo, deal: Deal, template: TemplateRef | None) -> list[str]:
        """Verify, don't trust (D4): the template, its plan, the people, not just what we sent."""
        s = self.s
        assert deal.tier is not None and deal.notification is not None
        label = TIER_POLICIES[deal.tier].label
        problems = []
        if template is not None:
            if template.template_id not in info.template_ids:
                problems.append(f"source templates {info.template_ids} lack {template.template_id}")
            if not any(label.lower() in name.lower() for name in info.template_names):
                problems.append(f"template name {list(info.template_names)} does not say {label}")
        expected = template.planned_duration_days if template else s.plan.durations[deal.tier]
        # Rocketlane schedules a template in working days, so the calendar length moves with the
        # start weekday (seen live: 29 days from a Wednesday, 31 from a Thursday). A few days of
        # drift is the calendar; a 14-day plan where 30 was expected is the wrong template.
        actual = info.planned_duration_days
        if actual is None or abs(actual - expected) > DURATION_TOLERANCE_DAYS:
            problems.append(
                f"planned duration {actual} days, expected about {expected} "
                f"(±{DURATION_TOLERANCE_DAYS})"
            )
        if deal.csm_email.lower() not in {m.lower() for m in info.member_emails}:
            problems.append(f"the CSM {deal.csm_email} is not on the project")
        delays = s.policy.retry_delays
        phases = with_retries(lambda: s.projects.list_phases(info.project_id), delays, s.sleep)
        tasks = with_retries(lambda: s.projects.list_tasks(info.project_id), delays, s.sleep)
        ours = KICKOFF_TASK.format(contact=self._contact(deal))
        waits = s.policy.template_copy_waits if template is not None else ()
        for wait in waits:  # the template's tasks may still be copying
            if len([t for t in tasks if t.name != ours]) >= TEMPLATE_TASK_COUNT:
                break
            s.sleep(wait)
            tasks = with_retries(lambda: s.projects.list_tasks(info.project_id), delays, s.sleep)
        names = [p.name for p in phases]
        if sorted(names) != sorted(PHASES):
            problems.append(f"phases {names}, expected {list(PHASES)}")
        template_tasks = [t for t in tasks if t.name != ours]
        if len(template_tasks) != TEMPLATE_TASK_COUNT:
            problems.append(f"{len(template_tasks)} template tasks, expected {TEMPLATE_TASK_COUNT}")
        signoff = next((t for t in tasks if t.name == SIGNOFF_TASK), None)
        contact = deal.notification.contact_email.lower()
        on_project = contact in {m.lower() for m in info.member_emails}
        owned_by_customer = signoff is not None and (
            contact in {e.lower() for e in signoff.assignee_emails}
            or (
                template is not None
                and template.customer_placeholder_id in signoff.placeholder_ids
                and on_project
            )
        )
        if signoff is None:
            problems.append(f"no '{SIGNOFF_TASK}' task")
        elif not owned_by_customer:
            problems.append(f"'{SIGNOFF_TASK}' is not assigned to the customer")
        return problems

    def _ensure_plan(self, deal: Deal, info: ProjectInfo) -> None:
        """api mode: create the plan's phases, tasks and dependency through the API. Every write
        is preceded by a lookup by name, so a retry after a lost response creates nothing twice."""
        s = self.s
        assert deal.tier is not None and deal.notification is not None
        delays = s.policy.retry_delays
        pid = info.project_id
        dated = schedule(s.plan, deal.tier, project_start(deal.received_at.date()))
        phases = {
            p.name: p.phase_id
            for p in with_retries(lambda: s.projects.list_phases(pid), delays, s.sleep)
        }
        tasks = {
            t.name: t for t in with_retries(lambda: s.projects.list_tasks(pid), delays, s.sleep)
        }
        made_phases = made_tasks = 0
        for phase in dated:
            if phase.name not in phases:
                phases[phase.name] = s.projects.create_phase(
                    pid, phase.name, phase.start, phase.due
                )
                made_phases += 1
            for task in phase.tasks:
                if task.name in tasks:
                    continue
                owner = deal.csm_email if task.owner == "pm" else deal.notification.contact_email
                spec = TaskSpec(
                    pid,
                    phases[phase.name],
                    task.name,
                    task.description,
                    owner,
                    task.due,
                    task.start,
                )
                s.projects.create_task(spec)
                made_tasks += 1
        if made_tasks:
            tasks = {t.name: t for t in s.projects.list_tasks(pid)}
        for phase in dated:
            for task in phase.tasks:
                if task.depends_on:
                    before = tasks[task.depends_on].task_id
                    if before not in tasks[task.name].dependency_ids:
                        s.projects.add_dependency(tasks[task.name].task_id, before)
        s.audit(
            deal,
            AGENT,
            "build_plan",
            input={"project": pid, "tier": deal.tier},
            output={"phases_created": made_phases, "tasks_created": made_tasks},
            decision="built" if made_tasks else "already built",
            reason="api mode: the onboarding plan created through the phase and task APIs",
        )

    def _contact(self, deal: Deal) -> str:
        assert deal.notification is not None
        return deal.notification.contact_name or deal.notification.contact_email

    def _ensure_kickoff_task(self, deal: Deal, info: ProjectInfo) -> None:
        """The one deal-specific task, created through the API (submission guideline): the
        kickoff call with this customer's contact, carrying the AE's notes, owned by the CSM."""
        s = self.s
        assert deal.notification is not None
        if deal.kickoff_task_id is not None:
            return
        name = KICKOFF_TASK.format(contact=self._contact(deal))
        delays = s.policy.retry_delays
        tasks = with_retries(lambda: s.projects.list_tasks(info.project_id), delays, s.sleep)
        existing = next((t for t in tasks if t.name == name), None)
        if existing is None:
            phases = with_retries(lambda: s.projects.list_phases(info.project_id), delays, s.sleep)
            kickoff = next(p for p in phases if p.name == PHASES[0])
            start = project_start(deal.received_at.date())
            spec = TaskSpec(
                project_id=info.project_id,
                phase_id=kickoff.phase_id,
                name=name,
                description=f"AE: {deal.notification.ae_name}. Notes from the deal email: "
                f"{deal.notification.notes or 'none'}",
                assignee_email=deal.csm_email,
                due_date=start + timedelta(days=3 if deal.tier is Tier.ENTERPRISE else 2),
            )
            deal.kickoff_task_id = s.projects.create_task(spec)
            s.audit(
                deal,
                AGENT,
                "create_task",
                input=spec,
                output={"task": deal.kickoff_task_id},
                decision="created",
                reason="deal-specific kickoff task with the AE's notes",
            )
        else:
            deal.kickoff_task_id = existing.task_id
        s.store.save(deal)
