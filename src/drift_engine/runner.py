"""Orchestration: one scheduled run, start to finish.

Implements the per-run flow from the project brief (§6):

    1. Day of week   -> which buckets apply today, in the DISTRICT'S OWN
                        local timezone (see ``resolve_run_date``) -- not the
                        host machine's, and not UTC.
    2. Load          -> the district's persistent working CSV stack
    3. Select        -> target records for each applicable bucket
    4. Generate      -> realistic values via the AI content step
    5. Apply         -> edits in memory
    6. Validate      -> the deletion guardrail, BEFORE anything is written
    7. Write + push  -> updated CSVs to the district's SFTP endpoint
    8. Log           -> an auditable record of what changed, EVEN when the run
                        fails or is refused by a safety gate

Invariants this module exists to protect:

**The working stack is the source of truth, not a fresh dataset each run.**
``state/<district>/current/`` is the accumulating stack the engine mutates.
``state/<district>/baseline/`` is the untouched original export, kept for
comparison and disaster recovery. The engine never writes to baseline.

**Nothing reaches SFTP that has not passed the guardrail.** Ordering matters:
select -> guardrail -> apply -> save -> push. The guardrail runs against the
pre-change stack (which is what Clever compares against) while the edits are
still only a list of intentions, so a blocked run leaves no trace on disk.

**Only one run per district at a time.** ``run_once`` acquires an exclusive,
non-blocking file lock on ``state/<district>/.lock`` for its entire duration
(see :func:`_district_lock`). Two overlapping runs against the same district
-- e.g. a scheduler retry landing while the previous invocation is still
running -- would otherwise both load the same pre-change stack, both mint
the same "next" engine-owned IDs (contacts, teachers), and each could
silently clobber the other's edits on save. If the lock is already held,
:class:`RunLockHeld` is raised immediately rather than queuing or racing.

**A SafetyViolation is always written to the audit log, then re-raised.** It
is never caught and downgraded to a merely-failed (``exit 1``) run anywhere
in this module -- see ``finish_and_raise`` below. A safety failure still
needs to be auditable (David needs to see it in the run's report just like
any other run), but it must also remain visibly distinct from an ordinary
run failure, all the way out to the CLI's exit code.

**The scale-sanity baseline cannot be silently bypassed by deleting a
file.** A missing or unreadable ``baseline_counts.json`` is only treated as
"this district's genuine first run ever" when there is ALSO no record of a
prior successful (non-dry) push (:meth:`RunPaths.has_prior_successful_push`).
Any other case -- the file is corrupt, or it is missing but a previous real
push is on record -- is a hard :class:`~drift_engine.models.SafetyViolation`,
not a warning-and-skip. See :meth:`RunPaths.read_baseline_counts`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import logging
import os
import random
import shutil
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit, cadence, guardrail, seed, selection, sftp_push
from .config import DistrictConfig, EngineConfig
from .content import build_content_generator
from .csvstack import CsvStack
from .models import Bucket, GuardrailViolation, RunPlan, RunResult, SafetyViolation
from .safety import TargetIdentity, assert_safe_target

log = logging.getLogger(__name__)

BASELINE_DIRNAME = "baseline"
CURRENT_DIRNAME = "current"

#: Name of the per-district lock file, under state/<district>/.
LOCK_FILENAME = ".lock"

#: Name of the marker file recording that a real (non-dry) push has
#: succeeded at least once for a district. See ``RunPaths.record_successful_push``.
LAST_PUSH_FILENAME = "last_push.json"

#: How many dry-run output directories to keep per district, most-recent-first.
#: A dry run writes a full copy of the stack (names, DOBs, emails, guardian
#: contact info -- the same PII the real stack carries) to
#: ``state/<district>/dry-run/<date>-<run_id>/`` and nothing ever cleaned
#: these up, so 100 dry runs against the real ~7MB stack would accumulate to
#: roughly 700MB of unbounded PII on disk. Keeping the most recent
#: ``DRY_RUN_RETENTION`` is enough to inspect "what would the last few runs
#: have done" without the directory growing forever.
DRY_RUN_RETENTION = 5

#: Fallback timezone used only when a district's own configured timezone
#: cannot be resolved (bad name, or the IANA tz database is unavailable in
#: this Python install). See ``resolve_run_date``.
_UTC_FALLBACK_LABEL = "UTC (fallback -- see error above)"


class BaselineCountsUnavailable(RuntimeError):
    """``baseline_counts.json`` exists but could not be read or parsed.

    Distinct from "the file does not exist at all" (see
    :meth:`RunPaths.read_baseline_counts`, which returns ``None`` for that
    case rather than raising) -- a corrupt file is never treated as "no
    baseline yet, write a fresh one", because that would silently re-anchor
    the scale-sanity check to whatever the stack currently looks like, which
    is exactly the failure mode this distinction exists to prevent.
    """


class RunLockHeld(RuntimeError):
    """Raised when another run already holds this district's lock file.

    See :func:`_district_lock`. The caller (``cli.py``) maps this to a
    distinct, non-zero exit code (3) so a scheduler or operator can tell
    "another run was already in progress" apart from an ordinary run
    failure or a safety violation.
    """


class RunPaths:
    """Filesystem layout for one district's persistent state."""

    def __init__(self, state_root: Path, district_id: str) -> None:
        self.root = Path(state_root) / district_id
        self.baseline = self.root / BASELINE_DIRNAME
        self.current = self.root / CURRENT_DIRNAME
        self.baseline_counts = self.root / "baseline_counts.json"
        self.lock_path = self.root / LOCK_FILENAME
        self.last_push_marker = self.root / LAST_PUSH_FILENAME

    def ensure_current(self) -> Path:
        """Seed ``current/`` from ``baseline/`` on first ever run.

        Done as a copy rather than working in place so the original SIS export
        stays pristine -- if drift ever corrupts the stack, baseline is the
        known-good state to reset to.
        """
        if self.current.exists() and any(self.current.glob("*.csv")):
            return self.current
        if not self.baseline.exists() or not any(self.baseline.glob("*.csv")):
            raise FileNotFoundError(
                f"No CSV stack found for this district. Expected the initial "
                f"export in {self.baseline}. Place the district's CSVs there "
                f"before the first run."
            )
        log.info("First run for this district: seeding %s from %s", self.current, self.baseline)
        self.current.mkdir(parents=True, exist_ok=True)
        for src in sorted(self.baseline.glob("*.csv")):
            shutil.copy2(src, self.current / src.name)
        return self.current

    def read_baseline_counts(self) -> dict[str, int] | None:
        """Reference record counts for the scale-sanity check.

        Returns ``None`` if ``baseline_counts.json`` does not exist at all --
        the caller decides whether that is acceptable (a genuine first run)
        or not (see :meth:`has_prior_successful_push`). Raises
        :class:`BaselineCountsUnavailable` if the file exists but cannot be
        read or parsed -- this is ALWAYS the caller's problem to treat as a
        hard failure, never silently swallowed into "treat this as unset".
        """
        if not self.baseline_counts.exists():
            return None
        try:
            return json.loads(self.baseline_counts.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise BaselineCountsUnavailable(
                f"{self.baseline_counts} exists but could not be read/parsed: {exc}"
            ) from exc

    def write_baseline_counts(self, stack: CsvStack) -> None:
        """Record reference counts once, from a pristine stack. Atomic."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(stack.counts(), fh, indent=2, sort_keys=True)
            os.replace(tmp, self.baseline_counts)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("Recorded reference record counts at %s", self.baseline_counts)

    def has_prior_successful_push(self) -> bool:
        """Whether a real (non-dry) push has ever succeeded for this district.

        Used to distinguish "baseline_counts.json is missing because this is
        this district's genuine first run ever" from "baseline_counts.json is
        missing because something deleted/corrupted it after a real push
        already happened" -- only the former is safe to silently re-anchor.
        """
        return self.last_push_marker.exists()

    def record_successful_push(self, *, run_id: str, run_date: _dt.date) -> None:
        """Persist the marker :meth:`has_prior_successful_push` checks for.

        Called only after a real (non-dry) ``sftp_push.push`` has actually
        succeeded -- never for a dry run, which pushes nothing. Overwrites
        the marker each time (only its existence is checked), atomically.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "run_date": run_date.isoformat(),
            "recorded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.last_push_marker)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Per-district locking (Fix 4: overlapping runs must never both proceed)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _district_lock(paths: RunPaths, *, district_id: str):
    """Hold an exclusive, non-blocking ``flock`` on ``state/<district>/.lock``.

    Acquired for the whole of a run (see ``run_once``), released in a
    ``finally`` no matter how the run ends. If another process already holds
    the lock, ``fcntl.flock(..., LOCK_NB)`` fails immediately with
    ``BlockingIOError`` -- this never blocks waiting for the other run to
    finish, and never proceeds anyway; it raises :class:`RunLockHeld`.

    Creates ``state/<district>/`` if it does not exist yet (a district's
    very first run, before ``current/`` has even been seeded from
    ``baseline/``, still needs somewhere to put the lock file).
    """

    paths.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(paths.lock_path), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunLockHeld(
                f"Another run is already in progress for district {district_id!r} "
                f"(lock file {paths.lock_path} is held). Refusing to start a second "
                "concurrent run -- overlapping runs can silently lose each other's "
                "edits and mint colliding record IDs (e.g. two runs both minting "
                "CON000001 for different students)."
            ) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Timezone-aware cadence date resolution (Fix 5)
# ---------------------------------------------------------------------------


def resolve_run_date(
    district: DistrictConfig | None,
    explicit: _dt.date | None = None,
) -> tuple[_dt.date, str]:
    """Resolve "today" for cadence purposes, in ``district``'s own local time.

    Returns ``(run_date, timezone_label)`` -- the caller is expected to log
    both, plus the resulting weekday, on every run (see ``run_once``).

    If ``explicit`` is given (e.g. ``--date`` on the CLI, or a test), it is
    returned as-is and no timezone resolution happens at all.

    Otherwise, resolves "now" in ``district.timezone`` via
    :mod:`zoneinfo` and takes its date. This matters at day boundaries: the
    project brief's fixed weekly cadence (§4) is a promise about the
    district's OWN calendar, not the host machine's or UTC's -- a scheduled
    task running on a UTC host at, say, 03:30 UTC on a Saturday is still
    22:30 Friday in America/Chicago, and must still run Friday's big-teacher
    bucket, not silently skip it as "Saturday is a weekend".

    Falls back to UTC (with a loud ``log.error``, never a silent swallow) if
    the district's timezone name is invalid or the IANA tz database is not
    available in this Python installation (``zoneinfo.ZoneInfoNotFoundError``)
    -- a scheduled task should never crash outright over a timezone data
    problem, but silently getting the wrong day is worse than being loud
    about degrading to UTC.
    """

    if explicit is not None:
        return explicit, "explicit --date (no timezone resolution)"

    tz_name = district.timezone if district is not None else "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        log.error(
            "Could not resolve timezone %r%s (%s). Falling back to UTC for cadence "
            "date resolution -- this can cause the WRONG cadence bucket to run near "
            "a day boundary. Fix the district's configured timezone, or ensure the "
            "IANA tz database ('tzdata' package) is installed.",
            tz_name,
            f" for district {district.id!r}" if district is not None else "",
            exc,
        )
        return _dt.datetime.now(_dt.timezone.utc).date(), _UTC_FALLBACK_LABEL

    return _dt.datetime.now(tz).date(), tz_name


# ---------------------------------------------------------------------------
# Dry-run output retention (Fix 6)
# ---------------------------------------------------------------------------


def _prune_dry_run_dirs(
    dry_run_root: Path,
    *,
    keep: int = DRY_RUN_RETENTION,
    protect: str | None = None,
) -> list[str]:
    """Delete all but the ``keep`` most recent dry-run output directories.

    Directory names are ``<iso-date>-<run_id>``, and ``run_id`` itself starts
    with a UTC timestamp (see ``audit.new_run_id``), so lexicographic name
    order is chronological order -- no need to stat/parse each one.

    Called only after a successful dry-run write (see ``run_once``), so a
    failed write never triggers pruning of otherwise-good prior output.
    Returns the names pruned, purely so the caller can log them.
    """

    if keep < 0 or not dry_run_root.exists():
        return []

    dirs = sorted((d for d in dry_run_root.iterdir() if d.is_dir()), key=lambda d: d.name)

    # Never prune the run that is currently in progress. Directory names lead
    # with the DISTRICT-LOCAL run date, which a caller can backdate via
    # --date, so name order is not reliably "current run last" -- a backdated
    # run sorts below existing output and would otherwise delete its own files
    # out from under the push step that reads them next.
    #
    # ``keep`` counts TOTAL directories retained, the in-progress one included,
    # so protecting it reduces the number of older directories kept by one
    # rather than quietly raising the ceiling to keep+1.
    budget = keep
    if protect is not None:
        held_back = [d for d in dirs if d.name == protect]
        dirs = [d for d in dirs if d.name != protect]
        budget = max(keep - len(held_back), 0)

    to_prune = dirs[:-budget] if budget > 0 else dirs
    pruned: list[str] = []
    for d in to_prune:
        shutil.rmtree(d, ignore_errors=True)
        pruned.append(d.name)
    return pruned


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_once(
    *,
    cfg: EngineConfig,
    district: DistrictConfig,
    run_date: _dt.date | None = None,
    dry_run: bool = True,
    state_root: Path,
    logs_root: Path,
    seed_value: int | None = None,
    force: bool = False,
    canned_only: bool | None = None,
    seed_contacts_limit: int | None = None,
) -> RunResult:
    """Execute a single run for one district.

    Public entry point: acquires this district's exclusive run lock (Fix 4)
    before anything else, and releases it in a ``finally`` regardless of how
    the run ends. Raises :class:`RunLockHeld` immediately (never blocks,
    never proceeds anyway) if another run already holds it. All the actual
    run logic lives in :func:`_run_once_locked`, which assumes the lock is
    already held.
    """

    paths = RunPaths(state_root, district.id)
    with _district_lock(paths, district_id=district.id):
        return _run_once_locked(
            cfg=cfg,
            district=district,
            run_date=run_date,
            dry_run=dry_run,
            logs_root=logs_root,
            seed_value=seed_value,
            force=force,
            canned_only=canned_only,
            seed_contacts_limit=seed_contacts_limit,
            paths=paths,
        )


def _run_once_locked(
    *,
    cfg: EngineConfig,
    district: DistrictConfig,
    run_date: _dt.date | None,
    dry_run: bool,
    logs_root: Path,
    seed_value: int | None,
    force: bool,
    canned_only: bool | None,
    seed_contacts_limit: int | None,
    paths: RunPaths,
) -> RunResult:
    """The actual per-run flow. Assumes the caller already holds the district lock.

    ``dry_run`` defaults to True deliberately at the ``run_once`` boundary --
    every caller that wants a real push must say so explicitly; no code path
    can accidentally omit the flag and get a live upload.

    ``force`` runs the weekday's bucket logic even on a weekend, for manual
    testing. It does not bypass the guardrail or the safety gates -- nothing
    does.

    ``seed_contacts_limit`` switches the run into contacts-seeding mode instead
    of drift, creating guardian records for up to N students that have none.
    """
    run_date, tz_used = resolve_run_date(district, run_date)
    started = _dt.datetime.now(_dt.timezone.utc)
    run_id = audit.new_run_id()

    # Prove the audit destination is writable BEFORE doing any work -- above all
    # before a live push. An unwritable logs/ directory would otherwise produce
    # the worst possible outcome: roster data uploaded to the district with no
    # record anywhere of what was changed.
    audit.preflight(logs_root)

    content_gen = None  # bound below; referenced by finish() for AI-vs-canned stats

    plan = cadence.plan_for(run_date)
    log.info(
        "Resolved run date %s (%s), using timezone %s for district %s.",
        run_date, plan.weekday_name, tz_used, district.id,
    )
    if plan.skipped and force:
        log.warning("Weekend run forced; treating %s as a small-daily weekday", run_date)
        plan = RunPlan(run_date=run_date, buckets=(Bucket.SMALL_DAILY,))

    result = RunResult(
        run_id=run_id,
        plan=plan,
        district=district.id,
        dry_run=dry_run,
        started_at=started,
    )

    def finish(err: str | None = None) -> RunResult:
        result.error = err
        result.finished_at = _dt.datetime.now(_dt.timezone.utc)
        stats = getattr(content_gen, "stats", None) if content_gen is not None else None
        try:
            written = audit.write_run(
                result,
                logs_root=logs_root,
                district_label=district.label,
                content_stats=stats() if callable(stats) else None,
            )
            log.info("Run report: %s", written.get("markdown"))
        except Exception:  # pragma: no cover - logging must never mask the run
            log.exception("Failed to write audit artefacts for run %s", run_id)
        return result

    def finish_and_raise(violation: SafetyViolation, *, prefix: str = "safety") -> None:
        """Write the audit record for a safety failure, THEN re-raise it.

        A ``SafetyViolation`` is never caught and downgraded to an ordinary
        failed run (``exit 1``) anywhere in this module (see the module
        docstring) -- but a safety failure still needs to be auditable, so
        this always writes the run report first (via ``finish``), and only
        then lets the exception propagate all the way out of ``run_once`` to
        the CLI, which maps it to its own distinct exit code (2).
        """
        log.error("%s", violation)
        finish(f"{prefix}: {violation}")
        raise violation

    if plan.skipped:
        log.info("%s is a %s -- no drift scheduled. Nothing to do.", run_date, plan.weekday_name)
        return finish()

    if not district.eventing_verified:
        log.warning(
            "District %s has eventing_verified=false. Secure Sync / district-app "
            "token eventing has not been confirmed, so Clever may ingest these "
            "CSVs without emitting Events API records. Proceeding anyway.",
            district.id,
        )

    rng = random.Random(seed_value) if seed_value is not None else random.Random()
    # Email domains and phone area codes come from the district's own config, so
    # a second sandbox does not get this one's Tulsa-shaped values written into
    # its records (project brief §6: adding a district is config, not code).
    content_gen = build_content_generator(
        rng,
        canned_only=canned_only,
        staff_email_domain=district.staff_email_domain,
        student_email_domain=district.student_email_domain,
        area_codes=district.area_codes,
    )

    try:
        current_dir = paths.ensure_current()
        stack = CsvStack.load(current_dir)
    except Exception as exc:
        log.exception("Could not load the CSV stack")
        return finish(f"stack load failed: {exc}")

    log.info(
        "Loaded stack for %s: %s",
        district.id,
        ", ".join(f"{k}={v}" for k, v in sorted(stack.counts().items())),
    )
    if stack.migrated_columns:
        log.info(
            "Added engine-owned columns on load: %s. This should NOT produce a field-change "
            "event burst on the next sync -- Clever's users.updated fires on a genuine object "
            "change, and an absent-to-empty column is not one. See docs/SCHEMA.md.",
            stack.migrated_columns,
        )

    # --- Safety: host/username/fingerprint/scale, ALL before selection -------
    # The reference counts must be captured from the PRISTINE stack, before
    # this run's edits are applied. Recording them after a push would bake
    # one day of drift into the reference point, so the thing we compare
    # against would itself drift a little on first use.
    try:
        baseline_counts = paths.read_baseline_counts()
    except BaselineCountsUnavailable as exc:
        finish_and_raise(
            SafetyViolation(
                f"District {district.id!r}: {exc}. A missing-or-unreadable scale-sanity "
                "baseline is only acceptable on this district's genuine first-ever run; "
                "baseline_counts.json exists here (even if corrupted), so this is not "
                "that. Refusing to run rather than silently skip the scale check or "
                "silently re-anchor the baseline to whatever the stack currently looks "
                "like."
            )
        )

    if baseline_counts is None:
        if paths.has_prior_successful_push():
            finish_and_raise(
                SafetyViolation(
                    f"District {district.id!r}: baseline_counts.json is missing, but a "
                    f"prior successful push is on record ({paths.last_push_marker}). A "
                    "missing baseline file is only acceptable on a genuine first-ever "
                    "run -- refusing to silently re-anchor the scale-sanity baseline to "
                    "the current stack. If the file was deliberately removed as part of "
                    "a disaster-recovery reset, restore it from a backup or reset "
                    f"{paths.last_push_marker} deliberately as well."
                )
            )
        # Genuine first run ever for this district: nothing to compare
        # against yet. Record the reference point now, from the pristine
        # (pre-edit) stack -- keep this ordering (before selection/apply).
        paths.write_baseline_counts(stack)

    target = TargetIdentity(
        district_id=district.id,
        host=district.sftp.host,
        port=district.sftp.port,
        username=district.sftp.username,
        remote_dir=district.sftp.remote_dir,
    )
    try:
        assert_safe_target(
            target,
            allowlist=cfg.allowlist(),
            fingerprint=district.data_fingerprint,
            sample_values=stack.fingerprint_sample(),
            current_counts=stack.counts() if baseline_counts is not None else None,
            baseline_counts=baseline_counts,
        )
    except SafetyViolation as exc:
        finish_and_raise(exc)

    # --- Select ------------------------------------------------------------
    try:
        if seed_contacts_limit is not None:
            estimate = seed.estimate_seed_volume(stack)
            log.info("Seeding mode. Volume estimate: %s", estimate)
            changes = seed.seed_contacts(
                stack, content_gen, rng=rng, limit=seed_contacts_limit
            )
        else:
            changes = selection.select_changes(stack, plan, content_gen, rng=rng)
    except Exception as exc:
        log.exception("Change selection failed")
        return finish(f"selection failed: {exc}")

    result.changes = list(changes)
    if not changes:
        log.warning("Selection produced no changes. Nothing to push.")
        return finish()

    log.info(
        "Selected %d change(s). Expected events: %s",
        len(changes),
        result.event_counts(),
    )

    # --- Guardrail, before anything is written ------------------------------
    try:
        # The counts Clever last actually received. Passing them lets the
        # guardrail catch attrition it cannot otherwise see: a truncated or
        # partially-exported stack loses rows BEFORE selection runs, so those
        # losses never appear as Operation.DELETE changes. Without this the
        # guardrail would wave through a stack that had quietly shed thousands
        # of students, which Clever reads as mass deletion.
        last_pushed_counts = sftp_push.read_last_pushed_counts(current_dir)
        report = guardrail.enforce(
            stack, changes, last_pushed_counts=last_pushed_counts
        )
        result.guardrail = report.to_dict() if hasattr(report, "to_dict") else dict(report.__dict__)
        log.info("Guardrail passed.\n%s", report.summary() if hasattr(report, "summary") else report)
    except GuardrailViolation as exc:
        log.error("GUARDRAIL BLOCKED THIS RUN: %s", exc)
        return finish(f"guardrail blocked: {exc}")
    except Exception as exc:
        log.exception("Guardrail evaluation failed")
        return finish(f"guardrail error: {exc}")

    # --- Apply and persist --------------------------------------------------
    try:
        stack.apply(changes)
    except Exception as exc:
        log.exception("Applying changes failed")
        return finish(f"apply failed: {exc}")

    try:
        if dry_run:
            dry_run_root = paths.root / "dry-run"
            out_dir = dry_run_root / f"{run_date.isoformat()}-{run_id}"
            out_dir.mkdir(parents=True, exist_ok=True)
            written = stack.save(out_dir)
            log.info("DRY RUN: wrote %d file(s) to %s (nothing uploaded)", len(written), out_dir)
            pruned = _prune_dry_run_dirs(dry_run_root, protect=out_dir.name)
            if pruned:
                log.info(
                    "Pruned %d old dry-run director(y/ies), keeping the %d most recent: %s",
                    len(pruned), DRY_RUN_RETENTION, pruned,
                )
        else:
            written = stack.save(current_dir)
            log.info("Persisted %d file(s) to %s", len(written), current_dir)
    except Exception as exc:
        log.exception("Writing the CSV stack failed")
        return finish(f"save failed: {exc}")

    # --- Push --------------------------------------------------------------
    try:
        push_dir = out_dir if dry_run else current_dir
        result.pushed_files = sftp_push.push(
            push_dir,
            district,
            dry_run=dry_run,
            stack=stack,
            allowlist=cfg.allowlist(),
        )
    except SafetyViolation as exc:
        # Never downgraded (the brief's hard constraint) -- but still made
        # auditable first, same as the scale-sanity/fingerprint checks above.
        finish_and_raise(exc)
    except Exception as exc:
        log.exception("SFTP push failed")
        return finish(f"push failed: {exc}")

    if not dry_run:
        paths.record_successful_push(run_id=run_id, run_date=run_date)

    return finish()
