"""Tests for drift_engine.csvstack.

All fixtures are small synthetic CSVs written into ``tmp_path`` -- never the
real 33k-row baseline stack, so these tests stay fast and don't depend on
anything outside the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drift_engine import schema
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, Change, EventType, Operation

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
            # Deliberately WITHOUT "Middle name" -- matches the real export.
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


@pytest.fixture()
def baseline_dir(tmp_path: Path) -> Path:
    d = tmp_path / "baseline"
    _write_baseline_stack(d)
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


def test_contacts_csv_not_written_when_empty(tmp_path: Path, baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    out_dir = tmp_path / "out"
    written = stack.save(out_dir)
    names = {p.name for p in written}
    assert "contacts.csv" not in names
    assert not (out_dir / "contacts.csv").exists()


def test_contacts_csv_absent_on_disk_loads_as_empty(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    assert stack.contacts() == []
    assert stack.counts()["contacts"] == 0


# ---------------------------------------------------------------------------
# Engine-added column migration
# ---------------------------------------------------------------------------


def test_migration_adds_engine_columns_with_empty_values_and_correct_order(
    baseline_dir: Path,
) -> None:
    stack = CsvStack.load(baseline_dir)

    assert stack.migrated_columns["students.csv"] == ("Middle name",)
    assert stack.migrated_columns["sections.csv"] == ("Teacher 2 id",)

    for row in stack.students():
        assert row["Middle name"] == ""
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

    stack = CsvStack.load(baseline_dir)  # students.csv lacks "Middle name" only
    assert stack.migrated_columns["students.csv"] == ("Middle name",)


# ---------------------------------------------------------------------------
# apply(): CREATE / UPDATE / DELETE
# ---------------------------------------------------------------------------


def _change(
    filename: str,
    operation: Operation,
    key: dict[str, str],
    after: dict[str, str] | None = None,
) -> Change:
    return Change(
        filename=filename,
        operation=operation,
        key=key,
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_UPDATED,
        after=after or {},
    )


def test_apply_create_appends_row_with_all_columns_present(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    change = _change(
        "students.csv",
        Operation.CREATE,
        {"Student id": "STU3"},
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
    assert stack.get("students.csv", ("STU3",))["Last name"] == "New"


def test_apply_update_merges_after_into_matched_row(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.apply(
        [_change("students.csv", Operation.UPDATE, {"Student id": "STU1"}, {"Middle name": "Lee"})]
    )
    row = stack.get("students.csv", ("STU1",))
    assert row is not None
    assert row["Middle name"] == "Lee"
    # Untouched fields survive.
    assert row["Last name"] == "Barnes"


def test_apply_delete_removes_matching_row(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    assert stack.counts()["students"] == 2
    stack.apply([_change("students.csv", Operation.DELETE, {"Student id": "STU1"})])
    assert stack.counts()["students"] == 1
    assert stack.get("students.csv", ("STU1",)) is None
    assert stack.get("students.csv", ("STU2",)) is not None


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
    assert stack.get("students.csv", ("STU1",))["Middle name"] == ""


def test_apply_invalidates_indexes(baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.index("students.csv")  # force-build the cache
    stack.apply([_change("students.csv", Operation.DELETE, {"Student id": "STU2"})])
    assert stack.get("students.csv", ("STU2",)) is None
    assert stack.counts()["students"] == 1


# ---------------------------------------------------------------------------
# counts() / join helpers
# ---------------------------------------------------------------------------


def test_counts_matches_row_counts_per_record_type(baseline_dir: Path) -> None:
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


def test_contacts_for_student(tmp_path: Path, baseline_dir: Path) -> None:
    stack = CsvStack.load(baseline_dir)
    stack.apply(
        [
            _change(
                "contacts.csv",
                Operation.CREATE,
                {"Contact id": "CON1"},
                {
                    "School id": "SCH1",
                    "Student id": "STU1",
                    "Contact name": "Jamie Barnes",
                    "Contact type": "Parent",
                    "Relationship": "Mother",
                    "Phone": "555-0101",
                    "Phone type": "Mobile",
                    "Email": "jamie.barnes@example.com",
                    "Sequence": "1",
                },
            )
        ]
    )
    contacts = stack.contacts_for_student("STU1")
    assert len(contacts) == 1
    assert contacts[0]["Contact id"] == "CON1"
    assert stack.contacts_for_student("STU2") == []


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
    sections.csv, the 5th of 7 files in schema.ALL_SPECS) and assert the
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
