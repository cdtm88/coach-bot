"""The zone tables, and the one place they are enforced.

Ported from `pacer-ai` with one correction, and the correction is the reason the
file exists: its heart rate boundaries were Friel's under a methodology string
claiming Coggan's. See `docs/prior-art.md` section 2.

These are constants, so most of what is worth testing is the boundary
behaviour — which is exactly what `docs/state-of-build.md` records as a working
agreement, after a plateau threshold that could never fire. Every band is
checked at both ends.
"""

from __future__ import annotations

import pytest

from coach.plans import workout
from coach.science import zones

# --- the correction ---------------------------------------------------------


def test_the_heart_rate_zone_two_ceiling_is_the_corrected_one() -> None:
    """0.83 of LTHR, not 0.90. The number this whole file exists to preserve.

    Friel's boundaries under a Coggan label put the endurance ceiling at 0.90,
    which for a deconditioned athlete with a flagged back turns every prescribed
    endurance ride into tempo.
    """
    assert zones.hr_zone("z2").upper == 0.83


def test_the_heart_rate_bands_are_coggan_throughout() -> None:
    """Not only Z2. A half-applied correction is its own trap."""
    bounds = [(z.name, z.lower, z.upper) for z in zones.HR_ZONES]

    assert bounds == [
        ("z1", 0.00, 0.68),
        ("z2", 0.68, 0.83),
        ("z3", 0.83, 0.94),
        ("z4", 0.94, 1.05),
        ("z5", 1.05, None),
    ]


def test_the_power_bands_are_the_recorded_ones() -> None:
    bounds = [(z.name, z.lower, z.upper) for z in zones.POWER_ZONES]

    assert bounds == [
        ("z1", 0.00, 0.55),
        ("z2", 0.55, 0.75),
        ("z3", 0.75, 0.90),
        ("z4", 0.90, 1.05),
        ("z5", 1.05, 1.20),
        ("z6", 1.20, 1.50),
        ("z7", 1.50, None),
    ]


# --- membership, at both ends of every band --------------------------------


def test_a_boundary_value_belongs_to_exactly_one_zone() -> None:
    """Inclusive below, exclusive above.

    Inclusive at both ends puts 0.75 of FTP in Z2 and Z3 at once, and a session
    that is two zones is one that gets labelled one thing and ridden as another.
    """
    for table in (zones.POWER_ZONES, zones.HR_ZONES):
        for band in table:
            matched = [z.name for z in table if z.contains(band.lower)]
            assert matched == [band.name], f"{band.lower} matched {matched}"


def test_every_band_is_left_by_its_own_ceiling() -> None:
    for table in (zones.POWER_ZONES, zones.HR_ZONES):
        for band in table:
            if band.upper is not None:
                assert not band.contains(band.upper)


def test_the_top_zone_has_no_ceiling() -> None:
    assert zones.POWER_ZONES[-1].contains(9.0)
    assert zones.HR_ZONES[-1].contains(9.0)


def test_lookup_by_fraction_walks_the_table() -> None:
    assert zones.power_zone_for(0.60).name == "z2"
    assert zones.power_zone_for(0.75).name == "z3"
    assert zones.power_zone_for(2.00).name == "z7"
    assert zones.hr_zone_for(0.70).name == "z2"
    assert zones.hr_zone_for(0.83).name == "z3"


def test_a_negative_fraction_is_not_a_zone() -> None:
    with pytest.raises(ValueError):
        zones.power_zone_for(-0.1)
    with pytest.raises(ValueError):
        zones.hr_zone_for(-0.1)


def test_an_unknown_zone_name_is_refused() -> None:
    with pytest.raises(zones.UnknownZone):
        zones.power_zone("z9")
    with pytest.raises(zones.UnknownZone):
        zones.hr_zone("z6")  # the heart rate table has five bands, not seven


# --- the LTHR heuristic -----------------------------------------------------


def test_lthr_from_max_hr_is_the_midpoint_of_the_heuristic() -> None:
    assert zones.lthr_from_max_hr(190) == pytest.approx(166.25)


def test_a_nonsense_max_hr_is_refused() -> None:
    with pytest.raises(ValueError):
        zones.lthr_from_max_hr(0)


# --- the caller: a label that contradicts its own number --------------------


def test_a_zone_that_disagrees_with_its_percentage_is_refused() -> None:
    """The bug this check exists for.

    `target` resolves `power_pct` before `zone` and returns, so this step would
    publish as 95 percent with the z2 label silently dropped — a threshold
    effort on the calendar under an endurance name.
    """
    with pytest.raises(workout.UnpublishableStep) as caught:
        workout.render([{"duration_s": 1200, "zone": "z2", "power_pct": 95}])

    assert "z2" in str(caught.value)
    assert "95%" in str(caught.value)


def test_a_zone_that_agrees_with_its_percentage_publishes() -> None:
    """Agreement is not ambiguity. Only a contradiction is refused."""
    assert workout.render([{"duration_s": 1200, "zone": "z2", "power_pct": 65}])


def test_a_ramp_is_checked_at_both_ends() -> None:
    """A ramp that starts in the zone and ends outside it is still wrong."""
    with pytest.raises(workout.UnpublishableStep):
        workout.render([{"duration_s": 600, "zone": "z2", "ramp_pct": (60, 95)}])

    assert workout.render([{"duration_s": 600, "zone": "z2", "ramp_pct": (56, 74)}])


def test_a_zone_on_its_own_still_publishes_as_a_zone() -> None:
    """The check must not break the form it is guarding."""
    assert workout.render([{"duration_s": 1200, "zone": "z2"}]) == "- 20m z2"


def test_an_unknown_zone_beside_a_number_is_reported_rather_than_dropped() -> None:
    """The hole this check found while being written.

    `target` resolves `power_pct` first and returns, so an unrecognised zone
    name alongside a percentage was discarded in silence along with the rest of
    the label — the same silent-drop shape, one level down. Validating the name
    before the precedence runs is what closes it.
    """
    with pytest.raises(workout.UnpublishableStep) as caught:
        workout.render([{"duration_s": 600, "zone": "z9", "power_pct": 65}])

    assert "z9" in str(caught.value)


def test_an_unknown_zone_on_its_own_is_reported_the_same_way() -> None:
    """Both directions reach the one message."""
    with pytest.raises(workout.UnpublishableStep) as caught:
        workout.render([{"duration_s": 600, "zone": "z9"}])

    assert "z9" in str(caught.value)


def test_the_workout_zone_names_come_from_the_table() -> None:
    """So a name the renderer accepts is a name the boundaries can check."""
    assert workout.ZONES == zones.POWER_ZONE_NAMES
