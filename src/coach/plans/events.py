"""A prescription as an upstream calendar event. Shape only; nothing here calls out.

PLAN-02 (the stable coach id), PLAN-03 (what the description carries), PLAN-09 and
PLAN-11 (structured versus not). Separated from :mod:`coach.plans.publish` so the
payload can be asserted on without a network, which is most of what P08's tests do.

**On recognising our own events.** V1 settled that `oauth_client_id` is null on
everything a personal API key creates, so upstream cannot be asked which events
are the coach's. :func:`is_ours` answers it locally, from the `external_id`, and
the pattern is exact rather than a prefix test. That matters because PLAN-05
*deletes* what this function claims: a generous match here is data loss there, and
the athlete's own events are on the same calendar.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from coach.blocks import load as loadmod
from coach.plans import workout as workoutmod

# PLAN-02's namespace. `coach-bot` rather than `coach` so it cannot collide with
# `scripts/verify_intervals.py`, which publishes under `coach:verify:` — a probe
# left behind by a failed check is not a prescription and must not be swept as
# though it were one.
PREFIX = "coach-bot:presc:"

# What :func:`is_ours` will claim. Anchored and digits-only: the sweep deletes
# whatever matches, so the pattern is the safety boundary rather than a hint.
OURS = re.compile(rf"^{re.escape(PREFIX)}(\d+)$")

# Discipline to the upstream activity type. Upstream's vocabulary, not ours, and
# an unknown discipline is not guessed at — see :func:`activity_type`.
TYPES = {
    "ride": "Ride",
    "virtualride": "VirtualRide",
    "run": "Run",
    "virtualrun": "VirtualRun",
    "swim": "Swim",
    "walk": "Walk",
    "hike": "Hike",
    "gym": "WeightTraining",
    "strength": "WeightTraining",
    "weighttraining": "WeightTraining",
    "workout": "Workout",
    "golf": "Golf",
    "yoga": "Yoga",
}

# The type an unrecognised discipline publishes as. `Workout` is upstream's own
# catch-all and it renders on the calendar, which is the point: a session the coach
# planned must appear even if its discipline is new. Logged where it is used.
FALLBACK_TYPE = "Workout"


def external_id(prescription_id: int) -> str:
    """PLAN-02's stable coach id.

    Derived from the row id, so it is stable across every republication of the
    same prescription — which is what makes `upsert=true` update rather than
    duplicate, and what makes "changing a prescription twice leaves exactly one
    planned event" true without us tracking anything.
    """
    return f"{PREFIX}{prescription_id}"


def is_ours(event: dict[str, Any]) -> bool:
    """Did the coach publish this event?

    The `external_id` is the only evidence available (V1: `oauth_client_id` is
    null, `created_by_id` is the athlete). Deliberately strict — an event the
    athlete created that happens to start with our prefix is not ours, and the
    caller that acts on a false positive is the one that deletes.
    """
    return bool(OURS.match(str(event.get("external_id") or "")))


def prescription_id_of(event: dict[str, Any]) -> int | None:
    """The row this event was published from, or None if it is not ours."""
    match = OURS.match(str(event.get("external_id") or ""))
    return int(match.group(1)) if match else None


def activity_type(discipline: str) -> str:
    """The upstream `type` for a discipline.

    Falls back rather than raising. A discipline the map has not caught up with is
    a session the athlete is still expected to do, and refusing to publish it
    would turn a vocabulary gap into a missing training day.
    """
    return TYPES.get(discipline.lower(), FALLBACK_TYPE)


def _intensity_line(spec: dict[str, Any]) -> str | None:
    """PLAN-03's intensity target, in whatever terms the session has.

    Watts where FTP was known at generation, the intensity factor otherwise, and
    an RPE target for gym work — GYM-01 makes RPE the gym's intensity and there is
    no power number to give.
    """
    if spec.get("target_watts"):
        factor = spec.get("intensity_factor")
        suffix = f" (IF {factor:.2f})" if factor else ""
        return f"Target: {int(spec['target_watts'])}W{suffix}"
    if spec.get("intensity_factor"):
        return f"Target: IF {float(spec['intensity_factor']):.2f}"
    if spec.get("rpe_target"):
        return f"Target: RPE {float(spec['rpe_target']):g}"
    return None


def _movement_lines(spec: dict[str, Any]) -> list[str]:
    """GYM-01: sets, reps and the movement names, for a gym session.

    PLAN-11 keeps gym unstructured — no workout file, no power steps — but "duration
    and purpose only" would publish a session the athlete cannot actually do. The
    movements are the session. They are prose in the description, not a step list.
    """
    lines: list[str] = []
    for movement in spec.get("movements") or []:
        name = movement.get("name") or movement.get("exercise") or "movement"
        sets, reps = movement.get("sets"), movement.get("reps")
        scheme = f" — {sets}x{reps}" if sets and reps else ""
        note = f" ({movement['note']})" if movement.get("note") else ""
        lines.append(f"* {name}{scheme}{note}")
    return lines


def describe(spec: dict[str, Any]) -> str:
    """The event description. PLAN-03, and PLAN-09 or PLAN-11 depending on the spec.

    PLAN-03 wants duration, intensity target, route where relevant, and the
    purpose, on every published event. A structured session carries the workout
    text as well, in the same field — the platform parses the step lines and leaves
    the prose alone, which is why both can live here.
    """
    minutes = int(spec.get("duration_s", 0)) // 60
    parts: list[str] = []

    if spec.get("purpose"):
        parts.append(str(spec["purpose"]))

    facts = [f"Duration: {minutes} min"] if minutes else []
    intensity = _intensity_line(spec)
    if intensity:
        facts.append(intensity)
    # PLAN-03: "route where relevant". Genuinely optional — indoors the route is
    # the Zwift world, and outdoors there may not be one.
    if spec.get("route"):
        facts.append(f"Route: {spec['route']}")
    if facts:
        parts.append("\n".join(facts))

    movements = _movement_lines(spec)
    if movements:
        parts.append("\n".join(movements))

    if workoutmod.is_structured(spec):
        # PLAN-09. Last, and after a blank line, so the step lines are contiguous
        # and nothing above them can be mistaken for one.
        parts.append(workoutmod.render(list(spec["steps"])))

    return "\n\n".join(parts)


def name_for(spec: dict[str, Any], discipline: str) -> str:
    """The calendar label. Short, because this is what the athlete sees in a grid."""
    minutes = int(spec.get("duration_s", 0)) // 60
    purpose = str(spec.get("purpose") or discipline).strip()
    # A purpose is a sentence in some specs and a label in others. The first
    # clause is the label; the rest is already in the description.
    label = purpose.split(".")[0].split(",")[0].strip() or discipline
    return f"{label} {minutes}min" if minutes else label


def payload(prescription: dict[str, Any], start_local: datetime | None = None) -> dict[str, Any]:
    """One prescription as the event body upstream expects.

    `start_date_local` is sent naive and local, which is what the field name means
    and what the API wants: upstream stores the athlete's wall clock time and
    attaching an offset here would have it interpreted twice. TZ-01 already put
    the right local time in `planned_for`.
    """
    spec = dict(prescription.get("spec") or {})
    discipline = str(prescription["discipline"])
    when = start_local or prescription["planned_for"]

    body: dict[str, Any] = {
        "category": "WORKOUT",
        "start_date_local": when.replace(tzinfo=None).isoformat(timespec="seconds"),
        "type": activity_type(discipline),
        "name": name_for(spec, discipline),
        "description": describe(spec),
        "external_id": external_id(int(prescription["id"])),
    }

    # Duration in seconds, so the calendar shows the right block and the platform
    # can compute planned load. Named `moving_time` upstream even for gym work,
    # where nothing moves anywhere.
    if spec.get("duration_s"):
        body["moving_time"] = int(spec["duration_s"])

    # PLAN-11: gym is never exported as a workout file. Said explicitly rather
    # than left to the absence of steps, because `indoor` is what stops the
    # platform offering it to Zwift.
    if discipline.lower() in loadmod.GYM_DISCIPLINES:
        body["indoor"] = True

    return body
