"""Turning a block plan into prescriptions, safely.

BLOCK-03: four weeks, generating prescriptions for every planned session.
BLOCK-04: duration, intensity target, discipline, route where relevant, purpose —
all non-null on publish. BLOCK-06: a ramp test in week one, whose result
supersedes inferred physiology. BLOCK-07: the weekly ramp limit, rejected on
breach. BLOCK-08: restructure the whole remaining block, not only the coming
week. GYM-01: movement patterns, sets, reps and an RPE target on every movement.
SAFE-04 and GYM-02: nothing violating a constraint is ever written.

**The model writes the shape, this writes the rows.** The plan handed in says
"Tuesday, endurance, 60 minutes, 0.62 intensity" and "Thursday, gym, lower body,
RPE 7". It never names an exercise, because GYM-02 is not a rule a model can be
trusted to remember on a Thursday in week three — the movement is chosen here,
from the library, against the constraints, or it is not prescribed at all.

**Nothing is written until the whole block validates.** A generation that
breaches BLOCK-07 in week four must not leave weeks one to three in the
database, so the prescriptions are built in memory, checked, and inserted in one
transaction. That is also what makes BLOCK-08's restructure safe: it deletes and
regenerates the remaining weeks, and a failure leaves the old plan intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coach.blocks import constraints as constraintmod
from coach.blocks import library as librarymod
from coach.blocks import load as loadmod
from coach.memory import state as statemod

log = logging.getLogger(__name__)

# BLOCK-06: the ramp test is prescribed in week one, because everything else in
# the block is scaled off a threshold and generating four weeks against a
# guessed one is four weeks of the wrong intensity.
RAMP_TEST_WEEK = 1

# The physiology keys a ramp test result lands on. Measured beats inferred
# silently for these (CONS-05, and the conflict matrix's inferred-vs-measured
# row), so the supersession needs no announcement and gets none.
RAMP_TEST_KEYS = ("physiology.ftp_watts", "physiology.threshold_hr", "physiology.max_hr")


class GenerationRejected(RuntimeError):
    """The block cannot be generated as planned. Carries every reason at once."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class PlannedSession:
    """One session as the plan describes it, before a movement is chosen.

    Deliberately not a prescription: it carries intent and no exercise. The
    exercise is the library's decision, made against the constraints, and
    letting a plan name one would route around GYM-02.
    """

    week: int
    weekday: int
    discipline: str
    purpose: str
    duration_s: int
    intensity_factor: float | None = None
    rpe_target: float | None = None
    patterns: list[str] = field(default_factory=list)
    sets: int | None = None
    reps: str | None = None
    route: str | None = None
    is_ramp_test: bool = False


@dataclass
class Generated:
    """What a generation produced, before or after it was written."""

    prescriptions: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    weeks: list[loadmod.Week] = field(default_factory=list)

    @property
    def total_load(self) -> Decimal:
        return sum((Decimal(str(p["planned_load"])) for p in self.prescriptions), Decimal(0))


def build(
    conn: psycopg.Connection,
    block_id: int,
    starts_on: date,
    sessions: list[PlannedSession],
    tz: ZoneInfo,
    ftp_watts: float | None = None,
) -> Generated:
    """Resolve a plan into prescriptions in memory. Writes nothing.

    Separated from :func:`publish` so the whole block can be validated before a
    single row lands, and so a caller can show the athlete what would be written.
    """
    resolved = constraintmod.load(conn, librarymod.known_movements(conn))
    # SAFE-04 fails closed: a constraint nobody could read is not a constraint
    # that does not apply. Raises rather than proceeding.
    resolved.require_readable()

    result = Generated()
    for planned in sessions:
        prescription, blocked = _one(conn, block_id, starts_on, planned, tz, resolved, ftp_watts)
        result.blocked.extend(blocked)
        if prescription is not None:
            result.prescriptions.append(prescription)
    return result


def _one(
    conn: psycopg.Connection,
    block_id: int,
    starts_on: date,
    planned: PlannedSession,
    tz: ZoneInfo,
    resolved: constraintmod.Constraints,
    ftp_watts: float | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """One planned session as a prescription row, or nothing plus the reasons."""
    day = starts_on + timedelta(weeks=planned.week - 1, days=planned.weekday)
    planned_for = datetime.combine(day, time(18, 0), tzinfo=tz)

    spec: dict[str, Any] = {
        # BLOCK-04: every field non-null on publish.
        "duration_s": planned.duration_s,
        "purpose": planned.purpose,
        "discipline": planned.discipline,
    }
    blocked: list[dict[str, Any]] = []

    if planned.discipline in loadmod.GYM_DISCIPLINES:
        movements, blocked = _movements(conn, block_id, day, planned, resolved)
        if not movements:
            # Every pattern in the session was excluded and nothing substituted.
            # A gym session with no movements is not a session.
            return None, blocked
        spec["movements"] = movements
        # GYM-01: sets, reps and an RPE target on every movement.
        spec["rpe_target"] = planned.rpe_target
        # GYM-07: duration and purpose only. No structured export, ever.
        spec["export"] = "none"
    else:
        spec["intensity_factor"] = planned.intensity_factor
        if ftp_watts and planned.intensity_factor:
            spec["target_watts"] = round(ftp_watts * planned.intensity_factor)
            spec["ftp_watts"] = ftp_watts
        # BLOCK-04: route where relevant. Indoors on a trainer, the route is the
        # Zwift world, and it is genuinely optional rather than a missing field.
        if planned.route:
            spec["route"] = planned.route
        if planned.is_ramp_test:
            spec["ramp_test"] = True

    return {
        "block_id": block_id,
        "planned_for": planned_for,
        "discipline": planned.discipline,
        "spec": spec,
        "planned_load": loadmod.of_spec(planned.discipline, spec),
    }, blocked


def _movements(
    conn: psycopg.Connection,
    block_id: int,
    day: date,
    planned: PlannedSession,
    resolved: constraintmod.Constraints,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Choose a movement per pattern, substituting or refusing (GYM-01 to GYM-03)."""
    movements: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for pattern in planned.patterns:
        exercise, exclusion = librarymod.resolve(conn, pattern, resolved)

        if exclusion is not None:
            blocked.append(
                {
                    "block_id": block_id,
                    "discipline": planned.discipline,
                    "planned_for": day,
                    "movement": pattern,
                    "pattern": pattern,
                    "exclusion": exclusion,
                    "substituted_with": exercise.name if exercise else None,
                }
            )

        if exercise is None:
            continue

        # The last line of defence. `resolve` already respects the constraints;
        # this re-checks the actual chosen movement, because GYM-02 says the
        # excluded pattern is *never* prescribed and a substitution bug should
        # drop a movement rather than publish one.
        if resolved.blocks(exercise.movement_pattern, exercise.name) is not None:
            log.error("substitution returned an excluded movement: %s", exercise.name)
            continue

        movements.append(
            {
                # GYM-01: pattern, sets, reps and an RPE target.
                "movement_pattern": exercise.movement_pattern,
                "exercise": exercise.name,
                "equipment": exercise.equipment,
                "sets": planned.sets,
                "reps": planned.reps,
                "rpe_target": planned.rpe_target,
            }
        )

    return movements, blocked


def validate(conn: psycopg.Connection, generated: Generated, pct: Decimal | None = None) -> None:
    """Everything that must hold before a row is written.

    Raises :class:`GenerationRejected` carrying every reason, rather than the
    first. A generator being told one problem at a time will fix one problem at
    a time.
    """
    reasons: list[str] = []

    # BLOCK-04: all fields non-null on publish.
    for prescription in generated.prescriptions:
        spec = prescription["spec"]
        for required in ("duration_s", "purpose", "discipline"):
            if not spec.get(required):
                reasons.append(
                    f"{prescription['planned_for'].date()} {prescription['discipline']}: "
                    f"{required} is missing (BLOCK-04)"
                )
        if prescription["discipline"] in loadmod.GYM_DISCIPLINES:
            for movement in spec.get("movements", []):
                if not all(movement.get(f) for f in ("sets", "reps", "rpe_target")):
                    reasons.append(
                        f"{movement['exercise']}: sets, reps and an RPE target are all "
                        "required (GYM-01)"
                    )

    # BLOCK-07 and GYM-05: the combined weekly ramp.
    reasons.extend(loadmod.check_ramp(_weeks_of(generated), pct))

    if reasons:
        raise GenerationRejected(reasons)


def _weeks_of(generated: Generated) -> list[loadmod.Week]:
    """Week totals for a generation that has not been written yet.

    Computed in Python here rather than in SQL, unavoidably: MEM-08 keeps
    rollups out of the model's hands, and these rows do not exist to be summed
    in SQL yet. The stored equivalent is :func:`coach.blocks.load.planned_weeks`,
    and a test asserts the two agree.
    """
    buckets: dict[date, list[Decimal]] = {}
    disciplines: dict[date, list[str]] = {}
    for prescription in generated.prescriptions:
        starts_on = loadmod.week_of(prescription["planned_for"].date())
        buckets.setdefault(starts_on, []).append(Decimal(str(prescription["planned_load"])))
        disciplines.setdefault(starts_on, []).append(prescription["discipline"].lower())

    weeks = []
    for starts_on in sorted(buckets):
        loads = buckets[starts_on]
        kinds = disciplines[starts_on]
        paired = zip(loads, kinds, strict=True)
        gym = sum((load for load, kind in paired if kind in loadmod.GYM_DISCIPLINES), Decimal(0))
        weeks.append(
            loadmod.Week(
                starts_on=starts_on,
                total=sum(loads, Decimal(0)),
                cycling=sum(loads, Decimal(0)) - gym,
                gym=gym,
                sessions=len(loads),
            )
        )
    return weeks


def publish(conn: psycopg.Connection, generated: Generated) -> list[int]:
    """Write the prescriptions and the refusals, in one transaction.

    Call :func:`validate` first. This does not re-validate, because a caller
    that skipped validation should fail loudly in review rather than have this
    function quietly do it twice.
    """
    ids = []
    with conn.transaction(), conn.cursor() as cur:
        for prescription in generated.prescriptions:
            cur.execute(
                """
                insert into prescriptions
                    (block_id, planned_for, discipline, spec, planned_load)
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                (
                    prescription["block_id"],
                    prescription["planned_for"],
                    prescription["discipline"],
                    Jsonb(prescription["spec"]),
                    prescription["planned_load"],
                ),
            )
            ids.append(cur.fetchone()["id"])

    for blocked in generated.blocked:
        constraintmod.record_block(
            conn,
            block_id=blocked["block_id"],
            discipline=blocked["discipline"],
            movement=blocked["movement"],
            pattern=blocked["pattern"],
            exclusion=blocked["exclusion"],
            planned_for=blocked["planned_for"],
            substituted_with=blocked["substituted_with"],
        )
    return ids


def restructure(
    conn: psycopg.Connection,
    block_id: int,
    from_date: date,
    sessions: list[PlannedSession],
    starts_on: date,
    tz: ZoneInfo,
    ftp_watts: float | None = None,
    pct: Decimal | None = None,
) -> list[int]:
    """BLOCK-08: regenerate every remaining week, not only the coming one.

    Completed sessions are never touched — a prescription with a session
    attached is history — and neither is anything before `from_date`. The old
    plan survives a failed regeneration because the delete and the insert are
    one transaction and validation happens before either.
    """
    generated = build(conn, block_id, starts_on, sessions, tz, ftp_watts)
    validate(conn, generated, pct)

    ids = []
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "delete from prescriptions where block_id = %s and planned_for >= %s "
            "and session_id is null and status in ('planned', 'adjusted')",
            (block_id, from_date),
        )
        for prescription in generated.prescriptions:
            cur.execute(
                """
                insert into prescriptions
                    (block_id, planned_for, discipline, spec, planned_load)
                values (%s, %s, %s, %s, %s)
                returning id
                """,
                (
                    prescription["block_id"],
                    prescription["planned_for"],
                    prescription["discipline"],
                    Jsonb(prescription["spec"]),
                    prescription["planned_load"],
                ),
            )
            ids.append(cur.fetchone()["id"])
    return ids


# --- BLOCK-06: the ramp test ------------------------------------------------


def has_ramp_test(sessions: list[PlannedSession]) -> bool:
    """BLOCK-06: is there a ramp test in week one?"""
    return any(s.is_ramp_test and s.week == RAMP_TEST_WEEK for s in sessions)


def require_ramp_test(sessions: list[PlannedSession]) -> None:
    if not has_ramp_test(sessions):
        raise GenerationRejected(
            [
                "BLOCK-06: week one must contain a ramp test. Everything else in the "
                "block is scaled off a threshold, and four weeks against a guessed one "
                "is four weeks at the wrong intensity."
            ]
        )


def record_ramp_test(
    conn: psycopg.Connection,
    ftp_watts: int,
    threshold_hr: int | None = None,
    max_hr: int | None = None,
    source_ref: str | None = None,
) -> list[int]:
    """Queue a ramp test result for the night to ratify.

    Measured, so it supersedes an inferred value silently — CONS-05 and the
    conflict matrix's inferred-vs-measured row both say so, and neither needs
    this function to know it. What matters here is that a test result goes
    through `pending_writes` like every other observation: CONS-06 allows one
    direct writer outside consolidation and it is the SAFE-06 safety path, not
    this.

    Provenance is `computed` rather than `observed`. The number is derived from
    the test protocol — 75 percent of maximum aerobic power — rather than read
    off a sensor, and the seed records exactly that derivation for the 27 July
    test.
    """
    values = {
        "physiology.ftp_watts": ftp_watts,
        "physiology.threshold_hr": threshold_hr,
        "physiology.max_hr": max_hr,
    }
    queued = []
    for key, value in values.items():
        if value is None:
            continue
        queued.append(
            statemod.queue_write(
                conn,
                {
                    "key": key,
                    "value": value,
                    "provenance": "computed",
                    "reason": "ramp test result (BLOCK-06)",
                    "evidence": {"source_ref": source_ref} if source_ref else {},
                },
                origin="feed",
            )
        )
    return queued
