"""Enforces Clever's deletion threshold as a HARD gate (project brief §3).

    "Clever pauses a sync for review if more than 10% of any single record
    type is deleted in one sync ... This should be enforced in code as a
    hard guardrail, not just a design intention."

Two thresholds are in play, and they mean different things:

* ``CLEVER_PAUSE_THRESHOLD`` (10%) is Clever's own actual limit -- the point
  past which Clever itself pauses the sync for review. Breaching this is a
  hard block: :func:`enforce` raises before a single byte is written.
* ``SAFE_THRESHOLD`` (2%) is this engine's own, far stricter operating
  ceiling. The brief says to stay "well under" Clever's ratio, and the real
  fixed cadence (brief §4) only ever deletes a couple of contacts per run
  out of tens of thousands -- so a ratio anywhere near 2% is not "a bigger
  but still fine" version of a normal run, it is a sign that selection
  picked from the wrong pool, that the stack is far smaller than expected,
  or that something upstream is broken. Breaching this is a warning, not a
  block, so David sees it in the audit log without the run being stopped
  outright.

``evaluate``/``enforce`` are the only functions the runner needs: ``enforce``
is what must be called before ``CsvStack.save``/``sftp_push.push`` on every
run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import schema
from .csvstack import CsvStack
from .models import Change, GuardrailViolation, Operation

__all__ = [
    "CLEVER_PAUSE_THRESHOLD",
    "SAFE_THRESHOLD",
    "RecordTypeVerdict",
    "NetAttritionVerdict",
    "GuardrailReport",
    "evaluate",
    "enforce",
]

#: Clever's actual pause-for-review limit (brief §3). Breaching this blocks.
CLEVER_PAUSE_THRESHOLD = 0.10

#: This engine's own, stricter operating ceiling. Breaching this warns but
#: does not block -- see module docstring.
SAFE_THRESHOLD = 0.02

#: Below this total row count across the *whole* stack, any deletion at all
#: is treated as suspicious for the net-attrition check below. The fixed
#: cadence (brief §4) is sized for a real district (tens of thousands of
#: students/contacts); a stack this small deleting anything looks like the
#: engine is pointed at a stub/test stack rather than a real sandbox replica,
#: or that a prior run has already eroded it further than intended.
SMALL_STACK_ROW_THRESHOLD = 500

#: A run whose deletes outnumber its creates by more than this multiple,
#: once at least ``NET_ATTRITION_MIN_ABS`` more rows are deleted than
#: created, is flagged -- even if every individual record type stayed well
#: within ``SAFE_THRESHOLD``. See module docstring / ``evaluate`` for the
#: reasoning (this is the "net attrition" failure mode).
NET_ATTRITION_RATIO = 2.0
NET_ATTRITION_MIN_ABS = 3


@dataclass(frozen=True)
class RecordTypeVerdict:
    """The guardrail's verdict for a single record type in one run.

    ``deletes`` is the EFFECTIVE deletion count used for the ratio -- not
    necessarily the raw number of ``Operation.DELETE`` changes planned for
    this record type. Two adjustments feed into it (see ``evaluate``):

    * Fix 4: a CREATE/DELETE pair on this record type within the same run
      (e.g. an enrollment section move -- always a DELETE of the old
      (Section id, Student id) row plus a CREATE of the new one) is netted
      out first. ``moves_netted`` records how many pairs were excluded this
      way.
    * Fix 3: rows missing versus the last successfully-pushed count, with no
      corresponding planned delete in this run to explain them (a CSV
      truncated or altered outside this engine between runs), are added in.
      ``unexplained_loss`` records that amount.
    """

    record_type: str
    deletes: int
    total: int
    ratio: float
    verdict: str  # "ok" | "warn" | "block"
    reason: str = ""
    #: Fix 4: matched CREATE/DELETE pairs for this record type in this run,
    #: excluded from ``deletes``/``ratio`` -- see the class docstring.
    moves_netted: int = 0
    #: Fix 3: rows missing versus the last successfully-pushed count, with no
    #: corresponding planned delete in this run -- see the class docstring.
    unexplained_loss: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "deletes": self.deletes,
            "total": self.total,
            "ratio": self.ratio,
            "verdict": self.verdict,
            "reason": self.reason,
            "moves_netted": self.moves_netted,
            "unexplained_loss": self.unexplained_loss,
        }


@dataclass(frozen=True)
class NetAttritionVerdict:
    """Whole-run net create/delete balance, independent of per-type ratios.

    See ``evaluate`` for why this exists: the 10%/2% ratios above only ever
    look at *one run, one record type* in isolation. A small daily bias
    toward net deletion -- never enough to trip either ratio -- would still
    slowly shrink the sandbox over months of unattended daily runs. This
    field exists to catch that slow bleed, which the per-type ratios
    structurally cannot see.
    """

    total_creates: int
    total_deletes: int
    total_rows: int
    verdict: str  # "ok" | "warn"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_creates": self.total_creates,
            "total_deletes": self.total_deletes,
            "total_rows": self.total_rows,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GuardrailReport:
    """Full guardrail verdict for one run: per-record-type plus net attrition."""

    by_record_type: tuple[RecordTypeVerdict, ...]
    net_attrition: NetAttritionVerdict

    @property
    def blocked(self) -> bool:
        return any(v.verdict == "block" for v in self.by_record_type)

    @property
    def warnings(self) -> tuple[RecordTypeVerdict, ...]:
        return tuple(v for v in self.by_record_type if v.verdict == "warn")

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form, suitable for ``RunResult.guardrail``."""

        return {
            "by_record_type": [v.to_dict() for v in self.by_record_type],
            "net_attrition": self.net_attrition.to_dict(),
            "blocked": self.blocked,
        }

    def summary(self) -> str:
        """Human-readable rendering for the audit log."""

        lines = ["Guardrail report:"]
        if not self.by_record_type:
            lines.append("  (no DELETE operations in this run)")
        for v in self.by_record_type:
            line = (
                f"  {v.record_type:<12} deletes={v.deletes:<5} total={v.total:<7} "
                f"ratio={v.ratio:>7.2%}  verdict={v.verdict.upper()}"
            )
            if v.reason:
                line += f"\n      -- {v.reason}"
            lines.append(line)

        na = self.net_attrition
        na_line = (
            f"  net_attrition   creates={na.total_creates:<5} deletes={na.total_deletes:<5} "
            f"stack_total={na.total_rows:<7} verdict={na.verdict.upper()}"
        )
        if na.reason:
            na_line += f"\n      -- {na.reason}"
        lines.append(na_line)
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - trivial delegation
        return self.summary()


def _record_type_for(filename: str) -> str:
    spec = schema.BY_FILENAME.get(filename)
    return spec.record_type if spec is not None else filename


def evaluate(
    stack: CsvStack,
    changes: Sequence[Change],
    *,
    last_pushed_counts: Mapping[str, int] | None = None,
) -> GuardrailReport:
    """Evaluate ``changes`` against ``stack``'s CURRENT (pre-change) counts.

    Clever computes the 10% threshold against the size of the record type
    *before* the sync it is evaluating -- i.e. the denominator is the
    pre-change total in the currently-loaded stack, not the post-change
    total. That is exactly what ``stack.counts()`` returns here, since this
    is called before ``CsvStack.apply``.

    Per record type, an EFFECTIVE deletion count is computed and divided by
    a denominator, then assigned a verdict:

    * ``total == 0`` and ``deletes > 0`` -> ``block`` (deleting from an
      empty/absent record type is treated as an explicit block, never a
      divide-by-zero, and never silently "0% since there's nothing to
      delete from" -- if a Change references a record type the stack claims
      has zero rows, something upstream is already wrong).
    * ``ratio > CLEVER_PAUSE_THRESHOLD`` -> ``block``.
    * ``ratio > SAFE_THRESHOLD`` -> ``warn``.
    * otherwise -> ``ok``.

    The effective deletion count is the raw ``Operation.DELETE`` count for
    that record type, adjusted two ways:

    * Fix 4 (matched moves are not attrition): any CREATE for the SAME
      record type in the SAME run is netted against the deletes first (e.g.
      selection.py's enrollment section move is always a DELETE of the old
      (Section id, Student id) row plus a CREATE of the new one -- Clever's
      row-level diff sees both, but this is not the mass-deletion pattern
      the threshold exists to catch, and on a small stack a single matched
      move could otherwise trip the ratio purely from a tiny denominator).
      Deletes left over after netting are genuine and counted in full.
    * Fix 3 (truncation outside this engine is not invisible): when the
      caller supplies ``last_pushed_counts`` -- the record counts as of the
      last successful real push, see ``sftp_push.read_last_pushed_counts``/
      ``sftp_push._write_last_pushed_counts`` -- any record type whose
      CURRENT (pre-this-run) count is lower than that, with no planned
      delete in this run to explain the gap, is added to the effective
      deletion count as ``unexplained_loss``. This is what catches a CSV
      truncated or altered by something other than this engine between two
      runs: the pre-change stack this function sees already reflects the
      damage, with zero DELETE changes to show for it, so the raw-deletes-only
      computation is structurally blind to it. When ``last_pushed_counts``
      has an entry for a record type, that entry (not the current count) is
      used as the ratio's denominator too, matching what Clever itself last
      saw. Growth (e.g. contacts going 0 -> 50,000 in a seed run) never
      contributes here -- only a net LOSS versus last_pushed_counts does, so
      legitimate growth is never mistaken for attrition. ``last_pushed_counts``
      being absent entirely (``None``) is a genuine first run / not-yet-wired
      caller and reproduces the exact pre-fix behaviour.

    Also computes the whole-run net-attrition verdict (see
    :class:`NetAttritionVerdict`), which already nets a matched move's
    create/delete pair to zero at the whole-run level and is unaffected by
    either adjustment above.
    """

    current_counts = stack.counts()

    deletes_by_type: dict[str, int] = {}
    creates_by_type: dict[str, int] = {}
    for change in changes:
        record_type = _record_type_for(change.filename)
        if change.operation is Operation.DELETE:
            deletes_by_type[record_type] = deletes_by_type.get(record_type, 0) + 1
        elif change.operation is Operation.CREATE:
            creates_by_type[record_type] = creates_by_type.get(record_type, 0) + 1

    # Fix 4: net matched CREATE/DELETE pairs per record type before anything
    # else touches the deletion count -- see the docstring above.
    moves_netted_by_type: dict[str, int] = {}
    net_deletes_by_type: dict[str, int] = {}
    for record_type, raw_deletes in deletes_by_type.items():
        matched = min(raw_deletes, creates_by_type.get(record_type, 0))
        moves_netted_by_type[record_type] = matched
        net_deletes_by_type[record_type] = raw_deletes - matched

    # Fix 3: rows missing versus the last successfully-pushed counts, not
    # explained by this run's own planned deletes -- see the docstring above.
    unexplained_loss_by_type: dict[str, int] = {}
    if last_pushed_counts:
        for record_type, last_count in last_pushed_counts.items():
            loss = last_count - current_counts.get(record_type, 0)
            if loss > 0:
                unexplained_loss_by_type[record_type] = loss

    verdicts: list[RecordTypeVerdict] = []
    # Report on any record type with genuine planned-delete activity OR
    # unexplained loss versus the last push -- a report entry per record
    # type in the whole schema regardless of activity would bury the signal
    # in noise in the audit log.
    record_types = set(net_deletes_by_type) | set(unexplained_loss_by_type)
    for record_type in sorted(record_types):
        moves_netted = moves_netted_by_type.get(record_type, 0)
        unexplained = unexplained_loss_by_type.get(record_type, 0)
        deletes = net_deletes_by_type.get(record_type, 0) + unexplained

        use_last_pushed = (
            last_pushed_counts is not None
            and last_pushed_counts.get(record_type, 0) > 0
        )
        total = last_pushed_counts[record_type] if use_last_pushed else current_counts.get(record_type, 0)

        notes: list[str] = []
        if moves_netted:
            notes.append(
                f"{moves_netted} matched CREATE/DELETE pair(s) for this record type in this "
                "run (e.g. an enrollment section move) were netted out and are not counted "
                "as attrition."
            )
        if unexplained:
            notes.append(
                f"{unexplained} row(s) of this record type are missing versus the last "
                "successfully-pushed count, with no corresponding planned delete in this "
                "run to explain the gap -- this looks like the CSV was truncated or "
                "altered outside this engine between runs."
            )

        if total <= 0:
            if deletes > 0:
                ratio = 1.0
                verdict = "block"
                reason = (
                    f"{deletes} delete(s) effectively planned for record type "
                    f"{record_type!r}, but the stack currently has zero rows of that "
                    "type. Refusing to treat this as 0% (divide-by-zero) -- a delete "
                    "against an empty/absent record type means something upstream "
                    "already picked an invalid target."
                )
            else:
                ratio = 0.0
                verdict = "ok"
                reason = ""
        else:
            ratio = deletes / total
            if ratio > CLEVER_PAUSE_THRESHOLD:
                verdict = "block"
                reason = (
                    f"{deletes}/{total} ({ratio:.2%}) of {record_type} would be deleted in "
                    f"this run, exceeding Clever's {CLEVER_PAUSE_THRESHOLD:.0%} pause-for-review "
                    "threshold (brief §3). Clever would pause this sync; refusing to write."
                )
            elif ratio > SAFE_THRESHOLD:
                verdict = "warn"
                reason = (
                    f"{deletes}/{total} ({ratio:.2%}) of {record_type} would be deleted in "
                    f"this run -- above this engine's {SAFE_THRESHOLD:.0%} operating ceiling, "
                    f"though still under Clever's {CLEVER_PAUSE_THRESHOLD:.0%} pause threshold. "
                    "The fixed cadence normally deletes only a handful of rows out of tens of "
                    "thousands, so this ratio is worth a second look even though it will not "
                    "block."
                )
            else:
                verdict = "ok"
                reason = ""

        if notes:
            reason = f"{reason} {' '.join(notes)}".strip()

        verdicts.append(
            RecordTypeVerdict(
                record_type,
                deletes,
                total,
                ratio,
                verdict,
                reason,
                moves_netted=moves_netted,
                unexplained_loss=unexplained,
            )
        )

    total_creates = sum(creates_by_type.values())
    total_deletes = sum(deletes_by_type.values())
    total_rows = sum(current_counts.values())

    net_verdict = "ok"
    net_reason = ""
    if total_deletes > 0 and total_rows < SMALL_STACK_ROW_THRESHOLD:
        net_verdict = "warn"
        net_reason = (
            f"This run deletes {total_deletes} row(s) while the whole stack has only "
            f"{total_rows} rows across every record type (below the "
            f"{SMALL_STACK_ROW_THRESHOLD}-row sanity floor). Any deletion at all against a "
            "stack this small is treated as suspicious, regardless of per-type ratio."
        )
    else:
        net_delta = total_deletes - total_creates
        ratio_breach = total_creates == 0 or (total_deletes / max(total_creates, 1)) > NET_ATTRITION_RATIO
        if net_delta >= NET_ATTRITION_MIN_ABS and ratio_breach:
            net_verdict = "warn"
            net_reason = (
                f"This run deletes {total_deletes} row(s) against only {total_creates} "
                f"creation(s) across the whole run (net attrition of {net_delta}, more than "
                f"{NET_ATTRITION_RATIO}x). No single record type may have tripped its own "
                "threshold, but a per-run bias toward deletion, repeated daily over months "
                "with nothing that ever tops the roster back up, would silently erode the "
                "sandbox's size. Flagging so David can confirm this run's shape is expected."
            )

    net_attrition = NetAttritionVerdict(total_creates, total_deletes, total_rows, net_verdict, net_reason)

    return GuardrailReport(by_record_type=tuple(verdicts), net_attrition=net_attrition)


def enforce(
    stack: CsvStack,
    changes: Sequence[Change],
    *,
    last_pushed_counts: Mapping[str, int] | None = None,
) -> GuardrailReport:
    """``evaluate`` plus a hard stop: raise if ANY record type blocks.

    This is what the runner calls before writing anything (``CsvStack.save``
    or ``sftp_push.push``). Net-attrition warnings never raise here -- by
    design they are a signal for David's audit log, not a hard stop -- only
    a per-record-type ``block`` verdict raises.

    ``last_pushed_counts``, if supplied, feeds ``evaluate``'s Fix 3
    unexplained-loss check (see that function's docstring) -- pass
    ``sftp_push.read_last_pushed_counts(current_dir)`` here.
    """

    report = evaluate(stack, changes, last_pushed_counts=last_pushed_counts)
    if report.blocked:
        blocked = [v for v in report.by_record_type if v.verdict == "block"]
        detail = "; ".join(v.reason for v in blocked)
        names = ", ".join(v.record_type for v in blocked)
        raise GuardrailViolation(
            f"Guardrail blocked this run for record type(s) [{names}]: {detail}"
        )
    return report
