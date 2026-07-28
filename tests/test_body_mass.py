"""Body mass: HLTH-04 to HLTH-16, and the wellness read behind them.

The organising fact for this suite is that the live wellness feed carries no
weight at all — a 21 day read on 28 July 2026 returned `weight` null on every
populated day. So the empty case is not an edge case here, it is the current
production state, and it is asserted first.

Every threshold comes from the PRD's weight trend confidence table. There is one
test per row of it, named after the row.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.agent import interruptions, naturalness, prompt
from coach.health import bodymass, breaks, trend, wellness

REPO = Path(__file__).resolve().parents[1]
DUBAI = ZoneInfo("Asia/Dubai")
TODAY = date(2026, 7, 28)


class FakeIntervals:
    """A wellness feed that returns what the test hands it."""

    def __init__(self, rows: list[dict] | Exception):
        self.rows = rows
        self.calls: list[tuple[date, date]] = []

    def wellness(self, oldest: date, newest: date) -> list[dict]:
        self.calls.append((oldest, newest))
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows


def day(offset: int) -> date:
    """A date `offset` days before TODAY. Negative offsets are in the past."""
    return TODAY - timedelta(days=offset)


def seed(conn: psycopg.Connection, points: list[tuple[int, float]]) -> None:
    """Record readings oldest first, through the real ingest path.

    Oldest first matters: HLTH-11 tests each reading against the pattern the
    earlier ones establish, so seeding out of order would exercise a code path
    that never happens.
    """
    for offset, kg in sorted(points, reverse=True):
        bodymass.record(conn, day(offset), Decimal(str(kg)))


def wellness_row(offset: int, **fields: object) -> dict:
    return {"id": day(offset).isoformat(), **fields}


# --- the empty case, which is production ------------------------------------


def test_no_readings_produces_no_claims_and_no_error(conn: psycopg.Connection) -> None:
    """The live account, as of 28 July 2026. Nothing feeds weight."""
    fitted = trend.fit(conn, TODAY)
    claims = trend.Claims.of(fitted)

    assert fitted.n == 0
    assert fitted.slope_kg_per_week is None
    assert not any(
        [
            claims.may_report_reading,
            claims.may_state_direction,
            claims.may_quote_rate,
            claims.may_call_plateau,
            claims.may_arbitrate_energy_balance,
        ]
    )


def test_an_empty_series_still_tells_the_coach_the_feed_is_silent(
    conn: psycopg.Connection,
) -> None:
    """Silence would otherwise read as a stable weight.

    HLTH-15 bars body mass from the generic staleness block, so this block is the
    only thing in the prompt saying the feed has never delivered.
    """
    rendered = bodymass.context(conn, TODAY)
    assert "No readings have arrived" in rendered
    assert "do not raise the subject" in rendered

    assembled = prompt.assemble(conn, datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI), tz=DUBAI)
    assert "body_mass" in assembled.names()


def test_body_mass_is_excluded_from_the_generic_staleness_block(
    conn: psycopg.Connection,
) -> None:
    """HLTH-15: the staleness mechanism never emits a body mass mention.

    The feed row is still maintained for OBS-05. What changes is who may speak
    about it.
    """
    now = datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI)
    stale = prompt.render_staleness(conn, now)

    assert "body_mass" not in stale
    assert "wellness" in stale  # the others are still surfaced

    with conn.cursor() as cur:
        cur.execute("select stale_after_hours from feeds where name = 'body_mass'")
        assert cur.fetchone()["stale_after_hours"] == 24 * bodymass.GAP_DAYS


def test_an_empty_series_is_not_a_gap(conn: psycopg.Connection) -> None:
    """A gap presupposes a reading. Never having had one is feed staleness.

    Otherwise the coach asks the athlete why he stopped doing something he was
    never able to start.
    """
    window = bodymass.gap(conn, TODAY)
    assert window.last_on is None
    assert not window.open
    assert bodymass.candidates(conn, TODAY) == []


# --- HLTH-04 and the wellness read ------------------------------------------


def test_weight_from_wellness_becomes_a_reading(conn: psycopg.Connection) -> None:
    """HLTH-04: body mass is read from intervals.icu wellness."""
    client = FakeIntervals([wellness_row(1, weight=84.2, restingHR=48)])
    result = wellness.sync(conn, client, TODAY)

    assert result.days == 1
    assert result.readings == 1
    with conn.cursor() as cur:
        cur.execute("select local_date, weight_kg, source, status from body_mass_readings")
        row = cur.fetchone()
    assert row["local_date"] == day(1)
    assert float(row["weight_kg"]) == 84.2
    assert (row["source"], row["status"]) == ("wellness", "accepted")


def test_the_live_shape_stores_recovery_and_no_weight(conn: psycopg.Connection) -> None:
    """The 28 July 2026 read: six fields populated, hrvSDNN and weight null.

    RECOV-02: a field the feed does not carry is recorded absent rather than
    defaulted. A zero here would mean the athlete did not sleep.
    """
    client = FakeIntervals(
        [
            wellness_row(
                1,
                sleepSecs=27000,
                sleepScore=76,
                sleepQuality=3,
                restingHR=64,
                hrv=45.9,
                readiness=77,
                respiration=14.2,
                spO2=96,
            )
        ]
    )
    result = wellness.sync(conn, client, TODAY)

    assert result.readings == 0
    assert result.fields_absent == {"weight", "hrvSDNN"}
    with conn.cursor() as cur:
        cur.execute("select * from wellness")
        row = cur.fetchone()
    assert row["sleep_secs"] == 27000
    assert row["resting_hr"] == 64
    assert row["weight_kg"] is None
    assert row["hrv_sdnn"] is None


def test_re_reading_a_fortnight_creates_no_duplicates(conn: psycopg.Connection) -> None:
    """RECOV-05: wellness reads are idempotent across overlapping ranges."""
    rows = [wellness_row(offset, weight=84 - offset * 0.05) for offset in range(14, 0, -1)]
    client = FakeIntervals(rows)

    wellness.sync(conn, client, TODAY)
    wellness.sync(conn, client, TODAY)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from wellness")
        assert cur.fetchone()["n"] == 14
        cur.execute("select count(*) as n from body_mass_readings")
        assert cur.fetchone()["n"] == 14


def test_a_failing_wellness_read_records_the_error_and_does_not_raise(
    conn: psycopg.Connection,
) -> None:
    """It runs beside activity ingest and must not take it down."""
    from coach.ingest import client as clientmod

    result = wellness.sync(conn, FakeIntervals(clientmod.IntervalsError("503")), TODAY)
    assert result.errors and "503" in result.errors[0]

    with conn.cursor() as cur:
        cur.execute("select last_error, last_success_at from feeds where name = 'wellness'")
        row = cur.fetchone()
    assert row["last_error"] == "503"
    assert row["last_success_at"] is None


def test_the_body_mass_feed_tracks_readings_not_fetches(conn: psycopg.Connection) -> None:
    """OBS-05 and HLTH-15: a successful fetch of no weight is not a reading.

    Collapsing the two feeds would let a healthy wellness endpoint hide a weight
    pipeline that has been dead for a month, which is the live situation.
    """
    wellness.sync(conn, FakeIntervals([wellness_row(1, restingHR=48)]), TODAY)

    with conn.cursor() as cur:
        cur.execute(
            "select name, last_success_at from feeds where name in ('wellness', 'body_mass')"
        )
        feeds = {r["name"]: r["last_success_at"] for r in cur.fetchall()}

    assert feeds["wellness"] is not None
    assert feeds["body_mass"] is None


def test_a_string_weight_is_absent_rather_than_invented(conn: psycopg.Connection) -> None:
    """An unparseable value is the same situation as a null."""
    wellness.sync(conn, FakeIntervals([wellness_row(1, weight="n/a")]), TODAY)
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from body_mass_readings")
        assert cur.fetchone()["n"] == 0


# --- HLTH-14: body fat is excluded from v1 ----------------------------------


def test_body_fat_is_not_stored_anywhere(conn: psycopg.Connection) -> None:
    """HLTH-14: excluded from v1, never quoted as a number, never a target.

    Asserted against the schema rather than against a code path, because a
    column nobody fills is how an excluded metric quietly becomes available.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public'"
        )
        columns = [f"{r['table_name']}.{r['column_name']}" for r in cur.fetchall()]

    offenders = [c for c in columns if re.search(r"body_?fat|fat_pct|fat_percent", c, re.I)]
    assert not offenders, offenders

    # And the wellness reader does not name it, so a payload carrying it is
    # dropped rather than kept in a column that appears later.
    assert not any("fat" in field.lower() for field in wellness.FIELDS)


def test_a_wellness_payload_carrying_body_fat_stores_no_body_fat_column(
    conn: psycopg.Connection,
) -> None:
    """It survives only inside `raw`, which nothing reads into a response."""
    wellness.sync(conn, FakeIntervals([wellness_row(1, weight=84.0, bodyFat=18.4)]), TODAY)
    with conn.cursor() as cur:
        cur.execute("select raw from wellness")
        assert cur.fetchone()["raw"]["bodyFat"] == 18.4


# --- the weight trend confidence table, one test per row --------------------


def test_row_1_a_single_reading_may_be_reported(conn: psycopg.Connection) -> None:
    """Report an individual reading: 1 reading, no span (HLTH-07)."""
    seed(conn, [(0, 84.0)])
    claims = trend.Claims.of(trend.fit(conn, TODAY))
    assert claims.may_report_reading
    assert not claims.may_state_direction


def test_row_2_two_readings_produce_no_trend_language(conn: psycopg.Connection) -> None:
    """HLTH-07's acceptance, exactly as written: two readings, no direction."""
    seed(conn, [(7, 84.6), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)
    claims = trend.Claims.of(fitted)

    assert fitted.n == 2
    assert not claims.may_state_direction
    rendered = trend.render(fitted, claims)
    assert "Not enough readings to state a direction" in rendered
    assert "falling" not in rendered and "rising" not in rendered


def test_row_2_three_readings_permit_a_direction_and_no_rate(conn: psycopg.Connection) -> None:
    """Any statement of direction: 3 readings, no span."""
    seed(conn, [(10, 84.6), (5, 84.3), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)
    claims = trend.Claims.of(fitted)

    assert claims.may_state_direction
    assert not claims.may_quote_rate
    assert fitted.slope_kg_per_week is not None and fitted.slope_kg_per_week < 0
    assert "a rate needs six readings across three weeks" in trend.render(fitted, claims)


def test_row_3_a_rate_needs_six_readings_across_three_weeks(conn: psycopg.Connection) -> None:
    """A rate of loss: 6 readings, 3 weeks (HLTH-08)."""
    five_over_three_weeks = [(21, 85.0), (16, 84.7), (11, 84.5), (6, 84.2), (0, 84.0)]
    seed(conn, five_over_three_weeks)
    assert not trend.Claims.of(trend.fit(conn, TODAY)).may_quote_rate

    seed(conn, [(3, 84.1)])  # the sixth
    assert trend.Claims.of(trend.fit(conn, TODAY)).may_quote_rate


def test_row_3_six_readings_inside_three_weeks_still_cannot_quote_a_rate(
    conn: psycopg.Connection,
) -> None:
    """Both bars, not either. Six readings in ten days is not three weeks."""
    seed(conn, [(10, 84.5), (8, 84.4), (6, 84.4), (4, 84.2), (2, 84.1), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)

    assert fitted.n == 6
    assert fitted.span_days == 10
    assert not trend.Claims.of(fitted).may_quote_rate


def test_row_3_any_rate_quoted_carries_an_uncertainty_range(conn: psycopg.Connection) -> None:
    """HLTH-08's acceptance: always a range, never a point estimate."""
    seed(conn, [(21, 85.0), (17, 84.8), (13, 84.6), (9, 84.4), (4, 84.2), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)
    claims = trend.Claims.of(fitted)

    assert claims.may_quote_rate
    assert fitted.rate_low is not None and fitted.rate_high is not None
    assert fitted.rate_low < fitted.slope_kg_per_week < fitted.rate_high

    rendered = trend.render(fitted, claims)
    assert "between" in rendered and "kg per week" in rendered
    assert "Quote the range, never a single figure." in rendered


def test_a_range_that_spans_zero_is_not_rendered_as_a_confident_rate(
    conn: psycopg.Connection,
) -> None:
    """HLTH-08's own output must not become the overclaim it prevents.

    An interval running from -0.42 to +0.16 rendered as unsigned magnitudes reads
    as "between 0.16 and 0.42 kg per week", which turns "we cannot tell which way"
    into a confident rate.
    """
    noisy = trend.Fit(
        n=8,
        as_of=TODAY,
        first_on=day(24),
        last_on=TODAY,
        weekdays=5,
        weeks_covered=4,
        slope_kg_per_week=Decimal("-0.13"),
        rate_low=Decimal("-0.42"),
        rate_high=Decimal("0.16"),
        latest_kg=Decimal("84.0"),
    )
    rendered = trend.render(noisy, trend.Claims.of(noisy))

    assert "not distinguishable from flat" in rendered
    assert "0.16" not in rendered and "0.42" not in rendered


def test_a_slope_of_almost_nothing_reads_as_flat(conn: psycopg.Connection) -> None:
    """ "Rising" at four grams a week is true and useless."""
    seed(conn, [(21, 84.0), (14, 84.01), (7, 84.0), (0, 84.01)])
    fitted = trend.fit(conn, TODAY)
    rendered = trend.render(fitted, trend.Claims.of(fitted))

    assert "holding steady" in rendered
    assert "rising" not in rendered and "falling" not in rendered


def test_row_4_three_flat_weeks_produce_no_programme_change(conn: psycopg.Connection) -> None:
    """HLTH-16's acceptance: plateau needs four weeks with weekly coverage."""
    seed(conn, [(20, 84.0), (17, 84.1), (13, 84.0), (9, 84.1), (5, 84.0), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)
    claims = trend.Claims.of(fitted)

    assert fitted.span_days == 20
    assert not claims.may_call_plateau
    assert "Do not call a plateau" in trend.render(fitted, claims)


def test_row_4_four_weeks_with_weekly_coverage_permit_a_plateau_call(
    conn: psycopg.Connection,
) -> None:
    seed(conn, [(27, 84.0), (21, 84.1), (14, 84.0), (7, 84.1), (0, 84.0)])
    claims = trend.Claims.of(trend.fit(conn, TODAY))
    assert claims.may_call_plateau


def test_row_4_a_missing_week_breaks_weekly_coverage(conn: psycopg.Connection) -> None:
    """Four weeks of span is not four weeks of coverage."""
    seed(conn, [(27, 84.0), (25, 84.1), (23, 84.0), (2, 84.1), (0, 84.0)])
    fitted = trend.fit(conn, TODAY)

    assert fitted.span_days == 27
    assert fitted.weeks_covered < trend.PLATEAU_MIN_WEEKS_COVERED
    assert not trend.Claims.of(fitted).may_call_plateau


def test_row_5_energy_balance_arbitration_uses_the_same_bar(conn: psycopg.Connection) -> None:
    """NUT-04: the trend arbitrates only once it meets the table's threshold."""
    seed(conn, [(20, 84.5), (13, 84.3), (6, 84.1), (0, 84.0)])
    assert not trend.Claims.of(trend.fit(conn, TODAY)).may_arbitrate_energy_balance

    seed(conn, [(27, 84.7)])
    claims = trend.Claims.of(trend.fit(conn, TODAY))
    assert claims.may_arbitrate_energy_balance
    assert claims.may_arbitrate_energy_balance == claims.may_call_plateau


def test_no_other_module_states_its_own_reading_threshold() -> None:
    """The PRD: "no requirement outside it may state its own bar".

    The thresholds live in one module. A comparison against a reading count
    anywhere else is the drift this test exists to catch.
    """
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        if path.name == "trend.py":
            continue
        for lineno, text in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bn\s*[<>]=?\s*\d|readings\s*[<>]=?\s*\d", text):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {text.strip()}")
    assert not offenders, offenders


# --- HLTH-06: the fit itself -------------------------------------------------


def test_irregular_spacing_fits_without_special_casing(conn: psycopg.Connection) -> None:
    """HLTH-05 makes the series irregular by design, so HLTH-06 must tolerate it."""
    seed(conn, [(26, 85.0), (25, 84.9), (18, 84.6), (11, 84.3), (10, 84.3), (2, 83.9)])
    fitted = trend.fit(conn, TODAY)

    assert fitted.n == 6
    assert fitted.slope_kg_per_week is not None
    # Roughly a quarter kilo a week down, over 24 days from 85.0 to 83.9.
    assert Decimal("-0.40") < fitted.slope_kg_per_week < Decimal("-0.20")


def test_a_fortnight_gap_degrades_confidence_without_breaking_the_fit(
    conn: psycopg.Connection,
) -> None:
    """HLTH-06's acceptance, exactly as written."""
    seed(conn, [(27, 85.0), (25, 84.9), (23, 84.8), (21, 84.7)])
    gapped = trend.fit(conn, TODAY)

    assert gapped.n == 4
    assert gapped.slope_kg_per_week is not None  # it did not break
    # It degraded: four readings inside one week of a 28 day window cannot
    # support a rate or a plateau however tidy the line through them is.
    claims = trend.Claims.of(gapped)
    assert not claims.may_quote_rate
    assert not claims.may_call_plateau


def test_the_fit_weights_recent_readings_more_heavily(conn: psycopg.Connection) -> None:
    """The half life is what makes a stale reading count for less than today's.

    A series that is flat for three weeks and then falls should read as falling,
    which an unweighted fit through the whole window would understate.
    """
    seed(conn, [(27, 85.0), (24, 85.0), (21, 85.0), (18, 85.0), (14, 85.0)])
    flat = trend.fit(conn, TODAY)
    seed(conn, [(4, 84.4), (2, 84.2), (0, 84.0)])
    recent = trend.fit(conn, TODAY)

    assert abs(flat.slope_kg_per_week) < Decimal("0.05")
    assert recent.slope_kg_per_week < Decimal("-0.20")


def test_the_trend_refits_as_each_reading_lands(conn: psycopg.Connection) -> None:
    """HLTH-06: refitted as each reading lands, not on a nightly schedule."""
    seed(conn, [(14, 85.0), (10, 84.8), (6, 84.6)])
    before = trend.fit(conn, TODAY).slope_kg_per_week

    seed(conn, [(0, 83.8)])
    after = trend.fit(conn, TODAY).slope_kg_per_week

    assert after < before


# --- HLTH-10: weekday bias ---------------------------------------------------


def test_readings_all_on_one_weekday_are_flagged(conn: psycopg.Connection) -> None:
    """HLTH-10: a single weekday sampling pattern is detected internally."""
    seed(conn, [(21, 85.0), (14, 84.7), (7, 84.4), (0, 84.1)])  # all the same weekday
    fitted = trend.fit(conn, TODAY)

    assert fitted.weekdays == 1
    assert fitted.weekday_bias
    assert "same weekday" in trend.render(fitted, trend.Claims.of(fitted))


def test_two_readings_on_one_weekday_are_not_a_pattern(conn: psycopg.Connection) -> None:
    """Below three this is a coincidence, and flagging it would fire immediately."""
    seed(conn, [(7, 84.4), (0, 84.1)])
    assert not trend.fit(conn, TODAY).weekday_bias


def test_mixed_weekdays_are_not_flagged(conn: psycopg.Connection) -> None:
    seed(conn, [(20, 85.0), (13, 84.7), (5, 84.4), (0, 84.1)])
    assert not trend.fit(conn, TODAY).weekday_bias


# --- HLTH-11: the outlier gate ----------------------------------------------


def test_an_outlier_is_held_out_of_the_trend(conn: psycopg.Connection) -> None:
    """HLTH-11: confirmed once before it enters the trend."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    before = trend.fit(conn, TODAY).slope_kg_per_week

    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))

    assert recorded.held_for_confirmation
    assert recorded.outlier_delta is not None and recorded.outlier_delta > 5
    assert trend.fit(conn, TODAY).slope_kg_per_week == before  # it did not enter


def test_an_ordinary_fluctuation_is_not_an_outlier(conn: psycopg.Connection) -> None:
    """It fires on a different person on the scale, not on a Tuesday."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    assert not bodymass.record(conn, TODAY, Decimal("84.5")).held_for_confirmation


def test_nothing_is_an_outlier_before_a_pattern_exists(conn: psycopg.Connection) -> None:
    """With no established pattern there is nothing to be outside of."""
    assert not bodymass.record(conn, day(4), Decimal("84.0")).held_for_confirmation
    assert not bodymass.record(conn, day(2), Decimal("91.0")).held_for_confirmation


def test_confirming_an_outlier_lets_it_into_the_trend(conn: psycopg.Connection) -> None:
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))
    before = trend.fit(conn, TODAY).n

    assert bodymass.resolve(conn, recorded.reading_id, accepted=True) == "accepted"
    assert trend.fit(conn, TODAY).n == before + 1


def test_rejecting_an_outlier_keeps_it_out_permanently(conn: psycopg.Connection) -> None:
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))
    bodymass.resolve(conn, recorded.reading_id, accepted=False)

    assert bodymass.pending_confirmation(conn) is None
    assert trend.fit(conn, TODAY).n == 4


def test_a_resync_does_not_ask_about_a_confirmed_reading_twice(
    conn: psycopg.Connection,
) -> None:
    """HLTH-11 confirms once, and RECOV-05 means the same day is read repeatedly."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))
    bodymass.resolve(conn, recorded.reading_id, accepted=True)

    again = bodymass.record(conn, TODAY, Decimal("89.5"))

    assert again.status == "accepted"
    assert bodymass.pending_confirmation(conn) is None


def test_a_rejected_reading_is_not_re_offered_on_every_resync(
    conn: psycopg.Connection,
) -> None:
    """The same trap as the confirmed case, and easier to miss.

    A rejected outlier that reverted to the fresh verdict on resync would be
    offered again every hour, for as long as the day stayed in the window.
    """
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))
    bodymass.resolve(conn, recorded.reading_id, accepted=False)

    for _ in range(3):
        bodymass.record(conn, TODAY, Decimal("89.5"))

    assert bodymass.pending_confirmation(conn) is None
    assert bodymass.candidates(conn, TODAY) == []


def test_a_corrected_value_is_a_new_reading_and_is_re_tested(
    conn: psycopg.Connection,
) -> None:
    """An answer given about the old number says nothing about the new one."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    recorded = bodymass.record(conn, TODAY, Decimal("89.5"))
    bodymass.resolve(conn, recorded.reading_id, accepted=False)

    corrected = bodymass.record(conn, TODAY, Decimal("91.2"))
    assert corrected.held_for_confirmation


def test_an_unanswered_outlier_is_asked_about_once_and_then_left_alone(
    conn: psycopg.Connection,
) -> None:
    """HLTH-11: "confirmed once, without interrogation".

    If the athlete never answers, the reading stays out of the trend rather than
    the question being repeated. That is the failure-safe direction: an
    unconfirmed outlier outside the fit distorts nothing.
    """
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    held = bodymass.record(conn, TODAY, Decimal("89.5"))
    now = datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI)

    offered = bodymass.candidates(conn, TODAY)
    assert [c.kind for c in offered] == ["outlier_confirmation"]
    assert interruptions.claim(conn, offered, now) is not None

    assert bodymass.candidates(conn, TODAY) == []
    assert bodymass.pending_confirmation(conn)["id"] == held.reading_id
    assert trend.fit(conn, TODAY).n == 4  # still out of the fit


def test_a_reading_is_tested_against_the_others_not_against_itself(
    conn: psycopg.Connection,
) -> None:
    """The baseline excludes the day under test, so a resync is stable."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    first = bodymass.record(conn, TODAY, Decimal("89.5"))
    second = bodymass.record(conn, TODAY, Decimal("89.5"))
    assert first.status == second.status == "pending_confirmation"


def test_a_held_reading_is_offered_as_one_interruption(conn: psycopg.Connection) -> None:
    """HLTH-11 asks once, and CHAT-11 decides whether it gets the budget."""
    seed(conn, [(12, 84.2), (9, 84.1), (6, 84.0), (3, 83.9)])
    bodymass.record(conn, TODAY, Decimal("89.5"))

    offered = bodymass.candidates(conn, TODAY)
    assert [c.kind for c in offered] == ["outlier_confirmation"]


# --- HLTH-12, HLTH-15: the gap ----------------------------------------------


def test_a_gap_produces_exactly_one_mention(conn: psycopg.Connection) -> None:
    """HLTH-15's acceptance: a gap of any length produces exactly one mention."""
    seed(conn, [(40, 84.0)])
    now = datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI)

    first = bodymass.candidates(conn, TODAY)
    assert [c.kind for c in first] == ["body_mass_gap"]
    assert interruptions.claim(conn, first, now) is not None

    # Every later conversation, for as long as the gap runs.
    for extra_days in (1, 7, 30):
        later = TODAY + timedelta(days=extra_days)
        assert bodymass.candidates(conn, later) == []


def test_a_reading_resets_the_counter(conn: psycopg.Connection) -> None:
    """HLTH-15: "not again until a reading resets the counter"."""
    seed(conn, [(40, 84.0)])
    now = datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI)
    interruptions.claim(conn, bodymass.candidates(conn, TODAY), now)
    assert bodymass.candidates(conn, TODAY) == []

    bodymass.record(conn, day(20), Decimal("83.6"))
    assert [c.kind for c in bodymass.candidates(conn, TODAY)] == ["body_mass_gap"]


def test_a_gap_inside_the_threshold_says_nothing(conn: psycopg.Connection) -> None:
    seed(conn, [(11, 84.0)])
    assert bodymass.candidates(conn, TODAY) == []
    assert bodymass.gap(conn, TODAY).days == 11


def test_a_held_reading_still_resets_the_counter(conn: psycopg.Connection) -> None:
    """The athlete stood on the scale. Telling him he has not would be wrong."""
    seed(conn, [(30, 84.2), (28, 84.1), (26, 84.0), (24, 83.9)])
    held = bodymass.record(conn, day(1), Decimal("89.5"))

    assert held.held_for_confirmation
    assert bodymass.gap(conn, TODAY).days == 1
    assert "body_mass_gap" not in [c.kind for c in bodymass.candidates(conn, TODAY)]


def test_missed_readings_never_reach_an_adherence_figure(conn: psycopg.Connection) -> None:
    """HLTH-12: missed readings are never a lapse and never count against adherence.

    Asserted structurally: the capture rate is computed and reported, and nothing
    writes it into `rollups.adherence_rate`, which is training adherence alone.
    """
    seed(conn, [(40, 84.0)])
    bodymass.recompute(conn, TODAY)

    with conn.cursor() as cur:
        cur.execute("select adherence_rate from rollups where as_of = %s", (TODAY,))
        row = cur.fetchone()
    assert row is not None
    assert row["adherence_rate"] is None

    source = (REPO / "src" / "coach" / "health" / "bodymass.py").read_text()
    assert "adherence" not in source.replace("never count against adherence", "").replace(
        "into an adherence figure", ""
    )


# --- HLTH-13: breaks ---------------------------------------------------------


def test_a_break_suppresses_the_weigh_in_prompt(conn: psycopg.Connection) -> None:
    """HLTH-13: a holiday produces no prompts and no catch up remarks."""
    seed(conn, [(40, 84.0)])
    breaks.create(conn, "holiday", starts_on=day(20), ends_on=day(-2))

    assert bodymass.gap(conn, TODAY).open
    assert bodymass.gap(conn, TODAY).suppressed_by_break
    assert bodymass.candidates(conn, TODAY) == []


def test_the_prompt_returns_after_the_break_ends(conn: psycopg.Connection) -> None:
    """The trend resumes on return; the gap is not backfilled or commented on."""
    seed(conn, [(40, 84.0)])
    breaks.create(conn, "holiday", starts_on=day(30), ends_on=day(1))

    assert bodymass.candidates(conn, TODAY - timedelta(days=5)) == []
    assert [c.kind for c in bodymass.candidates(conn, TODAY)] == ["body_mass_gap"]


def test_an_illness_break_does_not_end_on_its_end_date(conn: psycopg.Connection) -> None:
    """BREAK-04, honoured at P04's size so P10 cannot implement the resume."""
    seed(conn, [(40, 84.0)])
    break_id = breaks.create(conn, "illness", starts_on=day(20), ends_on=day(10))

    assert breaks.active_on(conn, TODAY) is not None
    breaks.end(conn, break_id)
    assert breaks.active_on(conn, TODAY) is None


# --- HLTH-05: a target rate, not a schedule ---------------------------------


def test_capture_rate_is_measured_against_a_configured_target(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HLTH-05: configuration expresses a target rate, not a schedule."""
    seed(conn, [(24, 84.4), (20, 84.3), (17, 84.2), (13, 84.1), (9, 84.0), (5, 83.9)])

    monkeypatch.setenv("COACH_WEIGH_IN_TARGET_PER_WEEK", "2.5")
    below = bodymass.capture_rate(conn, TODAY)
    assert below.readings == 6
    assert below.per_week == Decimal("1.5")
    assert not below.at_target

    monkeypatch.setenv("COACH_WEIGH_IN_TARGET_PER_WEEK", "1.0")
    assert bodymass.capture_rate(conn, TODAY).at_target


def test_consecutive_readings_are_counted_not_penalised(conn: psycopg.Connection) -> None:
    """HLTH-05 prefers non consecutive days; HLTH-12 forbids treating it as a lapse."""
    seed(conn, [(10, 84.2), (9, 84.2), (5, 84.0), (0, 83.9)])
    capture = bodymass.capture_rate(conn, TODAY)
    assert capture.consecutive_pairs == 1
    assert capture.readings == 4


def test_no_weigh_in_day_is_named_anywhere() -> None:
    """HLTH-05: there is no fixed weigh in day, so no code may name one."""
    days = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
    for name in ("bodymass.py", "trend.py", "wellness.py"):
        text = (REPO / "src" / "coach" / "health" / name).read_text()
        assert not days.search(text), f"{name} names a weekday"


# --- HLTH-09: what the coach may say ----------------------------------------


def test_the_readings_never_enter_the_prompt(conn: psycopg.Connection) -> None:
    """HLTH-09 costs nothing to obey when there is nothing to compare."""
    seed(conn, [(21, 85.0), (17, 84.8), (13, 84.6), (9, 84.4), (4, 84.2), (0, 84.0)])
    rendered = bodymass.context(conn, TODAY)

    for _, kg in [(21, 85.0), (17, 84.8), (13, 84.6), (9, 84.4), (4, 84.2)]:
        assert str(kg) not in rendered
    assert "84.0" in rendered  # the latest, which HLTH-07 permits reporting


def test_comparing_two_readings_is_a_violation() -> None:
    assert naturalness.compares_individual_readings(
        "You were 84.2 kg on Monday and 83.6 kg on Thursday."
    )
    assert "HLTH-09" in " ".join(
        naturalness.violations("You were 84.2 kg on Monday and 83.6 kg on Thursday.")
    )


def test_reacting_to_one_reading_is_a_violation() -> None:
    for text in (
        "Your weight is up a bit this morning.",
        "The scale went up today, nothing to worry about.",
        "You've gained since yesterday's weigh-in.",
    ):
        assert naturalness.reacts_to_single_reading(text), text


def test_a_permitted_rate_range_is_not_a_violation() -> None:
    """HLTH-08's own output must not trip HLTH-09's check."""
    clean = "The trend's coming down between 0.30 and 0.45 kg per week, which is about right."
    assert not naturalness.compares_individual_readings(clean)
    assert not naturalness.reacts_to_single_reading(clean)
    assert naturalness.violations(clean) == []


def test_reporting_one_reading_on_request_is_not_a_violation() -> None:
    """HLTH-07 permits reporting a reading; HLTH-09 forbids reacting to it."""
    clean = "Last reading was 84.0 kg."
    assert naturalness.violations(clean) == []


# --- the rollup --------------------------------------------------------------


def test_the_trend_is_written_to_the_rollup_in_sql(conn: psycopg.Connection) -> None:
    """MEM-08: the model reads a fitted slope and a range, never the readings."""
    seed(conn, [(21, 85.0), (17, 84.8), (13, 84.6), (9, 84.4), (4, 84.2), (0, 84.0)])
    written = bodymass.recompute(conn, TODAY)

    assert written > 0
    with conn.cursor() as cur:
        cur.execute("select * from rollups where as_of = %s", (TODAY,))
        row = cur.fetchone()

    assert row["weight_reading_n"] == 6
    assert row["weight_trend_slope"] < 0
    assert row["weight_trend_low"] < row["weight_trend_slope"] < row["weight_trend_high"]
    assert row["weight_span_days"] == 21
    assert row["weight_weeks_covered"] == 4
    assert float(row["weight_latest_kg"]) == 84.0


def test_the_weight_rollup_does_not_disturb_the_load_rollup(conn: psycopg.Connection) -> None:
    """Two writers, disjoint columns, either can run without the other."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into rollups (as_of, load_7d, load_28d) values (%s, 320, 1180)", (TODAY,)
        )
    seed(conn, [(6, 84.2), (3, 84.1), (0, 84.0)])
    bodymass.recompute(conn, TODAY)

    with conn.cursor() as cur:
        cur.execute("select load_7d, weight_reading_n from rollups where as_of = %s", (TODAY,))
        row = cur.fetchone()
    assert float(row["load_7d"]) == 320
    assert row["weight_reading_n"] == 3


def test_a_late_arriving_reading_recomputes_the_days_after_it(
    conn: psycopg.Connection,
) -> None:
    """HLTH-06 refits as each reading lands, and a late reading changes history."""
    seed(conn, [(10, 84.4), (5, 84.2), (0, 84.0)])
    bodymass.recompute(conn, TODAY)
    with conn.cursor() as cur:
        cur.execute("select weight_reading_n from rollups where as_of = %s", (day(5),))
        before = cur.fetchone()["weight_reading_n"]

    bodymass.record(conn, day(7), Decimal("84.3"))
    bodymass.recompute(conn, TODAY)
    with conn.cursor() as cur:
        cur.execute("select weight_reading_n from rollups where as_of = %s", (day(5),))
        after = cur.fetchone()["weight_reading_n"]

    assert after == before + 1


# --- the prompt --------------------------------------------------------------


def test_the_body_mass_block_reaches_the_prompt(conn: psycopg.Connection) -> None:
    seed(conn, [(10, 84.4), (5, 84.2), (0, 84.0)])
    assembled = prompt.assemble(conn, datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI), tz=DUBAI)

    assert "body_mass" in assembled.names()
    rendered = assembled.render()
    assert "BODY MASS" in rendered
    assert "never compare two readings" in rendered


def test_the_block_states_permissions_rather_than_instructions(
    conn: psycopg.Connection,
) -> None:
    """Permission to state a direction is not a reason to mention weight."""
    seed(conn, [(10, 84.4), (5, 84.2), (0, 84.0)])
    rendered = bodymass.context(conn, TODAY)
    assert "You may" in rendered or "you may" in rendered
    assert "Tell the athlete" not in rendered
