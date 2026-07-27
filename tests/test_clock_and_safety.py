"""P01 acceptance: time and locale (TZ-01/02/03) and no diagnosis (SAFE-05)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from coach import clock
from coach.agent import naturalness

DUBAI = ZoneInfo("Asia/Dubai")  # UTC+4, no DST
LONDON = ZoneInfo("Europe/London")  # DST, for the offset test
TOKYO = ZoneInfo("Asia/Tokyo")  # UTC+9, stands in for an upstream feed's zone


# --- TZ-01: local day boundaries -------------------------------------------


def test_a_late_ride_belongs_to_the_local_day() -> None:
    """TZ-01: a session ridden at 23:30 local is attributed to that local day."""
    ridden = datetime(2026, 7, 20, 23, 30, tzinfo=DUBAI)
    assert clock.local_day(ridden, DUBAI) == date(2026, 7, 20)
    # The same moment is already the 21st in UTC, which is the trap.
    assert clock.to_utc(ridden).date() == date(2026, 7, 20)
    assert clock.to_utc(datetime(2026, 7, 20, 23, 30, tzinfo=DUBAI)).hour == 19


def test_an_early_morning_ride_belongs_to_the_local_day() -> None:
    ridden = datetime(2026, 7, 21, 0, 30, tzinfo=DUBAI)
    assert clock.local_day(ridden, DUBAI) == date(2026, 7, 21)
    assert clock.to_utc(ridden).date() == date(2026, 7, 20)


def test_day_bounds_cover_exactly_one_local_day() -> None:
    start, end = clock.day_bounds(date(2026, 7, 20), DUBAI)
    assert end - start == timedelta(days=1)
    assert clock.local_day(start, DUBAI) == date(2026, 7, 20)
    assert clock.local_day(end - timedelta(seconds=1), DUBAI) == date(2026, 7, 20)
    assert clock.local_day(end, DUBAI) == date(2026, 7, 21)


def test_the_training_week_runs_monday_to_sunday() -> None:
    """The Sunday review closes a week rather than sitting in the middle of one."""
    monday, sunday = clock.week_bounds(date(2026, 7, 22), DUBAI)  # a Wednesday
    assert monday == date(2026, 7, 20)
    assert sunday == date(2026, 7, 26)
    assert monday.weekday() == 0 and sunday.weekday() == 6


# --- TZ-02: stored UTC, rendered local -------------------------------------


def test_timestamps_are_stored_utc_and_rendered_local() -> None:
    moment = datetime(2026, 7, 20, 19, 30, tzinfo=UTC)
    assert clock.to_utc(moment).tzinfo is UTC
    assert "23:30" in clock.render(moment, DUBAI)
    assert "19:30" in clock.render(moment, UTC)


def test_a_naive_timestamp_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="naive"):
        clock.to_utc(datetime(2026, 7, 20, 23, 30))


def test_offsets_respect_daylight_saving() -> None:
    assert clock.utc_offset(LONDON, datetime(2026, 1, 15, 12, tzinfo=UTC)) == timedelta(0)
    assert clock.utc_offset(LONDON, datetime(2026, 7, 15, 12, tzinfo=UTC)) == timedelta(hours=1)
    assert clock.utc_offset(DUBAI, datetime(2026, 7, 15, 12, tzinfo=UTC)) == timedelta(hours=4)


# --- TZ-03: travel does not shift the week ---------------------------------


def test_upstream_timezone_does_not_govern() -> None:
    """TZ-03: the configured timezone governs regardless of upstream data.

    The same instant, delivered by a feed that stamps it Asia/Tokyo, still lands
    on the athlete's local day.
    """
    instant = datetime(2026, 7, 21, 4, 30, tzinfo=TOKYO)  # 2026-07-20 19:30 UTC
    assert clock.local_day(instant, DUBAI) == date(2026, 7, 20)
    assert clock.local_day(instant, TOKYO) == date(2026, 7, 21)


def test_travelling_does_not_move_the_week() -> None:
    """A ride taken while abroad still falls in the configured week."""
    abroad = datetime(2026, 7, 27, 8, 0, tzinfo=TOKYO)  # Monday 03:00 in Dubai
    configured_day = clock.local_day(abroad, DUBAI)
    assert clock.week_bounds(configured_day, DUBAI) == (date(2026, 7, 27), date(2026, 8, 2))


# --- SAFE-05: observe, do not diagnose -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "That sounds like patellar tendinitis.",
        "You have tendinopathy in that knee.",
        "That's bursitis, most likely.",
        "Probably tendinitis given how it presents.",
        "I'd say it's a meniscus tear.",
    ],
)
def test_diagnosis_is_flagged(text: str) -> None:
    assert naturalness.diagnoses(text)
    assert any("SAFE-05" in v for v in naturalness.violations(text))


@pytest.mark.parametrize(
    "text",
    [
        "The knee has been sore on the same interval three weeks running. Worth "
        "getting a physio to look at it before we load it again.",
        "Your resting heart rate is eight beats above your own baseline and has "
        "been for four days. If it stays there, see your GP.",
        "That's the third time the hip has come up. I'd get it looked at.",
    ],
)
def test_observations_with_a_referral_are_clean(text: str) -> None:
    """SAFE-05 wants observations plus a pointer to a clinician."""
    assert not naturalness.diagnoses(text)
    assert naturalness.refers_clinically(text)
    assert naturalness.violations(text) == []


def test_ordinary_coaching_is_not_flagged_as_diagnosis() -> None:
    assert not naturalness.diagnoses("Ride easy tomorrow, the legs need it.")
    assert not naturalness.diagnoses("Your threshold went up 12 watts.")
