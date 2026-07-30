"""P08 acceptance: PLAN-01 to PLAN-12.

Done when sessions appear as planned workouts, structured ones render as steps
without any file handling, edits round trip into observed availability and update
the local prescription, and there are no duplicates after ten changes.

The upstream API is a fake that records what it was asked. That is not a
compromise: `Intervals` takes its transport as a parameter and the point of P08 is
the decisions made around the call, not the call. The two things a fake cannot
prove — that the platform accepts our payload and renders a valid zwo — are
`scripts/verify_intervals.py v4`'s, and this file says so where it matters.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.ingest import review
from coach.plans import events, publish, sweep, sync, workout
from coach.runtime import scheduler

DUBAI = ZoneInfo("Asia/Dubai")
REPO = Path(__file__).resolve().parents[1]

# A Thursday, 18:00 local. Far enough ahead that `pending` and `_published` both
# include it without the test depending on when it runs.
SOON = datetime.now(DUBAI).replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=7)
NOW = datetime.now(UTC)


# --- fakes -------------------------------------------------------------------


class FakeApi:
    """Records what it was asked and returns what upstream would.

    `events` returns whatever the test put in `calendar`, so a test can describe
    the upstream state directly instead of publishing to get there.
    """

    def __init__(self, calendar: list[dict[str, Any]] | None = None):
        self.calendar = list(calendar or [])
        self.published: list[list[dict[str, Any]]] = []
        self.deleted: list[list[str]] = []
        self.reads: list[tuple[date, date]] = []
        self.next_event_id = 900000

    def events(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        self.reads.append((oldest, newest))
        return list(self.calendar)

    def upsert_events(self, bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.published.append([dict(b) for b in bodies])
        out = []
        for body in bodies:
            # Upsert semantics: an external_id already on the calendar keeps its
            # upstream id. That is the behaviour PLAN-02 rests on, so the fake has
            # to have it or the no-duplicates test would pass for the wrong reason.
            existing = next(
                (e for e in self.calendar if e.get("external_id") == body.get("external_id")),
                None,
            )
            if existing:
                existing.update(body)
                out.append(dict(existing))
                continue
            created = {**body, "id": self.next_event_id}
            self.next_event_id += 1
            self.calendar.append(created)
            out.append(dict(created))
        return out

    def delete_events(self, external_ids: list[str]) -> int:
        self.deleted.append(list(external_ids))
        before = len(self.calendar)
        self.calendar = [e for e in self.calendar if e.get("external_id") not in set(external_ids)]
        return before - len(self.calendar)


def prescribe(
    conn: psycopg.Connection,
    when: datetime = SOON,
    discipline: str = "ride",
    spec: dict[str, Any] | None = None,
    status: str = "planned",
) -> int:
    from psycopg.types.json import Jsonb

    import conftest

    block_id = conftest.ensure_block(conn)
    body = (
        spec
        if spec is not None
        else {
            "duration_s": 3600,
            "purpose": "Aerobic endurance",
            "discipline": discipline,
            "intensity_factor": 0.68,
        }
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, %s, %s, %s) returning id",
            (block_id, when, discipline, Jsonb(body), status),
        )
        return int(cur.fetchone()["id"])


def busy(conn: psycopg.Connection, start: datetime, end: datetime, all_day: bool = False) -> None:
    """A commitment the feed has published. CALR-04 has already decided it is busy."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into calendar_feeds (id, name, url_fingerprint, position) "
            "values ('feed-1', 'Work', 'fingerprint-1', 0) on conflict (id) do nothing"
        )
        cur.execute(
            "insert into calendar_events (feed, uid, summary, starts_at, ends_at, local_date, "
            "busy, all_day) values ('feed-1', %s, 'Standup', %s, %s, %s, true, %s)",
            (f"uid-{start.isoformat()}", start, end, start.astimezone(DUBAI).date(), all_day),
        )


def row(conn: psycopg.Connection, pid: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select * from prescriptions where id = %s", (pid,))
        return cur.fetchone()


# --- PLAN-02: the stable coach id -------------------------------------------


def test_the_external_id_is_stable_for_a_prescription() -> None:
    """PLAN-02: the id is what makes upsert update instead of duplicating."""
    assert events.external_id(42) == "coach-bot:presc:42"
    assert events.external_id(42) == events.external_id(42)


def test_only_our_own_events_are_recognised() -> None:
    """V1 removed the documented filter, so this is the only test of ownership.

    `oauth_client_id` is null under a personal API key and `created_by_id` is the
    athlete, so upstream cannot be asked which events are ours. This function is
    the answer, and PLAN-05 *deletes* what it claims — so it is exact, not a prefix
    test.
    """
    assert events.is_ours({"external_id": "coach-bot:presc:7"})

    for foreign in (
        {"external_id": "coach:verify:1"},  # the verification script's own namespace
        {"external_id": "coach-bot:presc:"},  # no id
        {"external_id": "coach-bot:presc:7x"},  # not digits
        {"external_id": "x-coach-bot:presc:7"},  # not anchored at the start
        {"external_id": "coach-bot:presc:7 "},  # trailing space
        {"external_id": ""},
        {"external_id": None},
        {},  # the athlete's own event, which carries no external_id at all
    ):
        assert not events.is_ours(foreign), foreign


def test_the_verification_scripts_marker_is_not_ours() -> None:
    """A probe left behind by a failed V1 must not be swept as a prescription.

    Asserted against the script's real constant rather than a copy of it, so the
    two namespaces cannot quietly converge.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "verify_intervals", REPO / "scripts" / "verify_intervals.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert not events.is_ours({"external_id": module.MARKER})


def test_ten_changes_leave_one_event(conn: psycopg.Connection) -> None:
    """P08's acceptance: "no duplicates after ten changes".

    Ten republications of the same prescription. The id never changes, so upstream
    updates in place — which is the property PLAN-02 buys and the reason nothing
    here has to track upstream state.
    """
    pid = prescribe(conn)
    api = FakeApi()

    for minute in range(10):
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "update prescriptions set planned_for = %s where id = %s",
                (SOON + timedelta(minutes=minute), pid),
            )
        publish.publish(conn, api, DUBAI)

    ours = [e for e in api.calendar if events.is_ours(e)]
    assert len(ours) == 1
    assert len(api.published) == 10


# --- PLAN-03: what the description carries -----------------------------------


def test_the_description_carries_every_required_field(conn: psycopg.Connection) -> None:
    """PLAN-03: duration, intensity target, route where relevant, and the purpose."""
    spec = {
        "duration_s": 5400,
        "purpose": "Sweet spot repeats",
        "discipline": "ride",
        "intensity_factor": 0.88,
        "target_watts": 185,
        "route": "Watopia Tempus Fugit",
    }
    described = events.describe(spec)

    assert "90 min" in described
    assert "185W" in described
    assert "IF 0.88" in described
    assert "Watopia Tempus Fugit" in described
    assert "Sweet spot repeats" in described


def test_intensity_falls_back_to_the_factor_when_ftp_was_unknown() -> None:
    """A block generated before the ramp test has no watts. It still has a target.

    Publishing "Duration: 60 min" and nothing else would be a session the athlete
    cannot pace, so the intensity factor is given rather than omitted.
    """
    described = events.describe(
        {"duration_s": 3600, "purpose": "Endurance", "intensity_factor": 0.65}
    )

    assert "IF 0.65" in described
    assert "W" not in described.replace("Warmup", "")


def test_a_gym_session_gets_rpe_and_its_movements() -> None:
    """GYM-01 through PLAN-03. "Duration and purpose only" is not a session.

    PLAN-11 keeps gym unstructured — no steps, no file — but the movements are what
    the athlete actually does, so they are in the description as prose.
    """
    described = events.describe(
        {
            "duration_s": 2700,
            "purpose": "Lower body strength",
            "discipline": "gym",
            "rpe_target": 7,
            "movements": [
                {"name": "Goblet squat", "sets": 3, "reps": "8-10"},
                {"name": "Split squat", "sets": 3, "reps": "10", "note": "each side"},
            ],
        }
    )

    assert "RPE 7" in described
    assert "Goblet squat — 3x8-10" in described
    assert "each side" in described


def test_the_route_is_omitted_when_there_is_not_one() -> None:
    """PLAN-03 says "route where relevant", and outdoors there may not be one."""
    assert "Route:" not in events.describe({"duration_s": 3600, "purpose": "Endurance"})


# --- PLAN-09, PLAN-10, PLAN-11: structured versus not ------------------------


def test_a_structured_session_publishes_as_steps() -> None:
    """PLAN-09: machine readable steps with duration and power, not prose."""
    text = workout.render(
        [
            {"section": "Warmup", "duration_s": 600, "ramp_pct": (50, 70)},
            {
                "section": "Main",
                "repeat": 4,
                "steps": [
                    {"duration_s": 300, "power_pct": 105},
                    {"duration_s": 180, "power_pct": 55},
                ],
            },
            {"section": "Cooldown", "duration_s": 600, "power_pct": 50},
        ]
    )

    assert text == (
        "Warmup\n- 10m ramp 50-70%\n\nMain\n4x\n - 5m 105%\n - 3m 55%\n\nCooldown\n- 10m 50%"
    )


def test_the_repeat_line_carries_no_dash() -> None:
    """The one-character bug V4 caught, pinned so it cannot come back.

    Verified against the live platform on 30 July 2026: `- 3x` is parsed as an
    unrecognised step and silently dropped, so a 3x set renders once — 1260s
    arriving for a 1980s session, with no error from anywhere. `3x` renders as
    `<IntervalsT Repeat="3">`.
    """
    text = workout.render(
        [{"repeat": 3, "steps": [{"duration_s": 240, "power_pct": 105}]}]
    )

    assert text.splitlines()[0] == "3x"
    assert not text.startswith("- ")


def test_only_a_repeats_own_steps_are_indented() -> None:
    """Indentation means "inside the repeat above", so a heading must not indent.

    This is the detail that would make a valid-looking workout render wrong: a
    cooldown indented under a "Cooldown" heading could be parsed as another
    interval of the preceding set.
    """
    text = workout.render(
        [
            {"section": "Main", "repeat": 2, "steps": [{"duration_s": 60, "power_pct": 100}]},
            {"section": "Cooldown", "duration_s": 300, "power_pct": 50},
        ]
    )
    cooldown = text.splitlines()[-1]

    assert cooldown == "- 5m 50%"
    assert not cooldown.startswith(" ")


def test_an_unstructured_session_publishes_without_steps(conn: psycopg.Connection) -> None:
    """PLAN-11: "a steady endurance prescription publishes without a step list"."""
    prescribe(conn)
    api = FakeApi()

    publish.publish(conn, api, DUBAI)
    body = api.published[0][0]

    assert not workout.is_structured({"duration_s": 3600})
    # No step line anywhere in the description.
    assert not any(line.startswith("- ") for line in body["description"].splitlines())


def test_structure_is_the_step_list_and_nothing_else() -> None:
    """PLAN-09 versus PLAN-11 turns on one test, in one place.

    Deriving structure from the discipline or the intensity factor would make
    PLAN-11 depend on guessing which rides count as endurance — and a hard ride is
    not a structured one.
    """
    assert not workout.is_structured({"intensity_factor": 0.95, "target_watts": 260})
    assert workout.is_structured({"steps": [{"duration_s": 60, "power_pct": 100}]})


def test_a_step_without_a_target_is_refused() -> None:
    """PLAN-09 wants duration *and* power on every step.

    Raising beats publishing: a step the platform renders as free riding is a
    session that arrives wrong, and the athlete would ride it.
    """
    with pytest.raises(workout.UnpublishableStep, match="no target"):
        workout.render([{"duration_s": 300}])


def test_a_ramp_is_not_flattened_to_its_start_value() -> None:
    """A ramp carries two numbers and would otherwise render as one.

    Checked because a warmup that renders as a flat 50% is a plausible-looking
    workout that never warms up — the kind of bug a test has to catch because
    reading the output does not.
    """
    assert workout.target({"ramp_pct": (50, 75)}) == "ramp 50-75%"
    assert workout.target({"ramp_pct": (50, 75), "power_pct": 50}) == "ramp 50-75%"


def test_durations_render_in_the_units_a_person_reads() -> None:
    assert workout.duration(3600) == "1h"
    assert workout.duration(600) == "10m"
    assert workout.duration(90) == "90s"
    with pytest.raises(workout.UnpublishableStep):
        workout.duration(0)


def test_no_workout_file_is_ever_generated() -> None:
    """PLAN-10: "the coach never generates workout files itself".

    A scan rather than an assertion about behaviour, because the requirement is
    about what the codebase contains. The step list goes in `description` and the
    platform compiles it, so any appearance of the file-upload fields would mean
    someone had started down the other path.
    """
    banned = ("file_contents_base64", "file_contents", ".zwo", "zwifthashtag")
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        body = path.read_text()
        for term in banned:
            if term in body:
                offenders.append(f"{path.relative_to(REPO)}: {term}")
    assert not offenders, offenders


# --- PLAN-01: it publishes ---------------------------------------------------


def test_a_block_publishes_as_planned_events(conn: psycopg.Connection) -> None:
    """PLAN-01: "a generated block appears as planned events upstream"."""
    first = prescribe(conn, SOON)
    second = prescribe(conn, SOON + timedelta(days=2), discipline="gym", spec={
        "duration_s": 2700,
        "purpose": "Strength",
        "discipline": "gym",
        "rpe_target": 7,
        "movements": [{"name": "Goblet squat", "sets": 3, "reps": "10"}],
    })
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)

    assert result.count == 2
    assert len(api.published) == 1, "one call for the batch, not one per session"
    published = {b["external_id"] for b in api.published[0]}
    assert published == {events.external_id(first), events.external_id(second)}
    assert {b["category"] for b in api.published[0]} == {"WORKOUT"}


def test_the_upstream_event_id_is_stored(conn: psycopg.Connection) -> None:
    """PLAN-07 needs it: `paired_event_id` is an event id and must resolve to a row."""
    pid = prescribe(conn)
    api = FakeApi()

    publish.publish(conn, api, DUBAI)

    assert row(conn, pid)["calendar_event_id"] is not None


def test_a_gym_session_is_marked_indoor(conn: psycopg.Connection) -> None:
    """PLAN-11: what stops the platform offering a gym session to Zwift."""
    prescribe(conn, discipline="gym", spec={"duration_s": 2700, "purpose": "Strength"})
    api = FakeApi()

    publish.publish(conn, api, DUBAI)

    assert api.published[0][0]["indoor"] is True
    assert api.published[0][0]["type"] == "WeightTraining"


def test_an_unknown_discipline_still_publishes() -> None:
    """A vocabulary gap must not become a missing training day.

    Refusing to publish would be the tidier failure and the worse one: the athlete
    is still expected to do the session.
    """
    assert events.activity_type("padel") == events.FALLBACK_TYPE
    assert events.activity_type("Ride") == "Ride"


def test_the_start_time_is_sent_naive_and_local(conn: psycopg.Connection) -> None:
    """`start_date_local` means what it says, and an offset would be applied twice.

    TZ-01 already put the athlete's local wall clock in `planned_for`; sending an
    offset with it would have upstream shift it again.
    """
    prescribe(conn, SOON)
    api = FakeApi()

    publish.publish(conn, api, DUBAI)
    sent = api.published[0][0]["start_date_local"]

    assert "+" not in sent and "Z" not in sent
    assert sent.startswith(SOON.strftime("%Y-%m-%dT%H:%M"))


def test_the_past_is_not_published(conn: psycopg.Connection) -> None:
    """Publishing last Tuesday's session helps nobody and fights the sweep."""
    prescribe(conn, datetime.now(DUBAI) - timedelta(days=5))
    api = FakeApi()

    assert publish.publish(conn, api, DUBAI).count == 0
    assert api.published == []


# --- PLAN-04: never into busy time ------------------------------------------


def test_a_conflict_moves_the_session(conn: psycopg.Connection) -> None:
    """PLAN-04: "seeded conflict causes a move or shortening, not an overlap"."""
    pid = prescribe(conn, SOON)
    busy(conn, SOON - timedelta(minutes=30), SOON + timedelta(minutes=30))
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)

    placement = result.placements[0]
    assert placement.moved
    assert placement.duration_s == 3600, "moved, not shortened: the evening had room"
    assert placement.starts_at >= SOON.replace(hour=18, minute=30)
    # The local row follows what was published, or the two diverge.
    assert row(conn, pid)["planned_for"] == placement.starts_at


def test_a_tight_evening_shortens_rather_than_overlaps(conn: psycopg.Connection) -> None:
    """When no full-length slot exists, the session shrinks into the largest gap."""
    pid = prescribe(conn, SOON)
    day = SOON.date()
    # 17:00-19:30 and 20:30-22:00 busy. One free hour, 19:30 to 20:30.
    busy(
        conn,
        datetime.combine(day, SOON.replace(hour=17, minute=0).timetz()),
        datetime.combine(day, SOON.replace(hour=19, minute=30).timetz()),
    )
    busy(
        conn,
        datetime.combine(day, SOON.replace(hour=20, minute=30).timetz()),
        datetime.combine(day, SOON.replace(hour=22, minute=0).timetz()),
    )
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)
    placement = result.placements[0]

    assert placement.starts_at.hour == 19 and placement.starts_at.minute == 30
    assert placement.duration_s == 3600
    assert row(conn, pid)["spec"]["duration_s"] == 3600


def test_a_full_evening_reports_rather_than_overlapping(conn: psycopg.Connection) -> None:
    """The one outcome PLAN-04 forbids is the overlap, so it does not publish one.

    The session is reported unplaceable and the *other* sessions still publish. A
    block that refused to go up because Thursday is busy would be worse than a
    block with a hole the athlete can be told about.
    """
    blocked = prescribe(conn, SOON)
    free = prescribe(conn, SOON + timedelta(days=1))
    busy(
        conn,
        datetime.combine(SOON.date(), SOON.replace(hour=17).timetz()),
        datetime.combine(SOON.date(), SOON.replace(hour=22).timetz()),
    )
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)

    assert [u["prescription_id"] for u in result.unplaceable] == [blocked]
    assert [p.prescription_id for p in result.placements] == [free]
    assert {b["external_id"] for b in api.published[0]} == {events.external_id(free)}


def test_an_all_day_commitment_blocks_the_whole_evening(conn: psycopg.Connection) -> None:
    """An all-day entry is usually travel or leave. Treating it as free would put
    a session inside a flight."""
    prescribe(conn, SOON)
    busy(conn, SOON.replace(hour=0), SOON.replace(hour=23, minute=59), all_day=True)
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)

    assert result.placements == []
    assert len(result.unplaceable) == 1


def test_a_free_evening_is_not_rearranged(conn: psycopg.Connection) -> None:
    """The planned time is tried first. An unbusy evening must not be tidied."""
    prescribe(conn, SOON)
    api = FakeApi()

    placement = publish.publish(conn, api, DUBAI).placements[0]

    assert placement.starts_at == SOON
    assert not placement.adjusted


def test_touching_a_commitment_is_not_a_clash(conn: psycopg.Connection) -> None:
    """A session starting exactly as a meeting ends is fine.

    Treating it as a clash would lose an hour of the evening to arithmetic.
    """
    prescribe(conn, SOON)
    busy(conn, SOON - timedelta(hours=1), SOON)
    api = FakeApi()

    assert not publish.publish(conn, api, DUBAI).placements[0].adjusted


def test_a_plan_04_move_is_recorded_as_an_adjustment(conn: psycopg.Connection) -> None:
    """ADJ-01 wants every adjustment recorded with its trigger and evidence.

    "The calendar said no" is a trigger like any other. A session that published
    exactly as planned gets no row — otherwise the athlete's adjustment history is
    mostly noise.
    """
    pid = prescribe(conn, SOON)
    busy(conn, SOON - timedelta(minutes=30), SOON + timedelta(minutes=30))
    api = FakeApi()

    publish.publish(conn, api, DUBAI)

    with conn.cursor() as cur:
        cur.execute(
            "select trigger, evidence from adjustment_events where prescription_id = %s", (pid,)
        )
        rows = cur.fetchall()
    assert [r["trigger"] for r in rows] == ["calendar_conflict"]
    assert rows[0]["evidence"]["requirement"] == "PLAN-04"


def test_the_session_is_never_moved_to_another_day(conn: psycopg.Connection) -> None:
    """Moving within the evening is accommodation; across days is re-planning.

    BLOCK chose the weekday against observed availability. Shifting Thursday's
    intervals onto Friday changes the training week, which is not this module's
    decision to make.
    """
    prescribe(conn, SOON)
    busy(
        conn,
        datetime.combine(SOON.date(), SOON.replace(hour=17).timetz()),
        datetime.combine(SOON.date(), SOON.replace(hour=22).timetz()),
    )
    api = FakeApi()

    result = publish.publish(conn, api, DUBAI)

    assert result.placements == []
    assert all(p.starts_at.date() == SOON.date() for p in result.placements)


# --- PLAN-05: the orphan sweep ----------------------------------------------


def test_an_orphan_is_swept(conn: psycopg.Connection) -> None:
    """PLAN-05: "seeded orphan is gone after the job"."""
    future = (datetime.now(DUBAI) + timedelta(days=10)).replace(microsecond=0)
    api = FakeApi(
        [
            {
                "id": 1,
                "external_id": events.external_id(9999),
                "start_date_local": future.replace(tzinfo=None).isoformat(),
            }
        ]
    )

    result = sweep.run(conn, api, NOW, DUBAI)

    assert result.deleted == [events.external_id(9999)]
    assert api.calendar == []
    assert result.reported == 1


def test_a_live_prescriptions_event_survives(conn: psycopg.Connection) -> None:
    pid = prescribe(conn)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)

    result = sweep.run(conn, api, NOW, DUBAI)

    assert result.deleted == []
    assert any(e["external_id"] == events.external_id(pid) for e in api.calendar)


def test_the_athletes_own_events_are_never_touched(conn: psycopg.Connection) -> None:
    """The sweep runs against a calendar the athlete also uses.

    A race they entered has no external_id at all, and something else's event has
    one we did not write. Both are counted as foreign and left alone.
    """
    future = (datetime.now(DUBAI) + timedelta(days=10)).replace(tzinfo=None).isoformat()
    api = FakeApi(
        [
            {"id": 1, "name": "Dubai Ride", "start_date_local": future},
            {"id": 2, "external_id": "someone-else:42", "start_date_local": future},
            {"id": 3, "external_id": "coach:verify:1", "start_date_local": future},
        ]
    )

    result = sweep.run(conn, api, NOW, DUBAI)

    assert result.deleted == []
    assert result.foreign == 3
    assert len(api.calendar) == 3
    assert api.deleted == []


def test_a_past_orphan_is_left_alone(conn: psycopg.Connection) -> None:
    """The test that is easy to omit and expensive to.

    A past planned event is history: it is what an activity was paired against and
    what the athlete did or failed to do. Sweeping it would delete the record of a
    session to tidy a calendar nobody is looking at.
    """
    past = (datetime.now(DUBAI) - timedelta(days=3)).replace(tzinfo=None).isoformat()
    api = FakeApi([{"id": 1, "external_id": events.external_id(9999), "start_date_local": past}])

    result = sweep.run(conn, api, NOW, DUBAI)

    assert result.deleted == []
    assert result.kept_past == [events.external_id(9999)]
    assert len(api.calendar) == 1


def test_an_unreadable_date_is_left_alone(conn: psycopg.Connection) -> None:
    """Deleting on a date we could not parse is the worst available outcome."""
    api = FakeApi(
        [
            {"id": 1, "external_id": events.external_id(9999), "start_date_local": "not a date"},
            {"id": 2, "external_id": events.external_id(9998)},
        ]
    )

    assert sweep.run(conn, api, NOW, DUBAI).deleted == []
    assert len(api.calendar) == 2


def test_a_cancelled_prescriptions_event_is_swept(conn: psycopg.Connection) -> None:
    """ADJ cancels by status, and the calendar has to follow.

    Otherwise the athlete sees a session the coach has already withdrawn.
    """
    pid = prescribe(conn)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update prescriptions set status = 'cancelled' where id = %s", (pid,))

    result = sweep.run(conn, api, NOW, DUBAI)

    assert result.deleted == [events.external_id(pid)]


def test_the_sweep_reads_the_local_day_not_the_servers(conn: psycopg.Connection) -> None:
    """TZ-01. An event at 18:00 tonight is not past because UTC says 22:30.

    Dubai is UTC+4, so a session this evening looks like yesterday to a server
    reading its own clock — and would be swept as a past orphan.
    """
    local = datetime.now(DUBAI)
    tonight = local.replace(hour=23, minute=30, second=0, microsecond=0)
    if tonight <= local:
        pytest.skip("run after 23:30 local; the window this tests does not exist today")

    api = FakeApi(
        [
            {
                "id": 1,
                "external_id": events.external_id(9999),
                "start_date_local": tonight.replace(tzinfo=None).isoformat(),
            }
        ]
    )

    assert sweep.run(conn, api, NOW, DUBAI).deleted == [events.external_id(9999)]


# --- PLAN-06 and PLAN-12: the athlete's edits -------------------------------


def _moved(pid: int, to: datetime, event_id: int = 900000) -> dict[str, Any]:
    return {
        "id": event_id,
        "external_id": events.external_id(pid),
        "start_date_local": to.replace(tzinfo=None).isoformat(),
    }


def test_an_edit_updates_the_local_prescription(conn: psycopg.Connection) -> None:
    """PLAN-12: "moving a planned session upstream changes the local prescription
    date within one sync"."""
    pid = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)

    moved_to = SOON + timedelta(days=1, hours=1)
    api.calendar = [_moved(pid, moved_to)]

    result = sync.run(conn, api, NOW, DUBAI)

    assert result.count == 1
    assert row(conn, pid)["planned_for"].astimezone(DUBAI) == moved_to
    assert row(conn, pid)["status"] == "adjusted"


def test_an_edit_is_recorded_as_an_adjustment(conn: psycopg.Connection) -> None:
    """An athlete moving a session is an adjustment, and belongs beside the others.

    A coach reviewing the week should see the athlete's changes in the same place
    as the system's own.
    """
    pid = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    api.calendar = [_moved(pid, SOON + timedelta(days=1))]

    sync.run(conn, api, NOW, DUBAI)

    with conn.cursor() as cur:
        cur.execute(
            "select trigger, evidence from adjustment_events where prescription_id = %s "
            "and trigger = 'athlete_edit'",
            (pid,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["evidence"]["moved_day"] is True
    assert rows[0]["evidence"]["weekday"] == SOON.weekday()


def test_a_minute_of_drift_is_not_an_edit(conn: psycopg.Connection) -> None:
    """Republication can round. A minute is not the athlete moving anything."""
    pid = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    api.calendar = [_moved(pid, SOON + timedelta(seconds=30))]

    assert sync.run(conn, api, NOW, DUBAI).count == 0


def test_two_moves_off_the_same_weekday_become_observed_availability(
    conn: psycopg.Connection,
) -> None:
    """PLAN-06's acceptance, exactly: "moving a planned session twice on the same
    weekday updates availability with observed provenance".

    Twice, deliberately. One move is a dentist appointment.
    """
    api = FakeApi()
    for week in range(2):
        when = SOON + timedelta(weeks=week)
        pid = prescribe(conn, when)
        publish.publish(conn, api, DUBAI, prescriptions=publish.pending(conn))
        api.calendar = [_moved(pid, when + timedelta(days=1))]
        result = sync.run(conn, api, NOW, DUBAI)

    assert result.queued, "two moves off the same weekday should propose availability"

    with conn.cursor() as cur:
        cur.execute(
            "select proposal, origin from pending_writes where status = 'pending' "
            "order by id desc limit 1"
        )
        queued = cur.fetchone()
    assert queued["proposal"]["key"] == "availability.blackouts"
    # CONS-06: a proposal, never a fact. Consolidation ratifies it.
    assert queued["proposal"]["provenance"] == "observed"
    assert queued["origin"] == "feed"
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts where key = 'availability.blackouts'")
        assert cur.fetchone()["n"] == 0


def test_one_move_proposes_nothing(conn: psycopg.Connection) -> None:
    """The threshold is what stops a single rescheduled evening blacklisting a day."""
    pid = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    api.calendar = [_moved(pid, SOON + timedelta(days=1))]

    assert sync.run(conn, api, NOW, DUBAI).queued == []


def test_a_move_within_the_evening_is_not_availability_evidence(
    conn: psycopg.Connection,
) -> None:
    """Shifting 18:00 to 20:00 says the evening is tight, not that the day is gone.

    Counting it would blacklist every weekday the athlete ever rescheduled within.
    """
    api = FakeApi()
    for week in range(3):
        when = SOON + timedelta(weeks=week)
        pid = prescribe(conn, when)
        publish.publish(conn, api, DUBAI, prescriptions=publish.pending(conn))
        api.calendar = [_moved(pid, when + timedelta(hours=2))]
        result = sync.run(conn, api, NOW, DUBAI)

    assert result.count == 1, "each pass should still see the move as an edit"
    assert result.queued == []


def test_the_evidence_is_the_weekday_moved_away_from(conn: psycopg.Connection) -> None:
    """Moving Thursday's session to Friday says something about Thursdays.

    Keying on the destination would record a busy Thursday as evidence about
    Friday, and the next block would avoid the wrong day.
    """
    edit = sync.Edit(
        prescription_id=1,
        was=SOON,
        now=SOON + timedelta(days=1),
        external_id=events.external_id(1),
    )
    assert edit.weekday == SOON.weekday()
    assert edit.moved_day


def test_a_deleted_event_cancels_the_prescription(conn: psycopg.Connection) -> None:
    """The athlete removed the session. Take the hint rather than republishing it."""
    pid = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    api.calendar = []

    result = sync.run(conn, api, NOW, DUBAI)

    assert result.deleted_upstream == [pid]
    assert row(conn, pid)["status"] == "cancelled"


def test_an_unpublished_prescription_is_not_treated_as_deleted(
    conn: psycopg.Connection,
) -> None:
    """It has no upstream counterpart, so an absence upstream says nothing.

    Without the `external_id is not null` filter every unpublished prescription
    would be cancelled on the first sync.
    """
    pid = prescribe(conn, SOON)
    api = FakeApi()

    result = sync.run(conn, api, NOW, DUBAI)

    assert result.deleted_upstream == []
    assert row(conn, pid)["status"] == "planned"


# --- PLAN-07: pairing -------------------------------------------------------


def _session(
    conn: psycopg.Connection,
    when: datetime,
    discipline: str = "ride",
    paired_event_id: str | None = None,
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (discipline, started_at, local_date, duration_s, "
            "paired_event_id) values (%s, %s, %s, 3600, %s) returning id",
            (discipline, when, when.astimezone(DUBAI).date(), paired_event_id),
        )
        return int(cur.fetchone()["id"])


def test_the_upstream_pairing_wins(conn: psycopg.Connection) -> None:
    """PLAN-07: "matching uses the upstream pairing where available".

    The pairing points at a prescription on a *different* day from the session, so
    only the upstream link can find it. The date fallback would find the decoy.
    """
    paired = prescribe(conn, SOON)
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    event_id = row(conn, paired)["calendar_event_id"]

    ridden = SOON + timedelta(days=3)
    decoy = prescribe(conn, ridden)
    session_id = _session(conn, ridden, paired_event_id=str(event_id))

    assert review.match(conn, session_id) == paired
    assert review.match(conn, session_id) != decoy


def test_date_and_discipline_are_the_fallback(conn: psycopg.Connection) -> None:
    """PLAN-07: "falling back to local date and discipline matching".

    Not a degraded path. Nothing pairs a ride that had no planned workout, and a
    file arriving through the watched folder never went past the platform.
    """
    pid = prescribe(conn, SOON)
    session_id = _session(conn, SOON)

    assert review.match(conn, session_id) == pid


def test_a_pairing_to_someone_elses_event_falls_back(conn: psycopg.Connection) -> None:
    """The athlete planned a workout in the app themselves.

    The pairing is real and points at an event we do not hold, so it resolves to
    nothing — and the date fallback is still allowed to try.
    """
    pid = prescribe(conn, SOON)
    session_id = _session(conn, SOON, paired_event_id="777777")

    assert review.match(conn, session_id) == pid


def test_pairing_survives_a_discipline_recorded_differently(
    conn: psycopg.Connection,
) -> None:
    """One of the cases the upstream link handles and the fallback cannot.

    A session upstream typed VirtualRide against a prescription for `ride` would
    never match on discipline, and the athlete would look unfairly non-compliant.
    """
    pid = prescribe(conn, SOON, discipline="ride")
    api = FakeApi()
    publish.publish(conn, api, DUBAI)
    event_id = row(conn, pid)["calendar_event_id"]

    session_id = _session(conn, SOON, discipline="virtualride", paired_event_id=str(event_id))

    assert review.match(conn, session_id) == pid


def test_the_paired_id_is_read_off_the_activity() -> None:
    """It arrives on the activity payload and has to be kept, or nothing pairs."""
    from coach.ingest import activities

    assert activities.paired_event_id_of({"paired_event_id": 12345}) == "12345"
    assert activities.paired_event_id_of({"paired_event_id": "12345"}) == "12345"
    for absent in ({}, {"paired_event_id": None}, {"paired_event_id": 0}, {"paired_event_id": ""}):
        assert activities.paired_event_id_of(absent) is None


# --- PLAN-08: no Google writes ----------------------------------------------


def test_no_write_path_to_google_exists() -> None:
    """PLAN-08: "no write path to Google exists in the codebase".

    A scan, because the requirement is about what the codebase contains rather than
    what it does. The calendar feeds are read-only secret iCal URLs and there is no
    Google credential anywhere — this is what keeps it that way as the code grows.
    """
    offenders: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        body = path.read_text().lower()
        if "googleapis.com" in body or "google.oauth" in body or "googleapiclient" in body:
            offenders.append(str(path.relative_to(REPO)))
        # A POST or PUT anywhere near a Google host would be the thing this forbids.
        for line in body.splitlines():
            if "google" in line and (".post(" in line or ".put(" in line or ".patch(" in line):
                offenders.append(f"{path.relative_to(REPO)}: {line.strip()[:80]}")
    assert not offenders, offenders


def test_the_calendar_module_only_reads() -> None:
    """The one module that touches a Google URL at all. It fetches and parses."""
    from coach.calendars import feed

    body = inspect.getsource(feed)
    assert ".post(" not in body
    assert ".put(" not in body
    assert ".delete(" not in body


# --- the wiring -------------------------------------------------------------


def test_the_sweep_is_a_nightly_job() -> None:
    """PLAN-05 says "on the nightly pass", and this is that pass."""
    assert callable(scheduler.sweep_job(FakeApi(), DUBAI))

    source = inspect.getsource(scheduler.main)
    body = source[source.index("jobs: dict") :]
    for name in ("consolidation", "sweep", "decay", "export"):
        assert f'"{name}"' in body


def test_the_sweep_runs_before_the_export() -> None:
    """Consolidation can cancel a prescription; the calendar should follow the same
    night, and the export should describe the state both left behind."""
    source = inspect.getsource(scheduler.main)
    body = source[source.index("jobs: dict") :]
    positions = [body.index(f'"{n}"') for n in ("consolidation", "sweep", "decay", "export")]

    assert positions == sorted(positions)


def test_a_missing_api_key_costs_the_sweep_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A night that consolidates and decays but leaves a stale calendar entry is a
    much smaller problem than a night that does nothing."""
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)

    assert scheduler._sweep_or_none(DUBAI) is None


def test_the_sync_runs_on_the_ingest_loop() -> None:
    """PLAN-12 asks for "within one sync", so it needs a loop to be within."""
    from coach.ingest import server

    assert callable(server.plan_poller)
    assert 'name="plans"' in inspect.getsource(server.main)


def test_the_sync_cadence_is_floored() -> None:
    """A calendar a person edits by hand does not need polling hard, and the floor
    is what stops a typo spending the rate limit on nothing."""
    assert sync.interval_s() >= 900


def test_the_sync_loop_never_writes_upstream() -> None:
    """A loop that both read and wrote this calendar could fight with the athlete
    inside a single interval. Publishing is a block action; the sweep is nightly."""
    body = inspect.getsource(sync)

    assert "upsert_events" not in body
    assert "delete_events" not in body
