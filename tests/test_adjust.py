"""P09 acceptance: ADJ-01 to ADJ-08.

Done when downgrades fire automatically, an attempted upgrade is rejected, and a
missing file never restructures before the grace window and the load cross check.

Every threshold here is asserted from the module's own constant rather than a
literal, so a deliberate change to a coaching judgement moves one number and the
tests follow. A test that hard-coded 1.25 would have to be edited to permit a
change it was supposed to be checking.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

from coach.adjust import apply as applymod
from coach.adjust import authority as authmod
from coach.adjust import pass_ as passmod
from coach.adjust import triggers as trigmod
from coach.blocks import load as loadmod

DUBAI = ZoneInfo("Asia/Dubai")

# A Monday inside the current week, so ADJ-04 is satisfied without the test
# depending on which day it runs. `NOW` is midday Wednesday of that week.
THIS_MONDAY = loadmod.week_of(datetime.now(DUBAI).date())
NOW = datetime.combine(THIS_MONDAY + timedelta(days=2), datetime.min.time(), tzinfo=DUBAI).replace(
    hour=12
)


# --- fixtures ----------------------------------------------------------------


def prescribe(
    conn: psycopg.Connection,
    when: datetime,
    discipline: str = "ride",
    duration_s: int = 3600,
    intensity_factor: float | None = 0.85,
    rpe_target: float | None = None,
    status: str = "planned",
    steps: list[dict[str, Any]] | None = None,
) -> int:
    import conftest

    spec: dict[str, Any] = {
        "duration_s": duration_s,
        "purpose": "Threshold intervals",
        "discipline": discipline,
    }
    if intensity_factor is not None:
        spec["intensity_factor"] = intensity_factor
        spec["ftp_watts"] = 200
        spec["target_watts"] = round(200 * intensity_factor)
    if rpe_target is not None:
        spec["rpe_target"] = rpe_target
    if steps is not None:
        spec["steps"] = steps

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status, "
            "planned_load) values (%s, %s, %s, %s, %s, %s) returning id",
            (
                conftest.ensure_block(conn),
                when,
                discipline,
                Jsonb(spec),
                status,
                loadmod.of_spec(discipline, spec),
            ),
        )
        return int(cur.fetchone()["id"])


def ride(
    conn: psycopg.Connection,
    when: datetime,
    prescription_id: int | None = None,
    duration_ratio: float = 1.0,
    intensity_ratio: float | None = None,
    discipline: str = "ride",
) -> int:
    """A completed session with its compliance already frozen, as `attach` does."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, duration_s) "
            "values (%s, %s, %s, %s) returning id",
            (discipline, when, when.astimezone(DUBAI).date(), int(3600 * duration_ratio)),
        )
        session_id = int(cur.fetchone()["id"])

        if prescription_id is not None:
            compliance: dict[str, Any] = {"completed": True, "duration_ratio": duration_ratio}
            if intensity_ratio is not None:
                compliance["intensity_ratio"] = intensity_ratio
            cur.execute(
                "update prescriptions set status = 'completed', session_id = %s, compliance = %s "
                "where id = %s",
                (session_id, Jsonb(compliance), prescription_id),
            )
            cur.execute(
                "update sessions set prescription_id = %s where id = %s",
                (prescription_id, session_id),
            )
    return session_id


def wellness(conn: psycopg.Connection, day: date, hrv: int, resting_hr: int) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into wellness (local_date, hrv, resting_hr) values (%s, %s, %s) "
            "on conflict (local_date) do update set hrv = excluded.hrv, "
            "resting_hr = excluded.resting_hr",
            (day, hrv, resting_hr),
        )


def steady_baseline(conn: psycopg.Connection, until: date, days: int = 30) -> None:
    """A month of ordinary wellness, so a deviation has something to stand against.

    Jittered: a zero-variance baseline makes every z-score undefined and the
    deviation drops the field entirely, which is correct behaviour and useless as
    a fixture.
    """
    for offset in range(days, 0, -1):
        day = until - timedelta(days=offset)
        wellness(conn, day, hrv=60 + (offset % 5), resting_hr=50 + (offset % 3))


def events_for(conn: psycopg.Connection, prescription_id: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select trigger, evidence, before_spec, after_spec, announced, authority "
            "from adjustment_events where prescription_id = %s order by id",
            (prescription_id,),
        )
        return cur.fetchall()


def spec_of(conn: psycopg.Connection, prescription_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "select spec, planned_load, planned_for from prescriptions where id = %s",
            (prescription_id,),
        )
        return cur.fetchone()


# --- ADJ-01: the rules fire deterministically -------------------------------


def test_over_prescription_eases_the_next_hard_session(conn: psycopg.Connection) -> None:
    """Row 1 of the trigger table. The athlete went harder than asked."""
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    proposals = trigmod.evaluate(conn, session)

    assert [p.trigger for p in proposals] == ["over_prescription"]
    assert proposals[0].action == "ease"
    assert proposals[0].target_prescription_id == target


def test_the_threshold_is_the_modules_own_constant(conn: psycopg.Connection) -> None:
    """Just under fires nothing; just over fires. Asserted off the constant.

    A test written against a literal 1.25 would have to be edited to allow a
    deliberate change to the judgement, which is the opposite of what it is for.
    """
    under = float(trigmod.OVER_DURATION_RATIO) - 0.01
    over = float(trigmod.OVER_DURATION_RATIO)

    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    quiet = ride(conn, NOW - timedelta(days=1), done, duration_ratio=under)
    # Nothing at all: a hair under the line is not over it, and 1.24 is far above
    # `SHORT_RATIO` so the note rule has nothing to say either.
    assert trigmod.evaluate(conn, quiet) == []

    done2 = prescribe(conn, NOW - timedelta(days=1))
    loud = ride(conn, NOW - timedelta(days=1), done2, duration_ratio=over)
    assert [p.trigger for p in trigmod.evaluate(conn, loud)] == ["over_prescription"]


def test_abandonment_downgrades_the_next_hard_session(conn: psycopg.Connection) -> None:
    """Row 2. Stopped early is the athlete telling you something."""
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.4)

    proposals = trigmod.evaluate(conn, session)

    assert [p.trigger for p in proposals] == ["abandoned_or_faded"]
    assert proposals[0].evidence["kind"] == "abandoned"
    assert proposals[0].target_prescription_id == target


def test_a_power_fade_is_distinguished_from_abandonment(conn: psycopg.Connection) -> None:
    """Full duration at low power is fading; short at low power is stopping.

    Both match the same rule and the evidence has to say which, because "you
    stopped" and "you held on but faded" are different conversations.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.95, intensity_ratio=0.8)

    proposals = trigmod.evaluate(conn, session)

    assert proposals[0].trigger == "abandoned_or_faded"
    assert proposals[0].evidence["kind"] == "power_fade"


def test_a_recovery_flag_plus_a_poor_session_converts_to_rest(conn: psycopg.Connection) -> None:
    """Row 3, and the only rule that converts rather than eases.

    Two independent signals agree: the athlete's own body and the session itself.
    That is why it is allowed to be the strongest automatic action.
    """
    day = (NOW - timedelta(days=1)).astimezone(DUBAI).date()
    steady_baseline(conn, day)
    # A bad morning against that baseline: HRV well down, resting HR well up.
    wellness(conn, day, hrv=30, resting_hr=70)

    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.7)

    proposals = trigmod.evaluate(conn, session)

    assert proposals[0].trigger == "poor_session_on_low_recovery"
    assert proposals[0].action == "convert_to_rest"
    assert proposals[0].target_prescription_id == target
    assert proposals[0].evidence["recovery_deviation"] < float(trigmod.RECOVERY_FLAG_DEVIATION)


def test_a_poor_session_without_a_recovery_flag_does_not_convert(
    conn: psycopg.Connection,
) -> None:
    """One signal is not two. Without the recovery flag this is a short session."""
    day = (NOW - timedelta(days=1)).astimezone(DUBAI).date()
    steady_baseline(conn, day)
    wellness(conn, day, hrv=62, resting_hr=51)  # ordinary

    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.7)

    assert [p.trigger for p in trigmod.evaluate(conn, session)] == ["completed_short"]


def test_an_unusable_deviation_never_converts(conn: psycopg.Connection) -> None:
    """No baseline means the coach does not know, and not knowing is not grounds.

    The same reasoning as ADJ-08, one level down: RECOV-02 degrades a deviation
    gracefully, but a degraded-to-nothing deviation is an absence of evidence.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.7)

    # No wellness rows at all.
    assert [p.trigger for p in trigmod.evaluate(conn, session)] == ["completed_short"]


def test_a_short_session_is_noted_and_nothing_else(conn: psycopg.Connection) -> None:
    """Row 4: "note it, leave the week alone". No compensatory loading.

    Putting the missed work back is the well-meant increase ADJ-02 exists to
    forbid, and the reason it is forbidden is that the week was already the plan.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=2))
    before = spec_of(conn, target)["spec"]
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.7)

    passmod.run(conn, session, NOW, DUBAI)

    assert spec_of(conn, target)["spec"] == before
    assert events_for(conn, target) == []


def test_sustained_overperformance_defers_and_never_acts(conn: psycopg.Connection) -> None:
    """Row 5 and ADJ-03: "a review proposal, never an immediate change"."""
    # All inside the current week. `NOW` is the Wednesday, so counting back three
    # days would land on the previous Sunday — and a session in last week makes
    # `_next_hard_session` look in last week too, which is ADJ-04 working
    # correctly and not what this test is about.
    # Oldest first, so `session` ends up the most recent — the window looks
    # *back* from the session being evaluated, and evaluating the earliest one
    # would leave the others outside it.
    for offset in reversed(range(trigmod.OVERPERFORMANCE_SESSIONS)):
        when = NOW - timedelta(days=offset)
        pid = prescribe(conn, when - timedelta(hours=1))
        session = ride(conn, when, pid, duration_ratio=1.4)

    target = prescribe(conn, NOW + timedelta(days=2))
    before = spec_of(conn, target)["spec"]

    proposals = trigmod.evaluate(conn, session)
    triggers = [p.trigger for p in proposals]

    assert "sustained_overperformance" in triggers
    progression = next(p for p in proposals if p.trigger == "sustained_overperformance")
    assert progression.review_only
    assert authmod.decide(conn, progression, NOW, DUBAI).outcome is authmod.Outcome.DEFER

    # `over_prescription` also fires here and *is* allowed to act — riding 40%
    # long is unplanned load the week has to absorb. So the assertion is not "the
    # week is untouched" but the requirement itself: the progression became a
    # review item, and nothing got harder.
    outcome = passmod.run(conn, session, NOW, DUBAI)
    after = spec_of(conn, target)

    assert any("ADJ-03" in d for d in outcome.deferred)
    assert loadmod.of_spec("ride", after["spec"]) <= loadmod.of_spec("ride", before)
    filed = applymod.pending_for_review(conn, THIS_MONDAY)
    assert [f["trigger"] for f in filed] == ["sustained_overperformance"]
    assert filed[0]["deferred_by"] == "ADJ-03"


def test_an_unprescribed_ride_triggers_nothing(conn: psycopg.Connection) -> None:
    """No prescription, no comparison. A ride is not evidence about a plan it was
    never part of."""
    session = ride(conn, NOW - timedelta(days=1))

    assert trigmod.evaluate(conn, session) == []


def test_the_rules_are_deterministic(conn: psycopg.Connection) -> None:
    """ADJ-01: "Each rule fires deterministically on seeded input."

    Run twice on the same session with nothing changed in between. Same answer, or
    the rule is reading something it should not be.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    first = trigmod.evaluate(conn, session)
    second = trigmod.evaluate(conn, session)

    assert first == second


def test_only_one_restructuring_rule_wins(conn: psycopg.Connection) -> None:
    """Two rules reaching the same conclusion is the same conclusion twice.

    Abandonment and the recovery flag both match here. The recovery rule is first
    in the priority order because it has two signals, and the athlete should be
    told the better reason rather than the earlier one.
    """
    day = (NOW - timedelta(days=1)).astimezone(DUBAI).date()
    steady_baseline(conn, day)
    wellness(conn, day, hrv=30, resting_hr=70)

    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=2))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.3)

    triggers = [p.trigger for p in trigmod.evaluate(conn, session)]

    assert triggers == ["poor_session_on_low_recovery"]


def test_the_hardest_remaining_session_is_the_target(conn: psycopg.Connection) -> None:
    """Easing tomorrow's recovery spin would satisfy the letter and miss the point.

    The session that costs the most is the one whose reduction the athlete feels,
    and it is not necessarily the soonest.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=1), duration_s=1800, intensity_factor=0.55)
    hard = prescribe(conn, NOW + timedelta(days=3), duration_s=5400, intensity_factor=0.95)
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    assert trigmod.evaluate(conn, session)[0].target_prescription_id == hard


# --- ADJ-02: only ever downward ---------------------------------------------


def test_every_action_reduces_or_holds_the_load(conn: psycopg.Connection) -> None:
    """ADJ-02, checked against the projection rather than against a rule's promise.

    Each action is priced on GYM-08's combined scale and none may come out higher
    than it went in. This is the property the whole asymmetry rests on.
    """
    spec = {
        "duration_s": 3600,
        "intensity_factor": 0.9,
        "ftp_watts": 200,
        "target_watts": 180,
        "purpose": "Threshold",
    }
    before = loadmod.of_spec("ride", spec)

    for action in ("shorten", "ease", "convert_to_rest", "move_later"):
        after_spec, _ = applymod.project(spec, "ride", action, NOW)
        after = loadmod.of_spec("ride", after_spec)
        assert after <= before, f"{action} raised the load from {before} to {after}"


def test_an_upgrade_is_rejected_and_logged(
    conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """P09's acceptance: "an attempted upgrade is rejected".

    Constructed by hand, because no rule can express an increase — the action
    vocabulary has no word for one. This is the belt behind that: if a future rule
    somehow proposed a change that priced higher, the gate refuses it and says so
    at warning level, because a rule proposing an increase is a bug in the rule.
    """
    target = prescribe(conn, NOW + timedelta(days=2), duration_s=3600, intensity_factor=0.7)
    current = spec_of(conn, target)["planned_load"]

    hostile = trigmod.Proposal(
        trigger="hand_made_upgrade",
        action="ease",
        target_prescription_id=target,
        session_id=None,
        reason="pretend this makes it harder",
    )

    with caplog.at_level("WARNING"):
        decision = authmod.decide(conn, hostile, NOW, DUBAI, projected_load=Decimal(current) * 2)

    assert decision.outcome is authmod.Outcome.REJECT
    assert decision.requirement == "ADJ-02"
    assert "ADJ-02 rejected" in caplog.text


def test_an_unpriced_change_is_refused(conn: psycopg.Connection) -> None:
    """A change that cannot be shown to reduce load is not allowed to happen.

    Failing closed rather than open: "no price available" must not read as "no
    increase", which is the mistake that would let the one dangerous direction
    through.
    """
    target = prescribe(conn, NOW + timedelta(days=2))
    proposal = trigmod.Proposal("t", "ease", target, None, "why")

    decision = authmod.decide(conn, proposal, NOW, DUBAI, projected_load=None)

    assert decision.outcome is authmod.Outcome.REJECT
    assert decision.requirement == "ADJ-02"


def test_the_load_is_priced_on_the_week_not_the_session(conn: psycopg.Connection) -> None:
    """ADJ-02 says "weekly load", and the check has to be about the week.

    A change that reduced Thursday while somehow adding to Saturday would pass a
    per-session test and break the actual requirement.
    """
    prescribe(conn, NOW + timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=3))
    weeks = {w.starts_on: w for w in loadmod.planned_weeks(conn)}
    week_total = weeks[THIS_MONDAY].total

    ok, detail = authmod._reduces_load(
        conn, trigmod.Proposal("t", "ease", target, None, "why"), Decimal("1")
    )

    assert ok
    assert str(week_total) in detail


# --- ADJ-04: the current week only ------------------------------------------


def test_a_target_beyond_this_week_is_deferred(conn: psycopg.Connection) -> None:
    """ADJ-04: "No prescription dated beyond the current week is modified"."""
    target = prescribe(conn, NOW + timedelta(days=14))
    proposal = trigmod.Proposal("t", "ease", target, None, "why")

    decision = authmod.decide(conn, proposal, NOW, DUBAI, projected_load=Decimal("1"))

    assert decision.outcome is authmod.Outcome.DEFER
    assert decision.requirement == "ADJ-04"


def test_a_rule_will_not_reach_into_next_week(conn: psycopg.Connection) -> None:
    """The rule itself only looks inside the week, so nothing to defer arises.

    Belt and braces with the gate above: the rule not proposing it and the gate
    refusing it are two independent reasons ADJ-04 holds.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(days=14))  # next week, and the only candidate
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    assert [p.trigger for p in trigmod.evaluate(conn, session)] == []


# --- ADJ-05: one per week, never the same twice -----------------------------


def test_the_second_trigger_in_a_week_is_deferred(conn: psycopg.Connection) -> None:
    """ADJ-05: "Second trigger in the same week is queued for the review instead."

    And design section 10 says why: "repeated triggering means the block is wrong,
    which is a conversation, not a rule."
    """
    first_done = prescribe(conn, NOW - timedelta(days=2))
    first_target = prescribe(conn, NOW + timedelta(days=1))
    first_session = ride(conn, NOW - timedelta(days=2), first_done, duration_ratio=1.4)
    outcome = passmod.run(conn, first_session, NOW, DUBAI)
    assert outcome.changed

    second_done = prescribe(conn, NOW - timedelta(days=1))
    second_target = prescribe(conn, NOW + timedelta(days=3))
    second_session = ride(conn, NOW - timedelta(days=1), second_done, duration_ratio=1.4)
    before = spec_of(conn, second_target)["spec"]

    second = passmod.run(conn, second_session, NOW, DUBAI)

    assert not second.changed
    assert any("ADJ-05" in d for d in second.deferred)
    assert spec_of(conn, second_target)["spec"] == before
    assert applymod.pending_for_review(conn, THIS_MONDAY)
    del first_target


def test_the_same_prescription_is_never_adjusted_twice(conn: psycopg.Connection) -> None:
    """ADJ-05's second clause, and it is a separate rule from the weekly count.

    The count stops a bad week becoming five changes; this stops one session being
    whittled down by a rule that fires again tomorrow.
    """
    target = prescribe(conn, NOW + timedelta(days=2))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into adjustment_events (prescription_id, trigger, evidence, before_spec, "
            "after_spec, authority) values (%s, 'earlier', '{}', '{}', '{}', 'automatic')",
            (target,),
        )

    assert authmod.already_adjusted(conn, target)
    decision = authmod.decide(
        conn, trigmod.Proposal("t", "ease", target, None, "why"), NOW, DUBAI, Decimal("1")
    )
    assert decision.outcome is authmod.Outcome.DEFER
    assert decision.requirement == "ADJ-05"


def test_a_calendar_placement_does_not_spend_the_weeks_budget(
    conn: psycopg.Connection,
) -> None:
    """The reason `authority` is a column rather than a list of trigger names.

    PLAN-04 writes an adjustment_events row when it moves a session around a
    meeting, and PLAN-12 writes one when the athlete moves it themselves. Neither
    is the coach spending its ADJ-05 authority, and counting them would silence
    the coach for a week because the athlete rescheduled something.
    """
    target = prescribe(conn, NOW + timedelta(days=2))
    with conn.transaction(), conn.cursor() as cur:
        for trigger, authority in (("calendar_conflict", "calendar"), ("athlete_edit", "athlete")):
            cur.execute(
                "insert into adjustment_events (prescription_id, trigger, evidence, before_spec, "
                "after_spec, authority) values (%s, %s, '{}', '{}', '{}', %s)",
                (target, trigger, authority),
            )

    assert authmod.autonomous_this_week(conn, THIS_MONDAY) == 0
    assert not authmod.already_adjusted(conn, target)


def test_p08s_writers_declare_their_authority() -> None:
    """The column defaults to 'automatic', so the other two writers must say so.

    Asserted on the source because the alternative is a live test per writer, and
    what matters is that neither insert relies on the default.
    """
    from coach.plans import publish, sync

    assert "'calendar'" in inspect.getsource(publish._record)
    assert "'athlete'" in inspect.getsource(sync.apply)


# --- ADJ-06: the twelve hour notice ----------------------------------------


def test_a_change_inside_twelve_hours_sends_a_message(conn: psycopg.Connection) -> None:
    """ADJ-06, the loud path. The athlete is about to do the thing that changed."""
    soon = NOW + timedelta(hours=6)
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, soon)
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    sent: list[str] = []
    outcome = passmod.run(conn, session, NOW, DUBAI, send=sent.append)

    assert outcome.applied[0].notified
    assert len(sent) == 1
    assert "eased" in sent[0]


def test_a_change_outside_twelve_hours_is_silent(conn: psycopg.Connection) -> None:
    """ADJ-06, the normal path. "The reason is written to the calendar event only."

    Not a lesser path: unnecessary messages are a real cost, which is why CHAT-11
    has an interruption budget at all.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=3))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    sent: list[str] = []
    outcome = passmod.run(conn, session, NOW, DUBAI, send=sent.append)

    assert outcome.applied[0].notified is False
    assert sent == []
    # But the change happened and is recorded.
    assert events_for(conn, target)


def test_the_notice_boundary_is_the_original_start(conn: psycopg.Connection) -> None:
    """Measured against the start the athlete was expecting, not the new one.

    Which is the requirement's wording and the only sensible reading: what makes
    it urgent is that they are about to ride it.
    """
    assert applymod.needs_notice(NOW + timedelta(hours=applymod.NOTICE_HOURS - 1), NOW)
    assert not applymod.needs_notice(NOW + timedelta(hours=applymod.NOTICE_HOURS + 1), NOW)


def test_the_notice_is_an_assertion_not_a_question(conn: psycopg.Connection) -> None:
    """CHAT-04 allows one question per message and this is not where to spend it.

    The change has already happened; asking permission after the fact is worse
    than either asking first or not asking.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    prescribe(conn, NOW + timedelta(hours=6))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    sent: list[str] = []
    passmod.run(conn, session, NOW, DUBAI, send=sent.append)

    assert "?" not in sent[0]


def test_a_missing_transport_does_not_lose_the_change(
    conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """The change is the point; the message is a courtesy that can fail.

    ADJ-07's row is written either way, so the reason survives even when nobody
    could be told.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(hours=6))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    with caplog.at_level("WARNING"):
        outcome = passmod.run(conn, session, NOW, DUBAI, send=None)

    assert outcome.changed
    assert events_for(conn, target)
    assert "ADJ-06 wanted a message" in caplog.text


# --- ADJ-07: every change is recorded --------------------------------------


def test_every_automatic_change_records_trigger_evidence_and_both_specs(
    conn: psycopg.Connection,
) -> None:
    """ADJ-07: "Asking why a session moved returns the stored reason, not a
    reconstruction"."""
    done = prescribe(conn, NOW - timedelta(days=1))
    target = prescribe(conn, NOW + timedelta(days=3))
    before = spec_of(conn, target)["spec"]
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=1.4)

    passmod.run(conn, session, NOW, DUBAI)

    rows = events_for(conn, target)
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger"] == "over_prescription"
    assert row["authority"] == "automatic"
    assert row["before_spec"] == before
    assert row["after_spec"] != before
    assert row["evidence"]["action"] == "ease"
    assert row["evidence"]["decided_by"] == "ADJ-02"
    assert row["evidence"]["load_after"] < row["evidence"]["load_before"]
    assert "1.40" in row["evidence"]["reason"] or "1.4" in row["evidence"]["reason"]


def test_the_record_and_the_change_are_one_transaction(conn: psycopg.Connection) -> None:
    """A row written after the change could be lost and leave a silent difference.

    That is the exact failure ADJ-07 is about, so the assertion is on the source:
    the insert is inside the same `with conn.transaction()` as the update.
    """
    source = inspect.getsource(applymod.execute)
    block = source[source.index("with conn.transaction()") :]
    update = block.index("update prescriptions set spec")
    insert = block.index("insert into adjustment_events")
    # Both inside the block, and the next transaction starts only afterwards.
    assert update < insert
    assert "with conn.transaction()" not in block[:insert].replace(
        "with conn.transaction(), conn.cursor() as cur:", "", 1
    )


def test_a_note_does_not_write_an_adjustment_row(conn: psycopg.Connection) -> None:
    """`adjustment_events` is one row per change, and its foreign key says so.

    A "noted, no change" entry belongs in the episodic archive, which is where the
    Sunday review reads the week's story from.
    """
    done = prescribe(conn, NOW - timedelta(days=1))
    session = ride(conn, NOW - timedelta(days=1), done, duration_ratio=0.7)

    passmod.run(conn, session, NOW, DUBAI)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from adjustment_events")
        assert cur.fetchone()["n"] == 0
        cur.execute("select body from notes where kind = 'observation'")
        assert any("completed_short" in r["body"] for r in cur.fetchall())


# --- ADJ-08: the absence trap ----------------------------------------------


def test_an_unavailable_signal_defers_rather_than_acting(conn: psycopg.Connection) -> None:
    """ADJ-08: "With wellness unavailable, the system asks rather than acts."."""
    target = prescribe(conn, NOW + timedelta(days=2))
    proposal = trigmod.Proposal("t", "ease", target, None, "why")

    decision = authmod.decide(
        conn, proposal, NOW, DUBAI, projected_load=Decimal("1"), safe_to_act=False
    )

    assert decision.outcome is authmod.Outcome.DEFER
    assert decision.requirement == "ADJ-08"


def test_the_grace_window_and_the_load_check_come_from_review(
    conn: psycopg.Connection,
) -> None:
    """ADJ-08's two conditions were built in P05 and this is where they are used.

    `review.missed` already applies the grace window and cross-checks the load
    signal, shipping `safe_to_act` for exactly this. Duplicating that logic here
    would be a second implementation of the absence trap, which is how the two
    come to disagree.
    """
    from coach.ingest import review

    assert "safe_to_act" in inspect.getsource(review.missed)
    assert "safe_to_act" in inspect.getsource(authmod.decide)
    # And the gate honours it.
    assert review.GRACE_HOURS > 0


# --- the deferral queue, which REV-04 will read ---------------------------


def test_a_deferral_is_filed_once_per_prescription_and_trigger(
    conn: psycopg.Connection,
) -> None:
    """A rule that fires again next ingest must not give the review a duplicate."""
    target = prescribe(conn, NOW + timedelta(days=14))
    proposal = trigmod.Proposal("over_prescription", "ease", target, None, "why")
    decision = authmod.Decision(authmod.Outcome.DEFER, "next week", "ADJ-04")

    first = applymod.defer(conn, proposal, decision, THIS_MONDAY)
    second = applymod.defer(conn, proposal, decision, THIS_MONDAY)

    assert first is not None
    assert second is None
    assert len(applymod.pending_for_review(conn, THIS_MONDAY)) == 1


def test_a_deferral_changes_nothing(conn: psycopg.Connection) -> None:
    """The whole point: it is a proposal waiting for a person."""
    target = prescribe(conn, NOW + timedelta(days=14))
    before = spec_of(conn, target)

    applymod.defer(
        conn,
        trigmod.Proposal("t", "ease", target, None, "why"),
        authmod.Decision(authmod.Outcome.DEFER, "next week", "ADJ-04"),
        THIS_MONDAY,
    )

    after = spec_of(conn, target)
    assert after["spec"] == before["spec"]
    assert after["planned_load"] == before["planned_load"]
    assert events_for(conn, target) == []


def test_the_review_reads_the_week_it_asks_for(conn: psycopg.Connection) -> None:
    """REV-04 reads a week at a time; a proposal about a past week is history."""
    target = prescribe(conn, NOW + timedelta(days=14))
    applymod.defer(
        conn,
        trigmod.Proposal("t", "ease", target, None, "why"),
        authmod.Decision(authmod.Outcome.DEFER, "next week", "ADJ-04"),
        THIS_MONDAY,
    )

    assert applymod.pending_for_review(conn, THIS_MONDAY)
    assert applymod.pending_for_review(conn, THIS_MONDAY - timedelta(days=7)) == []


# --- what each action actually does ---------------------------------------


def test_easing_a_structured_session_drops_its_intervals(conn: psycopg.Connection) -> None:
    """ "Downgrade the next hard session to endurance", and intervals are not that.

    Leaving the step list on while lowering the target would publish intervals at
    endurance power — neither the session that was planned nor the one intended.
    """
    spec = {
        "duration_s": 3600,
        "intensity_factor": 0.95,
        "ftp_watts": 200,
        "target_watts": 190,
        "steps": [{"duration_s": 300, "power_pct": 105}],
    }

    after, _ = applymod.project(spec, "ride", "ease", NOW)

    assert "steps" not in after
    assert after["intensity_factor"] < spec["intensity_factor"]
    assert after["target_watts"] < spec["target_watts"]


def test_a_gym_session_eases_by_rpe(conn: psycopg.Connection) -> None:
    """GYM-01 makes RPE the gym's intensity; there is no power number to lower."""
    spec = {"duration_s": 2700, "rpe_target": 8, "purpose": "Strength"}

    after, _ = applymod.project(spec, "gym", "ease", NOW)

    assert after["rpe_target"] < 8
    assert loadmod.of_spec("gym", after) < loadmod.of_spec("gym", spec)


def test_converting_to_rest_leaves_something_visible(conn: psycopg.Connection) -> None:
    """A rest day the athlete can see is a decision; an empty slot is an absence."""
    spec = {"duration_s": 5400, "intensity_factor": 0.9, "ftp_watts": 200, "target_watts": 180}

    after, _ = applymod.project(spec, "ride", "convert_to_rest", NOW)

    assert after["duration_s"] > 0
    assert after["duration_s"] < spec["duration_s"]
    assert after["intensity_factor"] < spec["intensity_factor"]
    assert "Easy spin" in after["purpose"]
    assert loadmod.of_spec("ride", after) < loadmod.of_spec("ride", spec)


def test_moving_later_keeps_the_session_intact(conn: psycopg.Connection) -> None:
    """It buys recovery time without touching the work, which is why it counts as
    a downgrade at all."""
    spec = {"duration_s": 3600, "intensity_factor": 0.9, "ftp_watts": 200, "target_watts": 180}

    after, when = applymod.project(spec, "ride", "move_later", NOW.replace(hour=18))

    assert after == spec
    assert when.hour == applymod.LATEST_START.hour
    assert when.date() == NOW.date(), "ADJ-04 and PLAN-04 both forbid another day"


# --- the ingest wiring ----------------------------------------------------


def test_the_rules_run_on_ingest_and_only_when_asked() -> None:
    """ADJ-01 says "on FIT ingest", and a backfill must not restructure history.

    Replaying two years of rides would burn each week's ADJ-05 budget on a ride
    from 2024. ADJ-04 would reject them, but a backfill is the one path where
    "would have been rejected anyway" is not good enough.
    """
    from coach.ingest import service

    source = inspect.getsource(service.on_activity)
    assert "adjust: bool = False" in inspect.getsource(service.on_activity).replace("\n", " ") or (
        "adjust" in inspect.signature(service.on_activity).parameters
    )
    assert inspect.signature(service.on_activity).parameters["adjust"].default is False
    assert "not backfilled" in source


def test_compliance_is_frozen_when_the_session_is_matched(conn: psycopg.Connection) -> None:
    """The reason `prescriptions.compliance` exists, and it is not convenience.

    An `ease` rewrites the target spec. Recomputing compliance afterwards would
    compare the ride against the *reduced* target, so the figure would improve
    every time the coach downgraded something — ADJ-01's own rules would then be
    reading a number their own actions had moved.
    """
    from coach.ingest import review

    target = prescribe(conn, NOW - timedelta(days=1), duration_s=3600)
    session = ride(conn, NOW - timedelta(days=1))
    review.attach(conn, session, target)

    with conn.cursor() as cur:
        cur.execute("select compliance from prescriptions where id = %s", (target,))
        frozen = cur.fetchone()["compliance"]
    assert frozen and frozen["duration_ratio"] == 1.0

    # Now shrink the target as an `ease` would, and confirm the stored figure holds.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update prescriptions set spec = jsonb_set(spec, '{duration_s}', '1800') where id = %s",
            (target,),
        )
    with conn.cursor() as cur:
        cur.execute("select compliance from prescriptions where id = %s", (target,))
        assert cur.fetchone()["compliance"]["duration_ratio"] == 1.0
    # Recomputing would have said 2.0, which is the bug this prevents.
    assert review.compliance(conn, session, target).duration_ratio == 2.0
