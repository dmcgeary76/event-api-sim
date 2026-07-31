"""Loads ``config/districts.yml`` and ``.env`` into typed objects.

Two things shape this module's design:

1. **PyYAML may not be installed.** It is declared in ``pyproject.toml``, but
   this sandbox has no PyPI access, so the engine must not hard-fail just
   because ``import yaml`` fails. ``load_config`` tries ``yaml.safe_load``
   first and falls back to :func:`_parse_yaml_fallback`, a small, STRICT
   parser that understands exactly the YAML subset ``config/districts.yml``
   uses (nested mappings, a list of mappings, scalar str/int/bool, ``#``
   comments, blank lines) and raises :class:`ConfigError` on anything it
   doesn't recognise rather than guessing. That fallback parser is the
   riskiest piece of code in this module -- see ``tests/test_config.py`` for
   dedicated coverage of it.
2. **Credentials never live in YAML.** ``config/districts.yml`` only names
   the environment variable holding each district's SFTP password
   (``password_env``); the actual value is read from the process
   environment via :meth:`SftpConfig.resolve_password`, which never logs or
   echoes it -- only the (missing) variable *name* ever appears in an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import safety as _safety
from .models import SafetyViolation

logger = logging.getLogger(__name__)

#: Fallback values matching this project's one real sandbox district
#: (Tulsa replica). Used whenever a district entry in ``districts.yml``
#: omits these optional keys, so adding a new district without them still
#: works, and existing behaviour for the current district is unchanged.
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_STAFF_EMAIL_DOMAIN = "tulsaschools-replica.org"
DEFAULT_STUDENT_EMAIL_DOMAIN = "students.tulsaschools-replica.org"
DEFAULT_AREA_CODES: tuple[str, ...] = ("918", "539", "405", "580")

#: ``src/drift_engine/config.py`` -> ``src/drift_engine`` -> ``src`` -> repo root.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "districts.yml"
DEFAULT_DOTENV_PATH = Path(".env")


class ConfigError(RuntimeError):
    """Raised for malformed or invalid engine configuration.

    Kept as a plain ``RuntimeError`` subclass, consistent with
    ``GuardrailViolation``/``SafetyViolation`` in ``models.py`` -- config
    problems are operator-facing failures, not programmer bugs.
    """


# ---------------------------------------------------------------------------
# Typed config objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SftpConfig:
    """One district's SFTP connection details. Never holds a password."""

    host: str
    port: int
    username: str
    #: Name of the environment variable holding the password -- not the
    #: password itself. See ``.env.example``.
    password_env: str
    remote_dir: str

    def resolve_password(self) -> str:
        """Read the password from the environment.

        Raises :class:`ConfigError` naming the missing variable if it is
        unset or empty. Never logs or returns anything about the value
        itself in the error message -- only the variable *name*.
        """

        value = os.environ.get(self.password_env)
        if not value:
            raise ConfigError(
                f"Environment variable {self.password_env!r} is not set (or is "
                f"empty). This holds the SFTP password for district "
                f"{self.username!r}'s config entry -- populate it in .env "
                "(see .env.example) before connecting."
            )
        return value


@dataclass(frozen=True)
class DistrictConfig:
    """One sandbox district this engine is permitted to drift.

    ``timezone``, ``staff_email_domain``, ``student_email_domain``, and
    ``area_codes`` are optional per-district overrides (project brief §6:
    adding a district should be a config-only change, never a code change).
    All four default to this project's one real sandbox district (Tulsa
    replica) so existing behaviour is unchanged when a district entry omits
    them.

    ``timezone`` is what cadence/day-of-week resolution uses for "what day is
    it, for this district" (see ``runner.resolve_run_date``) -- it must be
    the district's own local timezone, not the host machine's or UTC's,
    because the fixed weekly cadence (brief §4) is a promise about *that
    district's* calendar days.

    ``staff_email_domain``/``student_email_domain``/``area_codes`` are the
    per-district values a future content-generation pass (``content.py``/
    ``selection.py``) can consume instead of the hard-coded Tulsa constants
    currently in ``schema.py``/``content.py`` -- this dataclass is the
    config-side half of that contract.
    """

    id: str
    label: str
    enabled: bool
    sftp: SftpConfig
    data_fingerprint: str
    eventing_verified: bool
    timezone: str = DEFAULT_TIMEZONE
    staff_email_domain: str = DEFAULT_STAFF_EMAIL_DOMAIN
    student_email_domain: str = DEFAULT_STUDENT_EMAIL_DOMAIN
    area_codes: tuple[str, ...] = DEFAULT_AREA_CODES


@dataclass(frozen=True)
class EngineConfig:
    """The full set of configured districts."""

    districts: tuple[DistrictConfig, ...]

    def get(self, district_id: str) -> DistrictConfig:
        for district in self.districts:
            if district.id == district_id:
                return district
        raise KeyError(f"No configured district {district_id!r}")

    def enabled_districts(self) -> tuple[DistrictConfig, ...]:
        return tuple(d for d in self.districts if d.enabled)

    def allowlist(self) -> frozenset[str]:
        """Every configured SFTP username.

        Feeds ``safety.assert_username_allowlisted`` -- the hard gate that
        stops this engine from ever writing to a non-sandbox target. This
        returns every configured username (enabled or not) deliberately: a
        disabled district is still a *known, deliberately reviewed* sandbox
        entry (brief §6 -- "adding a district is a config change, never a
        code change"), just one not currently scheduled to run. Excluding it
        here would not add safety, since ``enabled`` is only consulted by the
        scheduler/runner, never by the safety gate.
        """

        return frozenset(d.sftp.username for d in self.districts)


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal stdlib ``.env`` reader.

    Supports ``KEY=VALUE`` lines, blank lines, ``#``-prefixed comments, and
    optional surrounding single/double quotes on the value. Deliberately
    does NOT overwrite a variable that is already set in the process
    environment -- an operator's real shell/CI environment always wins over
    a checked-in-adjacent ``.env`` file. No third-party dependency is added
    for this; it is intentionally small.
    """

    p = Path(path)
    if not p.exists():
        return

    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in os.environ:
            continue
        os.environ[key] = value


# ---------------------------------------------------------------------------
# YAML loading: PyYAML if present, else a small strict fallback parser.
# ---------------------------------------------------------------------------


def _parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.info("PyYAML is not installed; using the built-in fallback YAML parser.")
        return _parse_yaml_fallback(text)
    return yaml.safe_load(text) or {}


def _strip_inline_comment(s: str) -> str:
    """Strip a trailing ``# comment`` from ``s``, respecting quoted strings.

    A ``#`` only starts a comment when it is outside any quotes and is
    either the first character or preceded by whitespace -- this matches
    YAML's own rule closely enough for the strings this file actually
    contains (hostnames, emails, ids; no ``#`` inside a quoted value).
    """

    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or s[i - 1].isspace():
                return s[:i]
    return s


def _parse_scalar(text: str, lineno: int) -> Any:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text == "true":
        return True
    if text == "false":
        return False
    if text in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class _Entry:
    """One non-blank, comment-stripped logical line of the fallback parser.

    ``raw_indent`` is the literal leading-space count. ``starts_item`` is
    True for a YAML sequence item (a line beginning with ``"- "`` or exactly
    ``"-"``). ``level`` is the indent at which the line's *content* should be
    compared for grouping purposes: for a sequence item this is
    ``raw_indent + 2`` (the column where the key text after ``"- "`` begins),
    so a mapping opened by ``"- id: foo"`` and continued by plain
    ``"label: bar"`` lines indented two spaces further than the dash lines up
    correctly as one mapping.
    """

    __slots__ = ("raw_indent", "level", "starts_item", "content", "lineno")

    def __init__(self, raw_indent: int, level: int, starts_item: bool, content: str, lineno: int):
        self.raw_indent = raw_indent
        self.level = level
        self.starts_item = starts_item
        self.content = content
        self.lineno = lineno


def _preprocess(text: str) -> list[_Entry]:
    entries: list[_Entry] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise ConfigError(
                f"line {lineno}: tabs are not supported by the fallback YAML parser "
                "(use spaces for indentation)"
            )
        stripped_leading = raw.lstrip(" ")
        raw_indent = len(raw) - len(stripped_leading)
        content = _strip_inline_comment(stripped_leading).rstrip()
        if not content:
            continue

        starts_item = content == "-" or content.startswith("- ")
        if starts_item:
            item_content = "" if content == "-" else content[2:]
            level = raw_indent + 2
        else:
            item_content = content
            level = raw_indent

        entries.append(_Entry(raw_indent, level, starts_item, item_content, lineno))
    return entries


def _parse_mapping(entries: list[_Entry], idx: int, level: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    first = True
    while idx < len(entries):
        entry = entries[idx]
        if entry.level != level:
            break
        if not first and entry.starts_item:
            # A new sequence item at this level ends the current mapping.
            break
        first = False

        if ":" not in entry.content:
            raise ConfigError(
                f"line {entry.lineno}: expected a 'key: value' mapping entry, "
                f"got {entry.content!r}"
            )
        key, _, rest = entry.content.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            raise ConfigError(f"line {entry.lineno}: mapping entry has an empty key")
        if key in result:
            raise ConfigError(f"line {entry.lineno}: duplicate key {key!r} in mapping")

        idx += 1
        if rest == "":
            if idx < len(entries) and entries[idx].level > level:
                value, idx = _parse_block(entries, idx, entries[idx].level)
            else:
                raise ConfigError(
                    f"line {entry.lineno}: key {key!r} has no scalar value and no "
                    "nested mapping/list beneath it"
                )
        else:
            value = _parse_scalar(rest, entry.lineno)
        result[key] = value
    return result, idx


def _parse_sequence(entries: list[_Entry], idx: int, level: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while idx < len(entries) and entries[idx].level == level and entries[idx].starts_item:
        entry = entries[idx]
        if entry.content == "":
            idx += 1
            if idx < len(entries) and entries[idx].level > level:
                value, idx = _parse_block(entries, idx, entries[idx].level)
            else:
                raise ConfigError(f"line {entry.lineno}: empty list item with no nested value")
        elif ":" in entry.content:
            value, idx = _parse_mapping(entries, idx, level)
        else:
            value = _parse_scalar(entry.content, entry.lineno)
            idx += 1
        items.append(value)
    return items, idx


def _parse_block(entries: list[_Entry], idx: int, level: int) -> tuple[Any, int]:
    if idx >= len(entries):
        return {}, idx
    entry = entries[idx]
    if entry.level != level:
        raise ConfigError(
            f"line {entry.lineno}: unexpected indentation (expected column {level})"
        )
    if entry.starts_item:
        return _parse_sequence(entries, idx, level)
    return _parse_mapping(entries, idx, level)


def _parse_yaml_fallback(text: str) -> Any:
    """Parse the YAML subset used by ``config/districts.yml``.

    Handles nested mappings, a list of mappings, scalar str/int/bool,
    ``#`` comments, and blank lines. Raises :class:`ConfigError` -- naming
    the offending line -- on anything outside that subset (tabs, missing
    colons, inconsistent indentation, duplicate keys, empty values with no
    nested block) rather than silently guessing at a parse.
    """

    entries = _preprocess(text)
    if not entries:
        return {}

    value, idx = _parse_block(entries, 0, entries[0].level)
    if idx != len(entries):
        raise ConfigError(
            f"line {entries[idx].lineno}: unexpected content {entries[idx].content!r} "
            "(indentation does not match any open block)"
        )
    return value


# ---------------------------------------------------------------------------
# Config loading + validation
# ---------------------------------------------------------------------------


def _build_sftp(entry: Mapping[str, Any], district_id: str) -> SftpConfig:
    sftp_raw = entry.get("sftp")
    if not isinstance(sftp_raw, Mapping):
        raise ConfigError(f"district {district_id!r} is missing an 'sftp' mapping")

    username = str(sftp_raw.get("username", "") or "").strip()
    if not username:
        raise ConfigError(f"district {district_id!r} has an empty sftp.username")

    host = str(sftp_raw.get("host", "") or "").strip()
    if not host:
        raise ConfigError(f"district {district_id!r} has an empty sftp.host")

    port_raw = sftp_raw.get("port", 22)
    try:
        port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"district {district_id!r} has a non-integer sftp.port: {port_raw!r}"
        ) from exc

    password_env = str(sftp_raw.get("password_env", "") or "").strip()
    if not password_env:
        raise ConfigError(f"district {district_id!r} is missing sftp.password_env")

    remote_dir = str(sftp_raw.get("remote_dir", "/") or "/")

    return SftpConfig(
        host=host,
        port=port,
        username=username,
        password_env=password_env,
        remote_dir=remote_dir,
    )


def _build_district(entry: Any) -> DistrictConfig:
    if not isinstance(entry, Mapping):
        raise ConfigError(f"each entry under 'districts' must be a mapping, got {entry!r}")

    district_id = str(entry.get("id", "") or "").strip()
    if not district_id:
        raise ConfigError(f"district entry {entry!r} is missing a non-empty 'id'")

    sftp = _build_sftp(entry, district_id)

    data_fingerprint = str(entry.get("data_fingerprint", "") or "").strip()
    if not data_fingerprint:
        raise ConfigError(
            f"district {district_id!r} has no data_fingerprint configured -- "
            "safety.assert_fingerprint_present requires one before this engine "
            "will write anything for this district."
        )
    try:
        _safety.validate_fingerprint(data_fingerprint)
    except SafetyViolation as exc:
        # Re-raised as ConfigError, not SafetyViolation: this is caught at
        # config *load* time, before any run starts, so it belongs to this
        # module's own operator-facing error type (see the ConfigError
        # docstring above) -- the message from safety.validate_fingerprint is
        # preserved verbatim so the reason is still actionable.
        raise ConfigError(
            f"district {district_id!r} has an invalid data_fingerprint: {exc}"
        ) from exc

    eventing_verified = bool(entry.get("eventing_verified", False))
    if not eventing_verified:
        logger.warning(
            "District %r has eventing_verified=false -- Secure Sync / district-app "
            "token Events API emission has not yet been confirmed as active for "
            "this district (project brief §9). Confirm in the Clever dashboard "
            "before relying on this district for partner-facing testing.",
            district_id,
        )

    timezone = str(entry.get("timezone", "") or "").strip() or DEFAULT_TIMEZONE

    staff_email_domain = (
        str(entry.get("staff_email_domain", "") or "").strip() or DEFAULT_STAFF_EMAIL_DOMAIN
    )
    student_email_domain = (
        str(entry.get("student_email_domain", "") or "").strip() or DEFAULT_STUDENT_EMAIL_DOMAIN
    )

    area_codes_raw = entry.get("area_codes")
    if area_codes_raw is None:
        area_codes: tuple[str, ...] = DEFAULT_AREA_CODES
    else:
        if not isinstance(area_codes_raw, list):
            raise ConfigError(
                f"district {district_id!r} has a non-list 'area_codes': {area_codes_raw!r} "
                "(expected a YAML list of area code strings, e.g. ['918', '539'])"
            )
        area_codes = tuple(str(v).strip() for v in area_codes_raw)
        if not all(area_codes):
            raise ConfigError(
                f"district {district_id!r} has an empty area code in 'area_codes': {area_codes_raw!r}"
            )

    return DistrictConfig(
        id=district_id,
        label=str(entry.get("label", district_id) or district_id),
        enabled=bool(entry.get("enabled", False)),
        sftp=sftp,
        data_fingerprint=data_fingerprint,
        eventing_verified=eventing_verified,
        timezone=timezone,
        staff_email_domain=staff_email_domain,
        student_email_domain=student_email_domain,
        area_codes=area_codes,
    )


def load_config(path: str | Path | None = None) -> EngineConfig:
    """Load and validate ``config/districts.yml`` (or ``path``, if given).

    Validation performed here (beyond parsing):

    * every district must have a non-empty ``id``;
    * every district must have a non-empty ``sftp.username`` and
      ``sftp.host``, and a non-empty ``sftp.password_env``;
    * every district must have a non-empty ``data_fingerprint`` (the
      secondary safety fingerprint ``safety.assert_fingerprint_present``
      hard-requires) that also passes ``safety.validate_fingerprint`` --
      i.e. it must look like a real domain fragment and contain a recognised
      sandbox marker, not just be any non-empty string (a bare ``"@"`` is
      rejected here, at load time, rather than only failing much later at
      write time);
    * ``eventing_verified: false`` is allowed (a district can be configured
      before go-live), but is logged as a warning since it means Events API
      emission has not been confirmed for that district yet (brief §9);
    * ``area_codes``, if present, must be a YAML list of strings.

    ``timezone``, ``staff_email_domain``, and ``student_email_domain`` are
    optional and default to this project's one real sandbox district (Tulsa
    replica) if omitted.

    Any violation raises :class:`ConfigError` with an actionable message.
    """

    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    text = cfg_path.read_text(encoding="utf-8")
    data = _parse_yaml(text)

    if not isinstance(data, Mapping):
        raise ConfigError(f"{cfg_path}: top-level YAML document must be a mapping")

    districts_raw = data.get("districts")
    if districts_raw is None:
        raise ConfigError(f"{cfg_path}: missing required top-level 'districts' key")
    if not isinstance(districts_raw, list):
        raise ConfigError(f"{cfg_path}: 'districts' must be a list")

    districts = tuple(_build_district(entry) for entry in districts_raw)
    return EngineConfig(districts=districts)
