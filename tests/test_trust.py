"""TRUST-01 to TRUST-07, and the corpus that makes the scanner safe to change.

The aggregate tests are the important ones. A scanner with false negatives lets
an invented FTP reach the athlete; a scanner with false positives gets switched
off within a week, which is the same outcome by a slower route.
"""

from __future__ import annotations

import pytest

from coach.agent import trust
from fixtures import trust_corpus


def attribution_for(case: trust_corpus.Case) -> trust.Attribution:
    return trust.Attribution(grounded=list(case.grounded), self_reported=list(case.self_reported))


# --- the corpus, in aggregate ----------------------------------------------


def test_no_false_negatives_across_the_corpus() -> None:
    """Every violation is caught. A miss here reaches the athlete."""
    missed = [
        case
        for case in trust_corpus.VIOLATIONS
        if not trust.unattributed(case.reply, attribution_for(case))
    ]

    assert missed == [], "\n".join(f"missed: {c.reply!r} ({c.why})" for c in missed)


def test_no_false_positives_on_the_near_misses() -> None:
    """A scanner that fires on honest prose is one that gets turned off."""
    fired = [
        (case, trust.unattributed(case.reply, attribution_for(case)))
        for case in trust_corpus.NEAR_MISSES
        if trust.unattributed(case.reply, attribution_for(case))
    ]

    assert fired == [], "\n".join(f"fired on: {c.reply!r} -> {f} ({c.why})" for c, f in fired)


def test_no_false_positives_on_attributed_figures() -> None:
    """A real number, correctly sourced, must pass."""
    fired = [
        (case, trust.unattributed(case.reply, attribution_for(case)))
        for case in trust_corpus.ATTRIBUTED
        if trust.unattributed(case.reply, attribution_for(case))
    ]

    assert fired == [], "\n".join(f"fired on: {c.reply!r} -> {f} ({c.why})" for c, f in fired)


def test_the_corpus_is_not_empty() -> None:
    """A corpus that has been emptied is a suite that always passes.

    `pacer-ai` wrote a meta-test for the same reason, with the docstring "a test
    that always passes is not verification".
    """
    assert len(trust_corpus.VIOLATIONS) >= 10
    assert len(trust_corpus.NEAR_MISSES) >= 12
    assert len(trust_corpus.ATTRIBUTED) >= 6


# --- the three channels -----------------------------------------------------


def test_a_tool_result_grounds_a_figure() -> None:
    attribution = trust.Attribution()
    attribution.add_tool_result('{"prescribed": [{"target_watts": 180}]}')

    assert trust.unattributed("Hold 180 watts.", attribution) == []


def test_a_number_inside_a_string_leaf_is_not_attribution() -> None:
    """`pacer-ai`'s third rewrite, and the reason it parses rather than regexes.

    A timestamp is a digit run that means nothing about the athlete's body. A
    substring scanner attributed `250` to the presence of `2026-08-03T12:50`;
    walking to genuine number leaves makes it structurally invisible instead of
    something to patch out case by case.
    """
    attribution = trust.Attribution()
    attribution.add_tool_result('{"started_at": "2026-08-03T12:50:00", "id": "act-250-x"}')

    assert trust.unattributed("Your FTP is 250 watts.", attribution)


def test_a_boolean_does_not_attribute_the_figure_one() -> None:
    """`bool` subclasses `int`, so this has to be excluded by name."""
    attribution = trust.Attribution()
    attribution.add_tool_result('{"data_unavailable": true}')

    assert attribution.grounded == []


def test_the_athlete_s_own_figure_is_a_separate_channel() -> None:
    """The bug that made `pacer-ai` fail three times and answer with nothing."""
    attribution = trust.Attribution()
    attribution.add_self_reported("my LTHR is 165 bpm")

    assert trust.unattributed("Working from an LTHR of 165 for now.", attribution) == []
    assert attribution.grounded == []
    assert attribution.self_reported == [165.0]


def test_the_channels_are_not_merged() -> None:
    """Kept apart so the argument for each stays legible, and so a
    self-reported figure can never be mistaken for something a tool returned."""
    attribution = trust.Attribution()
    attribution.add_tool_result('{"ftp_watts": 230}')
    attribution.add_self_reported("I weighed 128 kg")

    assert attribution.grounded == [230.0]
    assert attribution.self_reported == [128.0]


def test_a_tool_result_that_is_not_json_contributes_nothing() -> None:
    """Scraping it is the bug, so failing to parse it must not fall back to that."""
    attribution = trust.Attribution()
    attribution.add_tool_result("something went wrong: FTP 250")

    assert attribution.grounded == []


# --- rounding is allowed, invention is not ----------------------------------


def test_rounding_a_grounded_value_is_allowed() -> None:
    attribution = trust.Attribution(grounded=[129.1])

    assert trust.unattributed("You're around 129 kg.", attribution) == []


def test_a_nearby_number_is_not_a_rounding() -> None:
    attribution = trust.Attribution(grounded=[129.1])

    assert trust.unattributed("You're around 131 kg.", attribution)


def test_a_decimal_must_match_at_its_own_precision() -> None:
    """A decimal is never incidental in a coaching message."""
    attribution = trust.Attribution(grounded=[129.1])

    assert trust.unattributed("You're at 129.4 kg.", attribution)
    assert trust.unattributed("You're at 129.1 kg.", attribution) == []


# --- what is deliberately not checked ---------------------------------------


def test_a_zone_number_is_a_label_and_does_not_need_attribution() -> None:
    """Documented in the module, asserted here so it cannot change by accident."""
    assert trust.unattributed("Ride Thursday in Z2.", trust.Attribution()) == []
    assert trust.unattributed("Zone 4 is off the table.", trust.Attribution()) == []


def test_a_small_whole_number_is_prose_even_when_it_carries_a_unit() -> None:
    """The free integer bar, tested where it is actually the thing doing the work.

    This asserted on "give it 2 more weeks", which passes because "weeks" is not
    a physiological unit and so there is no claim at all — the unit scoping was
    doing the work and the bar was untested. Found by blinding the scanner and
    watching which tests failed: this one did not.
    """
    assert trust.unattributed("Add 5 kg to the bar next week.", trust.Attribution()) == []
    # And the bar is a bar, not a blanket: one above it is a claim again.
    assert trust.unattributed("Add 50 kg to the bar next week.", trust.Attribution())


def test_a_number_without_a_physiological_unit_is_never_a_claim() -> None:
    """The other half, which the near misses lean on far more than the bar does."""
    assert trust.unattributed("Give it 2 more weeks.", trust.Attribution()) == []
    assert trust.unattributed("Ride for 240 minutes.", trust.Attribution()) == []


def test_the_free_integer_bar_does_not_cover_a_plausible_figure() -> None:
    """The bar has to sit below anything that could be a real claim."""
    assert trust.FREE_INTEGER_MAX < 50


# --- both directions --------------------------------------------------------


def test_a_figure_stated_in_both_directions_is_found_once() -> None:
    claims = trust.claims_in("His FTP is 250 watts.")

    assert [c.value for c in claims] == [250.0]


def test_unit_before_and_after_are_both_matched() -> None:
    assert [c.value for c in trust.claims_in("CTL 42")] == [42.0]
    assert [c.value for c in trust.claims_in("85 TSS")] == [85.0]


# --- the review's stricter policy, over the same primitive ------------------


def test_the_review_policy_checks_every_number_not_only_physiological_ones() -> None:
    """`review.voice` is assembled entirely in SQL, so any figure not in the
    facts was invented, whatever unit it wears."""
    facts = "Effort: 3 sessions, 4h 10m, 96 km."

    assert trust.ungrounded_numbers("You rode 96 km across 3 sessions.", facts) == []
    assert trust.ungrounded_numbers("You rode 140 km.", facts) == [140.0]


def test_the_review_policy_allows_a_rounding() -> None:
    assert trust.ungrounded_numbers("about 129 kg", "129.1 kg") == []


def test_the_review_policy_refuses_a_decimal_the_facts_do_not_hold() -> None:
    """The case that was live for one commit in `review.voice`: a rate is
    exactly what HLTH-08 gates and sits under any sane free-integer bar."""
    assert trust.ungrounded_numbers("0.35 kg per week", "the trend is not yet callable")


@pytest.mark.parametrize("value,decimals", [(129.0, 0), (129.1, 1), (0.35, 2), (250.0, 0)])
def test_precision_is_read_off_the_claim(value: float, decimals: int) -> None:
    assert trust._decimals(value) == decimals


# --- TRUST-05: the named way out --------------------------------------------


def test_the_capability_gap_tool_records_and_returns_a_safe_sentence(
    conn,
) -> None:
    """A bare prohibition with no alternative is what pushes a model into
    inventing something. This is the alternative."""
    from coach.agent import tools as toolmod

    result = toolmod.dispatch(
        conn,
        "log_capability_gap",
        {"asked_for": "his current FTP", "reason": "no ramp test has been done"},
    )

    assert result["recorded"] is True
    assert "guess" in result["say"]
    with conn.cursor() as cur:
        cur.execute("select asked_for, reason from capability_gaps")
        row = cur.fetchone()
    assert row["asked_for"] == "his current FTP"


def test_the_internal_reason_never_reaches_the_reply(conn) -> None:
    """The discipline `pacer-ai` names: the method goes to the database only.

    A message explaining which methodology was unavailable is a message about
    the system rather than about his training.
    """
    from coach.agent import tools as toolmod

    result = toolmod.dispatch(
        conn,
        "log_capability_gap",
        {"asked_for": "his FTP", "reason": "critical power model not implemented"},
    )

    assert "critical power" not in result["say"]


def test_the_safe_sentence_carries_no_figure(conn) -> None:
    """It would be a poor escape hatch that needed checking by the scanner."""
    from coach.agent import tools as toolmod

    result = toolmod.dispatch(
        conn, "log_capability_gap", {"asked_for": "TSS", "reason": "not computed"}
    )

    assert trust.unattributed(result["say"], trust.Attribution()) == []


# --- TRUST-06: arguments the server owns ------------------------------------


def test_a_server_derived_argument_is_discarded(conn) -> None:
    """`pacer-ai` had the model guess a `user_id` it could not know, and **no
    onboarding profile was ever persisted in production**."""
    from coach.agent import tools as toolmod

    result = toolmod.dispatch(
        conn,
        "log_capability_gap",
        {"asked_for": "his FTP", "reason": "untested", "user_id": "user_001"},
    )

    assert result["recorded"] is True


def test_the_strip_is_unconditional_rather_than_schema_driven() -> None:
    """Anthropic does not enforce `additionalProperties: false`, so the model
    can emit a key that was never declared and there is no schema to check it
    against. The strip has to run over whatever arrived."""
    from coach.agent import tools as toolmod

    cleaned = toolmod._strip_server_derived(
        "get_plan", {"since": "2026-08-03", "athlete_id": 7, "chat_id": 42, "turn_id": "x"}
    )

    assert cleaned == {"since": "2026-08-03"}


def test_a_laundered_figure_cannot_be_echoed_back_as_attribution(conn) -> None:
    """The subtle half of TRUST-06, and why the strip exists at all.

    A model that invents a number, passes it into a tool as an *input*, and has
    the tool echo it back would then find it in the tool result and treat it as
    attributed. Stripping the keys the server owns is what breaks that loop for
    the arguments it applies to; this asserts the mechanism rather than the
    whole class.
    """
    from coach.agent import tools as toolmod

    attribution = trust.Attribution()
    cleaned = toolmod._strip_server_derived("get_plan", {"turn_id": 250})
    attribution.add_tool_result(__import__("json").dumps(cleaned))

    assert trust.unattributed("Your FTP is 250 watts.", attribution)
