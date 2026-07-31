# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
  run for that district would block. Deliberately not fixed yet — it needs
  a new `EventType` for teacher removal.
- Whether Clever's Events API emits `users.updated`/`sections.updated` for a
  column going from absent to empty (the `Middle name`/`Teacher 2 id`
  first-sync case) is not verified against Clever's real ingest behaviour.
  If it does, the first live sync is a very large, one-time event burst.
  This is the single biggest unknown in the project.
- `eventing_verified` is still `false` in `config/districts.yml` — Secure
  Sync / district-app token eventing has not been confirmed active for this
  district (brief §9). Must be verified before partner-facing use.
- Real `paramiko` SFTP behaviour (retry, timeout, host-key policy, size
  verification) is code-reviewed but never executed — `paramiko` is not
  installable in this build environment, so 2 tests skip. Watch the first
  live push closely.
