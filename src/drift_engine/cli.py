"""Command-line entry point.

    drift-engine schedule                 # print the fixed weekly cadence
    drift-engine plan --date 2026-08-04   # what would run on a given day
    drift-engine run                      # DRY RUN by default
    drift-engine run --live               # actually push to SFTP
    drift-engine seed --limit 4000        # stage guardian-contact seeding
    drift-engine estimate-seed            # how big would a full seed be
    drift-engine history --days 30        # is this working reliably?
    drift-engine simulate-week            # dry-run a whole Mon-Fri locally

Safety posture: ``run`` is a dry run unless ``--live`` is passed. There is no
config setting that flips that default, because a scheduled task that silently
became live would be exactly the failure the project brief's hard sandbox-only
constraint exists to prevent.

Exit code contract for ``run``/``seed`` (and any future command that calls
``runner.run_once``):

    0   Every requested district's run completed successfully. Also used by
        purely informational commands (``schedule``, ``plan``, ``history``,
        ``estimate-seed``, ``simulate-week``) that never touch a live target.
    1   At least one district's run completed but failed (``RunResult.error``
        is set -- e.g. the stack failed to load, selection raised, the
        guardrail blocked the run, or the SFTP push itself failed). This is
        an ordinary run failure, not a safety or locking problem.
    2   A ``SafetyViolation`` was raised -- the engine refused to run because
        the target failed one of the sandbox-only safety gates (host/
        username allowlist, data fingerprint, or scale sanity). This is
        never downgraded to 1 anywhere in this codebase (see ``safety.py``'s
        module docstring): an operator alerting on "wrong target" needs exit
        code 2 to mean exactly that, every time.
    3   Another run was already in progress for this district (the
        per-district lock at ``state/<district>/.lock`` was held) -- this
        invocation refused to start rather than race the one already
        running. See ``runner.RunLockHeld``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path

from . import audit, cadence, seed as seed_mod
from .config import DistrictConfig, EngineConfig, load_config, load_dotenv
from .content import build_content_generator
from .csvstack import CsvStack
from .models import SafetyViolation
from .runner import RunLockHeld, RunPaths, resolve_run_date, run_once

log = logging.getLogger("drift_engine")

#: Exit codes. See the module docstring's "Exit code contract" section.
EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_SAFETY_VIOLATION = 2
EXIT_LOCK_HELD = 3

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = REPO_ROOT / "state"
DEFAULT_LOGS = REPO_ROOT / "logs"
DEFAULT_CONFIG = REPO_ROOT / "config" / "districts.yml"


def _parse_date(value: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drift-engine",
        description="Generate Clever Events API activity in sandbox districts by "
        "drifting their CSV roster stack on a fixed weekday cadence.",
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
    p.add_argument("--logs-root", type=Path, default=DEFAULT_LOGS)
    p.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("schedule", help="print the fixed weekly cadence")

    sp = sub.add_parser("plan", help="show which buckets apply on a date")
    sp.add_argument("--date", type=_parse_date, default=None)
    sp.add_argument(
        "--district",
        default=None,
        help="district id, used to resolve 'today' in that district's own "
        "timezone when --date is omitted; default: the first configured "
        "district",
    )

    sr = sub.add_parser("run", help="execute a run (dry run unless --live)")
    sr.add_argument("--district", default=None, help="district id; default all enabled")
    sr.add_argument("--date", type=_parse_date, default=None)
    sr.add_argument(
        "--live",
        action="store_true",
        help="actually connect and upload. Without this flag nothing is pushed.",
    )
    sr.add_argument("--force", action="store_true", help="run even on a weekend")
    sr.add_argument("--seed", type=int, default=None, help="RNG seed, for reproducible runs")
    sr.add_argument(
        "--canned-content",
        action="store_true",
        help="skip the AI content step and use the built-in realistic value pool",
    )

    ss = sub.add_parser("seed", help="create guardian contact rows on students.csv (staged)")
    ss.add_argument("--district", default=None)
    ss.add_argument(
        "--limit",
        type=int,
        default=4000,
        help="max students to give guardians this run. Staging avoids one huge "
        "users.created (Contacts) burst; see docs/RUNBOOK.md.",
    )
    ss.add_argument("--live", action="store_true")
    ss.add_argument("--seed", type=int, default=None)
    ss.add_argument("--canned-content", action="store_true")

    se = sub.add_parser("estimate-seed", help="report how many contacts a full seed would create")
    se.add_argument("--district", default=None)

    sh = sub.add_parser("history", help="summarise recent runs")
    sh.add_argument("--district", default=None)
    sh.add_argument("--days", type=int, default=30)

    sw = sub.add_parser("simulate-week", help="dry-run Mon-Fri locally to preview a full week")
    sw.add_argument("--district", default=None)
    sw.add_argument("--start", type=_parse_date, default=None, help="a Monday; default next Monday")
    sw.add_argument("--seed", type=int, default=1234)
    sw.add_argument("--canned-content", action="store_true", default=True)

    return p


def _districts(cfg: EngineConfig, requested: str | None) -> list[DistrictConfig]:
    """Resolve ``--district`` (or "every enabled district") to a list.

    ``EngineConfig.get`` raises ``KeyError`` for an unknown id -- it never
    returns ``None`` (Fix 7: a prior version of this function checked for a
    ``None`` return here, which ``get`` can never produce, so an unknown
    ``--district`` produced a raw, unhandled ``KeyError`` traceback instead
    of a clean message). Caught here and turned into the same
    ``SystemExit``-with-a-helpful-message contract already used below for
    "no enabled districts".
    """
    if requested:
        try:
            d = cfg.get(requested)
        except KeyError:
            known = ", ".join(sorted(x.id for x in cfg.districts)) or "(none configured)"
            raise SystemExit(
                f"No district {requested!r} in config. Known districts: {known}."
            ) from None
        return [d]
    enabled = list(cfg.enabled_districts())
    if not enabled:
        raise SystemExit("No enabled districts in config.")
    return enabled


def _load_stack(cfg, district, state_root: Path) -> CsvStack:
    paths = RunPaths(state_root, district.id)
    return CsvStack.load(paths.ensure_current())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit.configure_logging(verbose=args.verbose)
    load_dotenv(args.env_file)

    if args.command == "schedule":
        print(cadence.describe_week())
        return EXIT_OK

    cfg = load_config(args.config)

    if args.command == "plan":
        # Fix 5: resolve "today" (when --date is omitted) in a real
        # district's own timezone, not the host machine's / UTC's --
        # otherwise this preview can show a different day than the one an
        # actual scheduled `run` would use near a day boundary. `plan` has
        # no required --district, so default to the one explicitly passed,
        # else the first configured district, else (no districts at all)
        # fall through to UTC inside resolve_run_date.
        district_for_tz = None
        if args.district:
            district_for_tz = _districts(cfg, args.district)[0]
        elif cfg.districts:
            district_for_tz = cfg.districts[0]
        resolved_date, tz_used = resolve_run_date(district_for_tz, args.date)

        plan = cadence.plan_for(resolved_date)
        print(f"Resolved date: {plan.run_date} (timezone: {tz_used})")
        if plan.skipped:
            print(f"{plan.run_date} ({plan.weekday_name}): no drift scheduled -- {plan.reason}")
        else:
            print(f"{plan.run_date} ({plan.weekday_name}): "
                  f"{', '.join(b.value for b in plan.buckets)}")
        return EXIT_OK

    if args.command == "estimate-seed":
        for d in _districts(cfg, args.district):
            stack = _load_stack(cfg, d, args.state_root)
            est = seed_mod.estimate_seed_volume(stack)
            print(f"\n=== {d.label} ({d.id}) ===")
            for k, v in est.items():
                print(f"  {k}: {v}")
        return EXIT_OK

    if args.command == "history":
        for d in _districts(cfg, args.district):
            print(audit.summarise_history(args.logs_root, d.id, days=args.days))
        return EXIT_OK

    if args.command == "simulate-week":
        return _simulate_week(cfg, args)

    # --- run / seed ------------------------------------------------------
    dry_run = not args.live
    if dry_run:
        log.info("DRY RUN: no SFTP connection will be opened. Pass --live to push.")
    else:
        log.warning("LIVE RUN: this will upload to the configured sandbox SFTP endpoint.")

    exit_code = EXIT_OK
    for d in _districts(cfg, args.district):
        try:
            result = run_once(
                cfg=cfg,
                district=d,
                run_date=getattr(args, "date", None),
                dry_run=dry_run,
                state_root=args.state_root,
                logs_root=args.logs_root,
                seed_value=args.seed,
                force=getattr(args, "force", False),
                canned_only=True if args.canned_content else None,
                seed_contacts_limit=args.limit if args.command == "seed" else None,
            )
        except RunLockHeld as exc:
            log.error(
                "LOCK HELD -- another run is already in progress for %s: %s", d.id, exc
            )
            return EXIT_LOCK_HELD
        except SafetyViolation as exc:
            log.error("SAFETY VIOLATION -- refusing to run: %s", exc)
            return EXIT_SAFETY_VIOLATION
        if not result.ok:
            log.error("Run %s for %s failed: %s", result.run_id, d.id, result.error)
            exit_code = EXIT_RUN_FAILED
        else:
            counts = result.event_counts()
            log.info(
                "Run %s for %s complete: %d change(s)%s",
                result.run_id,
                d.id,
                len(result.changes),
                f", expected events {counts}" if counts else "",
            )
    return exit_code


def _simulate_week(cfg, args) -> int:
    """Preview a full Mon-Fri locally, without touching persistent state.

    Useful for showing a partner what a week of activity looks like before
    pointing the engine at their sandbox for real.
    """
    import random

    for d in _districts(cfg, args.district):
        start = args.start
        if start is None:
            # Fix 5: resolve "today" in THIS district's own timezone, not the
            # host machine's / UTC's, before computing "next Monday" -- two
            # districts in different timezones could otherwise disagree on
            # what day it even is right now, near a day boundary.
            today, tz_used = resolve_run_date(d, None)
            start = today + _dt.timedelta(days=(7 - today.weekday()) % 7 or 7)
            log.info(
                "%s: resolved 'today' as %s using timezone %s; simulated week starts %s",
                d.id, today, tz_used, start,
            )
        if start.weekday() != 0:
            log.warning("--start %s is a %s, not a Monday", start, start.strftime("%A"))

        stack = _load_stack(cfg, d, args.state_root)
        rng = random.Random(args.seed)
        gen = build_content_generator(rng, canned_only=True)
        print(f"\n=== Simulated week for {d.label} ({d.id}) ===")
        print(f"Starting stack: {stack.counts()}\n")
        totals: dict[str, int] = {}
        for offset in range(5):
            day = start + _dt.timedelta(days=offset)
            plan = cadence.plan_for(day)
            from . import selection

            changes = selection.select_changes(stack, plan, gen, rng=rng)
            stack.apply(changes)
            per_day: dict[str, int] = {}
            for c in changes:
                per_day[c.expected_event.value] = per_day.get(c.expected_event.value, 0) + 1
                totals[c.expected_event.value] = totals.get(c.expected_event.value, 0) + 1
            print(f"{day} {day.strftime('%a')}  buckets={[b.value for b in plan.buckets]}")
            print(f"    {len(changes):>3} changes  {per_day or '(none)'}")
        print(f"\nWeek total expected events: {totals}")
        print(f"Ending stack: {stack.counts()}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
