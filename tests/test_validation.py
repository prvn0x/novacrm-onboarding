"""Brief, Parts 3 and 4: incomplete or malformed emails are asked about, never guessed."""

from __future__ import annotations

import pytest
from conftest import AE, OPP_18, World, deal_email

from novacrm.domain import DealState, ReviewReason
from novacrm.fakes import confirms


def _asked(world: World) -> str:
    (reply,) = world.inbox.replies
    assert reply["to"] == AE and reply["thread_id"] == world.deal().thread_id
    return reply["body"]


@pytest.mark.parametrize(
    ("email", "asked_for"),
    [
        (deal_email(customer=None), "the customer's company name"),
        (deal_email(contact=None), "the customer contact's email address"),
        (deal_email(contact="John Doe <john.doe@acme>"), "is not a valid email address"),
        (deal_email(contact="John Doe"), "the customer contact's email address"),
        (deal_email(opportunity=None), "the Salesforce opportunity link"),
        (deal_email(opportunity="https://example.com/deal/42"), "not a Salesforce opportunity"),
        (deal_email(signed="Sara"), "the signature names someone else"),
    ],
    ids=[
        "no customer name",
        "no contact email",
        "malformed contact email",
        "contact without an email",
        "no opportunity link",
        "not a Salesforce link",
        "signed by a different AE",
    ],
)
def test_incomplete_email_asks_the_ae_and_creates_nothing(
    world: World, email: str, asked_for: str
) -> None:
    world.send(email)
    world.poll()

    deal = world.deal()
    assert deal.state is DealState.NEEDS_INFO
    assert asked_for in _asked(world)
    assert world.voice.calls == [] and world.rocketlane.created == [] and world.slack.posts == []
    ask = next(r for r in world.store.trail("D1") if r["action"] == "ask_ae")
    assert ask["decision"] == "ask, do not guess"


def test_a_value_the_model_invents_is_not_used(world: World) -> None:
    """Grounding: the model returns a contact email that is not in the email at all."""
    world.llm.handlers["extract"] = lambda user: {
        "is_deal_notification": True,
        "customer_name": "Acme Corp",
        "contact_email": "ceo@acme.com",  # invented
        "opportunity_link": f"https://novacrm.my.salesforce.com/{OPP_18}",  # invented
        "ae_name": "Rahul",
    }
    world.send(deal_email(contact=None, opportunity=None))
    world.poll()

    assert world.deal().state is DealState.NEEDS_INFO
    body = _asked(world)
    assert "contact_email: the value read is not in the email" in body
    assert "opportunity_link: the value read is not in the email" in body


def test_the_aes_reply_completes_the_deal(world: World) -> None:
    world.voice.script = [confirms("enterprise")]
    thread = world.send(deal_email(contact=None))
    world.poll()
    assert world.deal().state is DealState.NEEDS_INFO

    world.send("Sorry!\nContact: John Doe <john.doe@acme.com>", thread_id=thread)
    world.poll()

    deal = world.deal()
    assert deal.state is DealState.CHANNEL_CREATED
    assert deal.notification is not None and deal.notification.contact_email == "john.doe@acme.com"
    assert len(world.voice.calls) == 1


def test_a_third_incomplete_answer_goes_to_a_person(world: World) -> None:
    thread = world.send(deal_email(contact=None))
    world.poll()
    world.send("Will send it soon.", thread_id=thread)
    world.poll()
    assert len(world.inbox.replies) == 2  # at most two asks
    world.send("Still checking.", thread_id=thread)
    world.poll()

    deal = world.deal()
    assert deal.state is DealState.HUMAN_REVIEW
    assert deal.review_reason is ReviewReason.MISSING_FIELDS
    assert len(world.inbox.replies) == 2
    assert "needs a person" in world.slack.posts_to("cs-onboarding-escalations")[0]


def test_no_reply_in_24_hours_goes_to_a_person(world: World) -> None:
    world.send(deal_email(opportunity=None))
    world.poll()
    world.clock.advance(hours=25)
    world.poll()
    deal = world.deal()
    assert deal.state is DealState.HUMAN_REVIEW
    assert deal.review_reason is ReviewReason.MISSING_FIELDS


def test_an_unknown_sender_is_rejected_and_nothing_happens(world: World) -> None:
    world.send(sender="someone@gmail.com")
    world.poll()
    assert world.deal().state is DealState.REJECTED
    assert world.inbox.replies == [] and world.voice.calls == []
    assert world.actions() == ["screen_sender"]


def test_an_email_that_is_not_a_deal_is_rejected(world: World) -> None:
    world.send("Hi team, lunch is at 1pm on Friday.\n\nThanks,\nRahul", subject="Lunch")
    world.poll()
    assert world.deal().state is DealState.REJECTED
    assert world.inbox.replies == []


def test_the_inbox_is_read_by_label_so_an_opened_email_still_counts(world: World) -> None:
    """A person opening the email first (read state) must not hide it; only our label does."""
    world.voice.script = [confirms("growth")]
    world.send()
    world.poll()
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert world.inbox.processed == {"msg-1"}
    world.poll()  # nothing new, nothing repeated
    assert len(world.voice.calls) == 1


def test_a_reply_from_someone_else_in_the_thread_changes_nothing(world: World) -> None:
    thread = world.send(deal_email(contact=None))
    world.poll()
    world.send("Contact: Mallory <m@evil.example>", sender="mallory@evil.example", thread_id=thread)
    world.poll()
    deal = world.deal()
    assert deal.state is DealState.NEEDS_INFO
    assert "followup_ignored" in world.actions()


def test_a_field_the_model_skipped_once_is_read_again_before_asking(world: World) -> None:
    """Seen live: the model left out a contact that was in the email. A second read finds it."""
    real = world.llm.handlers["extract"]
    reads: list[str] = []

    def flaky(user: str) -> dict[str, object]:
        reads.append(user)
        result = dict(real(user))
        if len(reads) == 1:
            result["contact_email"] = None
        return result

    world.llm.handlers["extract"] = flaky
    world.voice.script = [confirms("enterprise")]
    world.send()
    world.poll()

    assert len(reads) == 2
    assert world.deal().state is DealState.CHANNEL_CREATED
    assert world.inbox.replies == []
