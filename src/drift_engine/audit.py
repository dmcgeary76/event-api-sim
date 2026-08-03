"""Audit/logging layer -- the answer to project brief §9's open question:

    "Logging/audit format -- how David will review 'what changed on a given
    day' without manually diffing CSVs."

Three artefacts are written per run, under a ``logs/`` root namespaced by
district and date:

1. **JSON** (``<district>/<date>-<run_id>.json``) -- the full, lossless
   ``RunResult`` serialisation. This is the machine-readable source of truth:
   a future dashboard, partner-facing script, or ``load_run`` round-trip
   reads this, never the Markdown.
2. **Markdown** (``<district>/<date>-<run_id>.md``) -- the artefact David
   actually opens. Designed around the one question he's really asking each
   morning -- "what should the partner have seen, and did the run behave?" --
   so the expected-events table comes first, before any per-record detail,
   and tables are used throughout instead of prose (skimmable > exhaustive).
   A dry run is marked unmistakably (in the title itself, not just a small
   note) because mistaking a dry run for a real push is the single worst
   misread this report could cause.
3. **``history.jsonl``** (append-only, one compact line per run) -- cheap
   trend analysis (weekday reliability, cumulative event counts, AI-vs-canned
   drift) across weeks without re-parsing every full JSON report.

Design decisions worth calling out:

* **Redaction is scoped, not blanket.** The hard requirement is that no
  credential-shaped value ever gets serialised. The *only* places this module
  receives caller-controlled, semantically-opaque dict data are
  ``RunResult.guardrail`` and the optional ``content_stats`` argument --
  everything else is built field-by-field from ``Change``/``RunResult``,
  whose shapes this module already knows are safe (e.g. ``Change.key`` is a
  natural-key mapping like ``{"Student id": "STU100000"}`` -- note that the
  *field name* ``key`` itself would match a naive `/key/i` scan, which is
  exactly why the redaction pass is applied to ``before``/``after``/
  ``guardrail``/``content_stats`` sub-trees, and never to the enclosing
  ``Change`` dict as a whole).
* **A failed run still produces a full, readable report.** ``RunResult.error``
  being set does not short-circuit report generation -- the brief is explicit
  that a silent failure is the worst outcome for a scheduled task. The
  Markdown report gets an unmissable failure banner plus a "What failed"
  section with the raw error text, and every other section renders whatever
  partial data (changes, guardrail) had already been computed.
* **Atomic writes.** JSON and Markdown are written to a temp file in the same
  directory and moved into place with ``os.replace`` (atomic on POSIX and
  Windows), so a crash mid-write never leaves a half-written report next to a
  stale one under the same name. ``history.jsonl`` is opened in append mode
  and written with a single ``write()`` call per line, so it can only ever
  grow -- there is no code path that reads-modifies-rewrites the whole file,
  so earlier lines can't be corrupted by a later run.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .guardrail import CLEVER_PAUSE_THRESHOLD, SAFE_THRESHOLD
from .models import Change, Operation, RunResult

__all__ = [
    "write_run",
    "load_run",
    "read_history",
    "summarise_history",
    "new_run_id",
    "configure_logging",
    "preflight",
    "AuditPreflightError",
]

logger = logging.getLogger(__name__)

#: Bumped whenever the JSON record's shape changes in a way a reader would
#: need to know about. Round-trip consumers (``load_run`` callers, a future
#: dashboard) should check this before assuming field presence.
#:
#: v2 (2026-08-03): corrected a factual error inherited from the project
#: brief -- Clever's Events API has no contacts.*/teachers.* event types (see
#: models.EventType's docstring). Two consequences for THIS schema:
#:   * ``changes[].expected_event`` now only ever contains a bare
#:     ``users.*``/``sections.*`` wire value, never e.g. ``contacts.created``.
#:   * ``changes[].event_subject`` and ``changes[].expected_event_label`` are
#:     new fields -- the role/object (Students/Teachers/Contacts/Staff/
#:     Sections) an event is really about, and the two combined into a
#:     human-readable label, respectively.
#:   * ``event_counts`` is now keyed by that human-readable LABEL (e.g.
#:     ``"users.updated (Contacts)"``), not the bare wire event name, so
#:     David's per-role breakdown survives the collapse into ``users.*``. A
#:     new ``wire_event_counts`` field carries the bare wire-name totals --
#:     what the partner's real ``/events`` feed will actually show -- since
#:     that information is no longer recoverable from ``event_counts`` alone.
SCHEMA_VERSION = 2

_HISTORY_FILENAME = "history.jsonl"

#: Case-insensitive: any dict key containing one of these substrings is
#: treated as credential-shaped and its value is replaced wholesale. See the
#: module docstring for exactly which sub-trees this is applied to.
_SECRET_KEY_RE = re.compile(r"password|secret|token|key", re.IGNORECASE)
_REDACTED = "***REDACTED***"

_WEEKDAY_ORDER: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact(value: Any) -> Any:
    """Recursively replace credential-shaped dict values with a placeholder.

    Deliberately scoped -- see the module docstring's "Redaction is scoped,
    not blanket" note. Callers apply this to specific sub-trees
    (``Change.before``/``after``, ``RunResult.guardrail``, ``content_stats``),
    never to a dict that has a legitimate field literally named ``key``.
    """

    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via tmp-file + ``os.replace``.

    The temp file lives in the same directory as the destination so
    ``os.replace`` is a same-filesystem rename (atomic), and it is cleaned up
    on any failure -- no partially-written ``.tmp`` file is ever left behind.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise


def _append_history_line(path: Path, record: Mapping[str, Any]) -> None:
    """Append one compact JSON line to ``path``.

    Opened in append mode and written with a single ``write()`` call for the
    whole line (payload + newline) -- there is no seek, no read-modify-write
    of existing content, so a run writing its own history line can never
    corrupt a previous run's line, regardless of how this run ends.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# JSON record construction
# ---------------------------------------------------------------------------


def _iso(value: _dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration_seconds(result: RunResult) -> float | None:
    if result.started_at is None or result.finished_at is None:
        return None
    return (result.finished_at - result.started_at).total_seconds()


def _change_to_dict(change: Change) -> dict[str, Any]:
    return {
        "filename": change.filename,
        "operation": change.operation.value,
        "key": dict(change.key),
        "bucket": change.bucket.value,
        # Bare wire event name Clever actually emits -- never contacts.*/
        # teachers.*, see models.EventType.
        "expected_event": change.expected_event.value,
        # Role/object this event is really about (Students/Teachers/
        # Contacts/Staff/Sections) -- see models.EventSubject.
        "event_subject": change.event_subject.value,
        # The two combined, for display -- e.g. "users.updated (Contacts)".
        "expected_event_label": change.expected_event_label,
        "before": _redact(dict(change.before)),
        "after": _redact(dict(change.after)),
        "note": change.note,
        "ai_generated": change.ai_generated,
    }


def _build_record(
    result: RunResult,
    *,
    district_label: str,
    content_stats: Mapping[str, Any] | None,
) -> dict[str, Any]:
    plan = result.plan
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": __version__,
        "run_id": result.run_id,
        "district": result.district,
        "district_label": district_label or result.district,
        "run_date": plan.run_date.isoformat(),
        "weekday": plan.weekday_name,
        "buckets": [b.value for b in plan.buckets],
        "skipped": plan.skipped,
        "skip_reason": plan.reason,
        "dry_run": result.dry_run,
        "started_at": _iso(result.started_at),
        "finished_at": _iso(result.finished_at),
        "duration_seconds": _duration_seconds(result),
        "ok": result.ok,
        "error": result.error,
        # Keyed by human-readable LABEL (e.g. "users.updated (Contacts)") --
        # David's per-role breakdown. See SCHEMA_VERSION v2 note above.
        "event_counts": result.event_counts(),
        # Keyed by the BARE wire event name (e.g. "users.updated") -- what
        # the partner's real Events API /events feed will actually show.
        "wire_event_counts": result.wire_event_counts(),
        "changes": [_change_to_dict(c) for c in result.changes],
        "guardrail": _redact(dict(result.guardrail)) if result.guardrail else {},
        "pushed_files": list(result.pushed_files),
        "content_stats": _redact(dict(content_stats)) if content_stats else None,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


def load_run(path: str | Path) -> dict[str, Any]:
    """Read back a JSON report written by :func:`write_run`.

    Plain ``json.loads`` -- the format is deliberately just JSON, no custom
    envelope, so any tool (this repo's tests, a future dashboard, ``jq``) can
    read it without importing this module.
    """

    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _md_cell(value: Any) -> str:
    """Sanitise ``value`` for safe placement inside a Markdown table cell.

    Escapes backslashes and pipes (a bare ``|`` would split the cell) and
    collapses any newline to a single space (a raw newline would break the
    row entirely). Table structure is never at the mercy of what's in a CSV
    field.
    """

    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text


def _describe_change(c: Change) -> str:
    if c.operation is Operation.CREATE:
        return "created"
    if c.operation is Operation.DELETE:
        return "deleted"
    parts = [
        f'{field}: "{c.before.get(field, "")}" -> "{after_value}"'
        for field, after_value in c.after.items()
    ]
    return "; ".join(parts) if parts else "updated"


def _render_guardrail_section(guardrail: Mapping[str, Any] | None) -> list[str]:
    lines: list[str] = []
    guardrail = guardrail or {}
    by_type = guardrail.get("by_record_type") or []

    if not by_type:
        lines.append("No DELETE operations in this run -- nothing for the guardrail to evaluate.")
    else:
        lines.append(
            "| Record type | Deletes | Total | Ratio | Headroom to 2% ceiling | "
            "Headroom to 10% pause | Verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for v in by_type:
            ratio = float(v.get("ratio") or 0.0)
            headroom_safe = SAFE_THRESHOLD - ratio
            headroom_pause = CLEVER_PAUSE_THRESHOLD - ratio
            lines.append(
                f"| {_md_cell(v.get('record_type'))} | {v.get('deletes')} | {v.get('total')} | "
                f"{ratio:.2%} | {headroom_safe:+.2%} | {headroom_pause:+.2%} | "
                f"{_md_cell(str(v.get('verdict', '')).upper())} |"
            )

    net = guardrail.get("net_attrition") or {}
    if net:
        lines.append("")
        lines.append(
            f"Net attrition this run: {net.get('total_creates', 0)} created vs. "
            f"{net.get('total_deletes', 0)} deleted, against {net.get('total_rows', 0)} total "
            f"rows currently in the stack -- verdict **{str(net.get('verdict', '')).upper()}**."
        )
        if net.get("reason"):
            lines.append("")
            lines.append(f"> {_md_cell(net['reason'])}")

    return lines


def _build_markdown(
    result: RunResult,
    *,
    district_label: str,
    content_stats: Mapping[str, Any] | None,
) -> str:
    plan = result.plan
    label = district_label or result.district
    lines: list[str] = []

    title_prefix = "DRY RUN -- " if result.dry_run else ""
    lines.append(
        f"# {title_prefix}Drift Run Report -- {label} -- "
        f"{plan.run_date.isoformat()} ({plan.weekday_name})"
    )
    lines.append("")

    if result.dry_run:
        lines.append(
            "> **DRY RUN -- NOTHING WAS WRITTEN OR PUSHED TO SFTP.** Everything below "
            "describes what *would* have happened had this been a real run."
        )
        lines.append("")

    if not result.ok:
        lines.append("> **THIS RUN FAILED AND DID NOT COMPLETE.** See \"What failed\" below.")
        lines.append("")

    # --- Metadata -----------------------------------------------------
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Run ID | `{result.run_id}` |")
    lines.append(f"| District | {_md_cell(label)} (`{result.district}`) |")
    lines.append(f"| Date | {plan.run_date.isoformat()} ({plan.weekday_name}) |")
    if plan.buckets:
        bucket_str = ", ".join(b.value for b in plan.buckets)
    else:
        bucket_str = f"(none -- {plan.reason})" if plan.reason else "(none)"
    lines.append(f"| Buckets applied | {_md_cell(bucket_str)} |")
    lines.append(f"| Mode | {'DRY RUN' if result.dry_run else 'LIVE -- pushed to SFTP'} |")
    lines.append(f"| Started | {_iso(result.started_at) or '(unknown)'} |")
    lines.append(f"| Finished | {_iso(result.finished_at) or '(unknown)'} |")
    duration = _duration_seconds(result)
    lines.append(f"| Duration | {f'{duration:.1f}s' if duration is not None else '(unknown)'} |")
    lines.append(f"| Status | {'OK' if result.ok else 'FAILED'} |")
    lines.append("")

    if not result.ok:
        lines.append("## What failed")
        lines.append("")
        lines.append("```")
        lines.append(result.error or "(no error message was recorded on this RunResult)")
        lines.append("```")
        lines.append("")
        lines.append(
            "This run did not finish. Any tables below reflect only whatever changes/"
            "guardrail evaluation had already been computed before the failure -- treat "
            "them as partial, not authoritative. Re-run manually (with `--verbose` logging) "
            "to get a full stack trace if this recurs."
        )
        lines.append("")

    # --- Events the partner should see -- FIRST, before change detail --
    lines.append("## Events the partner should see")
    lines.append("")
    event_counts = result.event_counts()
    wire_event_counts = result.wire_event_counts()
    if event_counts:
        lines.append(
            "**By role (David's breakdown)** -- Clever's wire event alone (below) collapses "
            "students/teachers/contacts/staff into `users.*`; this table restores that "
            "distinction for review purposes. This is NOT a separate event type on the wire."
        )
        lines.append("")
        lines.append("| Expected event (role) | Count |")
        lines.append("|---|---|")
        for label, count in event_counts.items():
            lines.append(f"| {label} | {count} |")
        lines.append("")
        lines.append(
            "**By wire event (what the partner's `/events` feed actually shows)** -- Clever "
            "has no `contacts.*`/`teachers.*` event types; contacts, students, teachers, and "
            "staff are all `users.*` on the wire (role carried in the object's `roles` node, "
            "not the event name)."
        )
        lines.append("")
        lines.append("| Wire event | Count |")
        lines.append("|---|---|")
        for event, count in wire_event_counts.items():
            lines.append(f"| {event} | {count} |")
    else:
        lines.append(
            "No changes were planned in this run -- the partner should see no Events API "
            "activity attributable to it."
        )
    lines.append("")

    # --- Per-bucket change detail, grouped by file ---------------------
    lines.append("## Change detail")
    lines.append("")
    if not result.changes:
        lines.append("(no changes in this run)")
        lines.append("")
    else:
        covered_buckets = list(plan.buckets)
        for bucket in covered_buckets:
            bucket_changes = [c for c in result.changes if c.bucket == bucket]
            if not bucket_changes:
                continue
            lines.append(f"### Bucket: {bucket.value}")
            lines.append("")
            by_file: dict[str, list[Change]] = {}
            for c in bucket_changes:
                by_file.setdefault(c.filename, []).append(c)
            for filename in sorted(by_file):
                lines.append(f"**{filename}**")
                lines.append("")
                lines.append("| Key | Change | Expected event (role) | Note |")
                lines.append("|---|---|---|---|")
                for c in by_file[filename]:
                    lines.append(
                        f"| {_md_cell(c.key_str)} | {_md_cell(_describe_change(c))} | "
                        f"{c.expected_event_label} | {_md_cell(c.note)} |"
                    )
                lines.append("")

        # Defensive: a change whose bucket wasn't in today's plan (should
        # never happen, but a report that silently drops rows is worse than
        # one with an "Other" catch-all).
        stray = [c for c in result.changes if c.bucket not in covered_buckets]
        if stray:
            lines.append("### Other changes (bucket not in today's plan)")
            lines.append("")
            lines.append("| Key | Bucket | Change | Expected event (role) | Note |")
            lines.append("|---|---|---|---|---|")
            for c in stray:
                lines.append(
                    f"| {_md_cell(c.key_str)} | {c.bucket.value} | "
                    f"{_md_cell(_describe_change(c))} | {c.expected_event_label} | "
                    f"{_md_cell(c.note)} |"
                )
            lines.append("")

    # --- Guardrail ------------------------------------------------------
    lines.append("## Guardrail")
    lines.append("")
    lines.extend(_render_guardrail_section(result.guardrail))
    lines.append("")

    # --- AI/canned content stats, if provided ---------------------------
    if content_stats:
        stats = _redact(dict(content_stats))
        lines.append("## Content generation")
        lines.append("")
        lines.append("| Metric | Count |")
        lines.append("|---|---|")
        for k in sorted(stats):
            lines.append(f"| {_md_cell(k)} | {_md_cell(stats[k])} |")
        lines.append("")

    # --- Footer -----------------------------------------------------
    lines.append("## Verifying this on the Events API side")
    lines.append("")
    lines.append(
        "- The counts above are **predictions** based on the CSV edits this run made, not a "
        "confirmation that Clever actually emitted them."
    )
    lines.append(
        "- To confirm, check the partner's Events API subscription/webhook logs for the event "
        "types listed above, timestamped shortly after this run's sync "
        f"({_iso(result.finished_at) or 'see Started/Finished above'})."
    )
    lines.append(
        "- If the partner reports seeing nothing: first confirm this run actually pushed (a "
        "dry run or a failed run pushes nothing -- see Mode/Status above), then confirm Secure "
        "Sync / district-app token eventing is verified active for this district (brief §9), "
        "then check `history.jsonl` for this run_id."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# History line construction
# ---------------------------------------------------------------------------


def _worst_guardrail_ratio(guardrail: Mapping[str, Any] | None) -> float | None:
    by_type = (guardrail or {}).get("by_record_type") or []
    ratios = [
        float(v["ratio"])
        for v in by_type
        if isinstance(v, Mapping) and v.get("ratio") is not None
    ]
    return max(ratios) if ratios else None


def _build_history_record(
    result: RunResult, *, content_stats: Mapping[str, Any] | None
) -> dict[str, Any]:
    plan = result.plan
    record: dict[str, Any] = {
        "run_id": result.run_id,
        "date": plan.run_date.isoformat(),
        "weekday": plan.weekday_name,
        "dry_run": result.dry_run,
        "total_changes": len(result.changes),
        # Label-keyed (David's role breakdown); see SCHEMA_VERSION v2 note.
        "event_counts": result.event_counts(),
        # Bare wire-name totals -- what the partner's /events feed shows.
        "wire_event_counts": result.wire_event_counts(),
        "worst_guardrail_ratio": _worst_guardrail_ratio(result.guardrail),
        "ok": result.ok,
        "error": result.error,
    }
    if content_stats:
        stats = _redact(dict(content_stats))
        if "ai_served" in stats:
            record["ai_served"] = stats["ai_served"]
        if "fallback_served" in stats:
            record["fallback_served"] = stats["fallback_served"]
    return record


# ---------------------------------------------------------------------------
# Preflight (Fix 5): catch an unwritable logs directory BEFORE a run proceeds
# ---------------------------------------------------------------------------


class AuditPreflightError(RuntimeError):
    """``audit.preflight`` found ``logs_root`` unusable.

    Distinct from any exception ``write_run`` itself might raise: a run's
    own ``finish()``-style wrapper (see ``runner.py``) deliberately catches
    and merely logs a ``write_run`` failure rather than failing the run --
    that is the right call for a run that has ALREADY done its work and is
    just trying to record it (see the module docstring's "A failed run
    still produces a full, readable report"). But that has a sharp edge: if
    ``logs/`` is unwritable from the very start, the run proceeds anyway,
    pushes live to SFTP, and produces NO audit trail at all -- a live push
    with zero record of what changed.

    ``preflight`` exists to be called BEFORE any of that -- ideally before a
    run does any work at all -- so that case fails loudly and up front
    instead, as a real exception the caller cannot mistake for "logged and
    moved on."
    """


def preflight(logs_root: str | Path) -> None:
    """Verify ``logs_root`` is writable. Raises :class:`AuditPreflightError` if not.

    Creates ``logs_root`` if it does not exist yet (mirroring ``write_run``'s
    own ``mkdir(parents=True, exist_ok=True)``), then proves writability by
    actually creating and removing a small probe file -- not merely checking
    permission bits, which can be wrong (ACLs, read-only filesystems,
    containers running as an unexpected UID).

    Callers (runner.py) should call this once, early -- before selection,
    before apply, and certainly before any push -- so a run with no writable
    place to record itself never gets far enough to push live. See
    :class:`AuditPreflightError` for why this exists as a separate, loud,
    up-front check rather than relying on ``write_run``'s own (deliberately
    non-fatal, by design) failure handling.
    """

    root = Path(logs_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AuditPreflightError(
            f"logs_root {root} could not be created: {exc}. Refusing to proceed -- "
            "a run must never push live with no writable place to record what it "
            "did."
        ) from exc

    if not root.is_dir():
        raise AuditPreflightError(
            f"logs_root {root} exists but is not a directory. Refusing to proceed."
        )

    probe_path = root / f".preflight-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    try:
        with open(probe_path, "w", encoding="utf-8") as fh:
            fh.write("preflight\n")
    except OSError as exc:
        raise AuditPreflightError(
            f"logs_root {root} is not writable: {exc}. Refusing to proceed -- a run "
            "must never push live with no writable place to record what it did."
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            os.remove(probe_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_run(
    result: RunResult,
    *,
    logs_root: str | Path,
    district_label: str = "",
    content_stats: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Write the JSON report, Markdown report, and history line for one run.

    Returns ``{"json": Path, "markdown": Path, "history": Path}``. Creates
    ``<logs_root>/<district>/`` if it does not already exist. Safe to call
    for a failed run (``result.error`` set) -- see the module docstring.

    Fix 5: this function never catches its own failures -- if any of the
    three writes below raises, that propagates straight out. But some
    callers (``runner.py``'s ``finish()``) deliberately catch and merely log
    a failure here, precisely because by the time this is called the run's
    actual work (including, for a live run, the SFTP push) is already done.
    That is the right call for THAT caller, but it means a write failure
    here can otherwise vanish into an ordinary-looking log line. So this
    also logs at CRITICAL -- not just letting the exception speak for
    itself -- right at the point of failure, before re-raising, so the
    problem is maximally visible in log review even if a caller downgrades
    the exception afterward. See :func:`preflight` for the actual fix
    (verify ``logs_root`` is writable BEFORE a run proceeds at all, so this
    path is rarely hit during the run itself).
    """

    logs_root = Path(logs_root)
    district_dir = logs_root / result.district

    stem = f"{result.plan.run_date.isoformat()}-{result.run_id}"
    json_path = district_dir / f"{stem}.json"
    md_path = district_dir / f"{stem}.md"
    history_path = district_dir / _HISTORY_FILENAME

    try:
        district_dir.mkdir(parents=True, exist_ok=True)

        record = _build_record(result, district_label=district_label, content_stats=content_stats)
        _atomic_write_text(json_path, json.dumps(record, indent=2, sort_keys=False) + "\n")

        markdown = _build_markdown(result, district_label=district_label, content_stats=content_stats)
        _atomic_write_text(md_path, markdown)

        history_record = _build_history_record(result, content_stats=content_stats)
        _append_history_line(history_path, history_record)
    except Exception:
        logger.critical(
            "FAILED TO WRITE AUDIT ARTEFACTS for run %s (district=%s, dry_run=%s, "
            "ok=%s). This run's outcome may not be recorded anywhere on disk. If "
            "this was a LIVE (non-dry-run) push, there is now NO audit trail for "
            "it. Check that %s is writable -- see audit.preflight().",
            result.run_id, result.district, result.dry_run, result.ok, logs_root,
        )
        raise

    logger.info(
        "Wrote audit report for run %s (district=%s, ok=%s): %s",
        result.run_id, result.district, result.ok, md_path,
    )

    return {"json": json_path, "markdown": md_path, "history": history_path}


def read_history(
    logs_root: str | Path, district: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return history.jsonl lines for ``district``, in chronological order.

    ``limit``, if given, keeps only the most recent ``limit`` entries (still
    returned oldest-to-newest). Returns ``[]`` if no history file exists yet
    -- a district with no runs is not an error.
    """

    path = Path(logs_root) / district / _HISTORY_FILENAME
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))

    if limit is not None:
        records = records[-limit:] if limit > 0 else []
    return records


def summarise_history(logs_root: str | Path, district: str, *, days: int = 30) -> str:
    """Markdown summary of the last ``days`` days of runs for ``district``.

    Answers "is this working reliably?" at a glance: runs per weekday,
    cumulative expected-event counts, any failed runs, and an AI-vs-canned
    content trend if ``content_stats`` was ever supplied to ``write_run``.
    """

    records = read_history(logs_root, district)
    if not records:
        return f"# Run history summary -- {district}\n\nNo runs recorded yet.\n"

    cutoff = _dt.date.today() - _dt.timedelta(days=days)
    recent = [r for r in records if _dt.date.fromisoformat(r["date"]) >= cutoff]

    lines: list[str] = [f"# Run history summary -- {district} (last {days} days)", ""]

    if not recent:
        lines.append(
            f"No runs recorded in the last {days} days ({len(records)} older run(s) on file)."
        )
        return "\n".join(lines) + "\n"

    failed = [r for r in recent if not r.get("ok", True)]
    lines.append(f"- Runs recorded: {len(recent)} ({len(failed)} failed)")
    lines.append("")

    weekday_counts: dict[str, int] = {}
    for r in recent:
        wd = r.get("weekday", "?")
        weekday_counts[wd] = weekday_counts.get(wd, 0) + 1
    lines.append("## Runs per weekday")
    lines.append("")
    lines.append("| Weekday | Runs |")
    lines.append("|---|---|")
    for wd in _WEEKDAY_ORDER:
        if wd in weekday_counts:
            lines.append(f"| {wd} | {weekday_counts[wd]} |")
    lines.append("")

    event_totals: dict[str, int] = {}
    for r in recent:
        for event, count in (r.get("event_counts") or {}).items():
            event_totals[event] = event_totals.get(event, 0) + count
    lines.append("## Cumulative expected events (by role)")
    lines.append("")
    lines.append("| Event (role) | Count |")
    lines.append("|---|---|")
    if event_totals:
        for event in sorted(event_totals):
            lines.append(f"| {event} | {event_totals[event]} |")
    else:
        lines.append("| (none) | 0 |")
    lines.append("")

    wire_totals: dict[str, int] = {}
    for r in recent:
        for event, count in (r.get("wire_event_counts") or {}).items():
            wire_totals[event] = wire_totals.get(event, 0) + count
    if wire_totals:
        lines.append("## Cumulative expected events (bare wire event -- what `/events` shows)")
        lines.append("")
        lines.append("| Wire event | Count |")
        lines.append("|---|---|")
        for event in sorted(wire_totals):
            lines.append(f"| {event} | {wire_totals[event]} |")
        lines.append("")

    lines.append("## Failed runs")
    lines.append("")
    if failed:
        lines.append("| Date | Run ID | Error |")
        lines.append("|---|---|---|")
        for r in failed:
            error_text = r.get("error") or "(no error message recorded)"
            lines.append(f"| {r.get('date')} | {r.get('run_id')} | {_md_cell(error_text)} |")
    else:
        lines.append("None -- every recorded run in this window completed without error.")
    lines.append("")

    ai_records = [r for r in recent if "ai_served" in r or "fallback_served" in r]
    if ai_records:
        total_ai = sum(r.get("ai_served", 0) for r in ai_records)
        total_fallback = sum(r.get("fallback_served", 0) for r in ai_records)
        lines.append("## AI vs. canned content")
        lines.append("")
        lines.append(f"- AI-generated values served: {total_ai}")
        lines.append(f"- Canned/fallback values served: {total_fallback}")
        lines.append("")

    worst_ratios = [
        r["worst_guardrail_ratio"] for r in recent if r.get("worst_guardrail_ratio") is not None
    ]
    if worst_ratios:
        lines.append("## Guardrail")
        lines.append("")
        lines.append(f"Worst single-run deletion ratio in this window: {max(worst_ratios):.2%}")
        lines.append("")

    return "\n".join(lines)


def new_run_id() -> str:
    """A short, sortable, collision-resistant run id.

    Format: UTC timestamp (sorts correctly as a string) + 6 hex chars of
    ``secrets`` randomness (16.7M possibilities -- collisions across two runs
    started in the same second are not a practical concern for this engine's
    once-a-day-per-district cadence).
    """

    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3)
    return f"{timestamp}-{suffix}"


def configure_logging(verbose: bool = False) -> None:
    """Stdlib logging setup shared by the CLI.

    Concise single-line console format; ``force=True`` so this can be called
    more than once (e.g. once by a test, once by the CLI) without silently
    no-op'ing because the root logger was already configured.
    """

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
