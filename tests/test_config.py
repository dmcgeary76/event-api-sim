"""Tests for drift_engine.config.

Two things get the most scrutiny here, per the build brief:

* The fallback YAML parser (used when PyYAML is not installed) -- it must
  correctly parse the REAL config/districts.yml, and it must raise a clear
  error on malformed input rather than guessing.
* The .env reader and password resolution -- since credentials flow through
  here, we check quoting/comments/no-overwrite behaviour and that a missing
  password env var raises a clear, non-leaking error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drift_engine import config

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DISTRICTS_YML = REPO_ROOT / "config" / "districts.yml"


# ---------------------------------------------------------------------------
# Fallback YAML parser vs. the real config/districts.yml
# ---------------------------------------------------------------------------


def test_fallback_parser_parses_real_districts_yml():
    text = REAL_DISTRICTS_YML.read_text(encoding="utf-8")
    data = config._parse_yaml_fallback(text)

    assert isinstance(data, dict)
    assert list(data.keys()) == ["districts"]
    districts = data["districts"]
    assert isinstance(districts, list)
    assert len(districts) == 1

    entry = districts[0]
    assert entry["id"] == "steadfast-backpack-8880"
    assert entry["label"] == "Tulsa Replica Sandbox"
    assert entry["enabled"] is True
    assert entry["data_fingerprint"] == "tulsaschools-replica.org"
    assert entry["eventing_verified"] is False

    sftp = entry["sftp"]
    assert sftp["host"] == "sftp.clever.com"
    assert sftp["port"] == 22
    assert isinstance(sftp["port"], int)
    assert sftp["username"] == "steadfast-backpack-8880"
    assert sftp["password_env"] == "SFTP_PASSWORD_STEADFAST_BACKPACK_8880"
    assert sftp["remote_dir"] == "/"


def test_fallback_parser_matches_pyyaml_on_real_file():
    """Belt-and-braces: if PyYAML happens to be installed in some other
    environment, the fallback parser's output for this real file must match
    it exactly (order-independent), so there's no drift between the two
    parse paths."""

    yaml = pytest.importorskip("yaml")
    text = REAL_DISTRICTS_YML.read_text(encoding="utf-8")
    assert config._parse_yaml_fallback(text) == yaml.safe_load(text)


def test_load_config_reads_real_file_end_to_end():
    engine_config = config.load_config(REAL_DISTRICTS_YML)
    assert len(engine_config.districts) == 1
    district = engine_config.get("steadfast-backpack-8880")
    assert district.label == "Tulsa Replica Sandbox"
    assert district.enabled is True
    assert district.sftp.username == "steadfast-backpack-8880"
    assert district.sftp.host == "sftp.clever.com"
    assert district.sftp.port == 22
    assert district.data_fingerprint == "tulsaschools-replica.org"
    assert district.eventing_verified is False
    assert engine_config.allowlist() == frozenset({"steadfast-backpack-8880"})
    assert engine_config.enabled_districts() == (district,)

    # Fix 5 / Fix 9: the district's own timezone and per-district email
    # domains / area codes, as configured in the real districts.yml.
    assert district.timezone == "America/Chicago"
    assert district.staff_email_domain == "tulsaschools-replica.org"
    assert district.student_email_domain == "students.tulsaschools-replica.org"
    assert district.area_codes == ("918", "539", "405", "580")


def test_load_config_missing_district_raises_keyerror():
    engine_config = config.load_config(REAL_DISTRICTS_YML)
    with pytest.raises(KeyError):
        engine_config.get("does-not-exist")


# ---------------------------------------------------------------------------
# Fallback parser: malformed input raises rather than guesses
# ---------------------------------------------------------------------------


def test_fallback_parser_rejects_tabs():
    with pytest.raises(config.ConfigError, match="tabs"):
        config._parse_yaml_fallback("districts:\n\t- id: foo\n")


def test_fallback_parser_rejects_missing_colon():
    # A second mapping-level line with no colon at all -- not a valid
    # "key: value" entry and not a new list item, so it cannot be
    # interpreted as anything else.
    with pytest.raises(config.ConfigError, match="key: value"):
        config._parse_yaml_fallback("id: foo\nbar\n")


def test_fallback_parser_accepts_plain_scalar_list_items():
    """A colon-free '- value' line is legitimately a plain scalar list item
    (not every list in this subset is a list of mappings), so it must NOT
    raise."""

    data = config._parse_yaml_fallback("items:\n  - alpha\n  - beta\n")
    assert data == {"items": ["alpha", "beta"]}


def test_fallback_parser_rejects_bad_indentation():
    text = "districts:\n  - id: foo\n     label: bar\n"  # 5 spaces, doesn't align
    with pytest.raises(config.ConfigError):
        config._parse_yaml_fallback(text)


def test_fallback_parser_rejects_empty_key():
    with pytest.raises(config.ConfigError, match="empty key"):
        config._parse_yaml_fallback("districts:\n  - id: foo\n    : bar\n")


def test_fallback_parser_rejects_duplicate_keys():
    text = "id: foo\nid: bar\n"
    with pytest.raises(config.ConfigError, match="duplicate key"):
        config._parse_yaml_fallback(text)


def test_fallback_parser_rejects_key_with_no_value_and_no_block():
    text = "id:\n"
    with pytest.raises(config.ConfigError, match="no scalar value"):
        config._parse_yaml_fallback(text)


def test_fallback_parser_handles_comments_and_blank_lines():
    text = (
        "# a top comment\n"
        "\n"
        "districts:\n"
        "  - id: foo   # inline comment\n"
        "\n"
        "    enabled: true\n"
    )
    data = config._parse_yaml_fallback(text)
    assert data == {"districts": [{"id": "foo", "enabled": True}]}


def test_fallback_parser_handles_quoted_scalars_with_hash_inside():
    text = 'label: "Tulsa #1 Replica"\n'
    data = config._parse_yaml_fallback(text)
    assert data == {"label": "Tulsa #1 Replica"}


def test_fallback_parser_parses_int_bool_and_bare_string_scalars():
    text = "port: 22\nenabled: true\ndisabled_flag: false\nhost: sftp.clever.com\n"
    data = config._parse_yaml_fallback(text)
    assert data == {
        "port": 22,
        "enabled": True,
        "disabled_flag": False,
        "host": "sftp.clever.com",
    }
    assert isinstance(data["port"], int)


def test_fallback_parser_empty_document_returns_empty_dict():
    assert config._parse_yaml_fallback("") == {}
    assert config._parse_yaml_fallback("# just a comment\n\n") == {}


# ---------------------------------------------------------------------------
# load_config validation
# ---------------------------------------------------------------------------


def _minimal_yaml(**overrides: str) -> str:
    base = {
        "id": "test-district",
        "enabled": "true",
        "host": "sftp.clever.com",
        "username": "test-district-user",
        "password_env": "SFTP_PASSWORD_TEST",
        "remote_dir": "/",
        # Must pass safety.validate_fingerprint (contain "." and a recognised
        # sandbox marker, be at least 8 chars) -- plain "example.org" no
        # longer qualifies now that Fix 1 rejects weak fingerprints.
        "data_fingerprint": "sandbox.example.org",
    }
    base.update(overrides)
    return (
        "districts:\n"
        f"  - id: {base['id']}\n"
        f"    enabled: {base['enabled']}\n"
        "    sftp:\n"
        f"      host: {base['host']}\n"
        "      port: 22\n"
        f"      username: {base['username']}\n"
        f"      password_env: {base['password_env']}\n"
        f"      remote_dir: \"{base['remote_dir']}\"\n"
        f"    data_fingerprint: {base['data_fingerprint']}\n"
    )


def test_load_config_rejects_missing_data_fingerprint(tmp_path: Path):
    # An explicit empty quoted string -- parses fine, but load_config's own
    # validation (not the YAML parser) must reject it: safety.py hard-requires
    # a non-empty data_fingerprint per district.
    text = (
        "districts:\n"
        "  - id: test-district\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: test-district-user\n"
        "      password_env: SFTP_PASSWORD_TEST\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(config.ConfigError, match="data_fingerprint"):
        config.load_config(path)


def test_load_config_rejects_weak_data_fingerprint(tmp_path: Path):
    """Fix 1 (audit): a fingerprint like '@' used to pass because
    ``assert_fingerprint_present`` only checked non-empty substring
    membership. ``load_config`` must now reject it at load time, before any
    run starts."""

    text = (
        "districts:\n"
        "  - id: test-district\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: test-district-user\n"
        "      password_env: SFTP_PASSWORD_TEST\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"@\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(config.ConfigError, match="data_fingerprint"):
        config.load_config(path)


def test_load_config_rejects_domain_shaped_fingerprint_with_no_sandbox_marker(tmp_path: Path):
    """A fingerprint that looks domain-shaped but has no replica/sandbox/dev/
    test/demo/staging marker (e.g. a generic corporate domain) is exactly as
    dangerous as '@' -- it could match real production data just as easily."""

    text = (
        "districts:\n"
        "  - id: test-district\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: test-district-user\n"
        "      password_env: SFTP_PASSWORD_TEST\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"acme-schools.org\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(config.ConfigError, match="sandbox marker"):
        config.load_config(path)


def test_load_config_rejects_empty_id(tmp_path: Path):
    text = (
        "districts:\n"
        "  - id: \"\"\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: someone\n"
        "      password_env: SFTP_PASSWORD_X\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: example.org\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="id"):
        config.load_config(path)


def test_load_config_rejects_empty_username(tmp_path: Path):
    text = (
        "districts:\n"
        "  - id: someplace\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: \"\"\n"
        "      password_env: SFTP_PASSWORD_X\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: example.org\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="username"):
        config.load_config(path)


def test_load_config_warns_when_eventing_not_verified(tmp_path: Path, caplog):
    text = _minimal_yaml()
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    with caplog.at_level("WARNING"):
        engine_config = config.load_config(path)

    district = engine_config.get("test-district")
    assert district.eventing_verified is False
    assert any("eventing_verified" in record.message for record in caplog.records)


def test_load_config_missing_districts_key_raises(tmp_path: Path):
    path = tmp_path / "districts.yml"
    path.write_text("not_districts: []\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="districts"):
        config.load_config(path)


# ---------------------------------------------------------------------------
# Fix 9: per-district timezone / email domains / area codes
# ---------------------------------------------------------------------------


def test_district_optional_fields_default_when_omitted(tmp_path: Path):
    """A district entry that omits timezone/staff_email_domain/
    student_email_domain/area_codes gets this project's Tulsa defaults, so
    existing config files (and the real districts.yml before this change)
    keep working unchanged."""

    path = tmp_path / "districts.yml"
    path.write_text(_minimal_yaml(), encoding="utf-8")

    district = config.load_config(path).get("test-district")
    assert district.timezone == "America/Chicago"
    assert district.staff_email_domain == "tulsaschools-replica.org"
    assert district.student_email_domain == "students.tulsaschools-replica.org"
    assert district.area_codes == ("918", "539", "405", "580")


def test_district_optional_fields_can_be_overridden(tmp_path: Path):
    """A second district can override the email domains / area codes / tz
    entirely via config -- no code change required (project brief §6)."""

    text = (
        "districts:\n"
        "  - id: second-sandbox\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: second-sandbox-user\n"
        "      password_env: SFTP_PASSWORD_SECOND\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"second-district-replica.org\"\n"
        "    timezone: \"America/New_York\"\n"
        "    staff_email_domain: \"secondschools-replica.org\"\n"
        "    student_email_domain: \"students.secondschools-replica.org\"\n"
        "    area_codes:\n"
        "      - \"212\"\n"
        "      - \"718\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")

    district = config.load_config(path).get("second-sandbox")
    assert district.timezone == "America/New_York"
    assert district.staff_email_domain == "secondschools-replica.org"
    assert district.student_email_domain == "students.secondschools-replica.org"
    assert district.area_codes == ("212", "718")


def test_fallback_parser_handles_area_codes_as_a_scalar_list(tmp_path: Path):
    """``area_codes`` is a YAML list of bare scalar strings (not a list of
    mappings, like ``districts:`` itself is) -- the fallback parser must
    handle this shape, since it is the one place in this config file's
    schema that is a list of plain scalars rather than a list of mappings."""

    text = (
        "districts:\n"
        "  - id: scalar-list-district\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: scalar-list-user\n"
        "      password_env: SFTP_PASSWORD_SCALAR\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"scalar-list-replica.org\"\n"
        "    area_codes:\n"
        "      - \"111\"\n"
        "      - \"222\"\n"
        "      - \"333\"\n"
    )
    data = config._parse_yaml_fallback(text)
    entry = data["districts"][0]
    assert entry["area_codes"] == ["111", "222", "333"]

    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")
    district = config.load_config(path).get("scalar-list-district")
    assert district.area_codes == ("111", "222", "333")


def test_area_codes_must_be_a_list(tmp_path: Path):
    text = (
        "districts:\n"
        "  - id: bad-area-codes\n"
        "    enabled: true\n"
        "    sftp:\n"
        "      host: sftp.clever.com\n"
        "      port: 22\n"
        "      username: bad-area-codes-user\n"
        "      password_env: SFTP_PASSWORD_BAD\n"
        "      remote_dir: \"/\"\n"
        "    data_fingerprint: \"bad-area-codes-replica.org\"\n"
        "    area_codes: \"918\"\n"
    )
    path = tmp_path / "districts.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="area_codes"):
        config.load_config(path)


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------


def test_load_dotenv_parses_quotes_comments_and_blanks(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DOTENV_TEST_A", raising=False)
    monkeypatch.delenv("DOTENV_TEST_B", raising=False)
    monkeypatch.delenv("DOTENV_TEST_C", raising=False)
    monkeypatch.delenv("DOTENV_TEST_D", raising=False)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "# a comment\n"
        "\n"
        'DOTENV_TEST_A="quoted value"\n'
        "DOTENV_TEST_B='single quoted'\n"
        "DOTENV_TEST_C=bare_value  \n"
        "# DOTENV_TEST_D=should_not_be_set\n",
        encoding="utf-8",
    )

    config.load_dotenv(env_path)

    assert os.environ["DOTENV_TEST_A"] == "quoted value"
    assert os.environ["DOTENV_TEST_B"] == "single quoted"
    assert os.environ["DOTENV_TEST_C"] == "bare_value"
    assert "DOTENV_TEST_D" not in os.environ

    for key in ("DOTENV_TEST_A", "DOTENV_TEST_B", "DOTENV_TEST_C"):
        monkeypatch.delenv(key, raising=False)


def test_load_dotenv_does_not_overwrite_existing_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOTENV_TEST_EXISTING", "from_shell")
    env_path = tmp_path / ".env"
    env_path.write_text("DOTENV_TEST_EXISTING=from_dotenv\n", encoding="utf-8")

    config.load_dotenv(env_path)

    assert os.environ["DOTENV_TEST_EXISTING"] == "from_shell"


def test_load_dotenv_missing_file_is_a_noop(tmp_path: Path):
    # Should not raise.
    config.load_dotenv(tmp_path / "does-not-exist.env")


# ---------------------------------------------------------------------------
# Password resolution
# ---------------------------------------------------------------------------


def test_resolve_password_missing_env_var_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SFTP_PASSWORD_MISSING_TEST", raising=False)
    sftp = config.SftpConfig(
        host="sftp.clever.com",
        port=22,
        username="someone",
        password_env="SFTP_PASSWORD_MISSING_TEST",
        remote_dir="/",
    )
    with pytest.raises(config.ConfigError, match="SFTP_PASSWORD_MISSING_TEST"):
        sftp.resolve_password()


def test_resolve_password_reads_the_named_env_var(monkeypatch):
    monkeypatch.setenv("SFTP_PASSWORD_PRESENT_TEST", "hunter2")
    sftp = config.SftpConfig(
        host="sftp.clever.com",
        port=22,
        username="someone",
        password_env="SFTP_PASSWORD_PRESENT_TEST",
        remote_dir="/",
    )
    assert sftp.resolve_password() == "hunter2"
    monkeypatch.delenv("SFTP_PASSWORD_PRESENT_TEST", raising=False)
