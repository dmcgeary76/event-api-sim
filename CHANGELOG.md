# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.1] - 2026-08-05

### Fixed

- **`staff.csv` is now optional, matching Clever's real SFTP spec.**
  Confirmed against Clever's SFTP specification v2.2.0 (fetched directly from
  Clever's internal tooling): only five files must be uploaded together --
  schools, students, teachers, sections, enrollments. `staff.csv` is
  documented as optional ("Staff are non-teachers not in class rosters").
  0.2.0 tightened `sftp_push._assert_stack_complete` to require every file in
  `schema.ALL_SPECS` unconditionally, which closed the real `contacts.csv`
  loophole but incidentally made `staff.csv` stricter than Clever's own spec.
  New `schema.OPTIONAL_FILES` (currently just `staff.csv`) is wired into
  `CsvStack.load` (absent file loads as zero rows), `CsvStack.save` (a
  zero-row optional file is not written), and `sftp_push._assert_stack_complete`
  (absence tolerated only when the in-memory stack agrees it is genuinely
  empty). Distinct from the pre-0.2.0 `contacts.csv` exception on purpose:
  that one existed because the ENGINE owned the file; this one exists because
  CLEVER'S OWN SPEC says the file is optional. No effect on the real Tulsa
  stack, which always has 280 staff rows.
- Independently re-confirmed the 0.2.0 contacts rework against Clever's own
  internal district-system-settings tooling: the `Students` export's field
  list carries exactly `Contact_name`, `Contact_relationship`, `Contact_type`,
  `Contact_phone`, `Contact_phone_type`, `Contact_email`, `Contact_sis_id` as
  columns on the student record, matching what was implemented. Also surfaced
  `Contact_pickup_rights`, a field absent from the public SFTP spec text --
  correctly left unimplemented, since adding an undocumented column would be
  rejected by this engine's own unknown-column check (and likely by Clever's
  real ingest too).

### Verified against the live district

Checked the real Tulsa replica sandbox (district id `6a6a50609ce9a77c08b6587e`)
directly via Clever's internal tooling, not just simulated locally: no sync
hold, sync engine active and unpaused, last successful/attempted sync
2026-07-29 (the original baseline load: 33,621 students created, 0 contacts,
0 student_contacts -- confirms nothing contact-related has ever synced).
Secure Sync / Events API eventing activation is not visible through any of
these tools and still requires a direct check in the Clever dashboard;
`eventing_verified` remains `false`.

## [0.2.0] - 2026-08-05

Resolves the project's one remaining hard blocker: contacts were the wrong CSV
shape entirely. The blocker was confirmed real, not assumed away, and the
engine reworked around the verified spec. Also fixes two latent data-corruption
bugs found while rebuilding the test suite.

### Fixed

- **KNOWN BLOCKER RESOLVED: contacts are rows on `students.csv`, not a
  separate `contacts.csv`.** Confirmed against Clever's official **SFTP
  Instructions v2.1.1 (Dec 2025)** and the standard SFTP allowable-fields
  list. The spec states the mechanism directly under students.csv: "In order
  to provide multiple parent/guardian contacts, you may create multiple rows
  for a single student with different contact information." So the 5-contacts-
  per-student SFTP limit is **up to 5 rows sharing one `Student id`**, each
  carrying one contact via seven unsuffixed columns (`Contact relationship`,
  `Contact type`, `Contact name`, `Contact phone`, `Contact phone type`,
  `Contact email`, `Contact sis id`) -- not a separate file, and not numbered
  column slots. The engine's standalone `contacts.csv` would have been ignored
  on ingest (or rejected as unknown columns), meaning **none** of its predicted
  contact events would ever have fired. `schema.CONTACTS` is deleted;
  `ALL_SPECS` is six files, not seven.
- **`contact_name_2` .. `_5` do not exist.** An interim reading of the dev
  docs suggested contacts widened into numbered column slots the way
  co-teachers do. They do not. `sections.csv` genuinely does use numbered
  suffixes (`Teacher 2 id` through `Teacher 10 id`); contacts repeat as rows.
  Two different patterns in one spec, now called out explicitly in
  `schema.py` and `docs/SCHEMA.md` so they are not conflated again. All row
  expansion goes through a single function, `schema.expand_contact_rows`, so
  a future spec correction is one function rather than a rework.
- **`Contact sis id` is minted per contact and never edited.** Per Clever's
  docs, a contact carrying an sis id keeps the same Clever id across
  phone/email/name changes; without one, identity is derived from name+email
  (else name+phone, else name+type+relationship+phone type), so editing an
  email changes the identity key itself and reads as delete-then-create rather
  than `users.updated`. Confirmed by a live case: when IDEA Public Schools
  added `contact_sis_id`, PE confirmed every existing contact Clever id would
  change, there being no id-preservation path once the identity basis shifts.
  Seeded contacts use `SEED<student id>-<n>`, drift-added ones `CON######`;
  the column is deliberately absent from `STUDENTS.mutable`.
- **`students.csv`'s natural key is now `(Student id, Contact sis id)`.**
  `Student id` alone is no longer unique, and leaving the old single-column
  key would have been silent data loss rather than an error: `CsvStack.index`
  would collapse a student's contact rows last-one-wins, and `get` would
  return an arbitrary sibling.
- **Seeding no longer bricks the district on the scale-sanity gate.**
  `CsvStack.counts` now reports `students` as **distinct `Student id`** and
  adds a derived `contacts` count, because contacts have no file of their own
  to count rows in. Reported as raw rows, seeding takes students.csv from
  33,621 to ~52,900 -- a **+57%** move straight through
  `safety.MAX_SCALE_DRIFT` (25%). Since a stale baseline is a hard
  `SafetyViolation` and not a silent re-anchor, every run would then have
  blocked until someone re-baselined by hand, mid-seed.
- **Guardian churn no longer inflates the student deletion ratio.** A contact
  removal is a `students.csv` row delete, so attributing deletions by filename
  alone would walk the *students* ratio toward Clever's 10% pause threshold on
  routine churn -- and, worse, camouflage a genuine student deletion inside
  that noise. `guardrail._attributed_record_type` counts a change against
  `contacts` when its `event_subject` is CONTACT, and against `students` only
  when it is genuinely about the student. Attribution keys on the predicted
  Clever-level effect rather than the CSV operation, because for contacts the
  two legitimately diverge: filling a student's blank row is a CSV UPDATE but
  a contact CREATE to Clever.
- **Student-level edits fan out across a student's rows.** An edit to
  `Middle name` or `Student email` landing on only one of a student's rows
  would leave that student presenting two different values for one field in a
  single file. `CsvStack.apply` now copies student-level columns to every
  sibling row; contact-level columns deliberately stay put. Selection also
  draws from a new `distinct_students()` rather than raw rows, so a student
  with three guardians is no longer three times as likely to be picked.
- **A student's last row is never deleted while removing a contact.** That
  would delete the student along with the guardian. `selection.py` already
  refused to remove a student's final contact; `CsvStack.apply` now enforces
  it independently as a backstop, since the cost of getting it wrong is
  destroying a real student record.
- **`CsvStack.apply` deleted rows by a stale index.** Row positions were
  resolved in the validation pass and reused in the mutation pass, so the
  second of two DELETEs on the same file used an index that had already
  shifted -- an `IndexError` in a short file, or **silently deleting an
  innocent bystander row** in a long one. Predates this rework, but the
  row-per-contact shape makes it far easier to hit (removing two guardians
  from one student, or two enrollments from one section). The position is now
  re-resolved by object identity immediately before deletion.
- **Guardrail move-netting was loose enough to defeat itself.** Netting
  matched any CREATE against any DELETE of the same record type, so a Tue/Thu
  run adding 4 guardians and removing 2 computed `min(2, 4) == 2` and netted
  every contact deletion away to zero -- the guardrail could never report
  contact attrition at all. Netting is now matched on record *identity*
  (record type, `Student id`, and `Contact sis id` for contacts), so a genuine
  enrollment move still nets to zero while unrelated guardian churn does not.

### Changed

- **`FileSpec.engine_added` removed.** `contacts.csv` was its only user. The
  flag's two branches (skip a missing engine-added file on load, omit a
  zero-row one on save) now contradicted
  `sftp_push._assert_stack_complete`, which requires every file in
  `ALL_SPECS` unconditionally -- a stack `save` produced would have been
  rejected by `push`. Every file is now always written, header-only if empty.
- `sftp_push._assert_stack_complete` no longer has any exception: a missing
  file is always `IncompleteStackError`.
- A stale pre-0.2.0 `contacts.csv` in `current/` cleans itself up on the next
  `save`, which promotes a freshly staged directory containing only
  `ALL_SPECS`. It was never pushed live, so there is no remote copy.
- Test suite: 249 passing (was 222), 2 skipped (paramiko, unchanged).

### Verified against the real stack

Not just unit-tested -- run end to end against the 33,621-student Tulsa
replica export:

- `estimate-seed` unchanged at 33,621 students / 52,931 expected contacts /
  4,000 per staged run across ~9 runs.
- A staged `seed --limit 4000` dry run created 6,771 contacts across 4,000
  students, taking students.csv to 36,392 rows while `counts()["students"]`
  stayed 33,621. Scale sanity passed; the same run measured as raw rows would
  have read as +57% drift after a full seed.
- After a full seed (52,819 contacts): Monday predicted 6 `users.updated
  (Contacts)` + 4 `users.updated (Students)`; Tuesday added 8
  `sections.updated`, 3 `users.created (Contacts)` and 2 `users.deleted
  (Contacts)`, with the guardrail attributing those deletions to **contacts at
  0.0038%** -- far below the 2% warn and 10% block; Friday added 3
  `sections.updated` and 1 `users.created (Teachers)`. This is the brief's
  §11 success criteria met on real data.
- Integrity after three simulated days of drift: zero sibling-row column
  disagreements, zero students over the 5-contact cap, zero students orphaned
  to no contacts, all 52,820 `Contact sis id` values unique.

### Known limitations

- `eventing_verified` is still `false` in `config/districts.yml`. Unchanged
  and still required before partner-facing use.
- The Friday bucket adds one teacher a week with no attrition, breaching
  `MAX_SCALE_DRIFT` in roughly 7 years. Still deliberately deferred.
- Real `paramiko` SFTP behaviour remains code-reviewed but never executed (not
  installable here; 2 tests skip). Watch the first live push.
- **New:** `safety.assert_scale_sane` skips any record type whose *baseline*
  is 0, and `baseline_counts.json` is written once on a district's first run
  and never re-anchored. For this district `contacts: 0` is baked in, so
  contact growth and later contact attrition are never scale-checked. The 10%
  per-run deletion guardrail still covers contact deletion, so this is not a
  hole in deletion protection -- but if a post-seeding re-baseline step is
  ever added, that is the moment this gate starts working, and it should be a
  deliberate decision rather than a side effect.

### Note on partner communication

On the SIS-managed auto-sync side, `contact.sis_id` is only honored for
Infinite Campus, IC OneRoster API, Skyward, and Skyward API. Irrelevant to
this SFTP sandbox, but worth a footnote to any partner who will compare
sandbox behaviour against a real auto-synced district's feed.

## [0.1.0] - 2026-07-30

Initial release. Full build of the sandbox Events API data drift engine per
`sandbox-events-api-data-drift-project-brief.md`.

### Added

- **Core module set** (`src/drift_engine/`): `schema` (CSV column contract),
  `models` (shared `Change`/`RunPlan`/`RunResult`/`EventType` dataclasses),
  `config` (loads `config/districts.yml` + `.env`, with a strict stdlib
  fallback YAML parser for PyPI-less environments), `csvstack` (in-memory
  CSV load/apply/save), `cadence` (deterministic day-of-week bucket logic),
  `selection` (seeded, randomized target selection within the fixed weekly
  cadence), `content` (isolated AI-content-generation boundary), `seed`
  (one-time/staged `contacts.csv` baseline seeding), `guardrail` (deletion
  threshold enforcement), `sftp_push` (SFTP upload), `audit` (JSON/Markdown/
  history.jsonl reporting), `runner` (per-run orchestration), and `cli`
  (`drift-engine` command-line entry point).
- **Fixed weekly cadence**: small daily field edits every weekday; big
  student changes (enrollment moves, contact adds/removes) on Tuesday and
  Thursday; big teacher changes (co-teacher swaps, reassignment, new
  teacher) on Friday; weekends skipped. Magnitudes are hard-coded constants,
  not configuration, per brief §10.
- **Six Events API categories produced**: `contacts.created`,
  `contacts.updated`, `contacts.deleted`, `sections.updated`,
  `users.updated`, and `teachers.created` (added beyond the brief's original
  five, since Friday's "add a new teacher" sub-bucket structurally requires
  its own event type).
- **`drift-engine` CLI**: `schedule`, `plan`, `run` (dry-run by default,
  `--live` to push), `seed` (staged `contacts.csv` seeding), `estimate-seed`
  (read-only volume estimate), `history` (recent-run summary), and
  `simulate-week` (in-memory Mon-Fri preview).
- **Isolated AI content-generation boundary** (`content.py`): a
  `ContentGenerator` protocol with a stdlib-only `CannedContentGenerator`
  and an `AnthropicContentGenerator` (Claude Haiku, batched requests,
  every value validated before use, degrading to canned content on any
  failure). `selection.py` and `seed.py` are coded only against the
  protocol, never a concrete implementation, per brief §5.
- **Audit layer** (`audit.py`): JSON, Markdown, and append-only
  `history.jsonl` artefacts per run, answering brief §9's open question
  ("how will David review what changed on a given day without manually
  diffing CSVs"). The Markdown report leads with expected Events API
  activity, marks dry runs unmistakably, and never suppresses a failed
  run's report.
- **Three engine-added schema deviations**, documented in
  `docs/SCHEMA.md`: a `Middle name` column on `students.csv`, a
  `Teacher 2 id` column on `sections.csv`, and the entire `contacts.csv`
  file — all optional fields under Clever's SIS CSV spec, added because the
  real sandbox export had no mutable surface for three of the six event
  categories the brief requires.
- **`scripts/minipytest.py`**: a stdlib-only pytest-compatible fallback test
  runner, for environments without PyPI access. Real pytest remains
  authoritative.
- Full project documentation set: `README.md`, `docs/SCHEMA.md`,
  `docs/RUNBOOK.md`, this changelog, and `docs/index.html` (GitHub Pages
  project page).

- **Per-district configuration for email domains, phone area codes, and
  timezone** (`config.py`, `content.py`): `DistrictConfig` gained
  `timezone` (default `America/Chicago`), `staff_email_domain`,
  `student_email_domain`, and `area_codes`, all consumed by
  `build_content_generator` so a second sandbox district gets its own
  generated emails/phone numbers instead of Tulsa's. `ContentGenerator`
  gained a `teacher_email()` method so `selection.py` never builds an email
  address inline. This is what makes brief §6's "adding a district is a
  config-only change" actually true end-to-end.
- **`drift-engine` exit-code contract**: `0` success, `1` an ordinary run
  failure, `2` a `SafetyViolation`, `3` another run already in progress for
  this district. Documented once in `cli.py` and never violated elsewhere.

### Fixed

Findings from an independent audit of the initial build, all addressed
before this version's first live use:

- **Data fingerprint was not actually a safety check.** `data_fingerprint`
  is now validated for *strength* at config load time and again at write
  time (`safety.validate_fingerprint`): at least 8 characters, no
  whitespace, contains a `.`, and contains a recognised sandbox marker
  (`replica`/`sandbox`/`sbx`/`dev`/`test`/`demo`/`staging`). Previously any
  non-empty string passed — e.g. `data_fingerprint: "@"` — which could match
  almost any stack, including real production data. This was the audit's
  most serious finding.
- **The scale-sanity check could be silently skipped.** `assert_scale_sane`
  now runs *inside* `assert_safe_target` itself whenever both current and
  baseline counts are supplied, so it can no longer be bypassed by a caller
  that runs the other gates but forgets this one. A missing or corrupt
  `baseline_counts.json` is now a hard `SafetyViolation` on any run after
  the district's genuine first one (tracked via a `last_push.json` marker),
  not a warning-and-skip.
- **`SafetyViolation` is now always audited before it propagates.**
  `runner.py` writes the run's audit record, then re-raises — a safety
  failure is never silently un-recorded just because it's also fatal.
- **Overlapping runs could corrupt data.** `run_once` now holds an
  exclusive, non-blocking `flock` on `state/<district>/.lock` for the whole
  of a run; a second concurrent run for the same district exits immediately
  with code 3 instead of racing the first. Previously two overlapping runs
  could both mint the same "next" contact ID for two different students,
  silently re-parenting one child's guardian record onto another.
- **Cadence used host/UTC time instead of the district's own.** A new
  `timezone` field on `DistrictConfig` (default `America/Chicago`) is what
  cadence resolves "today" against. Previously a Friday-evening run on a
  UTC host could resolve to Saturday and silently skip the entire Friday
  big-teacher bucket — while still reporting a successful run.
- **The guardrail was blind to loss that happened before selection ran.**
  `guardrail.evaluate`/`enforce` now accept `last_pushed_counts` (written by
  `sftp_push` after every real push) and flag unexplained row loss — e.g. a
  truncated CSV export — that the old intent-only check could never see,
  since the damage was already baked into the stack before selection saw it.
  Matched CREATE/DELETE pairs from the same run (an enrollment move) are now
  netted out first, so a move is no longer miscounted as a deletion.
- **A missing CSV column was silently backfilled with blanks.**
  `CsvStack.load` now raises if a required (non-engine-added) column is
  missing from a file's header. Previously it backfilled with `""`, which
  would have pushed all 33,621 students with their email addresses blanked
  had a truncated export been loaded. `CsvStack.save` is now all-or-nothing
  across the whole stack (staged, then promoted with one atomic rename)
  instead of atomic per file.
- **A partial local stack could be pushed and reported as success.**
  `sftp_push.push` now asserts the whole stack is present on disk before
  describing or uploading anything. Its `allowlist` argument is now
  required — the previous optional default silently fell back to loading
  the default config location, which could allowlist-check a run against
  the wrong config entirely.
- **Every `contacts.csv` Email "update" wrote the identical value.**
  `guardian_email`/`student_email` were pure functions of their inputs, so a
  repeat "edit" recomputed the same address; Clever's CSV diff saw nothing,
  so the event never actually fired. Measured: ~62% of predicted
  `contacts.updated` events over 26 simulated weeks were silent no-ops.
  `selection.py` now re-rolls a generated value (varying alias style) up to
  4 times and skips the change entirely if it still wouldn't differ.
  Measured after the fix: 0% no-op rate across every updated field.
- **`stats()` over-counted AI-vs-canned values by 2-3x.** Fixed so this
  project's one real instrument for judging whether the AI content step is
  worth keeping reports accurate numbers.
- **An unwritable `logs/` directory could push live with zero audit
  trail.** `audit.preflight(logs_root)` now runs before any work and aborts
  the run if `logs/` can't actually be written to (probed, not just checked
  via permission bits).
- **Dry-run output accumulated PII forever.** Each dry run writes a full
  ~7MB copy of the stack, including student names, DOBs, and guardian
  contact details. Output is now pruned to the 5 most recent directories
  per district after each successful dry run.
- **`EventType` predicted event names that Clever does not emit (2026-08-03).**
  The initial build's `EventType` enum, following the project brief §3,
  included `CONTACTS_CREATED`/`CONTACTS_UPDATED`/`CONTACTS_DELETED` and
  `TEACHERS_CREATED` — none of these are real Clever Events API wire events.
  Verified against Clever's live dev docs
  ([Events API](https://dev.clever.com/docs/events-api),
  [Contacts & guardians](https://dev.clever.com/docs/contacts-guardians)):
  in API v3.x, contacts (guardians), students, teachers, staff, and district
  admins are all `users` objects — the only object-level wire events are
  `users.created`/`users.updated`/`users.deleted`, with role carried in the
  object's own `roles` node, not the event name. `EventType` now contains
  only the six real wire events (`users.*` x3, `sections.*` x3). A new
  `EventSubject` enum plus a required `Change.event_subject` field restore
  the student/teacher/contact/staff breakdown for reporting purposes only
  (`Change.expected_event_label`, e.g. `"users.updated (Contacts)"`) — this
  is never part of the event Clever actually emits. `RunResult.event_counts()`
  is now keyed by that label; a new `RunResult.wire_event_counts()` carries
  the bare wire-name totals (what the partner's real `/events` feed shows).
  The audit JSON schema is bumped to v2 to reflect this (`audit.SCHEMA_VERSION`).
  The project brief's §3 assumption that contacts had their own distinct
  event lifecycle was simply wrong; the brief itself is left unedited as the
  historical input document, and this correction is called out at the top of
  `models.py` so it doesn't read as a regression to a future maintainer.

### Security

- **Sandbox-only hard constraint** (`safety.py`): every write path routes
  through `assert_safe_target` before a single byte is written, checking a
  host allowlist, an SFTP **username** allowlist — chosen over hostname
  because `sftp.clever.com` is shared infrastructure and proves nothing
  about the target — a strength-validated data fingerprint that must appear
  in the district's own loaded data, scale sanity, and an advisory
  production-marker tripwire. `SafetyViolation` is never caught or
  downgraded anywhere in this codebase, and is always audited before it
  propagates.
- **Scale-sanity check** (`assert_scale_sane`): a stack whose record counts
  have moved more than 25% from its recorded baseline is refused, since that
  looks like a different district, not incremental drift. Runs inside
  `assert_safe_target` itself once counts are available; a missing/corrupt
  baseline is a hard failure after a district's first run, never a silent
  re-anchor.
- **Per-district run exclusivity**: an exclusive file lock on
  `state/<district>/.lock` for the whole of a run prevents two overlapping
  runs from racing each other and corrupting minted IDs.
- **Deletion guardrail** (`guardrail.py`): Clever's real 10% pause-for-review
  threshold is enforced as a hard block; this engine's own stricter 2%
  ceiling is enforced as a warning. Matched enrollment moves are netted out;
  unexplained row loss versus the last real push is caught. Evaluated before
  anything is written or pushed.
- **Dry-run by default**: `run` and `seed` never open an SFTP connection
  without an explicit `--live` flag; there is no configuration path that
  changes this default. Dry-run output retention is capped at 5 directories
  per district.
- **CSV load/save/push hardening**: a missing required column is a hard load
  error, not a silent blank backfill; `CsvStack.save` is all-or-nothing
  across the whole stack; `sftp_push.push` asserts stack completeness before
  uploading and requires its allowlist argument explicitly.
- **SFTP host key policy defaults to reject** (`paramiko.RejectPolicy`), not
  trust-on-first-use, since this connection carries a live credential
  uploading PII. `SFTP_ALLOW_UNKNOWN_HOST_KEY=1` is an explicit, loudly
  logged opt-in for genuine first-time connections.
- **Credential handling**: SFTP passwords are never stored in
  `config/districts.yml` — only the name of the environment variable
  holding each one. Passwords are never logged.
- **Scoped redaction in audit output** (`audit.py`): any dict key matching
  `password|secret|token|key` (case-insensitive) inside `Change.before`/
  `after`, `RunResult.guardrail`, or `content_stats` is replaced with a
  placeholder before being written to disk.
- **Audit preflight**: `logs/` writability is verified, by probe write, once
  before any run does any work — never after a live push has already
  happened with nowhere to record it.

### Known limitations

Documented honestly rather than hidden; none block sandbox use today:

- The Friday `big_teacher` bucket adds one new teacher a week with no
  attrition (+26 over 26 simulated weeks). Extrapolated, this breaches
  `safety.MAX_SCALE_DRIFT` (25%) after roughly 7 years, at which point every
  run for that district would block. Deliberately not fixed yet — follow-up
  work, not a bug. Correction (2026-08-03): this no longer needs a *new*
  `EventType` — `USERS_DELETED` with `EventSubject.TEACHER` already exists in
  the corrected enum — it just needs selection logic that picks a teacher to
  remove.
- **KNOWN BLOCKER, not yet fixed: contacts are very likely the wrong CSV
  shape.** Per Clever's SIS CSV docs
  ([Contacts & guardians](https://dev.clever.com/docs/contacts-guardians)),
  contacts shared over SFTP are not a separate `contacts.csv` — they are
  columns on `students.csv` (`contact_name`, `contact_type`,
  `contact_relationship`, `contact_phone`, `contact_phone_type`,
  `contact_email`, `contact_sis_id`), capped at 5 contacts per student. This
  engine currently writes a separate `contacts.csv`, which Clever will most
  likely ignore — meaning none of this engine's predicted
  `users.created`/`users.updated`/`users.deleted` (Contacts) events would
  actually fire against a real ingest. Relatedly, a contact's Clever id is
  only stable when `contact_sis_id` is populated; otherwise Clever derives
  identity from name+email, so an email edit reads as a delete+create, not
  an update. **Status: BLOCKED** pending David verifying the CSV spec his
  sandbox actually accepts. Deliberately **not implemented** here — do not
  attempt this rework speculatively; see `docs/SCHEMA.md`'s "KNOWN BLOCKER"
  section.
  **RESOLVED in 0.2.0 (2026-08-05)** — the blocker was real, and the caution
  was warranted: the shape was wrong in a way this entry did not fully
  anticipate. Contacts are not numbered columns on the student row but
  *repeated rows* per `Student id`, and the column names are spaced and
  capitalized (`Contact name`, not `contact_name`). See the 0.2.0 entry above
  for the verified spec and the rework.
- `eventing_verified` is still `false` in `config/districts.yml` — Secure
  Sync / district-app token eventing has not been confirmed active for this
  district (brief §9). Must be verified before partner-facing use.
- Real `paramiko` SFTP behaviour (retry, timeout, host-key policy, size
  verification) is code-reviewed but never executed — `paramiko` is not
  installable in this build environment, so 2 tests skip. Watch the first
  live push closely.

**Resolved** (previously listed here as an open risk, corrected 2026-08-03):
whether Clever's Events API emits `users.updated`/`sections.updated` for a
column going from absent to empty (the `Middle name`/`Teacher 2 id`
first-sync case). It does not — Clever's `users.updated` fires on a genuine
object change (surfaced via `previous_attributes`), and an absent-to-empty
column is not one. `runner.py`'s log line on migrated columns has been
corrected to say so instead of warning that the next sync WILL show these as
field changes.
