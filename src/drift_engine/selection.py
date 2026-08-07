"""Chooses WHICH records get touched on a given run, and emits ``Change``\
objects (project brief §4-5).

The weekly *schedule* (``cadence.py``) is rigid and deterministic by design.
This module is the other half of that split: "the specific targets are not"
(brief §4). Given a fixed seed, ``select_changes`` always picks the same
targets in the same order; given a different seed, it picks different ones.
Nothing here decides *when* a bucket runs -- it only decides *which rows*
are touched once ``cadence`` has already said a bucket applies today.

This module also isolates itself from the AI content-generation module
(brief §5: "the content-generation step must be swappable ... without
touching scheduling or selection"). It is coded against the informal
``content`` interface described in the project brief only -- ``middle_name``,
``guardian_name``, ``guardian_email``, ``phone``, ``teacher_name``,
``student_email`` -- and never imports that module at runtime. The import
below is guarded by ``TYPE_CHECKING`` (and, because this file uses
``from __future__ import annotations``, the annotation is never evaluated at
runtime either), so this module works whether or not ``drift_engine.content``
exists yet.

Determinism note: every candidate pool handed to ``random.Random`` is a
*list* built from CsvStack's on-disk row order, never a set. Python's string
hashing is randomized per-process by default, so iterating a ``set`` of ids
would silently break "same seed -> identical Change list" across process
runs even though the ``random.Random`` sequence itself is reproducible. Sets
are only ever used here for O(1) membership checks, never for iteration
order.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from . import schema
from .csvstack import CsvStack
from .models import Bucket, Change, EventSubject, EventType, Operation, RunPlan

if TYPE_CHECKING:
    from .content import ContentGenerator

from .cadence import (
    BIG_STUDENT_CONTACTS_ADDED,
    BIG_STUDENT_CONTACTS_REMOVED,
    BIG_STUDENT_ENROLLMENT_MOVES,
    BIG_TEACHER_COTEACHER_CHANGES,
    BIG_TEACHER_NEW_TEACHERS,
    BIG_TEACHER_SECTION_REASSIGNMENTS,
    BIG_TEACHER_TEACHERS_REMOVED,
    SMALL_DAILY_CONTACT_FIELD_EDITS,
    SMALL_DAILY_STUDENT_FIELD_EDITS,
)

__all__ = ["select_changes"]


# ---------------------------------------------------------------------------
# ID minting
# ---------------------------------------------------------------------------


class _IdMinter:
    """Mints new IDs for engine-created rows that cannot collide with
    anything already in the stack -- including other rows minted earlier in
    the same run, before any of them have actually been applied to the
    stack.

    Engine-created records get a clearly distinguishable prefix so David can
    tell them apart from seeded data at a glance: contacts get ``CON`` +
    a zero-padded counter, new teachers get ``TCH9`` + a counter (real
    seeded teacher ids in the sample data look like ``TCH5000``, so a
    ``TCH9...`` id is unambiguous at a glance without colliding with the
    real numbering space).

    A minted ``Contact sis id`` is PERMANENT. Per Clever's docs, a contact
    carrying an sis id keeps its Clever id across phone/email/name changes,
    and the one thing that *does* change its Clever id is the sis id itself
    changing. So this value is written once at contact creation and never
    touched again -- which is exactly what makes a guardian email edit surface
    as ``users.updated`` instead of a delete-then-create pair.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._minted: dict[str, set[str]] = {}
        self._existing_contact_sis_ids: set[str] | None = None

    def mint(self, stack: CsvStack, *, filename: str, prefix: str, width: int) -> str:
        counter = self._counters.get(prefix, 1)
        minted_here = self._minted.setdefault(prefix, set())
        while True:
            candidate = f"{prefix}{counter:0{width}d}"
            counter += 1
            if candidate in minted_here:
                continue
            # Verify against the stack's existing index, not just our own
            # bookkeeping -- the stack is the source of truth.
            if stack.get(filename, (candidate,)) is not None:
                continue
            minted_here.add(candidate)
            self._counters[prefix] = counter
            return candidate

    def mint_contact_sis_id(self, stack: CsvStack) -> str:
        """Mint an unused ``Contact sis id``.

        Cannot go through :meth:`mint`, which resolves candidates via
        ``stack.get(filename, (candidate,))``: students.csv is keyed on
        (Student id, Contact sis id) now, so a one-element key tuple would
        never match and every candidate would look free. Collision-checks
        against the set of sis ids actually present instead.
        """

        if self._existing_contact_sis_ids is None:
            self._existing_contact_sis_ids = {
                row.get(schema.CONTACT_SIS_ID_COLUMN, "") for row in stack.contacts()
            }
        existing = self._existing_contact_sis_ids
        minted_here = self._minted.setdefault("CON", set())
        counter = self._counters.get("CON", 1)
        while True:
            candidate = f"CON{counter:06d}"
            counter += 1
            if candidate in minted_here or candidate in existing:
                continue
            minted_here.add(candidate)
            self._counters["CON"] = counter
            return candidate

    def mint_teacher_id(self, stack: CsvStack) -> str:
        return self.mint(stack, filename=schema.TEACHERS.filename, prefix="TCH9", width=5)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _shuffled(rng: random.Random, rows: list[dict]) -> list[dict]:
    """A rng-shuffled *copy* of ``rows``, leaving the original order (and
    hence the stack) untouched. Built from a list, so iteration order is
    fully determined by ``rows``' original order plus ``rng``'s state.
    """

    return rng.sample(rows, len(rows)) if rows else []


def _touched(touched: dict[str, set[str]], label: str) -> set[str]:
    return touched.setdefault(label, set())


#: Bound on how many times a field-value generator is re-rolled when its
#: first result turns out to equal the value already on the row (Fix 1(b)).
#: Mirrors ``content._MAX_EMAIL_ATTEMPTS`` -- kept as this module's own
#: constant rather than importing it, since this module never imports
#: ``content`` at runtime (see the module docstring's isolation note).
_MAX_VALUE_REROLLS = 4


def _generate_changed_value(before_value: str, generate) -> str | None:
    """Call ``generate(attempt)`` for increasing ``attempt`` until it returns
    something other than ``before_value``, or give up.

    This is the general invariant Fix 1 requires: an ``Operation.UPDATE``
    whose generated "new" value is identical to the value already on the
    row is not a change at all -- Clever's CSV diff sees nothing, so no
    event is ever emitted, no matter how often selection.py "edits" that
    field. ``content.guardian_email``/``content.student_email`` accept an
    ``attempt`` kwarg specifically so this can ask for a genuinely
    different-looking (but still plausible) value instead of just calling
    the same pure function again; other generators (``content.phone``,
    ``content.middle_name``) are already randomized per call and simply
    ignore ``attempt``, which is why ``generate`` is a plain callable here
    rather than something that inspects the target method.

    Returns ``None`` if every attempt still matches ``before_value`` --
    callers must then skip emitting this change entirely (see brief-driven
    comments at each call site) rather than write a no-op UPDATE.
    """

    for attempt in range(_MAX_VALUE_REROLLS):
        candidate = generate(attempt)
        if candidate != before_value:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Small daily bucket
# ---------------------------------------------------------------------------

_CONTACT_EDIT_FIELDS: tuple[str, ...] = ("Contact email", "Contact phone", "Contact phone type")
_CONTACT_EDIT_WEIGHTS: tuple[int, ...] = (6, 2, 2)  # Email most often.

#: ``Contact sis id`` is deliberately absent from ``_CONTACT_EDIT_FIELDS`` and
#: from ``schema.STUDENTS.mutable``. Editing it would change the contact's
#: Clever id, turning every subsequent edit into a delete-then-create pair.

_STUDENT_EDIT_FIELDS: tuple[str, ...] = ("Middle name", "Student email")
_STUDENT_EDIT_WEIGHTS: tuple[int, ...] = (7, 3)  # Middle name is the primary driver.


def _small_daily(
    stack: CsvStack,
    content: "ContentGenerator",
    *,
    rng: random.Random,
    touched: dict[str, set[str]],
) -> list[Change]:
    changes: list[Change] = []

    # -- Contact field edits -------------------------------------------------
    # Contacts are rows on students.csv (schema module docstring), so a
    # contact row already carries its student's columns -- no join needed to
    # read the student's last name. A district whose students have no
    # guardians yet has zero contacts; that is not an error, it just means
    # this sub-bucket contributes nothing until seeding has run.
    contacts = stack.contacts()
    if contacts:
        # Shared with big_student's contact removal below, so the same
        # contact is never both field-edited and removed in one run -- that
        # would read as a confusing non-sequitur in David's audit log.
        contact_touched = _touched(touched, "contacts:touched")
        pool = [
            c
            for c in _shuffled(rng, contacts)
            if c[schema.CONTACT_SIS_ID_COLUMN] not in contact_touched
        ]
        for contact in pool[:SMALL_DAILY_CONTACT_FIELD_EDITS]:
            contact_sis_id = contact[schema.CONTACT_SIS_ID_COLUMN]
            contact_touched.add(contact_sis_id)
            field = rng.choices(_CONTACT_EDIT_FIELDS, weights=_CONTACT_EDIT_WEIGHTS, k=1)[0]
            student_id = contact.get("Student id", "")
            student_last_name = contact.get("Last name", "")
            before_value = contact.get(field, "")

            ai_generated = False
            if field == "Contact email":
                # Fix 1: ``guardian_email`` is a PURE function of (name,
                # student last name) -- calling it again with the exact same
                # inputs used to recompute the exact same address every
                # time, which is why 62% of predicted contacts.updated
                # events were actually no-op writes (CSV value unchanged,
                # Clever's diff sees nothing, no event emitted). ``attempt``
                # asks for a genuinely different-styled address for the same
                # person instead.
                contact_name = contact.get("Contact name", "")
                new_value = _generate_changed_value(
                    before_value,
                    lambda attempt: content.guardian_email(
                        contact_name, student_last_name, attempt=attempt
                    ),
                )
                ai_generated = True
            elif field == "Contact phone":
                new_value = _generate_changed_value(before_value, lambda attempt: content.phone())
                ai_generated = True
            else:  # Contact phone type -- a fixed domain pick, not AI content.
                choices = [v for v in schema.PHONE_TYPES if v != before_value]
                new_value = rng.choice(choices) if choices else None

            if new_value is None:
                # Fix 1(b): every attempt still matched the current value --
                # a change that changes nothing is a lie in the audit log,
                # so skip it rather than emit a no-op UPDATE.
                continue

            changes.append(
                Change(
                    filename=schema.STUDENTS.filename,
                    operation=Operation.UPDATE,
                    key={
                        "Student id": student_id,
                        schema.CONTACT_SIS_ID_COLUMN: contact_sis_id,
                    },
                    bucket=Bucket.SMALL_DAILY,
                    expected_event=EventType.USERS_UPDATED,
                    event_subject=EventSubject.CONTACT,
                    before={field: before_value},
                    after={field: new_value},
                    note=(
                        f"Small daily: changed {field!r} for contact "
                        f"{contact.get('Contact name', contact_sis_id)} "
                        f"(student {student_id}) from {before_value!r} to {new_value!r}. "
                        f"Contact sis id {contact_sis_id} is unchanged, so this is "
                        "expected to surface as users.updated, not delete-then-create."
                    ),
                    ai_generated=ai_generated,
                )
            )

    # -- Student field edits ---------------------------------------------------
    # Brief §4: small daily changes "primarily affect student records."
    # ``distinct_students``, not ``students``: a student with N contacts
    # occupies N rows, so sampling raw rows would weight each student by their
    # guardian count and hand students with more contacts proportionally more
    # of the daily edits. The edit itself still lands on a single row and
    # ``CsvStack.apply`` fans student-level columns out to the siblings, so
    # the student never presents two different emails in one file.
    student_touched = _touched(touched, "students.csv:field_edit")
    students = [
        s
        for s in _shuffled(rng, stack.distinct_students())
        if s["Student id"] not in student_touched
    ]
    for student in students[:SMALL_DAILY_STUDENT_FIELD_EDITS]:
        student_touched.add(student["Student id"])
        field = rng.choices(_STUDENT_EDIT_FIELDS, weights=_STUDENT_EDIT_WEIGHTS, k=1)[0]
        first, last = student.get("First name", ""), student.get("Last name", "")
        before_value = student.get(field, "")

        if field == "Middle name":
            new_value = _generate_changed_value(
                before_value, lambda attempt: content.middle_name(first, last)
            )
        else:
            student_number = student.get("Student number", "")
            # Fix 1: ``student_email`` is just as pure a function of its
            # inputs as ``guardian_email`` was -- same reroll-via-``attempt``
            # treatment applies here for the same reason.
            new_value = _generate_changed_value(
                before_value,
                lambda attempt: content.student_email(first, last, student_number, attempt=attempt),
            )

        if new_value is None:
            # Fix 1(b): could not produce an actual change after rerolling;
            # skip rather than emit a no-op UPDATE.
            continue

        changes.append(
            Change(
                filename=schema.STUDENTS.filename,
                operation=Operation.UPDATE,
                key={
                    "Student id": student["Student id"],
                    schema.CONTACT_SIS_ID_COLUMN: student.get(
                        schema.CONTACT_SIS_ID_COLUMN, ""
                    ),
                },
                bucket=Bucket.SMALL_DAILY,
                expected_event=EventType.USERS_UPDATED,
                event_subject=EventSubject.STUDENT,
                before={field: before_value},
                after={field: new_value},
                note=(
                    f"Small daily: set {field!r} for student {first} {last} "
                    f"({student['Student id']}) to {new_value!r}. Applied to every "
                    "row for this student, since a student's contact rows must "
                    "carry identical student-level columns."
                ),
                ai_generated=True,
            )
        )

    return changes


# ---------------------------------------------------------------------------
# Big student bucket (Tue/Thu)
# ---------------------------------------------------------------------------


def _find_move_target(
    stack: CsvStack, student: dict, current_section_id: str, rng: random.Random
) -> dict | None:
    """Pick a plausible new section for ``student``, or ``None`` if none exists.

    Per project brief §3, moving a student between sections is a
    section-membership change (sections.updated), never a user field change.
    Per the assignment: same school always; same grade preferred when
    possible; never back into a section the student is already enrolled in.

    Fix 4: the target is drawn with ``rng.choice``, not always
    ``candidates[0]``/``same_grade[0]``. Always picking the first candidate
    meant every enrollment move within a given school+grade funnelled into
    the SAME section (whichever happened to sort first), so that section's
    roster grew monotonically every Tue/Thu run while its siblings never
    gained anyone -- an unseeded bias with no randomization justification.
    """

    school_id = student.get("School id", "")
    current_section_ids = {
        e["Section id"] for e in stack.enrollments_for_student(student["Student id"])
    }
    candidates = [
        s
        for s in stack.sections_in_school(school_id)
        if s["Section id"] != current_section_id and s["Section id"] not in current_section_ids
    ]
    if not candidates:
        return None

    student_grade = student.get("Grade", "")
    same_grade = [s for s in candidates if s.get("Grade", "") == student_grade]
    return rng.choice(same_grade) if same_grade else rng.choice(candidates)


def _big_student(
    stack: CsvStack,
    content: "ContentGenerator",
    *,
    rng: random.Random,
    touched: dict[str, set[str]],
    id_minter: _IdMinter,
) -> list[Change]:
    changes: list[Change] = []

    # -- Enrollment moves --------------------------------------------------
    moved_students = _touched(touched, "students.csv:enrollment_move")
    enrollment_pool = _shuffled(rng, stack.enrollments())
    moves_made = 0
    for enrollment in enrollment_pool:
        if moves_made >= BIG_STUDENT_ENROLLMENT_MOVES:
            break
        student_id = enrollment.get("Student id", "")
        if student_id in moved_students:
            continue
        # student_rows_for, not stack.get: students.csv is keyed on
        # (Student id, Contact sis id) and an enrollment row only has the
        # Student id half. Any of the student's rows serves here -- only
        # student-level columns are read below, and those are identical
        # across siblings.
        student_rows = stack.student_rows_for(student_id)
        if not student_rows:
            continue
        student = student_rows[0]
        old_section_id = enrollment.get("Section id", "")
        new_section = _find_move_target(stack, student, old_section_id, rng)
        if new_section is None:
            continue  # No plausible target for this student; try another.

        moved_students.add(student_id)
        moves_made += 1
        school_id = student.get("School id", "")
        new_section_id = new_section["Section id"]
        note = (
            f"Big student (enrollment move): moved student {student.get('First name', '')} "
            f"{student.get('Last name', '')} ({student_id}) from section {old_section_id} "
            f"to section {new_section_id}. This is a section-membership change and is "
            "expected to surface as sections.updated, NOT users.updated (brief §3)."
        )
        changes.append(
            Change(
                filename=schema.ENROLLMENTS.filename,
                operation=Operation.DELETE,
                key={"Section id": old_section_id, "Student id": student_id},
                bucket=Bucket.BIG_STUDENT,
                expected_event=EventType.SECTIONS_UPDATED,
                event_subject=EventSubject.SECTION,
                before={"School id": school_id, "Section id": old_section_id, "Student id": student_id},
                note=note,
            )
        )
        changes.append(
            Change(
                filename=schema.ENROLLMENTS.filename,
                operation=Operation.CREATE,
                key={"Section id": new_section_id, "Student id": student_id},
                bucket=Bucket.BIG_STUDENT,
                expected_event=EventType.SECTIONS_UPDATED,
                event_subject=EventSubject.SECTION,
                after={"School id": school_id},
                note=note,
            )
        )

    # -- Contacts added ------------------------------------------------------
    # Two shapes, because a contact is a ROW on students.csv:
    #
    #   * Student already has >=1 contact -> CREATE a new row carrying the
    #     same student-level columns plus this guardian's contact columns.
    #   * Student has 0 contacts -> they still occupy exactly one row, with
    #     the contact columns blank. Filling that row in place is an UPDATE,
    #     not a CREATE, because creating a row would leave the blank one
    #     behind and duplicate the student.
    #
    # Either way the predicted event is users.created (Contacts): a guardian
    # object that did not exist now does. This is the one place in the engine
    # where the CSV operation and the Clever-level event deliberately
    # disagree, so the guardrail keys on the event, not the operation.
    contact_added_for = _touched(touched, "students.csv:contact_added")
    students_for_contacts = [
        s
        for s in _shuffled(rng, stack.distinct_students())
        if s["Student id"] not in contact_added_for
        # Spec ceiling: at most 5 contacts per student over SFTP. A student
        # already at the cap is skipped rather than truncated, so the run
        # simply adds one fewer guardian that day.
        and len(stack.contacts_for_student(s["Student id"])) < schema.MAX_CONTACTS_PER_STUDENT
    ]
    for student in students_for_contacts[:BIG_STUDENT_CONTACTS_ADDED]:
        student_id = student["Student id"]
        contact_added_for.add(student_id)
        last_name = student.get("Last name", "")
        guardian_name = content.guardian_name(last_name)
        guardian_email = content.guardian_email(guardian_name, last_name)
        phone = content.phone()
        contact_sis_id = id_minter.mint_contact_sis_id(stack)
        existing = stack.contacts_for_student(student_id)

        contact_values = {
            "Contact name": guardian_name,
            "Contact type": rng.choice(schema.CONTACT_TYPES),
            "Contact relationship": rng.choice(schema.RELATIONSHIPS),
            "Contact phone": phone,
            "Contact phone type": rng.choice(schema.PHONE_TYPES),
            "Contact email": guardian_email,
        }
        note = (
            f"Big student: added guardian contact {guardian_name} "
            f"(Contact sis id {contact_sis_id}) for student "
            f"{student.get('First name', '')} {last_name} ({student_id}), "
            f"giving them {len(existing) + 1} of at most "
            f"{schema.MAX_CONTACTS_PER_STUDENT} contact(s)."
        )

        if existing:
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
                    # The new row must repeat the student's columns verbatim,
                    # or this student would present blank student-level values
                    # on one of their rows.
                    after={**schema.student_fields(student), **contact_values},
                    note=note + " New students.csv row for the same Student id.",
                    ai_generated=True,
                )
            )
        else:
            changes.append(
                Change(
                    filename=schema.STUDENTS.filename,
                    operation=Operation.UPDATE,
                    key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: ""},
                    bucket=Bucket.BIG_STUDENT,
                    expected_event=EventType.USERS_CREATED,
                    event_subject=EventSubject.CONTACT,
                    before=schema.blank_contact_fields(),
                    after={**contact_values, schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
                    note=(
                        note + " Filled the student's existing contact-less row in "
                        "place; a CSV UPDATE, but a new guardian object to Clever."
                    ),
                    ai_generated=True,
                )
            )

    # -- Contacts removed ------------------------------------------------------
    # Hard rule: never remove a student's last remaining contact. The
    # eligibility check below uses the stack's pre-run per-student contact
    # count *minus* however many of that same student's contacts have
    # already been picked for removal earlier in this loop -- a plain
    # ``count > 1`` check against the static stack would let two contacts
    # belonging to the same 2-contact student both be selected in one run
    # (each individually "looks" safe), which would orphan the student once
    # both deletes were applied. Counting removals-in-progress closes that
    # gap. Using pre-run counts only (not counting contacts added above,
    # which have not been applied yet) is deliberately conservative -- it
    # may skip a removal that would technically be safe once this run's
    # additions land, but it can never orphan a student.
    # Because a contact IS a row, removing one is a row DELETE -- and the
    # "never remove a student's last contact" rule below is what keeps that
    # safe. A student's last contact row is also their last row, so deleting
    # it would delete the student too. CsvStack.apply refuses that outright as
    # a backstop; this loop is what makes sure it never comes up.
    removed_contacts = _touched(touched, "contacts:touched")
    removed_per_student: dict[str, int] = {}
    removals_made = 0
    for contact in _shuffled(rng, stack.contacts()):
        if removals_made >= BIG_STUDENT_CONTACTS_REMOVED:
            break
        contact_sis_id = contact[schema.CONTACT_SIS_ID_COLUMN]
        if contact_sis_id in removed_contacts:
            continue
        student_id = contact.get("Student id", "")
        original_count = len(stack.contacts_for_student(student_id))
        already_removed = removed_per_student.get(student_id, 0)
        if original_count - already_removed <= 1:
            continue  # Would orphan this student once applied; skip.

        removed_contacts.add(contact_sis_id)
        removed_per_student[student_id] = already_removed + 1
        removals_made += 1
        remaining = original_count - removed_per_student[student_id]
        changes.append(
            Change(
                filename=schema.STUDENTS.filename,
                operation=Operation.DELETE,
                key={
                    "Student id": student_id,
                    schema.CONTACT_SIS_ID_COLUMN: contact_sis_id,
                },
                bucket=Bucket.BIG_STUDENT,
                expected_event=EventType.USERS_DELETED,
                event_subject=EventSubject.CONTACT,
                before=schema.contact_fields(contact),
                note=(
                    f"Big student: removed guardian contact {contact.get('Contact name', '')} "
                    f"(Contact sis id {contact_sis_id}) for student {student_id}, "
                    f"leaving {remaining} contact(s) on record. Drops that "
                    "students.csv row; the student keeps their other row(s)."
                ),
            )
        )

    return changes


# ---------------------------------------------------------------------------
# Big teacher bucket (Fri)
# ---------------------------------------------------------------------------


def _same_school_teachers(stack: CsvStack, school_id: str, *, exclude: set[str]) -> list[dict]:
    return [
        t
        for t in stack.teachers()
        if t.get("School id", "") == school_id and t["Teacher id"] not in exclude
    ]


def _big_teacher(
    stack: CsvStack,
    content: "ContentGenerator",
    *,
    rng: random.Random,
    touched: dict[str, set[str]],
    id_minter: _IdMinter,
) -> list[Change]:
    changes: list[Change] = []

    # -- Co-teacher changes ---------------------------------------------------
    coteacher_touched = _touched(touched, "sections.csv:teacher2_edit")
    sections_for_coteacher = [
        s for s in _shuffled(rng, stack.sections()) if s["Section id"] not in coteacher_touched
    ]
    made = 0
    for section in sections_for_coteacher:
        if made >= BIG_TEACHER_COTEACHER_CHANGES:
            break
        school_id = section.get("School id", "")
        current_primary = section.get("Teacher id", "")
        current_co = section.get("Teacher 2 id", "")

        if current_co and rng.random() < 0.3:
            # Clear the existing co-teacher.
            new_co = ""
        else:
            candidates = _same_school_teachers(
                stack, school_id, exclude={current_primary, current_co} - {""}
            )
            if not candidates:
                continue
            new_co = rng.choice(candidates)["Teacher id"]

        coteacher_touched.add(section["Section id"])
        made += 1
        changes.append(
            Change(
                filename=schema.SECTIONS.filename,
                operation=Operation.UPDATE,
                key={"Section id": section["Section id"]},
                bucket=Bucket.BIG_TEACHER,
                expected_event=EventType.SECTIONS_UPDATED,
                event_subject=EventSubject.SECTION,
                before={"Teacher 2 id": current_co},
                after={"Teacher 2 id": new_co},
                note=(
                    f"Big teacher: co-teacher on section {section['Section id']} "
                    f"({section.get('Name', '')}) changed from {current_co!r} to {new_co!r}."
                ),
            )
        )

    # -- Section (primary teacher) reassignments -------------------------------
    reassign_touched = _touched(touched, "sections.csv:teacher_edit")
    sections_for_reassign = [
        s for s in _shuffled(rng, stack.sections()) if s["Section id"] not in reassign_touched
    ]
    made = 0
    for section in sections_for_reassign:
        if made >= BIG_TEACHER_SECTION_REASSIGNMENTS:
            break
        school_id = section.get("School id", "")
        current_primary = section.get("Teacher id", "")
        current_co = section.get("Teacher 2 id", "")
        candidates = _same_school_teachers(
            stack, school_id, exclude={current_primary, current_co} - {""}
        )
        if not candidates:
            continue
        new_primary = rng.choice(candidates)["Teacher id"]

        reassign_touched.add(section["Section id"])
        made += 1
        changes.append(
            Change(
                filename=schema.SECTIONS.filename,
                operation=Operation.UPDATE,
                key={"Section id": section["Section id"]},
                bucket=Bucket.BIG_TEACHER,
                expected_event=EventType.SECTIONS_UPDATED,
                event_subject=EventSubject.SECTION,
                before={"Teacher id": current_primary},
                after={"Teacher id": new_primary},
                note=(
                    f"Big teacher: reassigned primary teacher on section "
                    f"{section['Section id']} ({section.get('Name', '')}) from "
                    f"{current_primary!r} to {new_primary!r}."
                ),
            )
        )

    # -- New teachers ------------------------------------------------------
    schools = stack.schools()
    # Schools that gain a teacher this run -- the attrition pass below must
    # never remove from one of these (brief: removal is "always from another
    # school"), so it is tracked even though today's magnitude is always 1.
    schools_gained: set[str] = set()
    for _ in range(BIG_TEACHER_NEW_TEACHERS):
        if not schools:
            break
        school = rng.choice(schools)
        schools_gained.add(school["School id"])
        first, last = content.teacher_name()
        teacher_id = id_minter.mint_teacher_id(stack)
        # Fix 7: delegate to the content module's own ``teacher_email``
        # rather than building the address inline here. Building it here
        # both hard-coded the Tulsa-only ``schema.STAFF_EMAIL_DOMAIN``
        # (wrong for any second sandbox district, brief §6) and was itself
        # a small brief §5 boundary violation -- content generation leaking
        # into the selection module, which is supposed to only ever call
        # the ``ContentGenerator`` protocol, never construct values itself.
        email = content.teacher_email(first, last)

        changes.append(
            Change(
                filename=schema.TEACHERS.filename,
                operation=Operation.CREATE,
                key={"Teacher id": teacher_id},
                bucket=Bucket.BIG_TEACHER,
                expected_event=EventType.USERS_CREATED,
                event_subject=EventSubject.TEACHER,
                after={
                    "School id": school["School id"],
                    "Teacher number": teacher_id,
                    "Teacher email": email,
                    "First name": first,
                    "Last name": last,
                    "Title": "Teacher",
                },
                note=(
                    f"Big teacher: added new teacher {first} {last} ({teacher_id}) "
                    f"to school {school['School id']}."
                ),
                ai_generated=True,
            )
        )

    # -- Teacher attrition (paired with the addition above) -----------------
    # Closes the "no attrition" known limitation: with nothing ever removing
    # a teacher, headcount only ever grew, eventually breaching
    # safety.MAX_SCALE_DRIFT. Removing one teacher a week -- always from a
    # DIFFERENT school than the one that just gained one, so no single school
    # is ever seen both losing and gaining a teacher in the same run --
    # stabilizes the district's total teacher count without touching the
    # small-daily/big-student cadence.
    #
    # A teacher can be a section's primary ("Teacher id", required) or its
    # co-teacher ("Teacher 2 id", optional). Deleting one who still holds
    # either would leave a dangling reference no downstream ingest could
    # resolve -- the exact class of bug already fixed once for contacts/
    # students (README "KNOWN BLOCKER"). So a candidate is only removed once
    # every section referencing them has somewhere else to point: primary
    # slots are reassigned to another same-school teacher, co-teacher slots
    # are simply cleared. A candidate with no safe reassignment target for
    # some section (only possible in a school with exactly one teacher) is
    # skipped in favor of the next candidate, never forced through.
    #
    # ``section_overrides`` folds in every sections.csv Teacher id/Teacher 2
    # id change already staged above (co-teacher swaps, primary
    # reassignments) so a teacher who was JUST assigned somewhere earlier in
    # THIS run is never mistaken for free of sections, and a section already
    # touched for a given field this run (``coteacher_touched``/
    # ``reassign_touched``) is never selected again for that same field --
    # the same "each field touched at most once per run" rule every other
    # bucket in this module already follows.
    section_overrides: dict[str, dict[str, str]] = {}
    for c in changes:
        if c.filename == schema.SECTIONS.filename and c.operation is Operation.UPDATE:
            section_overrides.setdefault(c.key["Section id"], {}).update(c.after)

    def _effective_teacher_fields(section: dict) -> tuple[str, str]:
        override = section_overrides.get(section["Section id"], {})
        primary = override.get("Teacher id", section.get("Teacher id", ""))
        co = override.get("Teacher 2 id", section.get("Teacher 2 id", ""))
        return primary, co

    removed_teacher_touched = _touched(touched, "teachers.csv:removed")
    removal_pool = [
        t
        for t in _shuffled(rng, stack.teachers())
        if t.get("School id", "") not in schools_gained
        and t["Teacher id"] not in removed_teacher_touched
    ]

    for _ in range(BIG_TEACHER_TEACHERS_REMOVED):
        removed_teacher: dict | None = None
        staged: list[Change] = []

        for candidate in removal_pool:
            teacher_id = candidate["Teacher id"]
            if teacher_id in removed_teacher_touched:
                continue
            school_id = candidate.get("School id", "")
            candidate_changes: list[Change] = []
            safe = True

            for section in stack.sections():
                primary, co = _effective_teacher_fields(section)
                section_id = section["Section id"]

                if primary == teacher_id:
                    if section_id in reassign_touched:
                        safe = False
                        break
                    replacements = _same_school_teachers(
                        stack, school_id, exclude={teacher_id, co} - {""}
                    )
                    if not replacements:
                        safe = False
                        break
                    new_primary = rng.choice(replacements)["Teacher id"]
                    candidate_changes.append(
                        Change(
                            filename=schema.SECTIONS.filename,
                            operation=Operation.UPDATE,
                            key={"Section id": section_id},
                            bucket=Bucket.BIG_TEACHER,
                            expected_event=EventType.SECTIONS_UPDATED,
                            event_subject=EventSubject.SECTION,
                            before={"Teacher id": teacher_id},
                            after={"Teacher id": new_primary},
                            note=(
                                f"Big teacher: reassigned section {section_id} "
                                f"({section.get('Name', '')}) from departing "
                                f"teacher {teacher_id} to {new_primary!r} ahead "
                                "of removal."
                            ),
                        )
                    )
                    continue

                if co == teacher_id:
                    if section_id in coteacher_touched:
                        safe = False
                        break
                    candidate_changes.append(
                        Change(
                            filename=schema.SECTIONS.filename,
                            operation=Operation.UPDATE,
                            key={"Section id": section_id},
                            bucket=Bucket.BIG_TEACHER,
                            expected_event=EventType.SECTIONS_UPDATED,
                            event_subject=EventSubject.SECTION,
                            before={"Teacher 2 id": teacher_id},
                            after={"Teacher 2 id": ""},
                            note=(
                                f"Big teacher: cleared departing co-teacher "
                                f"{teacher_id} from section {section_id} "
                                f"({section.get('Name', '')})."
                            ),
                        )
                    )

            if not safe:
                continue

            removed_teacher = candidate
            staged = candidate_changes
            break

        if removed_teacher is None:
            continue  # No safe candidate this run; skip rather than orphan a section.

        teacher_id = removed_teacher["Teacher id"]
        removed_teacher_touched.add(teacher_id)
        for staged_change in staged:
            section_overrides.setdefault(staged_change.key["Section id"], {}).update(
                staged_change.after
            )
            if "Teacher id" in staged_change.after:
                reassign_touched.add(staged_change.key["Section id"])
            if "Teacher 2 id" in staged_change.after:
                coteacher_touched.add(staged_change.key["Section id"])
        changes.extend(staged)
        changes.append(
            Change(
                filename=schema.TEACHERS.filename,
                operation=Operation.DELETE,
                key={"Teacher id": teacher_id},
                bucket=Bucket.BIG_TEACHER,
                expected_event=EventType.USERS_DELETED,
                event_subject=EventSubject.TEACHER,
                before={col: removed_teacher.get(col, "") for col in schema.TEACHERS.columns},
                note=(
                    f"Big teacher: removed teacher "
                    f"{removed_teacher.get('First name', '')} "
                    f"{removed_teacher.get('Last name', '')} ({teacher_id}) from "
                    f"school {removed_teacher.get('School id', '')}, balancing "
                    "the new teacher added elsewhere this run."
                ),
            )
        )

    return changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def select_changes(
    stack: CsvStack, plan: RunPlan, content: "ContentGenerator", *, rng: random.Random
) -> list[Change]:
    """Select the targets for ``plan``'s buckets and return their ``Change`` objects.

    Deterministic given ``rng``'s seed; the *specific* targets picked are
    randomized within the fixed structure the cadence module already decided
    (brief §4). Never mutates ``stack`` -- the caller is responsible for
    applying the returned changes (see ``CsvStack.apply``).

    If ``plan.skipped`` (a weekend), returns an empty list without touching
    anything.
    """

    if plan.skipped:
        return []

    changes: list[Change] = []
    # Shared across every bucket function in this run so a record already
    # touched for a given field earlier today (e.g. in the small daily pass)
    # is not selected again for that same field later in the same run.
    touched: dict[str, set[str]] = {}
    id_minter = _IdMinter()

    for bucket in plan.buckets:
        if bucket is Bucket.SMALL_DAILY:
            changes.extend(_small_daily(stack, content, rng=rng, touched=touched))
        elif bucket is Bucket.BIG_STUDENT:
            changes.extend(
                _big_student(stack, content, rng=rng, touched=touched, id_minter=id_minter)
            )
        elif bucket is Bucket.BIG_TEACHER:
            changes.extend(
                _big_teacher(stack, content, rng=rng, touched=touched, id_minter=id_minter)
            )
        else:  # pragma: no cover - Bucket is an exhaustive enum
            raise ValueError(f"Unknown bucket {bucket!r}")

    return [c for c in changes if not _is_noop_update(c)]


def _is_noop_update(change: Change) -> bool:
    """Fix 1(b), belt-and-braces: an ``Operation.UPDATE`` whose ``after``
    matches ``before`` for every field it touches is not a real change --
    the CSV diff Clever computes would see nothing, so no event would ever
    be emitted, no matter what ``expected_event`` claims. Every UPDATE site
    above already re-rolls via ``_generate_changed_value`` before it ever
    builds a ``Change``, so this should never actually trigger in practice;
    it exists as a final, cheap safety net so a future UPDATE call site that
    forgets to re-roll fails safe (drops the no-op change) instead of
    silently lying in the audit log.
    """

    if change.operation is not Operation.UPDATE:
        return False
    return all(change.after.get(field, before) == before for field, before in change.before.items())
