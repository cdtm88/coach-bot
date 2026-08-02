"""P03 acceptance: FIT-01 to FIT-17 and SEC-02.

Done when full history backfills silently, a new ride reviews inside the PERF-03
budget, replayed and unsigned webhooks are rejected, reconcile restores a deleted
session, a locally dropped file ingests without upstream involvement, and a coach
authored activity returns without duplicating.

FIT files here are real: built with fit-tool, parsed with fitdecode.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

import conftest

sys.path.insert(0, str(Path(__file__).parent))
from coach.ingest import activities, archive, parse, reconcile, review, webhook  # noqa: E402
from conftest_fit import build_fit  # noqa: E402

DUBAI = ZoneInfo("Asia/Dubai")
SECRET = "s3cret-from-manage-app"
START = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)


def ride_fit(start: datetime = START, minutes: int = 2) -> bytes:
    n = minutes * 60
    return build_fit(
        start,
        power=[100] * (n // 2) + [200] * (n - n // 2),
        heart_rate=[130] * (n // 2) + [150] * (n - n // 2),
        cadence=[85] * n,
        altitude=[10.0 + i * 0.1 for i in range(n)],
    )


def activity(
    activity_id: str = "i1001",
    kind: str = "Ride",
    start_local: str = "2026-07-27T22:00:00",
    **extra: Any,
) -> dict[str, Any]:
    base = {
        "id": activity_id,
        "type": kind,
        "name": "Tempus Fugit",
        "start_date_local": start_local,
        "moving_time": 3600,
        "icu_training_load": 55,
        "icu_average_watts": 999,  # deliberately wrong; must never be used as parsed
        "icu_ftp": 115,
        "icu_atl": 30.5,
    }
    base.update(extra)
    return base


# --- SEC-02 / FIT-02: verification and replay ------------------------------


def payload(*events: dict[str, Any], secret: str = SECRET) -> dict[str, Any]:
    return {"secret": secret, "events": list(events)}


def uploaded(activity_id: str = "i1001", ts: str = "2026-07-27T18:05:00+00:00") -> dict[str, Any]:
    return {
        "type": "ACTIVITY_UPLOADED",
        "athlete_id": "i653843",
        "timestamp": ts,
        "activity": {"id": activity_id},
    }


def test_unsigned_payload_is_rejected(conn: psycopg.Connection) -> None:
    """SEC-02: unsigned requests rejected."""
    with pytest.raises(webhook.Rejected, match="secret"):
        webhook.accept(conn, {"events": [uploaded()]}, secret=SECRET)


def test_wrong_secret_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(webhook.Rejected, match="secret"):
        webhook.accept(conn, payload(uploaded(), secret="wrong"), secret=SECRET)


def test_absent_secret_is_rejected_like_a_wrong_one(conn: psycopg.Connection) -> None:
    """A payload omitting the field must not pass a truthiness check."""
    with pytest.raises(webhook.Rejected):
        webhook.accept(conn, {"secret": None, "events": [uploaded()]}, secret=SECRET)


def test_unconfigured_secret_refuses_rather_than_accepting_everything(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTERVALS_WEBHOOK_SECRET", raising=False)
    with pytest.raises(webhook.Rejected, match="not set"):
        webhook.accept(conn, payload(uploaded()))


def test_replayed_payload_is_dropped(conn: psycopg.Connection) -> None:
    """FIT-02: the same event twice acts once."""
    first = webhook.accept(conn, payload(uploaded()), secret=SECRET)
    conn.commit()
    assert len(first) == 1

    second = webhook.accept(conn, payload(uploaded()), secret=SECRET)
    conn.commit()
    assert second == [], "a replayed body must produce no work"


def test_a_later_event_for_the_same_activity_is_not_a_replay(conn: psycopg.Connection) -> None:
    webhook.accept(conn, payload(uploaded(ts="2026-07-27T18:05:00+00:00")), secret=SECRET)
    conn.commit()
    later = webhook.accept(conn, payload(uploaded(ts="2026-07-27T19:00:00+00:00")), secret=SECRET)
    conn.commit()
    assert len(later) == 1


def test_only_activity_uploaded_is_the_trigger(conn: psycopg.Connection) -> None:
    """FIT-01: ACTIVITY_ANALYZED is held 60s upstream and must not be the trigger."""
    events = webhook.accept(
        conn,
        payload(
            uploaded(),
            {
                "type": "ACTIVITY_ANALYZED",
                "timestamp": "2026-07-27T18:06:00+00:00",
                "activity": {"id": "i1001"},
            },
        ),
        secret=SECRET,
    )
    conn.commit()
    triggers = [e for e in events if e.is_trigger]
    assert len(triggers) == 1
    assert triggers[0].type == webhook.TRIGGER


def test_unknown_event_types_are_ignored_not_fatal(conn: psycopg.Connection) -> None:
    events = webhook.accept(
        conn,
        payload(uploaded(), {"type": "SOMETHING_NEW", "timestamp": "2026-07-27T18:06:00+00:00"}),
        secret=SECRET,
    )
    conn.commit()
    assert len(events) == 1


# --- FIT-03: parsed and derived are kept apart -----------------------------


def test_parsed_values_are_computed_not_borrowed(conn: psycopg.Connection) -> None:
    """FIT-03: the platform's derived numbers never become parsed values.

    icu_average_watts is set to 999 in the fixture. The parsed average must be
    150, computed from the samples, because the platform has no undecorated
    average for us to take.
    """
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select * from sessions where id = %s", (result.session_id,))
        session = cur.fetchone()

    assert float(session["avg_power_w"]) == 150.0
    assert session["sample_count"] == 120
    assert session["derived"]["icu_average_watts"] == 999
    assert float(session["avg_power_w"]) != session["derived"]["icu_average_watts"]


def test_a_reingest_with_no_file_keeps_the_numbers_it_already_parsed(
    conn: psycopg.Connection,
) -> None:
    """An update that read no samples has nothing to say about the parsed columns.

    It used to say it anyway: `Parsed()` is all nulls, and the update wrote every
    one of them. `reconcile.run(fetch_files=False)` did this by construction, and
    any re-ingest arriving while the original is briefly unavailable does it by
    accident — the ride keeps its row and silently loses its power, heart rate and
    cadence. The coach then reads a ride with no numbers and describes our write
    rather than the ride.
    """
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    activities.ingest(conn, activity(), DUBAI)  # no file, no streams
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select avg_power_w, np_power_w, avg_hr, max_hr, avg_cadence, sample_count, "
            "       duration_s, distance_m from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert float(row["avg_power_w"]) == 150.0
    assert row["np_power_w"] is not None
    assert row["avg_hr"] == 140
    assert row["max_hr"] == 150
    assert row["avg_cadence"] is not None
    assert row["sample_count"] == 120
    assert row["duration_s"] is not None
    assert row["distance_m"] is not None


def test_a_reparse_that_read_samples_writes_its_nulls(conn: psycopg.Connection) -> None:
    """The asymmetry, and it is deliberate.

    A file with no power meter is a positive statement that this ride had no
    power. Coalescing it the way the no-samples case does would leave the earlier
    figure standing on a ride that never produced one.
    """
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    no_power = build_fit(START, heart_rate=[130] * 120, cadence=[85] * 120)
    activities.ingest(conn, activity(), DUBAI, file_bytes=no_power)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select avg_power_w, np_power_w, avg_hr from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert row["avg_power_w"] is None, "the new file says there was no power"
    assert row["np_power_w"] is None
    assert row["avg_hr"] == 130


def test_reconcile_without_files_does_not_erase_what_it_stored(conn: psycopg.Connection) -> None:
    """`fetch_files=False` is the configuration that made this reachable."""
    fake = FakeIntervals([activity()], {"i1001": ride_fit()})
    reconcile.run(conn, fake, DUBAI, date(2026, 7, 1))
    conn.commit()

    reconcile.run(conn, fake, DUBAI, date(2026, 7, 1), backfill=True, fetch_files=False)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select avg_power_w, sample_count from sessions where external_ref = 'i1001'")
        row = cur.fetchone()
    assert float(row["avg_power_w"]) == 150.0
    assert row["sample_count"] == 120


def test_a_folder_scan_does_not_relabel_a_row_the_platform_described(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The watched folder knows a file and a timestamp. It does not know a type.

    `ingest_file` synthesises `{"type": "Ride", "source": "local_file"}` because
    that is all it has, and the update wrote both over a row the API had already
    described. A Zwift ride came back an outdoor one, off a source it never came
    from.
    """
    body = ride_fit()
    first = activities.ingest(conn, activity(kind="VirtualRide"), DUBAI, file_bytes=body)
    conn.commit()

    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(body)
    archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select source, discipline, activity_type from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert row["source"] == "intervals"
    assert row["discipline"] == "virtualride"
    assert row["activity_type"] == "VirtualRide"


def test_a_golf_round_does_not_acquire_power_from_a_folder_scan(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """FIT-07 has to survive the path that thinks everything is a ride.

    This is why the power decision reads the stored discipline rather than this
    call's. A scan that cannot set the discipline must not get to act as though
    it had.
    """
    body = ride_fit()
    first = activities.ingest(conn, activity(kind="Golf"), DUBAI, file_bytes=body)
    conn.commit()

    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(body)
    archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select discipline, avg_power_w, np_power_w, avg_hr from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert row["discipline"] == "golf"
    assert row["avg_power_w"] is None, "FIT-07: a golf round carries no power figures"
    assert row["np_power_w"] is None
    assert row["avg_hr"] == 140, "the samples it did read still land"


def test_a_folder_scan_does_not_disown_a_coach_authored_session(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """FIT-17's flag comes from an `external_id` a synthetic dict does not have."""
    body = ride_fit()
    first = activities.ingest(
        conn,
        activity(external_id=f"{activities.COACH_MARKER}0"),
        DUBAI,
        file_bytes=body,
    )
    conn.commit()

    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(body)
    archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select coach_authored from sessions where id = %s", (first.session_id,))
        assert cur.fetchone()["coach_authored"] is True


def test_a_file_with_no_platform_fields_does_not_erase_the_derived_block(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The same defect one column over, and this one moves a number the coach quotes.

    A watched-folder file carries no `icu_` fields at all, so its empty derived
    block used to be written straight over an upstream read — taking
    `icu_training_load` with it. That is what the load rollups sum, so the seven
    day load silently drops by a whole ride while every row still looks present.
    """
    body = ride_fit()
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=body)
    conn.commit()

    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(body)
    archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select derived, analyzed_at, derived_provisional from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert row["derived"]["icu_training_load"] == 55
    assert row["derived"]["icu_ftp"] == 115


def test_a_discipline_corrected_upstream_drops_the_power_figures(
    conn: psycopg.Connection,
) -> None:
    """FIT-07 on the update path, not only on the insert.

    A ride retyped as a golf round upstream must not keep the numbers it should
    never have carried, whether or not this call read any samples.
    """
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    activities.ingest(conn, activity(kind="Golf"), DUBAI)  # no file this time
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select discipline, avg_power_w, np_power_w, max_power_w, avg_hr "
            "from sessions where id = %s",
            (first.session_id,),
        )
        row = cur.fetchone()
    assert row["discipline"] == "golf"
    assert row["avg_power_w"] is None
    assert row["np_power_w"] is None
    assert row["max_power_w"] is None
    assert row["avg_hr"] == 140, "FIT-07 is about power, not about the whole row"


def test_derived_block_holds_the_platform_fields(conn: psycopg.Connection) -> None:
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select derived from sessions where id = %s", (result.session_id,))
        derived = cur.fetchone()["derived"]
    assert derived["icu_training_load"] == 55
    assert derived["icu_ftp"] == 115
    assert "name" not in derived, "only the platform's own computed fields belong here"


def test_normalised_power_exceeds_average_on_variable_power() -> None:
    parsed = parse.from_fit(ride_fit())
    assert parsed.np_power_w > parsed.avg_power_w


def test_normalised_power_is_absent_rather_than_wrong_on_a_short_effort() -> None:
    """A 20 second series has no meaningful NP; None beats a plausible number."""
    parsed = parse.from_fit(build_fit(START, power=[200] * 20))
    assert parsed.np_power_w is None
    assert any("normalised power" in w for w in parsed.warnings)


def test_streams_are_the_fallback_when_there_is_no_original_file(
    conn: psycopg.Connection,
) -> None:
    """Upstream serves no original for Strava activities; parsed must still fill."""
    streams = [
        {"type": "watts", "data": [100] * 60 + [200] * 60},
        {"type": "heartrate", "data": [140] * 120},
    ]
    result = activities.ingest(conn, activity(), DUBAI, streams=streams)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select avg_power_w, avg_hr from sessions where id = %s", (result.session_id,))
        row = cur.fetchone()
    assert float(row["avg_power_w"]) == 150.0
    assert row["avg_hr"] == 140


def test_no_file_and_no_streams_leaves_parsed_null_rather_than_borrowed(
    conn: psycopg.Connection,
) -> None:
    result = activities.ingest(conn, activity(), DUBAI)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select avg_power_w, derived from sessions where id = %s", (result.session_id,))
        row = cur.fetchone()
    assert row["avg_power_w"] is None
    assert row["derived"]["icu_average_watts"] == 999


# --- FIT-04: deduplication --------------------------------------------------


def test_redelivering_a_webhook_creates_no_second_session(conn: psycopg.Connection) -> None:
    fit = ride_fit()
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=fit)
    second = activities.ingest(conn, activity(), DUBAI, file_bytes=fit)
    conn.commit()
    assert first.session_id == second.session_id
    assert second.created is False
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1


def test_the_same_file_under_a_new_id_is_still_one_session(conn: psycopg.Connection) -> None:
    """The content hash is the second half of FIT-04 for a reason."""
    fit = ride_fit()
    first = activities.ingest(conn, activity("i1001"), DUBAI, file_bytes=fit)
    second = activities.ingest(conn, activity("i2002"), DUBAI, file_bytes=fit)
    conn.commit()
    assert first.session_id == second.session_id


# --- FIT-07 / FIT-08: discipline and source --------------------------------


def test_golf_produces_a_session_and_no_power_analysis(conn: psycopg.Connection) -> None:
    """FIT-07: activity only, no compliance calculation."""
    result = activities.ingest(conn, activity(kind="Golf"), DUBAI, file_bytes=ride_fit())
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "select discipline, avg_power_w, np_power_w from sessions where id = %s",
            (result.session_id,),
        )
        row = cur.fetchone()
    assert row["discipline"] == "golf"
    assert row["avg_power_w"] is None, "power figures must not survive on a golf round"
    assert row["np_power_w"] is None


def test_a_non_zwift_activity_takes_the_same_path(conn: psycopg.Connection) -> None:
    """FIT-08: outdoor and any connected device produce an equivalent row."""
    outdoor = activities.ingest(
        conn, activity("i3003", kind="Ride", source="GARMIN"), DUBAI, file_bytes=ride_fit()
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "select discipline, avg_power_w from sessions where id = %s", (outdoor.session_id,)
        )
        row = cur.fetchone()
    assert row["discipline"] == "ride"
    assert float(row["avg_power_w"]) == 150.0


# --- FIT-10: dated from the data -------------------------------------------


def test_a_late_upload_is_dated_from_the_activity(conn: psycopg.Connection) -> None:
    """FIT-10: an activity uploaded two days late is dated correctly."""
    result = activities.ingest(
        conn, activity(start_local="2026-07-20T07:30:00"), DUBAI, file_bytes=ride_fit()
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select local_date from sessions where id = %s", (result.session_id,))
        assert cur.fetchone()["local_date"] == date(2026, 7, 20)


def test_a_late_evening_ride_stays_on_its_local_day(conn: psycopg.Connection) -> None:
    """TZ-01 through the ingest path: 23:30 local is not tomorrow."""
    result = activities.ingest(
        conn, activity(start_local="2026-07-20T23:30:00"), DUBAI, file_bytes=ride_fit()
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "select local_date, started_at from sessions where id = %s", (result.session_id,)
        )
        row = cur.fetchone()
    assert row["local_date"] == date(2026, 7, 20)
    assert row["started_at"].astimezone(UTC).date() == date(2026, 7, 20)


# --- FIT-17: coach authored activities --------------------------------------


def test_a_coach_authored_activity_matches_rather_than_duplicates(
    conn: psycopg.Connection,
) -> None:
    """FIT-17: a gym session written from chat, returning through the webhook."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into sessions (source, discipline, started_at, local_date, name)
            values ('chat', 'weighttraining', %s, %s, 'Gym A') returning id
            """,
            (datetime(2026, 7, 27, 14, 0, tzinfo=UTC), date(2026, 7, 27)),
        )
        local_id = cur.fetchone()["id"]

    returning = activity(
        "i9009",
        kind="WeightTraining",
        start_local="2026-07-27T18:00:00",
        external_id=f"{activities.COACH_MARKER}{local_id}",
    )
    result = activities.ingest(conn, returning, DUBAI)
    conn.commit()

    assert result.session_id == local_id
    assert result.created is False
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1
        cur.execute("select coach_authored, external_ref from sessions where id = %s", (local_id,))
        row = cur.fetchone()
    assert row["coach_authored"] is True
    assert row["external_ref"] == "i9009"


# --- FIT-14 / FIT-15 / FIT-16: the local archive ---------------------------


def test_a_locally_dropped_file_ingests_without_upstream(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """FIT-14: a first class path, with no client and no network involved."""
    path = tmp_path / "2026-07-27-ride.fit"
    path.write_bytes(ride_fit())

    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    assert session_id is not None
    with conn.cursor() as cur:
        cur.execute(
            "select source, avg_power_w, external_ref from sessions where id = %s", (session_id,)
        )
        row = cur.fetchone()
    assert row["source"] == "local_file"
    assert float(row["avg_power_w"]) == 150.0
    assert row["external_ref"] is None, "nothing upstream was consulted"


def test_scanning_the_folder_is_idempotent(conn: psycopg.Connection, tmp_path: Path) -> None:
    (tmp_path / "a.fit").write_bytes(ride_fit())
    (tmp_path / "b.fit").write_bytes(ride_fit(START + timedelta(days=1)))
    (tmp_path / "notes.txt").write_text("ignore me")

    first = archive.scan(conn, tmp_path, DUBAI)
    conn.commit()
    assert len(first) == 2

    second = archive.scan(conn, tmp_path, DUBAI)
    conn.commit()
    assert set(second) == set(first)
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 2


def test_the_archive_survives_an_upstream_deletion(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """FIT-15: disconnecting upstream leaves the local archive intact.

    Simulated by deleting the session row, which is what an upstream-driven
    cleanup would cascade to. The archive row and the file both remain.
    """
    path = tmp_path / "ride.fit"
    path.write_bytes(ride_fit())
    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update fit_archive set session_id = null where session_id = %s", (session_id,))
        cur.execute("delete from sessions where id = %s", (session_id,))
    conn.commit()

    assert archive.restorable(conn), "archive row must outlive the session"
    assert path.exists()


def test_a_folder_scan_does_not_rename_a_ride_upstream_already_named(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The two ingest paths meet on one row, and the filename must not win.

    A Zwift ride reaches intervals.icu as "Tempus Fugit" and sits on disk as
    `2026-07-27-181000.fit`. The poll stores it, the watched folder scan finds
    the identical bytes, FIT-04 matches them on content hash, and the update that
    follows used to write the stem over the title. The coach then discussed
    "2026-07-27-181000" with the athlete, which is a lie the suite could not see
    because both halves worked exactly as written.
    """
    body = ride_fit()
    activities.ingest(conn, activity("i1001"), DUBAI, file_bytes=body)
    conn.commit()

    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(body)
    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1, "FIT-04 should have matched, not duplicated"
        cur.execute("select name from sessions where id = %s", (session_id,))
        assert cur.fetchone()["name"] == "Tempus Fugit"


def test_a_file_with_no_upstream_name_is_still_named_by_its_stem(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The stem is a fallback, so it must still name a row that has nothing."""
    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(ride_fit())
    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select name from sessions where id = %s", (session_id,))
        assert cur.fetchone()["name"] == "2026-07-27-181000"


def test_an_untitled_upstream_activity_does_not_erase_a_name(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """intervals.icu serves `name: ""` for an activity nobody titled.

    Stored, an empty string is a value, and it would win the coalesce against the
    name the row already had. So blank is read as absent.
    """
    path = tmp_path / "2026-07-27-181000.fit"
    path.write_bytes(ride_fit())
    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    activities.ingest(conn, activity("i1001", name="  "), DUBAI, file_bytes=path.read_bytes())
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select name, external_ref from sessions where id = %s", (session_id,))
        row = cur.fetchone()
    assert row["name"] == "2026-07-27-181000"
    assert row["external_ref"] == "i1001", "the same row, reached from upstream"


def test_upstream_renaming_a_ride_still_reaches_the_row(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Preferring upstream must not mean freezing the name at first sight."""
    body = ride_fit()
    activities.ingest(conn, activity("i1001"), DUBAI, file_bytes=body)
    conn.commit()

    activities.ingest(conn, activity("i1001", name="Zwift - Race: Stage 3"), DUBAI, file_bytes=body)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select name from sessions where external_ref = 'i1001'")
        assert cur.fetchone()["name"] == "Zwift - Race: Stage 3"


def test_the_archive_module_contains_no_delete() -> None:
    """FIT-15 as a property of the code, not just of one test's data."""
    source = (Path(__file__).parents[1] / "src/coach/ingest/archive.py").read_text().lower()
    assert "delete from fit_archive" not in source
    assert ".unlink(" not in source


def test_a_file_can_be_replayed_upstream(conn: psycopg.Connection, tmp_path: Path) -> None:
    """FIT-16: the archive restores upstream through the upload endpoint."""
    path = tmp_path / "ride.fit"
    path.write_bytes(ride_fit())
    session_id = archive.ingest_file(conn, path, DUBAI)
    conn.commit()

    uploads: list[tuple[str, int, str | None]] = []

    class FakeClient:
        def upload_file(
            self, data: bytes, filename: str, external_id: str | None = None
        ) -> dict[str, Any]:
            uploads.append((filename, len(data), external_id))
            return {"id": "i7777"}

    row = archive.restorable(conn)[0]
    result = archive.restore(conn, FakeClient(), row["id"])
    conn.commit()

    assert result["id"] == "i7777"
    # FIT-16 plus FIT-17: the restore carries the coach marker keyed to the local
    # session, so the ride coming back through ingest matches rather than
    # duplicating. A restore that produced a second row would be a poor repair.
    assert uploads == [("ride.fit", path.stat().st_size, f"{activities.COACH_MARKER}{session_id}")]
    with conn.cursor() as cur:
        cur.execute("select external_ref, restored_at from fit_archive where id = %s", (row["id"],))
        restored = cur.fetchone()
    assert restored["external_ref"] == "i7777"
    assert restored["restored_at"] is not None


# --- FIT-05 / FIT-06: matching and review ----------------------------------


def prescribe(
    conn: psycopg.Connection, when: datetime, discipline: str = "ride", **spec: Any
) -> int:
    from psycopg.types.json import Jsonb

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec) "
            "values (%s, %s, %s, %s) returning id",
            (
                conftest.ensure_block(conn),
                when,
                discipline,
                Jsonb(spec or {"duration_s": 3600, "target_watts": 80}),
            ),
        )
        return cur.fetchone()["id"]


def test_a_session_matches_its_prescription_and_computes_compliance(
    conn: psycopg.Connection,
) -> None:
    """FIT-05: matched session sets status completed with duration and intensity deltas."""
    prescription_id = prescribe(conn, datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI))
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    matched = review.match(conn, result.session_id)
    assert matched == prescription_id

    compliance = review.attach(conn, result.session_id, matched)
    conn.commit()

    assert compliance.completed
    assert compliance.duration_delta_s is not None
    assert compliance.intensity_delta_w is not None
    with conn.cursor() as cur:
        cur.execute(
            "select status, session_id from prescriptions where id = %s", (prescription_id,)
        )
        row = cur.fetchone()
    assert row["status"] == "completed"
    assert row["session_id"] == result.session_id


def test_a_prescription_is_claimed_only_once(conn: psycopg.Connection) -> None:
    prescription_id = prescribe(conn, datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI))
    first = activities.ingest(conn, activity("i1"), DUBAI, file_bytes=ride_fit())
    review.attach(conn, first.session_id, prescription_id)
    conn.commit()

    second = activities.ingest(
        conn, activity("i2"), DUBAI, file_bytes=ride_fit(START + timedelta(hours=2))
    )
    conn.commit()
    assert review.match(conn, second.session_id) is None


def test_golf_gets_no_intensity_delta(conn: psycopg.Connection) -> None:
    """FIT-07: no compliance calculation on a non-power discipline."""
    prescription_id = prescribe(conn, datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI), discipline="golf")
    result = activities.ingest(conn, activity(kind="Golf"), DUBAI, file_bytes=ride_fit())
    conn.commit()
    compliance = review.attach(conn, result.session_id, prescription_id)
    assert compliance.intensity_delta_w is None


def test_a_review_is_written_as_an_observation_note(conn: psycopg.Connection) -> None:
    """FIT-06: compliance plus one forward looking note, as an observation."""
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    seen: list[dict[str, Any]] = []

    def write(context: dict[str, Any]) -> str:
        seen.append(context)
        return "Held 150 W steady. Next one, hold the cadence above 85 on the climbs."

    body = review.review(conn, result.session_id, write)
    conn.commit()

    assert body is not None
    assert seen[0]["parsed"]["avg_power_w"] == 150.0
    assert seen[0]["derived_by_platform"]["icu_average_watts"] == 999
    assert seen[0]["power_analysis_applies"] is True

    from coach.memory import notes as notemod

    written = [n for n in notemod.on_date(conn, date(2026, 7, 27)) if n.kind == "observation"]
    assert len(written) == 1
    assert written[0].refs == {"session_id": result.session_id}


def test_a_session_is_reviewed_only_once(conn: psycopg.Connection) -> None:
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()
    assert review.review(conn, result.session_id, lambda _: "first") is not None
    conn.commit()
    assert review.review(conn, result.session_id, lambda _: "second") is None


# --- FIT-09: history loads silently ----------------------------------------


def test_a_backfilled_session_is_never_reviewed(conn: psycopg.Connection) -> None:
    """FIT-09: loading history produces zero messages."""
    result = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit(), backfilled=True)
    conn.commit()
    calls: list[int] = []
    assert review.review(conn, result.session_id, lambda _: calls.append(1) or "note") is None
    assert calls == [], "the note writer must not even be called for backfilled history"


def test_a_later_ingest_does_not_un_backfill_a_row(conn: psycopg.Connection) -> None:
    """The flag is how the row came to exist, and no later call corrects that.

    It used to be written on every update, so any live-path ingest touching a
    backfilled row cleared it — and that flag is the only thing standing between
    a ride from two years ago and a review with a Telegram message attached. The
    review path checks `backfilled`, so this is asserted there rather than only
    on the column.
    """
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit(), backfilled=True)
    conn.commit()

    activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())  # the live path
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select backfilled from sessions where id = %s", (first.session_id,))
        assert cur.fetchone()["backfilled"] is True

    calls: list[int] = []
    assert review.review(conn, first.session_id, lambda _: calls.append(1) or "note") is None
    assert calls == [], "history spoke because an update cleared the flag"


def test_a_backfill_does_not_mark_a_session_that_arrived_live(conn: psycopg.Connection) -> None:
    """The other direction. History that already spoke was never silent."""
    first = activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit())
    conn.commit()

    activities.ingest(conn, activity(), DUBAI, file_bytes=ride_fit(), backfilled=True)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select backfilled from sessions where id = %s", (first.session_id,))
        assert cur.fetchone()["backfilled"] is False


# --- FIT-11 / FIT-13: reconcile and rollups --------------------------------


class FakeIntervals:
    """Stands in for the API. The client's own HTTP is exercised separately."""

    def __init__(self, upstream: list[dict[str, Any]], files: dict[str, bytes] | None = None):
        self.upstream = upstream
        self.files = files or {}
        self.last_limit = type("L", (), {"exhausted": False})()
        self.file_calls: list[str] = []

    def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
        return self.upstream

    def original_file(self, activity_id: str) -> bytes | None:
        self.file_calls.append(activity_id)
        return self.files.get(activity_id)

    def streams(self, activity_id: str) -> list[dict[str, Any]]:
        return []


def test_reconcile_restores_a_deleted_session(conn: psycopg.Connection) -> None:
    """FIT-11: deleting a session row and running reconcile restores it.

    The archive row is detached first, the same way
    `test_the_archive_survives_an_upstream_deletion` does it. Now that the poll
    archives what it downloads, `fit_archive.session_id` references the row, and
    the foreign key refuses a bare delete — which is FIT-15 working rather than
    something to route around. Nothing in `src/` deletes a session; this test is
    simulating one.
    """
    fake = FakeIntervals([activity()], {"i1001": ride_fit()})

    reconcile.run(conn, fake, DUBAI, date(2026, 7, 1))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select id from sessions")
        original = cur.fetchone()["id"]

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update fit_archive set session_id = null where session_id = %s", (original,))
        cur.execute("delete from sessions where id = %s", (original,))
    conn.commit()

    outcome = reconcile.run(conn, fake, DUBAI, date(2026, 7, 1))
    conn.commit()
    assert outcome.created == 1
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1


def test_reconcile_skips_what_it_already_has(conn: psycopg.Connection) -> None:
    fake = FakeIntervals([activity()], {"i1001": ride_fit()})
    reconcile.run(conn, fake, DUBAI, date(2026, 7, 1))
    conn.commit()

    outcome = reconcile.run(conn, fake, DUBAI, date(2026, 7, 1))
    conn.commit()
    assert outcome.skipped == 1
    assert outcome.created == 0


def test_backfill_marks_rows_and_writes_no_reviews(conn: psycopg.Connection) -> None:
    """FIT-09 through the reconcile path rather than the row flag directly."""
    fake = FakeIntervals(
        [
            activity("i1", start_local="2026-07-01T08:00:00"),
            activity("i2", start_local="2026-07-02T08:00:00"),
        ],
        {"i1": ride_fit(), "i2": ride_fit(START + timedelta(days=1))},
    )
    outcome = reconcile.run(conn, fake, DUBAI, date(2026, 6, 1), backfill=True)
    conn.commit()

    assert outcome.created == 2
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions where backfilled")
        assert cur.fetchone()["n"] == 2
        cur.execute("select count(*) as n from notes")
        assert cur.fetchone()["n"] == 0


def test_rollups_recompute_after_a_backfill(conn: psycopg.Connection) -> None:
    """FIT-13: rollups are correct immediately, not after the nightly job."""
    fake = FakeIntervals(
        [
            activity("i1", start_local="2026-07-01T08:00:00"),
            activity("i2", start_local="2026-07-02T08:00:00"),
        ],
        {},
    )
    reconcile.run(conn, fake, DUBAI, date(2026, 6, 1), backfill=True, fetch_files=False)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select as_of, load_7d from rollups order by as_of")
        rows = cur.fetchall()
    assert len(rows) == 2
    # Two activities at 55 load each; the later day's 7 day window covers both.
    assert float(rows[-1]["load_7d"]) == 110.0


def test_one_bad_activity_does_not_stop_the_run(conn: psycopg.Connection) -> None:
    fake = FakeIntervals([{"id": "broken"}, activity("i2")], {})
    outcome = reconcile.run(conn, fake, DUBAI, date(2026, 7, 1), fetch_files=False)
    conn.commit()
    assert outcome.created == 1
    assert len(outcome.errors) == 1
    assert "broken" in outcome.errors[0]


# --- FIT-12: the grace window ----------------------------------------------


def test_an_overnight_upload_is_not_missed(conn: psycopg.Connection) -> None:
    """FIT-12: an activity uploaded the next morning is matched, not missed."""
    planned = datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI)
    prescribe(conn, planned)
    conn.commit()

    # Ten hours later, inside the 18 hour window.
    verdicts = review.missed(conn, planned + timedelta(hours=10), DUBAI)
    assert verdicts == [], "still inside the grace window"


def test_a_prescription_with_a_session_on_the_day_is_unmatched_not_missed(
    conn: psycopg.Connection,
) -> None:
    """FIT-12's load cross check: they trained, the sync is what failed."""
    planned = datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI)
    prescribe(conn, planned, discipline="ride")
    activities.ingest(conn, activity(kind="Golf"), DUBAI)
    conn.commit()

    verdicts = review.missed(conn, planned + timedelta(hours=30), DUBAI)
    assert len(verdicts) == 1
    assert verdicts[0]["missed"] is False
    assert "unmatched rather than missed" in verdicts[0]["reason"]


def test_a_silent_day_past_the_window_is_missed(conn: psycopg.Connection) -> None:
    planned = datetime(2026, 7, 27, 18, 0, tzinfo=DUBAI)
    prescription_id = prescribe(conn, planned)
    conn.commit()

    verdicts = review.missed(conn, planned + timedelta(hours=30), DUBAI)
    assert len(verdicts) == 1 and verdicts[0]["missed"] is True

    assert review.mark_missed(conn, [prescription_id]) == 1
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select status from prescriptions where id = %s", (prescription_id,))
        assert cur.fetchone()["status"] == "missed"


# --- PLAN-07: the word the platform uses is not the word the coach uses ------


def test_a_zwift_ride_closes_a_ride_prescription(conn: psycopg.Connection) -> None:
    """Found live: seventeen ingested rides, all `virtualride`, none matching.

    Every indoor ride comes back from intervals.icu as `virtualride` because
    they are all Zwift. Prescriptions say `ride`. An exact comparison means the
    two never meet, so no session closes its prescription and adherence reads
    zero however faithfully the athlete trained — with nothing erroring.
    """
    from psycopg.types.json import Jsonb

    import conftest
    from coach.ingest import review as reviewmod

    on = date(2026, 7, 30)
    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'ride', %s, 'planned') returning id",
            (block_id, datetime(2026, 7, 30, 18, tzinfo=UTC), Jsonb({"duration_s": 3600})),
        )
        prescription_id = int(cur.fetchone()["id"])
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source, duration_s) "
            "values ('virtualride', %s, %s, 'intervals', 3600) returning id",
            (datetime(2026, 7, 30, 17, tzinfo=UTC), on),
        )
        session_id = int(cur.fetchone()["id"])

    assert reviewmod.match(conn, session_id) == prescription_id


def test_an_unknown_discipline_matches_only_itself(conn: psycopg.Connection) -> None:
    """The safe direction: a word in no group must not match everything."""
    from coach.ingest import review as reviewmod

    assert reviewmod.equivalents("kitesurfing") == ["kitesurfing"]
    assert "virtualride" in reviewmod.equivalents("ride")
    assert "ride" in reviewmod.equivalents("virtualride")
    assert "weighttraining" in reviewmod.equivalents("gym")
