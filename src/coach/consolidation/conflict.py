"""The conflict resolution matrix.

CONS-03: conflict resolution is executed in application code, not decided by the
model. The model emits candidate diffs; what lands is decided here. A precedence
claim in the model's output has no effect — this module never reads one.

Design section 7:

| Situation                              | Resolution                    |
| -------------------------------------- | ----------------------------- |
| Same key, same provenance              | Most recent wins              |
| Stated vs observed, behavioural key    | Observed wins, mentioned once |
| Stated vs observed, intent key (goals) | Stated wins                   |
| Inferred vs measured                   | Measured wins, silently       |
| Any change to a safety key             | Rejected (SAFE-02)            |
| Ambiguous or low evidence              | Held in pending, ages out     |
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from coach.memory import facts as factmod
from coach.memory import keys as keymod

# Design section 7: goals are intent, so a stated goal outranks observed
# behaviour. Everything else is behavioural, where behaviour outranks statement.
INTENT_CATEGORIES = frozenset({"goal"})

# "Measured" for the inferred-vs-measured row. A ramp test result is computed
# from data; an inference is the model's guess.
MEASURED = frozenset({"computed", "observed"})

# Provenance a model may claim, at any door. `computed` is the one value of
# MEM-04's four that it must not: MEM-08 reserves computed figures for SQL, and
# the line above treats computed as measured — so a model labelling its own
# arithmetic `computed` would have an inference promoted over a real
# measurement.
#
# It lives here, next to the rule that makes it necessary, because it was
# previously stated only in `consolidation.propose` and the *other* door was
# left open: `agent.tools.propose_fact` offered all four values of MEM-04 to the
# model in its tool schema. Nothing carried that value through to a fact — an
# in-turn proposal is briefing material for the nightly proposer, which re-emits
# under the narrow enum — but a schema that advertises a value the system will
# never honour is a description of the system that is not true, and the next
# door to be opened would have had nothing to copy from.
MODEL_PROVENANCE = ("stated", "observed", "inferred")

# Below this, a diff is too weak to land and is held rather than applied.
LOW_EVIDENCE = Decimal("0.30")


class Outcome(Enum):
    APPLY = "apply"
    APPLY_WITH_MENTION = "apply_with_mention"
    REJECT = "reject"
    HOLD = "hold"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str

    @property
    def applies(self) -> bool:
        return self.outcome in (Outcome.APPLY, Outcome.APPLY_WITH_MENTION)

    @property
    def mentions(self) -> bool:
        return self.outcome is Outcome.APPLY_WITH_MENTION


def resolve(
    fact_key: keymod.FactKey,
    current: factmod.Fact | None,
    proposed_provenance: str,
    proposed_confidence: Decimal = Decimal("1.00"),
) -> Decision:
    """Decide what happens to one candidate diff.

    The model's own view of precedence is not a parameter here, deliberately.
    """
    # SAFE-02: consolidation may not create, change or supersede a safety key.
    # Only the SAFE-06 athlete path can, and it does not come through here.
    if fact_key.safety:
        return Decision(
            Outcome.REJECT,
            f"safety key: consolidation may not write {fact_key.key!r} (SAFE-02)",
        )

    if proposed_confidence < LOW_EVIDENCE:
        return Decision(
            Outcome.HOLD,
            f"evidence too weak at {proposed_confidence}; held for conversation or ageing out",
        )

    if current is None:
        return Decision(Outcome.APPLY, "no active value for this key")

    was, now = current.provenance, proposed_provenance

    # Inferred vs measured: measured wins, silently. Checked before the
    # stated/observed rows so a ramp test superseding an inferred threshold
    # never generates a mention (CONS-05).
    if was == "inferred" and now in MEASURED:
        return Decision(Outcome.APPLY, f"measured {now} supersedes inferred, silently")
    if was in MEASURED and now == "inferred":
        return Decision(Outcome.REJECT, f"inferred value does not displace measured {was}")

    if was == now:
        return Decision(Outcome.APPLY, "same provenance: most recent wins")

    is_intent = fact_key.category in INTENT_CATEGORIES

    if was == "stated" and now == "observed":
        if is_intent:
            # CONS-04: stated wins for intent keys. What the athlete is aiming
            # at is not something behaviour gets to overrule.
            return Decision(Outcome.REJECT, "intent key: stated goal outranks observed behaviour")
        # CONS-04: observed supersedes stated for behavioural keys, and design
        # section 8 says it is mentioned once, in passing.
        return Decision(Outcome.APPLY_WITH_MENTION, "behavioural key: observed supersedes stated")

    if was == "observed" and now == "stated":
        if is_intent:
            return Decision(Outcome.APPLY, "intent key: the athlete restated the goal")
        # Behaviour already contradicted a statement here; another statement
        # does not undo that on its own.
        return Decision(
            Outcome.HOLD,
            "behavioural key already resolved to observed; a restatement is held for confirmation",
        )

    return Decision(Outcome.APPLY, f"{now} supersedes {was}")
