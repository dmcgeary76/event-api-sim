"""Tests for drift_engine.runner.

Before this file, ``runner.py`` -- the orchestrator that decides
save-vs-don't, push-vs-don't, dry-run-dir-vs-current, and
guardrail-before-write -- had zero test coverage (audit Fix 10). These tests
use a small synthetic CsvStack written into ``tmp_path`` (never the real
~33k-row sandbox stack, which is far too slow for a unit test) and a canned
(non-AI, non-network) content generator, so the whole suite stays fast and
hermetic.

Covers, at minimum (see the audit's Fix 10 checklist):

* a dry run does not modify ``state/<d>/current/`` and never reaches the
  real SFTP push;
* a ``GuardrailViolation`` leaves ``current/`` byte-identical;
* the scale-sanity gate runs even when ``baseline_counts.json`` is
  unreadable or missing-after-a-prior-push -- and now HARD FAILS instead of
  warning-and-skipping (Fix 2);
* a ``SafetyViolation`` propagates out of ``run_once`` rather than being
  caught and downgraded (Fix 3);
* a second concurrent run cannot acquire the per-district lock and raises
  ``RunLockHeld`` (Fix 4);
* cadence date resolution uses the district's own configured timezone, not
  UTC/host-local time (Fix 5);
* dry-run output directories are pruned to the retention limit (Fix 6);
* a weak ``data_fingerprint`` like ``"@"`` is rejected (Fix 1) -- exhaustive
  coverage lives in ``tests/test_config.py``; this is a light
  cross-reference confirming the same rule applies to a district built the
  way these tests build one;
* a full seeding run -- which multiplies students.csv ROWS, because contacts
  are rows on that file -- does NOT trip the scale-sanity gate, because
  ``CsvStack.counts()`` reports ``students`` as a distinct Student id count.

CONTACTS ARE ROWS ON students.csv HERE TOO
------------------------------------------
There is no contacts.csv in these fixtures, because there is no such file in
Clever's SFTP spec (see the ``schema`` module docstring / SFTP Instructions
v2.1.1). ``_write_baseline_stack``'s ``n_contacts`` therefore controls how
many of the synthetic students' rows carry a populated contact half, not how
many rows a seventh file gets -- and a student with no guardian still occupies
exactly one row, with the seven contact columns blank.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
from pathlib import Path

import pytest

from drift_engine import config as config_mod
from drift_engine import runner, safety, schema
from drift_engine.config import DistrictConfig, EngineConfig, SftpConfig
from drift_engine.csvstack import CsvStack
from drift_engine.models import Bucket, Change, EventSubject, EventType, Operation, SafetyViolation
from drift_engine.runner import RunLockHeld, RunPaths, run_once

#: Passes safety.validate_fingerprint: contains ".", a recognised sandbox
#: marker ("sandbox"/"replica"), and is well over the minimum length.
FINGERPRINT = "sandbox-test-replica.org"

CRLF = "\r\n"


# ---------------------------------------------------------------------------
# Synthetic stack + config helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in columns))
    path.write_text(CRLF.join(lines) + CRLF, encoding="utf-8")


def _student_fields(index: int) -> dict[str, str]:
    """The student-level half of one students.csv row.

    Every row sharing a Student id must carry identical student-level columns
    (``schema.STUDENT_LEVEL_COLUMNS``), so this is built once per student and
    repeated across that student's contact rows by
    ``schema.expand_contact_rows``.
    """

    sid = f"STU{index}"
    return {
        "School id": "SCH1", "Student id": sid, "Student number": sid,
        "Last name": f"Last{index}", "First name": f"First{index}", "Grade": "3",
        "Gender": "F", "DOB": "01/01/2015",
        "Student email": f"first{index}.last{index}@students.{FINGERPRINT}",
    }


def _contact_fields(n: int) -> dict[str, str]:
    """The contact half of one students.csv row, for guardian number ``n``.

    ``Contact sis id`` is always minted, and deliberately so: it is what
    ``schema.row_carries_contact`` keys on, so a row with contact values but no
    sis id is invisible to ``CsvStack.contacts()`` and to the derived
    ``contacts`` count the guardrail divides by. A fixture that left it blank
    would silently produce a stack with zero contacts.
    """

    return {
        "Contact relationship": "Mother",
        "Contact type": "Parent",
        "Contact name": f"Guardian {n}",
        "Contact phone": "9185550000",
        "Contact phone type": "Mobile",
        "Contact email": f"guardian{n}@{FINGERPRINT}",
        "Contact sis id": f"CON{n:03d}",
    }


def _write_baseline_stack(directory: Path, *, n_students: int = 6, n_contacts: int = 4) -> None:
    """A small, valid stack: 1 school, 2 teachers, N students, N contacts.

    SIX files, not seven: contacts have no file of their own, so ``n_contacts``
    guardians are dealt round-robin across the N students and become the
    contact half of rows on students.csv (SFTP Instructions v2.1.1 -- see the
    ``schema`` module docstring). With the defaults that means students.csv has
    6 rows, 4 of which carry a guardian and 2 of which have the seven contact
    columns blank, and ``CsvStack.counts()`` reports ``students=6``,
    ``contacts=4``.

    Email columns embed FINGERPRINT so ``safety.assert_fingerprint_present``
    passes; two teachers in the same school so big-teacher-bucket selection
    (co-teacher/reassignment) has a candidate to pick.
    """

    directory.mkdir(parents=True, exist_ok=True)

    schools = [
        {
            "School id": "SCH1", "School name": "Test Elementary", "School number": "1",
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

    # Deal the guardians round-robin, then render each student through
    # ``schema.expand_contact_rows`` -- the one function that encodes the
    # row-per-contact pattern -- rather than reimplementing it here. That keeps
    # this fixture correct-by-construction on the two rules that matter: a
    # student with no guardian still gets exactly one row (dropping it would
    # delete the student), and a student with N guardians gets N rows whose
    # student-level columns agree.
    contacts_by_student: dict[str, list[dict[str, str]]] = {}
    for i in range(1, n_contacts + 1):
        sid = f"STU{((i - 1) % n_students) + 1}"
        contacts_by_student.setdefault(sid, []).append(_contact_fields(i))

    student_rows: list[dict[str, str]] = []
    for i in range(1, n_students + 1):
        student = _student_fields(i)
        student_rows.extend(
            schema.expand_contact_rows(
                student, contacts_by_student.get(student["Student id"], [])
            )
        )
    _write_csv(directory / "students.csv", schema.STUDENTS.columns, student_rows)

    enrollments = [
        {"School id": "SCH1", "Section id": "SEC1", "Student id": f"STU{i}"}
        for i in range(1, n_students + 1)
    ]
    _write_csv(directory / "enrollments.csv", schema.ENROLLMENTS.columns, enrollments)


def _make_district(
    *,
    district_id: str = "test-sandbox",
    username: str = "test-sandbox-user",
    fingerprint: str = FINGERPRINT,
    timezone: str = "America/Chicago",
    eventing_verified: bool = True,
) -> DistrictConfig:
    return DistrictConfig(
        id=district_id,
        label="Test Sandbox",
        enabled=True,
        sftp=SftpConfig(
            host="sftp.clever.com",
            port=22,
            username=username,
            password_env="SFTP_PASSWORD_TEST_SANDBOX",
            remote_dir="/",
        ),
        data_fingerprint=fingerprint,
        eventing_verified=eventing_verified,
        timezone=timezone,
    )


def _make_cfg(*districts: DistrictConfig) -> EngineConfig:
    return EngineConfig(districts=tuple(districts))


MONDAY = _dt.date(2026, 7, 27)  # small_daily only -- see docstring below for why this date
FRIDAY = _dt.date(2026, 7, 31)  # small_daily + big_teacher


# ---------------------------------------------------------------------------
# Fix 10 checklist item: dry run never touches current/, never reaches push
# ---------------------------------------------------------------------------


def test_dry_run_does_not_modify_current_and_never_pushes_for_real(tmp_path, monkeypatch):
    from drift_engine import sftp_push

    state_root = tmp_path / "state"
    district = _make_district()
    baseline_dir = state_root / district.id / "baseline"
    _write_baseline_stack(baseline_dir)
    cfg = _make_cfg(district)

    def _boom(*args, **kwargs):
        raise AssertionError("a dry run must never reach the real SFTP push")

    monkeypatch.setattr(sftp_push, "_real_push", _boom)

    result = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=True,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )

    assert result.ok, result.error
    assert result.dry_run is True

    current_dir = state_root / district.id / "current"
    for f in sorted(baseline_dir.glob("*.csv")):
        assert (current_dir / f.name).read_bytes() == f.read_bytes(), (
            f"{f.name} in current/ changed as a result of a dry run"
        )

    dry_run_root = state_root / district.id / "dry-run"
    dirs = list(dry_run_root.iterdir())
    assert len(dirs) == 1
    assert (dirs[0] / "students.csv").exists()


def test_genuine_first_run_writes_baseline_counts_but_no_push_marker_on_dry_run(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    result = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=True,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )
    assert result.ok, result.error

    paths = RunPaths(state_root, district.id)
    assert paths.baseline_counts.exists(), "genuine first run must record the scale-sanity baseline"
    assert not paths.has_prior_successful_push(), "a dry run must never count as a real push"


# ---------------------------------------------------------------------------
# Fix 10 checklist item: GuardrailViolation leaves current/ byte-identical
# ---------------------------------------------------------------------------


def _guardrail_violating_changes(stack) -> list[Change]:
    """One DELETE that blows past Clever's 10% pause threshold for contacts.

    A guardian removal is a students.csv row DELETE now, keyed on
    (Student id, Contact sis id). It still has to be *attributed* to the
    ``contacts`` record type rather than ``students`` -- that is what
    ``event_subject=EventSubject.CONTACT`` buys (see
    ``guardrail._attributed_record_type``), and it is why one removal out of
    the fixture's four contacts reads as 25% of contacts rather than 1/6th of
    students. 25% is past Clever's 10% pause-for-review threshold, so the
    guardrail must block.
    """

    contact = stack.contacts()[0]
    return [
        Change(
            filename=schema.STUDENTS.filename,
            operation=Operation.DELETE,
            key={
                "Student id": contact["Student id"],
                schema.CONTACT_SIS_ID_COLUMN: contact[schema.CONTACT_SIS_ID_COLUMN],
            },
            bucket=Bucket.SMALL_DAILY,
            expected_event=EventType.USERS_DELETED,
            event_subject=EventSubject.CONTACT,
            before=schema.contact_fields(contact),
            note="test-induced guardrail violation",
        )
    ]


def test_guardrail_violation_leaves_current_byte_identical(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    district = _make_district()
    baseline_dir = state_root / district.id / "baseline"
    _write_baseline_stack(baseline_dir, n_students=6, n_contacts=4)  # 1 delete / 4 = 25% > 10%
    cfg = _make_cfg(district)

    monkeypatch.setattr(
        runner.selection,
        "select_changes",
        lambda stack, plan, content, *, rng: _guardrail_violating_changes(stack),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("push must never be reached once the guardrail has blocked a run")

    monkeypatch.setattr(runner.sftp_push, "push", _boom)

    result = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=False,  # even a LIVE attempt must not touch current/ once blocked
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )

    assert not result.ok
    assert "guardrail blocked" in (result.error or "")

    current_dir = state_root / district.id / "current"
    for f in sorted(baseline_dir.glob("*.csv")):
        assert (current_dir / f.name).read_bytes() == f.read_bytes(), (
            f"{f.name} in current/ was modified despite the guardrail blocking this run"
        )


# ---------------------------------------------------------------------------
# Fix 2 / Fix 3: scale-sanity gate hard-fails (and is auditable + re-raised)
# ---------------------------------------------------------------------------


def test_corrupt_baseline_counts_is_a_hard_safety_violation_and_is_audited(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    paths = RunPaths(state_root, district.id)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.baseline_counts.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SafetyViolation):
        run_once(
            cfg=cfg,
            district=district,
            run_date=MONDAY,
            dry_run=True,
            state_root=state_root,
            logs_root=tmp_path / "logs",
            seed_value=1,
            canned_only=True,
        )

    # Fix 3: a safety failure must still be auditable, not just raised.
    reports = list((tmp_path / "logs" / district.id).glob("*.json"))
    assert reports, "a SafetyViolation must still produce a written audit record"
    record = json.loads(reports[0].read_text(encoding="utf-8"))
    assert record["ok"] is False
    assert "safety" in (record["error"] or "")


def test_missing_baseline_after_a_prior_push_is_a_hard_safety_violation(tmp_path):
    """A missing baseline_counts.json is only acceptable on a district's
    genuine first-ever run. If a prior successful push is on record but the
    file is gone (deleted/corrupted), that must hard-fail, never silently
    re-anchor to the current stack."""

    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    paths = RunPaths(state_root, district.id)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.record_successful_push(run_id="prior-run", run_date=_dt.date(2026, 7, 20))
    assert not paths.baseline_counts.exists()

    with pytest.raises(SafetyViolation, match="missing"):
        run_once(
            cfg=cfg,
            district=district,
            run_date=MONDAY,
            dry_run=True,
            state_root=state_root,
            logs_root=tmp_path / "logs",
            seed_value=1,
            canned_only=True,
        )


def test_scale_sanity_blocks_a_run_whose_stack_size_diverges_from_baseline(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline", n_students=6, n_contacts=4)
    cfg = _make_cfg(district)

    paths = RunPaths(state_root, district.id)
    paths.root.mkdir(parents=True, exist_ok=True)
    # A baseline wildly different from the actual (tiny, synthetic) stack --
    # this must be treated as "wrong target", not "a normal day's drift".
    paths.baseline_counts.write_text(
        json.dumps({"students": 6000, "contacts": 4000}), encoding="utf-8"
    )

    with pytest.raises(SafetyViolation):
        run_once(
            cfg=cfg,
            district=district,
            run_date=MONDAY,
            dry_run=True,
            state_root=state_root,
            logs_root=tmp_path / "logs",
            seed_value=1,
            canned_only=True,
        )


def test_seeding_contacts_does_not_trip_the_scale_sanity_gate(tmp_path, monkeypatch):
    """The other side of the coin from the test above: a legitimate seeding
    run must NOT be mistaken for "wrong target", even though it multiplies
    students.csv ROWS by roughly 1.5x.

    This is the entire reason ``CsvStack.counts()`` reports ``students`` as a
    DISTINCT Student id count rather than a raw row count. Contacts are rows on
    students.csv, so seeding the real stack takes that file from 33,621 rows to
    ~52,931 -- a +57% move. Reported as rows, that blows straight through
    ``safety.MAX_SCALE_DRIFT`` (25%), and because a stale baseline is a hard
    ``SafetyViolation`` rather than a silent re-anchor (see the two tests
    above), the district would be BRICKED mid-seed until someone re-baselined
    by hand. Nothing in the seeding path would look wrong; the next run would
    simply refuse to start.

    Exercised end to end rather than as a unit test of ``counts()``, because
    the failure mode is a *sequence*: run 1 anchors baseline_counts.json from
    the pristine pre-seed stack and persists the fanned-out stack to
    ``current/``; run 2 then has to load that much larger file and still get
    through ``assert_safe_target``'s scale gate against run 1's baseline.
    """

    state_root = tmp_path / "state"
    district = _make_district()
    baseline_dir = state_root / district.id / "baseline"
    # No guardians at all, which is exactly the shape of David's real sandbox
    # export (see the seed module docstring) -- so every student is eligible
    # for seeding and every row seeding adds is growth this gate has to
    # tolerate. 40 students is enough that the ~70% row growth is not a
    # coin-flip away from the 25% tolerance.
    _write_baseline_stack(baseline_dir, n_students=40, n_contacts=0)
    cfg = _make_cfg(district)

    pre_seed = CsvStack.load(baseline_dir)
    pre_seed_counts = pre_seed.counts()
    pre_seed_rows = len(pre_seed.students())
    assert pre_seed_counts["students"] == 40
    assert pre_seed_counts["contacts"] == 0
    assert pre_seed_rows == 40, "a contact-less student still occupies exactly one row"

    # A LIVE run, because only a real (non-dry) run persists to current/ for
    # the follow-up run below to load. The transport is faked out for the same
    # reason as test_successful_live_push_records_marker_for_future_runs:
    # paramiko is not installable here, and this test is about runner.py's own
    # scale accounting, not SFTP.
    monkeypatch.setattr(
        runner.sftp_push,
        "push",
        lambda local_dir, district, *, dry_run, stack, allowlist: ["students.csv"],
    )

    seeded = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=False,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
        seed_contacts_limit=40,  # seed every student in one pass
    )
    assert seeded.ok, seeded.error
    assert seeded.changes, "seeding produced no changes, so this test proves nothing"

    paths = RunPaths(state_root, district.id)
    recorded = json.loads(paths.baseline_counts.read_text(encoding="utf-8"))
    assert recorded["students"] == 40, (
        "baseline_counts.json must anchor on distinct students, not students.csv rows"
    )

    post = CsvStack.load(paths.current)
    post_counts = post.counts()
    post_rows = len(post.students())

    # The whole point, stated twice: rows grew a lot, the students count did
    # not move at all.
    assert post_rows > pre_seed_rows, "seeding should have added students.csv rows"
    assert post_counts["students"] == pre_seed_counts["students"] == 40
    assert post_counts["contacts"] == post_rows, (
        "every student was seeded, so every row should now carry a guardian"
    )

    safety.assert_scale_sane(post_counts, recorded, district_id=district.id)

    # Sanity: proves this is a real gate rather than a tautology. Had
    # ``counts()`` reported students as a raw ROW count -- the naive/buggy
    # behaviour -- this same seeding pass would have moved it well past
    # MAX_SCALE_DRIFT and hard-failed the very next run.
    naive_row_drift = abs(post_rows - pre_seed_rows) / pre_seed_rows
    assert naive_row_drift > safety.MAX_SCALE_DRIFT, (
        f"row growth of {naive_row_drift:.0%} is inside the "
        f"{safety.MAX_SCALE_DRIFT:.0%} tolerance, so a raw row count would have "
        "passed too and this test would not be checking anything"
    )
    with pytest.raises(SafetyViolation):
        safety.assert_scale_sane(
            {**post_counts, "students": post_rows},
            {**recorded, "students": pre_seed_rows},
            district_id=district.id,
        )

    # ...and end to end: the next run has to load the fanned-out stack and get
    # through the gate inside run_once itself, not just via a direct call to
    # assert_scale_sane. A SafetyViolation here would propagate, not be
    # downgraded to result.ok == False, so this asserts on both.
    followup = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=True,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=2,
        canned_only=True,
    )
    assert followup.ok, followup.error


def test_successful_live_push_records_marker_for_future_runs(tmp_path, monkeypatch):
    """Confirms the OTHER half of the first-run/scale-sanity contract: once
    a real push succeeds, ``has_prior_successful_push`` becomes true, so a
    LATER missing baseline_counts.json is no longer excusable as "first
    run". ``sftp_push.push`` itself is faked out here -- paramiko is not
    installable in this sandbox, and this test is about runner.py's own
    bookkeeping, not the SFTP transport."""

    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    monkeypatch.setattr(
        runner.sftp_push,
        "push",
        lambda local_dir, district, *, dry_run, stack, allowlist: ["students.csv", "sections.csv"],
    )

    result = run_once(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=False,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )
    assert result.ok, result.error

    paths = RunPaths(state_root, district.id)
    assert paths.has_prior_successful_push()


# ---------------------------------------------------------------------------
# Fix 4: per-district locking
# ---------------------------------------------------------------------------


def test_second_concurrent_run_cannot_acquire_the_lock(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    paths = RunPaths(state_root, district.id)
    paths.root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(paths.lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RunLockHeld):
            run_once(
                cfg=cfg,
                district=district,
                run_date=MONDAY,
                dry_run=True,
                state_root=state_root,
                logs_root=tmp_path / "logs",
                seed_value=1,
                canned_only=True,
            )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_lock_is_released_after_a_run_so_a_later_run_can_proceed(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    kwargs = dict(
        cfg=cfg,
        district=district,
        run_date=MONDAY,
        dry_run=True,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )
    first = run_once(**kwargs)
    assert first.ok, first.error

    # Sequential, not concurrent -- the lock must not still be held once the
    # first call has returned.
    second = run_once(**kwargs)
    assert second.ok, second.error


# ---------------------------------------------------------------------------
# Fix 5: timezone-aware cadence date resolution
# ---------------------------------------------------------------------------


class _FrozenDateTime(_dt.datetime):
    """A ``datetime.datetime`` whose ``now()`` always returns a fixed instant.

    Used to prove ``resolve_run_date`` converts to the DISTRICT's own local
    time rather than using UTC or host-local time. The fixed instant below is
    2026-08-01 03:30 UTC, which is a Saturday in UTC but still Friday
    22:30 in America/Chicago (UTC-5 in late July/August, daylight time) --
    exactly the boundary case the audit called out.
    """

    _instant = _dt.datetime(2026, 8, 1, 3, 30, tzinfo=_dt.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._instant.replace(tzinfo=None)
        return cls._instant.astimezone(tz)


def test_resolve_run_date_uses_district_timezone_not_utc(monkeypatch):
    monkeypatch.setattr(runner._dt, "datetime", _FrozenDateTime)
    district = _make_district(timezone="America/Chicago")

    resolved_date, tz_used = runner.resolve_run_date(district, None)

    assert tz_used == "America/Chicago"
    assert resolved_date == FRIDAY
    assert resolved_date.strftime("%A") == "Friday"

    # Sanity: proves this is a real fix, not a no-op. The naive/buggy
    # behaviour (host-local `date.today()` on a UTC host, or a bare
    # `datetime.now(timezone.utc).date()`) would have landed on Saturday --
    # silently skipping the entire Friday big-teacher bucket.
    naive_utc_date = _FrozenDateTime._instant.date()
    assert naive_utc_date == _dt.date(2026, 8, 1)
    assert naive_utc_date.strftime("%A") == "Saturday"
    assert naive_utc_date != resolved_date

    plan = runner.cadence.plan_for(resolved_date)
    assert not plan.skipped
    assert Bucket.BIG_TEACHER in plan.buckets


def test_resolve_run_date_prefers_explicit_date_over_timezone(monkeypatch):
    monkeypatch.setattr(runner._dt, "datetime", _FrozenDateTime)
    district = _make_district(timezone="America/Chicago")

    resolved_date, label = runner.resolve_run_date(district, MONDAY)
    assert resolved_date == MONDAY
    assert "explicit" in label


def test_resolve_run_date_falls_back_to_utc_on_bad_timezone_name(caplog):
    district = _make_district(timezone="Not/A_Real_Zone")
    with caplog.at_level("ERROR"):
        resolved_date, label = runner.resolve_run_date(district, None)

    assert "UTC" in label
    assert any("Could not resolve timezone" in r.message for r in caplog.records)
    assert resolved_date == _dt.datetime.now(_dt.timezone.utc).date()


def test_run_once_resolves_cadence_via_district_timezone_end_to_end(tmp_path, monkeypatch):
    """The Friday-evening-Central / Saturday-UTC scenario, exercised through
    the full ``run_once`` flow (not just the helper function in isolation):
    the run must still be planned as Friday, with the big-teacher bucket,
    not silently skipped as a weekend."""

    monkeypatch.setattr(runner._dt, "datetime", _FrozenDateTime)

    state_root = tmp_path / "state"
    district = _make_district(timezone="America/Chicago")
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    result = run_once(
        cfg=cfg,
        district=district,
        run_date=None,  # force real resolution via the frozen "now"
        dry_run=True,
        state_root=state_root,
        logs_root=tmp_path / "logs",
        seed_value=1,
        canned_only=True,
    )

    assert result.ok, result.error
    assert result.plan.run_date == FRIDAY
    assert not result.plan.skipped
    assert Bucket.BIG_TEACHER in result.plan.buckets


# ---------------------------------------------------------------------------
# Fix 6: dry-run output retention
# ---------------------------------------------------------------------------


def test_dry_run_output_is_pruned_to_the_retention_limit(tmp_path):
    state_root = tmp_path / "state"
    district = _make_district()
    _write_baseline_stack(state_root / district.id / "baseline")
    cfg = _make_cfg(district)

    total_runs = runner.DRY_RUN_RETENTION + 3
    for i in range(total_runs):
        result = run_once(
            cfg=cfg,
            district=district,
            run_date=MONDAY,
            dry_run=True,
            state_root=state_root,
            logs_root=tmp_path / "logs",
            seed_value=i,
            canned_only=True,
        )
        assert result.ok, result.error

    dry_run_root = state_root / district.id / "dry-run"
    remaining = sorted(p.name for p in dry_run_root.iterdir())
    assert len(remaining) == runner.DRY_RUN_RETENTION


# ---------------------------------------------------------------------------
# Fix 1: weak data_fingerprint rejected (light cross-reference -- see
# tests/test_config.py for exhaustive coverage of safety.validate_fingerprint)
# ---------------------------------------------------------------------------


def test_weak_data_fingerprint_is_rejected_at_config_load(tmp_path):
    text = (
        "districts:\n"
        "  - id: weak-fingerprint-district\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: weak-fingerprint-user\n"
        "      password_env: SFTP_PASSWORD_WEAK\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"@\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="data_fingerprint"):
        config_mod.load_config(path)
