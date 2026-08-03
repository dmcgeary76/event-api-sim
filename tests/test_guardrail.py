"""Tests for drift_engine.guardrail.

Covers: ratios computed against the pre-change denominator, block above 10%,
warn above 2%, the zero-denominator block (not a crash), enforce() raising
GuardrailViolation naming the record type, the net-attrition warn, dict
serialisation / summary rendering, and a realistic-cadence sanity check (2
contact deletes against ~50,000 contacts).
"""

from __future__ import annotations

import pytest

from drift_engine import guardrail
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, Change, EventSubject, EventType, GuardrailViolation, Operation


def _stack_with_counts(**counts: int) -> CsvStack:
    """A CsvStack whose ``counts()`` match ``counts`` exactly.

    Row *contents* don't matter for the guardrail (it only calls
    ``stack.counts()``), so each table is just a list of empty dicts of the
    right length.
    """

    tables = {filename: [{} for _ in range(n)] for filename, n in _files_for(counts).items()}
    return CsvStack(tables, migrated_columns={})


# record_type -> filename, mirroring schema.BY_RECORD_TYPE without importing
# schema's specific column layout (the guardrail doesn't care about columns).
_RECORD_TYPE_FILES = {
    "schools": "schools.csv",
    "students": "students.csv",
    "teachers": "teachers.csv",
    "staff": "staff.csv",
    "sections": "sections.csv",
    "enrollments": "enrollments.csv",
    "contacts": "contacts.csv",
}


def _files_for(counts: dict[str, int]) -> dict[str, int]:
    return {_RECORD_TYPE_FILES[record_type]: n for record_type, n in counts.items()}


def _delete_change(filename: str, key: str = "K1") -> Change:
    return Change(
        filename=filename,
        operation=Operation.DELETE,
        key={"id": key},
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_DELETED,
        event_subject=EventSubject.CONTACT,
        before={"id": key},
    )


def _create_change(filename: str, key: str = "K1") -> Change:
    return Change(
        filename=filename,
        operation=Operation.CREATE,
        key={"id": key},
        bucket=Bucket.SMALL_DAILY,
        expected_event=EventType.USERS_CREATED,
        event_subject=EventSubject.CONTACT,
        after={"id": key},
    )


# ---------------------------------------------------------------------------
# Basic ratio / verdict computation
# ---------------------------------------------------------------------------


def test_ratio_computed_against_pre_change_denominator():
    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(5)]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.record_type == "contacts"
    assert verdict.deletes == 5
    assert verdict.total == 100  # pre-change total, not 95
    assert verdict.ratio == pytest.approx(0.05)
    assert verdict.verdict == "warn"  # >2%, <=10%


def test_block_above_ten_percent():
    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(11)]  # 11%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.11)
    assert verdict.verdict == "block"
    assert report.blocked is True


def test_exactly_ten_percent_does_not_block():
    """The guardrail says '> 10%', not '>= 10%' -- exactly the threshold is
    still within Clever's own limit."""

    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(10)]  # exactly 10%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.10)
    assert verdict.verdict == "warn"  # over 2%, not over 10%
    assert report.blocked is False


def test_warn_above_two_percent():
    stack = _stack_with_counts(contacts=1000)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(21)]  # 2.1%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.verdict == "warn"
    assert report.blocked is False


def test_exactly_two_percent_is_ok_not_warn():
    stack = _stack_with_counts(contacts=1000)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(20)]  # exactly 2%
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.ratio == pytest.approx(0.02)
    assert verdict.verdict == "ok"


def test_zero_denominator_is_a_block_not_a_crash():
    stack = _stack_with_counts(contacts=0)
    changes = [_delete_change("contacts.csv", "C1")]

    report = guardrail.evaluate(stack, changes)  # must not raise ZeroDivisionError

    [verdict] = report.by_record_type
    assert verdict.total == 0
    assert verdict.verdict == "block"
    assert report.blocked is True


def test_no_deletes_produces_no_record_type_verdicts():
    stack = _stack_with_counts(contacts=100, students=100)
    changes = [_create_change("contacts.csv", "C1")]
    report = guardrail.evaluate(stack, changes)

    assert report.by_record_type == ()
    assert report.blocked is False


def test_multiple_record_types_evaluated_independently():
    stack = _stack_with_counts(contacts=100, students=1000)
    changes = [_delete_change("contacts.csv", "C1")] + [
        _delete_change("students.csv", f"S{i}") for i in range(150)  # 15% of students
    ]
    report = guardrail.evaluate(stack, changes)

    by_type = {v.record_type: v for v in report.by_record_type}
    assert by_type["contacts"].verdict == "ok"  # 1/100 = 1%
    assert by_type["students"].verdict == "block"  # 150/1000 = 15%
    assert report.blocked is True


# ---------------------------------------------------------------------------
# enforce()
# ---------------------------------------------------------------------------


def test_enforce_raises_guardrail_violation_naming_record_type():
    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(11)]

    with pytest.raises(GuardrailViolation, match="contacts"):
        guardrail.enforce(stack, changes)


def test_enforce_passes_through_safe_run():
    stack = _stack_with_counts(contacts=50000)
    changes = [_delete_change("contacts.csv", "C1"), _delete_change("contacts.csv", "C2")]
    report = guardrail.enforce(stack, changes)  # must not raise
    assert report.blocked is False


def test_enforce_does_not_raise_for_warn_only():
    stack = _stack_with_counts(contacts=1000)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(21)]  # warn, not block
    report = guardrail.enforce(stack, changes)
    assert any(v.verdict == "warn" for v in report.by_record_type)


# ---------------------------------------------------------------------------
# Realistic cadence sanity check
# ---------------------------------------------------------------------------


def test_realistic_cadence_two_contact_deletes_against_fifty_thousand_passes_comfortably():
    """Brief §4's actual daily/Tue-Thu cadence deletes a handful of contacts
    against a district-scale stack. This must clear both thresholds by a
    wide margin."""

    stack = _stack_with_counts(contacts=50_000, students=33_620)
    changes = [_delete_change("contacts.csv", "C1"), _delete_change("contacts.csv", "C2")]

    report = guardrail.enforce(stack, changes)  # must not raise

    [verdict] = report.by_record_type
    assert verdict.deletes == 2
    assert verdict.total == 50_000
    assert verdict.ratio == pytest.approx(2 / 50_000)
    assert verdict.verdict == "ok"
    assert verdict.ratio < guardrail.SAFE_THRESHOLD
    assert verdict.ratio < guardrail.CLEVER_PAUSE_THRESHOLD


# ---------------------------------------------------------------------------
# Net attrition
# ---------------------------------------------------------------------------


def test_net_attrition_warns_when_deletes_far_outnumber_creates():
    stack = _stack_with_counts(contacts=10_000)
    changes = (
        [_delete_change("contacts.csv", f"C{i}") for i in range(10)]
        + [_create_change("contacts.csv", "NEW1")]
    )
    report = guardrail.evaluate(stack, changes)

    assert report.net_attrition.total_deletes == 10
    assert report.net_attrition.total_creates == 1
    assert report.net_attrition.verdict == "warn"
    assert report.net_attrition.reason


def test_net_attrition_ok_when_balanced():
    stack = _stack_with_counts(contacts=10_000)
    changes = [_delete_change("contacts.csv", "C1"), _create_change("contacts.csv", "NEW1")]
    report = guardrail.evaluate(stack, changes)

    assert report.net_attrition.verdict == "ok"


def test_net_attrition_warns_for_any_deletion_against_a_small_stack():
    stack = _stack_with_counts(contacts=10)  # well below the 500-row sanity floor
    changes = [_delete_change("contacts.csv", "C1"), _create_change("contacts.csv", "NEW1")]
    report = guardrail.evaluate(stack, changes)

    # Balanced 1-for-1, but the whole stack is suspiciously small.
    assert report.net_attrition.verdict == "warn"
    assert "500" in report.net_attrition.reason or "small" in report.net_attrition.reason.lower()


def test_net_attrition_does_not_warn_for_small_run_within_normal_bounds():
    stack = _stack_with_counts(contacts=50_000)
    changes = [_delete_change("contacts.csv", "C1")]
    report = guardrail.evaluate(stack, changes)

    # 1 delete, 0 creates -- below NET_ATTRITION_MIN_ABS, should not warn.
    assert report.net_attrition.verdict == "ok"


# ---------------------------------------------------------------------------
# Serialisation / rendering
# ---------------------------------------------------------------------------


def test_report_to_dict_is_json_shaped():
    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(11)]
    report = guardrail.evaluate(stack, changes)

    as_dict = report.to_dict()
    assert isinstance(as_dict, dict)
    assert as_dict["blocked"] is True
    assert as_dict["by_record_type"][0]["record_type"] == "contacts"
    assert as_dict["by_record_type"][0]["verdict"] == "block"
    assert "net_attrition" in as_dict
    assert isinstance(as_dict["net_attrition"], dict)

    import json

    json.dumps(as_dict)  # must be plain-JSON-serialisable


def test_report_summary_and_str_are_readable():
    stack = _stack_with_counts(contacts=100)
    changes = [_delete_change("contacts.csv", f"C{i}") for i in range(11)]
    report = guardrail.evaluate(stack, changes)

    summary = report.summary()
    assert "contacts" in summary
    assert "BLOCK" in summary
    assert str(report) == summary


# ---------------------------------------------------------------------------
# Fix 4: matched CREATE/DELETE pairs (enrollment moves) are not attrition
# ---------------------------------------------------------------------------


def test_matched_create_delete_pair_is_not_counted_as_a_deletion():
    """A DELETE+CREATE pair on the same record type in the same run (an
    enrollment section move) must not trip the ratio, even on a tiny stack
    where a single unmatched delete would."""

    stack = _stack_with_counts(enrollments=10)
    changes = [
        _delete_change("enrollments.csv", "OLD1"),
        _create_change("enrollments.csv", "NEW1"),
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.deletes == 0
    assert verdict.moves_netted == 1
    assert verdict.verdict == "ok"
    assert report.blocked is False


def test_unmatched_deletes_still_counted_after_netting_moves():
    stack = _stack_with_counts(enrollments=10)
    changes = [
        _delete_change("enrollments.csv", "OLD1"),
        _delete_change("enrollments.csv", "OLD2"),  # unmatched -- genuine
        _create_change("enrollments.csv", "NEW1"),  # matches OLD1 only
    ]
    report = guardrail.evaluate(stack, changes)

    [verdict] = report.by_record_type
    assert verdict.moves_netted == 1
    assert verdict.deletes == 1  # OLD2 is genuine and unmatched
    assert verdict.ratio == pytest.approx(0.1)


def test_move_on_small_toy_stack_does_not_block():
    """Regression for the reported 624 false blocks across 720 fuzzed toy
    stack shapes: a single matched move on a tiny stack must never block,
    even though a bare 1-delete-of-3 ratio (33%) would."""

    stack = _stack_with_counts(enrollments=3)
    changes = [
        _delete_change("enrollments.csv", "OLD1"),
        _create_change("enrollments.csv", "NEW1"),
    ]
    report = guardrail.enforce(stack, changes)  # must not raise
    assert report.blocked is False


def test_moves_do_not_mask_a_genuine_mass_deletion_of_a_different_type():
    """Netting is scoped per record type -- a pile of matched enrollment
    moves must never hide unrelated, unmatched attrition on another type."""

    stack = _stack_with_counts(enrollments=100, students=100)
    changes = (
        [_delete_change("enrollments.csv", f"E{i}") for i in range(20)]
        + [_create_change("enrollments.csv", f"N{i}") for i in range(20)]  # all matched
        + [_delete_change("students.csv", f"S{i}") for i in range(15)]  # 15%, unmatched
    )
    report = guardrail.evaluate(stack, changes)

    by_type = {v.record_type: v for v in report.by_record_type}
    assert by_type["enrollments"].deletes == 0
    assert by_type["enrollments"].verdict == "ok"
    assert by_type["students"].deletes == 15
    assert by_type["students"].verdict == "block"
    assert report.blocked is True


# ---------------------------------------------------------------------------
# Fix 3: actual row-count delta vs. the last successfully-pushed counts
# ---------------------------------------------------------------------------


def test_truncation_with_no_planned_deletes_is_invisible_without_last_pushed_counts():
    """Confirms the bug this fix closes still reproduces when the caller has
    no last_pushed_counts to give: students.csv truncated by 3,000/33,621
    rows (8.9%) with zero DELETE changes planned produces no verdicts at
    all, and nothing blocks."""

    stack = _stack_with_counts(students=30_621)  # already truncated on load
    report = guardrail.evaluate(stack, [])
    assert report.by_record_type == ()
    assert report.blocked is False


def test_truncation_at_8_9_percent_is_now_visible_as_a_warn_via_last_pushed_counts():
    """The exact reproduction from the audit: students.csv truncated by
    3,000/33,621 rows (8.9%) outside the engine, with zero DELETE changes
    planned. 8.9% is under Clever's own 10% pause threshold, so this
    correctly WARNS rather than blocks -- but critically, it is no longer
    invisible (by_record_type used to be () with nothing to show for it;
    now the truncation is on record for David to see, where before this fix
    landed silently)."""

    stack = _stack_with_counts(students=30_621)  # 33,621 - 3,000
    report = guardrail.evaluate(stack, [], last_pushed_counts={"students": 33_621})

    [verdict] = report.by_record_type
    assert verdict.record_type == "students"
    assert verdict.unexplained_loss == 3_000
    assert verdict.deletes == 3_000
    assert verdict.total == 33_621
    assert verdict.ratio == pytest.approx(3_000 / 33_621)
    assert verdict.verdict == "warn"
    assert "truncated or altered" in verdict.reason


def test_truncation_beyond_ten_percent_is_blocked_via_last_pushed_counts():
    """A larger truncation -- enough to cross Clever's actual 10%
    pause-for-review threshold -- must hard-block, exactly like an
    equivalent number of planned DELETE changes would."""

    stack = _stack_with_counts(students=30_000)  # 33,621 - 3,621 (~10.77%)
    report = guardrail.evaluate(stack, [], last_pushed_counts={"students": 33_621})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 3_621
    assert verdict.verdict == "block"
    assert report.blocked is True

    with pytest.raises(GuardrailViolation, match="students"):
        guardrail.enforce(stack, [], last_pushed_counts={"students": 33_621})


def test_first_run_with_no_last_pushed_counts_is_unaffected():
    """A genuine first run has no last_pushed_counts yet -- ordinary planned
    deletes must be evaluated exactly as before."""

    stack = _stack_with_counts(students=100)
    changes = [_delete_change("students.csv", "S1")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts=None)

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 0
    assert verdict.total == 100


def test_legitimate_growth_from_zero_is_not_treated_as_attrition():
    """contacts going 0 -> 50,000 in a seed run must never be flagged, even
    though last_pushed_counts reflects the pre-seed 0 -- growth is not loss."""

    stack = _stack_with_counts(contacts=0)
    changes = [_create_change("contacts.csv", f"C{i}") for i in range(50_000)]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 0})

    assert report.by_record_type == ()
    assert report.blocked is False


def test_last_pushed_counts_matching_current_changes_nothing():
    """When nothing has touched the stack outside this engine, current
    counts equal last_pushed_counts, so ordinary runs behave exactly as
    before the fix."""

    stack = _stack_with_counts(contacts=50_000)
    changes = [_delete_change("contacts.csv", "C1"), _delete_change("contacts.csv", "C2")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 50_000})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 0
    assert verdict.deletes == 2
    assert verdict.verdict == "ok"


def test_unexplained_loss_combines_with_this_runs_own_planned_deletes():
    """Truncation AND a normal run's own small planned deletes both count,
    additively, against the last-pushed denominator."""

    stack = _stack_with_counts(contacts=48_000)  # already down from 50,000
    changes = [_delete_change("contacts.csv", "C1"), _delete_change("contacts.csv", "C2")]
    report = guardrail.evaluate(stack, changes, last_pushed_counts={"contacts": 50_000})

    [verdict] = report.by_record_type
    assert verdict.unexplained_loss == 2_000
    assert verdict.deletes == 2_002
    assert verdict.total == 50_000
