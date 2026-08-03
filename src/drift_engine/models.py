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

CORRECTION (2026-08-03) -- ``EventType`` does NOT have separate
contacts.*/teachers.* members, even though the project brief (§3) assumed it
would. Contacts (guardians), students, teachers, staff, and district admins
are all ``users`` on Clever's wire protocol in API v3.x -- the role lives in
the object's own ``roles`` node, not in the event name. See ``EventType`` and
``EventSubject`` below for the full explanation and source docs. This is
flagged here, at the top of the module, because it will look like a mistake
to a future reader who only skims the enum -- it is a deliberate correction
of a wrong assumption inherited from the brief, not an oversight.
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

    IMPORTANT -- corrected 2026-08-03: this enum previously also had
    ``CONTACTS_CREATED``/``CONTACTS_UPDATED``/``CONTACTS_DELETED`` and
    ``TEACHERS_CREATED``. Those event types DO NOT EXIST on Clever's Events
    API. The project brief (§3) asserted that contacts had their own distinct
    event lifecycle -- that premise was simply wrong. In API v3.x, contacts
    (guardians), students, teachers, staff, and district admins are ALL
    ``users`` on the wire: the only object-level events are
    ``users.created`` / ``users.updated`` / ``users.deleted``, with the role
    (student, teacher, contact, staff) carried in the object's own ``roles``
    node, not in the event name. Clever's own event-ordering docs disambiguate
    by role in prose, e.g. "users.created (Students)", "users.updated
    (Contacts)", "users.deleted (Teachers)" -- see ``EventSubject`` and
    ``Change.expected_event_label`` below, which reproduce that exact
    phrasing so David's reports stay readable without inventing event types
    that don't exist. Section-membership changes are a separate object
    (``sections.*``), confirmed unaffected by this correction.

    If this looks like a regression to a future reader: it isn't. It is a
    correction of a factual error inherited from the project brief. Verified
    against Clever's live dev docs on 2026-08-03:
      * https://dev.clever.com/docs/events-api
      * https://dev.clever.com/docs/contacts-guardians
    """

    USERS_CREATED = "users.created"
    USERS_UPDATED = "users.updated"
    USERS_DELETED = "users.deleted"
    SECTIONS_CREATED = "sections.created"
    SECTIONS_UPDATED = "sections.updated"
    SECTIONS_DELETED = "sections.deleted"


class EventSubject(str, Enum):
    """Which role/object a ``users.*``/``sections.*`` event is really about.

    Clever's wire event name alone (``users.updated``) collapses students,
    teachers, contacts, and staff into one event type -- accurate to the API,
    but useless for a human report ("62 users.updated events" tells David
    nothing about what actually happened). This field restores that
    distinction for reporting purposes only; it is never part of the event
    name Clever itself emits. Values are Clever's own plural-noun labels,
    verbatim from their event-ordering documentation, so
    ``Change.expected_event_label`` reproduces phrasing partners will
    recognise, e.g. ``users.updated (Contacts)``.
    """

    STUDENT = "Students"
    TEACHER = "Teachers"
    CONTACT = "Contacts"
    STAFF = "Staff"
    SECTION = "Sections"


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
    #: Which role/object this event is really about (see ``EventSubject``).
    #: Required -- every call site must be deliberate about who a
    #: ``users.*``/``sections.*`` event actually concerns, since the wire
    #: event name alone cannot say.
    event_subject: EventSubject
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

    @property
    def expected_event_label(self) -> str:
        """Human-readable event name, disambiguated by role/object.

        Reproduces Clever's own event-ordering documentation phrasing, e.g.
        ``"users.updated (Contacts)"`` -- the wire event name Clever actually
        emits (``expected_event.value``) plus the subject that makes it
        legible to a human reader. Use this everywhere a report displays an
        event name to a person; use ``expected_event.value`` alone only when
        the exact wire value is what matters (e.g. cross-referencing the
        partner's real Events API feed).
        """

        return f"{self.expected_event.value} ({self.event_subject.value})"


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
        """Expected-event counts keyed by human-readable LABEL, e.g.
        ``"users.updated (Contacts)"`` -- see ``Change.expected_event_label``.

        This is what David reads: it restores the student/teacher/contact/
        staff distinction that the bare wire event name alone collapses. For
        the bare wire-name totals actually visible on the partner's
        ``/events`` feed, see :meth:`wire_event_counts`.
        """
        counts: dict[str, int] = {}
        for c in self.changes:
            label = c.expected_event_label
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    def wire_event_counts(self) -> dict[str, int]:
        """Expected-event counts keyed by the BARE wire event name Clever
        actually emits, e.g. ``"users.updated"`` -- no role/subject
        breakdown. This is what the partner's real Events API feed will show;
        cross-reference against this, not :meth:`event_counts`, when
        comparing to what Clever actually delivered.
        """
        counts: dict[str, int] = {}
        for c in self.changes:
            value = c.expected_event.value
            counts[value] = counts.get(value, 0) + 1
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
