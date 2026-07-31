"""Tests for drift_engine.cadence.

Cadence is pure and deterministic (no randomness, no I/O), so these tests
just check the fixed weekday -> bucket mapping and the weekend skip
behaviour, including across one real, known 2026 calendar week.
"""

from __future__ import annotations

from datetime import date

from drift_engine import cadence
from drift_engine.models import Bucket

# A real, known Monday-through-Sunday week in 2026, confirmed via
# datetime.date.weekday(): Jul 27 = Monday ... Aug 2 = Sunday.
MONDAY = date(2026, 7, 27)
TUESDAY = date(2026, 7, 28)
WEDNESDAY = date(2026, 7, 29)
THURSDAY = date(2026, 7, 30)
FRIDAY = date(2026, 7, 31)
SATURDAY = date(2026, 8, 1)
SUNDAY = date(2026, 8, 2)


def test_monday_is_small_daily_only():
    assert MONDAY.weekday() == 0
    plan = cadence.plan_for(MONDAY)
    assert not plan.skipped
    assert plan.buckets == (Bucket.SMALL_DAILY,)


def test_tuesday_stacks_big_student_on_small_daily():
    assert TUESDAY.weekday() == 1
    plan = cadence.plan_for(TUESDAY)
    assert not plan.skipped
    assert plan.buckets == (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT)
    # Stacking, not replacing: small daily must still be present.
    assert Bucket.SMALL_DAILY in plan.buckets


def test_wednesday_is_small_daily_only():
    assert WEDNESDAY.weekday() == 2
    plan = cadence.plan_for(WEDNESDAY)
    assert not plan.skipped
    assert plan.buckets == (Bucket.SMALL_DAILY,)


def test_thursday_stacks_big_student_on_small_daily():
    assert THURSDAY.weekday() == 3
    plan = cadence.plan_for(THURSDAY)
    assert not plan.skipped
    assert plan.buckets == (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT)
    assert Bucket.SMALL_DAILY in plan.buckets


def test_friday_stacks_big_teacher_on_small_daily():
    assert FRIDAY.weekday() == 4
    plan = cadence.plan_for(FRIDAY)
    assert not plan.skipped
    assert plan.buckets == (Bucket.SMALL_DAILY, Bucket.BIG_TEACHER)
    # Stacking, not replacing: small daily must still be present, and the
    # big bucket must be a teacher bucket, not a student bucket.
    assert Bucket.SMALL_DAILY in plan.buckets
    assert Bucket.BIG_STUDENT not in plan.buckets


def test_saturday_is_skipped():
    assert SATURDAY.weekday() == 5
    plan = cadence.plan_for(SATURDAY)
    assert plan.skipped
    assert plan.buckets == ()
    assert plan.reason


def test_sunday_is_skipped():
    assert SUNDAY.weekday() == 6
    plan = cadence.plan_for(SUNDAY)
    assert plan.skipped
    assert plan.buckets == ()
    assert plan.reason


def test_full_known_week_matches_expected_bucket_tuples():
    """One real 2026 week, day by day, pinned against the brief's table."""

    expected = {
        MONDAY: (Bucket.SMALL_DAILY,),
        TUESDAY: (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT),
        WEDNESDAY: (Bucket.SMALL_DAILY,),
        THURSDAY: (Bucket.SMALL_DAILY, Bucket.BIG_STUDENT),
        FRIDAY: (Bucket.SMALL_DAILY, Bucket.BIG_TEACHER),
        SATURDAY: (),
        SUNDAY: (),
    }
    for day, expected_buckets in expected.items():
        plan = cadence.plan_for(day)
        assert plan.buckets == expected_buckets, day
        assert plan.skipped == (expected_buckets == ()), day


def test_weekday_name_property_matches_calendar():
    assert cadence.plan_for(MONDAY).weekday_name == "Monday"
    assert cadence.plan_for(FRIDAY).weekday_name == "Friday"
    assert cadence.plan_for(SUNDAY).weekday_name == "Sunday"


def test_weekly_schedule_is_inspectable_and_covers_all_seven_days():
    assert set(cadence.WEEKLY_SCHEDULE.keys()) == set(range(7))
    # Big buckets always include small daily alongside them (stacking).
    for weekday, buckets in cadence.WEEKLY_SCHEDULE.items():
        if Bucket.BIG_STUDENT in buckets or Bucket.BIG_TEACHER in buckets:
            assert Bucket.SMALL_DAILY in buckets, weekday


def test_describe_week_renders_all_seven_days_and_bucket_names():
    text = cadence.describe_week()
    for name in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    ):
        assert name in text
    assert Bucket.SMALL_DAILY.value in text
    assert Bucket.BIG_STUDENT.value in text
    assert Bucket.BIG_TEACHER.value in text


def test_fixed_magnitude_constants_are_the_documented_values():
    """These are pinned, not-config constants (brief §4, §10) -- a change to
    any of these numbers should be a deliberate, reviewed code change."""

    assert cadence.SMALL_DAILY_CONTACT_FIELD_EDITS == 6
    assert cadence.SMALL_DAILY_STUDENT_FIELD_EDITS == 4
    assert cadence.BIG_STUDENT_ENROLLMENT_MOVES == 4
    assert cadence.BIG_STUDENT_CONTACTS_ADDED == 3
    assert cadence.BIG_STUDENT_CONTACTS_REMOVED == 2
    assert cadence.BIG_TEACHER_COTEACHER_CHANGES == 2
    assert cadence.BIG_TEACHER_SECTION_REASSIGNMENTS == 1
    assert cadence.BIG_TEACHER_NEW_TEACHERS == 1
