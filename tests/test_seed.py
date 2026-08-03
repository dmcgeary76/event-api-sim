"""Tests for drift_engine.seed.

Uses a small synthetic CsvStack (1 school, a handful of students, some of
which already have contacts) written into ``tmp_path``, plus a fake content
generator standing in for the real AI content generator -- seed.py is coded
against the same informal interface ``selection.py`` uses (``guardian_name``,
``guardian_email``, ``phone``), never against a concrete implementation.
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
    _write_csv(directory / "students.csv", schema.STUDENTS.columns, students)

    enrollments = [
        {"School id": "SCH1", "Section id": "SEC1", "Student id": sid} for sid in student_grades
    ]
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)

    if students_with_contacts:
        contacts = []
        for sid in students_with_contacts:
            contacts.append(
                {
                    "School id": "SCH1", "Student id": sid, "Contact id": f"PRE-{sid}",
                    "Contact name": f"Existing Guardian {sid}", "Contact type": "Parent",
                    "Relationship": "Mother", "Phone": "9185550000", "Phone type": "Mobile",
                    "Email": f"existing.{sid.lower()}@tulsaschools-replica.org", "Sequence": "1",
                }
            )
        _write_csv(directory / "contacts.csv", schema.CONTACTS.columns, contacts)


@pytest.fixture()
def content() -> FakeContent:
    return FakeContent()


def _make_stack(tmp_path: Path, **kwargs) -> CsvStack:
    d = tmp_path / "stack"
    _write_synthetic_stack(d, **kwargs)
    return CsvStack.load(d)


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
    assert all(c.operation is Operation.CREATE for c in changes)
    assert all(c.filename == schema.CONTACTS.filename for c in changes)
    # Seeded contacts are users.created (Contacts) on Clever's real Events
    # API, not a distinct contacts.created event -- see models.EventType.
    assert all(c.expected_event is EventType.USERS_CREATED for c in changes)
    assert all(c.event_subject is EventSubject.CONTACT for c in changes)
    assert all(c.expected_event_label == "users.created (Contacts)" for c in changes)

    by_student: dict[str, list] = {}
    for c in changes:
        by_student.setdefault(c.after["Student id"], []).append(c)

    assert set(by_student) == set(grades)
    for student_id, student_changes in by_student.items():
        assert 1 <= len(student_changes) <= 2
        sequences = sorted(c.after["Sequence"] for c in student_changes)
        assert sequences == ["1"] or sequences == ["1", "2"]

        # School id / Student id copied correctly from the student's row.
        for c in student_changes:
            assert c.after["School id"] == "SCH1"
            assert c.after["Student id"] == student_id

        # No duplicate Relationship within one student's contacts.
        relationships = [c.after["Relationship"] for c in student_changes]
        assert len(relationships) == len(set(relationships))


def test_younger_grades_skew_toward_two_guardians(tmp_path: Path, content: FakeContent) -> None:
    # All young-grade students, large N -- expect a clear majority with 2.
    grades = {f"STU{i}": "1" for i in range(1, 201)}
    stack = _make_stack(tmp_path, student_grades=grades)

    changes = seed_contacts(stack, content, rng=random.Random(7))
    by_student: dict[str, int] = {}
    for c in changes:
        sid = c.after["Student id"]
        by_student[sid] = by_student.get(sid, 0) + 1

    two_count = sum(1 for n in by_student.values() if n == 2)
    assert two_count > len(by_student) * 0.5


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
    touched_students = {c.after["Student id"] for c in changes}
    assert "STU1" not in touched_students
    assert "STU2" not in touched_students
    assert touched_students == {"STU3", "STU4", "STU5"}


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------


def test_limit_caps_number_of_students_processed(tmp_path: Path, content: FakeContent) -> None:
    grades = {f"STU{i}": "3" for i in range(1, 11)}
    stack = _make_stack(tmp_path, student_grades=grades)

    changes = seed_contacts(stack, content, rng=random.Random(9), limit=4)
    touched_students = {c.after["Student id"] for c in changes}
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
        all_touched.update(c.after["Student id"] for c in changes)

    assert all_touched == set(grades)
    # And a further call finds nothing left to do.
    assert seed_contacts(stack, content, rng=random.Random(11), limit=4) == []


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
