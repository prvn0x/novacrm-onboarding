"""Edge cases on the live path: real Slack and Rocketlane behaviour, crashes, odd inputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import OPP_18, OPP_LINK, World, deal_email

from novacrm.adapters.rocketlane import RocketlaneProjects
from novacrm.adapters.slack import SlackWorkspace
from novacrm.agents.tier import plain_yes
from novacrm.domain import Deal, DealState, ReviewReason, Tier
from novacrm.fakes import answered, confirms, unanswered
from novacrm.ports import TransientError
from novacrm.store import Store, mask

# ---- Slack -----------------------------------------------------------------------------------


def _slack(handler):  # type: ignore[no-untyped-def]
    return SlackWorkspace("xoxb-test", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_slack_calls_are_form_encoded_and_the_welcome_carries_its_marker() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True, "ts": "1793181600.000100"})

    _slack(handler).post("C0001", "Welcome!", marker=OPP_18)
    (req,) = seen
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = parse_qs(req.content.decode())
    assert form["channel"] == ["C0001"] and form["mrkdwn"] == ["true"]
    assert json.loads(form["metadata"][0]) == {
        "event_type": "novacrm_welcome",
        "event_payload": {"marker": OPP_18},
    }


def test_slacks_own_join_and_topic_messages_are_not_mistaken_for_the_welcome() -> None:
    history = {
        "ok": True,
        "messages": [
            {"user": "UBOT", "subtype": "channel_join", "text": "joined"},
            {"user": "UBOT", "subtype": "channel_topic", "text": "set the topic"},
        ],
    }
    slack = _slack(lambda _: httpx.Response(200, json=history))
    assert slack.has_posted("C0001", OPP_18) is False
    history["messages"].append(
        {
            "user": "UBOT",
            "metadata": {"event_type": "novacrm_welcome", "event_payload": {"marker": OPP_18}},
        }
    )
    assert slack.has_posted("C0001", OPP_18) is True
    assert slack.has_posted("C0001", "006OTHER000000000A") is False


def test_a_channel_our_bot_made_for_another_opportunity_is_not_adopted(world: World) -> None:
    """A rehearsal left `onb-acme-corp` behind with a fresh database: make a new one."""
    world.slack.add_existing(
        "onb-acme-corp", ours=True, purpose="NovaCRM onboarding for Salesforce opportunity 006X"
    )
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    deal = world.deal()
    assert deal.state is DealState.CHANNEL_CREATED and deal.channel_name == "onb-acme-corp-2"
    assert len(world.slack.posts_to(deal.channel_id or "")) == 1


# ---- Rocketlane ------------------------------------------------------------------------------


def test_an_ignored_reference_filter_cannot_make_every_deal_a_duplicate() -> None:
    other = {"projectId": 1, "projectName": "Globex", "externalReferenceId": "006OTHER"}
    page = {"data": [other], "pagination": {"hasMore": False}}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)))
    api = RocketlaneProjects("k", client=client)
    assert api.find_by_reference(OPP_18) is None
    assert api.find_by_name("Acme") == []


def test_tasks_are_listed_with_their_phase_and_assignees_requested() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        task = {
            "taskId": 8,
            "taskName": "Customer data verification sign-off",
            "assignees": {"members": [], "placeholders": [{"placeholderId": 12}]},
        }
        return httpx.Response(200, json={"data": [task], "pagination": {"hasMore": False}})

    (task,) = RocketlaneProjects(
        "k", client=httpx.Client(transport=httpx.MockTransport(handler))
    ).list_tasks(5001)
    assert seen[0].url.params["includeFields"] == "phase,assignees,dependencies"
    assert task.placeholder_ids == {12}


def test_a_crash_after_creating_the_kickoff_task_does_not_create_it_twice(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    real = world.rocketlane.create_task

    def lost_response(spec):  # type: ignore[no-untyped-def]
        real(spec)
        raise TransientError("rocketlane", "read timeout")  # it exists; we never heard back

    world.rocketlane.create_task = lost_response  # type: ignore[method-assign]
    world.poll()
    assert world.deal().state is DealState.TIER_CONFIRMED  # waiting to retry
    world.rocketlane.create_task = real  # type: ignore[method-assign]
    world.clock.advance(minutes=6)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.rocketlane.tasks_created) == 1  # found by name, not created again


def test_an_unexpected_error_parks_only_that_deal(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()

    def bug(spec):  # type: ignore[no-untyped-def]
        raise KeyError("projectId")

    world.rocketlane.create_project = bug  # type: ignore[method-assign]
    world.poll()
    deal = world.deal()
    assert deal.review_reason is ReviewReason.INTERNAL_ERROR and "KeyError" in deal.review_detail


# ---- the inbox -------------------------------------------------------------------------------


def test_a_real_deal_sent_into_a_thread_judged_not_a_deal_is_processed(world: World) -> None:
    thread = world.send("Hi team, quick question about lunch.\n\nThanks,\nRahul", subject="Hi")
    world.poll()
    assert world.deal().state is DealState.REJECTED
    world.voice.script = [confirms("growth")]
    world.send(thread_id=thread)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED and len(world.voice.calls) == 1


def test_an_email_missing_the_customer_name_is_merged_by_its_opportunity_link(
    world: World,
) -> None:
    """The brief's own case: the first email lacks the customer name, a complete one follows."""
    world.voice.script = [confirms("enterprise")]
    world.send(deal_email(customer=None))
    world.poll()
    assert world.deal("D1").state is DealState.NEEDS_INFO
    assert world.deal("D1").partial_opportunity == OPP_18
    world.send(subject="Closed won: Acme Corp (complete)")
    world.poll()
    assert world.deal("D1").state is DealState.CLOSED and world.deal("D1").merged_into == "D2"
    assert world.deal("D2").state is DealState.CHANNEL_CREATED
    world.clock.advance(hours=30)
    world.poll()
    assert world.slack.posts_to("cs-onboarding-escalations") == []  # no false page for D1


def test_an_incomplete_deal_already_with_a_person_is_closed_by_a_complete_resend(
    world: World,
) -> None:
    world.send(deal_email(contact=None))
    world.poll()
    world.clock.advance(hours=25)
    world.poll()
    assert world.deal("D1").review_reason is ReviewReason.MISSING_FIELDS
    world.voice.script = [confirms("growth")]
    world.send(subject="Closed won: Acme Corp (with contact)")
    world.poll()
    assert world.deal("D1").state is DealState.CLOSED
    assert world.deal("D2").state is DealState.CHANNEL_CREATED


# ---- the rest ---------------------------------------------------------------------------------


def test_one_broken_deal_does_not_stop_the_others(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.send(
        deal_email(customer="Globex", opportunity=OPP_LINK.replace(OPP_18, "006Ka00000ZzZzZ")),
        subject="Closed won: Globex",
    )

    def broken(user: str) -> object:
        if "Globex" in user:
            raise KeyError("a bug")
        from conftest import careful_extraction

        return careful_extraction(user)

    world.llm.handlers["extract"] = broken
    world.poll()
    assert world.deal("D1").state is DealState.CHANNEL_CREATED
    assert world.deal("D2").state is DealState.RECEIVED  # LLM errors are retried, not fatal


def test_masking_keeps_slack_timestamps_and_epoch_milliseconds() -> None:
    assert mask("ts 1793181600.000100 at 1793181600000") == "ts 1793181600.000100 at 1793181600000"
    assert mask("call 9800000001") == "call ******0001"


# ---- odd inputs and partial outages -------------------------------------------------------------


def test_a_strangers_second_email_in_their_thread_changes_nothing(world: World) -> None:
    thread = world.send(sender="someone@gmail.com")
    world.poll()
    world.send(sender="someone@gmail.com", thread_id=thread)
    world.poll()
    assert world.deal().state is DealState.REJECTED
    assert world.slack.posts_to("cs-onboarding-escalations") == []


def test_a_gmail_outage_does_not_stop_the_poll_loop(world: World) -> None:
    world.voice.script = [unanswered(), confirms("enterprise")]
    world.send()
    world.poll()
    real_fetch = world.inbox.fetch_new

    def down() -> list[object]:
        raise TransientError("gmail", "503")

    world.inbox.fetch_new = down  # type: ignore[method-assign]
    world.clock.advance(minutes=16)
    world.poll()  # must not raise; the open deal still advances
    assert world.deal().state is DealState.CHANNEL_CREATED
    world.inbox.fetch_new = real_fetch  # type: ignore[method-assign]


def test_a_failed_clarification_reply_is_retried_not_escalated(world: World) -> None:
    real_reply = world.inbox.reply

    def down(*args: object) -> None:
        raise TransientError("gmail", "503")

    world.inbox.reply = down  # type: ignore[method-assign]
    world.send(deal_email(contact=None))
    world.poll()
    assert world.deal().state is DealState.RECEIVED
    world.inbox.reply = real_reply  # type: ignore[method-assign]
    world.clock.advance(minutes=6)
    world.poll()
    assert world.deal().state is DealState.NEEDS_INFO and len(world.inbox.replies) == 1


@pytest.mark.parametrize(
    "yes",
    [
        "Yes, that's right.",
        "Uh, yes.",
        "Okay, yes",
        "That's correct.",
        "Absolutely.",
        "Mm-hmm.",
        "Yes, no problem.",
    ],
)
def test_ordinary_ways_of_saying_yes_confirm(world: World, yes: str) -> None:
    """With a model that quotes the AE's 'yes' rather than the plan name, as a real one may."""
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": yes,
    }
    world.voice.script = [
        answered(
            ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
            ("ae", "Enterprise."),
            ("assistant", "Just to confirm: Enterprise. Is that right?"),
            ("ae", yes),
        )
    ]
    world.send()
    world.poll()
    assert world.deal().tier is Tier.ENTERPRISE


def test_a_not_available_voicemail_greeting_is_no_answer(world: World) -> None:
    greeting = answered(
        ("assistant", "Hi Rahul, is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "The person you are calling is not available. Please leave a message."),
    )
    world.voice.script = [greeting]
    world.send()
    world.poll()
    decision = next(r for r in world.store.trail("D1") if r["action"] == "tier_decision")
    assert decision["decision"] == "unreachable"


def test_a_call_that_finished_after_we_stopped_waiting_is_still_used(world: World) -> None:
    world.voice.script = [confirms("enterprise"), confirms("enterprise")]
    world.voice.in_progress_polls = 10_000
    world.send()
    world.poll()
    world.clock.advance(minutes=11)
    world.poll()  # gave up waiting
    assert world.deal().abandoned_call_id == "call-1"
    world.voice._waits = 0  # the platform finally reports the first call: the AE confirmed
    world.clock.advance(minutes=5)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.voice.calls) == 1  # not called a second time


@pytest.mark.parametrize(
    "reply",
    [
        "Sure, but let me check with finance first.",
        "Yes, I'd assume so.",
        "Yeah, should be.",
        "Right, hang on, let me look that up.",
        "Uh-huh, I can't recall exactly.",
        "Yes, no worries, unless they downgrade.",
        "Sure, I reckon.",
        "Yes it is, they also asked about a discount and more seats next quarter.",
    ],
)
def test_a_qualified_yes_never_confirms_even_with_a_model_that_always_agrees(
    world: World, reply: str
) -> None:
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": "Enterprise",
    }
    call = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise"),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", reply),
    )
    world.voice.script = [call, call, call]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.HUMAN_REVIEW
    assert world.rocketlane.created == []


def test_a_one_letter_quote_is_not_evidence(world: World) -> None:
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": "e",
    }
    world.voice.script = [confirms("enterprise"), confirms("enterprise")]
    world.send()
    world.poll_through_calls()
    assert world.deal().review_reason is ReviewReason.TIER_UNCLEAR


def test_the_last_call_timing_out_is_re_read_before_going_to_a_person(world: World) -> None:
    world.voice.script = [unanswered(), unanswered(), confirms("enterprise")]
    world.send()
    world.poll_through_calls(2)  # two unanswered calls
    world.voice.in_progress_polls = 10_000
    world.poll()  # the third call is placed and hangs
    world.clock.advance(minutes=11)
    world.poll()
    assert world.deal().state is DealState.AWAITING_TIER  # not escalated yet
    world.voice._waits = 0  # the third call's result finally arrives: confirmed
    world.clock.advance(minutes=5)
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert len(world.voice.calls) == 3


def test_an_adopted_channel_without_a_purpose_is_marked_for_its_opportunity(world: World) -> None:
    world.slack.add_existing("onb-acme-corp", ours=True)  # our create landed, the purpose did not
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()
    (channel,) = world.slack.channels.values()
    assert channel["purpose"].endswith(OPP_18)


SHORT_HEDGES = [
    "Yes, for now",
    "Yes, I hope",
    "Yeah, hopefully",
    "Yes, apparently",
    "Correct, supposedly",
    "Yes, kind of",
    "Yeah, kinda",
    "Sure, whatever",
    "Yes, at the moment",
    "Right, so far",
    "Yeah, I'd hope so",
    "Sure, fine",
    "Yes, ish",
    "Mm-hmm, roughly",
]


@pytest.mark.parametrize("reply", SHORT_HEDGES)
def test_a_short_hedge_is_not_a_plain_yes(world: World, reply: str) -> None:
    assert not plain_yes(reply)


@pytest.mark.parametrize(
    "later",
    [
        ["Hmm, actually I'm not sure."],
        ["Wait, no."],
        ["Actually let me check with finance."],
        ["No wait, hold on!"],
    ],
)
def test_taking_the_yes_back_in_a_later_turn_never_confirms(world: World, later: list[str]) -> None:
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": "Enterprise",
    }
    turns = [
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise"),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", "Yes."),
        ("assistant", "Thank you, the onboarding will be set up now. Goodbye."),
        *[("ae", text) for text in later],
    ]
    call = answered(*turns)
    world.voice.script = [call, call, call]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.HUMAN_REVIEW
    assert world.rocketlane.created == []


@pytest.mark.parametrize(
    "reply",
    [
        "Yes, that's right.",
        "Uh, yes.",
        "Okay, yes",
        "That's correct.",
        "Absolutely.",
        "Mm-hmm.",
        "Yes, no problem.",
        "Yes please",
        "Yes, go ahead",
        "That's right, yes, thanks a lot.",
        "Yep, the 30-day one.",
        "Perfect, yes.",
    ],
)
def test_ordinary_yeses_are_plain(reply: str) -> None:
    assert plain_yes(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Yes?",
        "Right?",
        "Sure?",
        "Uh-huh?",
        "Yes, is it?",
        "Yes... is that the plan?",
        "Yes, the one?",
        "Right, so...",
    ],
)
def test_a_question_or_an_unfinished_reply_is_not_a_yes(reply: str) -> None:
    assert not plain_yes(reply)


@pytest.mark.parametrize(
    "later",
    [
        "Sorry, I misheard you.",
        "Hmm, which one did you say?",
        "Oh, did you say Enterprise?",
        "Sorry, say that again?",
        "Er, one sec.",
        "Give me a second.",
        "Ugh, my mistake.",
    ],
)
def test_doubt_in_a_later_turn_in_any_words_never_confirms(world: World, later: str) -> None:
    world.llm.handlers["tier"] = lambda _: {
        "outcome": "enterprise",
        "readback_confirmed": True,
        "evidence_quote": "Enterprise",
    }
    call = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise"),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", "Yes."),
        ("ae", later),
    )
    world.voice.script = [call, call, call]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.HUMAN_REVIEW and world.rocketlane.created == []


@pytest.mark.parametrize(
    "reply",
    [
        "Yes, thirty days",
        "Yes, the thirty-day one",
        "Yes, 30 days",
        "Yes, thank you very much",
        "Yes, sounds good",
        "Yes, all good",
        "Yes, that works",
        "Yes, spot on",
        "Yes, dedicated CSM",
        "Yes sir",
        "Yes, cheers",
    ],
)
def test_ordinary_confirmations_with_a_few_extra_words_are_plain(reply: str) -> None:
    assert plain_yes(reply)


def test_a_goodbye_after_the_yes_still_confirms(world: World) -> None:
    call = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise."),
        ("assistant", "Just to confirm: Enterprise. Is that right?"),
        ("ae", "Yes, that's right."),
        ("assistant", "Thank you, the onboarding will be set up now. Goodbye."),
        ("ae", "Thanks, bye!"),
    )
    world.voice.script = [call]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED


@pytest.mark.parametrize(
    ("readback", "reply"),
    [
        ("Enterprise", "Yes, the 14-day one."),
        ("Enterprise", "Yes, fourteen days."),
        ("Enterprise", "Yes, pooled CS team."),
        ("Enterprise", "Yep, the fourteen-day one."),
        ("Growth", "Yes, the 30-day one."),
        ("Growth", "Yes, dedicated CSM."),
        ("Growth", "Yes, thirty days."),
    ],
)
def test_describing_the_other_plan_never_confirms_the_read_back(
    world: World, readback: str, reply: str
) -> None:
    world.llm.handlers["tier"] = lambda _: {
        "outcome": readback.lower(),
        "readback_confirmed": True,
        "evidence_quote": readback,
    }
    call = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", readback),
        ("assistant", f"Just to confirm: {readback}. Is that right?"),
        ("ae", reply),
    )
    world.voice.script = [call, call, call]
    world.send()
    world.poll_through_calls()
    assert world.deal().state is DealState.HUMAN_REVIEW and world.rocketlane.created == []


@pytest.mark.parametrize(
    "goodbye",
    [
        "Okay, thanks, you too, bye.",
        "Bye, have a good day.",
        "Alright, thanks.",
        "Great, take care.",
        "Talk soon, bye-bye.",
    ],
)
def test_an_ordinary_goodbye_after_the_yes_keeps_the_confirmation(
    world: World, goodbye: str
) -> None:
    call = answered(
        ("assistant", "Is Acme on the Enterprise plan or the Growth plan?"),
        ("ae", "Enterprise."),
        (
            "assistant",
            "Just to confirm: Enterprise, the 30-day onboarding with a dedicated CSM. "
            "Is that right?",
        ),
        ("ae", "Yes."),
        ("assistant", "Thank you, the onboarding will be set up now. Goodbye."),
        ("ae", goodbye),
    )
    world.voice.script = [call]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED


@pytest.mark.parametrize("reply", ["Alright, yes", "All right", "Yes indeed", "Yes, you got it"])
def test_more_ordinary_yeses(reply: str) -> None:
    assert plain_yes(reply)


def test_deal_keys_count_up_by_one_however_often_deals_are_saved(tmp_path: Path) -> None:
    # Seen live: keys D1, D2, D16, D32, because every save advanced the row sequence.
    store = Store(str(tmp_path / "keys.db"))
    now = datetime(2026, 10, 28, tzinfo=UTC)
    first = Deal(
        key=store.new_key(),
        thread_id="t1",
        state=DealState.RECEIVED,
        state_since=now,
        received_at=now,
        ae_email="ae@example.com",
        subject="s",
        thread_text="x",
    )
    for _ in range(5):
        store.save(first)
    assert store.new_key() == "D2"
