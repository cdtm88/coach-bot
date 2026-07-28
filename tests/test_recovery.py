"""Recovery: RECOV-01 to RECOV-06.

The shape of the live feed drives this suite. A 21 day read on 28 July 2026
returned 46 keys, 18 of them populated on at least one day:

    hrv, readiness, respiration, restingHR, sleepSecs, sleepScore, sleepQuality
                                                       13 of 22 days
    ctl, atl, ctlLoad, atlLoad, rampRate                22 of 22 days
    hrvSDNN, weight, bodyFat, locked, and 24 others     never

`atlLoad` was zero on eight days and non-zero on fourteen, which is what makes
RECOV-06's cross check a real distinction rather than a hypothetical one.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from coach.agent import prompt
from coach.health import recovery, wellness
from coach.ingest import review

REPO = Path(__file__).resolve().parents[1]
DUBAI = ZoneInfo("Asia/Dubai")
TODAY = date(2026, 7, 28)


def day(offset: int) -> date:
    return TODAY - timedelta(days=offset)


def wellness_row(offset: int, **fields: object) -> dict:
    return {"id": day(offset).isoformat(), **fields}


def steady(offset: int, **overrides: object) -> dict:
    """A day at the athlete's ordinary values, before any override.

    The values wobble with the offset rather than repeating. That is not
    decoration: a baseline with no variance has a standard deviation of zero, and
    a deviation standardised against zero scale is a division this code refuses
    to do. A flat fixture would therefore have tested the refusal path in every
    test that meant to exercise the happy one — which is exactly what the first
    run of this suite did. :func:`flat` exists for the tests that want that case
    on purpose.
    """
    base = {
        "hrv": 45.0 + (offset % 5) - 2,
        "restingHR": 64 + (offset % 3) - 1,
        "sleepSecs": 27000 + (offset % 4) * 600 - 900,
        "respiration": round(14.2 + (offset % 3) * 0.2 - 0.2, 2),
        "spO2": 96.0 + (offset % 3) * 0.5 - 0.5,
        "readiness": 70,
        "sleepScore": 76,
        "atlLoad": 0,
        "ctl": 40.0,
        "atl": 38.0,
    }
    base.update(overrides)
    return wellness_row(offset, **base)


def flat(offset: int, **overrides: object) -> dict:
    """Every day identical, so every field has zero scale to standardise against."""
    base = {
        "hrv": 45.0,
        "restingHR": 64,
        "sleepSecs": 27000,
        "respiration": 14.2,
        "spO2": 96.0,
        "readiness": 70,
        "atlLoad": 0,
    }
    base.update(overrides)
    return wellness_row(offset, **base)


def store(conn: psycopg.Connection, rows: list[dict]) -> None:
    wellness.store(conn, rows)


def baseline(conn: psycopg.Connection, days: int = 21, **overrides: object) -> None:
    """A steady run of history, oldest first, ending the day before TODAY."""
    store(conn, [steady(offset, **overrides) for offset in range(days, 0, -1)])


# --- RECOV-02: what the feed carries, and what it does not -------------------


def test_the_load_curves_are_stored(conn: psycopg.Connection) -> None:
    """`atlLoad` is RECOV-06's signal, so it has to survive the read."""
    store(conn, [steady(1, atlLoad=139, ctlLoad=52, rampRate=1.4)])

    with conn.cursor() as cur:
        cur.execute("select ctl, atl, ctl_load, atl_load, ramp_rate from wellness")
        row = cur.fetchone()
    assert float(row["atl_load"]) == 139
    assert float(row["ctl_load"]) == 52
    assert float(row["ramp_rate"]) == 1.4


def test_sleep_quality_is_stored(conn: psycopg.Connection) -> None:
    """RECOV-02 names three sleep properties; P04 stored two of them."""
    store(conn, [steady(1, sleepQuality=3)])
    with conn.cursor() as cur:
        cur.execute("select sleep_quality from wellness")
        assert float(cur.fetchone()["sleep_quality"]) == 3


def test_temp_weight_is_never_stored(conn: psycopg.Connection) -> None:
    """The trap this project came closest to walking into.

    `tempWeight` is populated on every day of the live feed, so it looks like the
    body mass source HLTH-04 is missing. Across 22 days it carried two distinct
    values one kilogram apart, alternating — a carried-forward stand-in, not a
    measurement series. Fitting a 28 day trend on it would draw a confident line
    through two numbers and look exactly like data.
    """
    store(conn, [steady(1, tempWeight=84.0), steady(2, tempWeight=85.0)])

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from body_mass_readings")
        assert cur.fetchone()["n"] == 0

    assert "tempWeight" not in wellness.FIELDS
    assert "tempWeight" in wellness.NEVER_STORED


def test_load_fields_are_not_reported_as_recov_02_absences(
    conn: psycopg.Connection,
) -> None:
    """A load curve the platform did not publish is not a RECOV-02 field."""
    result = wellness.store(conn, [wellness_row(1, hrv=45.0, restingHR=64)])
    assert not any(field in result.fields_absent for field in wellness.LOAD_FIELDS)


# --- RECOV-03: no Whoop client ------------------------------------------------


def test_no_whoop_integration_exists() -> None:
    """RECOV-03: Whoop reaches the system only through intervals.icu wellness.

    Scanned rather than asserted by inspection, because the temptation to add one
    arrives exactly when a field turns out to be missing — which, for `hrvSDNN`,
    it now has.
    """
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        for lineno, text in enumerate(path.read_text().splitlines(), 1):
            # A mention in prose explaining why there is no client is the point
            # of the rule, not a breach of it. A URL or an import is a breach.
            if re.search(r"whoop", text, re.I) and re.search(
                r"import|https?://|api\.|client|token", text, re.I
            ):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {text.strip()}")
    assert not offenders, offenders


# --- RECOV-04: the deviation is local ----------------------------------------


def test_the_deviation_is_computed_from_stored_history(conn: psycopg.Connection) -> None:
    """RECOV-04's acceptance: a deviation calculated locally from stored history."""
    baseline(conn)
    store(conn, [steady(0, hrv=52.0, restingHR=58)])  # better than his baseline

    found = recovery.for_day(conn, TODAY)

    assert found is not None and found.usable
    assert found.deviation > 0
    assert found.fields_used == len(recovery.COMPONENTS)
    assert set(found.components) == set(recovery.COMPONENTS)


def test_a_bad_day_reads_negative(conn: psycopg.Connection) -> None:
    """Positive is better than his own baseline, negative is worse."""
    baseline(conn)
    store(conn, [steady(0, hrv=31.0, restingHR=74, sleepSecs=16000)])

    assert recovery.for_day(conn, TODAY).deviation < 0


def test_the_platform_score_is_not_an_input(conn: psycopg.Connection) -> None:
    """RECOV-04: "not against platform derived scores".

    The strongest available statement of it: move readiness as far as it goes and
    the locally computed deviation does not shift by a thousandth.
    """
    baseline(conn)
    store(conn, [steady(0, readiness=95)])
    optimistic = recovery.for_day(conn, TODAY)

    store(conn, [steady(0, readiness=5)])
    pessimistic = recovery.for_day(conn, TODAY)

    assert optimistic.deviation == pessimistic.deviation
    assert optimistic.platform_readiness != pessimistic.platform_readiness
    assert "readiness" not in recovery.COMPONENTS


def test_the_component_list_carries_no_platform_score() -> None:
    """A regression guard on the list itself, not on its output.

    Adding `readiness` to COMPONENTS would undo RECOV-04 silently and every other
    test here would still pass.
    """
    for score in recovery.PLATFORM_SCORES:
        assert score not in recovery.COMPONENTS


def test_a_field_the_feed_withholds_degrades_rather_than_fails(
    conn: psycopg.Connection,
) -> None:
    """RECOV-02's acceptance: a withheld field degrades the deviation.

    `hrvSDNN` is the real case — null on all 22 days of the live feed — and it
    needs no special handling, because being dropped is what happens by default.
    """
    baseline(conn, spO2=None, respiration=None)
    store(conn, [steady(0, hrv=52.0, restingHR=58, spO2=None, respiration=None)])

    found = recovery.for_day(conn, TODAY)

    assert found.usable
    assert found.fields_used == 3
    assert "spo2" not in found.components
    assert found.deviation > 0


def test_a_thin_baseline_produces_no_deviation_rather_than_a_confident_one(
    conn: psycopg.Connection,
) -> None:
    """A standard deviation over two readings is a number, not a baseline."""
    baseline(conn, days=3)
    store(conn, [steady(0, hrv=52.0)])

    assert recovery.for_day(conn, TODAY) is None


def test_a_flat_baseline_does_not_divide_by_zero(conn: psycopg.Connection) -> None:
    """Every prior day identical means no scale to standardise against."""
    store(conn, [flat(offset) for offset in range(21, -1, -1)])

    found = recovery.for_day(conn, TODAY)
    assert found is None  # sd is zero on every field, so every field drops out


def test_an_empty_feed_produces_nothing_and_does_not_raise(
    conn: psycopg.Connection,
) -> None:
    assert recovery.deviations(conn, TODAY) == []
    assert recovery.for_day(conn, TODAY) is None
    assert recovery.context(conn, TODAY) == ""


def test_the_deviation_reaches_the_rollup(conn: psycopg.Connection) -> None:
    """MEM-08: the model reads a computed figure, never the wellness rows."""
    baseline(conn)
    store(conn, [steady(0, hrv=52.0, restingHR=58, atlLoad=83)])
    recovery.recompute(conn, TODAY)

    with conn.cursor() as cur:
        cur.execute("select * from rollups where as_of = %s", (TODAY,))
        row = cur.fetchone()

    assert row["recovery_deviation"] > 0
    assert row["recovery_fields_used"] == len(recovery.COMPONENTS)
    assert set(row["recovery_components"]) == set(recovery.COMPONENTS)
    assert float(row["platform_readiness"]) == 70
    assert float(row["day_load"]) == 83


def test_the_recovery_rollup_does_not_disturb_the_others(conn: psycopg.Connection) -> None:
    """Three writers, disjoint columns, none waiting on another."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into rollups (as_of, load_7d, weight_reading_n) values (%s, 320, 4)",
            (TODAY,),
        )
    baseline(conn)
    store(conn, [steady(0, hrv=52.0)])
    recovery.recompute(conn, TODAY)

    with conn.cursor() as cur:
        cur.execute(
            "select load_7d, weight_reading_n, recovery_deviation from rollups where as_of = %s",
            (TODAY,),
        )
        row = cur.fetchone()
    assert float(row["load_7d"]) == 320
    assert row["weight_reading_n"] == 4
    assert row["recovery_deviation"] is not None


def test_a_day_the_feed_covered_but_could_not_score_still_records_its_load(
    conn: psycopg.Connection,
) -> None:
    """RECOV-06 reads the load whether or not recovery could be computed."""
    store(conn, [steady(0, atlLoad=99, hrv=None, restingHR=None)])
    recovery.recompute(conn, TODAY)

    with conn.cursor() as cur:
        cur.execute("select recovery_deviation, day_load from rollups where as_of = %s", (TODAY,))
        row = cur.fetchone()
    assert row["recovery_deviation"] is None
    assert float(row["day_load"]) == 99


# --- RECOV-04 in the prompt ---------------------------------------------------


def test_the_local_figure_leads_and_the_platform_score_follows(
    conn: psycopg.Connection,
) -> None:
    """Ordering is not decoration: it decides which number reads as authoritative."""
    baseline(conn)
    store(conn, [steady(0, hrv=52.0, restingHR=58)])

    rendered = recovery.context(conn, TODAY)
    assert rendered.index("standard deviations") < rendered.index("platform's own readiness")
    assert "That is its opinion, not a measurement." in rendered
    assert "the deviation above is computed from his own history and wins" in rendered


def test_a_partial_reading_says_it_is_partial(conn: psycopg.Connection) -> None:
    """Fewer signals is softer evidence, not a worse score."""
    baseline(conn, spO2=None, respiration=None)
    store(conn, [steady(0, hrv=52.0, spO2=None, respiration=None)])

    assert "partial reading" in recovery.context(conn, TODAY)


def test_recovery_reaches_the_assembled_prompt(conn: psycopg.Connection) -> None:
    baseline(conn)
    store(conn, [steady(0, hrv=52.0, restingHR=58)])

    assembled = prompt.assemble(conn, datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI), tz=DUBAI)
    assert "recovery" in assembled.names()
    assert "RECOVERY" in assembled.render()


# --- RECOV-06: disambiguating a missing session -------------------------------


def prescribe(conn: psycopg.Connection, when: datetime, discipline: str = "ride") -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec) "
            "values (1, %s, %s, '{}'::jsonb) returning id",
            (when, discipline),
        )
        return cur.fetchone()["id"]


LONG_AGO = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_load_recorded_with_no_activity_is_not_a_missed_session(
    conn: psycopg.Connection,
) -> None:
    """RECOV-06's acceptance, exactly as written.

    Without this a broken watcher reads as a fortnight of skipped sessions.
    """
    prescribe(conn, LONG_AGO)
    store(conn, [steady(8, atlLoad=83)])

    verdicts = review.missed(conn, NOW, DUBAI)

    assert len(verdicts) == 1
    assert not verdicts[0]["missed"]
    assert "upload is missing" in verdicts[0]["reason"]
    assert verdicts[0]["signals"]["load_recorded"] is True


def test_no_load_and_no_activity_is_a_missed_session(conn: psycopg.Connection) -> None:
    """The feed covered the day and recorded nothing. That is a skipped session."""
    prescribe(conn, LONG_AGO)
    store(conn, [steady(8, atlLoad=0)])

    verdicts = review.missed(conn, NOW, DUBAI)

    assert verdicts[0]["missed"]
    assert verdicts[0]["signals"]["load_recorded"] is False
    assert verdicts[0]["safe_to_act"]


def test_an_absent_wellness_row_is_not_a_recorded_zero(conn: psycopg.Connection) -> None:
    """The distinction ADJ-08 turns on.

    Marking the prescription missed is bookkeeping and happens anyway.
    Restructuring the week off it is an action, and `safe_to_act` says not to —
    the coach did not know, it did not observe a zero.
    """
    prescribe(conn, LONG_AGO)

    verdicts = review.missed(conn, NOW, DUBAI)

    assert verdicts[0]["missed"]
    assert verdicts[0]["signals"]["load_recorded"] is None
    assert not verdicts[0]["safe_to_act"]
    assert "wellness feed had nothing" in verdicts[0]["reason"]


def test_an_activity_on_the_day_still_outranks_everything(
    conn: psycopg.Connection,
) -> None:
    """FIT-12's existing gate is unchanged by RECOV-06's addition."""
    prescribe(conn, LONG_AGO)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date) values ('ride', %s, %s)",
            (LONG_AGO, day(8)),
        )
    store(conn, [steady(8, atlLoad=0)])

    verdicts = review.missed(conn, NOW, DUBAI)
    assert not verdicts[0]["missed"]
    assert "unmatched rather than missed" in verdicts[0]["reason"]


def test_the_sweep_does_not_mark_a_missing_upload_as_missed(
    conn: psycopg.Connection,
) -> None:
    """End to end through the loop that actually runs."""
    from coach.ingest import service

    prescribe(conn, LONG_AGO)
    store(conn, [steady(8, atlLoad=83)])

    assert service.sweep(conn, DUBAI, NOW) == {"missed": 0}

    with conn.cursor() as cur:
        cur.execute("select status from prescriptions")
        assert cur.fetchone()["status"] == "planned"


def test_the_recovery_deviation_is_attached_to_the_verdict(
    conn: psycopg.Connection,
) -> None:
    """RECOV-06 names recovery *and* load; both have to be on the verdict."""
    prescribe(conn, LONG_AGO)
    store(conn, [steady(offset) for offset in range(30, 8, -1)])
    store(conn, [steady(8, atlLoad=0, hrv=31.0, restingHR=74)])

    verdicts = review.missed(conn, NOW, DUBAI)
    assert verdicts[0]["signals"]["recovery_deviation"] < 0


def test_load_recorded_on_distinguishes_three_states(conn: psycopg.Connection) -> None:
    store(conn, [steady(1, atlLoad=50), steady(2, atlLoad=0), steady(3, atlLoad=None)])

    assert recovery.load_recorded_on(conn, day(1)) is True
    assert recovery.load_recorded_on(conn, day(2)) is False
    assert recovery.load_recorded_on(conn, day(3)) is None
    assert recovery.load_recorded_on(conn, day(9)) is None  # no row at all


# --- RECOV-01 and RECOV-05 were met in P04; asserted here so P05 is complete ---


def test_wellness_reads_use_basic_auth_and_no_token_exchange() -> None:
    """RECOV-01: HTTP basic with the literal username API_KEY.

    SEC-04's scan covers the absence of the alternative; this covers the presence
    of the right one.
    """
    source = (REPO / "src" / "coach" / "ingest" / "client.py").read_text()
    assert 'USERNAME = "API_KEY"' in source
    assert "auth=(USERNAME, key)" in source


def test_re_reading_an_overlapping_range_rewrites_rather_than_appends(
    conn: psycopg.Connection,
) -> None:
    """RECOV-05, asserted on the recovery columns rather than on body mass."""
    store(conn, [steady(offset) for offset in range(14, 0, -1)])
    store(conn, [steady(offset, hrv=50.0) for offset in range(7, 0, -1)])

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from wellness")
        assert cur.fetchone()["n"] == 14
        cur.execute("select hrv from wellness where local_date = %s", (day(3),))
        assert float(cur.fetchone()["hrv"]) == 50.0
