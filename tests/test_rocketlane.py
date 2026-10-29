"""Brief, Part 4: template accuracy, Rocketlane being down, and an existing onboarding project."""

from __future__ import annotations

import pytest
from conftest import (
    ENTERPRISE_TEMPLATE,
    GROWTH_TEMPLATE,
    OPP_15,
    OPP_18,
    OPP_LINK,
    TEAM,
    World,
    deal_email,
)
from pydantic import ValidationError

from novacrm.config import Team
from novacrm.domain import DealState, ReviewReason, Tier
from novacrm.fakes import confirms
from novacrm.ports import PermanentError, TransientError

# ---- template accuracy ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "template", "days", "dedicated"),
    [("enterprise", ENTERPRISE_TEMPLATE, 30, True), ("growth", GROWTH_TEMPLATE, 14, False)],
)
def test_the_confirmed_tier_picks_the_template_length_and_csm(
    world: World, tier: str, template: int, days: int, dedicated: bool
) -> None:
    world.voice.script = [confirms(tier)]
    world.send()
    world.poll()

    (spec,) = world.rocketlane.created
    (project,) = world.projects_for()
    assert spec.template_id == template and project["duration"] == days
    csms = {c.email for c in TEAM.dedicated_csms}
    assert (spec.csm_email in csms) is dedicated
    assert world.deal().state is DealState.CHANNEL_CREATED
    verify = next(r for r in world.store.trail("D1") if r["action"] == "verify_project")
    assert verify["decision"] == "verified"


def test_a_project_that_does_not_match_its_template_goes_to_a_person(world: World) -> None:
    """A template built with the wrong length in Rocketlane is caught, not left in place."""
    world.rocketlane.override_duration = 14
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    deal = world.deal()
    assert deal.state is DealState.HUMAN_REVIEW
    assert deal.review_reason is ReviewReason.TEMPLATE_MISMATCH
    assert "planned duration 14 days, expected about 30" in deal.review_detail
    assert world.slack.channels == {}

    # The person deletes the bad project and fixes the template; retry builds a good one.
    world.rocketlane.delete(deal.project_id or 0)
    world.rocketlane.override_duration = None
    world.orchestrator.retry("D1", by="priya")
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.projects_for()) == 1


def test_a_wrong_template_id_in_config_is_caught_by_name(world: World) -> None:
    swapped = TEAM.model_copy(deep=True)
    ent = swapped.rocketlane.templates[Tier.ENTERPRISE]
    grw = swapped.rocketlane.templates[Tier.GROWTH]
    ent.template_id, grw.template_id = grw.template_id, ent.template_id
    world.services.team = swapped
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    deal = world.deal()
    assert deal.review_reason is ReviewReason.TEMPLATE_MISMATCH
    assert "does not say Enterprise" in deal.review_detail


def test_a_config_that_maps_two_tiers_to_one_template_is_refused() -> None:
    raw = TEAM.model_dump(mode="json")
    raw["rocketlane"]["templates"]["growth"]["template_id"] = ENTERPRISE_TEMPLATE
    with pytest.raises(ValidationError, match="same Rocketlane template"):
        Team.model_validate(raw)


# ---- Rocketlane down ------------------------------------------------------------------------


def test_a_brief_read_failure_is_retried_within_the_step(world: World) -> None:
    world.rocketlane.fail = [TransientError("rocketlane", "503")]
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED


def test_a_lost_create_response_never_makes_a_second_project(world: World) -> None:
    world.rocketlane.crash_after_create = True
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.TIER_CONFIRMED  # waiting to retry
    world.clock.advance(minutes=6)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.projects_for()) == 1
    assert "adopt_project" in world.actions()
    assert len(world.voice.calls) == 1


def test_tasks_still_copying_from_the_template_are_waited_for(world: World) -> None:
    # Seen live on 28 Oct: the phases were there and the template's tasks not yet.
    world.rocketlane.tasks_hidden_reads = 2
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    verified = [r for r in world.store.trail("D1") if r["action"] == "verify_project"]
    assert [r["decision"] for r in verified] == ["verified"]


def test_tasks_that_never_appear_go_to_a_person(world: World) -> None:
    world.rocketlane.tasks_hidden_reads = 10
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.HUMAN_REVIEW
    assert world.deal().review_reason is ReviewReason.TEMPLATE_MISMATCH


def test_rocketlane_down_after_the_call_waits_and_resumes_without_calling_again(
    world: World,
) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    real_create = world.rocketlane.create_project

    def down(spec):  # type: ignore[no-untyped-def]
        raise TransientError("rocketlane", "503 Service Unavailable")

    world.rocketlane.create_project = down  # type: ignore[method-assign]
    world.poll()
    assert world.deal().state is DealState.TIER_CONFIRMED
    for _ in range(13):  # just over an hour of polls, 5 minutes apart
        world.clock.advance(minutes=5)
        world.poll()
    notices = world.slack.posts_to("cs-onboarding-escalations")
    assert len(notices) == 1 and "will resume on its own" in notices[0]

    world.rocketlane.create_project = real_create  # type: ignore[method-assign]
    world.clock.advance(minutes=5)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.voice.calls) == 1 and len(world.projects_for()) == 1


def test_rocketlane_down_for_a_day_goes_to_a_person(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()  # the project gets created in this poll; use a second deal for the outage
    world.rocketlane.down = True
    world.voice.script = [confirms("growth")]
    other = deal_email(customer="Globex", opportunity=OPP_LINK.replace(OPP_18, "006Ka00000ZzZzZ"))
    world.send(other, subject="Closed won: Globex")
    for _ in range(30):
        world.clock.advance(hours=1)
        world.poll()
    deal = world.deal("D2")
    assert deal.state is DealState.HUMAN_REVIEW
    assert deal.review_reason is ReviewReason.OUTAGE
    assert len(world.voice.calls) == 1  # D1's only: D2 was never called without its check


def test_rocketlane_down_before_the_call_means_no_call(world: World) -> None:
    """The duplicate check is a Rocketlane read; without it, nobody is called."""
    world.rocketlane.down = True
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    world.clock.advance(minutes=10)
    world.poll()
    assert world.deal().state is DealState.RECEIVED
    assert world.voice.calls == []
    world.rocketlane.down = False
    world.clock.advance(minutes=10)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED


def test_a_rejected_request_is_not_retried(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()

    def bad_request(spec):  # type: ignore[no-untyped-def]
        raise PermanentError("rocketlane", "400 owner not found")

    world.rocketlane.create_project = bad_request  # type: ignore[method-assign]
    world.poll()
    deal = world.deal()
    assert deal.review_reason is ReviewReason.REJECTED_BY_ROCKETLANE
    assert deal.resume_state is DealState.TIER_CONFIRMED


# ---- an existing onboarding project ---------------------------------------------------------


def test_an_existing_project_for_the_opportunity_is_a_duplicate_and_nobody_is_called(
    world: World,
) -> None:
    world.rocketlane.add_existing("Acme Corp onboarding", ENTERPRISE_TEMPLATE, ref=OPP_18)
    world.send()
    world.poll()
    assert world.deal().state is DealState.DUPLICATE
    assert world.voice.calls == [] and world.rocketlane.created == []
    (reply,) = world.inbox.replies
    assert "already has an onboarding project" in reply["body"]


def test_a_project_made_by_hand_for_the_customer_goes_to_a_person_before_any_call(
    world: World,
) -> None:
    world.rocketlane.add_existing("Acme Corp - onboarding (manual)", ENTERPRISE_TEMPLATE)
    world.send()
    world.poll()
    deal = world.deal()
    assert deal.review_reason is ReviewReason.POSSIBLE_DUPLICATE
    assert world.voice.calls == []

    # Priya checks: it is a different, older engagement. She lets the deal continue.
    world.voice.script = [confirms("enterprise")]
    world.orchestrator.retry("D1", by="priya", not_duplicate=True)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.rocketlane.created) == 1


def test_a_possible_duplicate_can_be_closed(world: World) -> None:
    world.rocketlane.add_existing("Acme Corp onboarding", GROWTH_TEMPLATE)
    world.send()
    world.poll()
    world.orchestrator.close("D1", by="priya", why="same onboarding, made by hand")
    assert world.deal().state is DealState.CLOSED


def test_a_project_made_by_hand_during_the_calls_is_not_adopted(world: World) -> None:
    from novacrm.fakes import unanswered

    world.voice.script = [unanswered(), confirms("enterprise")]
    world.send()
    world.poll()
    world.rocketlane.add_existing("Acme Corp (CSM made this)", ENTERPRISE_TEMPLATE)
    world.poll_through_calls()
    deal = world.deal()
    assert deal.review_reason is ReviewReason.POSSIBLE_DUPLICATE
    assert world.rocketlane.created == []


def test_the_same_opportunity_in_its_15_character_form_is_recognised(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    resend = deal_email(opportunity=OPP_LINK.replace(OPP_18, OPP_15))
    world.send(resend, subject="Fwd: Closed won: Acme Corp")
    world.poll()
    assert world.deal("D2").state is DealState.DUPLICATE
    assert len(world.voice.calls) == 1 and len(world.rocketlane.created) == 1


def test_a_resend_while_the_call_is_pending_starts_nothing_new(world: World) -> None:
    from novacrm.fakes import unanswered

    world.voice.script = [unanswered(), confirms("enterprise")]
    world.send()
    world.poll()
    world.send(subject="Re-sending: Closed won: Acme Corp")
    world.poll_through_calls()
    assert world.deal("D2").state is DealState.CLOSED
    assert world.deal("D2").merged_into == "D1"
    assert len(world.voice.calls) == 2  # D1's no-answer and its retry; none for D2
    assert world.deal().state is DealState.CHANNEL_CREATED


def test_an_incomplete_email_then_a_complete_one_in_a_new_thread_proceeds_once(
    world: World,
) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send(deal_email(opportunity=None))
    world.poll()
    assert world.deal("D1").state is DealState.NEEDS_INFO
    world.send(subject="Closed won: Acme Corp (with link)")
    world.poll()
    assert world.deal("D2").state is DealState.CHANNEL_CREATED
    assert world.deal("D1").state is DealState.CLOSED
    assert world.deal("D1").merged_into == "D2"
    assert len(world.voice.calls) == 1
    # A late reply on the first thread goes to the live deal and changes nothing.
    world.send("Opportunity: " + OPP_LINK, thread_id=world.deal("D1").thread_id)
    world.poll()
    assert world.deal("D2").state is DealState.CHANNEL_CREATED
    assert len(world.voice.calls) == 1


@pytest.mark.parametrize("days", [28, 31, 32])
def test_weekend_drift_in_the_planned_duration_is_accepted(world: World, days: int) -> None:
    # Live, 29 Oct: the Enterprise template planned 29 days from one start date, 31 from the next.
    world.rocketlane.override_duration = days
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
