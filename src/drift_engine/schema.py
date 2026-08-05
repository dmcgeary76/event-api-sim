"""Canonical CSV schema contract for the sandbox drift engine.

Headers here were read from David's actual sandbox export (Tulsa replica,
SFTP user steadfast-backpack-8880) on 2026-07-30 -- they are NOT the generic
Clever spec. Some columns are ADDED by this engine because the stack lacked
any mutable surface for the change categories the project brief requires:

  * students.csv  -> "Middle name"     (drives users.updated (Students))
  * sections.csv  -> "Teacher 2 id"    (drives Friday co-teacher changes)
  * students.csv  -> the seven
                     ``CONTACT_COLUMNS`` (drive users.created/updated/deleted
                                     (Contacts) -- NOT distinct contacts.*
                                     wire events; see models.EventType)

All are optional fields in Clever's SIS CSV spec, so adding them is
schema-legal. See docs/SCHEMA.md for the rationale and the seeding plan.

CONTACTS ARE ROWS ON students.csv, NOT A SEPARATE FILE (confirmed 2026-08-05)
---------------------------------------------------------------------------
An earlier version of this engine wrote a standalone ``contacts.csv``. That
file does not exist in Clever's SFTP spec and would have been ignored (or
rejected as unknown columns) on ingest, meaning none of this engine's
predicted contact events would ever have fired. Corrected against the
official **SFTP Instructions, v2.1.1 (Dec 2025)**, which lists exactly one
set of *unsuffixed* contact columns under students.csv and states the
multi-contact mechanism directly:

    "In order to provide multiple parent/guardian contacts, you may create
    multiple rows for a single student with different contact information."

So the 5-contact-per-student limit is **up to 5 rows sharing one Student id**,
each row carrying one contact's fields. There is NO ``Contact name 2`` /
``Contact email 2`` column convention -- those headers are not real and would
be dropped or rejected.

Do not conflate this with ``sections.csv``, which in the same spec *does* use
numbered suffixes (``Teacher 2 id`` .. ``Teacher 10 id``) for co-teachers.
Two genuinely different patterns in one spec: sections widen, contacts
repeat. All row expansion goes through :func:`expand_contact_rows` so this
assumption lives in exactly one place if it ever needs correcting again.

IMPORTANT: the source files use CRLF line endings and no quoting. Writers must
preserve both, or Clever will see a diff on every single row and the sync will
look like a full-district rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

LINE_TERMINATOR = "\r\n"
ENCODING = "utf-8"


@dataclass(frozen=True)
class FileSpec:
    """Describes one CSV in the stack."""

    filename: str
    #: Column order exactly as written back to SFTP.
    columns: tuple[str, ...]
    #: Columns forming the natural key for this record type.
    key: tuple[str, ...]
    #: Clever record type this file feeds, used for guardrail accounting.
    record_type: str
    #: Columns this engine is allowed to mutate in place.
    mutable: frozenset[str] = field(default_factory=frozenset)

    @property
    def added_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c in ENGINE_ADDED_COLUMNS.get(self.filename, ()))


# ---------------------------------------------------------------------------
# Contacts (guardians) -- rows on students.csv, per SFTP Instructions v2.1.1.
# ---------------------------------------------------------------------------

#: The seven contact columns, in the order the spec lists them. Unsuffixed and
#: singular: one contact per row, additional contacts as additional rows for
#: the same Student id. See the module docstring.
CONTACT_COLUMNS: tuple[str, ...] = (
    "Contact relationship",
    "Contact type",
    "Contact name",
    "Contact phone",
    "Contact phone type",
    "Contact email",
    "Contact sis id",
)

#: The column that makes a contact's Clever id stable. Per Clever's docs, a
#: contact WITH an sis_id keeps its Clever id across phone/email/name changes;
#: a contact WITHOUT one has its identity derived from name+email (or
#: name+phone, or name+type+relationship+phone type), so editing the email
#: changes the identity key itself and the ingest reads it as delete-then-
#: create rather than users.updated. This engine therefore mints one per
#: contact and NEVER edits it -- deliberately absent from ``STUDENTS.mutable``.
CONTACT_SIS_ID_COLUMN = "Contact sis id"

#: Contact fields this engine is allowed to edit in place. Excludes
#: ``Contact sis id`` by design (see above).
CONTACT_MUTABLE_COLUMNS: frozenset[str] = frozenset(
    c for c in CONTACT_COLUMNS if c != CONTACT_SIS_ID_COLUMN
)

#: Hard ceiling from the spec: "using SFTP limits a student's number of
#: contacts to 5 maximum with no custom mappings supported."
MAX_CONTACTS_PER_STUDENT = 5

#: Record type for guardrail/scale accounting. Contacts have no file of their
#: own now, so this is a *derived* record type -- see ``CsvStack.counts``.
CONTACTS_RECORD_TYPE = "contacts"

#: Student-level columns, i.e. everything on a students.csv row that describes
#: the student rather than one of their contacts. These MUST be identical
#: across every row sharing a Student id, otherwise one student presents
#: conflicting values in a single file. ``CsvStack.apply`` fans student-level
#: edits out across sibling rows to keep that true.
STUDENT_LEVEL_COLUMNS: tuple[str, ...] = (
    "School id",
    "Student id",
    "Student number",
    "Last name",
    "First name",
    "Middle name",
    "Grade",
    "Gender",
    "DOB",
    "Student email",
)


# Columns this engine appends to files that came from the SIS export.
ENGINE_ADDED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "students.csv": ("Middle name",) + CONTACT_COLUMNS,
    "sections.csv": ("Teacher 2 id",),
}


SCHOOLS = FileSpec(
    filename="schools.csv",
    columns=(
        "School id",
        "School name",
        "School number",
        "Low grade",
        "High grade",
        "Principal",
        "Principal email",
        "School address",
        "School city",
        "School state",
        "School zip",
        "School phone",
    ),
    key=("School id",),
    record_type="schools",
    # Schools are structural. The engine never touches them.
    mutable=frozenset(),
)

STUDENTS = FileSpec(
    filename="students.csv",
    columns=STUDENT_LEVEL_COLUMNS + CONTACT_COLUMNS,  # contact columns engine-added
    # NOT ("Student id",) -- a student with N contacts occupies N rows, so
    # Student id alone is no longer unique. Keying on it alone would make
    # CsvStack.index() collapse those rows silently (last one wins) and
    # CsvStack.get() return an arbitrary sibling. The sis id disambiguates:
    # it is minted unique per contact, and a student with no contacts has
    # exactly one row with it blank.
    key=("Student id", CONTACT_SIS_ID_COLUMN),
    record_type="students",
    mutable=frozenset({"Middle name", "Student email", "Last name"}) | CONTACT_MUTABLE_COLUMNS,
)

TEACHERS = FileSpec(
    filename="teachers.csv",
    columns=(
        "School id",
        "Teacher id",
        "Teacher number",
        "Teacher email",
        "First name",
        "Last name",
        "Title",
    ),
    key=("Teacher id",),
    record_type="teachers",
    mutable=frozenset({"Teacher email", "Last name", "Title"}),
)

STAFF = FileSpec(
    filename="staff.csv",
    columns=(
        "School id",
        "Staff id",
        "Staff email",
        "First name",
        "Last name",
        "Department",
        "Title",
        "Role",
    ),
    key=("Staff id",),
    record_type="staff",
    mutable=frozenset(),
)

#: NOTE: unlike contacts (which repeat as rows -- see the module docstring),
#: sections genuinely DO use numbered suffixes for co-teachers in the same
#: spec: "Teacher 2 id" through "Teacher 10 id". Only slot 2 is used here.
#: Do not "fix" one of these patterns to match the other; they differ on
#: purpose.
SECTIONS = FileSpec(
    filename="sections.csv",
    columns=(
        "School id",
        "Section id",
        "Teacher id",
        "Teacher 2 id",  # engine-added
        "Name",
        "Section number",
        "Grade",
        "Course name",
        "Course number",
        "Subject",
        "Term name",
    ),
    key=("Section id",),
    record_type="sections",
    mutable=frozenset({"Teacher id", "Teacher 2 id"}),
)

ENROLLMENTS = FileSpec(
    filename="enrollments.csv",
    columns=("School id", "Section id", "Student id"),
    key=("Section id", "Student id"),
    record_type="enrollments",
    # Enrollment rows are added/removed, never edited in place.
    mutable=frozenset(),
)

# NOTE: there is deliberately no CONTACTS FileSpec. Contacts are rows on
# students.csv (SFTP Instructions v2.1.1) -- see the module docstring. The
# previous standalone contacts.csv spec was removed on 2026-08-05 because
# Clever's SFTP ingest has no such file and would have ignored it.


#: Every file in the stack, in a stable order.
ALL_SPECS: tuple[FileSpec, ...] = (
    SCHOOLS,
    STUDENTS,
    TEACHERS,
    STAFF,
    SECTIONS,
    ENROLLMENTS,
)

BY_FILENAME: Mapping[str, FileSpec] = {s.filename: s for s in ALL_SPECS}
BY_RECORD_TYPE: Mapping[str, FileSpec] = {s.record_type: s for s in ALL_SPECS}


# ---------------------------------------------------------------------------
# Observed value domains, for generating plausible new records.
# ---------------------------------------------------------------------------

GRADES: tuple[str, ...] = (
    "PreKindergarten",
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
)
SUBJECTS: tuple[str, ...] = (
    "Arts and music",
    "English/language arts",
    "Homeroom/advisory",
    "Math",
    "PE and health",
    "Science",
    "Social studies",
)
STAFF_EMAIL_DOMAIN = "tulsaschools-replica.org"
STUDENT_EMAIL_DOMAIN = "students.tulsaschools-replica.org"

#: Any sandbox stack the engine writes to must look like this. Used as a
#: secondary safety fingerprint so the engine cannot be aimed at real data.
EXPECTED_DATA_FINGERPRINT = "tulsaschools-replica.org"

CONTACT_TYPES: tuple[str, ...] = ("Parent", "Guardian", "Emergency")
RELATIONSHIPS: tuple[str, ...] = (
    "Mother", "Father", "Grandmother", "Grandfather",
    "Stepmother", "Stepfather", "Aunt", "Uncle", "Guardian",
)
PHONE_TYPES: tuple[str, ...] = ("Mobile", "Home", "Work")


def header_line(spec: FileSpec) -> str:
    """Exact header row this engine writes for ``spec``."""
    return ",".join(spec.columns)


# ---------------------------------------------------------------------------
# Row-per-contact expansion -- the single place this pattern is encoded.
# ---------------------------------------------------------------------------


def contact_fields(row: Mapping[str, str]) -> dict[str, str]:
    """Just the contact half of a students.csv ``row``."""

    return {col: row.get(col, "") for col in CONTACT_COLUMNS}


def student_fields(row: Mapping[str, str]) -> dict[str, str]:
    """Just the student half of a students.csv ``row``."""

    return {col: row.get(col, "") for col in STUDENT_LEVEL_COLUMNS}


def blank_contact_fields() -> dict[str, str]:
    """Contact columns cleared, for a student row carrying no contact."""

    return {col: "" for col in CONTACT_COLUMNS}


def row_carries_contact(row: Mapping[str, str]) -> bool:
    """True when ``row``'s contact half is populated.

    Keyed on the sis id rather than "any contact column is non-empty", because
    this engine always mints an sis id for every contact it creates (see
    ``CONTACT_SIS_ID_COLUMN``). A row with contact values but no sis id did
    not come from this engine.
    """

    return bool(row.get(CONTACT_SIS_ID_COLUMN, ""))


def expand_contact_rows(
    student: Mapping[str, str],
    contacts: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Render one student plus ``contacts`` as the rows students.csv needs.

    This is the ONE function that encodes the row-per-contact pattern from
    SFTP Instructions v2.1.1. If that ever turns out to vary -- and coverage
    on contacts is thin enough that the current PDF should be treated as
    authoritative over general memory -- correcting it here is the whole
    change.

    Rules:

    * A student with no contacts still gets exactly **one** row, with the
      contact columns blank. Dropping the row entirely would delete the
      student.
    * A student with N contacts gets N rows, all sharing identical
      student-level columns, each carrying one contact.
    * At most :data:`MAX_CONTACTS_PER_STUDENT` contacts are emitted; extras
      are refused rather than silently truncated, since silently dropping a
      guardian is exactly the kind of thing that looks like a successful run
      and isn't.
    """

    if len(contacts) > MAX_CONTACTS_PER_STUDENT:
        raise ValueError(
            f"Student {student.get('Student id', '?')!r} would have "
            f"{len(contacts)} contacts; the SFTP spec allows at most "
            f"{MAX_CONTACTS_PER_STUDENT}. Refusing to truncate silently."
        )

    base = student_fields(student)
    if not contacts:
        return [{**base, **blank_contact_fields()}]
    return [{**base, **contact_fields(contact)} for contact in contacts]
