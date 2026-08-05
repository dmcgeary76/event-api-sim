"""Tests for drift_engine.guardrail.

Covers: ratios computed against the pre-change denominator, block above 10%,
warn above 2%, the zero-denominator block (not a crash), enforce() raising
GuardrailViolation naming the record type, the net-attrition warn, dict
serialisation / summary rendering, and a realistic-cadence sanity check (2
contact deletes against ~50,000 contacts).

CONTACTS HAVE NO FILE OF THEIR OWN (see ``schema``'s module docstring)
---------------------------------------------------------------------
There is no contacts.csv: a guardian is a ROW on students.csv, so every
contact change in this file is a students.csv change carrying
``event_subject=EventSubject.CONTACT``. That makes two of the guardrail's
behaviours much more load-bearing than they look, and this file exercises
both deliberately:

* Attribution is by ``event_subject``, not by filename. A guardian removal
  must count against ``contacts`` and never against ``students`` -- otherwise
  routine contact churn inflates the students deletion ratio toward Clever's
  10% pause threshold, and a genuine student deletion hides inside that noise.
* A contact's CSV operation and its Clever-level effect legitimately diverge.
  Adding a guardian to a student who had none FILLS that student's existing
  blank row: ``Operation.UPDATE`` on the CSV, ``users.created (Contacts)`` on
  the wire. The net-attrition accounting has to see it as a creation.

``_stack_with_counts`` therefore builds a real students.csv row layout rather
than a bag of empty dicts, because ``CsvStack.counts()`` now derives both the
``students`` count (distinct Student ids) and the ``contacts`` count (rows
carrying a Contact sis id) from those rows.
"""

from __future__ import annotations

import pytest

from drift_engine import guardrail, schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, Change, EventSubject, EventType, GuardrailViolation, Operation


# record_type -> filename for the files whose row contents the guardrail does
# not care about. students.csv is deliberately absent: it is the one file whose
# rows have to be built for real (see ``_student_table``), because both the
# ``students`` and the ``contacts`` counts are derived from them. There is no
# "contacts" entry because there is no contacts.csv.
_RECORD_TYPE_FILES = {
    "schools": "schools.csv",
    "teachers": "teachers.csv",
    "staff": "staff.csv",
    "sections": "sections.csv",
    "enrollments": "enrollments.csv",
}


def _student_table(n_students: int, n_contacts: int) -> list[dict[str, str]]:
    """``n_students`` students sharing ``n_contacts`` guardians between them.

    Built through :func:`schema.expand_contact_rows` rather than by hand, so
    these fixtures cannot drift from the one place the row-per-contact pattern
    is encoded. Contacts are spread as evenly as possible, so a student either
    has ``n_contacts // n_students`` guardians or one more than that; students
    who end up with none still get exactly one row with the contact columns
    blank (dropping the row would delete the student).

    The result satisfies, exactly: ``counts()["students"] == n_students`` and
    ``counts()["contacts"] == n_contacts``.
    """

    if n_students == 0:
        if n_contacts:
            raise AssertionError(
                f"Cannot place {n_contacts} contact(s) in a stack with no students -- "
                "a guardian is a row on students.csv and has nowhere to live."
            )
        return []
    if n_contacts > n_students * schema.MAX_CONTACTS_PER_STUDENT:
        raise AssertionError(
            f"{n_contacts} contacts across {n_students} students exceeds the SFTP "
            f"spec's {schema.MAX_CONTACTS_PER_STUDENT}-per-student ceiling; this "
            "fixture would describe a stack that cannot exist."
        )

    rows: list[dict[str, str]] = []
    minted = 0
    for i in range(n_students):
        share = n_contacts // n_students + (1 if i < n_contacts % n_students else 0)
        contacts = [
            {schema.CONTACT_SIS_ID_COLUMN: f"CON{minted + j:06d}"} for j in range(share)
        ]
        minted += share
        rows.extend(schema.expand_contact_rows({"Student id": f"STU{i:06d}"}, contacts))
    return rows


def _stack_with_counts(**counts: int) -> CsvStack:
    """A CsvStack whose ``counts()`` match ``counts`` exactly.

    Row *contents* don't matter for the guardrail (it only calls
    ``stack.counts()``), so each of the simple tables is just a list of empty
    dicts of the right length. students.csv is the exception: ``counts()``
    reports ``students`` as the number of DISTINCT Student ids and derives
    ``contacts`` from the rows carrying a Contact sis id, so those rows have to
    be shaped properly -- see :func:`_student_table`.

    ``contacts=N`` with no ``students=`` given means N students with one
    guardian each, since a guardian cannot exist without a student to hang off.
    """

    requested = dict(counts)
    n_contacts = requested.pop("contacts", 0)
    n_students = requested.pop("students", None)
    if n_students is None:
        n_students = n_contacts

    tables: dict[str, list[dict[str, str]]] = {
        _RECORD_TYPE_FILES[record_type]: [{} for _ in range(n)]
        for record_type, n in requested.items()
    }
    if n_students or n_contacts:
        tables[schema.STUDENTS.filename] = _student_table(n_students, n_contacts)
    return CsvStack(tables, migrated_columns={})


# ---------------------------------------------------------------------------
# Change factories.
#
# Every one of these mirrors the exact shape selection.py emits, because the
# guardrail's attribution and move-matching both key off those details: the
# filename, the event_subject, and the "Student id" in the key. A factory that
# invented its own shape would test nothing about the real pipeline.
# ---------------------------------------------------------------------------


def _contact_delete(student_id: str, contact_sis_id: str) -> Change:
    """Guardian removed: the contact's row on students.csv is deleted."""

    return Change(
        filename=schema.STUDENTS.filename,
        operation=Operation.DELETE,
        key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.USERS_DELETED,
        event_subject=EventSubject.CONTACT,
        before={schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
    )


def _contact_create(student_id: str, contact_sis_id: str) -> Change:
    """Guardian added to a student who ALREADY has at least one.

    A brand new students.csv row for the same Student id, so a CSV CREATE.
    """

    return Change(
        filename=schema.STUDENTS.filename,
        operation=Operation.CREATE,
        key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.USERS_CREATED,
        event_subject=EventSubject.CONTACT,
        after={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
    )


def _contact_create_filling_blank_row(student_id: str, contact_sis_id: str) -> Change:
    """Guardian added to a student who had NONE -- the other add shape.

    Fills the student's existing contact-less row in place, so the CSV
    operation is an UPDATE while the Clever-level effect is a brand new
    guardian object (``users.created (Contacts)``). Keyed on the blank sis id,
    which is what identifies that row today; ``after`` mints the new one.
    """

    return Change(
        filename=schema.STUDENTS.filename,
        operation=Operation.UPDATE,
        key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: ""},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.USERS_CREATED,
        event_subject=EventSubject.CONTACT,
        before=schema.blank_contact_fields(),
        after={schema.CONTACT_SIS_ID_COLUMN: contact_sis_id},
    )


def _student_delete(student_id: str) -> Change:
    """A student themselves removed from the roster.

    The fixed cadence never does this -- which is exactly why the guardrail
    has to be able to see it. This is the shape a student deletion would take
    if selection ever produced one (or produced one by mistake), and it lands
    on the same file as ``_contact_delete``; only ``event_subject`` tells them
    apart.
    """

    return Change(
        filename=schema.STUDENTS.filename,
        operation=Operation.DELETE,
        key={"Student id": student_id, schema.CONTACT_SIS_ID_COLUMN: ""},
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_DELETED,
        event_subject=EventSubject.STUDENT,
        before={"Student id": student_id},
    )


def _enrollment_delete(student_id: str, section_id: str) -> Change:
    """Half of a section move: the student leaves ``section_id``."""

    return Change(
        filename=schema.ENROLLMENTS.filename,
        operation=Operation.DELETE,
        key={"Section id": section_id, "Student id": student_id},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.SECTIONS_UPDATED,
        event_subject=EventSubject.SECTION,
        before={"Section id": section_id, "Student id": student_id},
    )


def _enrollment_create(student_id: str, section_id: str) -> Change:
    """The other half of a section move: the student arrives at ``section_id``."""

    return Change(
        filename=schema.ENROLLMENTS.filename,
        operation=Operation.CREATE,
        key={"Section id": section_id, "Student id": student_id},
        bucket=Bucket.BIG_STUDENT,
        expected_event=EventType.SECTIONS_UPDATED,
        event_subject=EventSubject.SECTION,
        after={"School id": "SCH1"},
    )


# ---------------------------------------------------------------------------
# Basic ratio / verdict computation
# ---------------------------------------------------------------------------


def test_ratio_computed_against_pre_change_denominator():
    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(5)]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"
    assert verdict.deletes == 5
    assert verdict.total == 100  # pre-change total, not 95
    assert verdict.ratio == pytest.approx(0.05)
    assert verdict.verdict == "warn"  # >2%, <=10%


def test_block_above_ten_percent():
    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(11)]  # 11%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.11)
    assert verdict.verdict == "block"
    assert report.blocked is True


def test_exactly_ten_percent_does_not_block():
    """The guardrail says '> 10%', not '>= 10%' -- exactly the threshold is
    still within Clever's own limit."""

    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(10)]  # exactly 10%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.10)
    assert verdict.verdict == "warn"  # over 2%, not over 10%
    assert report.blocked is False


def test_warn_above_two_percent():
    stack = _stack_with_counts(contacts=1000)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(21)]  # 2.1%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.verdict == "warn"
    assert report.blocked is False


def test_exactly_two_percent_is_ok_not_warn():
    stack = _stack_with_counts(contacts=1000)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(20)]  # exactly 2%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.02)
    assert verdict.verdict == "ok"


def test_zero_denominator_is_a_block_not_a_crash():
    stack = _stack_with_counts(contacts=0)
    changes = [_contact_delete("STU000000", "CON000000")]

    report = guardrail.evaluate(stack, changes)  # must not raise ZeroDivisionError

    [verdict] = report.by_record_type
    assert verdict.total == 0
    assert verdict.verdict == "block"
    assert report.blocked is True


def test_no_deletes_produces_no_record_type_verdicts():
    stack = _stack_with_counts(contacts=100, students=100)
    changes = [_contact_create("STU000000", "NEWCON1")]
    report = guardrail.evaluate(stack, changes)

    assert report.by_record_type == ()
    assert report.blocked is False


def test_multiple_record_types_evaluated_independently():
    """Both change sets below live on students.csv -- only ``event_subject``
    separates guardian churn from student deletion. They must still be
    evaluated as two independent record types, so the 15% student deletion
    blocks on its own merits instead of being diluted into a combined
    students.csv ratio."""

    stack = _stack_with_counts(contacts=100, students=1000)
    changes = [_contact_delete("STU000000", "CON000000")] + [
        _student_delete(f"STU{i:06d}") for i in range(100, 250)  # 15% of students
    ]
    report = guardrail.evaluate(stack, changes)

    by_type = {v.record_type: v for v in report.by_record_type}
    assert by_type["contacts"].verdict == "ok"  # 1/100 = 1%
    assert by_type["students"].verdict == "block"  # 150/1000 = 15%
    assert report.blocked is True


# ---------------------------------------------------------------------------
# enforce()
# ---------------------------------------------------------------------------


def test_enforce_raises_guardrail_violation_naming_record_type():
    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(11)]

    with pytest.raises(GuardrailViolation, match="contacts"):
        guardrail.enforce(stack, changes)


def test_enforce_passes_through_safe_run():
    stack = _stack_with_counts(contacts=50000)
    changes = [_contact_delete("STU000001", "CON000001"), _contact_delete("STU000002", "CON000002")]
    report = guardrail.enforce(stack, changes)  # must not raise
    assert report.blocked is False


def test_enforce_does_not_raise_for_warn_only():
    stack = _stack_with_counts(contacts=1000)
    changes = [
        _contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(21)
    ]  # warn, not block
    report = guardrail.enforce(stack, changes)
    assert any(v.verdict == "warn" for v in report.by_record_type)


# ---------------------------------------------------------------------------
# Realistic cadence sanity check
# ---------------------------------------------------------------------------


def test_realistic_cadence_two_contact_deletes_against_fifty_thousand_passes_comfortably():
    """Brief §4's actual daily/Tue-Thu cadence deletes a handful of contacts
    against a district-scale stack. This must clear both thresholds by a
    wide margin."""

    stack = _stack_with_counts(contacts=50_000, students=33_620)
    changes = [_contact_delete("STU000001", "CON000001"), _contact_delete("STU000002", "CON000002")]

    report = guardrail.enforce(stack, changes)  # must not raise

    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"  # never "students", despite the file
    assert verdict.deletes == 2
    assert verdict.total == 50_000
    assert verdict.ratio == pytest.approx(2 / 50_000)
    assert verdict.verdict == "ok"
    assert verdict.ratio < guardrail.SAFE_THRESHOLD
    assert verdict.ratio < guardrail.CLEVER_PAUSE_THRESHOLD


# ---------------------------------------------------------------------------
# Contact attribution: guardian churn is never student attrition
# ---------------------------------------------------------------------------


def test_contact_removal_is_attributed_to_contacts_never_to_students():
    """A guardian removal is a students.csv row delete, and attributing it by
    filename would be wrong twice over: it inflates the students deletion
    ratio with routine guardian churn (walking it toward Clever's 10% pause
    threshold for no real reason), and it camouflages a genuine student
    deletion inside that same noise.

    30 removals below is 3% of contacts -- enough to warn on the contacts
    ratio, and enough to be plainly visible if it ever leaked into students."""

    stack = _stack_with_counts(students=1_000, contacts=1_000)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(30)]
    report = guardrail.evaluate(stack, changes)

    by_type = {v.record_type: v for v in report.by_record_type}
    assert set(by_type) == {"contacts"}  # students not implicated at all
    assert by_type["contacts"].deletes == 30
    assert by_type["contacts"].total == 1_000
    assert by_type["contacts"].verdict == "warn"  # 3% of contacts
    assert "students" not in by_type


# ---------------------------------------------------------------------------
# Fix 4: matched CREATE/DELETE pairs (enrollment moves) are not attrition --
# and identity matching, so unrelated churn is NOT netted away
# ---------------------------------------------------------------------------


def test_matched_create_delete_pair_is_not_counted_as_a_deletion():
    """A DELETE+CREATE pair for the SAME record identity in the same run (an
    enrollment section move -- one student leaving one section and arriving at
    another) must not trip the ratio, even on a tiny stack where a single
    unmatched delete would."""

    stack = _stack_with_counts(enrollments=10)
    changes = [
        _enrollment_delete("STU000001", "SEC_OLD"),
        _enrollment_create("STU000001", "SEC_NEW"),
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.deletes == 0
    assert verdict.moves_netted == 1
    assert verdict.verdict == "ok"
    assert report.blocked is False


def test_unmatched_deletes_still_counted_after_netting_moves():
    stack = _stack_with_counts(enrollments=10)
    changes = [
        _enrollment_delete("STU000001", "SEC_OLD"),
        _enrollment_delete("STU000002", "SEC_OLD"),  # unmatched -- genuine
        _enrollment_create("STU000001", "SEC_NEW"),  # matches STU000001's delete only
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.moves_netted == 1
    assert verdict.deletes == 1  # STU000002's un-enrollment is genuine and unmatched
    assert verdict.ratio == pytest.approx(0.1)


def test_move_on_small_toy_stack_does_not_block():
    """Regression for the reported 624 false blocks across 720 fuzzed toy
    stack shapes: a single matched move on a tiny stack must never block,
    even though a bare 1-delete-of-3 ratio (33%) would."""

    stack = _stack_with_counts(enrollments=3)
    changes = [
        _enrollment_delete("STU000001", "SEC_OLD"),
        _enrollment_create("STU000001", "SEC_NEW"),
    ]
    report = guardrail.enforce(stack, changes)  # must not raise
    assert report.blocked is False


def test_genuine_enrollment_move_still_nets_to_zero_deletions():
    """The counterpart to the contact test below: identity matching must not
    over-correct. A real move -- same Student id out of the old section and
    into the new one -- is not attrition, because the enrollment still exists;
    it just points somewhere else. This is the ONLY pattern netting is meant
    to forgive, and it must keep working."""

    stack = _stack_with_counts(students=1_000, enrollments=1_000)
    changes = [
        _enrollment_delete("STU000042", "SEC_OLD"),
        _enrollment_create("STU000042", "SEC_NEW"),
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.record_type == "enrollments"
    assert verdict.deletes == 0
    assert verdict.moves_netted == 1
    assert verdict.verdict == "ok"
    assert report.blocked is False


def test_contact_removals_are_not_netted_away_by_unrelated_contact_additions():
    """Regression for the bug that made contact attrition invisible.

    Netting used to be "this record type had some creates and some deletes in
    the same run", so a Tue/Thu big-student run adding 4 guardians and
    removing 2 netted ``min(2, 4) == 2`` -- every contact deletion cancelled,
    and the contacts guardrail could never report attrition at all. Adding
    guardian A to one student does not offset removing guardian B from a
    different student: they are unrelated records, and the removed guardian is
    gone.

    Distinct Student ids on the two sides below is the whole point of the
    test -- that is what makes these additions *unrelated* to the removals."""

    stack = _stack_with_counts(students=10_000, contacts=10_000)
    removals = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(2)]
    unrelated_additions = [
        _contact_create(f"STU{500 + i:06d}", f"NEWCON{i}") for i in range(4)
    ]
    report = guardrail.evaluate(stack, removals + unrelated_additions)

    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"
    assert verdict.moves_netted == 0  # nothing here is a move
    assert verdict.deletes == 2  # both removals survive the netting pass
    assert report.net_attrition.total_deletes == 2
    assert report.net_attrition.total_creates == 4


def test_contact_removal_is_not_netted_away_by_an_addition_to_the_SAME_student():
    """The narrow version of the bug above, found while reworking these tests.

    Move identity used to be (record type, Student id) only, so removing
    guardian A from a student while adding unrelated guardian B to that SAME
    student in one run matched as a "move" and netted the removal away --
    ``deletes`` came back 0 with ``moves_netted`` 1. selection.py draws its
    contact-addition and contact-removal targets from two independently
    shuffled pools with no cross-check, so one student landing in both is a
    thing that happens, and it is near-certain on a small stack.

    A contact cannot move: guardian B has a freshly minted Contact sis id and
    is a different person. Guardian A is gone, and the guardrail has to say so.
    """

    stack = _stack_with_counts(students=1_000, contacts=2_000)
    changes = [
        _contact_delete("STU000001", "CON000001"),  # guardian A removed
        _contact_create("STU000001", "NEWCON1"),  # unrelated guardian B added
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"
    assert verdict.moves_netted == 0
    assert verdict.deletes == 1


def test_moves_do_not_mask_a_genuine_mass_deletion_of_a_different_type():
    """Netting is scoped per record type -- a pile of matched enrollment
    moves must never hide unrelated, unmatched attrition on another type."""

    stack = _stack_with_counts(enrollments=100, students=100)
    changes = (
        [_enrollment_delete(f"STU{i:06d}", f"SEC_OLD{i}") for i in range(20)]
        + [_enrollment_create(f"STU{i:06d}", f"SEC_NEW{i}") for i in range(20)]  # all matched
        + [_student_delete(f"STU{i:06d}") for i in range(15)]  # 15%, unmatched
    )
    report = guardrail.evaluate(stack, changes)

    by_type = {v.record_type: v for v in report.by_record_type}
    assert by_type["enrollments"].deletes == 0
    assert by_type["enrollments"].verdict == "ok"
    assert by_type["students"].deletes == 15
    assert by_type["students"].verdict == "block"
    assert report.blocked is True


# ---------------------------------------------------------------------------
# Net attrition
# ---------------------------------------------------------------------------


def test_net_attrition_warns_when_deletes_far_outnumber_creates():
    stack = _stack_with_counts(contacts=10_000)
    changes = (
        [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(10)]
        + [_contact_create("STU005000", "NEWCON1")]
    )
    report = guardrail.evaluate(stack, changes)

    assert report.net_attrition.total_deletes == 10
    assert report.net_attrition.total_creates == 1
    assert report.net_attrition.verdict == "warn"
    assert report.net_attrition.reason


def test_net_attrition_ok_when_balanced():
    stack = _stack_with_counts(contacts=10_000)
    changes = [
        _contact_delete("STU000001", "CON000001"),
        _contact_create("STU005000", "NEWCON1"),
    ]
    report = guardrail.evaluate(stack, changes)

    assert report.net_attrition.verdict == "ok"


def test_net_attrition_warns_for_any_deletion_against_a_small_stack():
    stack = _stack_with_counts(contacts=10)  # well below the 500-row sanity floor
    changes = [
        _contact_delete("STU000001", "CON000001"),
        _contact_create("STU000005", "NEWCON1"),
    ]
    report = guardrail.evaluate(stack, changes)

    # Balanced 1-for-1, but the whole stack is suspiciously small.
    assert report.net_attrition.verdict == "warn"
    assert "500" in report.net_attrition.reason or "small" in report.net_attrition.reason.lower()


def test_net_attrition_does_not_warn_for_small_run_within_normal_bounds():
    stack = _stack_with_counts(contacts=50_000)
    changes = [_contact_delete("STU000001", "CON000001")]
    report = guardrail.evaluate(stack, changes)

    # 1 delete, 0 creates -- below NET_ATTRITION_MIN_ABS, should not warn.
    assert report.net_attrition.verdict == "ok"


def test_contact_filling_a_blank_row_counts_as_a_create_in_net_attrition():
    """A guardian added to a student who had none is an ``Operation.UPDATE``
    (it fills that student's existing blank contact row) whose predicted event
    is ``users.created (Contacts)``. Net-attrition accounting must count it as
    a creation.

    If it were counted only as "an update, i.e. neither", this run -- 4
    guardians removed, 4 guardians added -- would read as 4 deletes against 0
    creates and warn about erosion that isn't happening. That is not a
    cosmetic mis-report: during seeding almost every contact arrives this way,
    so the whole seed run would look like pure attrition."""

    stack = _stack_with_counts(students=10_000, contacts=10_000)
    removals = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(4)]
    fills = [
        _contact_create_filling_blank_row(f"STU{500 + i:06d}", f"NEWCON{i}") for i in range(4)
    ]
    report = guardrail.evaluate(stack, removals + fills)

    assert report.net_attrition.total_creates == 4  # the UPDATEs, counted as creates
    assert report.net_attrition.total_deletes == 4
    assert report.net_attrition.verdict == "ok"  # balanced, not 4-against-0

    # The removals are still counted in full against contacts -- crediting the
    # fills as creates must not net any deletion away (different students).
    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"
    assert verdict.deletes == 4
    assert verdict.moves_netted == 0


# ---------------------------------------------------------------------------
# Serialisation / rendering
# ---------------------------------------------------------------------------


def test_report_to_dict_is_json_shaped():
    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(11)]
    report = guardrail.evaluate(stack, changes)

    as_dict = report.to_dict()
    assert isinstance(as_dict, dict)
    assert as_dict["blocked"] is True
    assert as_dict["by_record_type"][0]["record_type"] == "contacts"
    assert as_dict["by_record_type"][0]["verdict"] == "block"
    assert "net_attrition" in as_dict
    assert isinstance(as_dict["net_attrition"], dict)

    import json

    json.dumps(as_dict)  # must be plain-JSON-serialisable


def test_report_summary_and_str_are_readable():
    stack = _stack_with_counts(contacts=100)
    changes = [_contact_delete(f"STU{i:06d}", f"CON{i:06d}") for i in range(11)]
    report = guardrail.evaluate(stack, changes)

    summary = report.summary()
    assert "contacts" in summary
    assert "BLOCK" in summary
    assert str(report) == summary


# ---------------------------------------------------------------------------
# Fix 3: actual row-count delta vs. the last successfully-pushed counts
# ---------------------------------------------------------------------------


def test_truncation_with_no_planned_deletes_is_invisible_without_last_pushed_counts():
    """Confirms the bug this fix closes still reproduces when the caller has
    no last_pushed_counts to give: students.csv truncated by 3,000/33,621
    rows (8.9%) with zero DELETE changes planned produces no verdicts at
    all, and nothing blocks."""

    stack = _stack_with_counts(students=30_621)  # already truncated on load
    report = guardrail.evaluate(stack, [])
    assert report.by_record_type == ()
    assert report.blocked is False


def test_truncation_at_8_9_percent_is_now_visible_as_a_warn_via_last_pushed_counts():
    """The exact reproduction from the audit: students.csv truncated by
    3,000/33,621 rows (8.9%) outside the engine, with zero DELETE changes
    planned. 8.9% is under Clever's own 10% pause threshold, so this
    correctly WARNS rather than blocks -- but critically, it is no longer
    invisible (by_record_type used to be () with nothing to show for it;
    now the truncation is on record for David to see, where before this fix
    landed silently)."""

    stack = _stack_with_counts(students=30_621)  # 33,621 - 3,000
    report = guardrail.evaluate(stack, [], last_pushed_counts={"students": 33_621})

    [verdict] = report.by_record_type
    assert verdict.record_type == "students"
    assert verdict.unexplained_loss == 3_000
    assert verdict.deletes == 3_000
    assert verdict.total == 33_621
    assert verdict.ratio == pytest.approx(3_000 / 33_621)
    assert verdict.verdict == "warn"
    assert "truncated or altered" in verdict.reason


def test_truncation_beyond_ten_percent_is_blocked_via_last_pushed_counts():
    """A larger truncation -- enough to cross Clever's actual 10%
    pause-for-review threshold -- must hard-block, exactly like an
    equivalent number of planned DELETE changes would."""

    stack = _stack_with_counts(students=30_000)  # 33,621 - 3,621 (~10.77%)
    report = guardrail.evaluate(stack, [], last_pushed_counts={"students": 33_621})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 3_621
    assert verdict.verdict == "block"
    assert report.blocked is True

    with pytest.raises(GuardrailViolation, match="students"):
        guardrail.enforce(stack, [], last_pushed_counts={"students": 33_621})


def test_first_run_with_no_last_pushed_counts_is_unaffected():
    """A genuine first run has no last_pushed_counts yet -- ordinary planned
    deletes must be evaluated exactly as before."""

    stack = _stack_with_counts(students=100)
    changes = [_student_delete("STU000001")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts=None)

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 0
    assert verdict.total == 100


def test_legitimate_growth_from_zero_is_not_treated_as_attrition():
    """contacts going 0 -> 50,000 in a seed run must never be flagged, even
    though last_pushed_counts reflects the pre-seed 0 -- growth is not loss.

    Note the seed shape: every one of these arrives as an UPDATE filling a
    contact-less student's blank row, which is what a seed run genuinely looks
    like. The guardrail counts them as creations (see
    ``_is_effective_create``), so they must still produce no verdict and no
    block."""

    stack = _stack_with_counts(students=50_000, contacts=0)
    changes = [
        _contact_create_filling_blank_row(f"STU{i:06d}", f"CON{i:06d}") for i in range(50_000)
    ]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 0})

    assert report.by_record_type == ()
    assert report.blocked is False


def test_last_pushed_counts_matching_current_changes_nothing():
    """When nothing has touched the stack outside this engine, current
    counts equal last_pushed_counts, so ordinary runs behave exactly as
    before the fix."""

    stack = _stack_with_counts(contacts=50_000)
    changes = [_contact_delete("STU000001", "CON000001"), _contact_delete("STU000002", "CON000002")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 50_000})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 0
    assert verdict.deletes == 2
    assert verdict.verdict == "ok"


def test_unexplained_loss_combines_with_this_runs_own_planned_deletes():
    """Truncation AND a normal run's own small planned deletes both count,
    additively, against the last-pushed denominator."""

    stack = _stack_with_counts(contacts=48_000)  # already down from 50,000
    changes = [_contact_delete("STU000001", "CON000001"), _contact_delete("STU000002", "CON000002")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 50_000})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 2_000
    assert verdict.deletes == 2_002
    assert verdict.total == 50_000
