"""One-time guardian-contact seeding onto students.csv.

David's sandbox export carries no guardian data: every student has one
students.csv row with the seven engine-added contact columns blank. The
contact lifecycle this module seeds -- a guardian created, later edited,
later removed -- depends on students having *some* baseline set of contacts
to edit or remove; the small-daily and big-student buckets in
``selection.py`` already assume that, and simply produce zero contact-related
changes for a student with none.

Contacts are ROWS, not a file (corrected 2026-08-05; see the ``schema``
module docstring and SFTP Instructions v2.1.1). That makes seeding two
different operations rather than one:

* A student's **first** guardian fills their existing contact-less row in
  place -- an ``Operation.UPDATE``. Creating a row instead would leave the
  blank one behind and duplicate the student.
* Each **subsequent** guardian is a new row for the same Student id -- an
  ``Operation.CREATE`` repeating that student's student-level columns
  verbatim.

Both predict ``users.created`` (Contacts): a guardian object that did not
exist now does, whatever the CSV-level operation was.

NOTE: the project brief (§3) called these ``contacts.created`` /
``contacts.updated`` / ``contacts.deleted`` as if they were their own event
types. They are not -- on Clever's real Events API (v3.x), a contact is a
``users`` object, so every contact created here is expected to surface as
``users.created`` with a Contacts role, not a distinct ``contacts.created``
event. See ``models.EventType`` for the full correction and source docs.
This docstring's language below ("contacts.created events") describes the
*volume characteristics* of a seeding burst, which are unaffected by this
correction -- only the wire event name is.

David's decision, verbatim: "Seed it, then drift it" -- generate a baseline
set of guardian contacts once, push that baseline, and let the normal weekly
cadence drift it from there.

--------------------------------------------------------------------------
CRITICAL: DO NOT SEED THE WHOLE DISTRICT IN ONE RUN.
--------------------------------------------------------------------------
The real sandbox stack has 33,620 students. At ~1.5 guardians/student (the
weighted 1-2 split this module uses), a single unbounded seeding pass creates
roughly **50,000 ``users.created`` (Contacts) events in one sync**. That is an
enormous, unrepresentative first-sync burst for an application partner to
receive -- nothing like the small, steady drift cadence (brief §4) this whole
engine exists to produce, and likely to look like an outage or a bulk import
gone wrong from the partner's side of the Events API.

Use the ``limit`` parameter to stage seeding across multiple runs instead --
e.g. a few thousand students per day over the course of a week or two, run
alongside (or slightly ahead of) the normal weekday drift cadence, rather
than one 50k-row burst. Call ``estimate_seed_volume`` first to see the exact
numbers for the stack you're about to seed, and size ``limit`` accordingly.
Concretely, for the full 33,620-student stack: seeding ~3,000-5,000 students
per weekday would take roughly a week to finish and keeps each individual
sync's ``contacts.created`` volume in the low thousands rather than tens of
thousands.

Note on the 10% deletion guardrail (brief §3, enforced in ``guardrail.py``):
once seeded, a district with ~50,000 contacts has a deletion ceiling of
~5,000 contacts per sync (10% of 50,000) before Clever pauses the sync for
review. The steady-state drift cadence only removes
``cadence.BIG_STUDENT_CONTACTS_REMOVED`` (2) contacts per Tue/Thu run --
enormously below that ceiling. The ceiling matters for staged seeding too:
even mid-seeding, the guardrail is computed against the *current* contact
count, so it only gets more permissive as seeding progresses, never less.

Note on the scale-sanity gate (``safety.assert_scale_sane``, 25% tolerance):
seeding takes students.csv from 33,621 rows to ~52,931 -- a +57% move that
would trip that gate and, because a stale baseline is a hard SafetyViolation
rather than a silent re-anchor, brick the district mid-seed. It doesn't,
because ``CsvStack.counts`` reports ``students`` as distinct Student id
(flat at 33,621 throughout) and ``contacts`` as its own derived count. If
you ever change how students are counted, re-read that method's docstring
first.
"""

from __future__ import annotations

import random

from . import schema
from .csvstack import CsvStack
from .models import Bucket, Change, EventSubject, EventType, Operation

__all__ = ["seed_contacts", "estimate_seed_volume"]


#: Grades weighted toward getting 2 guardians rather than 1. Real guardian
#: data skews this way for younger students (both parents/guardians still
#: routinely listed), and varying it by grade also just makes the seeded
#: data look less mechanically uniform than "everyone gets exactly N."
_YOUNGER_GRADES: frozenset[str] = frozenset(
    {"PreKindergarten", "1", "2", "3", "4", "5"}
)

#: P(2 guardians) for younger vs older grades. Both are drawn from the same
#: ``(1, 2)`` support -- only the weighting shifts.
_YOUNGER_TWO_GUARDIAN_PROB = 0.7
_OLDER_TWO_GUARDIAN_PROB = 0.4

#: Relationship pools consistent with sequence position, so a single student
#: is never given two "Mother" rows (or two of anything). Sequence 1 draws
#: from the primary-guardian pool; sequence 2 draws from whatever's left in
#: the combined pool once sequence 1's choice is excluded. Splitting the
#: pools this way also means the *typical* two-guardian household reads as
#: "a mother and a father" (or similar) far more often than two relationships
#: that would never realistically co-occur, while still allowing e.g. two
#: grandparents.
_PRIMARY_RELATIONSHIPS: tuple[str, ...] = ("Mother", "Father", "Grandmother", "Grandfather", "Guardian")

#: Relationships that plausibly belong to a child's primary caregiver rather
#: than a backup emergency contact.
_PARENTAL_RELATIONSHIPS: frozenset[str] = frozenset(
    {"Mother", "Father", "Stepmother", "Stepfather", "Guardian"}
)


def _contact_type_for(sequence: int, relationship: str) -> str:
    """Pick a ``Contact type`` that is consistent with the relationship.

    Drawing ``Contact type`` at random from ``schema.CONTACT_TYPES`` produced
    rows like ``Relationship=Father, Sequence=1, Contact type=Emergency`` -- a
    child's father listed as the primary contact but typed as an emergency
    backup. Believability is the whole point of the generated content (brief
    §5), and a partner eyeballing the sandbox would notice that immediately, so
    the type is derived rather than rolled.
    """
    if relationship in _PARENTAL_RELATIONSHIPS:
        return "Parent" if relationship in {"Mother", "Father"} else "Guardian"
    # Non-parental relatives are plausible as either a secondary guardian or an
    # emergency contact; primaries lean guardian, secondaries lean emergency.
    return "Guardian" if sequence == 1 else "Emergency"
_SECONDARY_RELATIONSHIPS: tuple[str, ...] = schema.RELATIONSHIPS


class _ContentProto:  # pragma: no cover - structural typing only
    def guardian_name(self, student_last_name: str) -> str: ...
    def guardian_email(self, guardian_name: str, student_last_name: str) -> str: ...
    def phone(self) -> str: ...


def estimate_seed_volume(
    stack: CsvStack, guardians_per_student: tuple[int, int] = (1, 2)
) -> dict:
    """Report how many contacts a full (unbounded) seed pass would create.

    This does not run any seeding -- it is a cheap, read-only estimate meant
    to be checked *before* calling ``seed_contacts`` without a ``limit``, so
    the volume is a deliberate decision rather than a surprise in David's
    inbox the next morning.

    Returns a dict with:
      * ``students_without_contacts``: how many students currently have zero
        contacts and would be seeded.
      * ``estimated_contacts_low`` / ``estimated_contacts_high``: bounds
        assuming every eligible student gets the minimum / maximum of
        ``guardians_per_student``.
      * ``estimated_contacts_expected``: a rough point estimate using the
        same ~70%/40% younger/older weighting ``seed_contacts`` itself uses
        (approximated using the district-wide mix of younger vs older
        grades, since the exact per-student draw is random).
      * ``recommended_staged_limit``: a suggested per-run ``limit`` for
        staged seeding (a few thousand), and ``recommended_run_count``: how
        many runs at that limit it would take to finish.
      * ``note``: a human-readable one-liner about what this means for the
        partner's event stream, suitable for dropping straight into a run
        report.
    """

    low, high = guardians_per_student
    existing = {c.get("Student id", "") for c in stack.contacts()}
    eligible = [
        s for s in stack.distinct_students() if s.get("Student id", "") not in existing
    ]
    n = len(eligible)

    younger = sum(1 for s in eligible if s.get("Grade", "") in _YOUNGER_GRADES)
    older = n - younger
    expected_per_younger = 1 + _YOUNGER_TWO_GUARDIAN_PROB  # E[guardians] if low=1, high=2
    expected_per_older = 1 + _OLDER_TWO_GUARDIAN_PROB
    if (low, high) != (1, 2):
        # Fall back to a plain midpoint if a caller ever uses a non-default
        # range; the weighting constants above are calibrated for (1, 2).
        expected_per_younger = expected_per_older = (low + high) / 2

    expected = round(younger * expected_per_younger + older * expected_per_older)

    recommended_staged_limit = 4000
    run_count = -(-n // recommended_staged_limit) if n else 0  # ceil div

    return {
        "students_without_contacts": n,
        "estimated_contacts_low": n * low,
        "estimated_contacts_high": n * high,
        "estimated_contacts_expected": expected,
        "recommended_staged_limit": recommended_staged_limit,
        "recommended_run_count": run_count,
        "note": (
            f"Seeding all {n} students in one pass would emit roughly "
            f"{expected} users.created (Contacts) events in a single sync "
            f"(between {n * low} and {n * high} depending on the 1-2 "
            f"guardian split) -- an enormous, unrepresentative burst compared "
            f"to the normal drift cadence. Recommend staging via `limit` at "
            f"~{recommended_staged_limit} students/run (~{run_count} runs to "
            f"finish) instead of one unbounded pass."
        ),
    }


def _choose_guardian_count(rng: random.Random, grade: str) -> int:
    prob_two = _YOUNGER_TWO_GUARDIAN_PROB if grade in _YOUNGER_GRADES else _OLDER_TWO_GUARDIAN_PROB
    return 2 if rng.random() < prob_two else 1


def seed_contacts(
    stack: CsvStack,
    content: "_ContentProto",
    *,
    rng: random.Random,
    guardians_per_student: tuple[int, int] = (1, 2),
    limit: int | None = None,
) -> list[Change]:
    """Create baseline guardian contact(s) for students that have none.

    For every student with zero existing contacts (checked against
    ``stack``'s *current* in-memory state, so this is safe to call
    repeatedly across staged runs), creates 1-2 guardian rows:

    * Guardian order is carried by row order for that student, not by a
      ``Sequence`` column -- the SFTP contact spec has no such column, and the
      first guardian occupies the student's original row.
    * ``Contact sis id`` is ``SEED<student id>-<n>``: deterministic, so a
      re-run mints the same id rather than a duplicate guardian, and visibly
      distinct from drift-added ``CON######`` ids. It is written once here and
      never edited, which is what keeps each contact's Clever id stable
      through later email/phone edits.
    * ``Contact type`` / ``Contact phone type`` are drawn from
      ``schema.CONTACT_TYPES`` / ``schema.PHONE_TYPES``.
    * ``Contact relationship`` is drawn so it's internally consistent per
      student -- the first guardian draws from a "primary guardian" pool
      (Mother/Father/Grandmother/Grandfather/Guardian); the second draws from
      the full relationship pool *excluding* whatever the first got, so a
      single student is never given two "Mother" rows.
    * Student-level columns are copied verbatim from the student's own row,
      so every row for that student agrees.
    * Guardian name/email/phone come from ``content`` (the same
      ``ContentGenerator`` interface ``selection.py`` uses) -- this module
      never generates those values itself, keeping AI content generation
      isolated to ``content.py`` per brief §5.

    ``guardians_per_student`` is a ``(low, high)`` tuple; the actual count
    per student is 1 or 2, weighted toward 2 for younger grades (see
    ``_choose_guardian_count``) both because it's plausible (both
    parents/guardians listed more often for younger kids) and because it
    varies the seeded data instead of making every household mechanically
    identical.

    ``limit`` caps how many STUDENTS are processed in this call (not how
    many contacts are created) -- see the module docstring's staged-seeding
    warning; this is the mechanism for spreading a district-wide seed across
    several runs instead of one burst.

    Idempotent: a student who already has at least one contact (whether
    from a prior ``seed_contacts`` call that was applied, or from organic
    drift) is skipped entirely. Running ``seed_contacts`` -> ``stack.apply``
    -> ``seed_contacts`` again against the same stack produces zero new
    changes, because the second call sees the first call's contacts.

    Does NOT apply or push the returned changes -- exactly like
    ``selection.select_changes``, this only decides what to do; the caller
    is responsible for guardrail-checking, applying, and pushing.
    """

    only_low, only_high = guardians_per_student
    changes: list[Change] = []

    existing_student_ids = {c.get("Student id", "") for c in stack.contacts()}
    eligible = [
        s
        for s in stack.distinct_students()
        if s.get("Student id", "") not in existing_student_ids
    ]

    if limit is not None:
        eligible = eligible[:limit]

    for student in eligible:
        student_id = student.get("Student id", "")
        last_name = student.get("Last name", "")
        grade = student.get("Grade", "")

        count = _choose_guardian_count(rng, grade)
        count = max(only_low, min(only_high, count))
        count = min(count, schema.MAX_CONTACTS_PER_STUDENT)

        used_relationships: list[str] = []
        for sequence in range(1, count + 1):
            if sequence == 1:
                pool = list(_PRIMARY_RELATIONSHIPS)
            else:
                pool = [r for r in _SECONDARY_RELATIONSHIPS if r not in used_relationships]
                if not pool:
                    pool = list(_SECONDARY_RELATIONSHIPS)  # exhausted; allow a repeat rather than fail
            relationship = rng.choice(pool)
            used_relationships.append(relationship)

            guardian_name = content.guardian_name(last_name)
            guardian_email = content.guardian_email(guardian_name, last_name)
            phone = content.phone()
            contact_sis_id = f"SEED{student_id}-{sequence}"

            contact_values = {
                "Contact name": guardian_name,
                "Contact type": _contact_type_for(sequence, relationship),
                "Contact relationship": relationship,
                "Contact phone": phone,
                "Contact phone type": rng.choice(schema.PHONE_TYPES),
                "Contact email": guardian_email,
            }
            note = (
                f"Seed: added guardian contact {guardian_name} "
                f"(Contact sis id {contact_sis_id}), guardian {sequence}/{count}, "
                f"relationship {relationship!r}, for student "
                f"{student.get('First name', '')} {last_name} ({student_id})."
            )

            if sequence == 1:
                # The student already occupies exactly one row with the contact
                # columns blank. Fill it rather than adding a row, or the blank
                # row survives and the student appears twice.
                changes.append(
                    Change(
                        filename=schema.STUDENTS.filename,
                        operation=Operation.UPDATE,
                        key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: ""},
                        bucket=Bucket.BIG_STUDENT,
                        expected_event=EventType.USERS_CREATED,
                        event_subject=EventSubject.CONTACT,
                        before=schema.blank_contact_fields(),
                        after={
                            **contact_values,
                            schema.CONTACT_SIS_ID_COLUMN: contact_sis_id,
                        },
                        note=note + " Filled the student's existing blank row in place.",
                        ai_generated=True,
                    )
                )
            else:
                changes.append(
                    Change(
                        filename=schema.STUDENTS.filename,
                        operation=Operation.CREATE,
                        key={
                            "Student id": student_id,
                            schema.CONTACT_SIS_ID_COLUMN: contact_sis_id,
                        },
                        bucket=Bucket.BIG_STUDENT,
                        expected_event=EventType.USERS_CREATED,
                        event_subject=EventSubject.CONTACT,
                        after={**schema.student_fields(student), **contact_values},
                        note=note + " Added as an additional row for the same Student id.",
                        ai_generated=True,
                    )
                )

    return changes
