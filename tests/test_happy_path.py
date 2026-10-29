"""Brief, Part 4: the happy path, end to end, for both tiers."""

from __future__ import annotations

from conftest import AE, DEDICATED_CSMS, ENTERPRISE_TEMPLATE, OPP_18, POOLED_CSM, World

from novacrm.domain import SIGNOFF_TASK, DealState, Tier
from novacrm.fakes import confirms


def test_enterprise_deal_runs_from_email_to_slack_channel(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()

    deal = world.deal()
    assert deal.state is DealState.CHANNEL_CREATED
    assert deal.tier is Tier.ENTERPRISE and deal.tier_decided_by == "ae-call"

    # Parsed from the email, with the AE taken from the sender and the id normalised.
    n = deal.notification
    assert n is not None
    assert (n.customer_name, n.contact_email, n.contact_name) == (
        "Acme Corp",
        "john.doe@acme.com",
        "John Doe",
    )
    assert (n.ae_email, n.opportunity_id) == (AE, OPP_18)

    # One call, to the AE.
    assert [phone for phone, _ in world.voice.calls] == ["+919800000001"]

    # The project: the Enterprise template, a dedicated CSM, the customer owns the sign-off.
    (spec,) = world.rocketlane.created
    assert spec.template_id == ENTERPRISE_TEMPLATE
    assert spec.external_reference_id == OPP_18
    assert spec.csm_email in DEDICATED_CSMS
    assert spec.owner_email == "priya@novacrm.example"
    (project,) = world.projects_for()
    signoff = next(t for t in world.rocketlane.tasks.values() if t["name"] == SIGNOFF_TASK)
    assert signoff["project"] == project["id"] and signoff["placeholders"] == {12}
    (kickoff,) = world.rocketlane.tasks_created
    assert kickoff.name == "Kickoff call with John Doe"
    assert kickoff.assignee_email == spec.csm_email
    assert "pipeline reports" in kickoff.description

    # The channel: named for the customer, personalised for Enterprise.
    (channel,) = world.slack.channels.values()
    assert channel["name"] == "onb-acme-corp"
    assert channel["topic"].startswith("Enterprise onboarding · 30 days")
    assert "Dedicated CSM: Asha Rao" in channel["topic"] or "Vikram Iyer" in channel["topic"]
    assert set(channel["members"]) == {spec.csm_email, AE}
    (welcome,) = world.slack.posts_to(world.deal().channel_id or "")
    assert "your dedicated Customer Success Manager for the next 30 days" in welcome
    assert "SSO" in welcome
    assert "pipeline reports" in welcome.splitlines()[0]  # the LLM's opening line
    assert world.slack.markers == [(world.deal().channel_id, OPP_18)]
    assert channel["purpose"] == f"NovaCRM onboarding for Salesforce opportunity {OPP_18}"

    # Every step is in the audit log, with the handoff between the two agents.
    assert world.actions() == [
        "received",
        "extract",
        "duplicate_check",
        "state",
        "place_call",
        "tier_decision",
        "state",
        "create_project",
        "create_task",
        "verify_project",
        "state",
        "handoff",
        "create_channel",
        "post_welcome",
        "channel_ready",
        "state",
    ]
    rows = world.store.trail("D1")
    assert all(r["ts"] and r["agent"] for r in rows)
    handoff = next(r for r in rows if r["action"] == "handoff")
    assert handoff["agent"] == "intake" and handoff["decision"] == "to the communication agent"
    assert next(r for r in rows if r["action"] == "channel_ready")["agent"] == "comms"


def test_growth_deal_gets_the_growth_template_and_the_pooled_csm(world: World) -> None:
    world.voice.script = [confirms("growth")]
    world.send()
    world.poll()

    assert world.deal().state is DealState.CHANNEL_CREATED
    (spec,) = world.rocketlane.created
    assert spec.csm_email == POOLED_CSM
    (channel,) = world.slack.channels.values()
    assert channel["topic"].startswith("Growth onboarding · 14 days")
    assert "CS team: Growth CS Team" in channel["topic"]
    (welcome,) = world.slack.posts_to(world.deal().channel_id or "")
    assert "live in 14 days" in welcome
    assert "SSO" not in welcome


def test_the_call_can_take_several_polls_to_finish(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.voice.in_progress_polls = 2
    world.send()
    world.poll()
    assert world.deal().state is DealState.AWAITING_TIER  # ringing / talking
    world.poll(2)
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.voice.calls) == 1
