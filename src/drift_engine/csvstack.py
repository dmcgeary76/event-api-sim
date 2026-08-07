"""In-memory working copy of one district's CSV stack.

``CsvStack`` is the only place in this project that touches the SIS CSVs as
bytes on disk. Everything downstream (selection, guardrail, apply) works with
plain Python dicts and the :class:`~drift_engine.models.Change` type; this
module is where those changes actually become rows and where rows actually
become files again.

Two properties of the source data drive almost every design decision here:

* Clever computes deltas for the Events API by diffing the new CSV export
  against the previous one, essentially row-for-row. If we reorder rows, add
  quoting Clever didn't ask for, or flip CRLF to LF, Clever sees a diff on
  every single row and reports it as a full-district rewrite instead of the
  handful of deliberate changes this engine made. So: preserve row order,
  preserve CRLF, preserve "no quoting unless a value needs it" (QUOTE_MINIMAL
  matches the source, which never needs to quote anything Clever's spec
  allows).
* enrollments.csv has ~104k rows in the real stack. Any helper that maps a
  student/section/teacher id to its enrollments must be backed by a
  dictionary built once, not a per-call linear scan, or selection (which
  calls these helpers repeatedly while building a day's changes) becomes
  the slow part of every run.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from . import schema
from .models import Change, EventSubject, Operation
from .schema import ENGINE_ADDED_COLUMNS, FileSpec

BASELINE_COUNTS_FILENAME = "baseline_counts.json"

#: Files whose emails feed safety.assert_fingerprint_present.
_EMAIL_BEARING_FILES: tuple[tuple[str, str], ...] = (
    ("students.csv", "Student email"),
    ("teachers.csv", "Teacher email"),
    ("staff.csv", "Staff email"),
)

_MAX_FINGERPRINT_SAMPLE = 50


def row_key(spec: FileSpec, row: dict) -> tuple[str, ...]:
    """Natural-key tuple for ``row`` under ``spec``.

    Callers that need a hashable/dict key join this with "|" (see
    ``CsvStack.index``); kept as a tuple here so ``get``/``apply`` can build
    the same key from a ``Change.key`` mapping without re-parsing a string.
    """

    return tuple(row.get(col, "") for col in spec.key)


def _key_str(key_tuple: Sequence[str]) -> str:
    return "|".join(key_tuple)


class CsvStack:
    """The full set of CSVs for one district, loaded into memory.

    Rows are kept as ``list[dict[str, str]]`` in on-disk order per file.
    Order is preserved deliberately -- see the module docstring -- so a
    round trip of load -> save with zero ``Change`` objects applied
    reproduces the original bytes exactly (modulo the two columns this
    engine adds on load).
    """

    def __init__(self, tables: dict[str, list[dict[str, str]]], migrated_columns: dict[str, tuple[str, ...]]):
        self._tables = tables
        #: filename -> tuple of engine-added columns that were missing on
        #: disk and had to be backfilled with "" during load.
        self.migrated_columns: dict[str, tuple[str, ...]] = migrated_columns
        self._indexes: dict[str, dict[str, dict]] = {}
        # Reverse indexes for the join-y query helpers. Built lazily,
        # invalidated alongside ``_indexes``.
        self._enrollments_by_student: dict[str, list[dict]] | None = None
        self._enrollments_by_section: dict[str, list[dict]] | None = None
        self._sections_by_school: dict[str, list[dict]] | None = None
        self._contacts_by_student: dict[str, list[dict]] | None = None
        self._sections_by_teacher: dict[str, list[dict]] | None = None
        self._distinct_students: list[dict] | None = None
        self._student_rows_by_id: dict[str, list[dict]] | None = None

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, directory: Path) -> "CsvStack":
        """Read every file in ``schema.ALL_SPECS`` from ``directory``.

        Note there is no contacts.csv: contacts are rows on students.csv, and
        their seven columns are engine-added, so on a stack this engine has
        never touched they are simply absent from the header and get
        backfilled with "" (tracked in ``migrated_columns``). A stale
        contacts.csv left behind by the pre-2026-08-05 version of this engine
        is ignored on load and disappears on the next ``save``, since save
        promotes a freshly staged directory containing only ``ALL_SPECS``.

        ``schema.OPTIONAL_FILES`` (currently just staff.csv) may legitimately
        be absent, per Clever's own SFTP spec rather than anything this
        engine owns -- an absent optional file loads as zero rows.

        Every other file is a real SIS export and must be read exactly as
        Clever produced it: CRLF line endings, utf-8, no quoting. Passing
        ``newline=""`` to ``open`` is required so Python's csv module -- not
        universal-newline translation -- is the thing that decides what a
        line ending is; without it, CRLF gets silently turned into LF on
        read and we'd have no way to reproduce it on save.
        """

        tables: dict[str, list[dict[str, str]]] = {}
        migrated: dict[str, tuple[str, ...]] = {}

        for spec in schema.ALL_SPECS:
            path = directory / spec.filename
            if spec.filename in schema.OPTIONAL_FILES and not path.exists():
                # Per Clever's own spec, not an engine-owned exception -- see
                # schema.OPTIONAL_FILES. A real export may simply not have
                # this file (e.g. a district with no staff records at all).
                tables[spec.filename] = []
                continue
            with open(path, "r", encoding=schema.ENCODING, newline="") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []
                unknown = [c for c in fieldnames if c not in spec.columns]
                if unknown:
                    raise ValueError(
                        f"{spec.filename}: unrecognized column(s) {unknown} not in "
                        f"schema.{spec.filename.split('.')[0].upper()}.columns. Refusing to "
                        "load a file with columns this engine does not understand -- "
                        "silently dropping them would lose data on the next save."
                    )

                missing_added = tuple(
                    c for c in ENGINE_ADDED_COLUMNS.get(spec.filename, ()) if c not in fieldnames
                )

                # Fix 1: a required (non-engine-added) column silently absent
                # from the header must never be treated as "backfill with ''
                # like an engine-added column" -- that previously let a real
                # SIS field (e.g. "Student email") be dropped from an export,
                # loaded anyway with every row's value blanked, and written
                # back out that way on the next save. Only columns this
                # engine itself owns (``ENGINE_ADDED_COLUMNS``) are allowed to
                # be missing on load; anything else missing is a hard error
                # naming the file and the column(s).
                missing_required = tuple(
                    c
                    for c in spec.columns
                    if c not in fieldnames and c not in ENGINE_ADDED_COLUMNS.get(spec.filename, ())
                )
                if missing_required:
                    raise ValueError(
                        f"{spec.filename}: required column(s) {list(missing_required)} are "
                        "missing from the header. This engine only backfills columns it "
                        f"itself owns ({list(ENGINE_ADDED_COLUMNS.get(spec.filename, ()))}) -- "
                        "any other missing column means a real SIS field was dropped from "
                        "this export, and silently writing it back out blank would erase "
                        "that data (e.g. every student's email) on the next save. Restore "
                        "the column in the source export before loading this file."
                    )

                rows: list[dict[str, str]] = []
                for raw_row in reader:
                    row = {col: raw_row.get(col, "") or "" for col in spec.columns}
                    rows.append(row)

                tables[spec.filename] = rows
                if missing_added:
                    migrated[spec.filename] = missing_added

        return cls(tables, migrated)

    def save(self, directory: Path) -> list[Path]:
        """Write every spec's file back to ``directory``, all-or-nothing.

        Fix 2: this used to write each file to a ``<name>.tmp`` sibling and
        ``os.replace`` it into place ONE FILE AT A TIME -- atomic *per file*,
        but with no transaction across the whole stack. A failure partway
        through (e.g. ENOSPC while writing sections.csv) left every file
        written so far mutated and everything after it untouched, so
        ``current/`` ended up in a state that never existed as a deliberate
        save -- and the NEXT run then proceeded from that half-mutated stack
        as if it were normal.

        Now every file is written into a fresh staging directory (a sibling
        of ``directory``, on the same filesystem) first. If anything raises
        while writing to staging, staging is deleted and the exception
        propagates -- ``directory`` itself is never touched, so a failed
        save leaves it byte-identical to how it was before the call. Only
        once EVERY file has been written successfully is the whole stack
        promoted into place: the current ``directory`` (if it exists) is
        renamed out of the way, the staging directory is renamed into
        ``directory``'s place, and the old directory is then removed. Both
        renames are same-filesystem and therefore atomic and effectively
        instantaneous, so the window in which neither the old nor the new
        stack is at ``directory`` is vanishingly small -- a vast improvement
        over the previous per-file scheme, where that window spanned the
        entire multi-file write.

        A useful side effect of promoting a freshly staged directory: any file
        NOT in ``schema.ALL_SPECS`` is dropped. That is how a stale
        contacts.csv from the pre-2026-08-05 version of this engine cleans
        itself up locally. It was never pushed live (the shape was blocked
        before first push), so there is no remote copy to worry about.
        """

        directory = Path(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{directory.name}.", suffix=".staging", dir=str(directory.parent)
            )
        )
        # tempfile.mkdtemp defaults to 0700 (owner-only); match the more
        # permissive mode a plain mkdir(parents=True) would have produced so
        # promoting staging into ``directory``'s place doesn't quietly change
        # who can read the district's working stack.
        with contextlib.suppress(OSError):
            os.chmod(staging, 0o755)

        written: list[Path] = []
        try:
            for spec in schema.ALL_SPECS:
                rows = self._tables.get(spec.filename, [])
                if spec.filename in schema.OPTIONAL_FILES and not rows:
                    # Per Clever's own spec, staff.csv need not exist at all
                    # for a district with no staff records; skip rather than
                    # write a header-only file that only exists because this
                    # engine happened to touch the directory once. Every
                    # non-optional file is always written, even with zero
                    # rows -- omitting one of THOSE reads to Clever as every
                    # row having been deleted, and would be rejected outright
                    # by sftp_push._assert_stack_complete.
                    continue
                staged_path = staging / spec.filename
                with open(staged_path, "w", encoding=schema.ENCODING, newline="") as fh:
                    writer = csv.DictWriter(
                        fh,
                        fieldnames=list(spec.columns),
                        lineterminator=schema.LINE_TERMINATOR,
                        quoting=csv.QUOTE_MINIMAL,
                    )
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({col: row.get(col, "") for col in spec.columns})

                written.append(directory / spec.filename)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # --- Promote: swap the whole directory in, atomically. -------------
        # Everything above has already succeeded, so nothing below this line
        # can leave a half-written CSV -- only a fully-old or fully-new
        # ``directory``.
        backup: Path | None = None
        if directory.exists():
            backup = directory.parent / f".{directory.name}.replaced-{secrets.token_hex(8)}"
            os.replace(directory, backup)
        try:
            os.replace(staging, directory)
        except BaseException:
            # Best-effort rollback: restore the original directory exactly as
            # it was before this call, then let the exception propagate.
            if backup is not None:
                with contextlib.suppress(OSError):
                    os.replace(backup, directory)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)

        return written

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index(self, filename: str) -> dict[str, dict]:
        """Key-string -> row mapping for ``filename``, built lazily.

        Keyed by the spec's natural key columns joined with "|" (see
        ``row_key``); a plain string is used rather than a tuple so this can
        double as a normal dict without needing a custom hashable wrapper.
        """

        if filename not in self._indexes:
            spec = schema.BY_FILENAME[filename]
            self._indexes[filename] = {
                _key_str(row_key(spec, row)): row for row in self._tables.get(filename, [])
            }
        return self._indexes[filename]

    def get(self, filename: str, key_tuple: Sequence[str]) -> dict | None:
        return self.index(filename).get(_key_str(key_tuple))

    def counts(self) -> dict[str, int]:
        """record_type -> record count, for the guardrail and safety checks.

        NOT a raw row count for students, and this distinction is load-bearing.
        Contacts are rows on students.csv (see ``schema``), so a district
        where every student has 1-2 guardians has far more student *rows* than
        students. ``estimate-seed`` puts the real stack at 33,621 students and
        52,931 expected contacts, i.e. seeding takes students.csv from 33,621
        rows to 52,931 -- a +57% move. Reported as a raw row count that would
        blow straight through ``safety.MAX_SCALE_DRIFT`` (25%), and because a
        stale baseline is a hard SafetyViolation rather than a silent
        re-anchor, the district would be bricked mid-seed until someone
        re-baselined by hand.

        So ``students`` counts **distinct Student id** (flat across seeding,
        which is the honest answer to "is this still the same district?"), and
        ``contacts`` is a derived count of rows carrying a contact. Contacts
        have no file of their own to count rows in.
        """

        counts = {
            spec.record_type: len(self._tables.get(spec.filename, []))
            for spec in schema.ALL_SPECS
        }
        student_rows = self._tables.get(schema.STUDENTS.filename, [])
        counts["students"] = len({row.get("Student id", "") for row in student_rows})
        counts[schema.CONTACTS_RECORD_TYPE] = sum(
            1 for row in student_rows if schema.row_carries_contact(row)
        )
        return counts

    def _invalidate(self) -> None:
        self._indexes.clear()
        self._enrollments_by_student = None
        self._enrollments_by_section = None
        self._sections_by_school = None
        self._contacts_by_student = None
        self._sections_by_teacher = None
        self._distinct_students = None
        self._student_rows_by_id = None

    # ------------------------------------------------------------------
    # Bulk row access
    # ------------------------------------------------------------------

    def students(self) -> list[dict]:
        """Every students.csv ROW, contacts included.

        A student with N contacts appears N times. Selection almost always
        wants :meth:`distinct_students` instead -- picking at random from this
        list weights each student by their contact count, so a student with 3
        guardians is 3x more likely to be chosen than one with 1.
        """

        return self._tables.get(schema.STUDENTS.filename, [])

    def distinct_students(self) -> list[dict]:
        """One row per student, for unbiased selection and student-level reads.

        Returns the first row for each Student id in file order. Student-level
        columns are identical across a student's rows (``apply`` enforces
        this), so which sibling is returned does not matter for those columns.
        The contact half of the returned row is whichever contact happens to
        sit on that first row; use :meth:`contacts_for_student` to see all of
        them.
        """

        if self._distinct_students is None:
            seen: set[str] = set()
            result: list[dict] = []
            for row in self.students():
                sid = row.get("Student id", "")
                if sid not in seen:
                    seen.add(sid)
                    result.append(row)
            self._distinct_students = result
        return self._distinct_students

    def student_rows_for(self, student_id: str) -> list[dict]:
        """Every students.csv row sharing ``student_id``, in file order."""

        if self._student_rows_by_id is None:
            index: dict[str, list[dict]] = {}
            for row in self.students():
                index.setdefault(row.get("Student id", ""), []).append(row)
            self._student_rows_by_id = index
        return self._student_rows_by_id.get(student_id, [])

    def teachers(self) -> list[dict]:
        return self._tables.get(schema.TEACHERS.filename, [])

    def staff(self) -> list[dict]:
        return self._tables.get(schema.STAFF.filename, [])

    def schools(self) -> list[dict]:
        return self._tables.get(schema.SCHOOLS.filename, [])

    def sections(self) -> list[dict]:
        return self._tables.get(schema.SECTIONS.filename, [])

    def contacts(self) -> list[dict]:
        """Every populated contact in the district, as students.csv rows.

        A projection over students.csv, not a table of its own -- contacts
        stopped being a separate file on 2026-08-05 (see ``schema``).
        """

        return [row for row in self.students() if schema.row_carries_contact(row)]

    def enrollments(self) -> list[dict]:
        return self._tables.get(schema.ENROLLMENTS.filename, [])

    # ------------------------------------------------------------------
    # Join / reverse-index helpers
    #
    # enrollments.csv is ~104k rows in the real stack, so every one of these
    # builds a dict once (on first use after load/apply) rather than
    # filtering the full list on every call. Selection logic calls these
    # helpers many times per run.
    # ------------------------------------------------------------------

    def _enrollment_indexes(self) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        if self._enrollments_by_student is None or self._enrollments_by_section is None:
            by_student: dict[str, list[dict]] = {}
            by_section: dict[str, list[dict]] = {}
            for row in self.enrollments():
                by_student.setdefault(row.get("Student id", ""), []).append(row)
                by_section.setdefault(row.get("Section id", ""), []).append(row)
            self._enrollments_by_student = by_student
            self._enrollments_by_section = by_section
        return self._enrollments_by_student, self._enrollments_by_section

    def enrollments_for_student(self, student_id: str) -> list[dict]:
        by_student, _ = self._enrollment_indexes()
        return by_student.get(student_id, [])

    def enrollments_for_section(self, section_id: str) -> list[dict]:
        _, by_section = self._enrollment_indexes()
        return by_section.get(section_id, [])

    def sections_in_school(self, school_id: str) -> list[dict]:
        if self._sections_by_school is None:
            index: dict[str, list[dict]] = {}
            for row in self.sections():
                index.setdefault(row.get("School id", ""), []).append(row)
            self._sections_by_school = index
        return self._sections_by_school.get(school_id, [])

    def contacts_for_student(self, student_id: str) -> list[dict]:
        """This student's populated contact rows (0 to MAX_CONTACTS_PER_STUDENT).

        A student with no guardians has one students.csv row with the contact
        columns blank; that row is correctly excluded here, so this returns []
        rather than one empty pseudo-contact.
        """

        if self._contacts_by_student is None:
            index: dict[str, list[dict]] = {}
            for row in self.contacts():
                index.setdefault(row.get("Student id", ""), []).append(row)
            self._contacts_by_student = index
        return self._contacts_by_student.get(student_id, [])

    def sections_for_teacher(self, teacher_id: str) -> list[dict]:
        """Sections where ``teacher_id`` is the primary or co-teacher."""

        if self._sections_by_teacher is None:
            index: dict[str, list[dict]] = {}
            for row in self.sections():
                for col in ("Teacher id", "Teacher 2 id"):
                    tid = row.get(col, "")
                    if tid:
                        index.setdefault(tid, []).append(row)
            self._sections_by_teacher = index
        return self._sections_by_teacher.get(teacher_id, [])

    def students_in_section(self, section_id: str) -> list[dict]:
        """Students enrolled in ``section_id``, via the enrollments join.

        One row per student, not one per contact row -- resolved through
        ``student_rows_for`` rather than ``index()``, because the students.csv
        natural key is now (Student id, Contact sis id) and an enrollment row
        only carries the Student id half.
        """

        result = []
        for enrollment in self.enrollments_for_section(section_id):
            rows = self.student_rows_for(enrollment.get("Student id", ""))
            if rows:
                result.append(rows[0])
        return result

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(self, changes: Sequence[Change]) -> None:
        """Apply ``changes`` to the in-memory tables.

        Validated in two passes: first every change is resolved against the
        *current* state without mutating anything, then the mutations are
        performed. This makes the batch atomic in effect -- if change #47 of
        50 references a key that doesn't exist, changes 1-46 never happened
        either, so a caller that catches the KeyError and aborts the run
        never has to reason about a half-applied CSV stack.
        """

        # Pass 1: resolve every change against the current index state.
        resolved: list[tuple[Change, FileSpec, list[dict], dict | None, int]] = []
        # Track keys created earlier in this same batch so a CREATE followed
        # by an UPDATE/DELETE to the same key within one batch resolves too.
        pending_creates: dict[str, set[str]] = {}

        for change in changes:
            spec = schema.BY_FILENAME.get(change.filename)
            if spec is None:
                raise KeyError(f"Unknown file {change.filename!r} in change {change!r}")

            rows = self._tables.get(change.filename, [])
            key_tuple = tuple(change.key.get(col, "") for col in spec.key)
            key_str = _key_str(key_tuple)
            existing_index = self.index(change.filename)
            batch_keys = pending_creates.setdefault(change.filename, set())

            if change.operation is Operation.CREATE:
                if key_str in existing_index or key_str in batch_keys:
                    raise KeyError(
                        f"CREATE change for {change.filename} key {change.key!r} "
                        "already exists; refusing to create a duplicate row."
                    )
                batch_keys.add(key_str)
                resolved.append((change, spec, rows, None, -1))
                continue

            row = existing_index.get(key_str)
            if row is None and key_str not in batch_keys:
                raise KeyError(
                    f"{change.operation.value.upper()} change for {change.filename} "
                    f"key {change.key!r} does not resolve to an existing row."
                )
            position = -1
            if row is not None:
                # Identity-based scan (not list.index, which would compare
                # dict contents field-by-field) -- cheaper, and correct even
                # in the pathological case of two rows with identical values.
                position = next(i for i, r in enumerate(rows) if r is row)
            resolved.append((change, spec, rows, row, position))

        # Pass 2: mutate. Nothing above raised, so every change is safe to apply.
        for change, spec, rows, row, position in resolved:
            if change.operation is Operation.CREATE:
                new_row = {col: "" for col in spec.columns}
                new_row.update(change.after)
                for col, val in change.key.items():
                    new_row[col] = val
                rows.append(new_row)
            elif change.operation is Operation.UPDATE:
                assert row is not None
                row.update(change.after)
                if change.filename == schema.STUDENTS.filename:
                    self._fan_out_student_columns(rows, row, change.after)
            elif change.operation is Operation.DELETE:
                assert row is not None
                if change.filename == schema.STUDENTS.filename:
                    self._assert_not_last_student_row(rows, row, change)
                # Re-resolve the position by identity HERE rather than reusing
                # the one captured in pass 1. Pass-1 positions go stale the
                # moment an earlier DELETE in the same batch shifts the list:
                # deleting rows 1 and 3 of [A,B,C,D] by stale index removes B
                # (correct) and then index 3 of a now-3-element list
                # (IndexError) -- or, in a longer file, an innocent bystander
                # row nobody asked to delete. Two deletes on one file in one
                # batch is ordinary (two guardians removed from one student,
                # two enrollments dropped from one section).
                current = next((i for i, r in enumerate(rows) if r is row), None)
                if current is None:  # pragma: no cover - defensive
                    raise KeyError(
                        f"DELETE change for {change.filename} key {change.key!r} "
                        "resolved during validation but its row is no longer in the "
                        "table; refusing to delete by a stale position."
                    )
                del rows[current]
            else:  # pragma: no cover - Operation is an exhaustive enum
                raise ValueError(f"Unknown operation {change.operation!r}")

        self._invalidate()

    @staticmethod
    def _fan_out_student_columns(
        rows: list[dict], target: dict, after: Mapping[str, str]
    ) -> None:
        """Copy student-level edits from ``target`` to its sibling rows.

        A student with N contacts occupies N rows carrying identical
        student-level columns. An edit to ``Middle name`` or ``Student email``
        that landed on only one of them would leave that student presenting
        two different values for the same field in one file -- which Clever
        would read as an ambiguous record, and which no SIS export would ever
        produce. Contact-level columns are deliberately NOT fanned out: those
        are what distinguishes one row from its siblings.
        """

        student_columns = {
            col: val for col, val in after.items() if col in schema.STUDENT_LEVEL_COLUMNS
        }
        if not student_columns:
            return
        student_id = target.get("Student id", "")
        for sibling in rows:
            if sibling is not target and sibling.get("Student id", "") == student_id:
                sibling.update(student_columns)

    @staticmethod
    def _assert_not_last_student_row(rows: list[dict], row: dict, change: Change) -> None:
        """Refuse to delete a student's only row while removing a contact.

        Removing a guardian is a row delete -- unless it's the student's last
        remaining row, in which case deleting it deletes the STUDENT too. That
        is never what a contact removal means, and it would show up on the
        partner's feed as ``users.deleted (Students)`` plus a vanished roster
        entry. Selection must blank the contact columns in place instead (an
        UPDATE), keeping the student's single row alive. Enforced here rather
        than trusted to selection, because the cost of getting it wrong is
        deleting a real student record.
        """

        student_id = row.get("Student id", "")
        siblings = sum(1 for r in rows if r.get("Student id", "") == student_id)
        if siblings > 1:
            return
        if change.event_subject is EventSubject.CONTACT:
            raise ValueError(
                f"DELETE change for students.csv key {change.key!r} would remove "
                f"student {student_id!r}'s ONLY row while trying to remove a "
                "contact, deleting the student along with the guardian. Blank "
                "the contact columns in place instead (Operation.UPDATE)."
            )

    # ------------------------------------------------------------------
    # Safety-module support
    # ------------------------------------------------------------------

    def fingerprint_sample(self, limit: int = _MAX_FINGERPRINT_SAMPLE) -> list[str]:
        """Up to ``limit`` email values, for ``safety.assert_fingerprint_present``.

        Only a modest sample is needed -- the safety check just needs one
        value containing the expected domain, so scanning the full 33k
        students to build this list on every run would be wasted work.
        """

        sample: list[str] = []
        for filename, email_col in _EMAIL_BEARING_FILES:
            for row in self._tables.get(filename, []):
                value = row.get(email_col, "")
                if value:
                    sample.append(value)
                    if len(sample) >= limit:
                        return sample
        return sample

    def snapshot_counts(self, directory: Path) -> Path:
        """Write current ``counts()`` to ``baseline_counts.json`` in ``directory``.

        This is the baseline ``safety.assert_scale_sane`` compares future
        runs against, so it should be written once, right after a stack is
        first accepted as a legitimate sandbox target -- not on every run.
        """

        path = directory / BASELINE_COUNTS_FILENAME
        path.write_text(json.dumps(self.counts(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def read_baseline_counts(directory: Path) -> dict[str, int]:
        """Read back the counts written by ``snapshot_counts``."""

        path = directory / BASELINE_COUNTS_FILENAME
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
