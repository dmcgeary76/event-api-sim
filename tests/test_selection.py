"""Tests for drift_engine.selection.

Uses a small synthetic CsvStack (2 schools, 30 students, 10 teachers,
8 sections, one enrollment per student, and a handful of contacts with
deliberately mixed counts) built in ``tmp_path``, plus a fake content
generator that returns canned, inspectable strings instead of calling an
LLM.

CONTACTS ARE ROWS ON students.csv (corrected 2026-08-05)
--------------------------------------------------------
An earlier version of this file wrote a standalone ``contacts.csv``. That
file does not exist in Clever's SFTP spec (SFTP Instructions v2.1.1) -- a
student with N guardians occupies N students.csv rows sharing one
``Student id``, each carrying one guardian in the seven unsuffixed
``schema.CONTACT_COLUMNS``; a student with none has exactly one row with
those columns blank. ``_write_synthetic_stack`` therefore builds contacts
through :func:`schema.expand_contact_rows`, the one function that encodes
that pattern, rather than re-deriving it here.

Two consequences run through every test below:

  * A change's ``filename`` no longer tells you whether it is about a student
    or a contact -- both are ``students.csv``. The discriminator is
    ``Change.event_subject`` (plus ``expected_event``, since a contact ADD can
    be a CSV ``UPDATE``); see the ``_is_*`` predicates below.
  * "The students" is ``stack.distinct_students()``, not ``stack.students()``
    (which is one row per contact). Picking from raw rows would weight each
    student by their guardian count.

Synthetic layout (see ``_write_synthetic_stack``):
  * SCH1: TCH1-TCH5 (TCH5 unused as a section owner -- a "spare" for
    reassignment/co-teacher tests), SEC1/SEC2 (grade 3), SEC3/SEC4 (grade 4).
  * SCH2: TCH6-TCH10 (TCH10 spare), SEC5/SEC6 (grade 6), SEC7/SEC8 (grade 7).
  * STU1-STU15 at SCH1 (alternating grade 3/4, enrolled in SEC1 or SEC3 --
    SEC2/SEC4 are deliberately left empty as same-grade move targets).
  * STU16-STU30 at SCH2 (alternating grade 6/7, enrolled in SEC5 or SEC7 --
    SEC6/SEC8 left empty as move targets).
  * Contacts: STU1-STU5 each have two contacts (safe to remove one),
    STU6-STU10 each have exactly one contact (must never be removed),
    STU11-STU30 have zero contacts. So students.csv is 35 rows for 30
    students -- the whole point of the row-per-contact shape.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from drift_engine import schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, EventSubject, EventType, Operation, RunPlan
from drift_engine.selection import select_changes

CRLF = "\r\n"


class FakeContent:
    """Canned, inspectable stand-in for the real AI content generator."""

    def middle_name(self, first_name: str, last_name: str) -> str:
        return f"Mid{first_name}"

    def guardian_name(self, student_last_name: str) -> str:
        return f"Guardian{student_last_name}"

    def guardian_email(
        self, guardian_name: str, student_last_name: str, *, attempt: int = 0
    ) -> str:
        # Fix 1: mirrors the real generator's ``attempt`` contract -- a
        # different ``attempt`` must produce a genuinely different-looking
        # (but still deterministic-per-attempt) value, so tests exercising
        # selection.py's reroll-on-no-op logic have something real to
        # reroll through.
        suffix = "" if attempt == 0 else str(attempt + 1)
        return f"{guardian_name}.{student_last_name}{suffix}@example.com".lower()

    def phone(self) -> str:
        return "555-010-0100"

    def teacher_name(self) -> tuple[str, str]:
        return ("Nova", "Instructor")

    def teacher_email(self, first: str, last: str) -> str:
        return f"{first}.{last}@example.com".lower()

    def student_email(
        self, first: str, last: str, student_number: str, *, attempt: int = 0
    ) -> str:
        suffix = "" if attempt == 0 else str(attempt + 1)
        return f"{first}.{last}{suffix}.{student_number}@example.com".lower()


# ---------------------------------------------------------------------------
# Change classification helpers.
#
# Contacts and students now live in the SAME file, so ``c.filename`` cannot
# tell them apart and ``c.operation`` cannot either: a contact ADD is a row
# CREATE when the student already has guardians, but an in-place UPDATE of
# their blank row when they had none (both predict users.created). The honest
# discriminator is the pair (event_subject, expected_event), which is also
# exactly what the guardrail keys on -- so these predicates read the same way
# the engine does.
# ---------------------------------------------------------------------------


def _is_contact_field_edit(change) -> bool:
    """Small-daily edit to an existing guardian's email/phone/phone type."""

    return (
        change.filename == schema.STUDENTS.filename
        and change.event_subject is EventSubject.CONTACT
        and change.expected_event is EventType.USERS_UPDATED
    )


def _is_contact_add(change) -> bool:
    """A guardian that did not exist now does, in either CSV shape."""

    return (
        change.filename == schema.STUDENTS.filename
        and change.event_subject is EventSubject.CONTACT
        and change.expected_event is EventType.USERS_CREATED
    )


def _is_contact_removal(change) -> bool:
    return (
        change.filename == schema.STUDENTS.filename
        and change.event_subject is EventSubject.CONTACT
        and change.expected_event is EventType.USERS_DELETED
    )


def _is_student_field_edit(change) -> bool:
    return (
        change.filename == schema.STUDENTS.filename
        and change.operation is Operation.UPDATE
        and change.event_subject is EventSubject.STUDENT
    )


def _new_contact_sis_id(change) -> str:
    """The ``Contact sis id`` a contact-add change brings into existence.

    A row CREATE carries it in the key (it is half the students.csv natural
    key); a blank-row fill carries it in ``after``, because that row's key
    still has the sis id blank until the change is applied.
    """

    if change.operation is Operation.CREATE:
        return change.key[schema.CONTACT_SIS_ID_COLUMN]
    return change.after[schema.CONTACT_SIS_ID_COLUMN]


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in columns))
    path.write_text((CRLF.join(lines) + CRLF), encoding="utf-8")


#: Default guardian distribution for the synthetic stack -- see the module
#: docstring. Anything not listed here has zero contacts, i.e. one
#: students.csv row with the seven contact columns blank.
_DEFAULT_CONTACT_COUNTS: dict[str, int] = {
    **{f"STU{i}": 2 for i in range(1, 6)},
    **{f"STU{i}": 1 for i in range(6, 11)},
}

#: Per-contact suffix, sized to ``schema.MAX_CONTACTS_PER_STUDENT`` so a test
#: can build a student sitting exactly on the spec's ceiling.
_CONTACT_SUFFIXES: tuple[str, ...] = ("A", "B", "C", "D", "E")
_SYNTH_RELATIONSHIPS: tuple[str, ...] = (
    "Mother", "Father", "Grandmother", "Grandfather", "Aunt",
)


def _synthetic_contacts(student: dict[str, str], count: int) -> list[dict[str, str]]:
    """``count`` guardians for ``student``, as contact-column dicts.

    Returns only the contact half of a row; ``schema.expand_contact_rows`` is
    what pairs each one with the student half. Relationships are distinct per
    student so the fixture never presents one child with two "Mother" rows.

    ``Contact sis id`` values look like ``CTXSTU1A`` -- deliberately NOT the
    ``CON######`` shape ``selection._IdMinter`` mints, so a test can tell a
    pre-existing guardian from one this run created at a glance.
    """

    sid = student["Student id"]
    return [
        {
            "Contact relationship": _SYNTH_RELATIONSHIPS[n],
            "Contact type": "Parent" if n < 2 else "Emergency",
            "Contact name": f"Guardian{sid}{_CONTACT_SUFFIXES[n]}",
            "Contact phone": f"918-555-01{n:02d}",
            "Contact phone type": "Mobile" if n == 0 else "Home",
            "Contact email": f"guardian{sid}{_CONTACT_SUFFIXES[n]}@example.com".lower(),
            schema.CONTACT_SIS_ID_COLUMN: f"CTX{sid}{_CONTACT_SUFFIXES[n]}",
        }
        for n in range(count)
    ]


def _write_synthetic_stack(
    directory: Path,
    *,
    with_contacts: bool = True,
    contact_counts: dict[str, int] | None = None,
) -> None:
    """Write the synthetic stack described in the module docstring.

    ``contact_counts`` overrides the default guardian distribution
    (student id -> number of contacts); students absent from it get none.
    ``with_contacts=False`` is shorthand for "nobody has any", i.e. every
    student is a single row with the contact columns blank -- the state
    David's real export is actually in before ``seed.py`` has ever run.
    """

    directory.mkdir(parents=True, exist_ok=True)

    schools = [
        {"School id": "SCH1", "School name": "Alpha Elementary", "School number": "1",
         "Low grade": "3", "High grade": "4", "Principal": "P One",
         "Principal email": "p1@tulsaschools-replica.org", "School address": "1 A St",
         "School city": "Tulsa", "School state": "OK", "School zip": "74101",
         "School phone": "918-555-0001"},
        {"School id": "SCH2", "School name": "Beta Middle", "School number": "2",
         "Low grade": "6", "High grade": "7", "Principal": "P Two",
         "Principal email": "p2@tulsaschools-replica.org", "School address": "2 B St",
         "School city": "Tulsa", "School state": "OK", "School zip": "74102",
         "School phone": "918-555-0002"},
    ]
    _write_csv(directory / "schools.csv", schema.SCHOOLS.columns, schools)

    teachers = []
    for i in range(1, 6):
        teachers.append({
            "School id": "SCH1", "Teacher id": f"TCH{i}", "Teacher number": f"TCH{i}",
            "Teacher email": f"teacher{i}@tulsaschools-replica.org", "First name": f"T{i}",
            "Last name": "Teacher", "Title": "Teacher",
        })
    for i in range(6, 11):
        teachers.append({
            "School id": "SCH2", "Teacher id": f"TCH{i}", "Teacher number": f"TCH{i}",
            "Teacher email": f"teacher{i}@tulsaschools-replica.org", "First name": f"T{i}",
            "Last name": "Teacher", "Title": "Teacher",
        })
    _write_csv(directory / "teachers.csv", schema.TEACHERS.columns, teachers)

    staff = [{
        "School id": "SCH1", "Staff id": "STF1", "Staff email": "staff1@tulsaschools-replica.org",
        "First name": "S", "Last name": "One", "Department": "Office", "Title": "Registrar",
        "Role": "staff",
    }]
    _write_csv(directory / "staff.csv", schema.STAFF.columns, staff)

    sections = [
        {"School id": "SCH1", "Section id": "SEC1", "Teacher id": "TCH1", "Name": "Sec1",
         "Section number": "1", "Grade": "3", "Course name": "Math", "Course number": "M3",
         "Subject": "Math", "Term name": "Year"},
        {"School id": "SCH1", "Section id": "SEC2", "Teacher id": "TCH2", "Name": "Sec2",
         "Section number": "2", "Grade": "3", "Course name": "Math", "Course number": "M3",
         "Subject": "Math", "Term name": "Year"},
        {"School id": "SCH1", "Section id": "SEC3", "Teacher id": "TCH3", "Name": "Sec3",
         "Section number": "3", "Grade": "4", "Course name": "Math", "Course number": "M4",
         "Subject": "Math", "Term name": "Year"},
        {"School id": "SCH1", "Section id": "SEC4", "Teacher id": "TCH4", "Name": "Sec4",
         "Section number": "4", "Grade": "4", "Course name": "Math", "Course number": "M4",
         "Subject": "Math", "Term name": "Year"},
        {"School id": "SCH2", "Section id": "SEC5", "Teacher id": "TCH6", "Name": "Sec5",
         "Section number": "5", "Grade": "6", "Course name": "Science", "Course number": "S6",
         "Subject": "Science", "Term name": "Year"},
        {"School id": "SCH2", "Section id": "SEC6", "Teacher id": "TCH7", "Name": "Sec6",
         "Section number": "6", "Grade": "6", "Course name": "Science", "Course number": "S6",
         "Subject": "Science", "Term name": "Year"},
        {"School id": "SCH2", "Section id": "SEC7", "Teacher id": "TCH8", "Name": "Sec7",
         "Section number": "7", "Grade": "7", "Course name": "Science", "Course number": "S7",
         "Subject": "Science", "Term name": "Year"},
        {"School id": "SCH2", "Section id": "SEC8", "Teacher id": "TCH9", "Name": "Sec8",
         "Section number": "8", "Grade": "7", "Course name": "Science", "Course number": "S7",
         "Subject": "Science", "Term name": "Year"},
    ]
    _write_csv(directory / "sections.csv", schema.SECTIONS.columns, sections)

    students = []
    for i in range(1, 16):
        grade = "3" if i % 2 == 1 else "4"
        home_section = "SEC1" if grade == "3" else "SEC3"
        students.append({
            "School id": "SCH1", "Student id": f"STU{i}", "Student number": f"{1000 + i}",
            "Last name": f"Last{i}", "First name": f"First{i}", "Grade": grade,
            "Gender": "F" if i % 2 == 0 else "M", "DOB": "01/01/2015",
            "Student email": f"first{i}.last{i}@students.tulsaschools-replica.org",
            "_home_section": home_section,  # not a real column; stripped below
        })
    for i in range(16, 31):
        grade = "6" if i % 2 == 0 else "7"
        home_section = "SEC5" if grade == "6" else "SEC7"
        students.append({
            "School id": "SCH2", "Student id": f"STU{i}", "Student number": f"{1000 + i}",
            "Last name": f"Last{i}", "First name": f"First{i}", "Grade": grade,
            "Gender": "F" if i % 2 == 0 else "M", "DOB": "01/01/2013",
            "Student email": f"first{i}.last{i}@students.tulsaschools-replica.org",
            "_home_section": home_section,
        })

    # One enrollment per STUDENT, not per students.csv row -- enrollments.csv
    # has no contact dimension at all, so a two-guardian student must still be
    # enrolled exactly once.
    enrollments = [
        {"School id": s["School id"], "Section id": s["_home_section"], "Student id": s["Student id"]}
        for s in students
    ]
    for s in students:
        del s["_home_section"]

    # Expand each student into one row per contact (or a single blank-contact
    # row if they have none). Goes through schema.expand_contact_rows rather
    # than building rows here, so the fixture cannot drift away from the one
    # place the row-per-contact rule actually lives.
    counts = {} if not with_contacts else (
        _DEFAULT_CONTACT_COUNTS if contact_counts is None else contact_counts
    )
    student_rows: list[dict[str, str]] = []
    for s in students:
        student_rows.extend(
            schema.expand_contact_rows(s, _synthetic_contacts(s, counts.get(s["Student id"], 0)))
        )

    _write_csv(directory / "students.csv", schema.STUDENTS.columns, student_rows)
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)


@pytest.fixture()
def stack_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stack"
    _write_synthetic_stack(d)
    return d


@pytest.fixture()
def stack(stack_dir: Path) -> CsvStack:
    return CsvStack.load(stack_dir)


@pytest.fixture()
def content() -> FakeContent:
    return FakeContent()


def _full_week_plan() -> RunPlan:
    """All three buckets in one plan, purely to exercise every code path in
    a single ``select_changes`` call. Cadence itself never produces this
    combination (see test_cadence.py) -- this is a selection-only fixture.
    """

    import datetime

    return RunPlan(
        run_date=datetime.date(2026, 7, 28),
        buckets=(Bucket.SMALL_DAILY, Bucket.BIG_STUDENT, Bucket.BIG_TEACHER),
    )


def _tuesday_plan() -> RunPlan:
    import datetime

    return RunPlan(
        run_date=datetime.date(2026, 7, 28),
        buckets=(Bucket.SMALL_DAILY, Bucket.BIG_STUDENT),
    )


def _friday_plan() -> RunPlan:
    import datetime

    return RunPlan(
        run_date=datetime.date(2026, 7, 31),
        buckets=(Bucket.SMALL_DAILY, Bucket.BIG_TEACHER),
    )


def _monday_plan() -> RunPlan:
    """Small daily only -- no big bucket, so the only students.csv changes are
    the small-daily contact/student field edits."""

    import datetime

    return RunPlan(run_date=datetime.date(2026, 7, 27), buckets=(Bucket.SMALL_DAILY,))


# ---------------------------------------------------------------------------
# Fixture sanity: the synthetic stack really is row-per-contact.
# ---------------------------------------------------------------------------


def test_synthetic_stack_is_rows_on_students_csv_not_a_contacts_file(
    stack_dir: Path, stack: CsvStack
) -> None:
    """Guards the fixture itself. If this file ever regrows a contacts.csv,
    every contact assertion below would be testing a file Clever's SFTP
    ingest does not read (SFTP Instructions v2.1.1)."""

    assert not (stack_dir / "contacts.csv").exists()
    # 5 students x2 + 5 students x1 + 20 students x1 blank row = 35 rows.
    assert len(stack.students()) == 35
    assert len(stack.distinct_students()) == 30
    assert len(stack.contacts()) == 15
    assert len(stack.contacts_for_student("STU1")) == 2
    assert len(stack.contacts_for_student("STU6")) == 1
    # A contact-less student still has exactly one row -- dropping it would
    # delete the student.
    assert len(stack.student_rows_for("STU11")) == 1
    assert stack.contacts_for_student("STU11") == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_produces_identical_change_list(stack: CsvStack, content: FakeContent) -> None:
    plan = _full_week_plan()
    changes_a = select_changes(stack, plan, content, rng=random.Random(42))
    changes_b = select_changes(stack, plan, content, rng=random.Random(42))
    assert changes_a == changes_b
    assert len(changes_a) > 0


def test_different_seed_produces_different_targets(stack: CsvStack, content: FakeContent) -> None:
    plan = _full_week_plan()
    changes_a = select_changes(stack, plan, content, rng=random.Random(1))
    changes_b = select_changes(stack, plan, content, rng=random.Random(2))
    keys_a = [(c.filename, tuple(sorted(c.key.items()))) for c in changes_a]
    keys_b = [(c.filename, tuple(sorted(c.key.items()))) for c in changes_b]
    assert keys_a != keys_b


def test_weekend_plan_produces_no_changes(stack: CsvStack, content: FakeContent) -> None:
    import datetime

    skipped_plan = RunPlan(
        run_date=datetime.date(2026, 8, 1), buckets=(), skipped=True, reason="weekend",
    )
    assert select_changes(stack, skipped_plan, content, rng=random.Random(1)) == []


# ---------------------------------------------------------------------------
# Enrollment moves
# ---------------------------------------------------------------------------


def test_enrollment_moves_emit_sections_updated_never_users_updated(
    stack: CsvStack, content: FakeContent
) -> None:
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(7))
    enrollment_changes = [c for c in changes if c.filename == "enrollments.csv"]
    assert enrollment_changes, "expected at least one enrollment move"
    for c in enrollment_changes:
        assert c.expected_event is EventType.SECTIONS_UPDATED
        assert c.expected_event is not EventType.USERS_UPDATED
        assert c.event_subject is EventSubject.SECTION


def test_enrollment_moves_produce_matched_delete_create_pairs(
    stack: CsvStack, content: FakeContent
) -> None:
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(7))
    enrollment_changes = [c for c in changes if c.filename == "enrollments.csv"]

    by_student: dict[str, list] = {}
    for c in enrollment_changes:
        by_student.setdefault(c.key["Student id"], []).append(c)

    for student_id, pair in by_student.items():
        ops = sorted(c.operation for c in pair)
        assert ops == sorted([Operation.DELETE, Operation.CREATE]), student_id
        deleted = next(c for c in pair if c.operation is Operation.DELETE)
        created = next(c for c in pair if c.operation is Operation.CREATE)
        assert deleted.key["Section id"] != created.key["Section id"]


def test_student_never_moved_into_a_section_already_enrolled_in_or_across_schools(
    stack: CsvStack, content: FakeContent
) -> None:
    original_enrollment_by_student = {
        e["Student id"]: e["Section id"] for e in stack.enrollments()
    }
    section_school = {s["Section id"]: s["School id"] for s in stack.sections()}

    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(11))
    creates = [
        c for c in changes if c.filename == "enrollments.csv" and c.operation is Operation.CREATE
    ]
    assert creates
    for c in creates:
        student_id = c.key["Student id"]
        new_section = c.key["Section id"]
        old_section = original_enrollment_by_student[student_id]
        assert new_section != old_section
        assert section_school[new_section] == section_school[old_section]


# ---------------------------------------------------------------------------
# Contacts added / removed
# ---------------------------------------------------------------------------


def test_contact_removal_never_orphans_a_student(stack: CsvStack, content: FakeContent) -> None:
    original_counts: dict[str, int] = {}
    for c in stack.contacts():
        original_counts[c["Student id"]] = original_counts.get(c["Student id"], 0) + 1

    # Run many seeds to exercise the removal path broadly.
    for seed in range(30):
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        removed_by_student: dict[str, int] = {}
        for c in changes:
            if _is_contact_removal(c):
                # The Student id now lives in the key (half the students.csv
                # natural key); ``before`` carries only the contact columns.
                student_id = c.key["Student id"]
                removed_by_student[student_id] = removed_by_student.get(student_id, 0) + 1

        for student_id, removed in removed_by_student.items():
            remaining = original_counts.get(student_id, 0) - removed
            assert remaining >= 1, f"seed {seed} orphaned student {student_id}"

    # And explicitly: students with exactly one contact (STU6-STU10) must
    # never appear as a removal target under any of these seeds. Removing
    # their only contact would delete their only students.csv row, i.e. the
    # STUDENT, not just the guardian.
    single_contact_students = {f"STU{i}" for i in range(6, 11)}
    for seed in range(30):
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        for c in changes:
            if _is_contact_removal(c):
                assert c.key["Student id"] not in single_contact_students


def test_contact_removal_is_a_row_delete_that_keeps_the_student(
    stack_dir: Path, content: FakeContent
) -> None:
    """A removed guardian takes its row with it -- and nothing else.

    Because a contact IS a row, this is the change shape with the most
    dangerous failure mode in the whole engine: delete the wrong row and the
    student disappears from the district. Applied here (against a freshly
    loaded stack, since apply mutates) rather than merely inspected, so the
    assertion is about the resulting CSV, not the intent.
    """

    stack = CsvStack.load(stack_dir)
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(7))
    removals = [c for c in changes if _is_contact_removal(c)]
    assert removals, "expected at least one contact removal"

    before_rows = {
        sid: len(stack.student_rows_for(sid))
        for sid in {c.key["Student id"] for c in removals}
    }
    stack.apply(removals)

    for c in removals:
        student_id = c.key["Student id"]
        assert c.operation is Operation.DELETE
        rows = stack.student_rows_for(student_id)
        # The student survives, one row lighter, and the removed guardian's
        # sis id is gone from the file.
        assert rows, f"student {student_id} was deleted along with their guardian"
        assert len(rows) == before_rows[student_id] - len(
            [r for r in removals if r.key["Student id"] == student_id]
        )
        sis_ids = {r[schema.CONTACT_SIS_ID_COLUMN] for r in rows}
        assert c.key[schema.CONTACT_SIS_ID_COLUMN] not in sis_ids


def test_contacts_added_are_ai_generated_and_create_events(
    stack: CsvStack, content: FakeContent
) -> None:
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(3))
    added = [c for c in changes if _is_contact_add(c)]
    assert added
    for c in added:
        assert c.expected_event is EventType.USERS_CREATED
        assert c.event_subject is EventSubject.CONTACT
        assert c.expected_event_label == "users.created (Contacts)"
        assert c.ai_generated is True
        assert c.note
        # The CSV operation deliberately disagrees with the Clever-level event
        # for one of the two shapes: filling a contact-less student's blank
        # row is an UPDATE that still creates a guardian. Both shapes are
        # legal here; nothing else is.
        assert c.operation in (Operation.CREATE, Operation.UPDATE)
        assert c.after["Contact name"]
        assert _new_contact_sis_id(c).startswith("CON")


def test_contact_add_shapes_match_whether_the_student_already_had_guardians(
    stack: CsvStack, content: FakeContent
) -> None:
    """CREATE a new row for a student who already has contacts; fill the blank
    row in place for a student who has none.

    Getting this backwards is silently destructive in one direction: CREATEing
    a row for a contact-less student leaves their original blank row behind, so
    the student appears twice in students.csv.
    """

    seen_shapes = set()
    for seed in range(30):
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        for c in changes:
            if not _is_contact_add(c):
                continue
            student_id = c.key["Student id"]
            had_contacts = bool(stack.contacts_for_student(student_id))
            if had_contacts:
                assert c.operation is Operation.CREATE, (seed, student_id)
                # A new row must repeat the student's own columns verbatim.
                for col in schema.STUDENT_LEVEL_COLUMNS:
                    assert c.after[col] == stack.student_rows_for(student_id)[0][col]
                assert c.key[schema.CONTACT_SIS_ID_COLUMN]
            else:
                assert c.operation is Operation.UPDATE, (seed, student_id)
                # Keyed on the blank sis id, because that is what the row
                # still carries until this change lands.
                assert c.key[schema.CONTACT_SIS_ID_COLUMN] == ""
                assert c.before == schema.blank_contact_fields()
                assert c.after[schema.CONTACT_SIS_ID_COLUMN]
                # Must not restate (and so risk rewriting) student columns.
                assert not set(c.after) & set(schema.STUDENT_LEVEL_COLUMNS)
            seen_shapes.add(c.operation)

    assert seen_shapes == {Operation.CREATE, Operation.UPDATE}, (
        "both add shapes should be reachable in this fixture (STU1-STU10 have "
        "guardians, STU11-STU30 do not)"
    )


def test_filling_a_blank_row_does_not_duplicate_the_student(
    stack_dir: Path, content: FakeContent
) -> None:
    """Applied form of the shape rule above: a contact-less student who gains
    a guardian still occupies exactly one row afterwards."""

    stack = CsvStack.load(stack_dir)
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(3))
    adds = [c for c in changes if _is_contact_add(c)]
    fills = [c for c in adds if c.operation is Operation.UPDATE]
    assert fills, "expected at least one blank-row fill under this seed"

    stack.apply(adds)
    for c in fills:
        student_id = c.key["Student id"]
        rows = stack.student_rows_for(student_id)
        assert len(rows) == 1, f"student {student_id} was duplicated by a contact add"
        assert schema.row_carries_contact(rows[0])


def test_student_at_the_contact_cap_is_never_given_a_sixth_contact(
    tmp_path: Path, content: FakeContent
) -> None:
    """``schema.MAX_CONTACTS_PER_STUDENT`` is a hard SFTP ceiling ("using SFTP
    limits a student's number of contacts to 5 maximum"), so a student already
    at the cap must be skipped rather than pushed over it. The run simply adds
    one fewer guardian that day; it never truncates, and never emits a 6th row
    that Clever would reject.
    """

    d = tmp_path / "capped_stack"
    # STU1 sits exactly on the cap; STU2 has room for more; everyone else has
    # none, so there is always an alternative target and a skip is visible as
    # a skip rather than as "no adds were possible at all".
    _write_synthetic_stack(
        d, contact_counts={"STU1": schema.MAX_CONTACTS_PER_STUDENT, "STU2": 1}
    )
    stack = CsvStack.load(d)
    assert len(stack.contacts_for_student("STU1")) == schema.MAX_CONTACTS_PER_STUDENT

    total_adds = 0
    for seed in range(40):
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        adds = [c for c in changes if _is_contact_add(c)]
        total_adds += len(adds)
        for c in adds:
            assert c.key["Student id"] != "STU1", (
                f"seed {seed} would give STU1 a "
                f"{schema.MAX_CONTACTS_PER_STUDENT + 1}th contact"
            )
    assert total_adds, "fixture produced no contact adds at all; test is vacuous"

    # And the degenerate case: when EVERY student is at the cap there is
    # simply nothing to add. Not an error, not a truncation -- zero adds.
    full = tmp_path / "fully_capped_stack"
    _write_synthetic_stack(
        full,
        contact_counts={f"STU{i}": schema.MAX_CONTACTS_PER_STUDENT for i in range(1, 31)},
    )
    full_stack = CsvStack.load(full)
    for seed in range(10):
        changes = select_changes(full_stack, _tuesday_plan(), content, rng=random.Random(seed))
        assert [c for c in changes if _is_contact_add(c)] == []


# ---------------------------------------------------------------------------
# Contact identity stability (``Contact sis id``)
#
# This is the whole reason the engine mints a sis id per contact. Per Clever's
# docs, a contact WITH an sis id keeps its Clever id across name/email/phone
# changes; a contact WITHOUT one has its identity derived from name+email, so
# editing the email changes the identity key itself and the ingest reads it as
# delete-then-create rather than users.updated. An edit that quietly rewrote
# the sis id would therefore turn every "guardian updated their email" demo
# into a spurious users.deleted + users.created pair on the partner's feed.
# ---------------------------------------------------------------------------


def test_contact_sis_id_is_not_an_editable_field() -> None:
    assert schema.CONTACT_SIS_ID_COLUMN not in schema.STUDENTS.mutable
    assert schema.CONTACT_SIS_ID_COLUMN not in schema.CONTACT_MUTABLE_COLUMNS
    # It IS half the natural key, which is what lets an edit name one specific
    # guardian among a student's siblings.
    assert schema.CONTACT_SIS_ID_COLUMN in schema.STUDENTS.key


def test_editing_a_contact_email_leaves_its_contact_sis_id_unchanged(
    stack_dir: Path, content: FakeContent
) -> None:
    stack = CsvStack.load(stack_dir)

    # Static half: no contact field edit may carry the sis id in ``after`` at
    # all (nor claim it in ``before``, which would imply it is in play).
    edits_seen = 0
    for seed in range(40):
        changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(seed))
        for c in changes:
            if not _is_contact_field_edit(c):
                continue
            edits_seen += 1
            assert schema.CONTACT_SIS_ID_COLUMN not in c.after, (seed, c)
            assert schema.CONTACT_SIS_ID_COLUMN not in c.before, (seed, c)
            assert c.key[schema.CONTACT_SIS_ID_COLUMN], "an edit must name one contact"
    assert edits_seen, "no contact field edits were produced; test is vacuous"

    # Applied half: after an email edit lands, the SAME (Student id, Contact
    # sis id) key still resolves to a row, and that row's email is the new one.
    # That is exactly the property that makes the change a users.updated
    # rather than a delete-then-create pair.
    changes = select_changes(stack, _monday_plan(), content, rng=random.Random(5))
    email_edits = [
        c for c in changes if _is_contact_field_edit(c) and "Contact email" in c.after
    ]
    assert email_edits, "expected at least one contact email edit under this seed"
    sis_ids_before = {r[schema.CONTACT_SIS_ID_COLUMN] for r in stack.contacts()}

    stack.apply(email_edits)

    for c in email_edits:
        key = (c.key["Student id"], c.key[schema.CONTACT_SIS_ID_COLUMN])
        row = stack.get(schema.STUDENTS.filename, key)
        assert row is not None, f"contact {key} lost its identity on edit"
        assert row["Contact email"] == c.after["Contact email"]
        assert row[schema.CONTACT_SIS_ID_COLUMN] == c.key[schema.CONTACT_SIS_ID_COLUMN]
    # No sis id anywhere in the district was invented, retired, or reshuffled.
    assert {r[schema.CONTACT_SIS_ID_COLUMN] for r in stack.contacts()} == sis_ids_before


# ---------------------------------------------------------------------------
# Student-level columns must agree across a student's rows
# ---------------------------------------------------------------------------


def test_student_level_edit_lands_on_every_row_of_a_multi_contact_student(
    stack_dir: Path, content: FakeContent
) -> None:
    """A student with N guardians occupies N rows carrying identical
    student-level columns. A ``Middle name`` edit that landed on only one of
    them would leave that student presenting two different values for the same
    field in a single file -- an ambiguous record no SIS export would ever
    produce. Selection targets one row and ``CsvStack.apply`` fans the
    student-level half out to the siblings; this asserts the end state.
    """

    multi_row_students_exercised = 0
    for seed in range(20):
        # Fresh stack per seed: apply mutates, and a stack carrying seed N's
        # edits would make seed N+1's ``before`` values wrong.
        stack = CsvStack.load(stack_dir)
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        student_edits = [c for c in changes if _is_student_field_edit(c)]
        assert student_edits, seed
        stack.apply(student_edits)

        for c in student_edits:
            student_id = c.key["Student id"]
            rows = stack.student_rows_for(student_id)
            if len(rows) > 1:
                multi_row_students_exercised += 1
            for field, value in c.after.items():
                assert all(r[field] == value for r in rows), (seed, student_id, field)

        # Nothing anywhere in the file may end up with siblings that disagree
        # on a student-level column, edited this run or not.
        for sid in {r["Student id"] for r in stack.students()}:
            rows = stack.student_rows_for(sid)
            for col in schema.STUDENT_LEVEL_COLUMNS:
                assert len({r[col] for r in rows}) == 1, (seed, sid, col)

    assert multi_row_students_exercised, (
        "no multi-contact student was ever edited, so the fan-out path was "
        "never exercised"
    )


def test_contact_level_columns_are_not_fanned_out_to_sibling_rows(
    stack_dir: Path, content: FakeContent
) -> None:
    """The mirror image of the test above, and the reason fan-out is column-
    aware rather than row-wide: contact columns are precisely what distinguishes
    one of a student's rows from another. Copying an email edit across siblings
    would give a student two identical guardians.
    """

    stack = CsvStack.load(stack_dir)

    # Find one edit to a guardian whose student has at least one OTHER
    # guardian that this run did not also edit -- both of a two-contact
    # student's rows can legitimately be picked in the same run (there are 15
    # contacts and ``SMALL_DAILY_CONTACT_FIELD_EDITS`` is 6), and a sibling
    # that was itself an edit target proves nothing about leakage.
    target = None
    for seed in range(30):
        edits = [
            c
            for c in select_changes(stack, _monday_plan(), content, rng=random.Random(seed))
            if _is_contact_field_edit(c)
        ]
        edited_keys = {(c.key["Student id"], c.key[schema.CONTACT_SIS_ID_COLUMN]) for c in edits}
        for c in edits:
            student_id = c.key["Student id"]
            untouched = [
                r
                for r in stack.student_rows_for(student_id)
                if (student_id, r[schema.CONTACT_SIS_ID_COLUMN]) not in edited_keys
            ]
            if untouched:
                target = c
                break
        if target is not None:
            break

    assert target is not None, (
        "no seed produced an edit to one guardian of a multi-guardian student"
    )
    student_id = target.key["Student id"]
    edited_sis_id = target.key[schema.CONTACT_SIS_ID_COLUMN]

    # Snapshot the sibling rows' contact halves BEFORE applying, so the
    # assertion is "untouched", not the much weaker "differs from the new
    # value" (which a sibling would satisfy by accident).
    before_siblings = {
        r[schema.CONTACT_SIS_ID_COLUMN]: schema.contact_fields(r)
        for r in stack.student_rows_for(student_id)
        if r[schema.CONTACT_SIS_ID_COLUMN] != edited_sis_id
    }
    assert before_siblings

    stack.apply([target])  # this ONE edit, so nothing else can explain a diff

    for sibling in stack.student_rows_for(student_id):
        sis_id = sibling[schema.CONTACT_SIS_ID_COLUMN]
        if sis_id == edited_sis_id:
            continue
        assert schema.contact_fields(sibling) == before_siblings[sis_id], (
            f"contact columns leaked from {edited_sis_id} onto sibling {sis_id}"
        )

    # ...while the edit itself did land on the row it named.
    edited = stack.get(schema.STUDENTS.filename, (student_id, edited_sis_id))
    assert edited is not None
    for field, value in target.after.items():
        assert edited[field] == value


# ---------------------------------------------------------------------------
# Selection draws students, not rows
# ---------------------------------------------------------------------------


def test_student_selection_is_unbiased_by_contact_count(
    tmp_path: Path, content: FakeContent
) -> None:
    """A student with 3 guardians must not be 3x more likely to be picked.

    ``stack.students()`` is one row PER CONTACT, so sampling it weights every
    student by their guardian count -- students.csv would drift lopsidedly
    toward the households that happen to have the most contacts, which is a
    sampling bias with no randomization justification behind it.
    ``distinct_students()`` is the unbiased pool.
    """

    d = tmp_path / "lopsided_stack"
    _write_synthetic_stack(d, contact_counts={"STU1": 3, "STU2": 1})
    stack = CsvStack.load(d)

    # (1) Structural: the pool is one entry per student, whatever the rows say.
    assert len(stack.students()) == 32  # 3 + 1 + 28 blank-contact rows
    pool_ids = [s["Student id"] for s in stack.distinct_students()]
    assert len(pool_ids) == len(set(pool_ids)) == 30
    assert pool_ids.count("STU1") == 1

    # (2) Behavioural: the student-edit pool really is ``distinct_students()``.
    # Narrowing that method narrows the targets; if selection were reading raw
    # rows instead, STU3+ would still show up here.
    real_pool = stack.distinct_students()
    stack.distinct_students = lambda: [  # type: ignore[method-assign]
        s for s in real_pool if s["Student id"] in {"STU1", "STU20"}
    ]
    try:
        for seed in range(20):
            changes = select_changes(stack, _monday_plan(), content, rng=random.Random(seed))
            targets = {c.key["Student id"] for c in changes if _is_student_field_edit(c)}
            assert targets, seed
            assert targets <= {"STU1", "STU20"}, (seed, targets)
    finally:
        del stack.distinct_students  # type: ignore[attr-defined]

    # (3) Frequency: with the real pool restored, STU1 (3 rows) is picked at
    # roughly the same rate as anybody else. Seeds are a fixed range, so this
    # is deterministic, not a flaky statistical test -- the bound is set well
    # below the ~3x share a row-weighted pool would produce and well above
    # ordinary sampling noise around the 1-in-30 fair share.
    picks: Counter[str] = Counter()
    for seed in range(200):
        changes = select_changes(stack, _monday_plan(), content, rng=random.Random(seed))
        for c in changes:
            if _is_student_field_edit(c):
                picks[c.key["Student id"]] += 1
    total = sum(picks.values())
    fair_share = total / len(pool_ids)
    row_weighted_share = total * 3 / len(stack.students())
    assert picks["STU1"] < 1.7 * fair_share, (picks["STU1"], fair_share)
    assert picks["STU1"] < 0.75 * row_weighted_share, (picks["STU1"], row_weighted_share)


# ---------------------------------------------------------------------------
# Co-teacher / section reassignment
# ---------------------------------------------------------------------------


def test_coteacher_assignment_only_uses_same_school_teacher(
    stack: CsvStack, content: FakeContent
) -> None:
    section_school = {s["Section id"]: s["School id"] for s in stack.sections()}
    teacher_school = {t["Teacher id"]: t["School id"] for t in stack.teachers()}

    changes = select_changes(stack, _friday_plan(), content, rng=random.Random(5))
    coteacher_changes = [
        c
        for c in changes
        if c.filename == "sections.csv" and "Teacher 2 id" in c.after
    ]
    assert coteacher_changes
    for c in coteacher_changes:
        new_teacher = c.after["Teacher 2 id"]
        if new_teacher:  # empty string means "cleared", not a new assignment
            section_id = c.key["Section id"]
            assert teacher_school[new_teacher] == section_school[section_id]


def test_section_reassignment_only_uses_same_school_teacher(
    stack: CsvStack, content: FakeContent
) -> None:
    section_school = {s["Section id"]: s["School id"] for s in stack.sections()}
    teacher_school = {t["Teacher id"]: t["School id"] for t in stack.teachers()}

    changes = select_changes(stack, _friday_plan(), content, rng=random.Random(5))
    reassignments = [
        c for c in changes if c.filename == "sections.csv" and "Teacher id" in c.after
    ]
    assert reassignments
    for c in reassignments:
        new_teacher = c.after["Teacher id"]
        section_id = c.key["Section id"]
        assert teacher_school[new_teacher] == section_school[section_id]


# ---------------------------------------------------------------------------
# Teacher attrition (paired with the weekly new-teacher add)
# ---------------------------------------------------------------------------


def test_teacher_removal_is_paired_one_for_one_with_the_addition(
    stack: CsvStack, content: FakeContent
) -> None:
    changes = select_changes(stack, _friday_plan(), content, rng=random.Random(5))
    creates = [
        c for c in changes if c.filename == "teachers.csv" and c.operation is Operation.CREATE
    ]
    deletes = [
        c for c in changes if c.filename == "teachers.csv" and c.operation is Operation.DELETE
    ]
    assert len(creates) == 1
    assert len(deletes) == 1


def test_teacher_removal_always_comes_from_a_different_school_than_the_addition(
    stack: CsvStack, content: FakeContent
) -> None:
    for seed in range(30):
        changes = select_changes(stack, _friday_plan(), content, rng=random.Random(seed))
        created = next(
            c
            for c in changes
            if c.filename == "teachers.csv" and c.operation is Operation.CREATE
        )
        deleted = next(
            (
                c
                for c in changes
                if c.filename == "teachers.csv" and c.operation is Operation.DELETE
            ),
            None,
        )
        if deleted is None:
            continue  # Graceful skip (no safe candidate) is allowed; nothing to check.
        assert deleted.before["School id"] != created.after["School id"], seed


def test_teacher_removal_never_leaves_a_section_pointing_at_a_deleted_teacher(
    stack_dir: Path, stack: CsvStack, content: FakeContent
) -> None:
    """The exact class of bug the contacts.csv rework fixed once already
    (README "KNOWN BLOCKER") -- a removed teacher must never stay referenced
    as a section's primary or co-teacher."""

    for seed in range(30):
        changes = select_changes(stack, _friday_plan(), content, rng=random.Random(seed))
        deleted = next(
            (
                c
                for c in changes
                if c.filename == "teachers.csv" and c.operation is Operation.DELETE
            ),
            None,
        )
        if deleted is None:
            continue
        deleted_id = deleted.key["Teacher id"]

        applied = CsvStack.load(stack_dir)
        applied.apply(changes)
        remaining_ids = {t["Teacher id"] for t in applied.teachers()}
        assert deleted_id not in remaining_ids, seed
        for section in applied.sections():
            assert section.get("Teacher id") != deleted_id, (seed, section["Section id"])
            assert section.get("Teacher 2 id") != deleted_id, (seed, section["Section id"])


def test_teacher_removal_reassignments_only_use_same_school_teachers(
    stack: CsvStack, content: FakeContent
) -> None:
    section_school = {s["Section id"]: s["School id"] for s in stack.sections()}
    teacher_school = {t["Teacher id"]: t["School id"] for t in stack.teachers()}

    for seed in range(30):
        changes = select_changes(stack, _friday_plan(), content, rng=random.Random(seed))
        for c in changes:
            if c.filename != "sections.csv":
                continue
            section_id = c.key["Section id"]
            new_primary = c.after.get("Teacher id")
            if new_primary:
                assert teacher_school[new_primary] == section_school[section_id], seed
            new_co = c.after.get("Teacher 2 id")
            if new_co:
                assert teacher_school[new_co] == section_school[section_id], seed


# ---------------------------------------------------------------------------
# ID minting
# ---------------------------------------------------------------------------


def test_new_ids_never_collide_with_existing_ones(stack: CsvStack, content: FakeContent) -> None:
    existing_contact_sis_ids = {c[schema.CONTACT_SIS_ID_COLUMN] for c in stack.contacts()}
    existing_teacher_ids = {t["Teacher id"] for t in stack.teachers()}

    changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(9))

    # A minted sis id arrives via the key (new row) or via ``after`` (blank
    # row filled in place) -- see ``_new_contact_sis_id``.
    new_contact_sis_ids = [_new_contact_sis_id(c) for c in changes if _is_contact_add(c)]
    new_teacher_ids = [
        c.key["Teacher id"]
        for c in changes
        if c.filename == "teachers.csv" and c.operation is Operation.CREATE
    ]

    assert new_contact_sis_ids, "expected at least one new contact"
    assert new_teacher_ids, "expected at least one new teacher"
    for cid in new_contact_sis_ids:
        assert cid not in existing_contact_sis_ids
        assert cid.startswith("CON")
    for tid in new_teacher_ids:
        assert tid not in existing_teacher_ids
        assert tid.startswith("TCH9")
    # No collisions among ids minted within the same run, either.
    assert len(new_contact_sis_ids) == len(set(new_contact_sis_ids))
    assert len(new_teacher_ids) == len(set(new_teacher_ids))


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_zero_contacts_still_produces_valid_small_daily_run(
    tmp_path: Path, content: FakeContent
) -> None:
    d = tmp_path / "no_contacts_stack"
    _write_synthetic_stack(d, with_contacts=False)
    stack = CsvStack.load(d)
    assert stack.contacts() == []
    # Every student still has exactly one row; "no contacts" means blank
    # contact columns, never a missing student.
    assert len(stack.students()) == len(stack.distinct_students()) == 30

    plan = _monday_plan()
    changes = select_changes(stack, plan, content, rng=random.Random(4))

    assert changes  # student edits still happen
    # Nothing contact-related can be produced -- there are no guardians to
    # edit yet, which is the state of David's export before seeding.
    assert all(c.event_subject is not EventSubject.CONTACT for c in changes)
    assert any(
        c.expected_event is EventType.USERS_UPDATED and c.event_subject is EventSubject.STUDENT
        for c in changes
    )


# ---------------------------------------------------------------------------
# No record touched twice in one run
# ---------------------------------------------------------------------------


def test_no_record_touched_twice_in_one_run(stack: CsvStack, content: FakeContent) -> None:
    for seed in range(20):
        changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(seed))

        # Small daily contact field edits: each contact at most once. Keyed on
        # the sis id, which is what names one guardian among a student's rows.
        contact_edits = [
            c.key[schema.CONTACT_SIS_ID_COLUMN]
            for c in changes
            if _is_contact_field_edit(c)
        ]
        assert len(contact_edits) == len(set(contact_edits)), seed

        # Small daily student field edits: each student at most once. Filtered
        # by event_subject, not by filename+operation -- contact edits and
        # blank-row contact fills are also students.csv UPDATEs now.
        student_edits = [
            c.key["Student id"] for c in changes if _is_student_field_edit(c)
        ]
        assert len(student_edits) == len(set(student_edits)), seed

        # Enrollment moves: each student moved at most once.
        moved_students = [
            c.key["Student id"]
            for c in changes
            if c.filename == "enrollments.csv" and c.operation is Operation.DELETE
        ]
        assert len(moved_students) == len(set(moved_students)), seed

        # Contacts removed: each contact removed at most once, and no
        # contact is both field-edited and removed in the same run.
        removed_contacts = [
            c.key[schema.CONTACT_SIS_ID_COLUMN] for c in changes if _is_contact_removal(c)
        ]
        assert len(removed_contacts) == len(set(removed_contacts)), seed
        assert not (set(contact_edits) & set(removed_contacts)), seed

        # Co-teacher edits: each section at most once for that field.
        coteacher_sections = [
            c.key["Section id"]
            for c in changes
            if c.filename == "sections.csv" and "Teacher 2 id" in c.after
        ]
        assert len(coteacher_sections) == len(set(coteacher_sections)), seed

        # Section reassignments: each section at most once for that field.
        reassigned_sections = [
            c.key["Section id"]
            for c in changes
            if c.filename == "sections.csv" and "Teacher id" in c.after
        ]
        assert len(reassigned_sections) == len(set(reassigned_sections)), seed

        # Newly minted ids: no duplicate contact sis ids or teacher ids
        # created, and no student is given two new guardians in one run
        # (which for a contact-less student would mean two changes racing for
        # the same blank row).
        new_contact_sis_ids = [_new_contact_sis_id(c) for c in changes if _is_contact_add(c)]
        assert len(new_contact_sis_ids) == len(set(new_contact_sis_ids)), seed
        students_given_contacts = [c.key["Student id"] for c in changes if _is_contact_add(c)]
        assert len(students_given_contacts) == len(set(students_given_contacts)), seed


def test_every_change_has_a_note(stack: CsvStack, content: FakeContent) -> None:
    changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(13))
    assert changes
    for c in changes:
        assert c.note and c.note.strip()


# ---------------------------------------------------------------------------
# Fix 1: no UPDATE change may be a no-op (after == before for every field).
#
# The audit that found this bug measured 486/486 (100%) of contact "Contact
# email" edits as no-op writes -- ``guardian_email`` is a pure function of
# (name, student last name), so re-deriving it from the exact same inputs
# during a small-daily "edit" just recomputed the identical address every
# time. Clever's CSV diff sees nothing in that case, so no users.updated
# (Contacts) event is ever emitted no matter how many times selection.py
# "changes" that field. This test is the one the audit specifically called out
# as missing -- its absence is what let the bug through in the first place.
# ---------------------------------------------------------------------------


def test_every_update_change_actually_changes_a_value(
    stack: CsvStack, content: FakeContent
) -> None:
    for seed in range(60):
        changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(seed))
        for c in changes:
            if c.operation is not Operation.UPDATE:
                continue
            for field, before_value in c.before.items():
                after_value = c.after.get(field, before_value)
                assert after_value != before_value, (seed, c.filename, c.key, field, c)


def test_contact_email_edit_actually_changes_the_email_even_when_derivation_would_repeat(
    stack: CsvStack,
) -> None:
    """Reproduces the exact audit scenario: a contact's stored ``Contact
    email`` already equals what the (pure, deterministic) generator would
    derive for attempt=0 -- e.g. because a previous edit (or the seeding step)
    already applied that exact convention. The small-daily "email tweak" must
    still land on a genuinely different address (via the ``attempt`` reroll)
    rather than silently re-writing the same one, or (per Fix 1(b)) skip
    the field entirely rather than emit a no-op UPDATE.
    """

    # Force every contact's stored email to exactly what attempt=0 would
    # derive, so whichever contact gets picked for a small-daily email edit
    # is guaranteed to hit the "derivation repeats the current value" case
    # the audit found -- without this, the test would pass trivially (any
    # freshly-seeded contact's email already differs from
    # "stuck@example.com", so the bug this test targets would never
    # actually be exercised). Mutating the rows in place is fine:
    # ``stack.contacts()`` hands back the live students.csv rows.
    for c in stack.contacts():
        c["Contact email"] = "stuck@example.com"

    class _StuckContent(FakeContent):
        """Derives the SAME value for attempt=0 no matter which contact/
        student is passed -- the ``attempt`` reroll is the only way this
        generator can ever produce a genuine change.
        """

        def guardian_email(
            self, guardian_name: str, student_last_name: str, *, attempt: int = 0
        ) -> str:
            if attempt == 0:
                return "stuck@example.com"
            return f"stuck{attempt}@example.com"

    stuck = _StuckContent()
    saw_an_edit = False
    for seed in range(20):
        changes = select_changes(stack, _tuesday_plan(), stuck, rng=random.Random(seed))
        email_edits = [
            c
            for c in changes
            if _is_contact_field_edit(c) and "Contact email" in c.after
        ]
        for c in email_edits:
            saw_an_edit = True
            assert c.before["Contact email"] == "stuck@example.com"
            assert c.after["Contact email"] != c.before["Contact email"], (seed, c)
    assert saw_an_edit, "no contact email edits were exercised; test is vacuous"


# ---------------------------------------------------------------------------
# Fix 4: enrollment-move targets are chosen with ``rng.choice``, not always
# the first candidate in list order -- otherwise every move within a given
# school+grade funnels into the SAME section every run.
# ---------------------------------------------------------------------------


class _FakeStackForMoveTarget:
    """Minimal stand-in exposing only what ``_find_move_target`` calls."""

    def __init__(self, sections: list[dict], enrollments: list[dict]) -> None:
        self._sections = sections
        self._enrollments = enrollments

    def enrollments_for_student(self, student_id: str) -> list[dict]:
        return [e for e in self._enrollments if e["Student id"] == student_id]

    def sections_in_school(self, school_id: str) -> list[dict]:
        return [s for s in self._sections if s["School id"] == school_id]


def test_find_move_target_varies_which_same_grade_section_it_picks() -> None:
    from drift_engine.selection import _find_move_target

    sections = [
        {"Section id": "SEC1", "School id": "SCH1", "Grade": "3"},
        {"Section id": "SEC2", "School id": "SCH1", "Grade": "3"},
        {"Section id": "SEC3", "School id": "SCH1", "Grade": "3"},
        {"Section id": "SEC4", "School id": "SCH1", "Grade": "4"},
    ]
    enrollments = [{"Student id": "STU1", "Section id": "SEC1"}]
    fake_stack = _FakeStackForMoveTarget(sections, enrollments)
    student = {"Student id": "STU1", "School id": "SCH1", "Grade": "3"}

    picks = set()
    for seed in range(50):
        target = _find_move_target(fake_stack, student, "SEC1", random.Random(seed))
        assert target is not None
        picks.add(target["Section id"])

    # SEC1 is the student's current section (excluded); SEC4 is a different
    # grade (excluded by the same-grade preference). Only SEC2/SEC3 are
    # eligible -- both must show up across enough seeds, not just whichever
    # sorts first.
    assert picks == {"SEC2", "SEC3"}


# ---------------------------------------------------------------------------
# Correction regression: EventType must only ever contain wire event names
# Clever actually emits (project brief §3 wrongly assumed contacts/teachers
# had their own distinct event types -- see models.EventType's docstring).
# ---------------------------------------------------------------------------


def test_event_type_enum_contains_only_real_clever_wire_events() -> None:
    """No ``EventType`` member may be a name Clever does not emit on the
    wire. Clever's Events API (v3.x) only ever emits ``users.*`` and
    ``sections.*`` -- contacts/students/teachers/staff are all ``users``
    objects, distinguished by role, not by event name. This test exists so a
    future contributor cannot silently reintroduce a ``contacts.*``/
    ``teachers.*`` member the way the original project brief did."""

    allowed_prefixes = ("users.", "sections.")
    for member in EventType:
        assert member.value.startswith(allowed_prefixes), (
            f"EventType.{member.name} = {member.value!r} is not a real Clever "
            "wire event -- Clever only emits users.*/sections.* events."
        )

    # And explicitly: the wrong, brief-inherited event names must never come
    # back as members of this enum.
    banned_values = {
        "contacts.created", "contacts.updated", "contacts.deleted", "teachers.created",
    }
    actual_values = {member.value for member in EventType}
    assert not (actual_values & banned_values)


def test_expected_event_label_matches_clevers_event_ordering_doc_phrasing() -> None:
    """``Change.expected_event_label`` must read exactly like Clever's own
    event-ordering documentation, e.g. 'users.updated (Contacts)' -- verified
    against https://dev.clever.com/docs/events-api."""

    from drift_engine.models import Change, EventSubject, Operation

    # A contact edit is a students.csv change keyed on (Student id, Contact
    # sis id) -- contacts have no file of their own. The label must still read
    # "(Contacts)", which is the entire point of ``event_subject``: the file a
    # change lands in no longer identifies the role it concerns.
    change = Change(
        filename="students.csv",
        operation=Operation.UPDATE,
        key={"Student id": "STU100001", schema.CONTACT_SIS_ID_COLUMN: "CON000001"},
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_UPDATED,
        event_subject=EventSubject.CONTACT,
        before={"Contact email": "old@example.com"},
        after={"Contact email": "new@example.com"},
    )
    assert change.expected_event_label == "users.updated (Contacts)"

    created = Change(
        filename="students.csv",
        operation=Operation.CREATE,
        key={"Student id": "STU100001", schema.CONTACT_SIS_ID_COLUMN: ""},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.USERS_CREATED,
        event_subject=EventSubject.STUDENT,
        after={"First name": "New"},
    )
    assert created.expected_event_label == "users.created (Students)"
