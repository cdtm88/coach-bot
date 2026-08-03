"""The one-off reconciliation for the matching defect. `coach-reconcile`.

This writes to the athlete's real training history, so most of what is worth
testing is what it refuses to do: it must not claim a prescription somebody
decided about, must not let two rides close one session, and must not touch
anything at all without `--apply`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: E402
from coach.ingest import backfill  # noqa: E402

START = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


def prescribe(
    conn: psycopg.Connection,
    when: datetime = START,
    discipline: str = "ride",
    status: str = "planned",
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, %s, %s, %s) returning id",
            (
                conftest.ensure_block(conn),
                when,
                discipline,
                Jsonb({"duration_s": 3600, "target_watts": 150}),
                status,
            ),
        )
        return int(cur.fetchone()["id"])


def ride(
    conn: psycopg.Connection,
    when: datetime = START,
    discipline: str = "ride",
    backfilled: bool = False,
    reviewed: bool = True,
) -> int:
    """A session as the broken poll path left it: reviewed, and unmatched."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source, "
            "backfilled, reviewed_at, duration_s, avg_power_w) "
            "values (%s, %s, %s, 'intervals', %s, %s, 3600, 150) returning id",
            (
                discipline,
                when,
                when.date(),
                backfilled,
                when if reviewed else None,
            ),
        )
        return int(cur.fetchone()["id"])


def status_of(conn: psycopg.Connection, prescription_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "select status, session_id, compliance from prescriptions where id = %s",
            (prescription_id,),
        )
        return cur.fetchone()


# --- the dry run is the default ---------------------------------------------


def test_planning_writes_nothing(conn: psycopg.Connection) -> None:
    """The whole reason this is a command and not a migration."""
    prescription_id = prescribe(conn)
    session_id = ride(conn)

    found = backfill.plan(conn)

    assert [m.session_id for m in found] == [session_id]
    assert status_of(conn, prescription_id)["status"] == "planned"
    assert status_of(conn, prescription_id)["session_id"] is None


def test_main_without_apply_changes_nothing(conn: psycopg.Connection, monkeypatch, capsys) -> None:
    prescription_id = prescribe(conn)
    ride(conn)
    monkeypatch.setattr(backfill.db, "connect", lambda: _held(conn))

    assert backfill.main([]) == 0

    assert "Would match 1" in capsys.readouterr().out
    assert status_of(conn, prescription_id)["status"] == "planned"


def test_apply_closes_the_prescription_and_freezes_compliance(
    conn: psycopg.Connection,
) -> None:
    prescription_id = prescribe(conn)
    session_id = ride(conn)

    backfill.apply(conn)

    row = status_of(conn, prescription_id)
    assert row["status"] == "completed"
    assert row["session_id"] == session_id
    assert row["compliance"] is not None


# --- what it refuses to touch -----------------------------------------------


def test_a_suspended_prescription_is_left_alone(conn: psycopg.Connection) -> None:
    """A break is a decision the coach agreed to, and claiming it retroactively
    would erase it from the record."""
    prescription_id = prescribe(conn, status="suspended")
    ride(conn)

    assert backfill.plan(conn) == []
    assert status_of(conn, prescription_id)["status"] == "suspended"


def test_a_cancelled_prescription_is_left_alone(conn: psycopg.Connection) -> None:
    prescription_id = prescribe(conn, status="cancelled")
    ride(conn)

    assert backfill.plan(conn) == []
    assert status_of(conn, prescription_id)["status"] == "cancelled"


def test_a_missed_prescription_is_left_alone(conn: psycopg.Connection) -> None:
    """Settled is settled. The sweep already decided about this one."""
    prescription_id = prescribe(conn, status="missed")
    ride(conn)

    assert backfill.plan(conn) == []
    assert status_of(conn, prescription_id)["status"] == "missed"


def paired(
    conn: psycopg.Connection, event_id: str = "evt-1", status: str = "suspended"
) -> tuple[int, int]:
    """A ride paired upstream to a prescription, which is the path with no guard.

    `review.match` short-circuits on `paired_event_id` and filters only on
    `session_id is null`, so this is the one way a settled prescription can be
    offered up. Every other test here goes down the date path, which checks
    status itself.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status, "
            "calendar_event_id) values (%s, %s, 'ride', %s, %s, %s) returning id",
            (
                conftest.ensure_block(conn),
                START,
                Jsonb({"duration_s": 3600, "target_watts": 150}),
                status,
                event_id,
            ),
        )
        prescription_id = int(cur.fetchone()["id"])
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source, reviewed_at, "
            "duration_s, paired_event_id) values ('ride', %s, %s, 'intervals', %s, 3600, %s) "
            "returning id",
            (START, START.date(), START, event_id),
        )
        return int(cur.fetchone()["id"]), prescription_id


def test_a_suspended_prescription_paired_upstream_is_still_left_alone(
    conn: psycopg.Connection,
) -> None:
    """The guard this module actually adds, tested on the only path it can fire.

    The three tests above pass with the guard removed, because `match`'s date
    path filters status by itself. This one does not: without the re-check, a
    break the coach agreed to would be erased by a retroactive completion.
    """
    _, prescription_id = paired(conn, status="suspended")

    assert backfill.plan(conn) == []
    assert status_of(conn, prescription_id)["status"] == "suspended"


def test_a_paired_prescription_that_is_still_open_is_matched(
    conn: psycopg.Connection,
) -> None:
    """The guard must not reject the case the pairing path exists for."""
    session_id, prescription_id = paired(conn, status="planned")

    found = backfill.plan(conn)

    assert [(m.session_id, m.prescription_id) for m in found] == [(session_id, prescription_id)]


def test_a_backfilled_session_is_not_a_candidate(conn: psycopg.Connection) -> None:
    """FIT-09: loading history produces rows and rollups, not a closed plan."""
    prescribe(conn)
    ride(conn, backfilled=True)

    assert backfill.plan(conn) == []


def test_an_unplanned_ride_matches_nothing(conn: psycopg.Connection) -> None:
    """Most rides have no prescription behind them, and that is not a fault."""
    ride(conn)

    assert backfill.plan(conn) == []


def test_a_ride_that_is_already_matched_is_skipped(conn: psycopg.Connection) -> None:
    prescription_id = prescribe(conn)
    ride(conn)
    backfill.apply(conn)

    assert backfill.plan(conn) == []
    assert status_of(conn, prescription_id)["status"] == "completed"


# --- the batch problem `match` cannot see on its own ------------------------


def test_two_rides_on_one_day_do_not_both_claim_one_prescription(
    conn: psycopg.Connection,
) -> None:
    """One prescription, two rides. Only the first may close it.

    `match` answers from stored state, so a read-only dry run would offer the
    same prescription to both and the printed plan would promise something the
    apply could not keep. The simulation attaches for real and rolls back, so
    the second ride sees it already taken.
    """
    prescribe(conn)
    first = ride(conn)
    ride(conn, when=START + timedelta(hours=3))

    found = backfill.plan(conn)

    assert [m.session_id for m in found] == [first]


def test_two_prescriptions_on_one_day_take_one_ride_each(
    conn: psycopg.Connection,
) -> None:
    prescribe(conn)
    prescribe(conn, when=START + timedelta(hours=3))
    ride(conn)
    ride(conn, when=START + timedelta(hours=3))

    found = backfill.plan(conn)

    assert len({m.prescription_id for m in found}) == 2
    assert len(found) == 2


# --- scope ------------------------------------------------------------------


def test_since_bounds_the_window(conn: psycopg.Connection) -> None:
    prescribe(conn, when=START - timedelta(days=30))
    ride(conn, when=START - timedelta(days=30))
    prescribe(conn)
    recent = ride(conn)

    found = backfill.plan(conn, since=START.date())

    assert [m.session_id for m in found] == [recent]


def test_the_dry_run_leaves_the_database_exactly_as_it_found_it(
    conn: psycopg.Connection,
) -> None:
    """The simulation really attaches and then rolls back, so this is the test
    that the rollback actually happens rather than being intended."""
    prescription_id = prescribe(conn)
    session_id = ride(conn)

    backfill.plan(conn)

    assert status_of(conn, prescription_id)["status"] == "planned"
    with conn.cursor() as cur:
        cur.execute("select prescription_id from sessions where id = %s", (session_id,))
        assert cur.fetchone()["prescription_id"] is None


def test_planning_twice_gives_the_same_answer(conn: psycopg.Connection) -> None:
    """A rollback that leaked would make the second run find nothing."""
    prescribe(conn)
    ride(conn)

    assert [m.session_id for m in backfill.plan(conn)] == [
        m.session_id for m in backfill.plan(conn)
    ]


# --- what it says -----------------------------------------------------------


def test_an_empty_result_says_so_plainly(conn: psycopg.Connection) -> None:
    assert "Nothing to reconcile" in backfill.render([], applied=False)


def test_the_applied_message_names_the_consequence(conn: psycopg.Connection) -> None:
    """Adherence for past weeks changes, and the operator should read that."""
    prescribe(conn)
    ride(conn)
    written = backfill.apply(conn)

    rendered = backfill.render(written, applied=True)

    assert "Matched 1" in rendered
    assert "adherence" in rendered


class _held:
    """`db.connect` is a context manager; the test already owns the connection."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> psycopg.Connection:
        return self._conn

    def __exit__(self, *_: object) -> None:
        return None
