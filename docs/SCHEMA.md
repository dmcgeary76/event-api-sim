# CSV schema contract

This is the exact CSV contract the engine reads and writes, as defined in
[`src/drift_engine/schema.py`](../src/drift_engine/schema.py). Headers were
read from David's actual sandbox export (Tulsa replica, SFTP user
`steadfast-backpack-8880`) on 2026-07-30 — this is **not** a generic Clever
SIS CSV layout; column names, order, and which files exist are specific to
this sandbox's real export.

Three columns/files are **added by this engine** and did not exist in the
original export. They're covered in their own section below.

## Corrected event types (2026-08-03)

The project brief (§3) asserted that contacts had their own distinct event
lifecycle — `contacts.created` / `contacts.updated` / `contacts.deleted` —
as if they were separate types on Clever's wire protocol. **That assumption
was factually wrong**, verified against Clever's live dev docs on
2026-08-03: [Events API](https://dev.clever.com/docs/events-api) /
[Contacts & guardians](https://dev.clever.com/docs/contacts-guardians). In
API v3.x, contacts (guardians), students, teachers, staff, and district
admins are all `users` objects on the wire — the only object-level events
are `users.created` / `users.updated` / `users.deleted`, with role carried
in the object's own `roles` node. Section membership changes remain their
own `sections.*` events, unaffected. Every reference to `contacts.*` or
`teachers.created` below has been corrected to the real wire event plus a
role label (e.g. `users.updated (Contacts)`) — see
`src/drift_engine/models.py`'s `EventType`/`EventSubject` for the
implementation and `Change.expected_event_label` for how the two combine.

## The seven files

For each file: exact column order as the engine writes it, the natural key
used to look up/apply changes, which columns the engine is allowed to
mutate, and the Clever record type it feeds (used for guardrail accounting).

### schools.csv

| | |
|---|---|
| Columns | `School id, School name, School number, Low grade, High grade, Principal, Principal email, School address, School city, School state, School zip, School phone` |
| Natural key | `School id` |
| Record type | `schools` |
| Mutable columns | **None.** Schools are structural; the engine never touches this file. |

### students.csv

| | |
|---|---|
| Columns | `School id, Student id, Student number, Last name, First name, Middle name, Grade, Gender, DOB, Student email` |
| Natural key | `Student id` |
| Record type | `students` |
| Mutable columns | `Middle name`, `Student email`, `Last name` |

`Middle name` is engine-added (see below). `Last name` is mutable in the
schema but is not currently written to by any selection/seed logic in this
build.

### teachers.csv

| | |
|---|---|
| Columns | `School id, Teacher id, Teacher number, Teacher email, First name, Last name, Title` |
| Natural key | `Teacher id` |
| Record type | `teachers` |
| Mutable columns | `Teacher email`, `Last name`, `Title` |

New rows are also **created** here on Fridays (`users.created (Teachers)` —
see below on why this is not a distinct `teachers.created` event) — see
`selection._big_teacher`. `Teacher email`/`Last name`/`Title` are mutable in
the schema but are not currently written to by any selection logic in this
build; only row creation happens today.

### staff.csv

| | |
|---|---|
| Columns | `School id, Staff id, Staff email, First name, Last name, Department, Title, Role` |
| Natural key | `Staff id` |
| Record type | `staff` |
| Mutable columns | **None.** Not touched by this engine at all — present in the stack, used only as a source of email samples for the safety fingerprint check. |

### sections.csv

| | |
|---|---|
| Columns | `School id, Section id, Teacher id, Teacher 2 id, Name, Section number, Grade, Course name, Course number, Subject, Term name` |
| Natural key | `Section id` |
| Record type | `sections` |
| Mutable columns | `Teacher id`, `Teacher 2 id` |

`Teacher 2 id` is engine-added (see below). Both mutable columns drive the
Friday `big_teacher` bucket (co-teacher swap, primary teacher reassignment).
Section rows are also implicitly affected by enrollment changes (see
`enrollments.csv`), which is what actually produces `sections.updated` for
student moves — Clever surfaces enrollment membership as a property of the
section, not the user (brief §3).

### enrollments.csv

| | |
|---|---|
| Columns | `School id, Section id, Student id` |
| Natural key | `Section id, Student id` (composite) |
| Record type | `enrollments` |
| Mutable columns | **None** — rows are only ever added or removed (a student "move" is a paired DELETE + CREATE), never edited in place. |

~104,000 rows in the real sandbox stack. `CsvStack` builds `Student id` ->
enrollments and `Section id` -> enrollments reverse indexes once per load,
since selection logic calls these lookups repeatedly per run.

### contacts.csv

| | |
|---|---|
| Columns | `School id, Student id, Contact id, Contact name, Contact type, Relationship, Phone, Phone type, Email, Sequence` |
| Natural key | `Contact id` |
| Record type | `contacts` |
| Mutable columns | `Email`, `Phone`, `Contact name`, `Relationship`, `Phone type` |

**This entire file is engine-added** — see below. Drives all three of the
contact lifecycle events: `users.created (Contacts)` / `users.updated
(Contacts)` / `users.deleted (Contacts)` — **not** distinct
`contacts.created`/`contacts.updated`/`contacts.deleted` events, which do not
exist on Clever's real Events API. See "Corrected event types" below.

> **KNOWN BLOCKER — this file is very likely the wrong shape.** Per
> Clever's SIS CSV docs
> ([Contacts & guardians](https://dev.clever.com/docs/contacts-guardians)),
> Clever does not accept a standalone `contacts.csv` over SFTP at all.
> Contacts are columns **on `students.csv`**: `contact_name`, `contact_type`,
> `contact_relationship`, `contact_phone`, `contact_phone_type`,
> `contact_email`, `contact_sis_id`, repeated for up to **5 contacts per
> student** (`contact_name_2`, `contact_email_2`, ... through `_5`). This
> engine's separate `contacts.csv` will most likely simply be ignored by a
> real sandbox ingest. **Status: BLOCKED**, pending David verifying the exact
> CSV shape his sandbox SFTP endpoint actually accepts — this rework is
> explicitly **not** implemented here; do not attempt it speculatively. See
> the README's "KNOWN BLOCKER" section for the related consequence: a
> contact's Clever id is only stable when `contact_sis_id` is populated,
> otherwise Clever derives identity from name+email, so an email edit reads
> as delete-then-create rather than an update.

## A missing required column is a hard load error

`CsvStack.load` only ever backfills the three engine-added columns/file
described below. Any other column missing from a file's header is a hard
`ValueError` naming the file and the column(s) — the engine refuses to load
a file that has silently lost a real SIS field rather than write it back out
with every row blanked. This matters directly for onboarding a new export
(see [docs/RUNBOOK.md](RUNBOOK.md)'s first-time setup): a short or truncated
CSV export now fails loudly at load time instead of loading anyway with a
field like `Student email` blank on every row.

## The three engine-added deviations

The real sandbox export, as received, had no mutable surface for three of
the six event categories this project needs to produce. All three additions
are **optional fields in Clever's SIS CSV spec**, so adding them is
schema-legal — these were explicit decisions made during the build, not
assumptions layered on top of an ambiguous spec.

| Addition | File | What was missing | Why it mattered |
|---|---|---|---|
| `Middle name` column | `students.csv` | No mutable student-level field existed that wasn't already load-bearing (e.g. `Last name` changes read oddly as a routine "drift" edit). | Without it, `users.updated (Students)` (brief §3, category 5 — "minor field change, e.g. middle name added") had no realistic field to edit. |
| `Teacher 2 id` column | `sections.csv` | No co-teacher slot existed on a section. | Without it, the Friday `big_teacher` bucket's "swapping a co-teacher on a section" (brief §4) had nothing to change. |
| `contacts.csv` (whole file) | — | The export had no guardian/contact records at all. | Without it, all three contact lifecycle events — `users.created`, `users.updated`, `users.deleted` (Contacts role; brief §3, categories 1–3) — were structurally impossible. **See the KNOWN BLOCKER above: this file is very likely the wrong shape for a real ingest and this is unresolved.** |

### What happens on the first sync after they appear

`CsvStack.load` backfills any missing engine-added column with an empty
string in memory (tracked in `CsvStack.migrated_columns`) so the engine
always has a value to read/write against, even before the column exists on
disk. The column is only actually written back to the CSV the next time
`CsvStack.save` runs — i.e. the first real (or dry) run after this engine is
pointed at a stack for the first time.

Practical effect, on that first sync:

- **Resolved, good news (corrected 2026-08-03):** adding the empty
  `Middle name`/`Teacher 2 id` columns should **not** produce a field-change
  event burst. Clever's `users.updated`/`sections.updated` fire when Clever
  detects a genuine change **on the object itself** (surfaced to the partner
  via a `previous_attributes` hash) — an earlier draft of this document
  treated whether an *absent → empty* column counts as that kind of change
  as an open, unverified question, framed as "the single biggest unknown in
  the project." It isn't open: absent-from-the-header to present-but-empty
  is not a value change on any existing row, so there is nothing for
  Clever's diff to see. `runner.run_once`'s log line has been updated to say
  so rather than warn that the next sync WILL show these as field changes.
- `contacts.csv` appears on the SFTP endpoint for the first time as a brand
  new file — **but see the KNOWN BLOCKER above: Clever very likely ignores
  this file entirely**, since contacts belong on `students.csv` as columns,
  not as their own file. Until that blocker is resolved, no
  `users.created (Contacts)` events should be expected from this file
  appearing, regardless of how many rows are in it.

## CRLF, no quoting, row order — and why it matters

The source CSVs use **CRLF line endings** and **no quoting** (`csv.QUOTE_MINIMAL`,
which matches the source exactly since nothing in this data ever needs
quoting). `CsvStack.load`/`save` preserve both, and preserve on-disk **row
order** exactly.

This is not cosmetic. Clever computes Events API deltas by diffing the new
CSV export against the previous one, essentially row-for-row. If this engine
reordered rows, added quoting Clever didn't ask for, or flipped CRLF to LF,
Clever would see a difference on **every single row** and report the sync as
a full-district rewrite — tens of thousands of spurious events — instead of
the handful of deliberate changes this engine actually made. `CsvStack.load`
opens files with `newline=""` specifically so Python's csv module (not
universal-newline translation) decides what a line ending is, and `save`
writes via a temp file + `os.replace` so a crash mid-write can never leave a
truncated CSV on disk (which Clever would read as "everyone past that point
was deleted").

## Provenance: how to tell engine-created rows apart at a glance

Two distinct ID conventions exist for rows this engine creates, so David can
tell where a row came from just by looking at its id:

| Prefix | Created by | Format | Example |
|---|---|---|---|
| `SEED<Student id>-<sequence>` | `seed.seed_contacts` (one-time/staged baseline seeding) | `SEED` + the student's own id + `-1` or `-2` | `SEEDSTU100000-1` |
| `CON######` | `selection._big_student` (normal weekly drift, new guardian contact) | `CON` + a 6-digit zero-padded counter | `CON000001` |
| `TCH9#####` | `selection._big_teacher` (normal weekly drift, new teacher) | `TCH9` + a 5-digit zero-padded counter | `TCH90001` |

`TCH9...` is deliberately outside the real seeded teacher id numbering space
(real teacher ids look like `TCH5000`), so a drift-minted teacher id can
never collide with — or be confused for — a real one. The `_IdMinter` class
in `selection.py` verifies every candidate id against the stack's actual
current rows (not just its own counter) before handing it out, so minted ids
can't collide even within a single run's batch of new rows.
