"""In-memory fakes of every external system (design Appendix).

They behave like the real services where the agents can tell the difference: Rocketlane builds
the phases and tasks from the template and computes the planned duration, does not enforce unique
`externalReferenceId`s, and can be made to fail (including after a write has landed); Slack refuses
a taken channel name; the voice fake plays back scripted calls.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .domain import PHASES, SIGNOFF_TASK, CallResult, CallStatus, InboundEmail, Turn
from .ports import (
    ChannelInfo,
    NameTaken,
    PermanentError,
    PhaseInfo,
    ProjectGone,
    ProjectInfo,
    ProjectSpec,
    TaskInfo,
    TaskSpec,
    TransientError,
)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 28, 10, 0, tzinfo=UTC)  # a Wednesday

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class FakeInbox:
    def __init__(self, own_address: str = "cs@novacrm.example") -> None:
        self.own_address = own_address
        self.messages: list[InboundEmail] = []
        self.processed: set[str] = set()
        self.replies: list[dict[str, str]] = []
        self._ids = itertools.count(1)

    def deliver(
        self,
        sender: str,
        subject: str,
        body: str,
        *,
        received_at: datetime,
        thread_id: str | None = None,
    ) -> InboundEmail:
        n = next(self._ids)
        thread_id = thread_id or f"thread-{n}"
        earlier = [m.thread_text for m in self.messages if m.thread_id == thread_id][-1:]
        thread_text = "\n\n---\n\n".join([*earlier, f"Subject: {subject}\n\n{body}"])
        email = InboundEmail(
            f"msg-{n}", thread_id, sender.lower(), subject, body, received_at, thread_text
        )
        self.messages.append(email)
        return email

    def fetch_new(self) -> list[InboundEmail]:
        return [
            m
            for m in self.messages
            if m.message_id not in self.processed and m.sender != self.own_address
        ]

    def mark_processed(self, message_id: str) -> None:
        self.processed.add(message_id)

    def reply(self, thread_id: str, to: str, subject: str, body: str) -> None:
        self.replies.append({"thread_id": thread_id, "to": to, "subject": subject, "body": body})


def answered(*lines: tuple[str, str]) -> CallResult:
    """A connected call from (speaker, text) pairs; speaker is "ae" or "assistant"."""
    turns = tuple(Turn("ae" if s == "ae" else "assistant", t) for s, t in lines)
    return CallResult("", CallStatus.ANSWERED, turns)


def unanswered(status: CallStatus = CallStatus.NO_ANSWER) -> CallResult:
    return CallResult("", status, (), detail=f"fake {status.value}")


def confirms(tier: str) -> CallResult:
    """The happy call: the AE names the tier and confirms the read-back."""
    word = tier.capitalize()
    return answered(
        (
            "assistant",
            "Hi, this is NovaCRM's automated onboarding assistant. Is Acme on "
            "the Enterprise plan or the Growth plan?",
        ),
        ("ae", f"It's {word}."),
        ("assistant", f"Just to confirm: {word}. Is that right?"),
        ("ae", "Yes, that's right."),
    )


@dataclass
class FakeVoice:
    """Plays one scripted result per call, in order. A script entry may be an exception."""

    script: list[CallResult | Exception] = field(default_factory=list)
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    results: dict[str, CallResult] = field(default_factory=dict)
    in_progress_polls: int = 0  # how many polls report "still in progress" before the result
    _waits: int = 0

    def check(self) -> None:
        return None

    def place_call(self, phone: str, variables: dict[str, str]) -> str:
        if not self.script:
            raise AssertionError("FakeVoice: no scripted call left")
        entry = self.script.pop(0)
        self.calls.append((phone, variables))
        if isinstance(entry, Exception):
            raise entry
        call_id = f"call-{len(self.calls)}"
        self.results[call_id] = CallResult(call_id, entry.status, entry.turns, entry.detail)
        self._waits = self.in_progress_polls
        return call_id

    def get_result(self, call_id: str) -> CallResult | None:
        if self._waits > 0:
            self._waits -= 1
            return None
        return self.results[call_id]


@dataclass
class FakeTemplate:
    template_id: int
    name: str
    planned_duration_days: int
    tasks_per_phase: tuple[int, ...] = (4, 4, 3, 4)  # 15 tasks, task 8 is the sign-off


class FakeRocketlane:
    def __init__(self, templates: list[FakeTemplate], clock: FakeClock) -> None:
        self.templates = {t.template_id: t for t in templates}
        self.clock = clock
        self.projects: dict[int, dict[str, Any]] = {}
        self.phases: dict[int, dict[str, Any]] = {}
        self.tasks: dict[int, dict[str, Any]] = {}
        self.fail: list[Exception] = []  # raised in order by the next calls, then normal again
        self.down = False
        self.override_duration: int | None = None  # simulate a mis-built template
        self.due_date: date | None = None  # the end date Rocketlane reports, if set
        self.created: list[ProjectSpec] = []
        self.tasks_created: list[TaskSpec] = []
        self.phases_created: list[str] = []
        self.crash_after_create = False  # the write lands, then the response is lost
        self.tasks_hidden_reads = 0  # task lists read empty this many times (background copy)
        self._ids = itertools.count(1001)

    def _maybe_fail(self) -> None:
        if self.down:
            raise TransientError("rocketlane", "503 Service Unavailable")
        if self.fail:
            raise self.fail.pop(0)

    def _info(self, project: dict[str, Any]) -> ProjectInfo:
        template = self.templates.get(project["template_id"])
        return ProjectInfo(
            project_id=project["id"],
            name=project["name"],
            external_reference_id=project["ref"],
            template_ids=(project["template_id"],) if template else (),
            template_names=(template.name,) if template else (),
            planned_duration_days=project["duration"],
            member_emails=frozenset(project["members"]),
            url=f"https://novacrm.rocketlane.com/projects/{project['id']}",
            created_at=project["created_at"],
            due_date=self.due_date,
        )

    def _build(
        self,
        name: str,
        ref: str,
        template: FakeTemplate,
        members: set[str],
        customer_placeholder: int | None,
    ) -> int:
        pid = next(self._ids)
        self.projects[pid] = {
            "id": pid,
            "name": name,
            "ref": ref,
            "template_id": template.template_id,
            "duration": self.override_duration or template.planned_duration_days,
            "members": members,
            "created_at": self.clock.now(),
        }
        number = 0
        for phase_name, count in zip(PHASES, template.tasks_per_phase, strict=True):
            phid = next(self._ids)
            self.phases[phid] = {"project": pid, "name": phase_name}
            for _ in range(count):
                number += 1
                signoff = number == 8
                self.tasks[next(self._ids)] = {
                    "project": pid,
                    "phase": phid,
                    "name": SIGNOFF_TASK if signoff else f"{phase_name} task {number}",
                    "assignees": set(),
                    # As in Rocketlane: a template task is assigned to the placeholder, which the
                    # project maps to the customer contact.
                    "placeholders": {customer_placeholder}
                    if signoff and customer_placeholder
                    else set(),
                }
        return pid

    def add_existing(self, name: str, template_id: int, ref: str = "") -> int:
        """A project someone made by hand (usually with no reference)."""
        return self._build(name, ref, self.templates[template_id], set(), None)

    def find_by_reference(self, external_reference_id: str) -> ProjectInfo | None:
        self._maybe_fail()
        for project in self.projects.values():
            if project["ref"] == external_reference_id:
                return self._info(project)
        return None

    def find_by_name(self, text: str) -> list[ProjectInfo]:
        self._maybe_fail()
        return [self._info(p) for p in self.projects.values() if text.lower() in p["name"].lower()]

    def create_project(self, spec: ProjectSpec) -> ProjectInfo:
        self._maybe_fail()
        members = {spec.owner_email, spec.csm_email, spec.contact_email}
        if spec.template_id is None:  # api mode: an empty project with explicit dates
            assert spec.due_date is not None, "api mode needs a due date"
            pid = next(self._ids)
            self.projects[pid] = {
                "id": pid,
                "name": spec.name,
                "ref": spec.external_reference_id,
                "template_id": None,
                "duration": (spec.due_date - spec.start_date).days,
                "members": members,
                "created_at": self.clock.now(),
            }
        else:
            template = self.templates.get(spec.template_id)
            if template is None:
                raise PermanentError("rocketlane", f"400 template {spec.template_id} not found")
            pid = self._build(
                spec.name,
                spec.external_reference_id,
                template,
                members,
                spec.customer_placeholder_id,
            )
        self.created.append(spec)
        if self.crash_after_create:
            self.crash_after_create = False
            raise TransientError("rocketlane", "connection reset after the request was sent")
        return self._info(self.projects[pid])

    def get_project(self, project_id: int) -> ProjectInfo:
        self._maybe_fail()
        if project_id not in self.projects:
            raise ProjectGone("rocketlane", f"404 project {project_id} not found")
        return self._info(self.projects[project_id])

    def delete(self, project_id: int) -> None:
        """What a person does in the UI after a template mismatch."""
        del self.projects[project_id]

    def list_phases(self, project_id: int) -> list[PhaseInfo]:
        self._maybe_fail()
        return [
            PhaseInfo(i, p["name"]) for i, p in self.phases.items() if p["project"] == project_id
        ]

    def list_tasks(self, project_id: int) -> list[TaskInfo]:
        self._maybe_fail()
        if self.tasks_hidden_reads:
            self.tasks_hidden_reads -= 1
            return []
        return [
            TaskInfo(
                i,
                t["name"],
                t["phase"],
                frozenset(t["assignees"]),
                frozenset(t["placeholders"]),
                frozenset(t.get("dependencies", ())),
            )
            for i, t in self.tasks.items()
            if t["project"] == project_id
        ]

    def create_task(self, spec: TaskSpec) -> int:
        self._maybe_fail()
        tid = next(self._ids)
        self.tasks[tid] = {
            "project": spec.project_id,
            "phase": spec.phase_id,
            "name": spec.name,
            "assignees": {spec.assignee_email},
            "placeholders": set(),
        }
        self.tasks_created.append(spec)
        return tid

    def create_phase(self, project_id: int, name: str, start: date, due: date) -> int:
        self._maybe_fail()
        phid = next(self._ids)
        self.phases[phid] = {"project": project_id, "name": name, "start": start, "due": due}
        self.phases_created.append(name)
        return phid

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        self._maybe_fail()
        self.tasks[task_id].setdefault("dependencies", set()).add(depends_on_task_id)


class FakeSlack:
    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {}
        self.posts: list[tuple[str, str]] = []
        self.markers: list[tuple[str, str]] = []
        self.down = False
        self.crash_after_create = False
        self.crash_after_post = False
        self._ids = itertools.count(1)

    def _check(self) -> None:
        if self.down:
            raise TransientError("slack", "503 Service Unavailable")

    def _add(self, name: str, ours: bool, purpose: str) -> str:
        cid = f"C{next(self._ids):04d}"
        self.channels[cid] = {
            "name": name,
            "topic": "",
            "purpose": purpose,
            "members": [],
            "ours": ours,
        }
        return cid

    def create_channel(self, name: str, purpose: str) -> str:
        self._check()
        if any(c["name"] == name for c in self.channels.values()):
            raise NameTaken(name)
        cid = self._add(name, ours=True, purpose=purpose)
        if self.crash_after_create:
            self.crash_after_create = False
            raise TransientError("slack", "timeout after the channel was created")
        return cid

    def add_existing(self, name: str, *, ours: bool = False, purpose: str = "") -> str:
        """A channel someone else made, or one our bot made for an earlier run."""
        return self._add(name, ours=ours, purpose=purpose)

    def find_channel(self, name: str) -> ChannelInfo | None:
        self._check()
        for cid, c in self.channels.items():
            if c["name"] == name:
                return ChannelInfo(cid, c["ours"], c["purpose"])
        return None

    def set_purpose(self, channel_id: str, purpose: str) -> None:
        self._check()
        self.channels[channel_id]["purpose"] = purpose

    def set_topic(self, channel_id: str, topic: str) -> None:
        self._check()
        self.channels[channel_id]["topic"] = topic
        self.posts.append((channel_id, f"set the channel topic: {topic}"))  # as Slack does

    def invite(self, channel_id: str, emails: list[str]) -> None:
        self._check()
        members = self.channels[channel_id]["members"]
        members.extend(e for e in emails if e not in members)

    def post(self, channel: str, text: str, marker: str = "") -> str:
        self._check()
        self.posts.append((channel, text))
        if marker:
            self.markers.append((channel, marker))
        if self.crash_after_post:
            self.crash_after_post = False
            raise TransientError("slack", "timeout after the message was posted")
        return f"{len(self.posts)}.000100"

    def has_posted(self, channel_id: str, marker: str) -> bool:
        self._check()
        return (channel_id, marker) in self.markers

    def posts_to(self, channel: str) -> list[str]:
        return [t for c, t in self.posts if c == channel and not t.startswith("set the channel")]


class FakeLLM:
    """Answers by task name. A handler may return a value or raise to simulate an outage."""

    def __init__(self, handlers: dict[str, Callable[[str], Any]] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, task: str, system: str, user: str, schema: dict[str, Any]) -> Any:
        self.calls.append((task, user))
        return self.handlers[task](user)

    def complete_text(self, task: str, system: str, user: str) -> str:
        self.calls.append((task, user))
        handler = self.handlers.get(task)
        return str(handler(user)) if handler else ""
