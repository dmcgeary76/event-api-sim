"""Tests for drift_engine.content.

The Anthropic-backed tests never make a real network call -- they stub
``anthropic`` via ``sys.modules`` (or, for the "not installed" case, blank it
out entirely) and drive ``AnthropicContentGenerator`` against a fake client
whose ``messages.create`` returns canned response objects.
"""

from __future__ import annotations

import random
import re
import sys
import types

import pytest

from drift_engine import content as content_mod
from drift_engine import schema
from drift_engine.content import (
    AnthropicContentGenerator,
    CannedContentGenerator,
    build_content_generator,
    is_valid_middle_name,
    is_valid_phone,
    is_valid_staff_email,
    is_valid_student_email,
)

STAFF_EMAIL_RE = re.compile(r"^[a-z]+\.[a-z]+@" + re.escape(schema.STAFF_EMAIL_DOMAIN) + r"$")
STUDENT_EMAIL_RE = re.compile(
    r"^[a-z]+\.[a-z]+\d+@" + re.escape(schema.STUDENT_EMAIL_DOMAIN) + r"$"
)


# ---------------------------------------------------------------------------
# CannedContentGenerator
# ---------------------------------------------------------------------------


def test_canned_generator_is_deterministic_under_fixed_seed() -> None:
    gen_a = CannedContentGenerator(random.Random(42))
    gen_b = CannedContentGenerator(random.Random(42))

    for _ in range(10):
        assert gen_a.middle_name("Jordan", "Barnes") == gen_b.middle_name("Jordan", "Barnes")
        assert gen_a.guardian_name("Barnes") == gen_b.guardian_name("Barnes")
        assert gen_a.phone() == gen_b.phone()
        assert gen_a.teacher_name() == gen_b.teacher_name()
        assert gen_a.student_email("Jordan", "Barnes", "1001") == gen_b.student_email(
            "Jordan", "Barnes", "1001"
        )


def test_canned_guardian_email_matches_staff_convention() -> None:
    gen = CannedContentGenerator(random.Random(1))
    for _ in range(50):
        name = gen.guardian_name("Barnes")
        email = gen.guardian_email(name, "Barnes")
        assert STAFF_EMAIL_RE.match(email), email
        assert is_valid_staff_email(email)


def test_canned_student_email_matches_student_convention() -> None:
    gen = CannedContentGenerator(random.Random(2))
    for i in range(50):
        email = gen.student_email("Jordan", "Barnes", str(1000 + i))
        assert STUDENT_EMAIL_RE.match(email), email
        assert is_valid_student_email(email)


def test_canned_phones_are_exactly_ten_digits() -> None:
    gen = CannedContentGenerator(random.Random(3))
    for _ in range(100):
        phone = gen.phone()
        assert phone.isdigit()
        assert len(phone) == 10
        assert is_valid_phone(phone)


def test_canned_middle_names_are_single_alphabetic_tokens() -> None:
    gen = CannedContentGenerator(random.Random(4))
    for _ in range(50):
        name = gen.middle_name("Jordan", "Barnes")
        assert name.isalpha()
        assert " " not in name
        assert is_valid_middle_name(name)


def test_canned_guardian_surname_sometimes_matches_sometimes_differs() -> None:
    gen = CannedContentGenerator(random.Random(5))
    matches = 0
    differs = 0
    for _ in range(200):
        name = gen.guardian_name("Barnes")
        last = name.split()[-1]
        if last == "Barnes":
            matches += 1
        else:
            differs += 1
    assert matches > 0
    assert differs > 0


# ---------------------------------------------------------------------------
# Fix 1(a): guardian_email/student_email can produce a genuinely different
# address for the SAME person via the ``attempt`` kwarg.
# ---------------------------------------------------------------------------


def test_guardian_email_attempt_produces_different_addresses_for_same_person() -> None:
    gen = CannedContentGenerator(random.Random(6))
    name = "Mary Smith"
    seen = {gen.guardian_email(name, "Smith", attempt=i) for i in range(4)}
    assert len(seen) > 1, seen
    for email in seen:
        assert is_valid_staff_email(email), email


def test_guardian_email_attempt_zero_matches_original_convention() -> None:
    # Default (attempt=0) must reproduce this project's original, only
    # convention exactly -- no existing caller that never passes ``attempt``
    # should see any behaviour change.
    gen = CannedContentGenerator(random.Random(6))
    assert gen.guardian_email("Mary Smith", "Smith", attempt=0) == "mary.smith@" + schema.STAFF_EMAIL_DOMAIN


def test_student_email_attempt_produces_different_addresses_for_same_person() -> None:
    gen = CannedContentGenerator(random.Random(7))
    seen = {
        gen.student_email("Jordan", "Barnes", "1001", attempt=i) for i in range(4)
    }
    assert len(seen) > 1, seen
    for email in seen:
        assert is_valid_student_email(email), email


# ---------------------------------------------------------------------------
# Fix 2: CannedContentGenerator.guardian_email must never produce an invalid
# (e.g. leading-dot) local part, even when the guardian name's first token
# has no [a-z] characters at all (e.g. imported from a real/hand-edited
# contacts.csv). Exercises the real generator, not just the predicate
# functions in isolation.
# ---------------------------------------------------------------------------


def test_canned_guardian_email_handles_a_first_token_with_no_letters() -> None:
    gen = CannedContentGenerator(random.Random(8))
    email = gen.guardian_email("123 Smith", "Smith")
    assert is_valid_staff_email(email), email
    assert not email.startswith("."), email


def test_canned_guardian_email_handles_a_completely_unusable_name() -> None:
    gen = CannedContentGenerator(random.Random(9))
    email = gen.guardian_email("123 456", "")
    assert is_valid_staff_email(email), email


def test_canned_teacher_email_matches_staff_convention() -> None:
    gen = CannedContentGenerator(random.Random(10))
    email = gen.teacher_email("Alex", "Rivera")
    assert email == "alex.rivera@" + schema.STAFF_EMAIL_DOMAIN
    assert is_valid_staff_email(email)


# ---------------------------------------------------------------------------
# Fix 6: validators must reject a trailing newline (re.match with a "$" in
# the pattern is not "matches the whole string").
# ---------------------------------------------------------------------------


def test_validators_reject_a_trailing_newline() -> None:
    assert not is_valid_middle_name("James\n")
    assert not is_valid_phone("9185551234\n")
    assert not is_valid_staff_email("first.last@" + schema.STAFF_EMAIL_DOMAIN + "\n")
    assert not is_valid_student_email("first.last000123@" + schema.STUDENT_EMAIL_DOMAIN + "\n")


# ---------------------------------------------------------------------------
# Fix 7: per-district domain / area-code configurability.
# ---------------------------------------------------------------------------


def test_canned_generator_honours_configured_domains_and_area_codes() -> None:
    gen = CannedContentGenerator(
        random.Random(11),
        staff_email_domain="example-schools.org",
        student_email_domain="students.example-schools.org",
        area_codes=("212",),
    )

    email = gen.guardian_email("Mary Smith", "Smith")
    assert email.endswith("@example-schools.org")
    assert is_valid_staff_email(email, "example-schools.org")
    assert not is_valid_staff_email(email)  # wrong domain vs. the global default

    student_email = gen.student_email("Jordan", "Barnes", "1001")
    assert student_email.endswith("@students.example-schools.org")
    assert is_valid_student_email(student_email, "students.example-schools.org")

    teacher_email = gen.teacher_email("Alex", "Rivera")
    assert teacher_email.endswith("@example-schools.org")

    for _ in range(20):
        assert gen.phone().startswith("212")


def test_build_content_generator_passes_through_domain_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRIFT_CONTENT_CANNED_ONLY", "1")
    gen = build_content_generator(
        random.Random(12),
        staff_email_domain="example-schools.org",
        student_email_domain="students.example-schools.org",
        area_codes=("212",),
    )
    assert isinstance(gen, CannedContentGenerator)
    assert gen.guardian_email("Mary Smith", "Smith").endswith("@example-schools.org")


# ---------------------------------------------------------------------------
# build_content_generator
# ---------------------------------------------------------------------------


def test_build_returns_canned_when_env_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIFT_CONTENT_CANNED_ONLY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-present")
    gen = build_content_generator(random.Random(1))
    assert isinstance(gen, CannedContentGenerator)


def test_build_returns_canned_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIFT_CONTENT_CANNED_ONLY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    gen = build_content_generator(random.Random(1))
    assert isinstance(gen, CannedContentGenerator)


def test_build_returns_canned_when_canned_only_kwarg_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-present")
    monkeypatch.delenv("DRIFT_CONTENT_CANNED_ONLY", raising=False)
    gen = build_content_generator(random.Random(1), canned_only=True)
    assert isinstance(gen, CannedContentGenerator)


def test_build_returns_anthropic_when_key_present_and_not_canned_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key-present")
    monkeypatch.delenv("DRIFT_CONTENT_CANNED_ONLY", raising=False)
    gen = build_content_generator(random.Random(1))
    assert isinstance(gen, AnthropicContentGenerator)


# ---------------------------------------------------------------------------
# AnthropicContentGenerator: fake client plumbing
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    """Stands in for ``client.messages``. ``create`` is swappable per test."""

    def __init__(self, create_fn) -> None:
        self._create_fn = create_fn

    def create(self, **kwargs):
        return self._create_fn(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, create_fn) -> None:
        self.messages = _FakeMessages(create_fn)


def _install_fake_anthropic_module(monkeypatch: pytest.MonkeyPatch, create_fn) -> None:
    """Install a fake ``anthropic`` module into sys.modules for this test.

    ``anthropic.Anthropic(api_key=...)`` returns a ``_FakeAnthropicClient``
    whose ``messages.create`` calls ``create_fn``.
    """

    fake_module = types.ModuleType("anthropic")

    class _FakeAnthropicClass:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self._client = _FakeAnthropicClient(create_fn)
            # Delegate attribute access so `anthropic.Anthropic(...)` behaves
            # like the real client object.
            self.messages = self._client.messages

    fake_module.Anthropic = _FakeAnthropicClass
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def _json_response(payload: str) -> _FakeResponse:
    return _FakeResponse(payload)


# ---------------------------------------------------------------------------
# Degradation: anthropic not installed
# ---------------------------------------------------------------------------


def test_degrades_to_canned_when_anthropic_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate "package not installed": `import anthropic` raises ImportError.
    monkeypatch.setitem(sys.modules, "anthropic", None)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")
    value = gen.middle_name("Jordan", "Barnes")

    assert is_valid_middle_name(value)
    stats = gen.stats()
    assert stats["fallback_served"] >= 1


# ---------------------------------------------------------------------------
# Degradation: API raises
# ---------------------------------------------------------------------------


def test_degrades_to_canned_when_api_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kwargs):
        raise RuntimeError("simulated network/timeout/rate-limit failure")

    _install_fake_anthropic_module(monkeypatch, _raise)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")
    value = gen.phone()

    assert is_valid_phone(value)
    stats = gen.stats()
    assert stats["batch_failures"] >= 1
    assert stats["fallback_served"] >= 1


# ---------------------------------------------------------------------------
# Degradation: malformed JSON
# ---------------------------------------------------------------------------


def test_degrades_to_canned_when_response_is_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _create(**kwargs):
        return _json_response("this is not json at all {{{")

    _install_fake_anthropic_module(monkeypatch, _create)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")
    value = gen.middle_name("Jordan", "Barnes")

    assert is_valid_middle_name(value)
    stats = gen.stats()
    assert stats["batch_failures"] >= 1
    assert stats["fallback_served"] >= 1


# ---------------------------------------------------------------------------
# Degradation: values failing validation
# ---------------------------------------------------------------------------


def test_degrades_to_canned_when_values_fail_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    def _create(**kwargs):
        prompt = kwargs.get("messages", [{}])[0].get("content", "")
        if "middle_names" in prompt:
            payload = json.dumps({"middle_names": ["123!!", "Mc Donald", "", "way-too-long-" * 5]})
        elif "phones" in prompt:
            payload = json.dumps({"phones": ["not-a-phone", "12345", "555-1234"]})
        else:
            payload = json.dumps({"given_names": ["Ok"], "surnames": ["Also-Ok!"]})
        return _json_response(payload)

    _install_fake_anthropic_module(monkeypatch, _create)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")

    middle = gen.middle_name("Jordan", "Barnes")
    assert is_valid_middle_name(middle)

    phone = gen.phone()
    assert is_valid_phone(phone)

    stats = gen.stats()
    assert stats["values_rejected_by_validation"] >= 1
    assert stats["fallback_served"] >= 1


def test_validation_layer_rejects_deliberately_malformed_email_phone_middle_name() -> None:
    assert not is_valid_middle_name("123")
    assert not is_valid_middle_name("Mc Donald")
    assert not is_valid_middle_name("")
    assert not is_valid_middle_name("x" * 21)

    assert not is_valid_phone("12345")
    assert not is_valid_phone("not-a-phone")
    assert not is_valid_phone("555-123-4567")

    assert not is_valid_staff_email("not-an-email")
    assert not is_valid_staff_email("first.last@wrong-domain.com")
    assert not is_valid_student_email("first.last@" + schema.STAFF_EMAIL_DOMAIN)

    # And the valid counterparts pass, as a sanity check the validators
    # aren't just rejecting everything.
    assert is_valid_middle_name("Marie")
    assert is_valid_phone("9185551234")
    assert is_valid_staff_email("first.last@" + schema.STAFF_EMAIL_DOMAIN)
    assert is_valid_student_email("first.last000123@" + schema.STUDENT_EMAIL_DOMAIN)

    # The predicate checks above exercise the validators in isolation, but
    # that alone let Fix 2's bug through undetected: this ALSO drives the
    # real generator with the exact input the audit reproduced the bug
    # with (a Contact name whose first token has no [a-z] at all), and
    # asserts the value it actually produces passes validation -- not just
    # that the validator correctly rejects a hand-written bad string.
    gen = CannedContentGenerator(random.Random(13))
    email = gen.guardian_email("123 Smith", "Smith")
    assert is_valid_staff_email(email), email


def test_anthropic_guardian_email_retry_tries_a_different_candidate_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: the old "retry" called the same pure fallback function with the
    same arguments and returned the identical (invalid) value anyway. This
    drives the real ``AnthropicContentGenerator.guardian_email`` path (not
    just the predicate in isolation) with validation forced to fail a few
    times, and asserts the retry actually produced different-looking
    candidates before succeeding -- not the same rejected value repeated.
    """

    gen = AnthropicContentGenerator(random.Random(14), api_key="sk-fake")
    seen: list[str] = []

    def fake_is_valid_staff_email(value: str, domain: str | None = None) -> bool:
        seen.append(value)
        return len(seen) >= 3  # first two candidates "fail" validation

    monkeypatch.setattr(content_mod, "is_valid_staff_email", fake_is_valid_staff_email)

    email = gen.guardian_email("Mary Smith", "Smith")

    assert len(seen) >= 3
    assert len(set(seen)) > 1, "retry must try a different candidate, not repeat the same one"
    assert email == seen[-1]
    stats = gen.stats()
    assert stats["values_rejected_by_validation"] == 2
    assert stats["fallback_served"] == 1


# ---------------------------------------------------------------------------
# Stats surface: AI vs fallback tracked
# ---------------------------------------------------------------------------


def test_stats_tracks_ai_served_when_api_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    def _create(**kwargs):
        prompt = kwargs.get("messages", [{}])[0].get("content", "")
        if "middle_names" in prompt:
            payload = json.dumps({"middle_names": ["Marie", "Elizabeth", "Grace"]})
        elif "phones" in prompt:
            payload = json.dumps({"phones": ["9185551234", "5395559876"]})
        else:
            payload = json.dumps({"given_names": ["Alex"], "surnames": ["Rivera"]})
        return _json_response(payload)

    _install_fake_anthropic_module(monkeypatch, _create)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")
    value = gen.middle_name("Jordan", "Barnes")

    assert value in {"Marie", "Elizabeth", "Grace"}
    stats = gen.stats()
    # Fix 3: exactly 1 -- one ``_take`` draw for one method call. Tightened
    # from ">= 1" (the original assertion) because ">=" cannot catch a
    # double-count regression; it would pass just as happily whether this
    # said 1 or 3.
    assert stats["ai_served"] == 1
    assert stats["batch_requests"] == 1


def test_stats_double_count_fixed_for_two_field_value_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 3: ``guardian_name``/``teacher_name`` each draw from TWO queues
    (a given name and a surname) via ``_take``, which already increments
    ``ai_served`` once per draw. The methods themselves used to ALSO
    increment ``ai_served``/``fallback_served`` on top of that -- the audit
    measured a single ``teacher_name()`` call reporting ``ai_served: 3``
    instead of the 2 values it actually drew.
    """

    import json

    def _create(**kwargs):
        prompt = kwargs.get("messages", [{}])[0].get("content", "")
        if "given_names" in prompt:
            payload = json.dumps({"given_names": ["Alex"]})
        else:
            payload = json.dumps({"surnames": ["Rivera"]})
        return _json_response(payload)

    _install_fake_anthropic_module(monkeypatch, _create)

    gen = AnthropicContentGenerator(random.Random(1), api_key="sk-fake")
    first, last = gen.teacher_name()

    assert (first, last) == ("Alex", "Rivera")
    stats = gen.stats()
    assert stats["ai_served"] == 2, stats  # one draw for first, one for last -- not 3
    assert stats["fallback_served"] == 0
