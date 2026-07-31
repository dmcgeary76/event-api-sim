"""Hard sandbox-only enforcement.

The project brief states the sandbox-only constraint is "a hard constraint
throughout the build, not just a configuration default" (§1). This module is
that constraint. Every write path -- local or remote -- routes through
``assert_safe_target`` before a single byte is written.

Four independent gates, all of which must pass:

  1. Username allowlist. sftp.clever.com is shared infrastructure, so the
     hostname is not evidence of anything. The SFTP *username* identifies the
     district, and only usernames present in config/districts.yml are permitted.
  2. Data fingerprint. The loaded stack must contain the district's expected
     fingerprint (e.g. the replica email domain). This catches an allowlisted
     credential that has been repointed at real roster data. The fingerprint
     itself is also validated for *strength* (:func:`validate_fingerprint`) --
     a config value like ``"@"`` would technically be "present" in almost any
     stack with an email column, including real production data, so a weak
     fingerprint is rejected before it is ever used as a check. This
     validation runs both at config load time (``config._build_district``)
     and again here (``assert_fingerprint_present``), so a caller that
     bypasses ``config.load_config`` entirely still gets it.
  3. Scale sanity (:func:`assert_scale_sane`). A stack that has grown or
     shrunk wildly versus the recorded baseline suggests the engine is
     looking at a different district than it thinks. This runs inside
     ``assert_safe_target`` itself whenever both ``current_counts`` and
     ``baseline_counts`` are supplied to it, so it cannot be silently skipped
     by a caller that goes through ``assert_safe_target`` but forgets to
     also call ``assert_scale_sane`` separately. (It is optional on
     ``assert_safe_target`` only because not every call site in this
     codebase has counts available yet -- see ``runner.py``, the one place
     that always supplies them before a run proceeds.)
  4. No production markers in the target identifiers (advisory tripwire).

``SafetyViolation`` is never caught and downgraded anywhere in this codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import SafetyViolation

#: Hostnames the engine will ever talk to. Belt-and-braces alongside the
#: username allowlist -- a typo'd host cannot silently become a new target.
PERMITTED_HOSTS: frozenset[str] = frozenset({"sftp.clever.com"})

#: Substrings that, if present in a target identifier, indicate production.
#: Purely advisory -- the allowlist is the real gate -- but a cheap tripwire.
PRODUCTION_MARKERS: tuple[str, ...] = ("prod", "production", "live")

#: A stack whose record counts differ from baseline by more than this factor is
#: treated as a different district, not drift.
MAX_SCALE_DRIFT = 0.25

#: Substrings a data_fingerprint must contain (case-insensitive) for
#: :func:`validate_fingerprint` to accept it as a real sandbox marker, not
#: just "any non-empty string". See that function's docstring for why this
#: exists.
SANDBOX_FINGERPRINT_MARKERS: tuple[str, ...] = (
    "replica", "sandbox", "sbx", "dev", "test", "demo", "staging",
)

#: Minimum acceptable length for a data_fingerprint. Below this, a value is
#: too generic to serve as a meaningful safety check (e.g. "@" or "x").
MIN_FINGERPRINT_LENGTH = 8


@dataclass(frozen=True)
class TargetIdentity:
    """The thing we are about to write to, described completely."""

    district_id: str
    host: str
    port: int
    username: str
    remote_dir: str


def assert_host_permitted(host: str) -> None:
    if host not in PERMITTED_HOSTS:
        raise SafetyViolation(
            f"SFTP host {host!r} is not in the permitted host set "
            f"{sorted(PERMITTED_HOSTS)}. Refusing to connect."
        )


def assert_username_allowlisted(username: str, allowlist: Iterable[str]) -> None:
    allowed = set(allowlist)
    if not allowed:
        raise SafetyViolation(
            "District allowlist is empty. Refusing to run rather than assume a "
            "target is safe. Populate config/districts.yml."
        )
    if username not in allowed:
        raise SafetyViolation(
            f"SFTP username {username!r} is not allowlisted. Permitted: "
            f"{sorted(allowed)}. Refusing to write. If this is a new sandbox, "
            f"add it to config/districts.yml deliberately."
        )


def assert_no_production_markers(*identifiers: str) -> None:
    for ident in identifiers:
        lowered = ident.lower()
        for marker in PRODUCTION_MARKERS:
            if marker in lowered:
                raise SafetyViolation(
                    f"Target identifier {ident!r} contains the production "
                    f"marker {marker!r}. Refusing to proceed."
                )


def validate_fingerprint(fingerprint: str) -> None:
    """Reject a ``data_fingerprint`` that is too weak to serve as a real check.

    The bug this closes: ``assert_fingerprint_present`` previously accepted
    ANY non-empty substring, so a config entry like
    ``data_fingerprint: "@"`` would "pass" against almost any stack that has
    an email column at all -- including real, non-sandbox data. That is not
    a safety check, it is theater.

    A fingerprint is accepted only if it:

      * is non-empty and contains no whitespace,
      * is at least :data:`MIN_FINGERPRINT_LENGTH` characters long,
      * contains a ``"."`` (i.e. looks like a domain fragment, e.g.
        ``tulsaschools-replica.org``), and
      * contains one of :data:`SANDBOX_FINGERPRINT_MARKERS` (case-insensitive)
        -- so it is not just *any* domain-shaped string, but one that
        actually reads as a non-production identifier.

    Raises :class:`~drift_engine.models.SafetyViolation` (not ``ConfigError``)
    so this one rule can be shared verbatim between ``config.py`` (checked
    once, at load time, so a weak fingerprint fails before any run starts)
    and :func:`assert_fingerprint_present` (checked again, at run time, so a
    caller that bypasses ``config.load_config`` entirely -- e.g. constructs a
    ``DistrictConfig`` by hand -- still gets the same check).
    """

    if not fingerprint:
        raise SafetyViolation(
            "data_fingerprint is empty; refusing to treat an empty string as a "
            "data-level safety check."
        )
    if any(ch.isspace() for ch in fingerprint):
        raise SafetyViolation(
            f"data_fingerprint {fingerprint!r} contains whitespace, which does not "
            "look like a plausible domain fragment. Refusing to accept it as a "
            "safety check."
        )
    if len(fingerprint) < MIN_FINGERPRINT_LENGTH:
        raise SafetyViolation(
            f"data_fingerprint {fingerprint!r} is only {len(fingerprint)} "
            f"character(s) long (minimum {MIN_FINGERPRINT_LENGTH}). A short, generic "
            "value like this could match almost any stack -- including real "
            "production data -- so it provides no real safety check. Use a "
            "substring that is actually unique to this sandbox's own data, e.g. "
            "its replica email domain."
        )
    if "." not in fingerprint:
        raise SafetyViolation(
            f"data_fingerprint {fingerprint!r} does not contain a '.' and so does "
            "not look like a domain fragment (e.g. 'tulsaschools-replica.org'). "
            "Refusing to accept it as a data-level safety check."
        )
    lowered = fingerprint.lower()
    if not any(marker in lowered for marker in SANDBOX_FINGERPRINT_MARKERS):
        raise SafetyViolation(
            f"data_fingerprint {fingerprint!r} does not contain any recognised "
            f"sandbox marker ({', '.join(SANDBOX_FINGERPRINT_MARKERS)}). A "
            "fingerprint that could just as easily match real production data "
            "(e.g. a generic corporate domain with no replica/sandbox/dev/test/"
            "demo/staging marker in it) is not a meaningful safety check. If this "
            "really is a sandbox domain, add one of these markers to it, or "
            "deliberately extend SANDBOX_FINGERPRINT_MARKERS in safety.py."
        )


def assert_fingerprint_present(
    sample_values: Iterable[str], fingerprint: str, *, district_id: str
) -> None:
    """Require ``fingerprint`` to appear in the district's own data.

    Also validates the fingerprint's *strength* (:func:`validate_fingerprint`)
    before checking presence -- a caller that constructs a ``DistrictConfig``
    without going through ``config.load_config`` (which already validates
    strength at load time) still cannot get past this gate with a weak value.
    """
    if not fingerprint:
        raise SafetyViolation(
            f"District {district_id!r} has no data_fingerprint configured. "
            f"Refusing to run without a data-level safety check."
        )
    validate_fingerprint(fingerprint)
    for value in sample_values:
        if fingerprint in value:
            return
    raise SafetyViolation(
        f"District {district_id!r} data does not contain the expected "
        f"fingerprint {fingerprint!r}. The credential may be pointed at data "
        f"other than the intended sandbox. Refusing to write."
    )


def assert_scale_sane(
    current_counts: Mapping[str, int],
    baseline_counts: Mapping[str, int],
    *,
    district_id: str,
    tolerance: float = MAX_SCALE_DRIFT,
) -> None:
    """Reject a stack that is not recognisably the same district as baseline."""
    for record_type, baseline in baseline_counts.items():
        if baseline == 0:
            continue
        current = current_counts.get(record_type, 0)
        delta = abs(current - baseline) / baseline
        if delta > tolerance:
            raise SafetyViolation(
                f"District {district_id!r}: {record_type} count moved from "
                f"{baseline} to {current} ({delta:.0%}), beyond the "
                f"{tolerance:.0%} sanity tolerance. This does not look like "
                f"incremental drift. Refusing to write; inspect the stack."
            )


def assert_safe_target(
    target: TargetIdentity,
    *,
    allowlist: Iterable[str],
    fingerprint: str,
    sample_values: Iterable[str],
    current_counts: Mapping[str, int] | None = None,
    baseline_counts: Mapping[str, int] | None = None,
) -> None:
    """Run every gate. Raises ``SafetyViolation`` on the first failure.

    ``current_counts``/``baseline_counts`` are optional, keyword-only, and
    default to ``None``. When BOTH are supplied, :func:`assert_scale_sane`
    runs as part of this same call -- the scale-sanity gate is no longer a
    separate, easy-to-forget step a caller has to remember to run alongside
    this function. When either is omitted (e.g. a call site that has not yet
    been threaded through to pass counts), the scale check is simply skipped
    for that call -- it does not raise on their absence, since ``None``
    legitimately means "this caller doesn't have counts to check yet", not
    "the counts are missing/corrupt" (that distinction belongs to the
    caller -- see ``runner.py``, which treats a missing/unreadable
    ``baseline_counts.json`` as a hard failure on any run after the district's
    genuine first one, rather than silently passing ``None`` through here).
    """
    assert_host_permitted(target.host)
    assert_username_allowlisted(target.username, allowlist)
    assert_no_production_markers(target.district_id, target.username, target.remote_dir)
    assert_fingerprint_present(
        sample_values, fingerprint, district_id=target.district_id
    )
    if current_counts is not None and baseline_counts is not None:
        assert_scale_sane(current_counts, baseline_counts, district_id=target.district_id)
