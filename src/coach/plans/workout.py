"""PLAN-09: a structured session as native intervals.icu workout text.

The API accepts a workout three ways: as an encoded file in one of four formats, as
raw markup in a body field, or as **native workout text in the event
`description`**. The third is the one used here, and PLAN-10 is why: "the coach
never generates workout files itself". Text needs no file generation at all, and
the platform compiles it into whatever Zwift eventually downloads.

So the coach writes a step list and the platform writes the file. That is not a
convenience — it is the requirement, and the two file-shaped alternatives are named
only obliquely above because `tests/test_plans.py` scans for them by name, the same
way SEC-04's scan guards the credential mechanism this repo does not use.

**The format**, as the platform documents it. Steps are lines beginning with a
dash, each carrying a duration and a target:

    Warmup
    - 10m 55%

    Main
    - 4x
     - 5m 105%
     - 3m 55%

    Cooldown
    - 10m 50%

Durations are `30s`, `10m`, `1h`. Targets are a percentage of FTP, absolute watts
(`210W`), or a ramp between two values (`ramp 50-75%`). A bare `Nx` line opens a
repeat and the indented lines under it are the set.

**Nothing here invents training content.** Steps come from `spec["steps"]` and a
prescription without them is not structured — it publishes in PLAN-11's form,
duration and purpose only. See :func:`coach.plans.events.describe`. No generator
emits steps yet: P07 produces steady sessions and a ramp test flag, and choosing a
ramp protocol is a training decision that belongs to BLOCK rather than to the
module that formats it for transport.
"""

from __future__ import annotations

from typing import Any

from coach.science import zones as zonemod

# Zone names the platform understands, so a step may say `z2` instead of a number.
# The same seven Coggan bands `coach.science.zones` holds the boundaries for, so
# a name accepted here is a name that can be checked against a percentage below.
ZONES = frozenset(zonemod.POWER_ZONE_NAMES)


class UnpublishableStep(ValueError):
    """A step that would not compile upstream. Raised rather than guessed at.

    PLAN-09's acceptance is that the platform renders the result as a valid zwo
    *with the intended intervals*. A step we cannot express is a session that
    would arrive wrong, and arriving wrong is worse than not arriving: the athlete
    would ride it.
    """


def duration(seconds: int) -> str:
    """Seconds as the platform's duration literal.

    Whole minutes render as minutes because `10m` is what a person reading the
    calendar expects to see; anything else keeps its seconds rather than being
    rounded into a tidier lie.
    """
    if seconds <= 0:
        raise UnpublishableStep(f"a step cannot last {seconds} seconds")
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _validated_zone(named: Any) -> str:
    """A zone name the platform will understand, or the reason it will not.

    One author for this message. It is reached from two directions — a step whose
    only target is a zone, and a step that carries a zone *and* a number — and
    the second was silently dropping an unrecognised name along with the rest of
    the label, because `target` resolves the number first and returns.
    """
    zone = str(named).lower()
    if zone not in ZONES:
        raise UnpublishableStep(f"{named!r} is not one of {sorted(ZONES)}")
    return zone


def _reject_contradiction(step: dict[str, Any]) -> None:
    """A step whose zone label disagrees with its own numbers is not publishable.

    `target` below resolves the most specific form and returns, so a step
    carrying both `zone: z2` and `power_pct: 95` publishes as 95 percent and the
    zone is **silently dropped**. That is the same shape as the `- 3x` repeat
    bug: two readings of one step, no error anywhere, and the athlete rides the
    one nobody intended. There it cost two thirds of a session's duration; here
    it would put a threshold effort on the calendar under an endurance label.

    Checked against the Coggan boundaries in `coach.science.zones` rather than
    against a number written here, so the corrected Z2 ceiling has one home.

    Watts are not checked, and the reason is that converting them needs an FTP
    this function is not given. Stated rather than silently skipped: a step
    carrying both `zone` and `watts` is unverified here, and the zone is still
    dropped by `target`'s precedence.
    """
    named = step.get("zone")
    if named is None:
        return

    zone = _validated_zone(named)

    fractions: list[float] = []
    if "ramp_pct" in step:
        low, high = step["ramp_pct"]
        fractions = [float(low) / 100, float(high) / 100]
    elif "power_pct" in step:
        fractions = [float(step["power_pct"]) / 100]

    band = zonemod.power_zone(zone)
    outside = [f for f in fractions if not band.contains(f)]
    if outside:
        quoted = ", ".join(f"{f * 100:g}%" for f in outside)
        raise UnpublishableStep(
            f"step names {zone} ({band.label}) and {quoted} of FTP, which is not in "
            f"that zone. {zone} is {band.lower * 100:g}% to "
            f"{'above' if band.upper is None else f'{band.upper * 100:g}%'}. "
            "Publishing would keep the number and drop the label."
        )


def target(step: dict[str, Any]) -> str:
    """The intensity half of a step line.

    Four forms, checked in order of how specific they are. `ramp` first because a
    ramp also carries a `power_pct` pair and would otherwise render as its start
    value with the ramp silently dropped — a warmup that never warms up.
    """
    _reject_contradiction(step)
    if "ramp_pct" in step:
        low, high = step["ramp_pct"]
        return f"ramp {int(low)}-{int(high)}%"
    if "power_pct" in step:
        return f"{int(step['power_pct'])}%"
    if "watts" in step:
        return f"{int(step['watts'])}W"
    if "zone" in step:
        return _validated_zone(step["zone"])
    raise UnpublishableStep(
        f"step {step!r} carries no target. PLAN-09 wants duration *and* power on "
        "every step; a step with only a duration is free riding, which belongs in "
        "an unstructured session (PLAN-11) rather than in a step list."
    )


def _lines(steps: list[dict[str, Any]], indent: str = "") -> list[str]:
    """Render steps, recursing into repeats."""
    out: list[str] = []
    for step in steps:
        if "repeat" in step:
            times = int(step["repeat"])
            if times < 2:
                raise UnpublishableStep(f"a repeat of {times} is not a repeat")
            inner = step.get("steps") or []
            if not inner:
                raise UnpublishableStep("a repeat with no steps in it")
            # No leading dash on the repeat line, and this is not a style choice.
            # Verified against the live platform on 30 July 2026: `- 3x` is parsed
            # as an unrecognised *step* and silently dropped, so the set renders
            # once instead of three times — a 66 minute session arriving as 21
            # minutes with no error anywhere. `3x` renders as
            # `<IntervalsT Repeat="3">`, which is correct.
            #
            # Silently. That is why `scripts/verify_intervals.py v4` checks total
            # duration rather than eyeballing the file: both forms look right.
            out.append(f"{indent}{times}x")
            out.extend(_lines(inner, indent + " "))
            continue
        if "duration_s" not in step:
            raise UnpublishableStep(f"step {step!r} has no duration_s")
        out.append(f"{indent}- {duration(int(step['duration_s']))} {target(step)}")
    return out


def render(steps: list[dict[str, Any]]) -> str:
    """A step list as workout text. Raises rather than emit something unridable.

    Sections are optional and come from a step's `section` key: a step that names
    one opens it, and following steps stay under it until another names a different
    one. They are labels for the athlete reading the calendar.

    **Steps under a section are not indented**, and that is deliberate rather than
    cosmetic. Indentation in this format means "inside the repeat above" — it is
    how a set is expressed — so indenting an ordinary step beneath a heading risks
    it being parsed as part of a preceding repeat. Only :func:`_lines` indents, and
    only for a repeat's own steps.
    """
    if not steps:
        raise UnpublishableStep("no steps: an empty structured session is not one")

    out: list[str] = []
    section: str | None = None
    for step in steps:
        named = step.get("section")
        if named and named != section:
            if out:
                out.append("")
            out.append(str(named))
            section = named
        out.extend(_lines([step]))
    return "\n".join(out)


def is_structured(spec: dict[str, Any]) -> bool:
    """Does this prescription publish as steps (PLAN-09) or as prose (PLAN-11)?

    One question, one place. The presence of a step list is the whole test —
    deriving structure from the discipline or the intensity factor would make
    PLAN-11's "endurance rides publish with duration and purpose only" depend on
    guessing which rides count as endurance.
    """
    return bool(spec.get("steps"))
