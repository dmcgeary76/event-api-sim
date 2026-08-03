"""Canonical CSV schema contract for the sandbox drift engine.

Headers here were read from David's actual sandbox export (Tulsa replica,
SFTP user steadfast-backpack-8880) on 2026-07-30 -- they are NOT the generic
Clever spec. Three columns/files are ADDED by this engine because the stack
lacked any mutable surface for the change categories the project brief
requires:

  * students.csv  -> "Middle name"  (drives users.updated (Students))
  * sections.csv  -> "Teacher 2 id" (drives Friday co-teacher changes)
  * contacts.csv  -> whole file     (drives users.created/updated/deleted
                                     (Contacts) -- NOT distinct contacts.*
                                     wire events; see models.EventType and
                                     the KNOWN BLOCKER in docs/SCHEMA.md
                                     about this file's shape being wrong)

All three are optional fields in Clever's SIS CSV spec, so adding them is
schema-legal. See docs/SCHEMA.md for the rationale and the seeding plan.

IMPORTANT: the source files use CRLF line endings and no quoting. Writers must
preserve both, or Clever will see a diff on every single row and the sync will
look like a full-district rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

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
    #: True when the engine (not the SIS export) owns this file.
    engine_added: bool = False

    @property
    def added_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c in ENGINE_ADDED_COLUMNS.get(self.filename, ()))


# Columns this engine appends to files that came from the SIS export.
ENGINE_ADDED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "students.csv": ("Middle name",),
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
    columns=(
        "School id",
        "Student id",
        "Student number",
        "Last name",
        "First name",
        "Middle name",  # engine-added
        "Grade",
        "Gender",
        "DOB",
        "Student email",
    ),
    key=("Student id",),
    record_type="students",
    mutable=frozenset({"Middle name", "Student email", "Last name"}),
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

CONTACTS = FileSpec(
    filename="contacts.csv",
    columns=(
        "School id",
        "Student id",
        "Contact id",
        "Contact name",
        "Contact type",
        "Relationship",
        "Phone",
        "Phone type",
        "Email",
        "Sequence",
    ),
    key=("Contact id",),
    record_type="contacts",
    mutable=frozenset({"Email", "Phone", "Contact name", "Relationship", "Phone type"}),
    engine_added=True,
)


#: Every file in the stack, in a stable order.
ALL_SPECS: tuple[FileSpec, ...] = (
    SCHOOLS,
    STUDENTS,
    TEACHERS,
    STAFF,
    SECTIONS,
    ENROLLMENTS,
    CONTACTS,
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
