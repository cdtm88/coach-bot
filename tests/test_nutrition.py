"""Intake rollups and what may be concluded from them: NUT-01 to NUT-06.

The figures are computed in SQL (MEM-08), so these tests go through the database
rather than through a Python aggregate. That is the point: the requirement that
is easiest to get wrong — NUT-03's treatment of a day with nothing logged — is
a property of the grouping, and a test that summed the rows itself would agree
with a buggy implementation for the same reason the implementation was buggy.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import psycopg

from coach.health import nutrition
from coach.memory import facts as factmod


def _d(n: int) -> timedelta:
    return timedelta(days=n)


def log_day(conn: psycopg.Connection, day: date, kcal: int, protein_g: int) -> None:
    """One meal on a day, which is all these tests need to distinguish."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into meals (external_id, eaten_at, local_date, name, kcal, protein_g) "
            "values (%s, %s, %s, 'meal', %s, %s)",
            (f"{day}-1", f"{day}T12:00:00+04:00", day, kcal, protein_g),
        )


def set_target(conn: psycopg.Connection, grams: int) -> None:
    factmod.ratify(
        conn,
        key=nutrition.PROTEIN_TARGET_KEY,
        value=grams,
        provenance="stated",
        reason="test",
    )


# --- NUT-01: the windows -----------------------------------------------------


def test_seven_and_twenty_eight_day_averages_including_protein(conn: psycopg.Connection) -> None:
    """NUT-01's acceptance: the rollups match a manual calculation on seeded data."""
    as_of = date(2026, 7, 28)
    for offset, (kcal, protein) in enumerate([(2000, 100), (2400, 140), (2200, 120)]):
        log_day(conn, as_of - _d(offset), kcal, protein)

    week, month = nutrition.rollup(conn, as_of)

    assert week.days == 7 and month.days == 28
    assert week.logged_days == 3
    assert week.kcal == Decimal(2200)  # (2000 + 2400 + 2200) / 3
    assert week.protein_g == Decimal(120)


def test_a_day_outside_the_window_is_not_counted(conn: psycopg.Connection) -> None:
    """The window is inclusive of `as_of` and exactly `days` long."""
    as_of = date(2026, 7, 28)
    log_day(conn, as_of, 2000, 100)
    log_day(conn, as_of - _d(6), 3000, 200)  # inside a 7 day window
    log_day(conn, as_of - _d(7), 9999, 999)  # outside it

    week = nutrition.window(conn, as_of, 7)

    assert week.logged_days == 2
    assert week.kcal == Decimal(2500)


# --- NUT-03: gap days -------------------------------------------------------


def test_a_gap_day_does_not_depress_the_average(conn: psycopg.Connection) -> None:
    """NUT-03's acceptance, exactly as written.

    Two logged days at 2000 kcal are a 2000 kcal average whether the other five
    days of the week were logged or not. Counting them as zero would report 571.
    """
    as_of = date(2026, 7, 28)
    log_day(conn, as_of, 2000, 100)
    log_day(conn, as_of - _d(1), 2000, 100)

    week = nutrition.window(conn, as_of, 7)

    assert week.logged_days == 2
    assert week.kcal == Decimal(2000)


def test_coverage_reports_what_the_average_hides(conn: psycopg.Connection) -> None:
    """Excluding gap days is right and misleading on its own.

    Two well-fed days out of seven should not read as a well-fed week, so the
    logged-day count travels with the figure rather than being available
    separately to a caller who remembers to ask.
    """
    as_of = date(2026, 7, 28)
    log_day(conn, as_of, 2000, 100)
    log_day(conn, as_of - _d(1), 2000, 100)

    week = nutrition.window(conn, as_of, 7)

    assert week.coverage == Decimal(2) / Decimal(7)
    assert "averaged over 2 logged days" in nutrition.render(
        [week], nutrition.arbitration(conn, as_of)
    )


def test_a_window_with_nothing_logged_is_not_an_error(conn: psycopg.Connection) -> None:
    week = nutrition.window(conn, date(2026, 7, 28), 7)

    assert week.logged_days == 0
    assert week.kcal is None
    assert week.adherence is None


# --- NUT-02: the target lives in facts --------------------------------------


def test_changing_the_target_changes_adherence_without_a_deploy(
    conn: psycopg.Connection,
) -> None:
    """NUT-02's acceptance, exactly as written."""
    as_of = date(2026, 7, 28)
    for offset, protein in enumerate([90, 110, 130]):
        log_day(conn, as_of - _d(offset), 2000, protein)

    set_target(conn, 100)
    assert nutrition.window(conn, as_of, 7).days_meeting_target == 2

    set_target(conn, 120)
    assert nutrition.window(conn, as_of, 7).days_meeting_target == 1


def test_adherence_is_over_logged_days_not_the_window(conn: psycopg.Connection) -> None:
    """A day nobody logged is not a day the athlete missed the target."""
    as_of = date(2026, 7, 28)
    set_target(conn, 100)
    log_day(conn, as_of, 2000, 120)
    log_day(conn, as_of - _d(1), 2000, 80)

    week = nutrition.window(conn, as_of, 7)

    assert week.adherence == Decimal(1) / Decimal(2)


def test_no_target_means_no_adherence_rather_than_zero(conn: psycopg.Connection) -> None:
    """Never stated is not the same as never met."""
    as_of = date(2026, 7, 28)
    log_day(conn, as_of, 2000, 150)

    week = nutrition.window(conn, as_of, 7)

    assert week.target_g is None
    assert week.adherence is None


# --- NUT-04: who arbitrates -------------------------------------------------


def test_a_disagreement_in_the_first_three_weeks_settles_nothing(
    conn: psycopg.Connection,
) -> None:
    """NUT-04's acceptance: no programme change and no claim about which is right.

    The gate is the trend's, not this module's — `may_arbitrate_energy_balance`
    is the same weekly-coverage bar HLTH-16 uses for a plateau. With no readings
    at all the trend plainly cannot arbitrate, which is the state the athlete is
    in on day one.
    """
    as_of = date(2026, 7, 28)
    log_day(conn, as_of, 3500, 100)

    verdict = nutrition.arbitration(conn, as_of)

    assert verdict.may_arbitrate is False
    assert verdict.verdict == "no claim"
    assert "settles nothing" in nutrition.render([nutrition.window(conn, as_of, 7)], verdict)
