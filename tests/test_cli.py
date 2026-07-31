"""Tests for drift_engine.cli.

Before this file, ``cli.py`` had zero test coverage (audit Fix 10) despite
being the thing an operator/scheduler actually invokes and the sole place
that maps engine outcomes to process exit codes. These tests exercise
``cli.main`` end-to-end against a small synthetic stack and a temporary
config file (never the real config/districts.yml or the real ~33k-row
sandbox stack), using the canned (non-AI, non-network) content generator so
the whole suite stays fast, hermetic, and free of any real SFTP/API access.

Covers, at minimum (see the audit's Fix 10 checklist):

* ``run --district <unknown>`` exits cleanly with a helpful message listing
  known districts, rather than a raw ``KeyError`` traceback (Fix 7);
* a ``SafetyViolation`` from ``run_once`` yields CLI exit code 2, never
  downgraded to 1 (Fix 3);
* another run already holding the district lock yields exit code 3 (Fix 4);
* an ordinary run failure (e.g. the guardrail blocking a run) yields exit
  code 1;
* a normal dry run yields exit code 0;
* cadence/"today" resolution for the ``plan`` command uses a real
  district's own configured timezone (Fix 5).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from drift_engine import cli, runner, schema
from drift_engine.runner import RunPaths

CRLF = "\r\n"

#: Passes safety.validate_fingerprint (contains ".", a recognised sandbox
#: marker, and is well over the minimum length).
FINGERPRINT = "cli-test-sandbox-replica.org"


# ---------------------------------------------------------------------------
# Config + stack fixtures (self-contained -- deliberately not shared with
# tests/test_runner.py, so each test file's fixtures can evolve independently)
# ---------------------------------------------------------------------------


def _write_config_yaml(
    path: Path,
    *,
    district_id: str = "cli-test-district",
    username: str = "cli-test-user",
    fingerprint: str = FINGERPRINT,
    timezone: str = "America/Chicago",
    enabled: bool = True,
) -> None:
    path.write_text(
        "districts:\n"
        f"  - id: {district_id}\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        f"      username: {username}\n"
        "      password_env: SFTP_PASSWORD_CLI_TEST\n"
        "      remote_dir: \"/\"\n"
        f"    data_fingerprint: \"{fingerprint}\"\n"
        f"    timezone: \"{timezone}\"\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in columns))
    path.write_text(CRLF.join(lines) + CRLF, encoding="utf-8")


def _write_baseline_stack(directory: Path, *, n_students: int = 6, n_contacts: int = 4) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    schools = [
        {
            "School id": "SCH1", "School name": "CLI Test Elementary", "School number": "1",
            "Low grade": "1", "High grade": "12", "Principal": "P One",
            "Principal email": f"p1@{FINGERPRINT}", "School address": "1 A St",
            "School city": "Testville", "School state": "OK", "School zip": "74101",
            "School phone": "9185550001",
        }
    ]
    _write_csv(directory / "schools.csv", schema.SCHOOLS.columns, schools)

    teachers = [
        {
            "School id": "SCH1", "Teacher id": "TCH1", "Teacher number": "TCH1",
            "Teacher email": f"t1@{FINGERPRINT}", "First name": "Terry", "Last name": "Teacher",
            "Title": "Teacher",
        },
        {
            "School id": "SCH1", "Teacher id": "TCH2", "Teacher number": "TCH2",
            "Teacher email": f"t2@{FINGERPRINT}", "First name": "Tara", "Last name": "Tutor",
            "Title": "Teacher",
        },
    ]
    _write_csv(directory / "teachers.csv", schema.TEACHERS.columns, teachers)

    staff = [
        {
            "School id": "SCH1", "Staff id": "STF1", "Staff email": f"s1@{FINGERPRINT}",
            "First name": "S1", "Last name": "Staff", "Department": "Office",
            "Title": "Registrar", "Role": "staff",
        }
    ]
    _write_csv(directory / "staff.csv", schema.STAFF.columns, staff)

    sections = [
        {
            "School id": "SCH1", "Section id": "SEC1", "Teacher id": "TCH1", "Name": "Homeroom",
            "Section number": "1", "Grade": "3", "Course name": "Homeroom", "Course number": "HR",
            "Subject": "Homeroom/advisory", "Term name": "Year",
        }
    ]
    _write_csv(directory / "sections.csv", schema.SECTIONS.columns, sections)

    students = [
        {
            "School id": "SCH1", "Student id": f"STU{i}", "Student number": f"STU{i}",
            "Last name": f"Last{i}", "First name": f"First{i}", "Grade": "3",
            "Gender": "F", "DOB": "01/01/2015",
            "Student email": f"first{i}.last{i}@students.{FINGERPRINT}",
        }
        for i in range(1, n_students + 1)
    ]
    _write_csv(directory / "students.csv", schema.STUDENTS.columns, students)

    enrollments = [
        {"School id": "SCH1", "Section id": "SEC1", "Student id": f"STU{i}"}
        for i in range(1, n_students + 1)
    ]
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)

    if n_contacts:
        contacts = [
            {
                "School id": "SCH1", "Student id": f"STU{((i - 1) % n_students) + 1}",
                "Contact id": f"CON{i:03d}", "Contact name": f"Guardian {i}",
                "Contact type": "Parent", "Relationship": "Mother", "Phone": "9185550000",
                "Phone type": "Mobile", "Email": f"guardian{i}@{FINGERPRINT}", "Sequence": "1",
            }
            for i in range(1, n_contacts + 1)
        ]
        _write_csv(directory / "contacts.csv", schema.CONTACTS.columns, contacts)


def _base_args(tmp_path: Path, *extra: str) -> list[str]:
    """Common top-level flags every invocation needs, pointed at tmp_path."""
    return [
        "--config", str(tmp_path / "districts.yml"),
        "--state-root", str(tmp_path / "state"),
        "--logs-root", str(tmp_path / "logs"),
        "--env-file", str(tmp_path / "does-not-exist.env"),
        *extra,
    ]


MONDAY_ISO = "2026-07-27"


# ---------------------------------------------------------------------------
# Fix 7: unknown --district exits cleanly, no raw traceback
# ---------------------------------------------------------------------------


def test_run_unknown_district_exits_cleanly_with_known_districts_listed(tmp_path):
    _write_config_yaml(tmp_path / "districts.yml", district_id="the-real-one")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(_base_args(tmp_path, "run", "--district", "totally-not-configured"))

    message = str(exc_info.value)
    assert "totally-not-configured" in message
    assert "the-real-one" in message
    assert "Known districts" in message


def test_plan_unknown_district_exits_cleanly(tmp_path):
    _write_config_yaml(tmp_path / "districts.yml", district_id="the-real-one")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(_base_args(tmp_path, "plan", "--district", "nope", "--date", MONDAY_ISO))

    assert "nope" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Exit code contract: 0 (ok), 1 (run failed), 2 (safety), 3 (lock held)
# ---------------------------------------------------------------------------


def test_run_dry_run_ok_exits_zero(tmp_path):
    _write_config_yaml(tmp_path / "districts.yml")
    _write_baseline_stack(tmp_path / "state" / "cli-test-district" / "baseline")

    code = cli.main(
        _base_args(
            tmp_path, "run",
            "--district", "cli-test-district",
            "--date", MONDAY_ISO,
            "--seed", "1",
            "--canned-content",
        )
    )
    assert code == cli.EXIT_OK


def test_run_guardrail_blocked_exits_one(tmp_path, monkeypatch):
    _write_config_yaml(tmp_path / "districts.yml")
    _write_baseline_stack(
        tmp_path / "state" / "cli-test-district" / "baseline", n_students=6, n_contacts=4
    )

    def _fake_select(stack, plan, content, *, rng):
        from drift_engine.models import Bucket, Change, EventType, Operation

        contact = stack.contacts()[0]
        return [
            Change(
                filename=schema.CONTACTS.filename,
                operation=Operation.DELETE,
                key={"Contact id": contact["Contact id"]},
                bucket=Bucket.SMALL_DAILY,
                expected_event=EventType.CONTACTS_DELETED,
                before=dict(contact),
                note="forced guardrail violation for exit-code test",
            )
        ]

    monkeypatch.setattr(runner.selection, "select_changes", _fake_select)

    code = cli.main(
        _base_args(
            tmp_path, "run",
            "--district", "cli-test-district",
            "--date", MONDAY_ISO,
            "--seed", "1",
            "--canned-content",
        )
    )
    assert code == cli.EXIT_RUN_FAILED


def test_run_safety_violation_exits_two(tmp_path):
    """A corrupt baseline_counts.json is a hard SafetyViolation (Fix 2) that
    must propagate out of run_once and yield exit code 2 at the CLI (Fix 3),
    never downgraded to the generic exit code 1."""

    _write_config_yaml(tmp_path / "districts.yml")
    _write_baseline_stack(tmp_path / "state" / "cli-test-district" / "baseline")

    paths = RunPaths(tmp_path / "state", "cli-test-district")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.baseline_counts.write_text("{not valid json", encoding="utf-8")

    code = cli.main(
        _base_args(
            tmp_path, "run",
            "--district", "cli-test-district",
            "--date", MONDAY_ISO,
            "--seed", "1",
            "--canned-content",
        )
    )
    assert code == cli.EXIT_SAFETY_VIOLATION


def test_run_lock_held_exits_three(tmp_path):
    _write_config_yaml(tmp_path / "districts.yml")
    _write_baseline_stack(tmp_path / "state" / "cli-test-district" / "baseline")

    paths = RunPaths(tmp_path / "state", "cli-test-district")
    paths.root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(paths.lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code = cli.main(
            _base_args(
                tmp_path, "run",
                "--district", "cli-test-district",
                "--date", MONDAY_ISO,
                "--seed", "1",
                "--canned-content",
            )
        )
        assert code == cli.EXIT_LOCK_HELD
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Fix 5: `plan` resolves "today" via the district's own timezone
# ---------------------------------------------------------------------------


def test_plan_with_explicit_date_ignores_timezone_and_prints_it(tmp_path):
    _write_config_yaml(tmp_path / "districts.yml", timezone="America/Chicago")

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(
            _base_args(
                tmp_path, "plan",
                "--district", "cli-test-district",
                "--date", "2026-07-31",  # a Friday
            )
        )
    assert code == cli.EXIT_OK
    output = buf.getvalue()
    assert "explicit" in output
    assert "Friday" in output
    assert "big_teacher" in output


class _FrozenDateTime(_dt.datetime):
    """A ``datetime.datetime`` whose ``now()`` always returns a fixed instant:
    2026-08-01 03:30 UTC, which is Saturday in UTC but still Friday 22:30 in
    America/Chicago. Deliberately duplicated from ``tests/test_runner.py``
    rather than imported from it -- ``tests`` is not reliably importable as a
    package under ``scripts/minipytest.py`` (see that script's own
    docstring), so each test file's fixtures stay self-contained."""

    _instant = _dt.datetime(2026, 8, 1, 3, 30, tzinfo=_dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._instant.replace(tzinfo=None)
        return cls._instant.astimezone(tz)


def test_plan_without_explicit_date_uses_district_timezone(tmp_path, monkeypatch):
    """Same Friday-evening-Central / Saturday-UTC boundary case as
    tests/test_runner.py's resolve_run_date tests, but exercised through the
    CLI's `plan` command with no --date."""

    monkeypatch.setattr(runner._dt, "datetime", _FrozenDateTime)
    _write_config_yaml(tmp_path / "districts.yml", timezone="America/Chicago")

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(
            _base_args(tmp_path, "plan", "--district", "cli-test-district")
        )
    assert code == cli.EXIT_OK
    output = buf.getvalue()
    assert "America/Chicago" in output
    assert "Friday" in output
    assert "Saturday" not in output.split("\n")[1]  # the resolved-date line, not incidental text
