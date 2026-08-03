"""A FIT file with no record messages, and the three things that can mean.

Found on the live deployment on 3 August 2026: two files warning "contained no
record messages" on every poll pass since 12 and 21 July, so two of the
athlete's activities were absent from the system and the only sign was a log
line that repeated for ever.

The repeat was the smaller half. Migration 015 had already settled what a
session with no usable data is worth — the row stays, because deleting the
evidence that he trained is what turns a session he did into one FIT-12 reports
he skipped — and the watched folder path decided the same question the opposite
way, in a different module, by dropping the file.
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
from conftest_fit import build_fit, build_recordless_fit  # noqa: E402

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


def test_a_file_with_no_records_and_no_time_is_unparseable() -> None:
    """FIT-10 forbids dating it by the clock, so there is nothing to be done."""
    data = build_recordless_fit(start=None, with_session=False)
    with pytest.raises(parse.UnparseableActivity) as raised:
        parse.from_fit(data)
    assert not isinstance(raised.value, parse.NotAnActivityFile)


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
