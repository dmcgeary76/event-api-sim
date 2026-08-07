# CSV schema contract

This is the exact CSV contract the engine reads and writes, as defined in
[`src/drift_engine/schema.py`](../src/drift_engine/schema.py). Headers were
read from David's actual sandbox export (Tulsa replica, SFTP user
`steadfast-backpack-8880`) on 2026-07-30 — this is **not** a generic Clever
SIS CSV layout; column names, order, and which files exist are specific to
this sandbox's real export.

Three sets of columns are **added by this engine** and did not exist in the
original export. They're covered in their own section below.

Note there is **no `contacts.csv`** in this contract. Guardian contacts are
columns on `students.csv`, one contact per row, confirmed against Clever's
official **SFTP Instructions, v2.1.1 (Dec 2025)** on 2026-08-05 -- see
[Contacts are rows on students.csv](#contacts-are-rows-on-studentscsv-confirmed-2026-08-05)
below for the spec detail and what it forced in the engine.

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

## The six files

For each file: exact column order as the engine writes it, the natural key
used to look up/apply changes, which columns the engine is allowed to
mutate, and the Clever record type it feeds (used for guardrail accounting).

One of the six, `staff.csv`, is optional per Clever's own SFTP spec (v2.2.0):
only schools/students/teachers/sections/enrollments must be uploaded
together. `schema.OPTIONAL_FILES` is the one exception to "every file in
`ALL_SPECS` is required" -- see [README.md](../README.md#data-integrity-csv-load--save--push)
for the mechanics.

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
| Columns | `School id, Student id, Student number, Last name, First name, Middle name, Grade, Gender, DOB, Student email, Contact relationship, Contact type, Contact name, Contact phone, Contact phone type, Contact email, Contact sis id` |
| Natural key | `Student id, Contact sis id` (composite) |
| Record type | `students` (plus a derived `contacts` count -- see below) |
| Mutable columns | `Middle name`, `Student email`, `Last name`, and every contact column **except** `Contact sis id` |

`Middle name` and all seven `Contact *` columns are engine-added (see below).
`Last name` is mutable in the schema but is not currently written to by any
selection/seed logic in this build.

**`Student id` alone is not the key, and is not unique.** A student with N
guardians occupies N rows here. Keying on `Student id` alone would make
`CsvStack.index()` silently collapse those rows (last one wins) and
`CsvStack.get()` return an arbitrary sibling. `Contact sis id` disambiguates:
it is minted unique per contact, and a student with no contacts has exactly one
row with it blank.

`Contact sis id` is deliberately **not** mutable. That is the whole mechanism
by which a contact field edit reads to Clever as `users.updated (Contacts)`
rather than a delete-then-create -- see
[Contacts are rows on students.csv](#contacts-are-rows-on-studentscsv-confirmed-2026-08-05).

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

Rows are also **deleted** here on Fridays, one per run
(`cadence.BIG_TEACHER_TEACHERS_REMOVED`), as of 2026-08-07 — paired 1-for-1
with the new-teacher creation above so total teacher headcount stays roughly
flat instead of only ever growing (see README "Known limitations" #1). The
removed teacher is always at a *different* school than the one that gained a
teacher this run, and is only ever picked once every `sections.csv` row that
lists them as primary (`Teacher id`) or co-teacher (`Teacher 2 id`) has
somewhere else to point — see `selection._big_teacher` for the reassign/clear
logic that guarantees this.

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
Friday `big_teacher` bucket (co-teacher swap, primary teacher reassignment,
and — as of 2026-08-07 — the forced reassignment/clear a teacher removal
requires before that teacher's row can be deleted).

**Numbered suffixes are real here, and only here.** SFTP Instructions v2.1.1
gives `sections.csv` co-teacher slots `Teacher 2 id` through `Teacher 10 id`;
this engine uses only slot 2. That is genuinely a different pattern from
contacts in the same spec, which repeat as rows instead of widening into
numbered columns. Do not "fix" either one to match the other -- see the warning
in the contacts section below, and the matching notes in `schema.py`.
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

### There is no contacts.csv

An earlier version of this engine defined one, with its own `Contact id` key
and a `Sequence` column. It was removed on 2026-08-05 -- Clever's SFTP ingest
has no such file, so it would have been ignored (or rejected as unknown
columns) and none of this engine's contact events would ever have fired. The
`contacts` record type still exists for guardrail and scale accounting, but it
is now **derived** from `students.csv` rows rather than counted out of a file
of its own. See the next section.

## Contacts are rows on students.csv (confirmed 2026-08-05)

**This was a genuine blocker, and it was verified before it was fixed** -- not
quietly deleted as a bad assumption. The authority is Clever's official **SFTP
Instructions, v2.1.1 (Dec 2025)**, confirmed by David on 2026-08-05 against
the standard SFTP allowable-fields list. Coverage on contacts from Clever's
internal answer bot was thin, so **the current PDF is authoritative over
general memory** -- that is worth remembering the next time this comes up.
Every spec claim in this section cites v2.1.1 so a future reader can tell a
spec fact from one of our inferences.

### What the spec says

- Guardian contacts are **columns on `students.csv`**: `Contact
  relationship`, `Contact type`, `Contact name`, `Contact phone`, `Contact
  phone type`, `Contact email`, `Contact sis id`. **Unsuffixed and singular**
  -- exactly one contact's worth of columns (v2.1.1).
- Multiple contacts are expressed as **multiple rows for the same `Student
  id`**. Quoting v2.1.1: *"In order to provide multiple parent/guardian
  contacts, you may create multiple rows for a single student with different
  contact information."* The ceiling is **5 contacts per student**, with no
  custom mappings supported (`schema.MAX_CONTACTS_PER_STUDENT`).
- There is **no `contact_name_2` / `contact_email_2` column convention.**
  Those headers are not real; they would be dropped or rejected on ingest.

> **Two different patterns live in one spec -- do not conflate them.**
> `sections.csv` genuinely *does* use numbered suffixes in v2.1.1
> (`Teacher 2 id` through `Teacher 10 id`) for co-teachers. Contacts do not.
> **Sections widen; contacts repeat.** This is an easy mistake to make in
> either direction, and making it in the contacts direction is exactly the bug
> this rework fixed, so it is flagged here, in the `sections.csv` entry above,
> and beside both definitions in `schema.py`.

### `Contact sis id` governs contact identity

- **With** a `Contact sis id`, a contact keeps the **same Clever id** across
  phone, email, and name changes. Only the sis id itself changing changes the
  Clever id.
- **Without** one, Clever derives the contact's id from name+email, else
  name+phone, else name+contact type+relationship+phone type. So editing an
  email changes the identity key itself, and the ingest reads it as a
  **delete-then-create of a different contact**, not a `users.updated` -- the
  precise opposite of what a "contact field edit" exists to demonstrate.
- Real-world confirmation: when IDEA Public Schools added `contact_sis_id` to
  their file, Clever's PE team confirmed that **every existing contact's
  Clever id would change**. There is no id-preservation path once the identity
  basis shifts, which is why this engine mints an sis id from the first push
  rather than adding one later.
- Partner-facing footnote, for context rather than for this engine: on the
  SIS-managed auto-sync side, `contact.sis_id` is only honored for **Infinite
  Campus, IC OneRoster API, Skyward, and Skyward API**. That has no bearing on
  an SFTP sandbox, but it matters as soon as a partner compares this sandbox's
  contact behaviour against a real auto-synced district's.

### What this forced in the engine

- **Every contact this engine creates gets a minted, permanent `Contact sis
  id`, and the engine never edits it.** That column is deliberately excluded
  from `STUDENTS.mutable` (`schema.CONTACT_MUTABLE_COLUMNS`). It is the single
  reason a contact email/phone edit surfaces as `users.updated (Contacts)`.
  Id conventions are in [Provenance](#provenance-how-to-tell-engine-created-rows-apart-at-a-glance)
  below.
- **A student with no contacts still occupies exactly one row**, contact
  columns blank. Dropping the row would delete the student.
- **Adding a student's first guardian fills that blank row in place.** That is
  a CSV `UPDATE` that is a contact **CREATE** to Clever -- the CSV operation
  and the wire event legitimately diverge here, and `guardrail.py` accounts
  for both separately rather than assuming they match.
- **Each guardian after the first is a new row** (`CREATE`). Removing a
  guardian deletes that row, and selection **refuses to remove a student's
  last contact**, because that row is also the student's last row.
- **Student-level column edits fan out to every row sharing a `Student id`**
  (`CsvStack._fan_out_student_columns`), so a student never presents
  conflicting values inside one file. Contact-level columns are deliberately
  *not* fanned out -- those are what distinguish a row from its siblings.
- **`schema.expand_contact_rows(student, contacts)` is the single function
  encoding the row-per-contact pattern.** Given how thin the available
  contacts coverage is, that is the deliberate hedge: if the spec turns out to
  vary, correcting that one function is the whole change. It also refuses to
  emit more than 5 contacts rather than truncating silently, since dropping a
  guardian is exactly the sort of thing that looks like a successful run and
  isn't.

### Counting: distinct students, derived contacts

`CsvStack.counts()` reports `students` as a **distinct `Student id` count**,
not a row count, and adds a **derived `contacts`** count (rows whose
`Contact sis id` is populated). This is load-bearing, not tidiness:

- Seeding takes `students.csv` from 33,621 rows to roughly 52,900 -- a **+57%**
  move.
- As a raw row count that blows straight through `safety.MAX_SCALE_DRIFT`
  (25%), and because a stale `baseline_counts.json` is a hard
  `SafetyViolation` rather than a silent re-anchor, the district would have
  been **bricked mid-seed** until someone re-baselined by hand.

The guardrail follows the same principle: a `students.csv` row deletion is
attributed to `contacts` when the change is about a contact, and only to
`students` otherwise (`guardrail._attributed_record_type`). Without that, routine
guardian churn would inflate the student deletion ratio toward Clever's 10%
threshold and camouflage a genuine student deletion in the noise. See
[RUNBOOK.md](RUNBOOK.md#the-guardrails-two-thresholds).

## A missing required column is a hard load error

`CsvStack.load` only ever backfills the engine-added columns described below
(`ENGINE_ADDED_COLUMNS`: `Middle name` and the seven `Contact *` columns on
`students.csv`, `Teacher 2 id` on `sections.csv`). Any other column missing
from a file's header is a hard `ValueError` naming the file and the column(s) --
the engine refuses to load a file that has silently lost a real SIS field
rather than write it back out with every row blanked. This matters directly for
onboarding a new export (see [docs/RUNBOOK.md](RUNBOOK.md)'s first-time setup):
a short or truncated CSV export now fails loudly at load time instead of
loading anyway with a field like `Student email` blank on every row.

An **unrecognized** column is a hard `ValueError` too, for the mirror-image
reason: silently dropping a column this engine does not understand would lose
that data on the next save.

And **every file in `schema.ALL_SPECS` must be present on disk, except
`schema.OPTIONAL_FILES`.** `FileSpec.engine_added` -- the flag that let a file
be legitimately absent on load -- has been removed. It only ever existed for
the old `contacts.csv`. Its replacement, `OPTIONAL_FILES` (currently just
`staff.csv`, per Clever's own SFTP spec), is narrower and for a different
reason: not "this engine owns the file" but "Clever's spec says the file is
optional." `save()` writes every non-optional file on every save, even one
with zero rows; an optional file with zero rows is skipped. A useful
side effect: because `save()` promotes a freshly staged directory containing
only `ALL_SPECS`, a stale `contacts.csv` left behind by the pre-2026-08-05
version of this engine cleans itself up locally on the next save. It was never
pushed live, so there is no remote copy to worry about.

## The three engine-added deviations

The real sandbox export, as received, had no mutable surface for three of
the six event categories this project needs to produce. All three additions
are **optional fields in Clever's SIS CSV spec** (SFTP Instructions v2.1.1),
so adding them is schema-legal -- these were explicit decisions made during
the build, not assumptions layered on top of an ambiguous spec. "Three" still
describes three *additions*, but the third is now seven columns on
`students.csv` rather than a file of its own.

| Addition | File | What was missing | Why it mattered |
|---|---|---|---|
| `Middle name` column | `students.csv` | No mutable student-level field existed that wasn't already load-bearing (e.g. `Last name` changes read oddly as a routine "drift" edit). | Without it, `users.updated (Students)` (brief §3, category 5 — "minor field change, e.g. middle name added") had no realistic field to edit. |
| `Teacher 2 id` column | `sections.csv` | No co-teacher slot existed on a section. | Without it, the Friday `big_teacher` bucket's "swapping a co-teacher on a section" (brief §4) had nothing to change. |
| The seven `Contact *` columns | `students.csv` | The export had no guardian/contact data at all -- no columns for it and no rows carrying it. | Without them, all three contact lifecycle events -- `users.created`, `users.updated`, `users.deleted` (Contacts role; brief §3, categories 1–3) -- were structurally impossible. These are the columns SFTP Instructions v2.1.1 actually specifies; an earlier build put them in a standalone `contacts.csv`, which Clever's ingest has no concept of. See [Contacts are rows on students.csv](#contacts-are-rows-on-studentscsv-confirmed-2026-08-05). |

### What happens on the first sync after they appear

`CsvStack.load` backfills any missing engine-added column with an empty
string in memory (tracked in `CsvStack.migrated_columns`) so the engine
always has a value to read/write against, even before the column exists on
disk. The column is only actually written back to the CSV the next time
`CsvStack.save` runs — i.e. the first real (or dry) run after this engine is
pointed at a stack for the first time.

Practical effect, on that first sync:

- **Resolved, good news (corrected 2026-08-03):** adding the empty
  `Middle name`/`Teacher 2 id`/`Contact *` columns should **not** produce a field-change
  event burst. Clever's `users.updated`/`sections.updated` fire when Clever
  detects a genuine change **on the object itself** (surfaced to the partner
  via a `previous_attributes` hash) — an earlier draft of this document
  treated whether an *absent → empty* column counts as that kind of change
  as an open, unverified question, framed as "the single biggest unknown in
  the project." It isn't open: absent-from-the-header to present-but-empty
  is not a value change on any existing row, so there is nothing for
  Clever's diff to see. `runner.run_once`'s log line has been updated to say
  so rather than warn that the next sync WILL show these as field changes.
- The seven `Contact *` columns behave the same way, and for the same reason.
  On the first save they appear in `students.csv`'s header, blank on every
  existing row, and a student with no guardians keeps their single row with
  those columns empty. Row *count* does not change either -- one student, one
  row, still. So no contact events are expected from the columns simply
  appearing; `users.created (Contacts)` only starts once a `seed` or drift run
  actually puts a guardian into one of those rows, which is exactly why
  seeding has to be staged (see
  [RUNBOOK.md](RUNBOOK.md#staged-contacts-seeding)).
- No new file appears on the SFTP endpoint at all. This engine now writes the
  same six files it reads, and nothing else.

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

Three distinct ID conventions exist for rows this engine creates, so David can
tell where a row came from just by looking at its id. The first two are
`Contact sis id` values on `students.csv`; the third is a `Teacher id` on
`teachers.csv`:

| Prefix | Column | Created by | Format | Example |
|---|---|---|---|---|
| `SEED<Student id>-<sequence>` | `Contact sis id` | `seed.seed_contacts` (one-time/staged baseline seeding) | `SEED` + the student's own id + `-1` or `-2` | `SEEDSTU100000-1` |
| `CON######` | `Contact sis id` | `selection._big_student` (normal weekly drift, new guardian contact) | `CON` + a 6-digit zero-padded counter | `CON000001` |
| `TCH9#####` | `Teacher id` | `selection._big_teacher` (normal weekly drift, new teacher) | `TCH9` + a 5-digit zero-padded counter | `TCH90001` |

Both contact conventions are **minted once and never edited**, which is what
keeps a contact's Clever id stable across field edits -- see
[Contacts are rows on students.csv](#contacts-are-rows-on-studentscsv-confirmed-2026-08-05).
The `SEED` form is deterministic from the student's own id, so a re-run cannot
mint a second, different id for the same seeded guardian.

`TCH9...` is deliberately outside the real seeded teacher id numbering space
(real teacher ids look like `TCH5000`), so a drift-minted teacher id can
never collide with — or be confused for — a real one. The `_IdMinter` class
in `selection.py` verifies every candidate id against the stack's actual
current rows (not just its own counter) before handing it out, so minted ids
can't collide even within a single run's batch of new rows.
