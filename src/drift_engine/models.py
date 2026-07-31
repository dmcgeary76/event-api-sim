"""Shared data contract between the engine's modules.

Every module in this package communicates through these types. The dependency
direction is deliberately one-way:

    cadence  -> RunPlan
    selection -> [Change]        (needs CsvStack, produces Changes)
    content   -> str             (pure value generation, no Change awareness)
    guardrail -> validates [Change] against CsvStack
    csvstack  -> applies [Change]
    audit     -> serialises [Change]

Nothing downstream of ``selection`` knows how targets were picked, and nothing
except ``content`` knows values may come from an LLM. That isolation is a
requirement from the project brief (section 5): the content-generation step must
be swappable without touching scheduling or selection.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Bucket(str, Enum):
    """A cadence bucket from the project brief's fixed weekly schedule."""

    SMALL_DAILY = "small_daily"
    BIG_STUDENT = "big_student"
    BIG_TEACHER = "big_teacher"


class Operation(str, Enum):
    """What is happening to a CSV row."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class EventType(str, Enum):
    """The Clever Events API event a change is expected to produce.

    These are predictions, not observations -- the engine writes CSVs and Clever
    decides what to emit. Recording the expectation is what makes a run
    auditable against what the partner actually sees on the Events API.
    """

    CONTACTS_CREATED = "contacts.created"
    CONTACTS_UPDATED = "contacts.updated"
    CONTACTS_DELETED = "contacts.deleted"
    SECTIONS_UPDATED = "sections.updated"
    USERS_UPDATED = "users.updated"
    TEACHERS_CREATED = "teachers.created"


@dataclass(frozen=True)
class Change:
    """One atomic edit to one CSV row.

    ``before``/``after`` hold only the columns that differ, so the audit log
    stays readable. For CREATE, ``before`` is empty; for DELETE, ``after`` is.
    """

    filename: str
    operation: Operation
    #: Natural key of the affected row, e.g. {"Student id": "STU100000"}.
    key: Mapping[str, str]
    bucket: Bucket
    expected_event: EventType
    before: Mapping[str, str] = field(default_factory=dict)
    after: Mapping[str, str] = field(default_factory=dict)
    #: Human-readable one-liner for the run report.
    note: str = ""
    #: True when any value in ``after`` came from the AI content generator.
    ai_generated: bool = False

    def __post_init__(self) -> None:
        if self.operation is Operation.CREATE and self.before:
            raise ValueError("CREATE change must not carry a 'before' state")
        if self.operation is Operation.DELETE and self.after:
            raise ValueError("DELETE change must not carry an 'after' state")
        if self.operation is Operation.UPDATE and not self.after:
            raise ValueError("UPDATE change must carry at least one 'after' value")

    @property
    def key_str(self) -> str:
        return "/".join(f"{k}={v}" for k, v in sorted(self.key.items()))


@dataclass(frozen=True)
class RunPlan:
    """Which buckets apply on a given date, decided purely by day of week."""

    run_date: _dt.date
    buckets: tuple[Bucket, ...]
    #: Set when the date is a weekend and nothing should run.
    skipped: bool = False
    reason: str = ""

    @property
    def weekday_name(self) -> str:
        return self.run_date.strftime("%A")


@dataclass
class RunResult:
    """Outcome of a single engine run, handed to the audit logger."""

    run_id: str
    plan: RunPlan
    district: str
    dry_run: bool
    changes: list[Change] = field(default_factory=list)
    #: Guardrail verdicts keyed by record type.
    guardrail: dict[str, Any] = field(default_factory=dict)
    pushed_files: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.changes:
            counts[c.expected_event.value] = counts.get(c.expected_event.value, 0) + 1
        return dict(sorted(counts.items()))

    def changes_for(self, filename: str) -> list[Change]:
        return [c for c in self.changes if c.filename == filename]


class GuardrailViolation(RuntimeError):
    """Raised when a planned run would breach Clever's deletion threshold."""


class SafetyViolation(RuntimeError):
    """Raised when the engine is pointed at a non-allowlisted target.

    This is the hard sandbox-only constraint from the project brief. It is never
    caught and downgraded to a warning anywhere in this codebase.
    """
