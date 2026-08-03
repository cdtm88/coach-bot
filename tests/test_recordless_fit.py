"""A FIT file with no record messages, and the four things that can mean.

Found on the live deployment on 3 August 2026: two files warning "contained no
record messages" on every poll pass since 12 and 21 July.

They are not lost rides. Both are Zwift abandoned starts — complete 1.5 KB
activity files whose session names no start time and records one second of
elapsed time, zero distance and zero power. The real rides for both days are
separate files that ingested perfectly; on 21 July the real one begins seven
seconds after the stub.

So the fix is a distinction, not a rescue. A file with no samples can mean a
ride whose data was lost, which migration 015 says must be recorded rather than
dropped, or a ride that never happened, which must not be recorded at all —
a `data_unavailable` row asserting the athlete trained would suppress FIT-12's
missed check for a day he has already been correctly credited for. Nothing in
this path could tell the two apart, and the symptom of not being able to was a
warning that repeated for ever.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: E402
from coach.ingest import archive, parse, review  # noqa: E402
from conftest_fit import build_abandoned_fit, build_fit, build_recordless_fit  # noqa: E402

DUBAI = ZoneInfo("Asia/Dubai")
# The first of the two files on the deployment, by its own name.
START = datetime(2026, 7, 12, 14, 12, 18, tzinfo=UTC)


def prescribe(conn: psycopg.Connection, when: datetime = START, discipline: str = "ride") -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec) "
            "values (%s, %s, %s, %s) returning id",
            (
                conftest.ensure_block(conn),
                when,
                discipline,
                Jsonb({"duration_s": 3600, "target_watts": 150}),
            ),
        )
        return int(cur.fetchone()["id"])


def drop(sandbox: Path, name: str, data: bytes) -> Path:
    inbox = sandbox / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_bytes(data)
    return path


def session_row(conn: psycopg.Connection, session_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("select * from sessions where id = %s", (session_id,))
        return cur.fetchone()


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------


def test_an_aborted_ride_is_dated_rather_than_rejected() -> None:
    """The header and session survived; the samples did not. That is a ride."""
    parsed = parse.from_fit(build_recordless_fit(START, sport="cycling"))

    assert parsed.samples_missing is True
    assert parsed.started_at == START
    assert parsed.sport == "cycling"
    assert parsed.file_type == "activity"


def test_the_devices_own_totals_are_not_read() -> None:
    """FIT-03, and the `data_unavailable` contract in `review.weekly`.

    The session message carries `total_elapsed_time`, and the weekly review
    promises that an unreadable session's time is *missing* rather than sourced
    from somewhere else. Reading it here would make that sentence false.
    """
    parsed = parse.from_fit(build_recordless_fit(START, sport="cycling"))

    assert parsed.duration_s is None
    assert parsed.distance_m is None
    assert parsed.avg_power_w is None
    assert parsed.sample_count == 0


def test_zwifts_abandoned_start_is_not_a_ride() -> None:
    """The two files that started this, and they are not lost rides.

    Zwift writes a complete activity file when a ride is started and ended
    immediately. Recording one would claim the athlete trained on a day whose
    real ride is a separate file that ingested perfectly.
    """
    with pytest.raises(parse.AbandonedActivity):
        parse.from_fit(build_abandoned_fit(START))


def test_a_session_of_one_second_still_counts() -> None:
    """The lower boundary. Zero is a claim; anything above it is a ride."""
    parsed = parse.from_fit(build_recordless_fit(START, sport="cycling", timer_seconds=1.0))
    assert parsed.samples_missing is True


def test_a_session_of_no_seconds_does_not() -> None:
    """The other side of the same boundary."""
    with pytest.raises(parse.AbandonedActivity):
        parse.from_fit(build_recordless_fit(START, sport="cycling", timer_seconds=0.0))


def test_a_device_that_states_no_duration_is_given_the_benefit() -> None:
    """Null is unknown, not zero.

    A writer that omits the timer entirely must not be read as declaring the
    ride lasted no time; the session named a start, which is claim enough.
    """
    parsed = parse.from_fit(build_recordless_fit(START, sport="cycling", timer_seconds=None))
    assert parsed.samples_missing is True
    assert parsed.started_at == START


def test_a_workout_file_is_not_an_activity() -> None:
    with pytest.raises(parse.NotAnActivityFile):
        parse.from_fit(build_recordless_fit(START, file_type="WORKOUT"))


def test_a_settings_file_is_not_an_activity() -> None:
    """The other half of what a device leaves lying in a synced folder."""
    with pytest.raises(parse.NotAnActivityFile):
        parse.from_fit(build_recordless_fit(START, file_type="SETTINGS"))


def test_not_an_activity_is_still_unparseable() -> None:
    """Callers that do not care about the distinction keep working."""
    assert issubclass(parse.NotAnActivityFile, parse.UnparseableActivity)


def test_a_file_with_only_a_header_is_unparseable() -> None:
    """FIT-10 forbids dating it by the clock, so there is nothing to be done."""
    data = build_recordless_fit(start=None, with_session=False)
    with pytest.raises(parse.UnparseableActivity) as raised:
        parse.from_fit(data)
    assert not isinstance(raised.value, parse.NotAnActivityFile)


def test_the_files_own_creation_time_does_not_date_a_ride() -> None:
    """`file_id.time_created` says a file was made, not that a ride happened.

    Zwift stamps it on abandoned starts too, so dating a session by it is
    exactly how the two July stubs would have become two false rides.
    """
    data = build_recordless_fit(START, sport="cycling", with_session=False)
    with pytest.raises(parse.UnparseableActivity) as raised:
        parse.from_fit(data)
    assert not isinstance(raised.value, parse.AbandonedActivity)


def test_a_file_that_declares_no_type_is_read_as_an_activity() -> None:
    """Absent is not the same as 'workout'.

    Every fixture written before `file_id` was read is a file that declares no
    type, and a `file_type is None` treated as non-activity would have silently
    stopped ingesting all of them.
    """
    parsed = parse.from_fit(build_recordless_fit(START, with_file_id=False))
    assert parsed.samples_missing is True
    assert parsed.started_at == START


def test_a_normal_ride_is_unaffected() -> None:
    parsed = parse.from_fit(build_fit(START, power=[150] * 120, heart_rate=[140] * 120))
    assert parsed.samples_missing is False
    assert parsed.sample_count == 120
    assert parsed.avg_power_w == 150.0


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def test_the_ride_becomes_a_session_the_coach_can_see(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """Migration 015's rule, now reached by FIT-14's path.

    The regression for the defect itself: before this the file was dropped and
    the day looked like a rest day.
    """
    path = drop(sandbox, "2026-07-12-14-12-18.fit", build_recordless_fit(START, sport="cycling"))

    session_id = archive.ingest_file(conn, path, DUBAI)

    assert session_id is not None
    row = session_row(conn, session_id)
    assert row["data_unavailable"] is True
    assert row["local_date"].isoformat() == "2026-07-12"
    assert row["duration_s"] is None
    assert row["avg_power_w"] is None


def test_a_second_scan_does_not_ingest_it_twice(conn: psycopg.Connection, sandbox: Path) -> None:
    """FIT-04 still holds when the file has no samples to hash differently."""
    path = drop(sandbox, "aborted.fit", build_recordless_fit(START, sport="cycling"))

    first = archive.ingest_file(conn, path, DUBAI)
    second = archive.ingest_file(conn, path, DUBAI)

    assert first == second
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1


def test_an_abandoned_start_makes_no_session_and_is_not_retried(
    conn: psycopg.Connection, sandbox: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The regression for the two files on the deployment.

    No session, because the athlete did not ride — and silence on the second
    pass, because that is what was actually broken about them.
    """
    path = drop(sandbox, "2026-07-21-17-02-17.fit", build_abandoned_fit(START))

    assert archive.ingest_file(conn, path, DUBAI) is None
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0
    assert "abandoned" in archive.unreadable(conn)[0]["unreadable_reason"]

    with caplog.at_level(logging.INFO, logger="coach.ingest.archive"):
        assert archive.ingest_file(conn, path, DUBAI) is None
    assert caplog.records == []


def test_the_real_ride_beside_it_still_ingests(conn: psycopg.Connection, sandbox: Path) -> None:
    """What the deployment actually holds: a stub and, seconds later, the ride.

    The day must end up with exactly one session, and it must be the real one.
    """
    drop(sandbox, "2026-07-21-17-02-17.fit", build_abandoned_fit(START))
    drop(
        sandbox,
        "2026-07-21-17-02-24.fit",
        build_fit(START, power=[87] * 600, heart_rate=[140] * 600, sport="cycling"),
    )

    ingested = archive.scan(conn, sandbox / "inbox", DUBAI)

    assert len(ingested) == 1
    row = session_row(conn, ingested[0])
    assert row["data_unavailable"] is False
    assert row["avg_power_w"] == 87
    assert len(archive.unreadable(conn)) == 1


def test_a_workout_file_is_recorded_as_unreadable_and_not_retried(
    conn: psycopg.Connection, sandbox: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The repeat, which is what made two missing rides invisible.

    Asserting the *second* pass is silent rather than that the first one warns:
    a warning is correct once and noise for ever, and it was the for ever that
    cost three weeks here.
    """
    path = drop(sandbox, "Settings.fit", build_recordless_fit(START, file_type="SETTINGS"))

    assert archive.ingest_file(conn, path, DUBAI) is None
    recorded = archive.unreadable(conn)
    assert len(recorded) == 1
    assert "settings" in recorded[0]["unreadable_reason"]
    assert recorded[0]["unreadable_at"] is not None

    with caplog.at_level(logging.INFO, logger="coach.ingest.archive"):
        assert archive.ingest_file(conn, path, DUBAI) is None
    assert caplog.records == [], "a file judged once must not be judged again out loud"


def test_the_first_judgement_keeps_its_date(conn: psycopg.Connection, sandbox: Path) -> None:
    """`unreadable_at` is when the file first defeated us, not when we last looked.

    Backdated by hand rather than compared against a second call's stamp.
    Postgres `now()` is the *transaction* timestamp and this suite runs a test
    inside one, so both calls would read the same instant and the assertion
    would hold whether or not the column was overwritten.
    """
    path = drop(sandbox, "Settings.fit", build_recordless_fit(START, file_type="SETTINGS"))
    archive.ingest_file(conn, path, DUBAI)
    sha = archive.unreadable(conn)[0]["sha256"]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update fit_archive set unreadable_at = %s where sha256 = %s", (START, sha))

    archive.mark_unreadable(conn, sha, "looked again")

    row = archive.unreadable(conn)[0]
    assert row["unreadable_at"] == START
    # The reason is not preserved: a later, better explanation is worth having.
    assert row["unreadable_reason"] == "looked again"


def test_a_scan_ingests_the_ride_and_skips_the_settings(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """Both kinds in one folder, which is what a synced device actually produces."""
    inbox = sandbox / "inbox"
    drop(sandbox, "2026-07-12-14-12-18.fit", build_recordless_fit(START, sport="cycling"))
    drop(sandbox, "Settings.fit", build_recordless_fit(START, file_type="SETTINGS"))

    ingested = archive.scan(conn, inbox, DUBAI)

    assert len(ingested) == 1
    assert session_row(conn, ingested[0])["data_unavailable"] is True
    assert len(archive.unreadable(conn)) == 1


def test_the_poll_reports_how_many_files_it_cannot_read(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """`archive.unreadable`'s caller on the live path.

    The count belongs in `poll`'s result because that is where the operator
    already looks, and because a file judged once is never logged again — a row
    nothing reads is the same silence this whole change is about.
    """
    from coach.ingest import service
    from ingest_harness import Upstream

    def one_pass() -> dict:
        return service.poll(conn, Upstream([], {}), DUBAI, service.no_review, lookback_days=3650)

    assert one_pass()["unreadable_files"] == 0

    drop(sandbox, "Settings.fit", build_recordless_fit(START, file_type="SETTINGS"))
    result = one_pass()

    assert result["unreadable_files"] == 1
    # Standing, not per-pass: the file is judged once and skipped thereafter, so
    # a count of what this pass discovered would drop back to zero and the fact
    # would vanish from the only place it is reported.
    assert one_pass()["unreadable_files"] == 1


def test_the_file_is_still_archived_when_it_cannot_be_read(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """FIT-15: the archive keeps what it cannot use.

    A better parser or a firmware fix makes it readable later, and it is still
    the only copy of that ride in the meantime.
    """
    path = drop(sandbox, "Settings.fit", build_recordless_fit(START, file_type="SETTINGS"))
    archive.ingest_file(conn, path, DUBAI)

    stored = Path(archive.unreadable(conn)[0]["path"])
    assert stored.exists()
    assert stored.parent == archive.archive_folder()


# --------------------------------------------------------------------------
# what it must not do downstream
# --------------------------------------------------------------------------


def test_it_cannot_close_a_prescription(conn: psycopg.Connection, sandbox: Path) -> None:
    """`review.missed` states this rule; `match` had no code for it.

    Without the guard the prescription would be marked completed with
    `compliance: {completed: true}` and no deltas — which reads exactly like a
    ride that met its target.
    """
    prescription_id = prescribe(conn)
    path = drop(sandbox, "aborted.fit", build_recordless_fit(START, sport="cycling"))
    session_id = archive.ingest_file(conn, path, DUBAI)

    assert review.match(conn, session_id) is None
    with conn.cursor() as cur:
        cur.execute(
            "select status, session_id from prescriptions where id = %s", (prescription_id,)
        )
        row = cur.fetchone()
    assert row["status"] == "planned"
    assert row["session_id"] is None


def test_a_readable_ride_on_the_same_day_still_matches(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The guard is on the flag, not on the day.

    Both files exist for the same morning — the aborted one and the one that
    recorded properly — and refusing the day outright would be the mirror
    defect.
    """
    prescription_id = prescribe(conn)
    drop(sandbox, "aborted.fit", build_recordless_fit(START, sport="cycling"))
    good = drop(
        sandbox,
        "2026-07-12-good.fit",
        build_fit(START, power=[150] * 120, heart_rate=[140] * 120, sport="cycling"),
    )
    archive.ingest_file(conn, sandbox / "inbox" / "aborted.fit", DUBAI)
    good_id = archive.ingest_file(conn, good, DUBAI)

    assert review.match(conn, good_id) == prescription_id


def test_it_is_not_reviewed(conn: psycopg.Connection, sandbox: Path) -> None:
    """FIT-06's note would be the model describing a session it has never seen."""
    path = drop(sandbox, "aborted.fit", build_recordless_fit(START, sport="cycling"))
    session_id = archive.ingest_file(conn, path, DUBAI)

    def refuse(_context: dict) -> str:
        raise AssertionError("no review may be written for a session with no data")

    assert review.review(conn, session_id, refuse) is None


def test_the_day_is_not_reported_as_missed(conn: psycopg.Connection, sandbox: Path) -> None:
    """FIT-12, and the reason the row has to exist at all.

    The prescription stays open — nothing here knows whether the lost ride was
    the prescribed one — but 'missed' is a claim about the athlete and it would
    be false.
    """
    prescribe(conn)
    path = drop(sandbox, "aborted.fit", build_recordless_fit(START, sport="cycling"))
    archive.ingest_file(conn, path, DUBAI)

    verdicts = review.missed(conn, datetime(2026, 7, 20, 12, tzinfo=UTC), DUBAI)

    assert [v["missed"] for v in verdicts] == [False]
    assert verdicts[0]["signals"]["unreadable_on_day"] == 1
