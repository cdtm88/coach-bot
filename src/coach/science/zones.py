"""Training zone boundaries, checked against a source once so nobody checks again.

Ported from `pacer-ai` while archiving it. `docs/prior-art.md` section 2 records
where these came from and why they are worth having in code rather than in a
comment somewhere: the expensive part of that repository was never the
arithmetic, it was establishing which numbers were right.

**The heart rate table is a correction, not a transcription, and it is the
reason this file exists at all.** `pacer-ai` shipped Friel-style boundaries
under a methodology string that claimed Coggan and Allen. Corrected, the Zone 2
ceiling drops from 0.90 to 0.83 of LTHR. Its own note on the fix calls that
"materially gentler for a deconditioned, back-flagged beginner", which is a
description of this athlete. A Z2 ride prescribed against the wrong ceiling is
a tempo ride with an endurance label on it, every time, for weeks.

There is a second warning attached (`08-RESEARCH.md:347`): aggregator and
calculator sites transcribe these inconsistently, quoting 91 or 95 percent for
the Z4 lower bound. Trust Coggan and TrainingPeaks over a calculator site, and
do not "fix" these against one.

**Membership is inclusive below and exclusive above**, except at the top where
there is nothing above. That is deliberate: inclusive on both ends puts exactly
75 percent of FTP in two zones at once, and a boundary value landing in two
zones is how a session gets labelled one thing and ridden as another.

**What this module does not do.** It holds no opinion about what the athlete
should be doing, computes nothing from ride data, and is not in the model's
context. The one consumer today is `coach.plans.workout`, which uses it to
refuse a step whose zone label contradicts its own numeric target. Everything
else `pacer-ai` had — normalized power, TSS, the Banister constants, critical
power — stays in `docs/prior-art.md` until something needs it, because building
the rest now would be the regret-tier pattern that document warns about.
"""

from __future__ import annotations

from dataclasses import dataclass

# The ratio between a reported maximum heart rate and LTHR: the midpoint of the
# commonly cited 85 to 90 percent heuristic. Explicitly low confidence, and
# anything that uses it has to say so — a heuristic quoted without its hedge is
# indistinguishable from a measurement.
LTHR_FROM_MAX_HR = 0.875


@dataclass(frozen=True)
class Zone:
    """One band. `upper` is None at the top, where there is no ceiling."""

    name: str
    lower: float
    upper: float | None
    label: str

    def contains(self, fraction: float) -> bool:
        if fraction < self.lower:
            return False
        return self.upper is None or fraction < self.upper


# Coggan and Allen, as a fraction of FTP.
POWER_ZONES: tuple[Zone, ...] = (
    Zone("z1", 0.00, 0.55, "active recovery"),
    Zone("z2", 0.55, 0.75, "endurance"),
    Zone("z3", 0.75, 0.90, "tempo"),
    Zone("z4", 0.90, 1.05, "threshold"),
    Zone("z5", 1.05, 1.20, "VO2max"),
    Zone("z6", 1.20, 1.50, "anaerobic capacity"),
    Zone("z7", 1.50, None, "neuromuscular"),
)

# As a fraction of LTHR. See the module docstring: the Z2 ceiling of 0.83 is the
# corrected value and the number most worth not losing.
HR_ZONES: tuple[Zone, ...] = (
    Zone("z1", 0.00, 0.68, "active recovery"),
    Zone("z2", 0.68, 0.83, "endurance"),
    Zone("z3", 0.83, 0.94, "tempo"),
    Zone("z4", 0.94, 1.05, "threshold"),
    Zone("z5", 1.05, None, "VO2max and above"),
)

POWER_ZONE_NAMES = frozenset(z.name for z in POWER_ZONES)
HR_ZONE_NAMES = frozenset(z.name for z in HR_ZONES)


class UnknownZone(ValueError):
    """A zone name outside the table it was looked up in."""


def power_zone(name: str) -> Zone:
    for zone in POWER_ZONES:
        if zone.name == name.lower():
            return zone
    raise UnknownZone(f"{name!r} is not one of {sorted(POWER_ZONE_NAMES)}")


def hr_zone(name: str) -> Zone:
    for zone in HR_ZONES:
        if zone.name == name.lower():
            return zone
    raise UnknownZone(f"{name!r} is not one of {sorted(HR_ZONE_NAMES)}")


def power_zone_for(fraction: float) -> Zone:
    """The zone a fraction of FTP falls in. Negative power is not a zone."""
    if fraction < 0:
        raise ValueError(f"{fraction} is not a fraction of FTP")
    for zone in POWER_ZONES:
        if zone.contains(fraction):
            return zone
    return POWER_ZONES[-1]  # pragma: no cover - the top zone is unbounded


def hr_zone_for(fraction: float) -> Zone:
    """The zone a fraction of LTHR falls in."""
    if fraction < 0:
        raise ValueError(f"{fraction} is not a fraction of LTHR")
    for zone in HR_ZONES:
        if zone.contains(fraction):
            return zone
    return HR_ZONES[-1]  # pragma: no cover - the top zone is unbounded


def lthr_from_max_hr(max_hr: int | float) -> float:
    """A low confidence LTHR estimate from a reported maximum.

    Deliberately returns a bare number and not a measurement: a caller that
    stores this must record it as inferred, and anything that says it out loud
    has to say it is an estimate. `conflict.MEASURED` exists so that a real ramp
    test supersedes this silently the moment one lands.
    """
    if max_hr <= 0:
        raise ValueError(f"{max_hr} is not a heart rate")
    return max_hr * LTHR_FROM_MAX_HR
