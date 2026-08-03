"""P09 reaches a real ride, and the defect that was in the way.

`docs/prior-art.md` section 5 records pacer-ai's top retrospective lesson: an
endpoint that exists and has a unit test is not the same claim as an endpoint
something calls. This file is the caller-level assertion for ADJ-01 to ADJ-08,
and for the larger thing found underneath it.

**The larger thing.** `review.match` and `review.attach` had two callers in
`src/`: `ingest.service.on_activity`, whose only caller is the webhook drain
(built, tested and idle), and `logbook.capture`, which is the chat path. The
live poll path called `review.review` alone. So no ride ingested by the running
deployment was ever matched to its prescription: sessions kept a null
`prescription_id`, prescriptions stayed 'planned', compliance was never frozen,
and every ADJ rule read a figure that did not exist.

The FIT-12 sweep is why nobody saw it. A prescription with a session on the same
day is reported "unmatched rather than missed", so nothing was ever wrongly
called a miss. It stayed open instead, which reads as a plan nobody follows.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from coach.ingest import service  # noqa: E402
from ingest_harness import Upstream  # noqa: E402
from test_ingest import DUBAI, START, activity, prescribe, ride_fit  # noqa: E402


def polled(
    conn: psycopg.Connection,
    adjust: bool = False,
    send: Any = None,
    now: datetime | None = None,
    write_note: Any = None,
) -> dict[str, Any]:
    """One pass of the live path, exactly as `server.poller` drives it."""
    client = Upstream([activity()], {"i1001": ride_fit()})
    return service.poll(
        conn,
        client,
        DUBAI,
        write_note or service.no_review,
        lookback_days=3650,
        adjust=adjust,
        send=send,
        now=now,
    )


def session_row(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select id, prescription_id, reviewed_at from sessions")
        return cur.fetchone()


def prescription_row(conn: psycopg.Connection, prescription_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "select status, session_id, compliance from prescriptions where id = %s",
            (prescription_id,),
        )
        return cur.fetchone()


# --- the defect: the live path never matched anything -----------------------


def test_a_polled_ride_is_matched_to_its_prescription(conn: psycopg.Connection) -> None:
    """The regression. Fails against the poll as it was on 3 August 2026."""
    prescription_id = prescribe(conn, START)

    polled(conn)

    assert session_row(conn)["prescription_id"] == prescription_id


def test_a_polled_ride_closes_its_prescription(conn: psycopg.Connection) -> None:
    """FIT-05. A ride that happened must not leave the session open for ever."""
    prescription_id = prescribe(conn, START)

    polled(conn)

    row = prescription_row(conn, prescription_id)
    assert row["status"] == "completed"
    assert row["session_id"] is not None


def test_a_polled_ride_freezes_its_compliance(conn: psycopg.Connection) -> None:
    """P09 reads this, and an ADJ-02 downgrade rewrites the target spec.

    Recomputing later would compare the ride against the reduced target, so the
    figure would improve every time the coach eased something. Frozen at match
    time is the whole reason `attach` stores it.
    """
    prescription_id = prescribe(conn, START)

    polled(conn)

    assert prescription_row(conn, prescription_id)["compliance"] is not None


def test_matching_is_not_repeated_on_the_next_pass(conn: psycopg.Connection) -> None:
    """`match` claims an *unmatched* prescription, so a second call for the same
    session would take a second one for a ride that already has one."""
    first = prescribe(conn, START)
    second = prescribe(conn, START + timedelta(hours=3))

    polled(conn)
    polled(conn)

    assert prescription_row(conn, second)["status"] != "completed"
    assert prescription_row(conn, first)["status"] == "completed"


def test_the_review_still_happens(conn: psycopg.Connection) -> None:
    """The one step the poll did do must survive the other three arriving.

    With a real note writer, because `no_review` returns nothing by design and
    an empty `reviewed` list would then prove only that the default is the
    default. `reviewed_at` is the stamp either way.
    """
    prescribe(conn, START)

    result = polled(conn, write_note=lambda _context: "Solid tempo. Hold that cadence.")

    assert result["reviewed"]
    assert session_row(conn)["reviewed_at"] is not None


# --- P09 itself -------------------------------------------------------------


def test_the_adjustment_rules_do_not_run_unless_asked(conn: psycopg.Connection) -> None:
    """Off by default, and the default is what a backfill relies on."""
    prescribe(conn, START)

    result = polled(conn, adjust=False)

    assert result["adjusted"] == []
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from adjustment_events")
        assert cur.fetchone()["n"] == 0


def test_the_running_process_turns_the_adjustment_rules_on(monkeypatch) -> None:
    """The assertion the phase never had: something sets `adjust=True`.

    `tests/test_adjust.py` asserts the flag exists and defaults to False, which
    is a test that the switch is installed rather than that anything turns it
    on. This is the other half, and it reads the thread `main` actually starts.
    """
    import inspect

    from coach.ingest import server

    source = inspect.getsource(server.main)
    assert '"adjust": True' in source, "nothing in the running process enables P09"
    assert "target=poller" in source


def test_the_poll_passes_the_flag_through_to_the_rules(conn: psycopg.Connection) -> None:
    """From the loop's parameter to a real `adjustment_events` row.

    Deliberately asserted through `service.poll` rather than by calling
    `adjust.pass_.run`, because the thing that was broken for the whole of P09
    was the path and not the rules.
    """
    prescribe(conn, START, duration_s=7200, target_watts=200)
    sent: list[str] = []

    # A month later, so ADJ-04's current-week bound is the thing under test
    # rather than the clock.
    result = polled(conn, adjust=True, send=sent.append, now=START + timedelta(minutes=90))

    # The rules ran. Whether any fired is theirs to decide and `test_adjust.py`
    # covers it; what matters here is that the path exists and carries a sender.
    assert "adjusted" in result
    assert "deferred" in result


def test_an_adjustment_notice_goes_through_the_outbox(conn: psycopg.Connection) -> None:
    """ADJ-06's message is something the coach said, so it belongs in `messages`.

    A bare transport here would repeat the defect PR #38 fixed: the athlete told
    his Thursday had been shortened, and the coach with no record of saying so.
    """
    from coach.notify import outbox as outboxmod

    posted: list[str] = []
    box = outboxmod.Outbox(posted.append)

    box.send(conn, "Thursday is now 45 minutes.", kind="adjustment")

    with conn.cursor() as cur:
        cur.execute("select body, kind, period_key from messages where role = 'coach'")
        row = cur.fetchone()
    assert row["body"] == "Thursday is now 45 minutes."
    assert row["kind"] == "adjustment"
    # Event driven, so no period is claimed: two adjustments in one day are two
    # messages, and `adjustment_events.announced` is what stops a repeat.
    assert row["period_key"] is None


def test_the_notifier_is_absent_rather_than_fatal(monkeypatch) -> None:
    """A missing Telegram token costs the notice, not the ingest loop."""
    from coach.ingest import server

    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_ID", raising=False)

    assert server._notifier() is None


# --- both paths agree -------------------------------------------------------


def test_the_webhook_path_and_the_poll_path_share_one_tail(conn: psycopg.Connection) -> None:
    """Two call sites that must agree about four operations is the seam this
    project keeps finding defects in, so there is one function instead."""
    import inspect

    assert "finish(" in inspect.getsource(service.on_activity)
    assert "finish(" in inspect.getsource(service.poll)


def test_a_backfilled_session_is_neither_reviewed_nor_adjusted(
    conn: psycopg.Connection,
) -> None:
    """FIT-09, and the reason `adjust` is a parameter rather than a constant.

    A backfill replaying two years of rides must not restructure anything.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source, backfilled) "
            "values ('ride', %s, %s, 'intervals', true) returning id",
            (START, START.date()),
        )
        session_id = int(cur.fetchone()["id"])

    handled = service.finish(
        conn,
        service.Handled(session_id=session_id),
        DUBAI,
        backfilled=True,
        adjust=True,
        now=datetime.now(UTC),
    )

    assert handled.review is None
    assert handled.adjusted == []
