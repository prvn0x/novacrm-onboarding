"""The interfaces the agents depend on (Appendix A). Each has a real adapter and an in-memory fake.

Adapters raise `TransientError` for failures worth retrying (timeouts, 5xx, 429) and
`PermanentError` for ones that will not fix themselves (4xx, bad credentials). Agents never see a
vendor exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from .domain import CallResult, InboundEmail


class IntegrationError(Exception):
    def __init__(self, system: str, message: str) -> None:
        super().__init__(f"{system}: {message}")
        self.system = system


class TransientError(IntegrationError):
    """Worth retrying: the same request may succeed later."""


class PermanentError(IntegrationError):
    """Not worth retrying: a person has to look."""


class Inbox(Protocol):
    def fetch_new(self) -> list[InboundEmail]:
        """Inbox messages not yet labelled processed, excluding mail sent by the inbox itself."""
        ...

    def mark_processed(self, message_id: str) -> None: ...

    def reply(self, thread_id: str, to: str, subject: str, body: str) -> None:
        """Reply in the same thread (the adapter threads it under the thread's last message)."""
        ...


class VoiceCaller(Protocol):
    def check(self) -> None:
        """Read-only: the voice account answers (for `novacrm check`)."""
        ...

    def place_call(self, phone: str, variables: dict[str, str]) -> str:
        """Start an outbound call; returns the call id."""
        ...

    def get_result(self, call_id: str) -> CallResult | None:
        """The finished call, or None while it is still ringing or in progress."""
        ...


@dataclass(frozen=True, slots=True)
class ProjectSpec:
    name: str
    customer_name: str
    contact_email: str
    owner_email: str
    csm_email: str
    start_date: date
    external_reference_id: str
    # template mode: the template and its placeholders; api mode: None, with an explicit due date
    template_id: int | None = None
    pm_placeholder_id: int | None = None
    customer_placeholder_id: int | None = None
    due_date: date | None = None


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    project_id: int
    name: str
    external_reference_id: str
    template_ids: tuple[int, ...]
    template_names: tuple[str, ...]
    planned_duration_days: int | None
    member_emails: frozenset[str]
    url: str
    created_at: datetime | None = None
    due_date: date | None = None  # as Rocketlane scheduled it


@dataclass(frozen=True, slots=True)
class PhaseInfo:
    phase_id: int
    name: str


@dataclass(frozen=True, slots=True)
class TaskInfo:
    task_id: int
    name: str
    phase_id: int | None
    assignee_emails: frozenset[str]
    placeholder_ids: frozenset[int] = frozenset()
    dependency_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class TaskSpec:
    project_id: int
    phase_id: int
    name: str
    description: str
    assignee_email: str
    due_date: date
    start_date: date | None = None


class ProjectStore(Protocol):
    def find_by_reference(self, external_reference_id: str) -> ProjectInfo | None: ...

    def find_by_name(self, text: str) -> list[ProjectInfo]:
        """Projects whose name contains `text` (catches projects made by hand, D5)."""
        ...

    def create_project(self, spec: ProjectSpec) -> ProjectInfo: ...

    def get_project(self, project_id: int) -> ProjectInfo:
        """Raises `ProjectGone` if a person deleted it."""
        ...

    def list_phases(self, project_id: int) -> list[PhaseInfo]: ...

    def list_tasks(self, project_id: int) -> list[TaskInfo]: ...

    def create_task(self, spec: TaskSpec) -> int: ...

    def create_phase(self, project_id: int, name: str, start: date, due: date) -> int: ...

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        """`task_id` starts only after `depends_on_task_id` is complete."""
        ...


class ProjectGone(PermanentError):
    """The project id we saved no longer exists (deleted by a person)."""


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    channel_id: str
    created_by_us: bool
    purpose: str


class ChatWorkspace(Protocol):
    def create_channel(self, name: str, purpose: str) -> str:
        """Create a channel and set its purpose; returns its id. NameTaken if the name is used."""
        ...

    def set_topic(self, channel_id: str, topic: str) -> None: ...

    def set_purpose(self, channel_id: str, purpose: str) -> None: ...

    def find_channel(self, name: str) -> ChannelInfo | None: ...

    def invite(self, channel_id: str, emails: list[str]) -> None:
        """Invite workspace members by email; already-members are not an error."""
        ...

    def post(self, channel: str, text: str, marker: str = "") -> str:
        """Post to a channel id or name; returns the message timestamp. `marker` is attached as
        message metadata so the post can be recognised later."""
        ...

    def has_posted(self, channel_id: str, marker: str) -> bool:
        """Whether a message with this marker is already in the channel (D5)."""
        ...


class NameTaken(Exception):
    pass


class LLM(Protocol):
    def complete_json(self, task: str, system: str, user: str, schema: dict[str, Any]) -> Any:
        """One schema-bound completion. `task` names the call for logs and fakes."""
        ...

    def complete_text(self, task: str, system: str, user: str) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
