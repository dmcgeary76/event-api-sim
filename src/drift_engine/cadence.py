"""Pure, deterministic day-of-week bucket logic (project brief §4).

This module answers exactly one question -- "given a calendar date, which
change buckets apply today?" -- and nothing else. No randomness, no I/O, no
knowledge of CSVs or districts. That is deliberate: the brief treats the
weekly cadence as "rigid and predictable" (partners are told to expect
activity on this schedule), while the *selection* of which records get
touched is randomized elsewhere (see ``selection.py``). Keeping this split
literal at the module boundary means the schedule can be unit tested without
ever touching a CsvStack.

The fixed weekly table (Mon-Fri small daily; Tue/Thu also big-student;
Fri also big-teacher; weekends skipped) is defined once here as
``WEEKLY_SCHEDULE`` so it is inspectable and printable rather than buried in
if/elif branches.
"""

from __future__ import annotations

from datetime import date

from .models import Bucket, RunPlan

# ---------------------------------------------------------------------------
# Fixed magnitudes (project brief §4, §10).
#
# These are intentionally hard-coded constants, NOT configuration. The brief
# is explicit that there is no per-district volume knob -- the same magnitude
# and cadence applies to every sandbox district regardless of size (§10:
# "No volume/intensity knob -- magnitude is fixed regardless of district
# size."). If a future iteration needs different magnitudes, that is a
# deliberate code change to this module, not a config value someone can tune
# per district.
# ---------------------------------------------------------------------------

#: Small daily bucket (every weekday) -- contact field edits.
SMALL_DAILY_CONTACT_FIELD_EDITS = 6
#: Small daily bucket (every weekday) -- student field edits.
SMALL_DAILY_STUDENT_FIELD_EDITS = 4

#: Big student bucket (Tue/Thu) -- students moved between sections.
BIG_STUDENT_ENROLLMENT_MOVES = 4  # brief says "~4 students"
#: Big student bucket (Tue/Thu) -- new guardian contacts added.
BIG_STUDENT_CONTACTS_ADDED = 3
#: Big student bucket (Tue/Thu) -- guardian contacts removed.
BIG_STUDENT_CONTACTS_REMOVED = 2

#: Big teacher bucket (Fri) -- co-teacher (Teacher 2 id) changes.
BIG_TEACHER_COTEACHER_CHANGES = 2
#: Big teacher bucket (Fri) -- primary teacher reassignments on a section.
BIG_TEACHER_SECTION_REASSIGNMENTS = 1
#: Big teacher bucket (Fri) -- brand new teachers added to the district.
BIG_TEACHER_NEW_TEACHERS = 1


# ---------------------------------------------------------------------------
# The fixed weekly schedule.
#
# Keyed by Python's ``date.weekday()`` convention: Monday=0 ... Sunday=6.
# Big buckets STACK ON TOP of the small daily bucket -- they are appended
# after it in the tuple, never in place of it (brief §4: "The big buckets
# stack on top of the small daily bucket for that day -- they don't replace
# it."). Weekend entries are empty tuples; ``plan_for`` turns those into a
# skipped ``RunPlan`` rather than an empty-but-not-skipped one, but the
# mapping itself stays complete (including weekends) so it can be printed as
# a full seven-day table by ``describe_week``.
# ---------------------------------------------------------------------------

WEEKLY_SCHEDULE: dict[int, tuple[Bucket, ...]] = {
    0: (Bucket.SMALL_DAILY,),                      # Monday
    1: (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT),   # Tuesday
    2: (Bucket.SMALL_DAILY,),                      # Wednesday
    3: (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT),   # Thursday
    4: (Bucket.SMALL_DAILY, Bucket.BIG_TEACHER),   # Friday
    5: (),                                          # Saturday
    6: (),                                          # Sunday
}

_WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def plan_for(run_date: date) -> RunPlan:
    """Return the ``RunPlan`` for ``run_date`` based purely on its weekday.

    Weekends produce a skipped plan with an empty bucket tuple and a
    human-readable reason. Weekdays always include ``Bucket.SMALL_DAILY``,
    with the big buckets appended on top per ``WEEKLY_SCHEDULE``.
    """

    buckets = WEEKLY_SCHEDULE[run_date.weekday()]

    if not buckets:
        return RunPlan(
            run_date=run_date,
            buckets=(),
            skipped=True,
            reason=f"{_WEEKDAY_NAMES[run_date.weekday()]} is a weekend; no drift runs on weekends.",
        )

    return RunPlan(run_date=run_date, buckets=buckets)


def describe_week() -> str:
    """Render the fixed weekly schedule as a human-readable table.

    Used for docs and CLI output ("what does this engine do, and when") so
    the cadence never has to be re-derived by hand from the code.
    """

    lines = ["Weekday      Buckets"]
    for weekday, name in enumerate(_WEEKDAY_NAMES):
        buckets = WEEKLY_SCHEDULE[weekday]
        rendered = ", ".join(b.value for b in buckets) if buckets else "(skipped -- weekend)"
        lines.append(f"{name:<12} {rendered}")
    return "\n".join(lines)
