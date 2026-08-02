"""What a break does to the plan: BREAK-01 to BREAK-04.

P04 needed one question answered — is today inside a break — because HLTH-13
suppresses weigh-in prompting during one. These are the rest: suspension,
upstream cancellation, adherence, and the fact that coming back is a proposal
rather than a resumption.

The upstream half is driven through a fake client. That is not a shortcut: the
requirement is that a failure to cancel upstream must not undo the local
suspension, and the only way to assert that is to make the call fail on purpose.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from coach.health import breaks
from coach.ingest import reconcile

MONDAY = date(2026, 7, 27)


class FakeCalendar:
    """Just the one method BREAK-02 uses."""

    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    def delete_events(self, external_ids: list[str]) -> int:
        if self.fail:
            raise RuntimeError("upstream is down")
        self.deleted.extend(external_ids)
        return len(external_ids)


def prescribe(
    conn: psycopg.Connection, on: date, external_id: str | None = None, status: str = "planned"
) -> int:
    from psycopg.types.json import Jsonb

    import conftest

    block_id = conftest.ensure_block(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status, "
            "external_id) values (%s, %s, 'ride', %s, %s, %s) returning id",
            (
                block_id,
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb({"duration_s": 3600, "intensity_factor": 0.68}),
                status,
                external_id,
            ),
        )
        return int(cur.fetchone()["id"])


def status_of(conn: psycopg.Connection, prescription_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("select status from prescriptions where id = %s", (prescription_id,))
        return cur.fetchone()["status"]


# --- BREAK-02: suspension ----------------------------------------------------


def test_a_break_suspends_the_prescriptions_it_covers(conn: psycopg.Connection) -> None:
    inside = prescribe(conn, MONDAY + timedelta(days=2))
    after = prescribe(conn, MONDAY + timedelta(days=20))

    brk_id = breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=13))
    brk = breaks.active_on(conn, MONDAY)
    assert brk is not None and brk.id == brk_id

    result = breaks.suspend(conn, brk)

    assert result.prescription_ids == [inside]
    assert status_of(conn, inside) == "suspended"
    assert status_of(conn, after) == "planned"


def test_suspended_is_not_missed_and_not_cancelled(conn: psycopg.Connection) -> None:
    """The distinction is the requirement, not a naming preference.

    'missed' feeds the ADJ-01 triggers and depresses adherence. 'cancelled' says
    the athlete declined a session. Neither happened: the coach agreed to the
    break.
    """
    inside = prescribe(conn, MONDAY + timedelta(days=1))
    breaks.create(conn, "travel", MONDAY, MONDAY + timedelta(days=6))

    breaks.suspend(conn, breaks.active_on(conn, MONDAY))

    assert status_of(conn, inside) == "suspended"


def test_an_open_ended_break_suspends_everything_from_its_start(
    conn: psycopg.Connection,
) -> None:
    """BREAK-01 allows no end date, and illness is open ended by default."""
    soon = prescribe(conn, MONDAY + timedelta(days=3))
    far = prescribe(conn, MONDAY + timedelta(days=60))
    breaks.create(conn, "illness", MONDAY)

    result = breaks.suspend(conn, breaks.active_on(conn, MONDAY))

    assert sorted(result.prescription_ids) == sorted([soon, far])


def test_a_completed_session_is_left_alone(conn: psycopg.Connection) -> None:
    """Suspension is about what has not happened yet."""
    done = prescribe(conn, MONDAY + timedelta(days=1), status="completed")
    breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=6))

    breaks.suspend(conn, breaks.active_on(conn, MONDAY))

    assert status_of(conn, done) == "completed"


def test_the_planned_events_are_cancelled_upstream(conn: psycopg.Connection) -> None:
    prescribe(conn, MONDAY + timedelta(days=1), external_id="coach-bot:presc:1")
    breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=6))
    api = FakeCalendar()

    result = breaks.cancel_upstream(api, breaks.suspend(conn, breaks.active_on(conn, MONDAY)))

    assert api.deleted == ["coach-bot:presc:1"]
    assert result.reported == 1


def test_an_upstream_failure_does_not_undo_the_local_suspension(
    conn: psycopg.Connection,
) -> None:
    """The plan is already suspended and PLAN-05's sweep removes the orphans.

    Refusing to suspend locally because a network call failed would be the wrong
    way round — the athlete is away either way.
    """
    inside = prescribe(conn, MONDAY + timedelta(days=1), external_id="coach-bot:presc:1")
    breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=6))

    result = breaks.cancel_upstream(
        FakeCalendar(fail=True), breaks.suspend(conn, breaks.active_on(conn, MONDAY))
    )

    assert status_of(conn, inside) == "suspended"
    assert result.reported == 0


# --- BREAK-02: adherence -----------------------------------------------------


def test_rollups_exclude_break_days_from_adherence(conn: psycopg.Connection) -> None:
    """BREAK-02's acceptance, exactly as written.

    Two sessions offered and one taken is 50%. A fortnight of suspended sessions
    must leave that at 50% rather than dragging it toward zero — which means
    they leave the denominator, not just the numerator.
    """
    prescribe(conn, MONDAY - timedelta(days=3), status="completed")
    prescribe(conn, MONDAY - timedelta(days=2), status="missed")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, source) "
            "values ('ride', %s, %s, 'intervals')",
            (datetime.combine(MONDAY, datetime.min.time()).replace(tzinfo=UTC), MONDAY),
        )

    reconcile.recompute_rollups(conn)
    before = _adherence(conn, MONDAY)

    for offset in range(1, 8):
        prescribe(conn, MONDAY + timedelta(days=offset))
    breaks.create(conn, "holiday", MONDAY + timedelta(days=1), MONDAY + timedelta(days=13))
    breaks.suspend(conn, breaks.active_on(conn, MONDAY + timedelta(days=1)))
    reconcile.recompute_rollups(conn)

    assert before == Decimal("0.5")
    assert _adherence(conn, MONDAY) == before


def _adherence(conn: psycopg.Connection, on: date) -> Any:
    with conn.cursor() as cur:
        cur.execute("select adherence_rate from rollups where as_of = %s", (on,))
        row = cur.fetchone()
    return row["adherence_rate"] if row else None


# --- the shared predicate ----------------------------------------------------


def test_a_closed_break_still_covered_the_days_it_covered(conn: psycopg.Connection) -> None:
    """Adherence for a past break must stay excluded forever, not only while it runs."""
    brk_id = breaks.create(conn, "travel", MONDAY, MONDAY + timedelta(days=3))
    breaks.end(conn, brk_id, MONDAY + timedelta(days=3))

    covered = breaks.covered_days(conn, MONDAY - timedelta(days=1), MONDAY + timedelta(days=5))

    assert covered == [MONDAY + timedelta(days=n) for n in range(4)]


def test_an_open_illness_break_covers_past_its_end_date(conn: psycopg.Connection) -> None:
    """BREAK-04, in the predicate rather than in a flag someone has to remember."""
    breaks.create(conn, "illness", MONDAY, MONDAY + timedelta(days=2))

    covered = breaks.covered_days(conn, MONDAY, MONDAY + timedelta(days=10))

    assert len(covered) == 11


# --- BREAK-03: coming back ---------------------------------------------------


def test_a_two_week_break_produces_a_reduced_re_entry(conn: psycopg.Connection) -> None:
    """BREAK-03's acceptance, exactly as written."""
    _rollup(conn, MONDAY - timedelta(days=1), load_7d=Decimal(400))
    brk_id = breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=13))
    brk = breaks.active_on(conn, MONDAY)

    proposal = breaks.re_entry(conn, brk, MONDAY + timedelta(days=14))

    assert proposal is not None
    assert proposal.break_id == brk_id
    assert proposal.days_away == 14
    assert proposal.baseline_load == Decimal(400)
    assert proposal.weeks == (Decimal(240), Decimal(320))  # 60% then 80%
    assert "240" in proposal.render()


def test_every_re_entry_starts_below_the_pre_break_week(conn: psycopg.Connection) -> None:
    """The only property the PRD actually fixes, over the whole ladder."""
    _rollup(conn, MONDAY - timedelta(days=1), load_7d=Decimal(400))
    brk = breaks.Break(id=1, kind="holiday", starts_on=MONDAY, ends_on=None, reason=None)

    for days in (7, 14, 21, 28, 60):
        proposal = breaks.re_entry(conn, brk, MONDAY + timedelta(days=days))
        assert proposal is not None, days
        assert proposal.weeks[0] < Decimal(400), days
        assert list(proposal.weeks) == sorted(proposal.weeks), days


def test_a_long_weekend_is_not_a_break_to_come_back_from(conn: psycopg.Connection) -> None:
    brk = breaks.Break(id=1, kind="holiday", starts_on=MONDAY, ends_on=None, reason=None)

    assert breaks.re_entry(conn, brk, MONDAY + timedelta(days=4)) is None


def test_no_pre_break_figure_produces_a_proposal_that_says_so(
    conn: psycopg.Connection,
) -> None:
    """Silence would be worse: the athlete is still coming back from two weeks off."""
    brk = breaks.Break(id=1, kind="holiday", starts_on=MONDAY, ends_on=None, reason=None)

    proposal = breaks.re_entry(conn, brk, MONDAY + timedelta(days=14))

    assert proposal is not None
    assert proposal.baseline_load is None
    assert "conversation" in proposal.render()


def test_the_re_entry_is_offered_once(conn: psycopg.Connection) -> None:
    """The review runs every Sunday; the proposal is not a weekly reminder."""
    brk_id = breaks.create(conn, "holiday", MONDAY, MONDAY + timedelta(days=13))
    breaks.end(conn, brk_id, MONDAY + timedelta(days=13))
    on = MONDAY + timedelta(days=14)

    assert breaks.awaiting_re_entry(conn, on) is not None
    breaks.mark_re_entry_proposed(conn, brk_id, on)
    assert breaks.awaiting_re_entry(conn, on) is None


def test_an_illness_break_is_not_offered_a_re_entry_until_it_is_closed(
    conn: psycopg.Connection,
) -> None:
    """BREAK-04: offering a re-entry is a resumption in everything but name."""
    brk_id = breaks.create(conn, "illness", MONDAY, MONDAY + timedelta(days=3))
    on = MONDAY + timedelta(days=10)

    assert breaks.awaiting_re_entry(conn, on) is None

    breaks.end(conn, brk_id, MONDAY + timedelta(days=9))
    assert breaks.awaiting_re_entry(conn, on) is not None


def _rollup(conn: psycopg.Connection, on: date, load_7d: Decimal) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into rollups (as_of, load_7d) values (%s, %s) "
            "on conflict (as_of) do update set load_7d = excluded.load_7d",
            (on, load_7d),
        )


@pytest.fixture(autouse=True)
def _no_ambient_breaks(conn: psycopg.Connection) -> None:
    """Each test states its own breaks; nothing here inherits one."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from breaks")
