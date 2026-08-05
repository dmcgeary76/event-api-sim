"""Tests for drift_engine.seed.

Uses a small synthetic CsvStack (1 school, a handful of students, some of
which already have contacts) written into ``tmp_path``, plus a fake content
generator standing in for the real AI content generator -- seed.py is coded
against the same informal interface ``selection.py`` uses (``guardian_name``,
``guardian_email``, ``phone``), never against a concrete implementation.

CONTACTS ARE ROWS ON students.csv (corrected 2026-08-05)
--------------------------------------------------------
An earlier version of this file wrote a standalone ``contacts.csv``; no such
file exists in Clever's SFTP spec (SFTP Instructions v2.1.1). Guardians are
rows on students.csv sharing one ``Student id``, so seeding is two different
CSV operations rather than one, and that split is what most of the assertions
below are really about:

  * A student's FIRST guardian fills the contact-less row they already
    occupy -- an ``Operation.UPDATE`` keyed on a blank ``Contact sis id``.
    Creating a row instead would leave the blank one behind and the student
    would appear twice in students.csv.
  * Each SUBSEQUENT guardian is a new row for the same ``Student id`` -- an
    ``Operation.CREATE`` repeating that student's student-level columns.

Both predict ``users.created`` (Contacts), whatever the CSV operation was, so
tests filter on the event rather than on the operation wherever they mean
"a guardian was created".

There is also no ``Sequence`` column any more (the SFTP contact spec has
none). Guardian order is carried by row order and visible in the
``Contact sis id`` (``SEED<student id>-<n>``), which is what the ordering
assertions below check instead.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from drift_engine import schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import EventSubject, EventType, Operation
from drift_engine.seed import estimate_seed_volume, seed_contacts

CRLF = "\r\n"


class FakeContent:
    def guardian_name(self, student_last_name: str) -> str:
        return f"Guardian {student_last_name}"

    def guardian_email(self, guardian_name: str, student_last_name: str) -> str:
        local = guardian_name.lower().replace(" ", ".")
        return f"{local}@tulsaschools-replica.org"

    def phone(self) -> str:
        return "9185550100"


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in columns))
    path.write_text((CRLF.join(lines) + CRLF), encoding="utf-8")


def _pre_existing_contact(student_id: str) -> dict[str, str]:
    """One already-seeded guardian, as the contact half of a students.csv row.

    The ``Contact sis id`` is ``PRE-<student id>`` -- deliberately neither the
    ``SEED...`` shape seed.py mints nor the ``CON######`` shape
    selection.py's ``_IdMinter`` mints, so a test can tell at a glance that a
    row predates this run.
    """

    return {
        "Contact relationship": "Mother",
        "Contact type": "Parent",
        "Contact name": f"Existing Guardian {student_id}",
        "Contact phone": "9185550000",
        "Contact phone type": "Mobile",
        "Contact email": f"existing.{student_id.lower()}@tulsaschools-replica.org",
        schema.CONTACT_SIS_ID_COLUMN: f"PRE-{student_id}",
    }


def _write_synthetic_stack(
    directory: Path,
    *,
    student_grades: dict[str, str],
    students_with_contacts: set[str] = frozenset(),
) -> None:
    """Writes a minimal stack with one school and students keyed by id.

    ``student_grades`` maps student id -> grade (e.g. {"STU1": "3"}).
    ``students_with_contacts`` names students that should already have a
    single pre-existing contact, to exercise the idempotency / skip path.
    Those students get one POPULATED students.csv row; everyone else gets one
    row with the seven contact columns blank, which is the state David's real
    export is in before seeding has ever run.
    """

    directory.mkdir(parents=True, exist_ok=True)

    schools = [
        {
            "School id": "SCH1", "School name": "Alpha Elementary", "School number": "1",
            "Low grade": "1", "High grade": "12", "Principal": "P One",
            "Principal email": "p1@tulsaschools-replica.org", "School address": "1 A St",
            "School city": "Tulsa", "School state": "OK", "School zip": "74101",
            "School phone": "9185550001",
        }
    ]
    _write_csv(directory / "schools.csv", schema.SCHOOLS.columns, schools)

    teachers = [
        {
            "School id": "SCH1", "Teacher id": "TCH1", "Teacher number": "TCH1",
            "Teacher email": "t1@tulsaschools-replica.org", "First name": "T1",
            "Last name": "Teacher", "Title": "Teacher",
        }
    ]
    _write_csv(directory / "teachers.csv", schema.TEACHERS.columns, teachers)

    staff = [
        {
            "School id": "SCH1", "Staff id": "STF1", "Staff email": "s1@tulsaschools-replica.org",
            "First name": "S1", "Last name": "Staff", "Department": "Office", "Title": "Registrar",
            "Role": "staff",
        }
    ]
    _write_csv(directory / "staff.csv", schema.STAFF.columns, staff)

    sections = [
        {
            "School id": "SCH1", "Section id": "SEC1", "Teacher id": "TCH1", "Name": "Homeroom",
            "Section number": "1", "Grade": "3", "Course name": "Homeroom", "Course number": "HR",
            "Subject": "Homeroom/advisory", "Term name": "Year",
        }
    ]
    _write_csv(directory / "sections.csv", schema.SECTIONS.columns, sections)

    students = []
    for student_id, grade in student_grades.items():
        students.append(
            {
                "School id": "SCH1", "Student id": student_id, "Student number": student_id,
                "Last name": f"Last{student_id}", "First name": f"First{student_id}", "Grade": grade,
                "Gender": "F", "DOB": "01/01/2015",
                "Student email": f"first{student_id}.last{student_id}@students.tulsaschools-replica.org",
            }
        )

    # Expand into one row per contact (or one blank-contact row) through
    # schema.expand_contact_rows -- the single place the row-per-contact rule
    # from SFTP Instructions v2.1.1 lives, so this fixture cannot drift away
    # from it.
    student_rows: list[dict[str, str]] = []
    for student in students:
        contacts = (
            [_pre_existing_contact(student["Student id"])]
            if student["Student id"] in students_with_contacts
            else []
        )
        student_rows.extend(schema.expand_contact_rows(student, contacts))
    _write_csv(directory / "students.csv", schema.STUDENTS.columns, student_rows)

    enrollments = [
        {"School id": "SCH1", "Section id": "SEC1", "Student id": sid} for sid in student_grades
    ]
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)


@pytest.fixture()
def content() -> FakeContent:
    return FakeContent()


def _make_stack(tmp_path: Path, **kwargs) -> CsvStack:
    d = tmp_path / "stack"
    _write_synthetic_stack(d, **kwargs)
    return CsvStack.load(d)


def _sis_id(change) -> str:
    """The ``Contact sis id`` a seeded guardian is given.

    Guardian 1 fills an existing row whose key still carries a BLANK sis id,
    so its new id is in ``after``; guardian 2+ is a new row, so its id is half
    the key. Both shapes mean the same thing -- a guardian object now exists.
    """

    if change.operation is Operation.CREATE:
        return change.key[schema.CONTACT_SIS_ID_COLUMN]
    return change.after[schema.CONTACT_SIS_ID_COLUMN]


# ---------------------------------------------------------------------------
# Basic seeding behaviour
# ---------------------------------------------------------------------------


def test_every_student_gets_one_or_two_contacts_with_correct_keys(
    tmp_path: Path, content: FakeContent
) -> None:
    grades = {f"STU{i}": ("3" if i % 2 == 0 else "9") for i in range(1, 21)}
    stack = _make_stack(tmp_path, student_grades=grades)

    changes = seed_contacts(stack, content, rng=random.Random(42))
    assert changes
    # Every change lands on students.csv -- contacts have no file of their own.
    assert all(c.filename == schema.STUDENTS.filename for c in changes)
    # The first guardian fills a row (UPDATE), later ones add rows (CREATE);
    # nothing here ever deletes.
    assert all(c.operation in (Operation.UPDATE, Operation.CREATE) for c in changes)
    # Seeded contacts are users.created (Contacts) on Clever's real Events
    # API, not a distinct contacts.created event -- see models.EventType. Note
    # this is true of the UPDATE shape too: the CSV operation and the
    # Clever-level event deliberately disagree there.
    assert all(c.expected_event is EventType.USERS_CREATED for c in changes)
    assert all(c.event_subject is EventSubject.CONTACT for c in changes)
    assert all(c.expected_event_label == "users.created (Contacts)" for c in changes)

    by_student: dict[str, list] = {}
    for c in changes:
        # Student id is half the students.csv natural key now, and is the only
        # place to read it for the UPDATE shape (which must not restate
        # student-level columns in ``after``).
        by_student.setdefault(c.key["Student id"], []).append(c)

    assert set(by_student) == set(grades)
    for student_id, student_changes in by_student.items():
        assert 1 <= len(student_changes) <= 2

        # Guardian order used to be a ``Sequence`` column; the SFTP contact
        # spec has no such column, so it is now carried by row order and
        # visible in the minted sis id.
        sis_ids = sorted(_sis_id(c) for c in student_changes)
        assert sis_ids in (
            [f"SEED{student_id}-1"],
            [f"SEED{student_id}-1", f"SEED{student_id}-2"],
        )

        # Exactly one blank-row fill per student, and it is guardian 1. Two
        # fills would race for the same row; a CREATE for guardian 1 would
        # leave the student's blank row behind and duplicate them.
        fills = [c for c in student_changes if c.operation is Operation.UPDATE]
        assert len(fills) == 1
        assert fills[0].key[schema.CONTACT_SIS_ID_COLUMN] == ""
        assert fills[0].before == schema.blank_contact_fields()
        assert _sis_id(fills[0]) == f"SEED{student_id}-1"
        # A fill must not restate student-level columns -- the row already
        # carries them, and rewriting them is how a student ends up with
        # blanked-out real SIS values.
        assert not set(fills[0].after) & set(schema.STUDENT_LEVEL_COLUMNS)

        # Additional guardians are new rows that repeat the student's own
        # columns verbatim, or that row would present blank student values.
        for c in (c for c in student_changes if c.operation is Operation.CREATE):
            assert c.after["School id"] == "SCH1"
            assert c.after["Student id"] == student_id
            assert c.key["Student id"] == student_id

        # No duplicate relationship within one student's contacts.
        relationships = [c.after["Contact relationship"] for c in student_changes]
        assert len(relationships) == len(set(relationships))

    # Applied, the file agrees: every student still resolves, every row of a
    # student carries identical student-level columns, and no blank-contact
    # row survives anywhere.
    stack.apply(changes)
    for student_id in grades:
        rows = stack.student_rows_for(student_id)
        assert 1 <= len(rows) <= 2
        assert len(rows) == len(by_student[student_id])
        assert all(schema.row_carries_contact(r) for r in rows)
        assert all(r["School id"] == "SCH1" for r in rows)
        for col in schema.STUDENT_LEVEL_COLUMNS:
            assert len({r[col] for r in rows}) == 1


def test_younger_grades_skew_toward_two_guardians(tmp_path: Path, content: FakeContent) -> None:
    # All young-grade students, large N -- expect a clear majority with 2.
    grades = {f"STU{i}": "1" for i in range(1, 201)}
    stack = _make_stack(tmp_path, student_grades=grades)

    changes = seed_contacts(stack, content, rng=random.Random(7))
    by_student: dict[str, int] = {}
    for c in changes:
        sid = c.key["Student id"]
        by_student[sid] = by_student.get(sid, 0) + 1

    two_count = sum(1 for n in by_student.values() if n == 2)
    assert two_count > len(by_student) * 0.5


# ---------------------------------------------------------------------------
# Row accounting: filling vs adding
#
# This is the correction that landed on 2026-08-05, and the failure mode is
# silent: seed a contact-less student with a CREATE and their original blank
# row survives, so students.csv carries the same student twice -- one row with
# a guardian, one without. Clever would see an ambiguous record and David
# would see a district that grew a phantom student for every seeded one.
# ---------------------------------------------------------------------------


def test_one_guardian_fills_the_students_existing_row_row_count_stays_one(
    tmp_path: Path, content: FakeContent
) -> None:
    stack = _make_stack(tmp_path, student_grades={"STU1": "3"})
    assert len(stack.student_rows_for("STU1")) == 1
    assert stack.contacts_for_student("STU1") == []

    # (1, 1) forces exactly one guardian, so the "fill, don't add" path is the
    # only path this call can take -- no reliance on the 1-vs-2 weighting.
    changes = seed_contacts(
        stack, content, rng=random.Random(1), guardians_per_student=(1, 1)
    )
    assert [c.operation for c in changes] == [Operation.UPDATE]

    stack.apply(changes)

    # 1 row -> 1 row: the guardian moved INTO the row the student already had.
    assert len(stack.student_rows_for("STU1")) == 1
    assert len(stack.contacts_for_student("STU1")) == 1
    row = stack.student_rows_for("STU1")[0]
    assert row[schema.CONTACT_SIS_ID_COLUMN] == "SEEDSTU1-1"
    assert row["Contact name"] == content.guardian_name("LastSTU1")
    # And the student's own columns survived the fill untouched.
    assert row["Student email"] == "firstSTU1.lastSTU1@students.tulsaschools-replica.org"
    assert row["School id"] == "SCH1"


def test_two_guardians_fill_then_add_row_count_goes_one_to_two(
    tmp_path: Path, content: FakeContent
) -> None:
    stack = _make_stack(tmp_path, student_grades={"STU1": "3"})
    assert len(stack.student_rows_for("STU1")) == 1

    # (2, 2) forces exactly two guardians: one fill plus one added row.
    changes = seed_contacts(
        stack, content, rng=random.Random(1), guardians_per_student=(2, 2)
    )
    assert [c.operation for c in changes] == [Operation.UPDATE, Operation.CREATE]

    stack.apply(changes)

    # 1 row -> 2 rows, one per guardian, and NOT 1 -> 3 (which is what a
    # CREATE-only implementation would produce: two new rows plus the
    # original blank one).
    rows = stack.student_rows_for("STU1")
    assert len(rows) == 2
    assert len(stack.contacts_for_student("STU1")) == 2
    assert all(schema.row_carries_contact(r) for r in rows)
    assert [r[schema.CONTACT_SIS_ID_COLUMN] for r in rows] == ["SEEDSTU1-1", "SEEDSTU1-2"]
    # Both rows describe the same student, so their student halves must be
    # byte-identical.
    assert schema.student_fields(rows[0]) == schema.student_fields(rows[1])
    # ``counts`` must still see ONE student (it counts distinct Student id) and
    # two contacts -- this is what keeps a district-wide seed from tripping
    # safety.assert_scale_sane on row growth alone.
    counts = stack.counts()
    assert counts["students"] == 1
    assert counts[schema.CONTACTS_RECORD_TYPE] == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_seed_apply_seed_again_produces_zero_new_changes(
    tmp_path: Path, content: FakeContent
) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 11)}
    stack = _make_stack(tmp_path, student_grades=grades)

    first_changes = seed_contacts(stack, content, rng=random.Random(1))
    assert first_changes
    stack.apply(first_changes)

    second_changes = seed_contacts(stack, content, rng=random.Random(2))
    assert second_changes == []


def test_students_with_existing_contacts_are_skipped(tmp_path: Path, content: FakeContent) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 6)}
    stack = _make_stack(tmp_path, student_grades=grades, students_with_contacts={"STU1", "STU2"})

    changes = seed_contacts(stack, content, rng=random.Random(3))
    touched_students = {c.key["Student id"] for c in changes}
    assert "STU1" not in touched_students
    assert "STU2" not in touched_students
    assert touched_students == {"STU3", "STU4", "STU5"}
    # The skipped students keep the guardian they already had, untouched.
    assert [c[schema.CONTACT_SIS_ID_COLUMN] for c in stack.contacts_for_student("STU1")] == [
        "PRE-STU1"
    ]


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------


def test_limit_caps_number_of_students_processed(tmp_path: Path, content: FakeContent) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 11)}
    stack = _make_stack(tmp_path, student_grades=grades)

    changes = seed_contacts(stack, content, rng=random.Random(9), limit=4)
    touched_students = {c.key["Student id"] for c in changes}
    assert len(touched_students) == 4


def test_staged_seeding_across_multiple_limited_calls_covers_everyone_once(
    tmp_path: Path, content: FakeContent
) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 11)}
    stack = _make_stack(tmp_path, student_grades=grades)

    all_touched: set[str] = set()
    for _ in range(5):  # 5 batches of up to 4 students each, for 10 students
        changes = seed_contacts(stack, content, rng=random.Random(11), limit=4)
        if not changes:
            break
        stack.apply(changes)
        all_touched.update(c.key["Student id"] for c in changes)

    assert all_touched == set(grades)
    # And a further call finds nothing left to do.
    assert seed_contacts(stack, content, rng=random.Random(11), limit=4) == []
    # Nobody was seeded twice: every student ends with 1-2 guardian rows, none
    # blank.
    for student_id in grades:
        rows = stack.student_rows_for(student_id)
        assert 1 <= len(rows) <= 2
        assert all(schema.row_carries_contact(r) for r in rows)


# ---------------------------------------------------------------------------
# estimate_seed_volume
# ---------------------------------------------------------------------------


def test_estimate_seed_volume_math(tmp_path: Path, content: FakeContent) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 11)}  # all younger-grade
    stack = _make_stack(tmp_path, student_grades=grades, students_with_contacts={"STU1"})

    estimate = estimate_seed_volume(stack)
    assert estimate["students_without_contacts"] == 9
    assert estimate["estimated_contacts_low"] == 9 * 1
    assert estimate["estimated_contacts_high"] == 9 * 2
    # All 9 eligible students are younger-grade -> expected ~9 * 1.7 == 15 (rounded).
    assert estimate["estimated_contacts_expected"] == round(9 * 1.7)
    assert estimate["recommended_staged_limit"] > 0
    assert estimate["recommended_run_count"] >= 1
    assert "note" in estimate and "9" in estimate["note"]


def test_estimate_seed_volume_zero_when_fully_seeded(tmp_path: Path, content: FakeContent) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 6)}
    stack = _make_stack(
        tmp_path, student_grades=grades, students_with_contacts=set(grades),
    )
    estimate = estimate_seed_volume(stack)
    assert estimate["students_without_contacts"] == 0
    assert estimate["estimated_contacts_low"] == 0
    assert estimate["estimated_contacts_high"] == 0
    assert estimate["recommended_run_count"] == 0


def test_estimate_counts_students_not_students_csv_rows(
    tmp_path: Path, content: FakeContent
) -> None:
    """A student with two guardians occupies two rows but is still one student.

    Estimating off raw row counts would over-count the remaining work (and,
    via ``CsvStack.counts``, would put a mid-seed district straight through
    safety.assert_scale_sane's 25% tolerance on row growth alone).
    """

    grades = {f"STU{i}": "3" for i in range(1, 4)}
    stack = _make_stack(tmp_path, student_grades=grades)
    # Give STU1 two guardians so students.csv has more rows than students.
    seeded = seed_contacts(
        stack, content, rng=random.Random(1), guardians_per_student=(2, 2), limit=1
    )
    stack.apply(seeded)
    assert len(stack.students()) == 4  # STU1 x2 + STU2 + STU3
    assert len(stack.distinct_students()) == 3

    estimate = estimate_seed_volume(stack)
    assert estimate["students_without_contacts"] == 2  # STU2, STU3 -- not 3
    assert stack.counts()["students"] == 3
    assert stack.counts()[schema.CONTACTS_RECORD_TYPE] == 2
