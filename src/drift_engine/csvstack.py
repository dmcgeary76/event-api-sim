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
from typing import Sequence

from . import schema
from .models import Change, Operation
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

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, directory: Path) -> "CsvStack":
        """Read every file in ``schema.ALL_SPECS`` from ``directory``.

        contacts.csv is engine-owned (``FileSpec.engine_added``) and will not
        exist for a district that has never had a contacts drift run yet;
        that is not an error, it just means we start with zero contacts.

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
            if spec.engine_added and not path.exists():
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

        contacts.csv is only written if it has rows, so a district that has
        never had a contact created doesn't get an empty engine-owned file
        cluttering the SFTP directory.
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
                if spec.engine_added and not rows:
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
        """record_type -> row count, for the guardrail and safety checks."""

        return {
            spec.record_type: len(self._tables.get(spec.filename, []))
            for spec in schema.ALL_SPECS
        }

    def _invalidate(self) -> None:
        self._indexes.clear()
        self._enrollments_by_student = None
        self._enrollments_by_section = None
        self._sections_by_school = None
        self._contacts_by_student = None
        self._sections_by_teacher = None

    # ------------------------------------------------------------------
    # Bulk row access
    # ------------------------------------------------------------------

    def students(self) -> list[dict]:
        return self._tables.get(schema.STUDENTS.filename, [])

    def teachers(self) -> list[dict]:
        return self._tables.get(schema.TEACHERS.filename, [])

    def staff(self) -> list[dict]:
        return self._tables.get(schema.STAFF.filename, [])

    def schools(self) -> list[dict]:
        return self._tables.get(schema.SCHOOLS.filename, [])

    def sections(self) -> list[dict]:
        return self._tables.get(schema.SECTIONS.filename, [])

    def contacts(self) -> list[dict]:
        return self._tables.get(schema.CONTACTS.filename, [])

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
        """Students enrolled in ``section_id``, via the enrollments join."""

        student_index = self.index(schema.STUDENTS.filename)
        result = []
        for enrollment in self.enrollments_for_section(section_id):
            student = student_index.get(_key_str((enrollment.get("Student id", ""),)))
            if student is not None:
                result.append(student)
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
            elif change.operation is Operation.DELETE:
                assert row is not None
                del rows[position]
            else:  # pragma: no cover - Operation is an exhaustive enum
                raise ValueError(f"Unknown operation {change.operation!r}")

        self._invalidate()

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
