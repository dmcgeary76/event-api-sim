"""AI-assisted content generation -- the ONLY place this project touches an LLM.

Project brief §5 is explicit that the schedule and selection logic must stay
deterministic and AI-free, and that content generation must be "cleanly
isolated (e.g., a single well-defined function/module) so it can be swapped
out later without touching the scheduling or selection logic." This module
is that isolation boundary, literally: ``selection.py`` only ever calls the
methods on the ``ContentGenerator`` protocol below, and never imports
this module at runtime (see the ``TYPE_CHECKING`` guard over there).

**This is the designed swap-out point.** David is running the Anthropic-backed
implementation for this initial rollout specifically to evaluate "how
consistent and useful the AI-generated content is over time" (brief §5). If
that evaluation goes badly, or a future iteration wants to drop the AI
dependency entirely, replacing it means exactly two things:

  1. Implement the ``ContentGenerator`` protocol in a new class.
  2. Change what ``build_content_generator`` returns.

Nothing in ``selection.py``, ``cadence.py``, ``csvstack.py``, or ``seed.py``
needs to change either way -- they are coded against the protocol, not
against either concrete implementation.

Both implementations are stdlib-only at import time. ``anthropic`` is
imported lazily, inside ``AnthropicContentGenerator``'s methods, specifically
so this module (and everything that imports it) keeps working in an
environment where the ``anthropic`` package -- an optional dependency, see
``pyproject.toml``'s ``[project.optional-dependencies].ai`` -- is not
installed. A scheduled sandbox task should never fail to *load* because of a
missing optional package; it should just fall back to canned content (see
``AnthropicContentGenerator`` below for the full list of failure modes this
degrades on).
"""

from __future__ import annotations

import logging
import os
import random
import re
from typing import Protocol, runtime_checkable

from . import schema

__all__ = [
    "ContentGenerator",
    "CannedContentGenerator",
    "AnthropicContentGenerator",
    "build_content_generator",
]

logger = logging.getLogger(__name__)

#: Env var: model to use for the Anthropic implementation.
_MODEL_ENV_VAR = "DRIFT_CONTENT_MODEL"
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
#: Env var: Anthropic API key.
_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
#: Env var: force canned-only, even if a key is present. "1" enables it.
_CANNED_ONLY_ENV_VAR = "DRIFT_CONTENT_CANNED_ONLY"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ContentGenerator(Protocol):
    """The exact interface ``selection.py`` calls.

    Every method returns a single plain string (or, for ``teacher_name``, a
    ``(first, last)`` tuple) -- no ``Change`` awareness, no CSV awareness, no
    knowledge of which bucket or record is being edited. That separation is
    what makes this module swappable: it produces *values*, never decisions.
    """

    def middle_name(self, first_name: str, last_name: str) -> str: ...

    def guardian_name(self, student_last_name: str) -> str: ...

    def guardian_email(
        self, guardian_name: str, student_last_name: str, *, attempt: int = 0
    ) -> str: ...

    def phone(self) -> str: ...

    def teacher_name(self) -> tuple[str, str]: ...

    def teacher_email(self, first: str, last: str) -> str: ...

    def student_email(
        self, first: str, last: str, student_number: str, *, attempt: int = 0
    ) -> str: ...


# ---------------------------------------------------------------------------
# Curated pools for the canned generator (and the Anthropic fallback).
# ---------------------------------------------------------------------------

_GIVEN_NAMES: tuple[str, ...] = (
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Nancy", "Daniel", "Lisa",
    "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra", "Donald", "Ashley",
    "Steven", "Kimberly", "Andrew", "Emily", "Paul", "Donna", "Joshua", "Michelle",
    "Kenneth", "Dorothy", "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa",
    "Timothy", "Deborah", "Ronald", "Stephanie", "Jason", "Rebecca", "Edward", "Laura",
    "Jeffrey", "Sharon", "Ryan", "Cynthia", "Jacob", "Kathleen", "Gary", "Amy",
    "Nicholas", "Angela", "Eric", "Shirley", "Jonathan", "Anna", "Stephen", "Brenda",
    "Larry", "Pamela", "Justin", "Emma", "Scott", "Nicole", "Brandon", "Helen",
    "Benjamin", "Samantha", "Samuel", "Katherine", "Gregory", "Christine", "Alexander", "Debra",
    "Frank", "Rachel", "Patrick", "Carolyn", "Raymond", "Janet", "Jack", "Maria",
    "Dennis", "Heather", "Jerry", "Diane",
)

_SURNAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
    "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White",
    "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young",
    "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker",
    "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy",
    "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
    "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson",
    "Watson", "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray", "Mendoza",
    "Ruiz", "Hughes", "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers",
)

#: Middle names skew toward classic/plain single tokens, deliberately distinct
#: from the given-name pool so a generated middle name doesn't just echo the
#: student's own first name half the time.
_MIDDLE_NAMES: tuple[str, ...] = (
    "Marie", "Ann", "Elizabeth", "Grace", "Rose", "Lynn", "Jane", "Claire",
    "James", "Michael", "Lee", "Allen", "Ray", "Wayne", "Scott", "Dean",
    "Nicole", "Renee", "Faith", "Hope", "Louise", "Kate", "Paige", "Jean",
    "Alexander", "Thomas", "Andrew", "Joseph", "Daniel", "Robert", "Charles", "Edward",
    "Nichole", "Danielle", "Michelle", "Victoria", "Olivia", "Simone", "Cole", "Reid",
)

#: Plausible Tulsa-area (918) and a few nearby Oklahoma area codes, matching
#: the "bare 10-digit string" format the real sandbox schools.csv carries
#: (e.g. 9188484967) -- no punctuation, no leading 1. This is the DEFAULT for
#: districts that don't configure their own ``area_codes`` (see
#: ``DistrictConfig`` in ``config.py`` and ``build_content_generator`` below)
#: -- a second sandbox district in a different state must not get Tulsa-area
#: phone numbers written into its own records (Fix 7 / brief §6: adding a
#: district is a config-only change).
_AREA_CODES: tuple[str, ...] = ("918", "539", "405", "580")


def _random_digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _clean_email_token(value: str) -> str:
    """Lowercase and strip anything but letters, for use in an email local-part."""

    return re.sub(r"[^a-z]", "", value.lower())


#: Local-part styles an email can plausibly take for the SAME person, cycled
#: through by ``_templated_local_part`` as ``attempt`` increases. This exists
#: so ``guardian_email``/``student_email`` can be asked for a genuinely
#: DIFFERENT plausible address for the same name (Fix 1: a small-daily
#: "email address tweak" that recomputes the exact same value from the exact
#: same inputs is not a change at all -- Clever's CSV diff sees nothing, and
#: no ``contacts.updated`` event is ever emitted, no matter how often
#: selection.py "edits" that field).
_EMAIL_LOCAL_TEMPLATES: tuple = (
    lambda f, l: f"{f}.{l}",
    lambda f, l: f"{f[0]}.{l}",
    lambda f, l: f"{f}{l}",
)


def _templated_local_part(first: str, last: str, attempt: int) -> str:
    """Cycle through ``_EMAIL_LOCAL_TEMPLATES`` as ``attempt`` increases.

    ``attempt=0`` reproduces this project's original, only convention
    (``first.last``) exactly, so every existing caller that never passes
    ``attempt`` sees no behaviour change. Higher attempts vary the style
    (first-initial+last, no separator) and, once every style has been tried
    once, append an incrementing numeric suffix -- so this function can
    supply as many distinct-looking, still-plausible local parts for the
    same person as a caller could ever need to re-roll through.

    ``first``/``last`` must already be non-empty, cleaned tokens (callers
    are responsible for that -- see the "guardian"/"contact" fallback
    tokens in ``guardian_email`` below) so this never produces an empty or
    leading-dot local part (Fix 2).
    """

    template = _EMAIL_LOCAL_TEMPLATES[attempt % len(_EMAIL_LOCAL_TEMPLATES)]
    local = template(first, last)
    cycle = attempt // len(_EMAIL_LOCAL_TEMPLATES)
    if cycle:
        local = f"{local}{cycle + 1}"
    return local


#: Bound on how many differently-styled candidates ``AnthropicContentGenerator``
#: will try (via the fallback generator) before giving up on validation and
#: emitting a guaranteed-valid synthetic address. Also the bound selection.py
#: uses when re-rolling an UPDATE whose generated value turned out to equal
#: the current one (Fix 1(b)).
_MAX_EMAIL_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# Validation helpers -- shared by CannedContentGenerator (belt-and-braces)
# and, critically, by AnthropicContentGenerator (the actual gate that keeps a
# bad AI value out of the CSV stack).
# ---------------------------------------------------------------------------

_MIDDLE_NAME_RE = re.compile(r"[A-Za-z]{1,20}")
_PHONE_RE = re.compile(r"\d{10}")
# Deliberately conservative "syntactically valid" email check: one local
# part, one "@", one domain with at least one dot. This is not meant to be
# RFC 5322-complete -- it exists to catch AI output that is obviously broken
# (missing "@", stray whitespace, markdown fences, etc.), not to be a general
# email validator.
_EMAIL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9])?@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# NOTE (Fix 6): every validator below uses ``fullmatch``, never ``match``.
# ``re.match`` with a trailing ``$`` in the pattern is NOT the same as
# "matches the whole string" -- ``$`` matches at the end of the string OR
# immediately before a single trailing "\n", so e.g.
# ``re.match(r"^[A-Za-z]{1,20}$", "James\n")`` incorrectly succeeds.
# ``fullmatch`` requires the match to consume the entire input, which is
# what "is this value valid" actually means here.


def is_valid_middle_name(value: str) -> bool:
    return bool(value) and bool(_MIDDLE_NAME_RE.fullmatch(value))


def is_valid_phone(value: str) -> bool:
    return bool(value) and bool(_PHONE_RE.fullmatch(value))


def is_valid_staff_email(value: str, domain: str | None = None) -> bool:
    """Staff/guardian-style email: ``first.last@<staff email domain>``.

    ``domain`` defaults to ``schema.STAFF_EMAIL_DOMAIN`` (this project's one
    real sandbox district) so every existing caller that doesn't pass it
    keeps working unchanged. A content generator built for a DIFFERENT
    district (Fix 7) must pass its own configured domain here -- validating
    against the global constant would reject that district's own,
    perfectly valid addresses.
    """

    if not value or not _EMAIL_RE.fullmatch(value):
        return False
    domain = domain or schema.STAFF_EMAIL_DOMAIN
    return value.lower().endswith(f"@{domain.lower()}")


def is_valid_student_email(value: str, domain: str | None = None) -> bool:
    """Student-style email: ``first.last<zero-padded-number>@<student domain>``.

    ``domain`` defaults to ``schema.STUDENT_EMAIL_DOMAIN`` -- see
    ``is_valid_staff_email`` above for why a caller validating for a
    different district must pass its own domain.
    """

    if not value or not _EMAIL_RE.fullmatch(value):
        return False
    domain = domain or schema.STUDENT_EMAIL_DOMAIN
    return value.lower().endswith(f"@{domain.lower()}")


# ---------------------------------------------------------------------------
# Canned implementation
# ---------------------------------------------------------------------------


class CannedContentGenerator:
    """Stdlib-only content generator drawing from curated realistic pools.

    No network access, no external dependency. This is both a standalone
    implementation (selected when there's no API key, or when
    ``DRIFT_CONTENT_CANNED_ONLY=1``) and the fallback layer inside
    ``AnthropicContentGenerator`` -- every AI value that fails validation is
    replaced with a value from here, so this class's output must always be
    valid by construction.
    """

    def __init__(
        self,
        rng: random.Random,
        *,
        staff_email_domain: str | None = None,
        student_email_domain: str | None = None,
        area_codes: "tuple[str, ...] | None" = None,
    ) -> None:
        self._rng = rng
        # Fix 7: per-district overrides, defaulting to this project's one
        # real sandbox district (Tulsa replica) so an unconfigured caller
        # sees no behaviour change. See ``DistrictConfig`` in ``config.py``
        # and ``build_content_generator`` below for how these flow in.
        self._staff_email_domain = staff_email_domain or schema.STAFF_EMAIL_DOMAIN
        self._student_email_domain = student_email_domain or schema.STUDENT_EMAIL_DOMAIN
        self._area_codes = area_codes or _AREA_CODES

    def middle_name(self, first_name: str, last_name: str) -> str:
        return self._rng.choice(_MIDDLE_NAMES)

    def guardian_name(self, student_last_name: str) -> str:
        first = self._rng.choice(_GIVEN_NAMES)
        # Guardian surnames usually (not always) match the student's -- real
        # guardian data has both, e.g. a stepparent or a different custodial
        # relative with a different surname.
        if student_last_name and self._rng.random() < 0.75:
            last = student_last_name
        else:
            last = self._rng.choice(_SURNAMES)
        return f"{first} {last}"

    def guardian_email(
        self, guardian_name: str, student_last_name: str, *, attempt: int = 0
    ) -> str:
        # Fix 2: ``first``/``last`` are guaranteed non-empty, cleaned tokens
        # (falling back to "guardian"/"contact") -- NOT the raw, possibly
        # unclean-able name pieces. A Contact name whose first token has no
        # [a-z] at all (e.g. "123 Smith", imported from a real/hand-edited
        # contacts.csv) used to produce a bare ``.smith@domain`` local part,
        # which fails ``is_valid_staff_email`` -- this generator must never
        # produce that in the first place.
        parts = guardian_name.split()
        raw_first = parts[0] if parts else ""
        raw_last = parts[-1] if len(parts) > 1 else (student_last_name or "")
        first = _clean_email_token(raw_first) or "guardian"
        last = _clean_email_token(raw_last) or "contact"
        # Fix 1(a): ``attempt`` selects a genuinely different, still
        # plausible local part for the SAME person, so a caller (see
        # selection.py's small-daily contact edit) asking for an "email
        # change" for someone whose address already matches this
        # convention's default (attempt=0) can actually get one.
        local = _templated_local_part(first, last, attempt)
        return f"{local}@{self._staff_email_domain}"

    def phone(self) -> str:
        area = self._rng.choice(self._area_codes)
        exchange = self._rng.randint(200, 999)  # avoid N11-style exchanges (211/311/...)
        line = _random_digits(self._rng, 4)
        return f"{area}{exchange}{line}"

    def teacher_name(self) -> tuple[str, str]:
        return (self._rng.choice(_GIVEN_NAMES), self._rng.choice(_SURNAMES))

    def teacher_email(self, first: str, last: str) -> str:
        """Fix 7: the ``teacher_email`` half of the ContentGenerator contract.

        Previously built inline in ``selection.py`` (a small brief §5
        boundary violation -- content generation leaking into the selection
        module) using the hard-coded ``schema.STAFF_EMAIL_DOMAIN`` regardless
        of which district was actually being drifted. Guaranteed valid by
        construction the same way ``guardian_email`` is (Fix 2).
        """

        first_tok = _clean_email_token(first) or "teacher"
        last_tok = _clean_email_token(last) or "teacher"
        return f"{first_tok}.{last_tok}@{self._staff_email_domain}"

    def student_email(
        self, first: str, last: str, student_number: str, *, attempt: int = 0
    ) -> str:
        first_tok = _clean_email_token(first) or "student"
        last_tok = _clean_email_token(last) or "student"
        local = _templated_local_part(first_tok, last_tok, attempt)
        digits = re.sub(r"\D", "", student_number or "")
        padded = digits.zfill(6) if digits else _random_digits(self._rng, 6)
        return f"{local}{padded}@{self._student_email_domain}"


# ---------------------------------------------------------------------------
# Anthropic-backed implementation
# ---------------------------------------------------------------------------

#: How many of each value type to request per batch. A scheduled run needs
#: on the order of 10-20 generated values total across all buckets (see
#: cadence.py's constants) -- middle names, guardian names/emails, phones,
#: teacher names, student emails. Making one API call per value would mean
#: up to ~20 round trips per run: at even a few hundred ms of latency each,
#: that is seconds of wall-clock time added to a scheduled task purely on
#: network overhead, and ~20x the token/request cost of a handful of batched
#: calls that each return many values in one JSON payload. Batching amortizes
#: both the latency and the per-request cost across the whole run.
_BATCH_SIZE = 20

#: Anthropic value-type keys, and the JSON array key each is requested under.
_MIDDLE_NAME_KEY = "middle_names"
_GIVEN_NAME_KEY = "given_names"
_SURNAME_KEY = "surnames"
_PHONE_KEY = "phones"


class AnthropicContentGenerator:
    """Content generator backed by the Anthropic Messages API.

    Design constraints (see module docstring and brief §5):

    * ``anthropic`` is imported lazily inside ``_client`` -- never at module
      import time -- so this class can exist in a process where the package
      isn't installed; only *calling* a method on an instance that ends up
      needing the network can fail, and even that is caught and degraded
      (see below).
    * Requests are batched (``_BATCH_SIZE`` values per API call, queued and
      served locally) rather than one call per value.
    * Every value returned by the API is validated before use. A value that
      fails validation is discarded and replaced with a canned value -- the
      AI is never allowed to inject a malformed value into the CSV stack.
    * Every failure mode (missing key, import error, HTTP error, timeout,
      rate limit, malformed JSON, validation failure) is caught here and
      degrades to ``self._fallback`` (a ``CannedContentGenerator``) rather
      than propagating. A scheduled task dying because an API hiccuped is
      worse than one that emits a slightly less varied name (brief intent,
      §5 & §11: "Running Monday through Friday without manual intervention").
    * ``stats()`` exposes how many values were served from AI vs fell back,
      so David can evaluate "how consistent and useful the AI-generated
      content is over time" (brief §5) -- that framing is the whole reason
      this class exists instead of just always using the canned generator.
    """

    def __init__(
        self,
        rng: random.Random,
        *,
        api_key: str | None = None,
        model: str | None = None,
        fallback: "CannedContentGenerator | None" = None,
        batch_size: int = _BATCH_SIZE,
        staff_email_domain: str | None = None,
        student_email_domain: str | None = None,
        area_codes: "tuple[str, ...] | None" = None,
    ) -> None:
        self._rng = rng
        self._api_key = api_key or os.environ.get(_API_KEY_ENV_VAR, "")
        self._model = model or os.environ.get(_MODEL_ENV_VAR, _DEFAULT_MODEL)
        # Fix 7: per-district domain/area-code overrides. Only used to
        # construct a DEFAULT fallback -- if a caller passes its own
        # ``fallback`` instance, that instance's own configuration wins, and
        # these are only kept here for this class's OWN validation calls
        # (``is_valid_staff_email``/``is_valid_student_email`` below must
        # check against the domain THIS generator was configured with, not
        # the global ``schema`` constant).
        self._staff_email_domain = staff_email_domain or schema.STAFF_EMAIL_DOMAIN
        self._student_email_domain = student_email_domain or schema.STUDENT_EMAIL_DOMAIN
        self._fallback = fallback or CannedContentGenerator(
            rng,
            staff_email_domain=self._staff_email_domain,
            student_email_domain=self._student_email_domain,
            area_codes=area_codes,
        )
        self._batch_size = max(1, batch_size)

        self._client = None  # lazily constructed anthropic.Anthropic instance
        self._queues: dict[str, list[str]] = {
            _MIDDLE_NAME_KEY: [],
            _GIVEN_NAME_KEY: [],
            _SURNAME_KEY: [],
            _PHONE_KEY: [],
        }

        self._stats: dict[str, int] = {
            "ai_served": 0,
            "fallback_served": 0,
            "batch_requests": 0,
            "batch_failures": 0,
            "values_rejected_by_validation": 0,
        }

    # -- public stats ------------------------------------------------------

    def stats(self) -> dict:
        """Counts of AI-served vs fallback-served values, and batch health.

        Keys: ``ai_served``, ``fallback_served``, ``batch_requests``,
        ``batch_failures``, ``values_rejected_by_validation``.
        """

        return dict(self._stats)

    # -- ContentGenerator interface -----------------------------------------

    def middle_name(self, first_name: str, last_name: str) -> str:
        value = self._take(_MIDDLE_NAME_KEY, is_valid_middle_name)
        if value is not None:
            return value
        return self._fallback.middle_name(first_name, last_name)

    def guardian_name(self, student_last_name: str) -> str:
        # Fix 3: ``_take`` already increments ``ai_served``/``fallback_served``
        # for EVERY value it hands out or fails to hand out -- this method
        # must not increment those counters again on top of that, or a
        # single ``guardian_name()`` call that draws two queue values (a
        # given name and a surname) gets counted 3-4 times instead of 1-2.
        first = self._take(_GIVEN_NAME_KEY, lambda v: bool(re.fullmatch(r"[A-Za-z]{1,20}", v)))
        if first is None:
            return self._fallback.guardian_name(student_last_name)
        if student_last_name and self._rng.random() < 0.75:
            return f"{first} {student_last_name}"
        last = self._take(_SURNAME_KEY, lambda v: bool(re.fullmatch(r"[A-Za-z]{1,25}", v)))
        if last is None:
            return self._fallback.guardian_name(student_last_name)
        return f"{first} {last}"

    def guardian_email(
        self, guardian_name: str, student_last_name: str, *, attempt: int = 0
    ) -> str:
        # Email format is a fixed convention (brief §5's "believable email
        # address formats" is about names, not domains) -- deriving it
        # deterministically from the already-chosen name is both cheaper and
        # safer than asking the model to invent a domain, and it's what
        # keeps validation trivial. This mirrors the canned implementation,
        # so this method never touches the API -- every value here is
        # ``fallback_served``, never ``ai_served``, by design.
        #
        # Fix 2: the old "retry" called ``self._fallback.guardian_email``
        # with the EXACT SAME arguments a second time -- since that method is
        # a pure function of its inputs, the "retry" produced the identical
        # (invalid) value and returned it anyway, defeating the validation
        # gate entirely. This tries ``_MAX_EMAIL_ATTEMPTS`` genuinely
        # different candidates (via ``attempt``, see ``_templated_local_part``)
        # before giving up, and only then falls back to a guaranteed-valid
        # synthetic address -- which, since ``CannedContentGenerator.
        # guardian_email`` is now valid by construction (Fix 2's other half),
        # should never actually be needed in practice.
        for offset in range(_MAX_EMAIL_ATTEMPTS):
            candidate = self._fallback.guardian_email(
                guardian_name, student_last_name, attempt=attempt + offset
            )
            if is_valid_staff_email(candidate, self._staff_email_domain):
                self._stats["fallback_served"] += 1
                return candidate
            self._stats["values_rejected_by_validation"] += 1
        self._stats["fallback_served"] += 1
        return f"guardian.contact{self._rng.randint(1000, 9999)}@{self._staff_email_domain}"

    def phone(self) -> str:
        value = self._take(_PHONE_KEY, is_valid_phone)
        if value is not None:
            return value
        return self._fallback.phone()

    def teacher_name(self) -> tuple[str, str]:
        # See the Fix 3 note in ``guardian_name`` above -- ``_take`` already
        # accounts for both draws; no additional increment belongs here.
        first = self._take(_GIVEN_NAME_KEY, lambda v: bool(re.fullmatch(r"[A-Za-z]{1,20}", v)))
        last = self._take(_SURNAME_KEY, lambda v: bool(re.fullmatch(r"[A-Za-z]{1,25}", v)))
        if first is None or last is None:
            return self._fallback.teacher_name()
        return (first, last)

    def teacher_email(self, first: str, last: str) -> str:
        # Same reasoning and same Fix 2 retry-with-different-candidates
        # pattern as guardian_email/student_email -- never touches the API.
        for _ in range(_MAX_EMAIL_ATTEMPTS):
            candidate = self._fallback.teacher_email(first, last)
            if is_valid_staff_email(candidate, self._staff_email_domain):
                self._stats["fallback_served"] += 1
                return candidate
            self._stats["values_rejected_by_validation"] += 1
        self._stats["fallback_served"] += 1
        return f"teacher.contact{self._rng.randint(1000, 9999)}@{self._staff_email_domain}"

    def student_email(
        self, first: str, last: str, student_number: str, *, attempt: int = 0
    ) -> str:
        # Same reasoning as guardian_email: the domain/format is a fixed
        # convention, not something worth asking the model to produce.
        for offset in range(_MAX_EMAIL_ATTEMPTS):
            candidate = self._fallback.student_email(
                first, last, student_number, attempt=attempt + offset
            )
            if is_valid_student_email(candidate, self._student_email_domain):
                self._stats["fallback_served"] += 1
                return candidate
            self._stats["values_rejected_by_validation"] += 1
        self._stats["fallback_served"] += 1
        digits = re.sub(r"\D", "", student_number or "") or _random_digits(self._rng, 6)
        return f"student.contact{digits}@{self._student_email_domain}"

    # -- internal: queue management -----------------------------------------

    def _take(self, key: str, validator) -> str | None:
        """Pop one validated value from ``key``'s queue, refilling if empty.

        Returns ``None`` (never raises) if a value could not be produced --
        callers are expected to fall back to the canned generator in that
        case. Every value taken here has already passed ``validator``; values
        that fail validation are dropped during refill and counted in
        ``values_rejected_by_validation``.
        """

        queue = self._queues[key]
        if not queue:
            self._refill(key)
            queue = self._queues[key]

        while queue:
            candidate = queue.pop(0)
            if validator(candidate):
                self._stats["ai_served"] += 1
                return candidate
            self._stats["values_rejected_by_validation"] += 1
            # Try the next queued candidate before giving up entirely.

        self._stats["fallback_served"] += 1
        return None

    def _refill(self, key: str) -> None:
        """Fetch a fresh batch for ``key`` from the API, best-effort.

        Any failure here (import error, missing key, HTTP error, timeout,
        rate limit, malformed JSON) is caught and logged; the queue is simply
        left empty, and ``_take`` degrades to the canned generator. This
        method never raises.
        """

        if not self._api_key:
            return

        try:
            client = self._get_client()
            if client is None:
                return
            values = self._request_batch(client, key)
        except Exception:  # noqa: BLE001 - degrade on literally any failure
            logger.warning(
                "AnthropicContentGenerator: batch request for %r failed; "
                "degrading to canned content for this refill.",
                key,
                exc_info=True,
            )
            self._stats["batch_failures"] += 1
            return

        self._stats["batch_requests"] += 1
        self._queues[key].extend(values)

    def _get_client(self):
        """Lazily construct (and cache) the ``anthropic.Anthropic`` client.

        The import is inside this method, not at module scope, so importing
        ``drift_engine.content`` never requires the ``anthropic`` package to
        be installed. Returns ``None`` (never raises) if the import or
        construction fails.
        """

        if self._client is not None:
            return self._client
        try:
            import anthropic  # noqa: PLC0415 - deliberately lazy, see docstring
        except ImportError:
            logger.warning(
                "AnthropicContentGenerator: 'anthropic' package not installed; "
                "degrading to canned content."
            )
            return None
        try:
            self._client = anthropic.Anthropic(api_key=self._api_key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "AnthropicContentGenerator: failed to construct anthropic client; "
                "degrading to canned content.",
                exc_info=True,
            )
            return None
        return self._client

    def _request_batch(self, client, key: str) -> list[str]:
        """One batched API call for ``key``, returning a list of raw strings.

        Asks for strict JSON so the response can be parsed defensively.
        Parsing/validation failures here are handled by the caller
        (``_refill``), which treats any exception as "no values this time."
        """

        prompt = _PROMPTS[key](self._batch_size)
        response = client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in getattr(response, "content", []) if getattr(block, "type", "") == "text"
        )
        return _parse_json_array(text, key)


def _parse_json_array(text: str, expected_key: str) -> list[str]:
    """Defensively parse a JSON object of the form ``{"<key>": [...]}``.

    Strips markdown code fences if the model wrapped the JSON in one (a
    common failure mode even under an explicit "return only JSON"
    instruction). Raises ``ValueError`` on anything that doesn't parse to
    the expected shape -- the caller treats that as a batch failure and
    degrades to canned content.
    """

    import json

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"```\s*$", "", stripped)
    data = json.loads(stripped)
    if not isinstance(data, dict) or expected_key not in data:
        raise ValueError(f"expected JSON object with key {expected_key!r}, got {data!r}")
    values = data[expected_key]
    if not isinstance(values, list):
        raise ValueError(f"expected a list under {expected_key!r}, got {type(values)!r}")
    return [str(v).strip() for v in values if isinstance(v, (str, int, float))]


_PROMPTS: dict[str, "callable"] = {
    _MIDDLE_NAME_KEY: lambda n: (
        f"Generate {n} plausible American middle names for K-12 students, as a "
        'strict JSON object: {"middle_names": ["Name1", "Name2", ...]}. Each '
        "name must be a single alphabetic word (no spaces, hyphens, initials, "
        "or punctuation), between 2 and 20 letters. Return only the JSON, "
        "nothing else."
    ),
    _GIVEN_NAME_KEY: lambda n: (
        f"Generate {n} plausible American first names (a mix of ages/genders, "
        'suitable for parents/guardians or teachers), as a strict JSON object: '
        '{"given_names": ["Name1", "Name2", ...]}. Each name must be a single '
        "alphabetic word, no punctuation. Return only the JSON, nothing else."
    ),
    _SURNAME_KEY: lambda n: (
        f"Generate {n} plausible American surnames, as a strict JSON object: "
        '{"surnames": ["Name1", "Name2", ...]}. Each surname must be a single '
        "alphabetic word, no punctuation, no spaces. Return only the JSON, "
        "nothing else."
    ),
    _PHONE_KEY: lambda n: (
        f"Generate {n} plausible US phone numbers for the Tulsa, Oklahoma area, "
        'as a strict JSON object: {"phones": ["9185551234", ...]}. Each value '
        "must be exactly 10 digits, no punctuation, no country code, no letters. "
        "Return only the JSON, nothing else."
    ),
}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_content_generator(
    rng: random.Random,
    *,
    canned_only: bool | None = None,
    staff_email_domain: str | None = None,
    student_email_domain: str | None = None,
    area_codes: "tuple[str, ...] | None" = None,
) -> ContentGenerator:
    """Return the content generator this run should use.

    Resolution order:

    1. ``canned_only=True`` explicitly passed -> canned.
    2. ``DRIFT_CONTENT_CANNED_ONLY=1`` in the environment -> canned.
    3. No ``ANTHROPIC_API_KEY`` in the environment -> canned.
    4. Otherwise -> ``AnthropicContentGenerator`` (itself wrapping a canned
       fallback for any failure mode).

    This is the single call site a future swap-out would change (see the
    module docstring) -- everything else in the engine calls this function,
    never a concrete class, to obtain a ``ContentGenerator``.

    ``staff_email_domain``/``student_email_domain``/``area_codes`` are the
    Fix 7 per-district overrides (see ``DistrictConfig`` in ``config.py``).
    Passing ``None`` for any of them (the default) means "use this module's
    built-in default" (this project's one real sandbox district, Tulsa
    replica) -- so an existing caller that only passes ``canned_only`` (or
    nothing at all) sees no behaviour change. A caller drifting a second
    district should pass that district's own
    ``staff_email_domain``/``student_email_domain``/``area_codes`` here so
    its generated emails and phone numbers actually belong to ITS data, not
    Tulsa's.
    """

    kwargs = dict(
        staff_email_domain=staff_email_domain,
        student_email_domain=student_email_domain,
        area_codes=area_codes,
    )

    if canned_only is True:
        logger.info("content: using CannedContentGenerator (canned_only=True requested by caller).")
        return CannedContentGenerator(rng, **kwargs)

    if os.environ.get(_CANNED_ONLY_ENV_VAR, "") == "1":
        logger.info(
            "content: using CannedContentGenerator (%s=1 in environment).",
            _CANNED_ONLY_ENV_VAR,
        )
        return CannedContentGenerator(rng, **kwargs)

    api_key = os.environ.get(_API_KEY_ENV_VAR, "")
    if not api_key:
        logger.info(
            "content: using CannedContentGenerator (no %s configured).",
            _API_KEY_ENV_VAR,
        )
        return CannedContentGenerator(rng, **kwargs)

    model = os.environ.get(_MODEL_ENV_VAR, _DEFAULT_MODEL)
    logger.info(
        "content: using AnthropicContentGenerator (model=%s), with canned fallback "
        "for any API failure.",
        model,
    )
    return AnthropicContentGenerator(rng, api_key=api_key, model=model, **kwargs)
