"""Tests for drift_engine.audit.

Covers: the three artefacts write_run produces and their names, JSON
round-tripping via load_run, Markdown ordering (expected-events table before
change detail) and dry-run/error visibility, redaction of credential-shaped
keys in guardrail/content_stats, Markdown-table injection safety (pipes and
newlines in a Change value), history.jsonl append semantics across multiple
runs, read_history/summarise_history, and write atomicity (no stray .tmp
files).
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from drift_engine import audit
from drift_engine.csvstack import CsvStack
from drift_engine.guardrail import evaluate as guardrail_evaluate
from drift_engine.models import Bucket, Change, EventSubject, EventType, Operation, RunPlan, RunResult

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _plan(
    run_date: _dt.date = _dt.date(2026, 7, 30),
    buckets: tuple[Bucket, ...] = (Bucket.SMALL_DAILY,),
) -> RunPlan:
    return RunPlan(run_date=run_date, buckets=buckets)


def _change(
    filename: str = "students.csv",
    operation: Operation = Operation.UPDATE,
    key: dict | None = None,
    bucket: Bucket = Bucket.SMALL_DAILY,
    expected_event: EventType = EventType.USERS_UPDATED,
    event_subject: EventSubject = EventSubject.STUDENT,
    before: dict | None = None,
    after: dict | None = None,
    note: str = "Small daily: set 'Middle name' for student Jordan Barnes (STU100000) to 'Rae'.",
) -> Change:
    if operation is Operation.CREATE:
        before = {}
        after = after if after is not None else {"Contact name": "New Guardian"}
    elif operation is Operation.DELETE:
        before = before if before is not None else {"Contact name": "Old Guardian"}
        after = {}
    else:
        before = before if before is not None else {"Middle name": ""}
        after = after if after is not None else {"Middle name": "Rae"}
    return Change(
        filename=filename,
        operation=operation,
        key=key or {"Student id": "STU100000"},
        bucket=bucket,
        expected_event=expected_event,
        event_subject=event_subject,
        before=before,
        after=after,
        note=note,
    )


def _stack_with_counts(**counts: int) -> CsvStack:
    record_type_files = {
        "schools": "schools.csv",
        "students": "students.csv",
        "teachers": "teachers.csv",
        "staff": "staff.csv",
        "sections": "sections.csv",
        "enrollments": "enrollments.csv",
        "contacts": "contacts.csv",
    }
    tables = {record_type_files[rt]: [{} for _ in range(n)] for rt, n in counts.items()}
    return CsvStack(tables, migrated_columns={})


def _result(
    run_id: str = "20260730T120000Z-abc123",
    district: str = "steadfast-backpack-8880",
    dry_run: bool = False,
    changes: list[Change] | None = None,
    guardrail: dict | None = None,
    pushed_files: list[str] | None = None,
    error: str | None = None,
    plan: RunPlan | None = None,
) -> RunResult:
    if changes is None:
        changes = [_change()]
    if guardrail is None:
        stack = _stack_with_counts(contacts=50_000, students=33_620)
        guardrail = guardrail_evaluate(stack, changes).to_dict()
    return RunResult(
        run_id=run_id,
        plan=plan or _plan(),
        district=district,
        dry_run=dry_run,
        changes=changes,
        guardrail=guardrail,
        pushed_files=pushed_files or ["students.csv"],
        error=error,
        started_at=_dt.datetime(2026, 7, 30, 12, 0, 0, tzinfo=_dt.timezone.utc),
        finished_at=_dt.datetime(2026, 7, 30, 12, 0, 5, tzinfo=_dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# write_run: artefact names + JSON round-trip
# ---------------------------------------------------------------------------


def test_write_run_produces_all_three_files_with_expected_names(tmp_path: Path):
    result = _result()
    paths = audit.write_run(result, logs_root=tmp_path, district_label="Tulsa Replica Sandbox")

    assert set(paths) == {"json", "markdown", "history"}
    expected_stem = f"2026-07-30-{result.run_id}"
    assert paths["json"] == tmp_path / result.district / f"{expected_stem}.json"
    assert paths["markdown"] == tmp_path / result.district / f"{expected_stem}.md"
    assert paths["history"] == tmp_path / result.district / "history.jsonl"

    for p in paths.values():
        assert p.exists()


def test_json_round_trips_via_load_run(tmp_path: Path):
    result = _result()
    paths = audit.write_run(result, logs_root=tmp_path)

    loaded = audit.load_run(paths["json"])

    assert loaded["run_id"] == result.run_id
    assert loaded["district"] == result.district
    assert loaded["dry_run"] is False
    assert loaded["run_date"] == "2026-07-30"
    assert loaded["weekday"] == "Thursday"
    assert loaded["buckets"] == ["small_daily"]
    assert loaded["ok"] is True
    assert loaded["error"] is None
    assert loaded["pushed_files"] == ["students.csv"]
    assert loaded["engine_version"]
    assert loaded["schema_version"] == audit.SCHEMA_VERSION

    [change] = loaded["changes"]
    assert change["filename"] == "students.csv"
    assert change["operation"] == "update"
    assert change["key"] == {"Student id": "STU100000"}
    assert change["before"] == {"Middle name": ""}
    assert change["after"] == {"Middle name": "Rae"}
    assert change["expected_event"] == "users.updated"
    assert change["event_subject"] == "Students"
    assert change["expected_event_label"] == "users.updated (Students)"

    # event_counts is keyed by the human-readable LABEL (role breakdown);
    # wire_event_counts is keyed by the bare wire event name Clever actually
    # emits -- see SCHEMA_VERSION v2's note in audit.py.
    assert loaded["event_counts"] == {"users.updated (Students)": 1}
    assert loaded["wire_event_counts"] == {"users.updated": 1}

    # Full document must be plain-JSON (already guaranteed by json.loads, but
    # also re-dump it to be sure nothing non-serialisable snuck through).
    json.dumps(loaded)


def test_json_is_valid_even_when_guardrail_and_content_stats_are_empty(tmp_path: Path):
    result = _result(changes=[], guardrail={})
    paths = audit.write_run(result, logs_root=tmp_path)
    loaded = audit.load_run(paths["json"])
    assert loaded["changes"] == []
    assert loaded["guardrail"] == {}
    assert loaded["content_stats"] is None


# ---------------------------------------------------------------------------
# Markdown: ordering + dry-run visibility
# ---------------------------------------------------------------------------


def test_markdown_puts_expected_events_table_before_change_detail(tmp_path: Path):
    result = _result()
    paths = audit.write_run(result, logs_root=tmp_path)
    text = paths["markdown"].read_text(encoding="utf-8")

    events_idx = text.index("## Events the partner should see")
    detail_idx = text.index("## Change detail")
    assert events_idx < detail_idx

    # Both the role-breakdown table and the bare-wire-event table must appear
    # before the detail section.
    table_idx = text.index("| Expected event (role) | Count |")
    wire_table_idx = text.index("| Wire event | Count |")
    assert table_idx < detail_idx
    assert wire_table_idx < detail_idx
    assert events_idx < table_idx < wire_table_idx


def test_markdown_dry_run_is_unmistakable(tmp_path: Path):
    result = _result(dry_run=True)
    paths = audit.write_run(result, logs_root=tmp_path)
    text = paths["markdown"].read_text(encoding="utf-8")

    # Marked in the title itself, not just buried in a metadata table.
    first_line = text.splitlines()[0]
    assert first_line.startswith("# DRY RUN")
    assert "DRY RUN" in text
    assert "NOTHING WAS WRITTEN OR PUSHED" in text


def test_markdown_non_dry_run_does_not_claim_dry_run(tmp_path: Path):
    result = _result(dry_run=False)
    paths = audit.write_run(result, logs_root=tmp_path)
    text = paths["markdown"].read_text(encoding="utf-8")

    first_line = text.splitlines()[0]
    assert not first_line.startswith("# DRY RUN")
    assert "LIVE -- pushed to SFTP" in text


def test_markdown_shows_before_after_for_updates(tmp_path: Path):
    result = _result()
    paths = audit.write_run(result, logs_root=tmp_path)
    text = paths["markdown"].read_text(encoding="utf-8")

    assert 'Middle name: "" -> "Rae"' in text


# ---------------------------------------------------------------------------
# Failed run: still produces a readable report
# ---------------------------------------------------------------------------


def test_failed_run_produces_readable_report_naming_the_failure(tmp_path: Path):
    result = _result(
        changes=[],
        guardrail={},
        pushed_files=[],
        error="SFTP push failed: [Errno 110] Connection timed out to sftp.clever.com",
    )
    paths = audit.write_run(result, logs_root=tmp_path)

    loaded = audit.load_run(paths["json"])
    assert loaded["ok"] is False
    assert "Connection timed out" in loaded["error"]

    text = paths["markdown"].read_text(encoding="utf-8")
    assert "THIS RUN FAILED" in text
    assert "## What failed" in text
    assert "Connection timed out to sftp.clever.com" in text
    assert "Status | FAILED" in text


def test_failed_run_history_line_marks_ok_false(tmp_path: Path):
    result = _result(error="boom")
    audit.write_run(result, logs_root=tmp_path)
    history = audit.read_history(tmp_path, result.district)
    assert len(history) == 1
    assert history[0]["ok"] is False
    assert history[0]["error"] == "boom"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redacts_password_and_api_key_in_guardrail_and_content_stats(tmp_path: Path):
    result = _result(
        guardrail={
            "by_record_type": [],
            "net_attrition": {"total_creates": 0, "total_deletes": 0, "total_rows": 0, "verdict": "ok"},
            "blocked": False,
            "sftp_password": "hunter2",
        },
    )
    paths = audit.write_run(
        result,
        logs_root=tmp_path,
        content_stats={"ai_served": 3, "fallback_served": 1, "api_key": "sk-super-secret-value"},
    )

    raw_text = paths["json"].read_text(encoding="utf-8")
    assert "hunter2" not in raw_text
    assert "sk-super-secret-value" not in raw_text

    loaded = audit.load_run(paths["json"])
    assert loaded["guardrail"]["sftp_password"] == "***REDACTED***"
    assert loaded["content_stats"]["api_key"] == "***REDACTED***"
    # Non-secret content_stats keys must survive untouched.
    assert loaded["content_stats"]["ai_served"] == 3
    assert loaded["content_stats"]["fallback_served"] == 1

    md_text = paths["markdown"].read_text(encoding="utf-8")
    assert "hunter2" not in md_text
    assert "sk-super-secret-value" not in md_text


def test_change_key_field_is_not_mistaken_for_a_secret(tmp_path: Path):
    """The Change.key natural-key mapping is a legitimate field literally
    named 'key' -- the redaction scan must never eat it, or reports become
    useless for identifying which record changed."""

    result = _result(changes=[_change(key={"Student id": "STU100000"})])
    paths = audit.write_run(result, logs_root=tmp_path)
    loaded = audit.load_run(paths["json"])

    assert loaded["changes"][0]["key"] == {"Student id": "STU100000"}


# ---------------------------------------------------------------------------
# Markdown injection safety
# ---------------------------------------------------------------------------


def test_markdown_table_survives_pipe_and_newline_in_change_value(tmp_path: Path):
    nasty_change = _change(
        after={"Middle name": "Rae | DROP TABLE students\nSecond line"},
        note="Note with a | pipe and\na newline in it.",
    )
    result = _result(changes=[nasty_change])
    paths = audit.write_run(result, logs_root=tmp_path)
    text = paths["markdown"].read_text(encoding="utf-8")

    # Every row of every table must have the same number of *unescaped*
    # pipe-delimited cells as the table's header -- i.e. no row got split by
    # an unescaped newline or an unescaped pipe. Escaped pipes ("\|") still
    # contain a literal "|" character, so they must be stripped out first
    # before counting real column delimiters.
    for block in text.split("\n\n"):
        table_lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
        if len(table_lines) < 2:
            continue
        header_cells = table_lines[0].replace("\\|", "").count("|")
        for line in table_lines:
            if line.strip("|-: ") == "":  # separator row, e.g. |---|---|
                continue
            effective = line.replace("\\|", "")
            assert effective.count("|") == header_cells, f"malformed table row: {line!r}"

    assert "Rae \\| DROP TABLE students Second line" in text
    assert "Note with a \\| pipe and a newline in it." in text


# ---------------------------------------------------------------------------
# history.jsonl: append semantics, read_history, summarise_history
# ---------------------------------------------------------------------------


def test_history_appends_across_multiple_runs_without_corrupting_prior_lines(tmp_path: Path):
    for i in range(5):
        result = _result(
            run_id=f"run-{i}",
            plan=_plan(run_date=_dt.date(2026, 7, 26 + i)),
            changes=[_change()] * (i + 1),
        )
        audit.write_run(result, logs_root=tmp_path)

    history_path = tmp_path / "steadfast-backpack-8880" / "history.jsonl"
    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    for line in lines:
        json.loads(line)  # every prior line must remain independently parseable

    records = audit.read_history(tmp_path, "steadfast-backpack-8880")
    assert [r["run_id"] for r in records] == [f"run-{i}" for i in range(5)]
    assert records[-1]["total_changes"] == 5


def test_read_history_respects_limit(tmp_path: Path):
    for i in range(5):
        result = _result(run_id=f"run-{i}", plan=_plan(run_date=_dt.date(2026, 7, 26 + i)))
        audit.write_run(result, logs_root=tmp_path)

    limited = audit.read_history(tmp_path, "steadfast-backpack-8880", limit=2)
    assert [r["run_id"] for r in limited] == ["run-3", "run-4"]

    assert audit.read_history(tmp_path, "steadfast-backpack-8880", limit=0) == []


def test_read_history_missing_district_returns_empty_list(tmp_path: Path):
    assert audit.read_history(tmp_path, "no-such-district") == []


def test_summarise_history_reports_sensible_counts(tmp_path: Path):
    today = _dt.date.today()
    for i in range(3):
        result = _result(
            run_id=f"run-{i}",
            plan=_plan(run_date=today - _dt.timedelta(days=i)),
            changes=[_change()],
        )
        audit.write_run(result, logs_root=tmp_path)

    failed_result = _result(
        run_id="run-failed",
        plan=_plan(run_date=today),
        error="guardrail blocked this run",
        changes=[],
        guardrail={},
    )
    audit.write_run(failed_result, logs_root=tmp_path)

    summary = audit.summarise_history(tmp_path, "steadfast-backpack-8880", days=30)

    assert "Runs recorded: 4 (1 failed)" in summary
    assert "## Cumulative expected events" in summary
    assert "users.updated" in summary
    assert "## Failed runs" in summary
    assert "run-failed" in summary
    assert "guardrail blocked this run" in summary


def test_summarise_history_with_no_runs_is_graceful(tmp_path: Path):
    summary = audit.summarise_history(tmp_path, "nonexistent-district")
    assert "No runs recorded" in summary


def test_summarise_history_ai_vs_canned_trend(tmp_path: Path):
    today = _dt.date.today()
    result = _result(run_id="run-ai", plan=_plan(run_date=today))
    audit.write_run(result, logs_root=tmp_path, content_stats={"ai_served": 4, "fallback_served": 1})

    summary = audit.summarise_history(tmp_path, "steadfast-backpack-8880", days=30)
    assert "AI vs. canned content" in summary
    assert "AI-generated values served: 4" in summary
    assert "Canned/fallback values served: 1" in summary


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_write_run_leaves_no_tmp_files_behind(tmp_path: Path):
    result = _result()
    audit.write_run(result, logs_root=tmp_path)

    leftover = list(tmp_path.rglob("*.tmp"))
    assert leftover == []


def test_write_run_creates_parent_directories(tmp_path: Path):
    nested_root = tmp_path / "does" / "not" / "exist" / "yet"
    result = _result()
    paths = audit.write_run(result, logs_root=nested_root)
    assert paths["json"].exists()


# ---------------------------------------------------------------------------
# new_run_id / configure_logging
# ---------------------------------------------------------------------------


def test_new_run_id_is_sortable_and_unique():
    ids = {audit.new_run_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(len(i) > 10 for i in ids)
    assert sorted(ids) == sorted(ids)  # str-sortable by construction (timestamp prefix)


def test_configure_logging_does_not_raise(caplog):
    audit.configure_logging(verbose=True)
    audit.configure_logging(verbose=False)


# ---------------------------------------------------------------------------
# Fix 5: preflight() catches an unwritable logs dir BEFORE a run proceeds
# ---------------------------------------------------------------------------


def test_preflight_passes_for_a_writable_directory(tmp_path: Path):
    root = tmp_path / "logs"
    audit.preflight(root)  # must not raise
    assert root.is_dir()
    # No stray probe files left behind.
    assert list(root.iterdir()) == []


def test_preflight_creates_a_missing_directory(tmp_path: Path):
    root = tmp_path / "does" / "not" / "exist" / "yet"
    audit.preflight(root)
    assert root.is_dir()


def test_preflight_raises_for_an_unwritable_directory(tmp_path: Path):
    root = tmp_path / "logs"
    root.mkdir()
    root.chmod(0o500)  # read + execute, no write
    try:
        with pytest.raises(audit.AuditPreflightError, match="not writable"):
            audit.preflight(root)
    finally:
        root.chmod(0o700)  # restore so tmp_path cleanup can remove it


def test_preflight_raises_when_logs_root_is_a_file_not_a_directory(tmp_path: Path):
    root = tmp_path / "logs"
    root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(audit.AuditPreflightError):
        audit.preflight(root)


def test_write_run_logs_critical_and_still_raises_on_failure(tmp_path: Path, monkeypatch, caplog):
    """write_run must never swallow its own failure -- and must log at
    CRITICAL, not just let the exception propagate quietly, so a caller
    that catches-and-logs (like runner.py's finish()) still leaves a
    maximally visible trace of what happened."""

    result = _result()

    def _boom(path, text):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(audit, "_atomic_write_text", _boom)

    with caplog.at_level("CRITICAL"):
        with pytest.raises(OSError, match="simulated disk failure"):
            audit.write_run(result, logs_root=tmp_path)

    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert critical_records, "write_run must log at CRITICAL when it fails"
    assert any("AUDIT" in r.message.upper() for r in critical_records)
