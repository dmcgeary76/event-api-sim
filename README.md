# Clever Sandbox Events API Data Drift Engine

Makes small, realistic, recurring changes to a sandbox district's CSV roster
stack on a fixed weekday cadence, then re-syncs it over the district's
existing SFTP pipeline — so Clever emits a predictable stream of Events API
`created` / `updated` / `deleted` records for application partners to build
and test against.

> ## ⚠️ SANDBOX / DEVELOPER DISTRICTS ONLY
> This engine must **never** be pointed at a production district or a
> production SFTP endpoint. That is a hard constraint enforced in code
> (`src/drift_engine/safety.py`), not just a config default — see
> [Safety model](#safety-model-in-plain-terms) below. Adding a new SFTP username to
> `config/districts.yml` is a deliberate, reviewed act, not a formality.

## What it does, and why

Clever sandbox districts are static once their roster CSVs are loaded — there
is no ongoing stream of Events API activity for application partners to
observe. This engine keeps a working copy of one sandbox district's CSV
stack, edits a small, bounded set of records on a fixed weekly pattern, and
pushes the result back over SFTP. Clever's own sync process diffs the new
export against the last one and emits the corresponding Events API records.
The engine never calls Clever's API directly — the CSV diff is the only
mechanism that produces events.

## The fixed weekly cadence

Rigid and calendar-fixed by design (project brief §4) — partners are told to
expect activity on this schedule, so it does not vary by district size or
change over time without a deliberate code change to `cadence.py`.

| Day | Bucket(s) | What happens |
|---|---|---|
| Monday | `small_daily` | A handful of minor field edits — contact email/phone tweaks, student middle-name/email edits. |
| Tuesday | `small_daily` + `big_student` | Small daily, plus ~4 students moved between sections, a few new guardian contacts, and a couple of contact removals. |
| Wednesday | `small_daily` | Small daily only. |
| Thursday | `small_daily` + `big_student` | Same as Tuesday. |
| Friday | `small_daily` + `big_teacher` | Small daily, plus co-teacher swaps, a primary-teacher reassignment, one brand-new teacher added, and one teacher removed from a different school. |
| Saturday / Sunday | — | Skipped. No drift runs on weekends. |

Big buckets **stack on top of** the small daily bucket for that day — they
never replace it. The exact magnitudes (`SMALL_DAILY_CONTACT_FIELD_EDITS`,
`BIG_STUDENT_ENROLLMENT_MOVES`, etc.) live as hard-coded constants in
`src/drift_engine/cadence.py`, not config, per brief §10 ("no per-district
volume knob").

Run `drift-engine schedule` to print this table straight from the code.

## Event types produced

> **Corrected 2026-08-03: the project brief (§3) was factually wrong about
> this.** The brief asserted that contacts had their own distinct event
> lifecycle — `contacts.created` / `contacts.updated` / `contacts.deleted` —
> as if they were separate wire event types. **They are not, and never have
> been, on Clever's real Events API.** Verified against Clever's live dev
> docs on 2026-08-03
> ([Events API](https://dev.clever.com/docs/events-api),
> [Contacts & guardians](https://dev.clever.com/docs/contacts-guardians)):
> in API v3.x, contacts (guardians), students, teachers, staff, and district
> admins are ALL `users` on the wire. The only object-level events are
> `users.created` / `users.updated` / `users.deleted`, and the role is
> carried in the object's own `roles` node — not in the event name. Section
> membership changes remain their own `sections.*` events, unaffected by this
> correction. `EventType` (`src/drift_engine/models.py`) has been corrected
> to the six real wire events; a new `EventSubject` field on `Change`
> restores the student/teacher/contact/staff distinction for reporting
> purposes only (see `Change.expected_event_label`) — it is never part of the
> event name Clever itself emits. This document, and every other doc in this
> project, is corrected below to match reality; the brief itself is left
> as-is as the historical (and, on this point, incorrect) input document.

| Wire event (what Clever actually emits) | Role (David's label) | Produced by |
|---|---|---|
| `users.created` | Contacts | Small daily / big-student guardian additions, and staged `seed` runs |
| `users.updated` | Contacts | Small daily contact field edits (email, phone, phone type) |
| `users.deleted` | Contacts | Big-student guardian removals (never orphans a student's last contact) |
| `users.updated` | Students | Small daily student field edits (middle name, student email) |
| `users.created` | Teachers | Big-teacher new-teacher additions (Friday) |
| `sections.updated` | Sections | Big-student enrollment moves; big-teacher co-teacher/primary-teacher changes |

There are only six real `EventType` members
(`USERS_CREATED`/`USERS_UPDATED`/`USERS_DELETED`/`SECTIONS_CREATED`/
`SECTIONS_UPDATED`/`SECTIONS_DELETED`) — every row above maps to one of them.
`Change.expected_event_label` renders the combination exactly the way
Clever's own event-ordering docs disambiguate by role, e.g.
`users.updated (Contacts)`.

Every generated `Email`/`Phone`/`Middle name`/`Student email` "update" is now
checked to actually differ from the current value before it's written —
`selection.py` re-rolls a generated value up to 4 times and drops the change
entirely if it still matches. Before this fix, `guardian_email`/
`student_email` were pure functions of (name, student), so a repeat "edit"
recomputed the identical address every time; Clever's CSV diff saw nothing,
so no event ever fired no matter how often selection "edited" that field.
Audit finding: ~62% of predicted `users.updated (Contacts)` events over 26
simulated weeks were silent no-ops on the Email field. Measured after the
fix: 0% no-op rate across every updated field.

### Confirmed, then fixed: contacts are rows on students.csv, not a separate CSV

**Status: RESOLVED 2026-08-05.** This was recorded here for weeks as a KNOWN
BLOCKER, and it was a real one -- it has now been *verified* and then fixed,
not assumed away. Earlier versions of this engine wrote a standalone
`contacts.csv`. No such file exists in Clever's SFTP spec, so it would have
been ignored (or rejected as unknown columns) on ingest, and not one of this
engine's predicted `users.created`/`users.updated`/`users.deleted` (Contacts)
events would ever have fired against a real sandbox.

Verified against Clever's official **SFTP Instructions, v2.1.1 (Dec 2025)**,
confirmed by David on 2026-08-05 against the standard SFTP allowable-fields
list. Coverage on contacts from Clever's internal answer bot was thin, so the
current PDF is treated as authoritative over general memory. Every spec claim
below and in [docs/SCHEMA.md](docs/SCHEMA.md) is cited to that version inline,
so a future reader can always tell a spec fact from one of our inferences.

**What the spec says:**

- Guardian contacts are **columns on `students.csv`**: `Contact
  relationship`, `Contact type`, `Contact name`, `Contact phone`, `Contact
  phone type`, `Contact email`, `Contact sis id`. Unsuffixed and singular --
  exactly one contact's worth of columns (SFTP Instructions v2.1.1).
- Multiple contacts are expressed as **multiple rows for the same `Student
  id`**. Quoting v2.1.1 directly: *"In order to provide multiple
  parent/guardian contacts, you may create multiple rows for a single student
  with different contact information."* At most **5 contacts per student**,
  with no custom mappings supported.
- There is **no `contact_name_2` / `contact_email_2` column convention.**
  Those headers are not real and would be dropped or rejected. Do not
  conflate this with `sections.csv`, which in the *same* spec genuinely does
  use numbered suffixes (`Teacher 2 id` through `Teacher 10 id`) for
  co-teachers. Two different patterns in one document: sections widen,
  contacts repeat. That is easy to conflate in either direction, so it is
  called out in `schema.py` next to both definitions as well as here.
- `Contact sis id` governs a contact's **identity stability**. *With* one, the
  contact keeps the same Clever id across phone, email, and name changes;
  only the sis id itself changing changes the Clever id. *Without* one, Clever
  derives the id from name+email, else name+phone, else name+contact
  type+relationship+phone type -- so editing an email changes the identity key
  itself, and the ingest reads it as a delete-then-create of a different
  contact rather than a `users.updated`. That is the exact opposite of what a
  "contact field edit" is meant to demonstrate.
- Real-world confirmation: when IDEA Public Schools added `contact_sis_id` to
  their file, Clever's PE team confirmed that **every existing contact's
  Clever id would change**, because there is no id-preservation path once the
  identity basis shifts. The sis id has to be present from the first push,
  not bolted on later.
- Partner-facing footnote: on the SIS-managed auto-sync side, `contact.sis_id`
  is only honored for **Infinite Campus, IC OneRoster API, Skyward, and
  Skyward API**. Irrelevant to an SFTP sandbox, but it matters the moment a
  partner compares this sandbox's contact behaviour against a real
  auto-synced district's.

**What the engine now does about it** (all implemented, all covered by tests):

- **Every contact this engine creates gets a minted, permanent `Contact sis
  id`, and the engine never edits it** -- that column is deliberately absent
  from `STUDENTS.mutable`. Seeded contacts use `SEED<student id>-<n>`;
  drift-added ones use `CON######`. This is the single thing that makes a
  contact email/phone edit surface as `users.updated (Contacts)` instead of a
  delete-then-create pair.
- **A student with no contacts occupies exactly one row**, with the contact
  columns blank -- dropping the row entirely would delete the student. Adding
  their first guardian **fills that row in place**: a CSV `UPDATE` that is a
  contact CREATE to Clever. Every guardian after that is a new row
  (`CREATE`). Removing a guardian deletes that row, and selection **refuses
  to remove a student's last contact**, because that row is also the
  student's last row.
- **`students.csv`'s natural key is now `(Student id, Contact sis id)`.**
  `Student id` alone is no longer unique.
- **Student-level column edits fan out to every row sharing a `Student id`**,
  so a student never presents conflicting values inside one file.
- **`CsvStack.counts()` reports `students` as a DISTINCT student count** and
  adds a derived `contacts` count. This is load-bearing, not cosmetic:
  seeding takes `students.csv` from 33,621 rows to ~52,900, a **+57%** move
  that as a raw row count would blow straight through
  `safety.MAX_SCALE_DRIFT` (25%) -- and because a stale baseline is a hard
  `SafetyViolation` rather than a silent re-anchor, that would have bricked
  the district mid-seed.
- **The guardrail attributes a `students.csv` row delete to `contacts`** when
  the change is about a contact, and only to `students` otherwise. Without
  that, routine guardian churn would inflate the student deletion ratio
  toward Clever's 10% threshold and camouflage a genuine student deletion
  behind the noise.
- **`schema.expand_contact_rows(student, contacts)` is the one function that
  encodes the row-per-contact pattern.** Given how thin the available
  contacts coverage is, that is the deliberate hedge: if the spec turns out
  to vary, correcting that function is the whole change.
- **`FileSpec.engine_added` is gone.** `schema.ALL_SPECS` is now **six** files,
  not seven, and every one of them is a real SIS export. `save()` always
  writes each one, even with zero rows -- except `staff.csv` (see below),
  which Clever's own spec, not this engine, says may be absent.
- **`staff.csv` is optional, per Clever's own spec (added 0.2.1).** Clever's
  SFTP specification (v2.2.0) requires only five files together --
  schools, students, teachers, sections, enrollments -- and documents
  `staff.csv` as optional. `schema.OPTIONAL_FILES` (currently just
  `staff.csv`) is the one exception to "every file unconditionally required":
  an absent `staff.csv` loads as zero rows, and `save()`/`sftp_push` tolerate
  its absence only when the in-memory stack agrees it is genuinely empty.
  Deliberately a different mechanism from the old `contacts.csv` exception:
  that one existed because the engine owned the file; this one exists because
  Clever's spec says the file is optional. Has no effect on the real Tulsa
  stack, which always has 280 staff rows.

Two source bugs were found and fixed while doing this work, both latent before
the rework:

- `CsvStack.apply` deleted rows by a **stale index** when one batch contained
  two deletes against the same file, so the second delete could silently take
  out an innocent bystander row.
- The guardrail's **move-netting was loose enough to be self-defeating**:
  adding a guardian to the same student you removed one from netted the
  removal away to zero, so contact attrition could never be reported at all.
  Netting now includes the `Contact sis id`, because a contact cannot "move"
  -- a CREATE is never the same contact as a DELETE.

### Resolved: the absent → empty column question is no longer an open risk

The `Middle name`/`Teacher 2 id` engine-added columns (see
[docs/SCHEMA.md](docs/SCHEMA.md)) load as empty strings on every existing row
the first time this engine touches a district's stack. Earlier drafts of
this project treated whether that produces a field-change event burst as an
open, unverified question. It does not: Clever's `users.updated` fires when
it detects a change **on the object** (surfaced via `previous_attributes`),
and a column going from *absent-from-the-header* to *present-but-empty* is
not a value change on any existing student/section — there is nothing for
Clever's diff to see. No first-sync burst is expected from this alone.

The same reasoning covers the seven `Contact *` columns, which are engine-added
on `students.csv` in exactly the same way: on the first save they appear in the
header, blank on every student row, and a student with no guardians keeps their
single row with those columns empty. Contact events only start once a `seed` or
drift run actually puts a guardian into one of those rows.

## Quickstart

```bash
git clone https://github.com/dmcgeary76/event-api-sim.git
cd event-api-sim

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# edit .env: fill in SFTP_PASSWORD_STEADFAST_BACKPACK_8880 (required),
# and ANTHROPIC_API_KEY (optional — falls back to canned content without it)

drift-engine schedule        # print the fixed weekly cadence
drift-engine run              # DRY RUN by default — no SFTP connection at all
drift-engine run --live       # actually push to SFTP
```

`run` is a dry run unless `--live` is passed. There is no config setting that
changes that default — a scheduled task silently going live is exactly the
failure mode the sandbox-only constraint exists to prevent.

## CLI command reference

All commands accept `--config`, `--state-root`, `--logs-root`, `--env-file`,
and `-v/--verbose` at the top level. Full command list from `drift-engine
--help`:

| Command | Purpose |
|---|---|
| `schedule` | Print the fixed weekly cadence table. |
| `plan --date YYYY-MM-DD` | Show which bucket(s) apply on a given date (defaults to today). |
| `run [--district ID] [--live] [--force] [--seed N] [--canned-content]` | Execute a drift run. Dry run unless `--live`. `--force` runs the bucket logic even on a weekend (still subject to the guardrail and safety gates). `--seed` makes the run reproducible. |
| `seed [--district ID] [--limit N=4000] [--live] [--seed N] [--canned-content]` | Create baseline guardian contact rows on `students.csv` for students that have none, `--limit` students at a time. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for why this must be staged. |
| `estimate-seed [--district ID]` | Report how many `users.created (Contacts)` events an unbounded seed pass would generate — read-only, no writes. |
| `history [--district ID] [--days N=30]` | Summarise recent runs from `logs/<district>/history.jsonl` (runs per weekday, cumulative event counts, failures, AI-vs-canned trend). |
| `simulate-week [--district ID] [--start YYYY-MM-DD] [--seed N=1234]` | Dry-run a full Mon–Fri locally, in memory, to preview a week of activity without touching persistent state. |

Example (captured against the real sandbox stack):

```
$ drift-engine estimate-seed
=== Tulsa Replica Sandbox (steadfast-backpack-8880) ===
  students_without_contacts: 33621
  estimated_contacts_low: 33621
  estimated_contacts_high: 67242
  estimated_contacts_expected: 52931
  recommended_staged_limit: 4000
  recommended_run_count: 9
```

## Adding another sandbox district

Per brief §6, this is a **config-only** change — never a code change:

1. Add an entry under `districts:` in `config/districts.yml` with a new `id`,
   `label`, `sftp` block (`host`, `port`, `username`, `password_env`,
   `remote_dir`), a `data_fingerprint` (a substring guaranteed to appear in
   that district's own data — e.g. its email domain — that also passes the
   strength check in [Safety model](#safety-model-in-plain-terms): at least
   8 characters, no whitespace, contains a `.`, and contains one of
   `replica`/`sandbox`/`sbx`/`dev`/`test`/`demo`/`staging`), and
   `eventing_verified` (leave `false` until confirmed — see
   [docs/RUNBOOK.md](docs/RUNBOOK.md)).
2. Optionally set `timezone` (default `America/Chicago` — this is what
   cadence resolves "today" against for this district), `staff_email_domain`,
   `student_email_domain`, and `area_codes` (a YAML list of strings). All
   four default to this project's one real sandbox district (Tulsa replica)
   if omitted, so a second district's generated staff/guardian/teacher
   emails and phone numbers actually belong to *its* data, not Tulsa's.
3. Add the matching `password_env` variable to `.env`.
4. Drop the district's initial CSV export into
   `state/<district-id>/baseline/`.

Nothing in `src/drift_engine/` changes. The scheduling and selection logic is
identical across every configured district.

## Safety model, in plain terms

`assert_safe_target` (`src/drift_engine/safety.py`) runs every gate below
before a single byte is written to a real SFTP connection. `SafetyViolation`
is never caught or downgraded anywhere in this codebase — it is always
written to the audit log first, then re-raised (see
[Exit codes](#exit-codes) below).

1. **Host allowlist.** Only `sftp.clever.com` is ever connected to.
2. **Username allowlist, not hostname.** `sftp.clever.com` is shared
   infrastructure across many districts, so the hostname proves nothing
   about whether a target is a sandbox. The SFTP **username** identifies the
   district, and the engine hard-fails if the resolved username isn't listed
   in `config/districts.yml`.
3. **Data fingerprint, strength-checked.** The loaded stack must contain a
   configured substring (e.g. `tulsaschools-replica.org`) in its own email
   data — but the fingerprint itself is also validated for *strength* before
   it's ever used as a check (`safety.validate_fingerprint`, run once at
   config load and again at write time): it must be at least 8 characters,
   contain no whitespace, contain a `.`, and contain one of
   `SANDBOX_FINGERPRINT_MARKERS` (`replica`, `sandbox`, `sbx`, `dev`, `test`,
   `demo`, `staging`). A weak value like `"@"` now fails at config load time,
   before any run can even start — previously any non-empty substring
   passed, which was the most serious finding in an independent audit of
   this engine.
4. **Scale sanity.** If the stack's record counts have moved more than 25%
   from the recorded baseline, the run refuses to proceed. This check now
   runs *inside* `assert_safe_target` itself whenever both current and
   baseline counts are supplied (which `runner.py` always does once a
   baseline exists), so it can no longer be silently skipped by a caller
   that remembers the other gates but forgets this one. A missing or
   corrupt `baseline_counts.json` is a hard `SafetyViolation` on any run
   after the district's genuine first run — not a warning-and-skip — unless
   there is no baseline file *and* no record of a prior successful push,
   which is the one legitimate "first run ever" case. Note the counts being
   compared are **distinct students** and a **derived contacts count**, not
   raw row counts (`CsvStack.counts()`) -- without that, seeding's +57% growth
   in `students.csv` rows would trip this gate mid-seed. Note also that any
   record type whose *baseline* is `0` is skipped entirely, which for this
   district permanently excludes `contacts` -- see limitation 4 under
   [Known limitations](#known-limitations).
5. **Production-marker tripwire (advisory).** The district id, username, and
   remote directory are checked for the substrings `prod`, `production`, or
   `live`. This is a cheap belt-and-braces check, not the real gate — the
   allowlist is.

On top of the safety gates, `guardrail.py` enforces Clever's own **10%
deletion pause threshold** as a hard block, plus this engine's own stricter
**2% warning ceiling** (a run above 2% deletions for any record type still
succeeds, but is flagged in the audit log as worth a second look). Two
further refinements: a matched CREATE/DELETE pair on the same record type in
one run (an enrollment section move) is netted out first, so a move is never
counted as attrition; and when a real push has happened before, the
guardrail also compares against `last_pushed_counts.json` to catch
**unexplained row loss that happened before selection ever ran** — e.g. a
CSV export truncated outside this engine — which the old intent-only check
was structurally blind to.

Finally, `run` and `seed` are **dry-run by default** — `--live` is required
to actually open an SFTP connection, and dry runs write their would-be
output to `state/<district>/dry-run/<date>-<run_id>/` instead of touching
`current/`. Only the 5 most recent dry-run directories per district are kept
(`runner.DRY_RUN_RETENTION`) — each one is a full copy of the stack,
including student names, DOBs, and guardian contact details, so this used to
accumulate unbounded PII on disk with nothing ever cleaning it up.

### Concurrency: one run per district at a time

`run_once` holds an exclusive, non-blocking `flock` on
`state/<district>/.lock` for the entire duration of a run. A second
overlapping run for the same district (e.g. a scheduler retry landing while
the previous invocation is still running) exits immediately with **code 3**
rather than racing the first. This matters concretely: two overlapping runs
previously could both load the same pre-change stack, both mint the same
"next" engine-owned contact ID for two *different* students, and each
silently clobber the other's edits on save — which could re-parent one
child's guardian record onto another.

### Timezone: cadence resolves in the district's own local time

Each district's `timezone` field in `config/districts.yml` (default
`America/Chicago`) is what decides which weekday it is for cadence purposes
— never the host machine's local time, never UTC. This matters at day
boundaries: a scheduled task running on a UTC host at 03:30 UTC on a
Saturday is still 22:30 Friday in `America/Chicago`, and must still run
Friday's big-teacher bucket, not silently treat it as Saturday. Before this
was fixed, a Friday-evening run on a UTC host would resolve to Saturday and
silently skip the entire Friday bucket — while still reporting a clean,
successful run, so nothing in the audit log flagged that anything had gone
wrong.

### Exit codes

`drift-engine run`/`seed` (and any future command built on `runner.run_once`)
use a fixed, documented exit-code contract:

| Code | Meaning |
|---|---|
| 0 | Every requested district's run completed successfully. Also used by read-only commands (`schedule`, `plan`, `history`, `estimate-seed`, `simulate-week`). |
| 1 | At least one district's run completed but failed — stack load, selection, a guardrail block, or the SFTP push itself. An ordinary run failure, not a safety or locking problem. |
| 2 | A `SafetyViolation` was raised — the engine refused to run because the target failed a safety gate. Never downgraded to 1. |
| 3 | Another run was already in progress for this district (`state/<district>/.lock` was held). |

### Data integrity (CSV load / save / push)

- `CsvStack.load` **raises** if a required (non-engine-added) column is
  missing from a file's header — it no longer silently backfills a dropped
  SIS column with empty strings. Previously, a short/malformed export would
  have loaded anyway with that column blanked on every row and been pushed
  back out that way (the specific bug found in audit: a truncated
  `students.csv` export would have blanked all 33,621 students' email
  addresses). It also raises on an **unrecognized** column, since silently
  dropping a column this engine does not understand would lose that data on
  the next save. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for what this means
  when dropping in a new CSV export.
- **Every file in `schema.ALL_SPECS` is required, except `schema.OPTIONAL_FILES`.**
  There is no longer a `FileSpec.engine_added` flag letting a file be
  legitimately absent on load -- that only ever existed for the removed
  `contacts.csv`. Its replacement, `OPTIONAL_FILES` (currently just
  `staff.csv`), is narrower and spec-driven rather than engine-driven: see
  "Confirmed, then fixed" above. `save()` writes every non-optional file
  every time, even one with zero rows; an optional file with zero rows is
  not written at all.
- `CsvStack.save` is **all-or-nothing** across the whole stack — every file
  is written to a staging directory first, and only promoted into place
  (one atomic rename) once every file has written successfully. A failure
  partway through no longer leaves `current/` half-mutated.
- `sftp_push.push` asserts the local stack is **complete** (every schema
  file present) before describing or uploading anything, and its
  `allowlist` argument is now **required** — there is no silent fallback to
  loading the default config location if a caller forgets to pass it.

## Project layout

```
src/drift_engine/
  schema.py      # CSV column contract, file specs, natural keys; the one place
                 # the row-per-contact pattern lives (expand_contact_rows)
  models.py      # shared dataclasses: Change, RunPlan, RunResult, EventType,
                 # EventSubject (see the corrected event-type note in this file)
  safety.py      # sandbox-only hard gates: host/username allowlist, fingerprint
                 # (presence + strength), scale sanity, production-marker tripwire
  config.py      # loads config/districts.yml + .env (PyYAML, with a stdlib fallback parser)
  csvstack.py    # in-memory CSV load/apply/save; raises on a missing required
                 # column; save is all-or-nothing across the whole stack;
                 # counts() reports distinct students + derived contacts
  cadence.py     # deterministic day-of-week bucket logic
  selection.py   # randomized (seeded) target selection within the fixed cadence;
                 # re-rolls/skips any UPDATE whose value wouldn't actually change
  content.py     # the ONLY module that touches an LLM — isolated per brief §5
  seed.py        # one-time/staged guardian-contact-row baseline seeding
  guardrail.py   # Clever's 10% pause threshold + this engine's 2% ceiling;
                 # nets out matched enrollment-move pairs; attributes contact-row
                 # deletes to `contacts`, not `students`; catches unexplained row loss
  sftp_push.py   # SFTP upload (paramiko, imported lazily); asserts stack completeness
                 # before uploading; writes last_pushed_counts.json after a real push
  audit.py       # JSON / Markdown / history.jsonl report generation;
                 # preflight() verifies logs/ is writable before a run proceeds
  runner.py      # orchestrates one full run start to finish; holds the per-district
                 # file lock; resolves cadence in the district's own timezone
  cli.py         # drift-engine command-line entry point; exit-code contract (0-3)
config/districts.yml   # district registry: id, SFTP details, fingerprint,
                        # eventing_verified, timezone, email domains, area codes
.env.example            # credential env var names (never real values)
state/<district>/       # baseline/ (pristine export, never written to) + current/
                         # (working stack) + .lock (per-run exclusivity) +
                         # baseline_counts.json + last_push.json + last_pushed_counts.json
logs/<district>/        # per-run JSON + Markdown reports, plus history.jsonl
docs/SCHEMA.md           # the CSV contract, the engine-added columns, and the
                         # row-per-contact shape on students.csv
docs/RUNBOOK.md          # operational guide
docs/index.html          # GitHub Pages project page
```

## Running tests

Real `pytest` is authoritative for this project:

```bash
pip install -e ".[dev]"
pytest
```

The build sandbox this project was developed in has no PyPI access, so
`scripts/minipytest.py` also exists — a small, stdlib-only fallback runner
that understands enough of pytest's surface (`fixture`, `raises`, `mark`,
`approx`, `importorskip`, `skip`, plus `tmp_path`/`monkeypatch`/`caplog`) to
run this repo's existing test suite unmodified. It is **not** a pytest
replacement; use real pytest whenever it's available. Run it as a script,
never as `python3 -m scripts.minipytest` (see the module docstring for why):

```bash
python3 scripts/minipytest.py
```

Current result: **249 passed, 2 skipped** (the 2 skips are `paramiko`
host-key-policy tests — `paramiko` isn't installable in the no-PyPI build
sandbox; they run under real pytest with `paramiko` installed). Real
paramiko behaviour (retry, timeout, host-key policy, size verification) is
therefore code-reviewed but never actually executed in this build
environment — watch the first live push closely.

## Known limitations

Documented honestly rather than hidden — none of these block sandbox use,
but all four should be understood before a first live push:

1. **~~Teacher population has no attrition.~~ RESOLVED 2026-08-07.** The
   Friday `big_teacher` bucket used to add one new teacher every week with
   nothing ever removing one (+26 teachers measured over 26 simulated
   weeks), which extrapolated would have breached `safety.MAX_SCALE_DRIFT`
   (25%) roughly 7 years out. `selection._big_teacher` now pairs every
   weekly addition with a removal of one teacher from a *different* school,
   via `cadence.BIG_TEACHER_TEACHERS_REMOVED` (1). No new `EventType` was
   needed — `USERS_DELETED` with `EventSubject.TEACHER` already existed. The
   removal is only ever applied to a candidate teacher whose sections can
   all be safely handed off first: any section where they are primary
   teacher is reassigned to another teacher at the same school, and any
   section where they are a co-teacher has that slot cleared — so a removal
   can never leave a section pointing at a teacher id that no longer
   exists. If no such safe candidate exists in another school this run
   (only possible in a school with exactly one teacher), the removal is
   skipped for that run rather than forced through.
2. **~~`eventing_verified` is still `false`.~~ RESOLVED 2026-08-07.** David
   confirmed the "Enable events" toggle is ON in the Clever dashboard for
   this district (brief §9) -- Secure Sync eventing is active.
   `config/districts.yml`'s `eventing_verified` is now `true`.
3. **paramiko is untested in this build.** See the test-suite note above --
   real SFTP transport behaviour is reviewed, not executed.
4. **Contacts are never scale-checked, because their baseline is zero.**
   `safety.assert_scale_sane` skips any record type whose *baseline* count is
   `0`, and `baseline_counts.json` is written once on a district's genuine
   first run and never re-anchored afterwards. For this district that bakes in
   `contacts: 0` permanently, so contact growth during seeding -- and, more
   importantly, later contact *attrition* -- is never scale-checked at all.
   This is not a hole in deletion protection: the guardrail's 10% per-run
   deletion threshold still covers contact deletion, and now attributes
   contact-row deletes to `contacts` correctly. But if a deliberate
   re-baseline step is ever added post-seeding, that is the exact moment this
   gate starts working -- it should be a considered decision, not a side
   effect of someone tidying up state files.

Resolved (both previously listed here as open risks):

- Whether the `Middle name`/`Teacher 2 id` engine-added columns loading as
  empty produces a first-sync event burst. It does not -- see "Resolved" under
  [Event types produced](#event-types-produced) above.
- Whether contacts belong in their own `contacts.csv`. They do not, and the
  engine has been reworked accordingly -- verified against SFTP Instructions
  v2.1.1 on 2026-08-05. See "Confirmed, then fixed" under
  [Event types produced](#event-types-produced) above.

## More documentation

- [docs/SCHEMA.md](docs/SCHEMA.md) -- the CSV contract, the engine-added
  columns and why they were needed, and the row-per-contact shape of
  `students.csv`.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operational guide: setup, go-live
  checklist, staged seeding, reading audit reports, troubleshooting.
- [CHANGELOG.md](CHANGELOG.md) — release history.
