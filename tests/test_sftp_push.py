"""Tests for drift_engine.sftp_push.

No real network is ever touched here. dry_run's "no connection attempted"
guarantee is checked by monkeypatching ``_real_push`` (the only function in
the module that imports paramiko / opens a socket) to fail loudly if it is
ever called. The safety-gate tests confirm ``SafetyViolation`` fires for a
non-allowlisted username, a wrong host, and a missing/absent fingerprint --
and that it is never swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drift_engine import schema, sftp_push
from drift_engine.config import DistrictConfig, SftpConfig
from drift_engine.csvstack import CsvStack
from drift_engine.models import SafetyViolation

FINGERPRINT = "tulsaschools-replica.org"


def _district(
    *,
    district_id: str = "steadfast-backpack-8880",
    host: str = "sftp.clever.com",
    username: str = "steadfast-backpack-8880",
    fingerprint: str = FINGERPRINT,
) -> DistrictConfig:
    return DistrictConfig(
        id=district_id,
        label="Test District",
        enabled=True,
        sftp=SftpConfig(
            host=host,
            port=22,
            username=username,
            password_env="SFTP_PASSWORD_TEST_DISTRICT",
            remote_dir="/",
        ),
        data_fingerprint=fingerprint,
        eventing_verified=True,
    )


def _stack_with_fingerprint(fingerprint_value: str | None) -> CsvStack:
    """A CsvStack whose fingerprint_sample() contains (or omits) a value."""

    students = [
        {
            "School id": "SCH1",
            "Student id": "STU1",
            "Student number": "STU1",
            "Last name": "Barnes",
            "First name": "Jordan",
            "Middle name": "",
            "Grade": "5",
            "Gender": "F",
            "DOB": "01/01/2015",
            "Student email": f"jordan.barnes@{fingerprint_value}" if fingerprint_value else "",
        }
    ]
    return CsvStack({"students.csv": students}, migrated_columns={})


#: Every schema file, in the order push() expects to find them -- Fix 6:
#: push() asserts every one of these is present in local_dir before it will
#: describe or upload anything. As of 2026-08-05 that means EVERY file in
#: ``schema.ALL_SPECS``, with no exceptions: contacts stopped being a
#: standalone engine-owned contacts.csv (they are rows on students.csv now), so
#: there is no longer any engine-owned file whose absence is tolerated.
_CORE_FILENAMES = tuple(spec.filename for spec in schema.ALL_SPECS)


def _write_stack_files(
    tmp_path: Path, *, with_stale_contacts: bool = False, enrollment_rows: int = 0
) -> Path:
    """Write a minimal, but COMPLETE, local push directory.

    ``students.csv`` gets one real data row (used by several tests to check
    size/row reporting); every other file gets a header-only placeholder, since
    push()'s completeness gate (Fix 6) only checks existence, not content.

    ``with_stale_contacts`` plants a leftover ``contacts.csv`` of the kind the
    pre-2026-08-05 version of this engine wrote. It is NOT part of
    ``schema.ALL_SPECS`` any more, so nothing should describe, count, or upload
    it -- planting it is how the tests prove that.

    ``enrollment_rows`` adds that many real data rows to enrollments.csv, for
    the tests that need an on-disk row count the in-memory stack does not share.
    """

    local_dir = tmp_path / "stack"
    local_dir.mkdir()
    (local_dir / "students.csv").write_text(
        "School id,Student id\r\nSCH1,STU1\r\n", encoding="utf-8"
    )
    for spec in schema.ALL_SPECS:
        if spec.filename == "students.csv":
            continue
        (local_dir / spec.filename).write_text(
            ",".join(spec.columns) + "\r\n", encoding="utf-8"
        )
    if enrollment_rows:
        (local_dir / "enrollments.csv").write_text(
            ",".join(schema.ENROLLMENTS.columns)
            + "\r\n"
            + "".join(f"SCH1,SEC1,STU{i + 1}\r\n" for i in range(enrollment_rows)),
            encoding="utf-8",
        )
    if with_stale_contacts:
        (local_dir / "contacts.csv").write_text(
            "School id,Student id,Contact id\r\nSCH1,STU1,CON1\r\n", encoding="utf-8"
        )
    return local_dir


# ---------------------------------------------------------------------------
# dry_run performs NO connection
# ---------------------------------------------------------------------------


def test_dry_run_never_calls_real_push(tmp_path: Path, monkeypatch):
    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    def _boom(*args, **kwargs):
        raise AssertionError("dry_run must never open a real connection")

    monkeypatch.setattr(sftp_push, "_real_push", _boom)

    result = sftp_push.push(
        local_dir,
        district,
        dry_run=True,
        stack=stack,
        allowlist={district.sftp.username},
    )

    assert set(result) == set(_CORE_FILENAMES)


def test_dry_run_returns_would_be_file_list_and_logs(tmp_path: Path, caplog):
    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    with caplog.at_level("INFO"):
        result = sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )

    assert sorted(result) == sorted(_CORE_FILENAMES)
    log_text = "\n".join(r.message for r in caplog.records)
    assert "DRY RUN" in log_text
    assert "students.csv" in log_text
    assert "sections.csv" in log_text


def test_dry_run_does_not_import_paramiko(tmp_path: Path, monkeypatch):
    """Even if paramiko is entirely absent, dry_run must succeed."""

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "paramiko" or name.startswith("paramiko."):
            raise ImportError("paramiko is not installed in this sandbox")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    result = sftp_push.push(
        local_dir,
        district,
        dry_run=True,
        stack=stack,
        allowlist={district.sftp.username},
    )
    assert result


# ---------------------------------------------------------------------------
# Safety gate: never bypassed, never swallowed
# ---------------------------------------------------------------------------


def test_non_allowlisted_username_raises_safety_violation(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district(username="not-a-real-sandbox-user")
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(SafetyViolation, match="not-a-real-sandbox-user"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={"some-other-user"},
        )


def test_wrong_host_raises_safety_violation(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district(host="sftp.some-other-host.example.com")
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(SafetyViolation, match="not in the permitted host set"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_absent_fingerprint_in_stack_raises_safety_violation(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district()
    # Stack data does not contain the configured fingerprint anywhere.
    stack = _stack_with_fingerprint("some-unrelated-domain.org")

    with pytest.raises(SafetyViolation, match="does not contain the expected"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_district_with_no_fingerprint_configured_raises_safety_violation(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district(fingerprint="")
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(SafetyViolation, match="no data_fingerprint configured"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_empty_allowlist_raises_safety_violation_rather_than_assuming_safe(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(SafetyViolation, match="allowlist is empty"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist=[],
        )


def test_safety_violation_is_never_swallowed_even_when_push_raises_in_caller(tmp_path: Path):
    """A caller wrapping push() in a broad except must still see SafetyViolation
    propagate -- confirm it is a real exception, not silently downgraded to a
    return value or a logged warning."""

    local_dir = _write_stack_files(tmp_path)
    district = _district(username="definitely-not-allowlisted")
    stack = _stack_with_fingerprint(FINGERPRINT)

    caught = None
    try:
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={"someone-else"},
        )
    except SafetyViolation as exc:
        caught = exc

    assert caught is not None, "SafetyViolation must propagate, not be swallowed"


def test_safety_gate_runs_before_real_push_is_ever_reached(tmp_path: Path, monkeypatch):
    """Even on a real (non-dry-run) push, an unsafe target must never reach
    _real_push (i.e. never reach paramiko / the network)."""

    local_dir = _write_stack_files(tmp_path)
    district = _district(username="not-allowlisted-at-all")
    stack = _stack_with_fingerprint(FINGERPRINT)

    def _boom(*args, **kwargs):
        raise AssertionError("_real_push must never be reached for an unsafe target")

    monkeypatch.setattr(sftp_push, "_real_push", _boom)

    with pytest.raises(SafetyViolation):
        sftp_push.push(
            local_dir,
            district,
            dry_run=False,
            stack=stack,
            allowlist={"someone-else"},
        )


# ---------------------------------------------------------------------------
# Fix 7: allowlist is required -- push() must never silently guess a config
# ---------------------------------------------------------------------------


def test_missing_allowlist_fails_loudly_instead_of_silently_loading_a_config(tmp_path: Path):
    """Before this fix, omitting ``allowlist`` made push() silently call
    ``config.load_config()`` with NO path -- i.e. the default config
    location, ignoring whatever ``--config`` override the caller's own
    ``EngineConfig`` came from. A run against a non-default config would
    then be allowlist-checked against the wrong file, with no error at all.
    ``allowlist`` is now a required keyword-only argument, so omitting it
    fails immediately and loudly (a clear ``TypeError``) instead of
    guessing which config was meant."""

    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(TypeError, match="allowlist"):
        sftp_push.push(local_dir, district, dry_run=True, stack=stack)  # type: ignore[call-arg]


def test_allowlist_from_the_callers_own_config_is_what_gets_checked(tmp_path: Path):
    """The replacement for the old default-loading behaviour: a caller that
    loaded a district from some ``EngineConfig`` must pass THAT config's own
    allowlist -- exercised here against the real project config/district,
    same as the old default-loading test did, just explicit now."""

    from drift_engine.config import load_config

    local_dir = _write_stack_files(tmp_path)
    district = _district()  # steadfast-backpack-8880, matches config/districts.yml
    stack = _stack_with_fingerprint(FINGERPRINT)

    cfg = load_config()
    result = sftp_push.push(
        local_dir, district, dry_run=True, stack=stack, allowlist=cfg.allowlist()
    )
    assert result


# ---------------------------------------------------------------------------
# Fix 6: push() refuses a partial local stack, for both dry-run and real push
# ---------------------------------------------------------------------------


def test_missing_core_file_raises_before_describing_or_uploading_anything(tmp_path: Path):
    """Reproduction: deleting students.csv from the push dir used to make
    push() return the remaining files and report success -- a partial
    upload reads to Clever exactly like mass deletion."""

    local_dir = _write_stack_files(tmp_path)
    (local_dir / "students.csv").unlink()
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    with pytest.raises(sftp_push.IncompleteStackError, match="students.csv"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_missing_core_file_raises_on_real_push_too_before_any_upload(tmp_path: Path, monkeypatch):
    local_dir = _write_stack_files(tmp_path)
    (local_dir / "sections.csv").unlink()
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    def _boom(*args, **kwargs):
        raise AssertionError("a partial stack must never reach _real_push")

    monkeypatch.setattr(sftp_push, "_real_push", _boom)

    with pytest.raises(sftp_push.IncompleteStackError, match="sections.csv"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=False,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_absent_file_is_flagged_even_when_the_stack_has_zero_rows_for_it(tmp_path: Path):
    """A file with nothing in it must still EXIST on disk, header row and all.

    Retargeted 2026-08-05, and deliberately the inverse of what this test
    asserted before. It used to cover the one tolerated exception: an absent
    contacts.csv was fine when the in-memory stack agreed it had zero rows,
    because that file was engine-owned and only written once it had content.
    Contacts are rows on students.csv now, contacts.csv is gone from
    ``schema.ALL_SPECS``, and every remaining file is a real SIS export -- so
    "the stack says zero rows" is no longer a reason to accept an absent file.
    Zero rows and an absent file look identical to Clever's ingest only if you
    are the one writing the export; from Clever's side the absent file reads as
    every row it used to have having been deleted.
    """

    local_dir = _write_stack_files(tmp_path)
    (local_dir / "enrollments.csv").unlink()  # header-only, i.e. genuinely 0 rows
    district = _district()
    # The stack has no enrollments table at all, so it agrees there are 0 rows.
    stack = _stack_with_fingerprint(FINGERPRINT)
    assert stack.counts().get("enrollments", 0) == 0

    with pytest.raises(sftp_push.IncompleteStackError, match="enrollments.csv"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_absent_optional_staff_csv_is_tolerated_when_stack_agrees_it_is_empty(
    tmp_path: Path,
):
    """staff.csv is the one file in ``schema.OPTIONAL_FILES`` -- Clever's own
    SFTP spec (v2.2.0), not this engine, says it need not exist. An absent
    staff.csv must NOT raise when the in-memory stack agrees it has zero staff
    rows. This is a narrower exception than the pre-2026-08-05 contacts.csv
    one: it applies to exactly one file, for a documented spec reason, not a
    "this engine owns the file" reason."""

    local_dir = _write_stack_files(tmp_path)
    (local_dir / "staff.csv").unlink()
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)
    assert stack.counts().get("staff", 0) == 0

    result = sftp_push.push(
        local_dir,
        district,
        dry_run=True,
        stack=stack,
        allowlist={district.sftp.username},
    )
    assert "staff.csv" not in set(result)


def test_absent_optional_staff_csv_with_nonzero_stack_rows_is_still_flagged(
    tmp_path: Path,
):
    """The mismatch case for the new exception: the stack claims staff rows
    exist, but staff.csv is absent from disk. The optional-file exception
    only covers a GENUINE absence of staff data, not a stack that disagrees
    with what's on disk -- the same principle as the general mismatch test
    below, narrowed to the one file that is otherwise allowed to be absent."""

    local_dir = _write_stack_files(tmp_path)
    (local_dir / "staff.csv").unlink()
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)
    stack._tables["staff.csv"] = [{"Staff id": "STF1"}]
    assert stack.counts()["staff"] == 1

    with pytest.raises(sftp_push.IncompleteStackError, match="staff.csv"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


def test_absent_file_with_nonzero_stack_rows_is_flagged(tmp_path: Path):
    """The mismatch case: the in-memory stack holds rows for a file that is not
    on disk at all. Whatever else that is, it is not a stack that can be pushed
    -- uploading the rest of it reads to Clever as every one of that file's
    rows having been deleted. (Retargeted from contacts.csv, which is no longer
    a file, onto enrollments.csv, which still is.)"""

    local_dir = _write_stack_files(tmp_path)
    (local_dir / "enrollments.csv").unlink()
    district = _district()
    stack = CsvStack(
        {
            "students.csv": [
                {
                    "School id": "SCH1",
                    "Student id": "STU1",
                    "Student number": "STU1",
                    "Last name": "Barnes",
                    "First name": "Jordan",
                    "Middle name": "",
                    "Grade": "5",
                    "Gender": "F",
                    "DOB": "01/01/2015",
                    "Student email": f"jordan.barnes@{FINGERPRINT}",
                }
            ],
            # The stack says this student is enrolled somewhere...
            "enrollments.csv": [
                {"School id": "SCH1", "Section id": "SEC1", "Student id": "STU1"}
            ],
        },
        migrated_columns={},
    )
    assert stack.counts()["enrollments"] == 1  # ...but the file is not on disk.

    with pytest.raises(sftp_push.IncompleteStackError, match="enrollments.csv"):
        sftp_push.push(
            local_dir,
            district,
            dry_run=True,
            stack=stack,
            allowlist={district.sftp.username},
        )


# ---------------------------------------------------------------------------
# Fix 8: dry-run byte size and row count come from the same (on-disk) source
# ---------------------------------------------------------------------------


def test_dry_run_row_count_reflects_disk_not_a_mismatched_in_memory_stack(tmp_path: Path):
    """Reproduction: a push dir whose contents don't match ``stack`` used to
    pair an on-disk byte size with an in-memory ``stack.counts()`` row count,
    so a 3-row file could be reported as "0 rows" -- two numbers describing two
    different versions of the data, neither reliably the one about to be
    uploaded. Both must come from the file actually on disk.

    Retargeted 2026-08-05: the original mismatch was staged with a stale
    contacts.csv, which is no longer a schema file (and so is never described
    at all). enrollments.csv stages the same mismatch on a file that still
    exists -- 3 rows on disk, no enrollments table whatsoever in the stack.
    """

    local_dir = _write_stack_files(tmp_path, enrollment_rows=3, with_stale_contacts=True)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)  # no enrollments table -> counts() == 0
    assert stack.counts().get("enrollments", 0) == 0

    files = sftp_push._describe_files(local_dir)
    by_name = {name: (size, rows) for name, size, rows in files}

    # The rows that are ACTUALLY on disk, not the stack's 0.
    assert by_name["enrollments.csv"][1] == 3
    assert by_name["students.csv"][1] == 1
    # ...and the byte size comes from the same place, so the two numbers always
    # describe one file rather than two versions of it.
    for filename, (size, _rows) in by_name.items():
        assert size == (local_dir / filename).stat().st_size

    # The stale contacts.csv is not a schema file, so it is neither described
    # nor pushed -- it must not sneak into the report as a seventh file.
    assert (local_dir / "contacts.csv").exists()
    assert "contacts.csv" not in by_name
    assert set(by_name) == set(_CORE_FILENAMES)

    # The public dry-run path reports exactly the same set.
    result = sftp_push.push(
        local_dir,
        district,
        dry_run=True,
        stack=stack,
        allowlist={district.sftp.username},
    )
    assert sorted(result) == sorted(_CORE_FILENAMES)


# ---------------------------------------------------------------------------
# Fix 3 (guardrail.py): last-pushed counts are written after a real push
# ---------------------------------------------------------------------------


def test_real_push_writes_last_pushed_counts_as_sibling_of_local_dir(tmp_path: Path, monkeypatch):
    local_dir = _write_stack_files(tmp_path, with_stale_contacts=True)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    monkeypatch.setattr(
        sftp_push, "_real_push", lambda local_dir, district, files: [f[0] for f in files]
    )

    assert sftp_push.read_last_pushed_counts(local_dir) is None

    sftp_push.push(
        local_dir,
        district,
        dry_run=False,
        stack=stack,
        allowlist={district.sftp.username},
    )

    counts = sftp_push.read_last_pushed_counts(local_dir)
    assert counts is not None
    assert counts["students"] == 1  # distinct Student ids in the STACK
    # 0 because no students.csv row in the stack carries a "Contact sis id" --
    # contacts are a projection over that file now, so the stale contacts.csv
    # sitting on disk contributes nothing at all here.
    assert counts["contacts"] == 0

    counts_path = local_dir.parent / sftp_push.LAST_PUSHED_COUNTS_FILENAME
    assert counts_path.exists()
    assert counts_path.parent == local_dir.parent


def test_dry_run_never_writes_last_pushed_counts(tmp_path: Path):
    local_dir = _write_stack_files(tmp_path)
    district = _district()
    stack = _stack_with_fingerprint(FINGERPRINT)

    sftp_push.push(
        local_dir,
        district,
        dry_run=True,
        stack=stack,
        allowlist={district.sftp.username},
    )

    assert sftp_push.read_last_pushed_counts(local_dir) is None


# ---------------------------------------------------------------------------
# Host key policy
# ---------------------------------------------------------------------------


def test_host_key_policy_defaults_to_reject(monkeypatch):
    monkeypatch.delenv(sftp_push.ALLOW_UNKNOWN_HOST_KEY_ENV, raising=False)
    pytest.importorskip("paramiko")
    import paramiko

    policy = sftp_push._host_key_policy()
    assert isinstance(policy, paramiko.RejectPolicy)


def test_host_key_policy_opt_in_uses_auto_add_and_logs_loudly(monkeypatch, caplog):
    monkeypatch.setenv(sftp_push.ALLOW_UNKNOWN_HOST_KEY_ENV, "1")
    pytest.importorskip("paramiko")
    import paramiko

    with caplog.at_level("WARNING"):
        policy = sftp_push._host_key_policy()

    assert isinstance(policy, paramiko.AutoAddPolicy)
    assert any("unrecognized SFTP host key" in r.message for r in caplog.records)
    monkeypatch.delenv(sftp_push.ALLOW_UNKNOWN_HOST_KEY_ENV, raising=False)
