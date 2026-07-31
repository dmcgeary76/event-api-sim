# Project Brief: Sandbox Events API Data Drift Engine

**Owner:** David McGeary (Sales Engineering, Clever)
**Intended venue:** Claude Cowork project, with GitHub sync + GitHub Pages
**GitHub repo:** `<INSERT REPO URL HERE — repo to be created by David before build begins>`

---

## 1. Purpose

Clever sandbox districts are set up for application partners to test workflows, including workflows built on Clever's **Events API**. Right now, sandbox data is static once loaded, so there is no ongoing stream of `created` / `updated` / `deleted` events for partners to observe and build against.

This project builds a **scheduled, semi-autonomous data drift engine** that makes small, realistic, recurring changes to a sandbox district's roster data and re-syncs it via the district's existing CSV-over-SFTP pipeline. This causes Clever to generate a steady, predictable stream of Events API activity that partners can use to test and validate their event-driven workflows.

**This system operates exclusively against sandbox / developer environments.** It must never be pointed at a production district or a production SFTP endpoint. This should be treated as a hard constraint throughout the build, not just a configuration default.

---

## 2. Background: How the district's data pipeline works today

- Each sandbox district's roster data is maintained as a **CSV stack** (the standard Clever SIS CSV format: districts, schools, users/students, teachers, sections, enrollments, contacts, etc.).
- These CSVs are synced to Clever via an **SFTP endpoint** on a recurring basis.
- Clever ingests the CSVs and, for districts on Secure Sync with a district-app token, emits **Events API** records reflecting what changed since the last sync.

This means the engine does not need to touch Clever's API directly to *create* the drift — it only needs to:
1. Maintain a working copy of the current CSV stack for a given sandbox district.
2. Apply a bounded set of edits to that CSV stack.
3. Write the updated CSVs back to the district's SFTP endpoint.

Clever's own sync process will detect the diffs and generate the corresponding events.

---

## 3. Events API research findings (confirmed via Clever dev docs)

This directly shapes what kinds of CSV edits are worth making:

- **Enrollment changes do *not* generate `users.updated` events.** Student/teacher enrollment membership lives on the section object, so enrollment changes surface as **`sections.updated`** events. Any "move a student from Course A to Course B" logic should be understood and documented as a section-membership change, not a user change.
- **Contacts (guardians) have their own full event lifecycle**: `contacts.created`, `contacts.updated`, and `contacts.deleted` are all distinct, clean event types. This makes contacts an ideal category for both small (field edit) and large (add/remove) changes.
- **Deletion safety threshold:** Clever pauses a sync for review if **more than 10% of any single record type is deleted** in one sync. Any bucket that removes/unenrolls records (contacts or students) must stay well under this ratio per run. This should be enforced in code as a hard guardrail, not just a design intention.
- General user field changes (e.g., adding a middle name) generate standard `users.updated` events.

**Net takeaway — the finite set of change categories to build against:**
1. `contacts.created` (guardian added)
2. `contacts.updated` (email address change, minor field edit)
3. `contacts.deleted` (guardian removed) — subject to the 10% threshold
4. `sections.updated` via enrollment change (student or teacher added/removed from a section's roster)
5. `users.updated` via minor field change (e.g., middle name added/edited)

This list is intentionally universal — it should not vary per application partner or per district. The same fixed pattern applies to every sandbox district connected to this workflow.

---

## 4. The fixed weekly cadence

This is the core design decision and should be treated as **rigid and predictable**, because partners will use it as a known testing cadence. No per-district "volume knob" — the pattern is the same magnitude and cadence for every district, regardless of size.

| Day | Change bucket | What happens |
|---|---|---|
| Mon–Fri (every weekday) | **Small daily changes** | A handful of minor, granular edits — contact field updates (e.g., email address tweak), minor user field edits (e.g., middle name added). Primarily affects **student** records. |
| **Tuesday & Thursday** | **Big student changes** | A larger batch layered on top of the daily small changes: e.g., enrollment shifts for ~4 students (moved between sections), plus contact record changes (add/edit/remove) for a handful of additional students. |
| **Friday** | **Big teacher changes** | Least frequent bucket. Teacher-focused structural changes — e.g., swapping a co-teacher on a section, adding a new teacher to the district, or reassigning a teacher-owned section. |

Notes:
- The **big buckets stack on top of** the small daily bucket for that day — they don't replace it.
- Cadence is **calendar-fixed** (specific days of week), not randomized — this predictability is a requirement, not a nice-to-have, because partners will be told to expect activity on this schedule.
- The *selection* of which records get touched (which student, which contact field) can and should be randomized within the fixed structure — the schedule is rigid, the specific targets are not.

---

## 5. Where AI fits in (and where it deliberately doesn't)

Design intent: **keep the schedule and selection logic deterministic; use AI only for realistic content generation.**

- **Deterministic (no AI needed):**
  - Day-of-week bucket logic (what category of change happens today)
  - Random weighted selection of *which* records are touched
  - Enforcement of the 10% deletion guardrail
  - CSV diffing, rewriting, and SFTP push

- **AI-assisted (LLM call per generated value):**
  - Generating realistic new values — plausible names, believable email address formats, sensible middle names, realistic new-contact details, etc.
  - The goal is believability over pure randomness (e.g., not `test123@test.com`, but something a real guardian record would plausibly contain).

This is an intentional starting posture, not a permanent architecture decision: David has one application partner for this initial rollout and wants to run this AI-involved version to see how consistent and useful the AI-generated content is over time. If it proves stable, a future iteration may replace the AI content-generation step with a fixed/canned pool of realistic values and drop the AI dependency entirely. **The build should keep this content-generation step cleanly isolated (e.g., a single well-defined function/module) so it can be swapped out later without touching the scheduling or selection logic.**

---

## 6. Proposed architecture

- **Execution model:** A scheduled Cowork task (not a standalone always-on app, not a conversational skill). Runs on weekdays.
- **State:** The engine needs a persistent "current CSV stack" per sandbox district to diff against and mutate — this is the source of truth it edits each run, not a fresh randomized dataset each time.
- **Config surface:** Minimal and generic by design — per the discussion, there is no per-district volume knob and no per-partner change-profile system. Config should really only need: which sandbox district(s)/SFTP endpoint(s) this applies to, and credentials for reaching them. The change logic itself is fixed and shared across all districts.
- **Flow per scheduled run:**
  1. Determine day of week → determine which bucket(s) apply today (small daily always; big student on Tue/Thu; big teacher on Fri).
  2. Load current CSV stack for the district.
  3. Deterministically select target records for each applicable bucket.
  4. For fields requiring realistic new content, call the AI content-generation step.
  5. Apply edits to the CSV stack in memory.
  6. Validate against the 10% deletion guardrail before writing anything.
  7. Write updated CSVs and push to the district's SFTP endpoint.
  8. Log what changed (for David's own visibility/debugging, and to make the run auditable).

- **Scale consideration:** Although there's only one application partner right now, the architecture should assume more SFTP endpoints/districts will be added later. Structure it so adding a new district is a config addition, not a code change.

---

## 7. Build approach recommendation

- **Phase planning / orchestration:** Recommend Opus, given the need to coordinate scheduling logic, state management, the AI content-generation step, and SFTP integration coherently, and to spin up sub-agents for implementation work.
- **Implementation of individual components:** The mechanical pieces (CSV parsing/diffing, day-of-week bucket logic, weighted random selection, SFTP write, guardrail enforcement) are straightforward enough to hand to Sonnet-level sub-agents. This is a recommendation for how to split work, not a hard requirement — Opus/David should feel free to adjust once the phase plan is scoped.

---

## 8. GitHub requirements

- This should live as its **own standalone repo** (not folded into an existing monorepo), so it can eventually be shared via an internal directory for other SEs running similar sandbox evaluations.
- Needs a **GitHub Pages** project page (summary + changelog), consistent with David's existing github-workflow conventions.
- David will create the repo ahead of time and provide the URL to the build agent; the build agent should treat repo creation as already handled and focus on committing/pushing into the provided repo.

---

## 9. Open items for the build agent to confirm (not yet finalized)

- Exact CSV schema/field names in use for this partner's sandbox stack (should confirm against an actual sample export rather than assuming a generic Clever CSV layout).
- SFTP credential storage/handling approach (should follow whatever secrets pattern is appropriate for a Cowork-scheduled task — this wasn't specified and needs a decision during build).
- Logging/audit format — how David will review "what changed on a given day" without manually diffing CSVs.
- Confirmation of how "district-app token" / Secure Sync eventing is verified as active for the target sandbox district before this goes live.

---

## 10. Explicit non-goals (for this phase)

- No per-partner or per-district configuration profiles — the pattern is universal.
- No volume/intensity knob — magnitude is fixed regardless of district size.
- No randomized scheduling — bucket days are fixed and predictable.
- No production environment access, ever.

---

## 11. Success criteria

- Running Monday through Friday without manual intervention.
- Reliable small daily changes producing `contacts.updated` / `users.updated` events.
- Reliable Tuesday/Thursday big-student runs producing `sections.updated` and contact create/delete events.
- Reliable Friday big-teacher runs producing analogous teacher-side events.
- Deletion-type changes never trip Clever's 10% threshold.
- Repo live on GitHub with Pages summary/changelog, ready to share via internal directory.
