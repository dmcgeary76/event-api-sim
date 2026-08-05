"""Tests for drift_engine.csvstack.

All fixtures are small synthetic CSVs written into ``tmp_path`` -- never the
real 33k-row baseline stack, so these tests stay fast and don't depend on
anything outside the repo.

Two fixtures, deliberately at opposite ends of the engine's lifecycle:

* ``baseline_dir`` -- a raw SIS export. None of the engine-added columns are
  present ("Middle name", "Teacher 2 id", and the seven contact columns), so
  it exercises the migration path and the "no student has a guardian yet"
  state.
* ``seeded_dir`` -- the same district after seeding. Every column present, and
  STU1 has three guardians, so STU1 occupies THREE students.csv rows sharing
  one Student id (SFTP Instructions v2.1.1 -- contacts are rows on
  students.csv, there is no contacts.csv). Anything about the two-column
  students.csv key, student-level fan-out, or contact deletion needs this one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drift_engine import schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, Change, EventSubject, EventType, Operation

CRLF = "\r\n"


def _write(path: Path, lines: list[str]) -> bytes:
    """Write ``lines`` (without terminators) joined by CRLF and return the bytes."""

    data = (CRLF.join(lines) + CRLF).encode("utf-8")
    path.write_bytes(data)
    return data


def _write_baseline_stack(directory: Path) -> dict[str, bytes]:
    """Write a small, internally-consistent stack (no engine-added columns).

    Returns the exact bytes written per filename, for byte-identity checks.
    """

    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, bytes] = {}

    written["schools.csv"] = _write(
        directory / "schools.csv",
        [
            ",".join(schema.SCHOOLS.columns),
            "SCH1,Anderson Elementary,SCH1,PreKindergarten,5,Pat Roberts,pat@tulsaschools-replica.org,1 Main St,Tulsa,OK,74114,918-555-0100",
        ],
    )

    written["students.csv"] = _write(
        directory / "students.csv",
        [
            # Deliberately WITHOUT "Middle name" AND without the seven
            # CONTACT_COLUMNS -- all eight are engine-added, so this is exactly
            # the shape of a fresh SIS export that this engine has never
            # touched: one row per student, no guardians on file yet.
            "School id,Student id,Student number,Last name,First name,Grade,Gender,DOB,Student email",
            "SCH1,STU1,STU1,Barnes,Jordan,PreKindergarten,F,08/13/2020,jordan.barnes@students.tulsaschools-replica.org",
            "SCH1,STU2,STU2,Martinez,Mary,PreKindergarten,M,01/11/2020,mary.martinez@students.tulsaschools-replica.org",
        ],
    )

    written["teachers.csv"] = _write(
        directory / "teachers.csv",
        [
            ",".join(schema.TEACHERS.columns),
            "SCH1,TCH1,TCH1,marilyn.gomez@tulsaschools-replica.org,Marilyn,Gomez,Teacher",
            "SCH1,TCH2,TCH2,carolyn.gonzales@tulsaschools-replica.org,Carolyn,Gonzales,Teacher",
        ],
    )

    written["staff.csv"] = _write(
        directory / "staff.csv",
        [
            ",".join(schema.STAFF.columns),
            "SCH1,STF1,michelle.taylor@tulsaschools-replica.org,Michelle,Taylor,District Office,Superintendent,",
        ],
    )

    written["sections.csv"] = _write(
        directory / "sections.csv",
        [
            # Deliberately WITHOUT "Teacher 2 id" -- matches the real export.
            "School id,Section id,Teacher id,Name,Section number,Grade,Course name,Course number,Subject,Term name",
            "SCH1,SEC1,TCH1,Homeroom,SEC1,PreKindergarten,Homeroom,HR,Homeroom/advisory,Year",
        ],
    )

    written["enrollments.csv"] = _write(
        directory / "enrollments.csv",
        [
            ",".join(schema.ENROLLMENTS.columns),
            "SCH1,SEC1,STU1",
            "SCH1,SEC1,STU2",
        ],
    )

    return written


#: STU1's three guardians in the seeded stack, in row order: the seven
#: ``schema.CONTACT_COLUMNS`` values for each. Named here (rather than only
#: living inside the CSV text) so the fan-out / row-identity tests can assert
#: against them without re-parsing the fixture.
SEEDED_STU1_CONTACTS: tuple[tuple[str, ...], ...] = (
    ("Mother", "Parent", "Jamie Barnes", "918-555-0101", "Mobile", "jamie.barnes@example.com", "CON1"),
    ("Father", "Parent", "Robin Barnes", "918-555-0102", "Home", "robin.barnes@example.com", "CON2"),
    (
        "Grandmother", "Emergency", "Alice Barnes", "918-555-0103", "Work",
        "alice.barnes@example.com", "CON3",
    ),
)

#: The ten ``schema.STUDENT_LEVEL_COLUMNS`` values STU1's three rows all share.
#: They MUST be identical across those rows -- one student cannot present two
#: different middle names in one file -- which is what ``apply``'s fan-out
#: exists to keep true.
_SEEDED_STU1_STUDENT_FIELDS = (
    "SCH1,STU1,STU1,Barnes,Jordan,,PreKindergarten,F,08/13/2020,"
    "jordan.barnes@students.tulsaschools-replica.org"
)

#: STU2 has NO guardians, so exactly one row with the seven contact columns
#: blank (the trailing commas). Dropping the row instead would delete STU2.
_SEEDED_STU2_ROW = (
    "SCH1,STU2,STU2,Martinez,Mary,,PreKindergarten,M,01/11/2020,"
    "mary.martinez@students.tulsaschools-replica.org" + "," * 7
)


def _write_seeded_stack(directory: Path) -> dict[str, bytes]:
    """The same district in the POST-seed state: contacts present, no gaps.

    Differs from ``_write_baseline_stack`` in two ways, both deliberate:

    * students.csv and sections.csv already carry every engine-added column,
      so ``migrated_columns`` is empty and the whole stack must round-trip
      byte-identically (nothing gets widened on load).
    * STU1 has three guardians, so STU1 occupies THREE rows sharing one
      Student id -- the row-per-contact mechanism from SFTP Instructions
      v2.1.1 (see ``schema.expand_contact_rows``). This is why
      ``schema.STUDENTS.key`` is ``("Student id", "Contact sis id")``: under
      the old single-column key these three rows collapsed to one index entry,
      silently losing two guardians.

    Returns the exact bytes written per filename, for byte-identity checks.
    """

    written = _write_baseline_stack(directory)

    written["students.csv"] = _write(
        directory / "students.csv",
        [",".join(schema.STUDENTS.columns)]
        + [
            f"{_SEEDED_STU1_STUDENT_FIELDS},{','.join(contact)}"
            for contact in SEEDED_STU1_CONTACTS
        ]
        + [_SEEDED_STU2_ROW],
    )

    # sections.csv rewritten WITH "Teacher 2 id" (blank) for the same reason:
    # a fully-migrated stack has nothing left to backfill on load.
    written["sections.csv"] = _write(
        directory / "sections.csv",
        [
            ",".join(schema.SECTIONS.columns),
            "SCH1,SEC1,TCH1,,Homeroom,SEC1,PreKindergarten,Homeroom,HR,Homeroom/advisory,Year",
        ],
    )

    return written


@pytest.fixture()
def baseline_dir(tmp_path: Path) -> Path:
    d = tmp_path / "baseline"
    _write_baseline_stack(d)
    return d


@pytest.fixture()
def seeded_dir(tmp_path: Path) -> Path:
    d = tmp_path / "seeded"
    _write_seeded_stack(d)
    return d


# ---------------------------------------------------------------------------
# Round trip / byte identity
# ---------------------------------------------------------------------------


def test_round_trip_preserves_crlf_and_is_byte_identical_for_untouched_files(
    tmp_path: Path, baseline_dir: Path
) -> None:
    """The most important test: load -> save with no changes must not
    perturb files that have no engine-added columns, and must preserve CRLF
    everywhere, including in files that DID get columns migrated in.
    """

    stack = CsvStack.load(baseline_dir)
    out_dir = tmp_path / "out"
    stack.save(out_dir)

    # schools.csv / teachers.csv / staff.csv / enrollments.csv have no
    # engine-added columns at all -> must be byte-for-byte identical.
    for filename in ("schools.csv", "teachers.csv", "staff.csv", "enrollments.csv"):
        original = (baseline_dir / filename).read_bytes()
        rewritten = (out_dir / filename).read_bytes()
        assert rewritten == original, f"{filename} changed on a no-op round trip"

    # students.csv / sections.csv gained engine columns, so bytes differ, but
    # CRLF line endings must still be used throughout.
    for filename in ("students.csv", "sections.csv"):
        rewritten = (out_dir / filename).read_bytes()
        assert b"\r\n" in rewritten
        assert b"\n\n" not in rewritten  # no bare LF sequences
        # No lone LF not preceded by CR.
        text = rewritten.decode("utf-8")
        assert all(
            text[i - 1] == "\r" for i, ch in enumerate(text) if ch == "\n" and i > 0
        )


def test_round_trip_is_byte_identical_for_a_students_csv_carrying_contact_rows(
    tmp_path: Path, seeded_dir: Path
) -> None:
    """The same byte-identity guarantee, on the shape contacts actually take.

    A fully-migrated stack (every engine-added column already in every header)
    has nothing to widen on load, so load -> save with zero changes must
    reproduce EVERY file byte for byte -- including a students.csv where one
    student occupies three rows. Clever diffs the new export against the
    previous one row-for-row: a reordered, requoted, or LF-terminated rewrite
    of 3 guardian rows reads as 3 changed records per student across the whole
    district, i.e. a full-district rewrite instead of the handful of
    deliberate edits this engine made.
    """

    stack = CsvStack.load(seeded_dir)
    # Nothing was backfilled -- otherwise "bytes differ" would be expected and
    # this test would be asserting nothing.
    assert stack.migrated_columns == {}

    out_dir = tmp_path / "out"
    stack.save(out_dir)

    for spec in schema.ALL_SPECS:
        original = (seeded_dir / spec.filename).read_bytes()
        rewritten = (out_dir / spec.filename).read_bytes()
        assert rewritten == original, f"{spec.filename} changed on a no-op round trip"

    # And specifically: STU1's three rows survived as three rows, in order.
    # read_bytes().decode(), not read_text(): read_text applies universal-newline
    # translation and would turn the CRLFs this test is inspecting into LFs.
    students_text = (out_dir / "students.csv").read_bytes().decode("utf-8")
    assert students_text.count("\r\nSCH1,STU1,") == 3
    assert [line.rsplit(",", 1)[-1] for line in students_text.split(CRLF)[1:4]] == [
        "CON1",
        "CON2",
        "CON3",
    ]


def test_contacts_csv_is_never_written_and_a_stale_one_is_dropped_by_save(
    tmp_path: Path, baseline_dir: Path
) -> None:
    """There is no contacts.csv in Clever's SFTP spec, so ``save`` must never
    produce one -- and a stale contacts.csv left in the target directory by the
    pre-2026-08-05 version of this engine must be gone after the next save,
    since save promotes a freshly-staged directory holding only
    ``schema.ALL_SPECS``. A leftover file that Clever's ingest does not
    recognise sitting in the pushed directory is exactly the shape of the
    original bug (contact events that could never fire)."""

    stack = CsvStack.load(baseline_dir)
    out_dir = tmp_path / "out"
    stack.save(out_dir)

    # Plant the stale artifact a pre-2026-08-05 run would have left behind.
    (out_dir / "contacts.csv").write_text(
        "School id,Student id,Contact id\r\nSCH1,STU1,CON1\r\n", encoding="utf-8"
    )

    written = stack.save(out_dir)
    names = {p.name for p in written}
    assert "contacts.csv" not in names
    assert not (out_dir / "contacts.csv").exists()
    assert {p.name for p in out_dir.iterdir()} == {spec.filename for spec in schema.ALL_SPECS}


def test_students_csv_without_contact_columns_loads_as_zero_contacts(
    baseline_dir: Path,
) -> None:
    """Contacts are a projection over students.csv rows, not a table of their
    own. A pre-seed export has no contact columns at all, so every row's
    contact half backfills blank -- which must read as zero contacts, not as
    one empty pseudo-contact per student."""

    stack = CsvStack.load(baseline_dir)
    assert stack.contacts() == []
    assert stack.counts()["contacts"] == 0
    # The rows themselves still exist -- it's the contacts that are absent.
    assert len(stack.students()) == 2


# ---------------------------------------------------------------------------
# Engine-added column migration
# ---------------------------------------------------------------------------


def test_migration_adds_engine_columns_with_empty_values_and_correct_order(
    baseline_dir: Path,
) -> None:
    stack = CsvStack.load(baseline_dir)

    # students.csv is missing "Middle name" AND all seven contact columns --
    # reported in ENGINE_ADDED_COLUMNS order, not header order or set order.
    assert stack.migrated_columns["students.csv"] == ("Middle name",) + schema.CONTACT_COLUMNS
    assert stack.migrated_columns["sections.csv"] == ("Teacher 2 id",)

    for row in stack.students():
        assert row["Middle name"] == ""
        # Every migrated contact column lands blank -- i.e. this student has no
        # guardian on file, which is different from having a blank guardian.
        assert schema.contact_fields(row) == schema.blank_contact_fields()
        assert not schema.row_carries_contact(row)
        assert tuple(row.keys()) == schema.STUDENTS.columns

    for row in stack.sections():
        assert row["Teacher 2 id"] == ""
        assert tuple(row.keys()) == schema.SECTIONS.columns


def test_unmigrated_file_not_listed_in_migrated_columns(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    assert "teachers.csv" not in stack.migrated_columns
    assert "schools.csv" not in stack.migrated_columns


# ---------------------------------------------------------------------------
# Unknown column validation
# ---------------------------------------------------------------------------


def test_unknown_column_on_disk_raises_value_error(tmp_path: Path, baseline_dir: Path) -> None:
    # Corrupt teachers.csv with a column the schema doesn't know about.
    bad = "School id,Teacher id,Teacher number,Teacher email,First name,Last name,Title,Mystery Column"
    row = "SCH1,TCH1,TCH1,marilyn.gomez@tulsaschools-replica.org,Marilyn,Gomez,Teacher,???"
    _write(baseline_dir / "teachers.csv", [bad, row])

    with pytest.raises(ValueError) as exc_info:
        CsvStack.load(baseline_dir)

    message = str(exc_info.value)
    assert "teachers.csv" in message
    assert "Mystery Column" in message


# ---------------------------------------------------------------------------
# Fix 1: a missing REQUIRED (non-engine-added) column must raise, never be
# silently backfilled with "" and written back out blank.
# ---------------------------------------------------------------------------


def test_missing_required_column_raises_value_error(baseline_dir: Path) -> None:
    """Reproduction: dropping 'Student email' from students.csv's header and
    rows used to load fine, blank every student's email in memory, and
    re-save it that way on the next sync -- reachable via the documented
    onboarding step of dropping a CSV export into baseline/."""

    header = "School id,Student id,Student number,Last name,First name,Grade,Gender,DOB"
    rows = [
        "SCH1,STU1,STU1,Barnes,Jordan,PreKindergarten,F,08/13/2020",
        "SCH1,STU2,STU2,Martinez,Mary,PreKindergarten,M,01/11/2020",
    ]
    _write(baseline_dir / "students.csv", [header] + rows)

    with pytest.raises(ValueError) as exc_info:
        CsvStack.load(baseline_dir)

    message = str(exc_info.value)
    assert "students.csv" in message
    assert "Student email" in message


def test_missing_required_column_is_reported_even_alongside_a_present_engine_added_column(
    baseline_dir: Path,
) -> None:
    """A file can be missing a required column while ALSO already carrying
    the engine-added one -- both states must be handled independently, and
    the required-column check must still fire."""

    header = "School id,Student id,Student number,Last name,First name,Middle name,Grade,Gender,DOB"
    rows = ["SCH1,STU1,STU1,Barnes,Jordan,J,PreKindergarten,F,08/13/2020"]
    _write(baseline_dir / "students.csv", [header] + rows)

    with pytest.raises(ValueError, match="Student email"):
        CsvStack.load(baseline_dir)


def test_multiple_missing_required_columns_all_named_in_the_error(baseline_dir: Path) -> None:
    header = "School id,Student id,Student number,First name,Grade,Gender,DOB"
    rows = ["SCH1,STU1,STU1,Jordan,PreKindergarten,F,08/13/2020"]
    _write(baseline_dir / "students.csv", [header] + rows)

    with pytest.raises(ValueError) as exc_info:
        CsvStack.load(baseline_dir)

    message = str(exc_info.value)
    assert "Last name" in message
    assert "Student email" in message


def test_engine_added_column_missing_alone_does_not_raise(baseline_dir: Path) -> None:
    """Sanity check: a file missing ONLY its engine-added column (the normal,
    expected state for a fresh SIS export) must still load fine -- Fix 1
    must not turn the documented migration path itself into an error."""

    # students.csv lacks only engine-added columns ("Middle name" + the seven
    # contact columns) -- the normal state of a fresh SIS export.
    stack = CsvStack.load(baseline_dir)
    assert stack.migrated_columns["students.csv"] == ("Middle name",) + schema.CONTACT_COLUMNS


# ---------------------------------------------------------------------------
# apply(): CREATE / UPDATE / DELETE
# ---------------------------------------------------------------------------


def _change(
    filename: str,
    operation: Operation,
    key: dict[str, str],
    after: dict[str, str] | None = None,
    *,
    subject: EventSubject = EventSubject.STUDENT,
) -> Change:
    """A Change for ``apply``.

    ``subject`` matters for students.csv DELETEs and is not decoration: a
    DELETE carrying ``EventSubject.CONTACT`` means "remove this guardian", and
    ``apply`` refuses it when the row is the student's last one (deleting it
    would delete the student too). ``EventSubject.STUDENT`` means "remove this
    student", where deleting the last row is the whole point.
    """

    return Change(
        filename=filename,
        operation=operation,
        key=key,
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_UPDATED,
        event_subject=subject,
        after=after or {},
    )


def test_apply_create_appends_row_with_all_columns_present(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    change = _change(
        "students.csv",
        Operation.CREATE,
        # The full natural key: a brand-new student with no guardians is one
        # row whose "Contact sis id" is blank. Spelled out rather than left to
        # default so the two-column key is visible at the call site.
        {"Student id": "STU3", "Contact sis id": ""},
        {
            "School id": "SCH1",
            "Student number": "STU3",
            "Last name": "New",
            "First name": "Kid",
            "Grade": "1",
            "Gender": "F",
            "DOB": "01/01/2019",
            "Student email": "new.kid@students.tulsaschools-replica.org",
        },
    )
    stack.apply([change])

    students = stack.students()
    assert len(students) == 3
    new_row = students[-1]
    assert tuple(new_row.keys()) == schema.STUDENTS.columns
    assert new_row["Student id"] == "STU3"
    assert new_row["Middle name"] == ""
    assert stack.get("students.csv", ("STU3", ""))["Last name"] == "New"


def test_apply_update_merges_after_into_matched_row(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.apply(
        [_change("students.csv", Operation.UPDATE, {"Student id": "STU1"}, {"Middle name": "Lee"})]
    )
    # ("STU1", "") -- STU1 has no guardians in the baseline stack, so its one
    # row's "Contact sis id" half of the key is blank.
    row = stack.get("students.csv", ("STU1", ""))
    assert row is not None
    assert row["Middle name"] == "Lee"
    # Untouched fields survive.
    assert row["Last name"] == "Barnes"


def test_apply_delete_removes_matching_row(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    assert stack.counts()["students"] == 2
    # EventSubject.STUDENT: deleting a contact-less student's only row IS the
    # intent here, which is why this is allowed where the same delete carrying
    # EventSubject.CONTACT is refused (see the last-row test below).
    stack.apply([_change("students.csv", Operation.DELETE, {"Student id": "STU1"})])
    assert stack.counts()["students"] == 1
    assert stack.get("students.csv", ("STU1", "")) is None
    assert stack.get("students.csv", ("STU2", "")) is not None


def test_apply_update_unresolvable_key_raises_keyerror(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    with pytest.raises(KeyError):
        stack.apply(
            [_change("students.csv", Operation.UPDATE, {"Student id": "NOPE"}, {"Middle name": "X"})]
        )


def test_apply_delete_unresolvable_key_raises_keyerror(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    with pytest.raises(KeyError):
        stack.apply([_change("students.csv", Operation.DELETE, {"Student id": "NOPE"})])


def test_apply_batch_with_one_bad_change_mutates_nothing(baseline_dir: Path) -> None:
    """Atomicity: a batch that would partially succeed must not partially apply."""

    stack = CsvStack.load(baseline_dir)
    before_students = [dict(r) for r in stack.students()]
    before_count = stack.counts()["students"]

    good = _change("students.csv", Operation.UPDATE, {"Student id": "STU1"}, {"Middle name": "Lee"})
    bad = _change("students.csv", Operation.UPDATE, {"Student id": "NOPE"}, {"Middle name": "X"})

    with pytest.raises(KeyError):
        stack.apply([good, bad])

    assert stack.counts()["students"] == before_count
    assert stack.students() == before_students
    assert stack.get("students.csv", ("STU1", ""))["Middle name"] == ""


def test_apply_invalidates_indexes(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.index("students.csv")  # force-build the cache
    stack.apply([_change("students.csv", Operation.DELETE, {"Student id": "STU2"})])
    assert stack.get("students.csv", ("STU2", "")) is None
    assert stack.counts()["students"] == 1


# ---------------------------------------------------------------------------
# counts() / join helpers
# ---------------------------------------------------------------------------


def test_counts_matches_expected_record_counts_per_type(baseline_dir: Path) -> None:
    # Row count == record count for every type here only because no student in
    # the baseline stack has a guardian yet. See
    # test_counts_reports_distinct_students_not_rows for the case where they
    # deliberately diverge.
    stack = CsvStack.load(baseline_dir)
    counts = stack.counts()
    assert counts == {
        "schools": 1,
        "students": 2,
        "teachers": 2,
        "staff": 1,
        "sections": 1,
        "enrollments": 2,
        "contacts": 0,
    }


def test_enrollments_for_student_and_section(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    assert len(stack.enrollments_for_section("SEC1")) == 2
    assert len(stack.enrollments_for_student("STU1")) == 1
    assert stack.enrollments_for_student("STU1")[0]["Section id"] == "SEC1"
    assert stack.enrollments_for_student("NOPE") == []


def test_sections_in_school(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    sections = stack.sections_in_school("SCH1")
    assert len(sections) == 1
    assert sections[0]["Section id"] == "SEC1"


def test_sections_for_teacher_checks_both_teacher_columns(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.apply(
        [_change("sections.csv", Operation.UPDATE, {"Section id": "SEC1"}, {"Teacher 2 id": "TCH2"})]
    )
    assert len(stack.sections_for_teacher("TCH1")) == 1
    assert len(stack.sections_for_teacher("TCH2")) == 1
    assert stack.sections_for_teacher("TCH1")[0]["Section id"] == "SEC1"


def test_students_in_section_joins_enrollments_to_students(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    students = stack.students_in_section("SEC1")
    ids = {s["Student id"] for s in students}
    assert ids == {"STU1", "STU2"}


def test_contacts_for_student(seeded_dir: Path) -> None:
    """Contacts resolve to the right student, and a student with none gets [].

    Retargeted 2026-08-05: this used to CREATE a row in a standalone
    contacts.csv, a file that does not exist in Clever's SFTP spec. The same
    two properties are asserted against the real shape -- guardian rows on
    students.csv -- plus the in-place UPDATE path that adding a FIRST guardian
    has to take (see below).
    """

    stack = CsvStack.load(seeded_dir)

    contacts = stack.contacts_for_student("STU1")
    assert len(contacts) == 3
    assert [c["Contact sis id"] for c in contacts] == ["CON1", "CON2", "CON3"]
    assert contacts[0]["Contact name"] == "Jamie Barnes"

    # STU2's single row has the contact columns blank, so it is not a contact.
    # Returning one empty pseudo-contact here would make every contact-less
    # student look like it had a guardian with no name.
    assert stack.contacts_for_student("STU2") == []

    # Giving STU2 a FIRST guardian is an in-place UPDATE of that blank row, not
    # a new row: a new row would leave STU2 presenting a blank pseudo-contact
    # alongside a real one, which no SIS export would ever produce.
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.UPDATE,
                {"Student id": "STU2", "Contact sis id": ""},
                {
                    "Contact relationship": "Mother",
                    "Contact type": "Parent",
                    "Contact name": "Dana Martinez",
                    "Contact phone": "918-555-0104",
                    "Contact phone type": "Mobile",
                    "Contact email": "dana.martinez@example.com",
                    "Contact sis id": "CON9",
                },
                subject=EventSubject.CONTACT,
            )
        ]
    )

    stu2_contacts = stack.contacts_for_student("STU2")
    assert [c["Contact sis id"] for c in stu2_contacts] == ["CON9"]
    assert stu2_contacts[0]["Contact name"] == "Dana Martinez"
    # STU2 still occupies exactly one row, and STU1 is untouched.
    assert len(stack.student_rows_for("STU2")) == 1
    assert len(stack.contacts_for_student("STU1")) == 3


# ---------------------------------------------------------------------------
# Row-per-contact: one student, N rows (SFTP Instructions v2.1.1)
#
# The tests below cover what the two-column students.csv key and apply()'s
# fan-out / last-row rules exist for. Under the old single-column
# ("Student id",) key, the first of them fails outright: three guardian rows
# collapsed to one index entry, last-one-wins, with the other two guardians
# silently unreachable -- and therefore silently dropped from any code path
# that went through index()/get() rather than the raw row list.
# ---------------------------------------------------------------------------


def test_three_contact_rows_for_one_student_are_three_distinct_index_entries(
    seeded_dir: Path,
) -> None:
    """Reproduction of the old key's data loss: STU1's three guardian rows must
    be three separate entries in index("students.csv"), with none lost."""

    stack = CsvStack.load(seeded_dir)
    index = stack.index("students.csv")

    # 3 rows for STU1 + 1 blank-contact row for STU2 = 4 entries. A collapsing
    # key would give 2 (one per student) and lose CON1/CON2.
    assert len(index) == 4
    assert sorted(index) == ["STU1|CON1", "STU1|CON2", "STU1|CON3", "STU2|"]

    stu1_keys = ["STU1|CON1", "STU1|CON2", "STU1|CON3"]
    # Distinct row OBJECTS, not three references to the surviving sibling.
    assert len({id(index[k]) for k in stu1_keys}) == 3
    assert [index[k]["Contact name"] for k in stu1_keys] == [
        "Jamie Barnes",
        "Robin Barnes",
        "Alice Barnes",
    ]
    # Every guardian in the file is reachable through the index.
    assert len(index) == len(stack.students())

    # get() returns the specific sibling asked for, not an arbitrary one.
    assert stack.get("students.csv", ("STU1", "CON2"))["Contact name"] == "Robin Barnes"
    assert stack.get("students.csv", ("STU1", "CON3"))["Contact phone"] == "918-555-0103"


def test_counts_reports_distinct_students_not_rows(seeded_dir: Path) -> None:
    """``counts()["students"]`` is distinct Student ids; ``contacts`` is derived.

    Load-bearing for the guardrail: reported as a raw row count, seeding the
    real district (33,621 students -> 52,931 student rows) is a +57% move that
    blows through safety.MAX_SCALE_DRIFT and bricks the district mid-seed.
    """

    stack = CsvStack.load(seeded_dir)

    assert len(stack.students()) == 4  # rows on disk: 3 for STU1, 1 for STU2
    counts = stack.counts()
    assert counts["students"] == 2  # ...but only two students
    assert counts["contacts"] == 3  # ...and three guardians, none of them STU2's
    # Contacts are counted by rows carrying a Contact sis id, so STU2's blank
    # row is not miscounted as a fourth guardian.
    assert counts["contacts"] == len(stack.contacts())
    assert len(stack.distinct_students()) == counts["students"]


def test_student_level_edit_fans_out_to_every_row_for_that_student(
    seeded_dir: Path,
) -> None:
    """A student-level edit landing on ONE of three sibling rows would leave
    STU1 presenting two different middle names in a single file -- an ambiguous
    record Clever has no way to resolve, and one no SIS export could produce."""

    stack = CsvStack.load(seeded_dir)
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.UPDATE,
                # Targeting the MIDDLE sibling deliberately: the fan-out has to
                # reach backwards and forwards, not just "the rest of the file".
                {"Student id": "STU1", "Contact sis id": "CON2"},
                {"Middle name": "Lee", "Student email": "jl.barnes@students.tulsaschools-replica.org"},
            )
        ]
    )

    rows = stack.student_rows_for("STU1")
    assert len(rows) == 3
    assert [r["Middle name"] for r in rows] == ["Lee", "Lee", "Lee"]
    assert [r["Student email"] for r in rows] == [
        "jl.barnes@students.tulsaschools-replica.org"
    ] * 3
    # Only the student half was fanned out -- the guardians are still distinct.
    assert [r["Contact sis id"] for r in rows] == ["CON1", "CON2", "CON3"]
    # A different student is not touched.
    assert stack.student_rows_for("STU2")[0]["Middle name"] == ""


def test_contact_level_edit_does_not_fan_out_to_sibling_rows(seeded_dir: Path) -> None:
    """The mirror image: contact columns are exactly what distinguishes one row
    from its siblings, so fanning them out would overwrite the other two
    guardians with a copy of the edited one -- losing them."""

    stack = CsvStack.load(seeded_dir)
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.UPDATE,
                {"Student id": "STU1", "Contact sis id": "CON2"},
                {"Contact email": "robin.b.new@example.com", "Contact phone": "918-555-9999"},
                subject=EventSubject.CONTACT,
            )
        ]
    )

    rows = stack.student_rows_for("STU1")
    assert [r["Contact email"] for r in rows] == [
        "jamie.barnes@example.com",
        "robin.b.new@example.com",
        "alice.barnes@example.com",
    ]
    assert [r["Contact phone"] for r in rows] == [
        "918-555-0101",
        "918-555-9999",
        "918-555-0103",
    ]
    # Contact sis id is never edited (it is what keeps a contact's Clever id
    # stable), so the natural key of the edited row is unchanged.
    assert stack.get("students.csv", ("STU1", "CON2"))["Contact email"] == "robin.b.new@example.com"
    assert stack.counts()["contacts"] == 3


def test_deleting_a_contact_row_with_siblings_leaves_the_student_present(
    seeded_dir: Path,
) -> None:
    """Removing a guardian from a student who has others IS a row delete."""

    stack = CsvStack.load(seeded_dir)
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.DELETE,
                {"Student id": "STU1", "Contact sis id": "CON2"},
                subject=EventSubject.CONTACT,
            )
        ]
    )

    rows = stack.student_rows_for("STU1")
    assert [r["Contact sis id"] for r in rows] == ["CON1", "CON3"]
    assert stack.get("students.csv", ("STU1", "CON2")) is None
    # The STUDENT survives: still two students, one guardian fewer.
    assert stack.counts()["students"] == 2
    assert stack.counts()["contacts"] == 2
    assert rows[0]["Last name"] == "Barnes"


def test_deleting_a_students_last_row_as_a_contact_removal_raises(
    seeded_dir: Path,
) -> None:
    """Deleting the last remaining row deletes the STUDENT, not just the
    guardian: the partner would see users.deleted (Students) plus a vanished
    roster entry. Selection must blank the contact columns in place (an UPDATE)
    instead, and this is enforced in apply() rather than trusted to selection
    because the cost of getting it wrong is deleting a real student record."""

    stack = CsvStack.load(seeded_dir)

    def _remove(sis_id: str) -> None:
        stack.apply(
            [
                _change(
                    "students.csv",
                    Operation.DELETE,
                    {"Student id": "STU1", "Contact sis id": sis_id},
                    subject=EventSubject.CONTACT,
                )
            ]
        )

    _remove("CON1")
    _remove("CON2")
    assert [r["Contact sis id"] for r in stack.student_rows_for("STU1")] == ["CON3"]

    with pytest.raises(ValueError) as exc_info:
        _remove("CON3")

    message = str(exc_info.value)
    assert "STU1" in message
    assert "ONLY row" in message
    # And it really did refuse: STU1 is still present, still with CON3.
    assert stack.counts()["students"] == 2
    assert [r["Contact sis id"] for r in stack.student_rows_for("STU1")] == ["CON3"]
    assert stack.get("students.csv", ("STU1", "CON3")) is not None

    # The prescribed alternative works and keeps the student's row alive.
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.UPDATE,
                {"Student id": "STU1", "Contact sis id": "CON3"},
                schema.blank_contact_fields(),
                subject=EventSubject.CONTACT,
            )
        ]
    )
    assert len(stack.student_rows_for("STU1")) == 1
    assert stack.contacts_for_student("STU1") == []
    assert stack.counts()["students"] == 2
    assert stack.counts()["contacts"] == 0


def test_apply_batch_of_two_deletes_removes_exactly_those_rows(seeded_dir: Path) -> None:
    """Two DELETEs in ONE batch must remove the two rows they named.

    Row positions resolved in apply()'s validation pass go stale the moment the
    first delete shifts the list, so a batch delete has to re-resolve the row
    it is about to remove by identity. Getting this wrong deletes an innocent
    bystander row -- here, a guardian nobody asked to remove.
    """

    stack = CsvStack.load(seeded_dir)
    stack.apply(
        [
            _change(
                "students.csv",
                Operation.DELETE,
                {"Student id": "STU1", "Contact sis id": "CON1"},
                subject=EventSubject.CONTACT,
            ),
            _change(
                "students.csv",
                Operation.DELETE,
                {"Student id": "STU1", "Contact sis id": "CON3"},
                subject=EventSubject.CONTACT,
            ),
        ]
    )

    assert [r["Contact sis id"] for r in stack.student_rows_for("STU1")] == ["CON2"]
    # STU2's row sat after STU1's in the file and must be untouched.
    assert len(stack.student_rows_for("STU2")) == 1
    assert stack.counts()["students"] == 2


# ---------------------------------------------------------------------------
# Fingerprint sample / baseline counts
# ---------------------------------------------------------------------------


def test_fingerprint_sample_contains_expected_domain(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    sample = stack.fingerprint_sample()
    assert 1 <= len(sample) <= 50
    assert any(schema.EXPECTED_DATA_FINGERPRINT in value for value in sample)


# ---------------------------------------------------------------------------
# Fix 2: save() is all-or-nothing across the whole stack.
# ---------------------------------------------------------------------------


def test_save_leaves_target_dir_byte_identical_after_a_write_failure_partway(
    tmp_path: Path, baseline_dir: Path, monkeypatch
) -> None:
    """Simulate an ENOSPC-style failure partway through the stack (writing
    sections.csv, the 5th of the 6 files in schema.ALL_SPECS) and assert the
    target directory -- which already holds a prior, successfully-saved
    stack -- is left exactly as it was, byte for byte. Reproduction: before
    this fix, students.csv (written before sections.csv) was left mutated
    while sections.csv was not, and the NEXT run proceeded from that
    half-mutated stack."""

    stack = CsvStack.load(baseline_dir)
    out_dir = tmp_path / "current"
    stack.save(out_dir)  # first, successful save -- establishes "prior state"

    before = {p.name: p.read_bytes() for p in sorted(out_dir.iterdir())}

    import builtins

    real_open = builtins.open

    def _flaky_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file).name == "sections.csv" and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _flaky_open)

    # Mutate the in-memory stack so a successful save WOULD have visibly
    # changed something -- proves the failure really did block the write,
    # not merely that there was nothing new to write anyway.
    stack.apply(
        [
            Change(
                filename="students.csv",
                operation=Operation.UPDATE,
                key={"Student id": "STU1"},
                bucket=Bucket.SMALL_DAILY,
                expected_event=EventType.USERS_UPDATED,
                event_subject=EventSubject.STUDENT,
                after={"Middle name": "ShouldNotAppear"},
            )
        ]
    )

    with pytest.raises(OSError):
        stack.save(out_dir)

    after = {p.name: p.read_bytes() for p in sorted(out_dir.iterdir())}
    assert after == before, "target directory changed despite a failed, partial save"

    # No leftover staging/backup directories next to the target (the
    # fixture's own "baseline" dir is expected and not part of this check).
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name not in ("current", "baseline")
    ]
    assert leftovers == [], f"stray directory left behind: {leftovers}"


def test_save_promotes_atomically_on_success(tmp_path: Path, baseline_dir: Path) -> None:
    """The normal (non-failing) path still leaves exactly one clean
    directory behind -- no stray staging/backup directories."""

    stack = CsvStack.load(baseline_dir)
    out_dir = tmp_path / "current"
    stack.save(out_dir)
    stack.save(out_dir)  # a second, successful save over the first

    leftovers = [
        p.name for p in tmp_path.iterdir() if p.name not in ("current", "baseline")
    ]
    assert leftovers == []
    assert (out_dir / "students.csv").exists()


def test_snapshot_and_read_baseline_counts_round_trip(tmp_path: Path, baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    path = stack.snapshot_counts(tmp_path)
    assert path.name == "baseline_counts.json"
    loaded = CsvStack.read_baseline_counts(tmp_path)
    assert loaded == stack.counts()
    # Sanity: it's plain JSON, not something exotic.
    assert json.loads(path.read_text()) == stack.counts()
