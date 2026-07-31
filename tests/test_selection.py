"""Tests for drift_engine.selection.

Uses a small synthetic CsvStack (2 schools, 30 students, 10 teachers,
8 sections, one enrollment per student, and a handful of contacts with
deliberately mixed counts) built in ``tmp_path``, plus a fake content
generator that returns canned, inspectable strings instead of calling an
LLM.

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
    STU11-STU30 have zero contacts.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from drift_engine import schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, EventType, Operation, RunPlan
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


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in columns))
    path.write_text((CRLF.join(lines) + CRLF), encoding="utf-8")


def _write_synthetic_stack(directory: Path, *, with_contacts: bool = True) -> None:
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

    enrollments = [
        {"School id": s["School id"], "Section id": s["_home_section"], "Student id": s["Student id"]}
        for s in students
    ]
    for s in students:
        del s["_home_section"]

    _write_csv(directory / "students.csv", schema.STUDENTS.columns, students)
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)

    if with_contacts:
        contacts = []
        seq = 1
        # STU1-STU5: two contacts each (safe to remove one).
        for i in range(1, 6):
            for suffix in ("A", "B"):
                contacts.append({
                    "School id": "SCH1", "Student id": f"STU{i}", "Contact id": f"CTX{i}{suffix}",
                    "Contact name": f"Guardian{i}{suffix}", "Contact type": "Parent",
                    "Relationship": "Mother", "Phone": "918-555-0000", "Phone type": "Mobile",
                    "Email": f"guardian{i}{suffix}@example.com", "Sequence": str(seq),
                })
                seq += 1
        # STU6-STU10: exactly one contact each (must never be removed).
        for i in range(6, 11):
            contacts.append({
                "School id": "SCH1", "Student id": f"STU{i}", "Contact id": f"CTX{i}A",
                "Contact name": f"Guardian{i}A", "Contact type": "Parent",
                "Relationship": "Father", "Phone": "918-555-0001", "Phone type": "Home",
                "Email": f"guardian{i}a@example.com", "Sequence": "1",
            })
        _write_csv(directory / "contacts.csv", schema.CONTACTS.columns, contacts)


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
            if c.filename == "contacts.csv" and c.operation is Operation.DELETE:
                student_id = c.before["Student id"]
                removed_by_student[student_id] = removed_by_student.get(student_id, 0) + 1

        for student_id, removed in removed_by_student.items():
            remaining = original_counts.get(student_id, 0) - removed
            assert remaining >= 1, f"seed {seed} orphaned student {student_id}"

    # And explicitly: students with exactly one contact (STU6-STU10) must
    # never appear as a removal target under any of these seeds.
    single_contact_students = {f"STU{i}" for i in range(6, 11)}
    for seed in range(30):
        changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(seed))
        for c in changes:
            if c.filename == "contacts.csv" and c.operation is Operation.DELETE:
                assert c.before["Student id"] not in single_contact_students


def test_contacts_added_are_ai_generated_and_create_events(
    stack: CsvStack, content: FakeContent
) -> None:
    changes = select_changes(stack, _tuesday_plan(), content, rng=random.Random(3))
    added = [c for c in changes if c.filename == "contacts.csv" and c.operation is Operation.CREATE]
    assert added
    for c in added:
        assert c.expected_event is EventType.CONTACTS_CREATED
        assert c.ai_generated is True
        assert c.note


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
# ID minting
# ---------------------------------------------------------------------------


def test_new_ids_never_collide_with_existing_ones(stack: CsvStack, content: FakeContent) -> None:
    existing_contact_ids = {c["Contact id"] for c in stack.contacts()}
    existing_teacher_ids = {t["Teacher id"] for t in stack.teachers()}

    changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(9))

    new_contact_ids = [
        c.key["Contact id"]
        for c in changes
        if c.filename == "contacts.csv" and c.operation is Operation.CREATE
    ]
    new_teacher_ids = [
        c.key["Teacher id"]
        for c in changes
        if c.filename == "teachers.csv" and c.operation is Operation.CREATE
    ]

    assert new_contact_ids, "expected at least one new contact"
    assert new_teacher_ids, "expected at least one new teacher"
    for cid in new_contact_ids:
        assert cid not in existing_contact_ids
        assert cid.startswith("CON")
    for tid in new_teacher_ids:
        assert tid not in existing_teacher_ids
        assert tid.startswith("TCH9")
    # No collisions among ids minted within the same run, either.
    assert len(new_contact_ids) == len(set(new_contact_ids))
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

    import datetime

    plan = RunPlan(run_date=datetime.date(2026, 7, 27), buckets=(Bucket.SMALL_DAILY,))
    changes = select_changes(stack, plan, content, rng=random.Random(4))

    assert changes  # student edits still happen
    assert all(c.filename != "contacts.csv" for c in changes)
    assert any(c.expected_event is EventType.USERS_UPDATED for c in changes)


# ---------------------------------------------------------------------------
# No record touched twice in one run
# ---------------------------------------------------------------------------


def test_no_record_touched_twice_in_one_run(stack: CsvStack, content: FakeContent) -> None:
    for seed in range(20):
        changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(seed))

        # Small daily contact field edits: each contact at most once.
        contact_edits = [
            c.key["Contact id"]
            for c in changes
            if c.filename == "contacts.csv" and c.operation is Operation.UPDATE
        ]
        assert len(contact_edits) == len(set(contact_edits)), seed

        # Small daily student field edits: each student at most once.
        student_edits = [
            c.key["Student id"]
            for c in changes
            if c.filename == "students.csv" and c.operation is Operation.UPDATE
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
            c.key["Contact id"]
            for c in changes
            if c.filename == "contacts.csv" and c.operation is Operation.DELETE
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

        # Newly minted ids: no duplicate contact or teacher ids created.
        new_contact_ids = [
            c.key["Contact id"]
            for c in changes
            if c.filename == "contacts.csv" and c.operation is Operation.CREATE
        ]
        assert len(new_contact_ids) == len(set(new_contact_ids)), seed


def test_every_change_has_a_note(stack: CsvStack, content: FakeContent) -> None:
    changes = select_changes(stack, _full_week_plan(), content, rng=random.Random(13))
    assert changes
    for c in changes:
        assert c.note and c.note.strip()


# ---------------------------------------------------------------------------
# Fix 1: no UPDATE change may be a no-op (after == before for every field).
#
# The audit that found this bug measured 486/486 (100%) of contacts.csv
# "Email" edits as no-op writes -- ``guardian_email`` is a pure function of
# (name, student last name), so re-deriving it from the exact same inputs
# during a small-daily "edit" just recomputed the identical address every
# time. Clever's CSV diff sees nothing in that case, so no contacts.updated
# event is ever emitted no matter how many times selection.py "changes" that
# field. This test is the one the audit specifically called out as missing
# -- its absence is what let the bug through in the first place.
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
    """Reproduces the exact audit scenario: a contact's stored Email already
    equals what the (pure, deterministic) generator would derive for
    attempt=0 -- e.g. because a previous edit (or the seeding step) already
    applied that exact convention. The small-daily "email tweak" must still
    land on a genuinely different address (via the ``attempt`` reroll)
    rather than silently re-writing the same one, or (per Fix 1(b)) skip
    the field entirely rather than emit a no-op UPDATE.
    """

    # Force every contact's stored Email to exactly what attempt=0 would
    # derive, so whichever contact gets picked for a small-daily email edit
    # is guaranteed to hit the "derivation repeats the current value" case
    # the audit found -- without this, the test would pass trivially (any
    # freshly-picked contact's real seeded Email already differs from
    # "stuck@example.com", so the bug this test targets would never
    # actually be exercised).
    for c in stack.contacts():
        c["Email"] = "stuck@example.com"

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
    for seed in range(20):
        changes = select_changes(stack, _tuesday_plan(), stuck, rng=random.Random(seed))
        email_edits = [
            c
            for c in changes
            if c.filename == "contacts.csv" and c.operation is Operation.UPDATE and "Email" in c.after
        ]
        for c in email_edits:
            assert c.before["Email"] == "stuck@example.com"
            assert c.after["Email"] != c.before["Email"], (seed, c)


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
