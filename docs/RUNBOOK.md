# Runbook

Operational guide for running, monitoring, and troubleshooting the drift
engine. See [README.md](../README.md) for what the project is, and
[SCHEMA.md](SCHEMA.md) for the CSV contract.

## First-time setup

1. `git clone` the repo, `python3 -m venv .venv && source .venv/bin/activate`,
   `pip install -e ".[dev]"`.
2. `cp .env.example .env` and fill in the SFTP password variable named by
   the district's `password_env` in `config/districts.yml` (currently
   `SFTP_PASSWORD_STEADFAST_BACKPACK_8880`). Optionally set
   `ANTHROPIC_API_KEY` — without it the engine automatically uses canned
   content and logs that it did so; nothing fails for want of a key.
3. Place the district's initial CSV export in
   `state/<district-id>/baseline/` (currently
   `state/steadfast-backpack-8880/baseline/` — `schools.csv`, `students.csv`,
   `teachers.csv`, `staff.csv`, `sections.csv`, `enrollments.csv`; no
   `contacts.csv` yet, since that file is engine-owned). **Every required
   (non-engine-added) column must be present in each file's header** —
   `CsvStack.load` raises a `ValueError` naming the file and the missing
   column(s) rather than silently loading a short export with a field like
   `Student email` blanked on every row. If the export was regenerated or
   trimmed before being dropped in here, diff its header against
   `docs/SCHEMA.md` first.

### Where state lives

```
state/<district-id>/
  baseline/     # the original SIS export. The engine NEVER writes to this.
  current/      # the working stack this engine mutates every run — the source of truth.
  dry-run/<date>-<run_id>/   # dry-run output (what WOULD be written), one dir per dry run
  .lock                      # per-district exclusive run lock (see "Concurrency" below)
  baseline_counts.json       # record counts snapshot, for the scale-sanity safety check
  last_push.json             # marker: has a REAL (non-dry) push ever succeeded here?
  last_pushed_counts.json    # record counts as of the last successful real push
                              # (used by the guardrail's unexplained-row-loss check)
```

`current/` is seeded from `baseline/` automatically the first time **any**
command loads the stack (`RunPaths.ensure_current`, called by `run`, `seed`,
`estimate-seed`, and `simulate-week` — not by `schedule` or `plan`, which
never touch the CSVs). After that, `current/` is the accumulating state the
engine diffs against and edits every run; it is never regenerated from
`baseline/` automatically again. `baseline/` should be treated as read-only
by hand as well as by the engine — it's the recovery point (see below).

`baseline_counts.json` is written once — on this district's genuine **first
run ever, dry or live** — from the pristine, pre-edit stack, before that
run's own selection/apply step runs. (A dry run still writes it, because the
scale-sanity baseline has to come from *some* run's snapshot of the stack,
and the file only gets written when it's missing *and* there's no record of
a prior real push — see `runner.RunPaths.read_baseline_counts`.) After that
first write, a missing or corrupt `baseline_counts.json` is a hard
`SafetyViolation` on every subsequent run, not a silent re-anchor — see
[Safety model](../README.md#safety-model-in-plain-terms) in the README.

`.lock` is created empty and held for the duration of a run via an exclusive
`flock`; it is never deleted, only locked/unlocked, so seeing it on disk
between runs is normal, not a sign of a crash. `last_push.json` and
`last_pushed_counts.json` are only ever written after a **real** (non-dry)
push actually succeeds — a district that has only ever been dry-run has
neither file yet.

`state/<district-id>/dry-run/` only ever keeps the **5 most recent**
run directories (`runner.DRY_RUN_RETENTION`) — older ones are pruned
automatically after each successful dry run. Each dry-run directory is a
full copy of the stack, including student names, DOBs, and guardian contact
details, so leaving this unbounded would accumulate PII on disk
indefinitely; if you need to keep a specific dry run's output longer, copy
it out of `state/` before running again.

## Concurrency: only one run per district at a time

`run_once` holds an exclusive, non-blocking file lock on
`state/<district-id>/.lock` for the whole of a run. If a second run for the
same district is started while the first is still in flight — e.g. a
scheduler retry firing before the previous invocation finished, or someone
manually running the CLI while a scheduled task is mid-run — it does not
wait and does not proceed: it raises immediately and the CLI exits with
**code 3**. This is not just tidiness. Two overlapping runs previously could
both load the identical pre-change stack, both mint the same "next"
engine-owned contact ID (e.g. both deciding `CON000042` is the next free id)
for two *different* students, and each silently clobber the other's edits on
save — which could re-parent one child's guardian record onto another
student entirely. If you see exit code 3 or a `RunLockHeld` error in the
logs, do not retry immediately in a loop; confirm the other run has actually
finished first.

## Pre-go-live checklist

Before pointing this at a partner-facing sandbox for real:

1. **Confirm Secure Sync + district-app token eventing is active** for the
   target district in the Clever dashboard, and flip `eventing_verified:
   true` in `config/districts.yml`. This is brief §9's open item — the build
   left it `false` by default and logs a warning on every config load and
   every run while it's `false` ("Secure Sync / district-app token eventing
   has not been confirmed... Proceeding anyway"). The engine will still run
   with `eventing_verified: false` — it is a warning, not a gate — but a
   partner will see nothing on the Events API if this hasn't actually been
   turned on, no matter how correct the CSV edits are.
2. **Confirm the SFTP account is a sandbox account**, not shared/production
   infrastructure repurposed. The safety gates (host/username allowlist,
   data fingerprint presence + strength, scale sanity) protect against the
   engine being pointed at the wrong target, but they can't protect against
   a genuinely-sandbox-labeled credential that turns out not to be.
3. **Run a dry run and read the report**: `drift-engine run` (no `--live`),
   then open the Markdown report under `logs/<district-id>/` and check the
   "Mode" row says `DRY RUN`, that the expected-events table looks like a
   normal day, and that the guardrail section shows no `BLOCK`/`WARN`.
4. **Resolved, no longer a pre-go-live blocker:** whether the absent-to-empty
   `Middle name`/`Teacher 2 id` columns (see [SCHEMA.md](SCHEMA.md)) produce a
   first-sync field-change burst. They should not — `users.updated`/
   `sections.updated` fire on a genuine object change, and an absent-to-empty
   column is not one. (An earlier draft of this runbook listed this as "the
   single biggest unknown in the project"; it wasn't actually unverifiable,
   just unverified — see docs/SCHEMA.md's "Resolved" note.)
5. **KNOWN BLOCKER — verify the CSV shape your sandbox actually accepts
   before relying on any contact event.** Per Clever's SIS CSV docs, contacts
   are columns on `students.csv` (`contact_name`, `contact_type`,
   `contact_relationship`, `contact_phone`, `contact_phone_type`,
   `contact_email`, `contact_sis_id`, up to 5 per student) — **not** their
   own `contacts.csv` file. This engine currently writes a separate
   `contacts.csv`, which Clever will most likely ignore. **Do not push
   `contacts.csv` live, and do not trust any `users.created`/`users.updated`/
   `users.deleted` (Contacts) prediction from this engine, until David has
   verified which CSV shape his sandbox SFTP endpoint actually accepts.**
   This rework is explicitly deferred — see [SCHEMA.md](SCHEMA.md)'s "KNOWN
   BLOCKER" section. Do not implement it speculatively.

## First live push

Sequence these in order — do not skip ahead, and do not combine steps:

1. **Confirm eventing** (checklist item 1 above) — flip `eventing_verified:
   true` only after Secure Sync / district-app token eventing is confirmed
   active in the Clever dashboard.
2. **Resolve the KNOWN BLOCKER first** (checklist item 5 above) — verify
   whether your sandbox actually wants contacts as columns on `students.csv`
   or will accept this engine's separate `contacts.csv` as-is. Do not stage
   contact seeding against an unverified CSV shape; a "successful" staged
   seed against the wrong file shape teaches you nothing and may need to be
   entirely redone.
3. **Staged contacts seeding**: `drift-engine seed --limit 4000 --live`,
   repeated roughly **9 times** to cover the full 33,621-student district
   (see "Staged contacts seeding" below for the exact numbers and why
   seeding must be staged, not run as one pass). Watch each run's Markdown
   report before starting the next.
4. **Only then enable the daily cadence** (`drift-engine run --live` on a
   recurring weekday schedule — see "Scheduling as a recurring weekday task"
   below). Bringing up the recurring cadence before seeding is done means
   the small-daily/big-student buckets are drifting a stack that doesn't
   have its baseline guardian contacts yet, which is confusing to reason
   about and not representative of steady state.

## Staged contacts seeding

> **KNOWN BLOCKER, see above:** this section describes seeding this engine's
> own `contacts.csv`, which Clever's real SFTP ingest most likely ignores
> (contacts belong on `students.csv` as columns — see SCHEMA.md). Do not
> stage a live seed against a real sandbox until that's verified; the volume
> math below is still correct, it's the file shape that's in question.

`contacts.csv` doesn't exist until it's created — either by the normal
weekly cadence's small guardian additions, or by a one-time/staged `seed`
pass to give every existing student a baseline set of guardians.
**Do not seed the whole district in one run.**

For the real stack (33,621 students, 0 existing contacts):

```
$ drift-engine estimate-seed
  students_without_contacts: 33621
  estimated_contacts_expected: 52931
  recommended_staged_limit: 4000
  recommended_run_count: 9
```

An unbounded seed pass would emit roughly **52,900 `users.created`
(Contacts) events in a single sync** — an enormous, unrepresentative burst
that would look like an outage or a bulk import gone wrong to the partner,
nothing like the steady drift cadence this engine exists to produce. (Not a
distinct `contacts.created` event — see docs/SCHEMA.md's "Corrected event
types.")

Recommended approach: stage it at `limit=4000` students per run, about **9
runs** to cover the full district:

```bash
drift-engine seed --limit 4000              # dry run first — read the report
drift-engine seed --limit 4000 --live       # then push for real
```

`seed_contacts` is idempotent — a student who already has at least one
contact (from a prior seed run or organic drift) is skipped, so re-running
`seed --limit 4000` repeatedly, once per weekday, safely works through the
whole district without re-seeding anyone. Interleave seeding runs with (or
run them slightly ahead of) the normal weekday drift cadence, not instead of
it.

## Scheduling as a recurring weekday task

This is designed as a scheduled task (brief §6: "a scheduled Cowork task,
not a standalone always-on app, not a conversational skill"), run on
weekdays. There is nothing to schedule on weekends — `cadence.plan_for`
returns a skipped plan for Saturday/Sunday, and `run_once` exits early
without touching anything. Point the scheduled task at:

```bash
drift-engine run --live
```

on a weekday cadence (e.g. once per weekday morning). If a scheduling
mechanism can't itself skip weekends, running this command on a Saturday is
harmless — it will log `"Saturday is a weekend; no drift runs on
weekends"` and do nothing.

## Reading the audit artefacts

Every run writes three things under `logs/<district-id>/`:

1. **JSON** (`<date>-<run_id>.json`) — the full, lossless record. Machine-
   readable source of truth; read this with `audit.load_run` (or `jq`) if
   you need to script against a run's exact detail.
2. **Markdown** (`<date>-<run_id>.md`) — the one David actually opens.
   Structured around "what should the partner have seen, and did the run
   behave": an expected-events table comes first, then per-bucket change
   detail, then the guardrail verdict, then (if applicable) AI-vs-canned
   content stats. A dry run is marked unmistakably in the title itself. A
   failed run gets an unmissable failure banner plus a "What failed" section
   with the raw error — a run never fails silently into an empty report.
3. **`history.jsonl`** (append-only, one line per run) — cheap trend
   analysis across weeks: `drift-engine history --days 30` renders runs per
   weekday, cumulative expected-event counts, any failed runs, and the
   AI-vs-canned content trend, without re-parsing every full JSON report.

### "I saw no events" — how to answer a partner

Work through this in order:

1. Open the run's Markdown report. Check the **Mode** row — if it says
   `DRY RUN`, nothing was pushed at all; that's expected, not a bug.
2. Check the **Status** row. If `FAILED`, read the "What failed" section —
   the run never reached the push step.
3. Check **Buckets applied** and the expected-events table. If it's empty
   with no `error`, selection legitimately produced zero changes for that
   day (should be rare given the fixed cadence, but possible if every
   eligible record was already touched this run).
4. Confirm `eventing_verified: true` for this district in
   `config/districts.yml` — if Secure Sync / district-app token eventing was
   never actually confirmed active in the Clever dashboard (brief §9),
   Clever can ingest the CSVs without emitting anything on the Events API,
   regardless of how correct the sync was.
5. Check `history.jsonl` for the run's `run_id` to confirm it's recorded and
   check `ok: true`.

## The guardrail's two thresholds

`guardrail.py` computes, per record type, `deletes / total-rows-of-that-type`
(denominator = the stack's count **before** this run's changes, matching how
Clever itself evaluates the threshold):

| Threshold | Value | Effect |
|---|---|---|
| `SAFE_THRESHOLD` (this engine's own ceiling) | 2% | **Warns** — logged in the audit report, does not block. The fixed cadence normally deletes only a couple of contacts per run out of tens of thousands, so anywhere near 2% is worth a second look even though it's not yet dangerous. |
| `CLEVER_PAUSE_THRESHOLD` (Clever's real limit) | 10% | **Blocks** — `guardrail.enforce` raises `GuardrailViolation` before a single byte is written or pushed. |

A whole-run **net-attrition** check also runs independently of the per-type
ratios: if a run deletes meaningfully more rows than it creates (more than
2x, with at least 3 more deletes than creates), it's flagged as a warning
even if no single record type tripped its own ratio — this catches a slow,
unattended monthly bleed that no single day's ratio would ever show.

Two refinements sit on top of the raw delete count:

- **Matched moves are netted out.** A CREATE and a DELETE for the same
  record type in the same run (an enrollment section move is always a
  DELETE of the old row and a CREATE of the new one) are matched and
  excluded from the deletion count first — a student moving sections is not
  attrition, and on a small stack a single move could otherwise trip the
  ratio purely from a tiny denominator.
- **Unexplained row loss is caught.** Once a district has a real push on
  record, `guardrail.enforce` also compares the freshly-loaded stack's
  counts against `last_pushed_counts.json` (what Clever actually last
  received). Any record type that lost rows versus that count, with no
  matching planned DELETE in this run to explain it, is added to the
  effective deletion count as `unexplained_loss` — this is what catches a
  CSV truncated or altered by something other than this engine between two
  runs, which the old intent-only (planned-deletes-only) check could not
  see at all, since the damage was already baked into the stack before
  selection ever ran.

### What to do when a run is blocked

`run_once` returns with `error` set to `"guardrail blocked: ..."` and the
Markdown report's "What failed" section names the exact record type and
ratio. **Do not** try to force it through — a block here means Clever itself
would pause the real sync for review if this were pushed. Investigate:

- Is `state/<district-id>/current/` unexpectedly smaller than it should be
  (e.g. from a bad prior manual edit)? Check `stack.counts()` for that
  record type against what you expect.
- Did selection logic run against the wrong district/state root?
- Is this genuinely a one-off legitimate large change that the fixed
  cadence wasn't designed for? If so, this needs a deliberate manual
  decision, not a code change to bypass the guardrail.

## Recovery: resetting `current/` from `baseline/`

If the working stack (`state/<district-id>/current/`) ever gets into a bad
state, the recovery path is to delete it and let `RunPaths.ensure_current`
re-copy from `baseline/` on the next run that loads the stack.

**This is not a casual operation.** Because Clever's Events API diffs the
new export against whatever it last saw, resetting `current/` back to the
pristine baseline means the next sync will show every drifted record
(everything this engine has changed since baseline) reverting all at once —
a large, sudden reverse diff, the same kind of "looks like a bulk
import/rewrite" burst that staged seeding exists to avoid. Only do this
deliberately, ideally after warning the application partner, not as a
routine fix.

## Troubleshooting

**Missing password env var.** `ConfigError`, naming the missing variable
(e.g. `"Environment variable 'SFTP_PASSWORD_STEADFAST_BACKPACK_8880' is not
set (or is empty)"`). Fix: populate it in `.env`; the error message never
echoes the value itself, only the variable name.

**Unknown SFTP host key.** By default, `sftp_push._host_key_policy` uses
`paramiko.RejectPolicy` — an unrecognized host key **refuses the
connection**, rather than silently trusting it (`paramiko.AutoAddPolicy`).
This is deliberate: this connection carries a live credential uploading PII
(names, emails, guardian contact info), so blind trust-on-first-use is not
an acceptable default even against a sandbox. Opt-in escape hatch: set
`SFTP_ALLOW_UNKNOWN_HOST_KEY=1` for a genuine first-time connection to a new
endpoint — this switches to `AutoAddPolicy` **with a loud warning-level log
line every time it's used**. If this endpoint has connected successfully
before and now shows an unknown host key, stop and investigate rather than
setting this — a host key that changed unexpectedly can mean the server was
compromised or you're talking to the wrong host.

**AI content falling back to canned.** This happens automatically and
silently succeeds (by design — brief §5/§11: a scheduled task should never
fail because an API call hiccuped), for any of: no `ANTHROPIC_API_KEY` set,
`DRIFT_CONTENT_CANNED_ONLY=1` set, the `anthropic` package not installed,
or any API failure (timeout, rate limit, malformed response, validation
failure). To see whether it happened: check the run's Markdown report's
"Content generation" section (present whenever content stats were
collected), or the JSON report's `content_stats` field, for `ai_served` vs
`fallback_served` counts. `drift-engine history` also rolls this up across a
window ("AI vs. canned content" section). Force canned content deliberately
with `--canned-content` on `run`/`seed`, or `DRIFT_CONTENT_CANNED_ONLY=1` in
`.env`.

**`ValueError: ... required column(s) ... are missing from the header`.**
`CsvStack.load` refuses to load a file that is missing a real (non-engine-
added) SIS column — this is not a bug, it is the fix for a bug: previously a
short export would load anyway with that column blanked on every row and
get pushed back out that way. Fix: restore the missing column in the source
export; do not edit the engine to backfill it.

**`RunLockHeld` / CLI exits with code 3.** Another run is already holding
`state/<district-id>/.lock`. Do not retry in a tight loop — confirm the
other run (a manual invocation, or the previous scheduled run still
finishing) has actually completed first. See "Concurrency" above.

**`SafetyViolation: data_fingerprint ... is only N character(s) long` (or
"does not contain a '.'" / "does not contain any recognised sandbox
marker").** The configured `data_fingerprint` in `config/districts.yml`
failed the strength check at config load time — a value like `"@"` is no
longer accepted. Fix: use a substring that's actually unique to this
sandbox's data (e.g. its replica email domain) and contains one of
`replica`/`sandbox`/`sbx`/`dev`/`test`/`demo`/`staging`.

**`SafetyViolation` naming `baseline_counts.json`.** If this fires on a
district that has run successfully before, do not delete
`baseline_counts.json` to "fix" it — a missing/corrupt baseline file is only
ever a legitimate no-op on a district's genuine first run. If it was
deliberately removed as part of a disaster-recovery reset, also reset
`last_push.json` deliberately (see "Where state lives" above), don't just
let the engine re-derive one on its own.

**Cadence resolved the "wrong" weekday near a day boundary, or logs
`"Could not resolve timezone ..."`.** Check the district's `timezone` in
`config/districts.yml` (default `America/Chicago`) — cadence is resolved in
that timezone, never the host's local time or UTC. A resolution failure
(bad timezone name, or the `tzdata` package/IANA database missing on this
host) falls back to UTC with a loud `log.error`, not a silent swallow —
install `tzdata` or fix the configured name if you see this.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (or a read-only command: `schedule`, `plan`, `history`, `estimate-seed`, `simulate-week`). |
| 1 | An ordinary run failure (stack load, selection, guardrail block, push failure). |
| 2 | `SafetyViolation` — refused to run because a safety gate failed. |
| 3 | Another run for this district was already in progress (`.lock` held). |

See [README.md](../README.md#exit-codes) for the full description of each.

## Known limitations

Carried over honestly from the last audit — none of these block sandbox use
today, but all four matter before treating this as production-ready for a
partner:

1. **Teacher population only grows.** The Friday bucket adds one new teacher
   a week with nothing removing one (+26 over 26 simulated weeks).
   Extrapolated, this breaches `safety.MAX_SCALE_DRIFT` (25%) after roughly
   7 years, at which point every run would block on the scale-sanity gate.
   Follow-up work, not an active bug. Correction: this no longer needs a
   *new* `EventType` — `USERS_DELETED` (with `EventSubject.TEACHER`) already
   exists in the corrected enum — it just needs selection logic that picks a
   teacher to remove.
2. **KNOWN BLOCKER: contacts are very likely the wrong CSV shape.** See
   checklist item 5 and "First live push" above, and docs/SCHEMA.md's "KNOWN
   BLOCKER" section. Status: BLOCKED pending David verifying the CSV spec
   his sandbox actually accepts. Do not implement the rework speculatively.
3. **`eventing_verified` is still `false`** in `config/districts.yml` —
   Secure Sync / district-app token eventing has not been confirmed for
   this district. Must be verified before partner-facing use.
4. **`paramiko` behaviour is code-reviewed, not executed.** It isn't
   installable in this build environment (no PyPI access), so the 2
   host-key-policy tests skip. Watch the first live push closely for
   retry/timeout/host-key surprises.

Resolved (previously listed here): whether the absent-to-empty
`Middle name`/`Teacher 2 id` columns produce a first-sync event burst. They
should not — see docs/SCHEMA.md's "Resolved" note under the engine-added
deviations section.
