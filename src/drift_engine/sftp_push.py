"""Writes the CSV stack to a district's SFTP endpoint.

SAFETY IS THE POINT OF THIS MODULE. Before anything else -- before a single
``os.stat`` on the local files, before ``paramiko`` is even imported --
:func:`push` builds a ``safety.TargetIdentity`` and calls
``safety.assert_safe_target``. That call is never wrapped in a
try/except here, never reordered to run after network or filesystem work,
and ``SafetyViolation`` is never caught anywhere in this module. If the
target is wrong, nothing below this point ever runs.

``paramiko`` is not installable in this sandbox (no PyPI access), so it is
imported lazily, only on the real (non-dry-run) push path -- the module must
still import, and ``dry_run=True`` must still work, with paramiko completely
absent.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Sequence

from . import schema
from .config import DistrictConfig
from .csvstack import CsvStack
from .safety import TargetIdentity, assert_safe_target

logger = logging.getLogger(__name__)

__all__ = [
    "push",
    "SftpTransferError",
    "IncompleteStackError",
    "ALLOW_UNKNOWN_HOST_KEY_ENV",
    "read_last_pushed_counts",
    "LAST_PUSHED_COUNTS_FILENAME",
]

#: Filename (sibling of the ``current/``-style directory passed as
#: ``local_dir``) that records ``stack.counts()`` as of the last successful
#: REAL push. See ``_write_last_pushed_counts``/``read_last_pushed_counts``
#: (Fix 3, guardrail.py).
LAST_PUSHED_COUNTS_FILENAME = "last_pushed_counts.json"

#: Explicit, loudly-logged opt-in for connecting to a host whose key is not
#: already in known_hosts. See ``_host_key_policy`` for why this is opt-in
#: rather than the paramiko default (``AutoAddPolicy``).
ALLOW_UNKNOWN_HOST_KEY_ENV = "SFTP_ALLOW_UNKNOWN_HOST_KEY"

CONNECT_TIMEOUT_SECONDS = 15
TRANSFER_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

#: Prefix for the temp remote name a file is uploaded to before being
#: renamed into place. Leading "." keeps it out of any directory listing a
#: partner-side tool might do a naive "*.csv" glob against mid-upload.
_TEMP_PREFIX = "."
_TEMP_SUFFIX = ".part"


class SftpTransferError(RuntimeError):
    """A genuine SFTP transport failure (connect, transfer, size mismatch).

    Distinct from ``SafetyViolation`` -- this is "the network/remote host
    misbehaved", not "we are pointed at the wrong target". Never used to
    mask a ``SafetyViolation``.
    """


class IncompleteStackError(RuntimeError):
    """Raised when ``local_dir`` is missing a required schema file (Fix 6).

    A missing core SIS file (schools/students/teachers/staff/sections/
    enrollments.csv) uploaded as a partial stack reads to Clever exactly like
    every one of that file's rows was deleted -- the same mass-deletion
    false signal the guardrail (``guardrail.py``) exists to catch on the
    content side, but here on the transport side, before the guardrail ever
    runs. ``contacts.csv`` is the one legitimate exception: it is
    engine-owned and ``CsvStack.save`` never writes it when it has zero
    rows, so its absence is only an error here when the in-memory ``stack``
    disagrees (claims contacts rows exist, but the file isn't on disk).
    Never caught and downgraded -- same posture as ``SafetyViolation``.
    """


# ---------------------------------------------------------------------------
# Completeness gate + file description (used by both dry-run and real push)
# ---------------------------------------------------------------------------


def _assert_stack_complete(local_dir: Path, stack: CsvStack) -> None:
    """Raise :class:`IncompleteStackError` if any required file is absent.

    Runs before ``_describe_files``, so both the dry-run report and the real
    upload path refuse a partial stack before claiming (or attempting)
    success -- see the module-level reproduction in ``IncompleteStackError``.
    """

    counts = stack.counts()
    missing: list[str] = []
    for spec in schema.ALL_SPECS:
        if (local_dir / spec.filename).exists():
            continue
        if spec.engine_added and counts.get(spec.record_type, 0) == 0:
            # Legitimate: an engine-owned file (contacts.csv) with zero rows
            # is never written by CsvStack.save, so its absence here is
            # expected, not a partial upload.
            continue
        missing.append(spec.filename)

    if missing:
        raise IncompleteStackError(
            f"{local_dir}: missing required file(s) {missing}. Refusing to describe "
            "or push a partial stack -- Clever would read each missing file's "
            "absence as every one of its rows having been deleted. If a file "
            "legitimately has zero rows, it must still exist on disk with just a "
            "header row; this is only ever skipped for an engine-added file like "
            "contacts.csv, and only when the in-memory stack agrees it has zero "
            "rows too."
        )


def _count_data_rows(path: Path) -> int:
    """Number of data rows (excluding the header) actually in ``path`` on disk."""

    with open(path, "r", encoding=schema.ENCODING, newline="") as fh:
        reader = csv.reader(fh)
        try:
            next(reader)  # header
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def _describe_files(local_dir: Path) -> list[tuple[str, int, int]]:
    """``(filename, byte_size, row_count)`` for every schema file present on disk.

    Fix 8: both numbers are read from the SAME source -- the file actually
    sitting in ``local_dir`` -- rather than pairing an on-disk byte size with
    an in-memory ``stack.counts()`` row count. If ``local_dir`` held a stale
    file left over from a previous run (or simply didn't match ``stack``
    exactly), size and row count would otherwise describe two different
    versions of the data, and neither would reliably reflect what is about
    to be uploaded. Reading both from disk means they always agree with each
    other and with what actually gets pushed.
    """

    described: list[tuple[str, int, int]] = []
    for spec in schema.ALL_SPECS:
        path = local_dir / spec.filename
        if not path.exists():
            continue
        size = path.stat().st_size
        rows = _count_data_rows(path)
        described.append((spec.filename, size, rows))
    return described


def _last_pushed_counts_path(local_dir: Path) -> Path:
    """Sibling-of-``local_dir`` location for the last-successful-push counts.

    For a real (non-dry) push, ``local_dir`` is always
    ``state/<district>/current/`` (see ``runner.py``'s ``RunPaths``/
    ``run_once``) -- this mirrors ``RunPaths.baseline_counts``
    (``state/<district>/baseline_counts.json``) by writing one directory up,
    alongside it, rather than inside ``current/`` itself (which only ever
    holds the seven schema CSVs).
    """

    return local_dir.parent / LAST_PUSHED_COUNTS_FILENAME


def read_last_pushed_counts(local_dir: Path) -> dict[str, int] | None:
    """Record counts as of the last successful REAL push, or ``None``.

    ``None`` means "no real push has ever succeeded from this directory yet"
    -- a genuine first run, not an error (mirrors
    ``runner.RunPaths.read_baseline_counts`` returning ``None`` for the
    analogous "file does not exist yet" case). Intended caller: runner.py,
    which should pass the result as ``guardrail.enforce``'s
    ``last_pushed_counts`` keyword argument (see that function's docstring
    for Fix 3) -- see this module's docstring for the exact wiring needed.
    """

    path = _last_pushed_counts_path(local_dir)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_last_pushed_counts(local_dir: Path, stack: CsvStack) -> None:
    """Persist ``stack.counts()`` after a successful real push. Atomic.

    Fix 3 (guardrail.py): the guardrail's per-record-type ratio only ever
    saw *planned* Change objects, so a CSV mutated by something other than
    this engine (a bad export, a disk problem, manual tampering) between two
    runs was invisible to it -- the pre-change stack it evaluated already
    reflected the damage, with zero DELETE changes to show for it. This file
    is what closes that gap: the next run's guardrail call can compare the
    freshly-loaded stack's counts against what was last known to have
    actually reached Clever, independent of what that run's own Change list
    contains.

    Deliberately best-effort at the call site (see ``push``): if this write
    fails, the actual upload already succeeded, and failing the whole run
    over a bookkeeping side file would be worse than logging loudly and
    moving on -- the next run's guardrail check simply falls back to
    whatever this file already held (or to the pre-Fix-3 planned-deletes-only
    check if it never existed), not to zero protection.
    """

    path = _last_pushed_counts_path(local_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    payload = json.dumps(stack.counts(), indent=2, sort_keys=True) + "\n"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp_path, path)


def _remote_path(remote_dir: str, filename: str) -> str:
    normalized = remote_dir.rstrip("/") or ""
    return f"{normalized}/{filename}" if normalized else f"/{filename}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def push(
    local_dir: Path,
    district: DistrictConfig,
    *,
    dry_run: bool,
    stack: CsvStack,
    allowlist: Iterable[str],
) -> list[str]:
    """Upload the CSV stack in ``local_dir`` to ``district``'s SFTP endpoint.

    ``allowlist`` is every configured SFTP username across ALL districts
    (``config.EngineConfig.allowlist()``), used for
    ``safety.assert_username_allowlisted``. It is a REQUIRED keyword-only
    argument (Fix 7): a previous version defaulted it to ``None`` and, when
    omitted, silently called ``config.load_config()`` with no path -- i.e.
    the DEFAULT config location, ignoring whatever ``--config`` override the
    caller's own ``EngineConfig`` was actually loaded from. A run against a
    non-default config would then be allowlist-checked against the wrong
    file entirely, with no error. There is no safe default here -- the
    caller is the only one who knows which config it means -- so this now
    fails immediately and loudly (a ``TypeError`` naming the missing
    argument) rather than silently guessing. Always pass
    ``cfg.allowlist()`` from the same ``EngineConfig`` the caller loaded
    ``district`` from.

    Every gate in ``safety.assert_safe_target`` runs before anything else --
    before ``local_dir`` is even stat'd, before ``paramiko`` is imported. It
    is never caught here.

    ``dry_run=True`` performs NO network connection at all -- not
    "connect, then skip the write". It logs exactly what would be
    uploaded (filenames, byte sizes, row counts) and returns that file list.
    ``paramiko`` is only imported on the real-push path (see
    :func:`_real_push`), so dry-run works even where paramiko cannot be
    installed.

    Before either path proceeds, every schema file that should exist is
    required to be present in ``local_dir`` (Fix 6) -- a partial upload
    (e.g. a missing students.csv) reads to Clever as mass deletion, exactly
    the failure mode the guardrail exists to prevent, just one step earlier
    in the pipeline. See :class:`IncompleteStackError`.
    """

    target = TargetIdentity(
        district_id=district.id,
        host=district.sftp.host,
        port=district.sftp.port,
        username=district.sftp.username,
        remote_dir=district.sftp.remote_dir,
    )

    # --- THE gate. Runs before anything else in this function. ---------
    assert_safe_target(
        target,
        allowlist=allowlist,
        fingerprint=district.data_fingerprint,
        sample_values=stack.fingerprint_sample(),
    )
    # ---------------------------------------------------------------------

    # --- Completeness gate (Fix 6). Runs before describing or uploading
    # anything, for both dry-run and real push. ---------------------------
    _assert_stack_complete(local_dir, stack)
    # ---------------------------------------------------------------------

    files = _describe_files(local_dir)

    if dry_run:
        logger.info(
            "[DRY RUN] district=%s user=%s@%s%s -- would push %d file(s); "
            "no SFTP connection attempted.",
            district.id, district.sftp.username, district.sftp.host,
            district.sftp.remote_dir, len(files),
        )
        for filename, size, rows in files:
            logger.info("[DRY RUN]   %s -- %d bytes, %d rows", filename, size, rows)
        return [filename for filename, _size, _rows in files]

    pushed = _real_push(local_dir, district, files)

    # Fix 3 (guardrail.py): record what actually just reached Clever, so the
    # NEXT run's guardrail call can compare against it. Best-effort -- see
    # ``_write_last_pushed_counts``'s docstring for why a failure here does
    # not fail this already-successful push.
    try:
        _write_last_pushed_counts(local_dir, stack)
    except OSError:
        logger.warning(
            "Could not persist %s after a successful push to district %s -- the "
            "upload itself succeeded, but the next run's guardrail will be unable "
            "to compare against this run's actual record counts until this is "
            "written successfully.",
            LAST_PUSHED_COUNTS_FILENAME, district.id,
            exc_info=True,
        )

    return pushed


# ---------------------------------------------------------------------------
# Real push (paramiko imported lazily, only reached here)
# ---------------------------------------------------------------------------


def _host_key_policy():
    """Build the paramiko host key policy for this connection.

    Default: reject any host key not already present in the loaded
    known_hosts (``paramiko.RejectPolicy``). Blind trust-on-first-use
    (``paramiko.AutoAddPolicy``) is NOT used unconditionally here, because
    this connection carries a live username+password credential to upload
    roster data (PII: names, emails, guardian contact info) over the
    network -- an unconditionally-auto-added host key would silently accept
    whatever key a man-in-the-middle presents, on every single connection,
    with no persistent record and no operator visibility. That is an
    inappropriate default for a credentialed data upload, even to a sandbox.

    Opt-in escape hatch: setting ``SFTP_ALLOW_UNKNOWN_HOST_KEY=1`` switches
    to ``AutoAddPolicy`` for this call, WITH a loud warning-level log line
    every time it is used. This is meant for a genuine first-time connection
    to a new endpoint, made deliberately by an operator who is watching the
    logs -- not a standing default anyone could forget was set.
    """

    import paramiko

    if os.environ.get(ALLOW_UNKNOWN_HOST_KEY_ENV) == "1":
        logger.warning(
            "%s=1 is set -- this connection will accept and persist ANY "
            "unrecognized SFTP host key without verification. This should only "
            "be used deliberately, for a genuine first-time connection to a new "
            "sandbox SFTP endpoint. If this endpoint has been connected to "
            "before, STOP: an unexpected host key change can mean the server "
            "was compromised or you are talking to the wrong host.",
            ALLOW_UNKNOWN_HOST_KEY_ENV,
        )
        return paramiko.AutoAddPolicy()
    return paramiko.RejectPolicy()


def _is_auth_failure(exc: BaseException) -> bool:
    import paramiko

    return isinstance(exc, paramiko.AuthenticationException)


def _with_retries(fn, *, description: str):
    """Call ``fn()``, retrying transient failures with backoff.

    Never retries an authentication failure -- that will not succeed on
    retry and retrying it needlessly hammers the credential. Any other
    failure gets up to ``MAX_RETRIES`` attempts with a linear backoff.
    """

    last_exc: BaseException | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below when appropriate
            if _is_auth_failure(exc):
                raise
            last_exc = exc
            if attempt >= MAX_RETRIES:
                break
            logger.warning(
                "Transient failure during %s (attempt %d/%d): %s. Retrying in %.1fs.",
                description, attempt, MAX_RETRIES, exc, RETRY_BACKOFF_SECONDS * attempt,
            )
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise SftpTransferError(
        f"{description} failed after {MAX_RETRIES} attempt(s): {last_exc}"
    ) from last_exc


def _upload_one(sftp, local_path: Path, remote_dir: str, local_size: int) -> None:
    """Upload ``local_path`` to a temp remote name, then rename into place.

    Uploading to a temp name and renaming only after a successful,
    size-verified transfer means Clever's SFTP poller can never see a
    half-written CSV. A truncated roster file (e.g. students.csv cut off
    partway through) reads to Clever as "everyone past that point was
    deleted" -- a mass-deletion false signal, which is exactly the failure
    mode the 10% guardrail (``guardrail.py``) exists to catch on the
    *content* side. This is the equivalent protection on the *transport*
    side: the guardrail cannot save us from a network hiccup mid-upload.
    """

    filename = local_path.name
    remote_final = _remote_path(remote_dir, filename)
    remote_temp = _remote_path(remote_dir, f"{_TEMP_PREFIX}{filename}{_TEMP_SUFFIX}")

    def _do_upload() -> None:
        sftp.put(str(local_path), remote_temp)
        remote_attrs = sftp.stat(remote_temp)
        if remote_attrs.st_size != local_size:
            raise SftpTransferError(
                f"Upload size mismatch for {filename}: local={local_size} bytes, "
                f"remote={remote_attrs.st_size} bytes. Refusing to rename into place."
            )
        if hasattr(sftp, "posix_rename"):
            sftp.posix_rename(remote_temp, remote_final)
        else:  # pragma: no cover - depends on server SFTP version
            sftp.rename(remote_temp, remote_final)

    _with_retries(_do_upload, description=f"uploading {filename}")


def _real_push(
    local_dir: Path, district: DistrictConfig, files: Sequence[tuple[str, int, int]]
) -> list[str]:
    import paramiko  # Lazy: not installable in this sandbox; dry-run never reaches here.

    password = district.sftp.resolve_password()
    host, port, username = district.sftp.host, district.sftp.port, district.sftp.username

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(_host_key_policy())

    def _connect() -> None:
        # Never log the password. Username + host only.
        logger.info("Connecting to SFTP host %s as %s", host, username)
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=CONNECT_TIMEOUT_SECONDS,
            auth_timeout=CONNECT_TIMEOUT_SECONDS,
            banner_timeout=CONNECT_TIMEOUT_SECONDS,
        )

    sftp = None
    try:
        _with_retries(_connect, description=f"connecting to {host}")

        sftp = client.open_sftp()
        channel = sftp.get_channel()
        if channel is not None:
            channel.settimeout(TRANSFER_TIMEOUT_SECONDS)

        pushed: list[str] = []
        for filename, size, _rows in files:
            _upload_one(sftp, local_dir / filename, district.sftp.remote_dir, size)
            pushed.append(filename)
        return pushed
    finally:
        if sftp is not None:
            sftp.close()
        client.close()
