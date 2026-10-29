"""Brief, Parts 3 and 4: no project without a clear tier from the AE; retry or escalate."""

from __future__ import annotations

import pytest
from conftest import World, deal_email

from novacrm.domain import CallStatus, DealState, ReviewReason, Tier
from novacrm.fakes import answered, confirms, unanswered
from novacrm.orchestrator import ReviewError
from novacrm.ports import TransientError

AMBIGUOUS = answered(
    ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
    ("ae", "Enterprise... or actually maybe Growth, I'd have to look."),
    ("assistant", "Just to confirm: Enterprise. Is that right?"),
    ("ae", "Hmm, no, wait, I'm not certain it's that one."),
)
UNSURE = answered(
    ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
    ("ae", "Honestly I'm not sure, check with finance."),
)


def _review(world: World) -> tuple[ReviewReason | None, str]:
    deal = world.deal()
    assert deal.state is DealState.HUMAN_REVIEW
    return deal.review_reason, deal.review_detail


def _nothing_created(world: World) -> None:
    assert world.rocketlane.created == [] and world.slack.channels == {}


def test_no_answer_three_times_goes_to_a_person_after_exactly_three_calls(world: World) -> None:
    world.voice.script = [unanswered(), unanswered(CallStatus.BUSY), unanswered()]
    world.send()
    world.poll_through_calls()

    reason, detail = _review(world)
    assert reason is ReviewReason.AE_UNREACHABLE and "3 calls" in detail
    assert len(world.voice.calls) == 3
    _nothing_created(world)
    escalation = world.slack.posts_to("cs-onboarding-escalations")
    assert len(escalation) == 1 and "ae_unreachable" in escalation[0]


def test_calls_are_spaced_not_back_to_back(world: World) -> None:
    world.voice.script = [unanswered(), confirms("enterprise")]
    world.send()
    world.poll()
    world.clock.advance(minutes=5)
    world.poll()
    assert len(world.voice.calls) == 1  # 15 minutes have not passed
    world.clock.advance(minutes=11)
    world.poll()
    assert len(world.voice.calls) == 2
    assert world.deal().state is DealState.CHANNEL_CREATED


def test_voicemail_counts_as_no_answer_and_a_later_call_can_succeed(world: World) -> None:
    world.voice.script = [unanswered(CallStatus.VOICEMAIL), confirms("growth")]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert world.deal().tier is Tier.GROWTH


def test_a_voice_platform_error_is_a_failed_attempt(world: World) -> None:
    world.voice.script = [TransientError("vapi", "502 Bad Gateway"), confirms("enterprise")]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.CHANNEL_CREATED
    place = [r for r in world.store.trail("D1") if r["action"] == "place_call"]
    assert place[0]["decision"] == "failed attempt" and place[1]["decision"] == "calling"


def test_an_ambiguous_answer_gets_one_more_call_then_a_person(world: World) -> None:
    world.voice.script = [AMBIGUOUS, AMBIGUOUS]
    world.send()
    world.poll_through_calls()

    reason, _ = _review(world)
    assert reason is ReviewReason.TIER_UNCLEAR
    assert len(world.voice.calls) == 2
    _nothing_created(world)


def test_an_ambiguous_answer_then_a_clear_one_proceeds(world: World) -> None:
    world.voice.script = [AMBIGUOUS, confirms("enterprise")]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.CHANNEL_CREATED


def test_an_unsure_ae_goes_to_a_person_without_more_calls(world: World) -> None:
    world.voice.script = [UNSURE]
    world.send()
    world.poll_through_calls()

    reason, _ = _review(world)
    assert reason is ReviewReason.AE_UNSURE
    assert len(world.voice.calls) == 1
    _nothing_created(world)


def test_the_two_signals_must_agree(world: World) -> None:
    """The model says Enterprise, but the read-back was never answered with a yes."""
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": "Enterprise I think",
    }
    no_yes = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise I think"),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", "Let me get back to you."),
    )
    world.voice.script = [no_yes, no_yes]
    world.send()
    world.poll_through_calls()
    reason, _ = _review(world)
    assert reason is ReviewReason.TIER_UNCLEAR
    decision = next(r for r in world.store.trail("D1") if r["action"] == "tier_decision")
    assert "transcript check found no confirmed read-back" in decision["reason"]


def test_a_correction_after_the_read_back_is_not_a_confirmation(world: World) -> None:
    corrected = answered(
        ("assistant", "Is Acme on Enterprise or Growth?"),
        ("ae", "Enterprise."),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", "Yes. Oh sorry, Growth actually."),
    )
    world.voice.script = [corrected, corrected]
    world.send()
    world.poll_through_calls()
    assert _review(world)[0] is ReviewReason.TIER_UNCLEAR


def test_an_llm_outage_while_reading_the_call_is_never_a_tier(world: World) -> None:
    def down(_: str) -> object:
        raise TransientError("gemini", "503")

    world.llm.handlers["tier"] = down
    world.voice.script = [confirms("enterprise"), confirms("enterprise")]
    world.send()
    world.poll_through_calls()
    assert _review(world)[0] is ReviewReason.TIER_UNCLEAR
    _nothing_created(world)


def test_a_person_can_set_the_tier_after_review(world: World) -> None:
    world.voice.script = [UNSURE]
    world.send()
    world.poll()
    deal = world.orchestrator.resolve("D1", Tier.GROWTH, by="priya")
    assert deal.state is DealState.CHANNEL_CREATED
    assert deal.tier_decided_by == "priya"
    (spec,) = world.rocketlane.created
    assert spec.template_id == 202
    resolve = next(r for r in world.store.trail("D1") if r["action"] == "resolve")
    assert resolve["agent"] == "human" and "priya" in resolve["reason"]


def test_retry_after_unreachable_tries_the_ae_again(world: World) -> None:
    world.voice.script = [unanswered(), unanswered(), unanswered(), confirms("enterprise")]
    world.send()
    world.poll_through_calls()
    assert _review(world)[0] is ReviewReason.AE_UNREACHABLE
    world.orchestrator.retry("D1", by="priya")
    world.poll_through_calls()
    assert world.deal().state is DealState.CHANNEL_CREATED


@pytest.mark.parametrize("command", ["resolve", "retry --not-duplicate"])
def test_a_person_cannot_use_the_wrong_exit(world: World, command: str) -> None:
    world.send(deal_email(contact=None))
    world.poll()
    world.clock.advance(hours=25)
    world.poll()
    assert _review(world)[0] is ReviewReason.MISSING_FIELDS
    with pytest.raises(ReviewError):
        if command == "resolve":
            world.orchestrator.resolve("D1", Tier.ENTERPRISE, by="priya")
        else:
            world.orchestrator.retry("D1", by="priya", not_duplicate=True)


def _always_agrees(tier: str):  # type: ignore[no-untyped-def]
    """A model that always says the AE confirmed `tier`, quoting the AE's first line."""

    def read(user: str) -> dict[str, object]:
        first_ae = next(line[4:] for line in user.splitlines() if line.startswith("AE: "))
        return {"outcome": tier, "readback_confirmed": True, "evidence_quote": first_ae}

    return read


@pytest.mark.parametrize(
    "call",
    [
        answered(
            ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
            ("ae", "Growth, I think."),
            ("assistant", "Just to confirm: Growth. Is that right?"),
            ("ae", "Yeah, I guess so, probably."),
        ),
        answered(
            ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
            ("ae", "Hmm, one second."),
            ("assistant", "Just to confirm: Growth. Is that right?"),
            ("ae", "Yep."),
        ),
        answered(
            ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
            ("ae", "Yeah."),
        ),
    ],
    ids=["hedged yes", "read-back the AE never said", "a bare yes"],
)
def test_a_model_that_always_agrees_still_cannot_confirm_a_tier_alone(
    world: World, call: object
) -> None:
    """Signal 2 stands on its own: even a model that rubber-stamps 'growth' is not enough."""
    world.llm.handlers["tier"] = _always_agrees("growth")
    world.voice.script = [call, call, call]  # type: ignore[list-item]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.HUMAN_REVIEW
    _nothing_created(world)


def test_a_voicemail_greeting_counts_as_no_answer_not_as_ambiguous(world: World) -> None:
    greeting = answered(
        ("assistant", "Hi Rahul, this is NovaCRM's onboarding assistant. Enterprise or Growth?"),
        ("ae", "Hi, you've reached Rahul Mehta. Please leave a message after the tone."),
    )
    world.voice.script = [greeting, greeting, greeting]
    world.send()
    world.poll_through_calls()
    reason, _ = _review(world)
    assert reason is ReviewReason.AE_UNREACHABLE
    assert len(world.voice.calls) == 3  # unreachable gets 3 calls; ambiguous would stop at 2


def test_a_call_that_never_reports_a_result_is_given_up_after_ten_minutes(world: World) -> None:
    world.voice.script = [confirms("enterprise"), confirms("enterprise")]
    world.voice.in_progress_polls = 10_000  # the platform never says "ended"
    world.send()
    world.poll()
    world.clock.advance(minutes=11)
    world.poll()
    attempt = [r for r in world.store.trail("D1") if r["action"] == "call_result"]
    assert attempt and attempt[-1]["decision"] == "failed attempt"
    assert world.deal().pending_call_id is None


def test_a_crash_between_placing_a_call_and_saving_its_id_never_calls_back_to_back(
    world: World,
) -> None:
    world.voice.script = [confirms("enterprise"), confirms("enterprise")]
    world.voice.in_progress_polls = 10_000
    world.send()
    world.poll()  # the first call is placed
    deal = world.deal()
    deal.pending_call_id = None  # the crash: the attempt was counted, the call id never saved
    world.store.save(deal)
    world.voice.in_progress_polls = 0
    world.poll()
    assert len(world.voice.calls) == 1  # still inside the 15-minute spacing
    world.clock.advance(minutes=16)
    world.poll()
    assert len(world.voice.calls) == 2
    assert world.deal().state is DealState.CHANNEL_CREATED
