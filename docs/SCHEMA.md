# CSV schema contract

This is the exact CSV contract the engine reads and writes, as defined in
[`src/drift_engine/schema.py`](../src/drift_engine/schema.py). Headers were
read from David's actual sandbox export (Tulsa replica, SFTP user
`steadfast-backpack-8880`) on 2026-07-30 — this is **not** a generic Clever
SIS CSV layout; column names, order, and which files exist are specific to
this sandbox's real export.

Three columns/files are **added by this engine** and did not exist in the
original export. They're covered in their own section below.

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

New rows are also **created** here on Fridays (`teachers.created`) — see
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

**This entire file is engine-added** — see below. Drives all three of
`contacts.created` / `contacts.updated` / `contacts.deleted`.

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
| `Middle name` column | `students.csv` | No mutable student-level field existed that wasn't already load-bearing (e.g. `Last name` changes read oddly as a routine "drift" edit). | Without it, `users.updated` (brief §3, category 5 — "minor field change, e.g. middle name added") had no realistic field to edit. |
| `Teacher 2 id` column | `sections.csv` | No co-teacher slot existed on a section. | Without it, the Friday `big_teacher` bucket's "swapping a co-teacher on a section" (brief §4) had nothing to change. |
| `contacts.csv` (whole file) | — | The export had no guardian/contact records at all. | Without it, all three contact lifecycle events — `contacts.created`, `contacts.updated`, `contacts.deleted` (brief §3, categories 1–3) — were structurally impossible. |

### What happens on the first sync after they appear

`CsvStack.load` backfills any missing engine-added column with an empty
string in memory (tracked in `CsvStack.migrated_columns`) so the engine
always has a value to read/write against, even before the column exists on
disk. The column is only actually written back to the CSV the next time
`CsvStack.save` runs — i.e. the first real (or dry) run after this engine is
pointed at a stack for the first time.

Practical effect, on that first sync:

- Every row of `students.csv` and `sections.csv` will show up as a
  **field change** (`users.updated` / `sections.updated`) to Clever's
  diff, purely because the new column (`Middle name` / `Teacher 2 id`) now
  exists where it previously didn't — not because the engine intentionally
  edited every row. This is a one-time, whole-file event vs. the steady
  handful-per-day cadence in every subsequent run.
- `contacts.csv` appears on the SFTP endpoint for the first time as a brand
  new file. Every row in it — whether from a `seed` run or the normal
  cadence's first `big_student` contact-add — is a genuine `contacts.created`
  event, since there is no prior version of this file for Clever to diff
  against.

`runner.run_once` logs this explicitly (`"Added engine-owned columns on
load: ...The next sync will show these as field changes on affected
records."`) whenever a stack has migrated columns.

**Open question, not yet verified against Clever's real ingest behaviour:**
whether Clever's Events API actually emits `users.updated`/
`sections.updated` for a column going from *absent from the header entirely*
to *present with an empty value* is unconfirmed. If it does, that first sync
is not a quiet no-op for every existing row — it is a very large, one-time
event burst (potentially every row of `students.csv` and `sections.csv`).
This is the single biggest unknown in the project and should be confirmed
with Clever before the first live push (see docs/RUNBOOK.md's "First live
push" section).

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
