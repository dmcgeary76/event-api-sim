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
> [Safety model](#safety-model) below. Adding a new SFTP username to
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
| Friday | `small_daily` + `big_teacher` | Small daily, plus co-teacher swaps, a primary-teacher reassignment, and one brand-new teacher added. |
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

### KNOWN BLOCKER — contacts are not a separate CSV on Clever's real SFTP feed

**Status: BLOCKED, not yet fixed — do not assume this engine's `contacts.csv`
is correct.** Per Clever's SIS CSV documentation
([Contacts & guardians](https://dev.clever.com/docs/contacts-guardians)),
contacts shared over SFTP are **not** their own `contacts.csv` file. They are
a fixed set of columns **on `students.csv` itself** —
`contact_name`, `contact_type`, `contact_relationship`, `contact_phone`,
`contact_phone_type`, `contact_email`, `contact_sis_id` — repeated up to
**5 times per student** (`contact_name_2`, `contact_email_2`, etc.). This
engine currently writes a **separate `contacts.csv`**, which Clever will most
likely simply ignore, meaning none of this engine's predicted
`users.created`/`users.updated`/`users.deleted` (Contacts) events would
actually fire against a real sandbox ingest.

A second, related wrinkle for whenever this is picked up: a contact's Clever
ID is only stable across syncs when `contact_sis_id` is populated on that
student row. Without it, Clever derives the contact's identity from
name+email, so something as small as an email correction reads to Clever as
a **delete-then-create of a whole new contact**, not an update — the exact
opposite of what a "contact field edit" is meant to demonstrate.

This is explicitly **deferred** — do not implement the `students.csv`-column
rework without David first verifying which CSV shape his actual sandbox
SFTP endpoint accepts. Tracked here so it is not silently forgotten.

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
| `seed [--district ID] [--limit N=4000] [--live] [--seed N] [--canned-content]` | Create baseline `contacts.csv` guardian records for students that have none, `--limit` students at a time. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for why this must be staged. |
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
   which is the one legitimate "first run ever" case.
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
  addresses). See [docs/RUNBOOK.md](docs/RUNBOOK.md) for what this means
  when dropping in a new CSV export.
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
  schema.py      # CSV column contract, file specs, natural keys
  models.py      # shared dataclasses: Change, RunPlan, RunResult, EventType,
                 # EventSubject (see the corrected event-type note in this file)
  safety.py      # sandbox-only hard gates: host/username allowlist, fingerprint
                 # (presence + strength), scale sanity, production-marker tripwire
  config.py      # loads config/districts.yml + .env (PyYAML, with a stdlib fallback parser)
  csvstack.py    # in-memory CSV load/apply/save; raises on a missing required
                 # column; save is all-or-nothing across the whole stack
  cadence.py     # deterministic day-of-week bucket logic
  selection.py   # randomized (seeded) target selection within the fixed cadence;
                 # re-rolls/skips any UPDATE whose value wouldn't actually change
  content.py     # the ONLY module that touches an LLM — isolated per brief §5
  seed.py        # one-time/staged contacts.csv baseline seeding
  guardrail.py   # Clever's 10% pause threshold + this engine's 2% ceiling;
                 # nets out matched enrollment-move pairs; catches unexplained row loss
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
docs/SCHEMA.md           # the CSV contract and the three engine-added deviations
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

Current result: **222 passed, 2 skipped** (the 2 skips are `paramiko`
host-key-policy tests — `paramiko` isn't installable in the no-PyPI build
sandbox; they run under real pytest with `paramiko` installed). Real
paramiko behaviour (retry, timeout, host-key policy, size verification) is
therefore code-reviewed but never actually executed in this build
environment — watch the first live push closely.

## Known limitations

Documented honestly rather than hidden — none of these block sandbox use,
but all four should be understood before a first live push:

1. **Teacher population has no attrition.** The Friday `big_teacher` bucket
   adds one new teacher every week with nothing ever removing one (+26
   teachers measured over 26 simulated weeks). Extrapolated, this eventually
   breaches `safety.MAX_SCALE_DRIFT` (25%) — roughly 7 years out at the
   current rate — at which point every run for that district would block on
   the scale-sanity gate. Deliberately not fixed yet: this is follow-up work,
   not a bug. Note this no longer needs a *new* `EventType` — `USERS_DELETED`
   with `EventSubject.TEACHER` already exists — it just needs selection
   logic that picks a teacher to remove.
2. **KNOWN BLOCKER: contacts are not their own CSV on Clever's real SFTP
   feed.** See "KNOWN BLOCKER" under [Event types produced](#event-types-produced)
   above — this engine's `contacts.csv` is very likely the wrong shape for a
   real sandbox ingest. Deferred pending David verifying the CSV spec his
   sandbox actually accepts; do not implement the rework speculatively.
3. **`eventing_verified` is still `false`.** Secure Sync / district-app
   token eventing has not been confirmed active for this district (brief
   §9). Must be verified in the Clever dashboard before any partner-facing
   use.
4. **paramiko is untested in this build.** See the test-suite note above —
   real SFTP transport behaviour is reviewed, not executed.

Resolved (previously listed here as an open risk): whether the
`Middle name`/`Teacher 2 id` engine-added columns loading as empty produces a
first-sync event burst. It does not — see "Resolved" under
[Event types produced](#event-types-produced) above.

## More documentation

- [docs/SCHEMA.md](docs/SCHEMA.md) — the CSV contract, the three engine-added
  columns/files, and why they were needed.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operational guide: setup, go-live
  checklist, staged seeding, reading audit reports, troubleshooting.
- [CHANGELOG.md](CHANGELOG.md) — release history.
