"""TRUST-01 to TRUST-07: the coach may not invent a physiological number.

`prompts/persona.md` asks for this and `docs/prd.md` states it architecturally —
"the coach is given permissions, not numbers" — and until now nothing between
the model's output and Telegram inspected what it had actually said. `pacer-ai`
established that a prompt is not sufficient and wrote down why
(`.planning/research/PITFALLS.md:105`):

> Adding "never emit physiological numbers" to the system prompt reduces but
> does not eliminate hallucinated numbers. The system prompt is a soft
> constraint. Models trained on sports content have strong prior knowledge of
> FTP values and will emit plausible numbers even when instructed otherwise.

**What this is not.** `pacer-ai` reached the same place through a `ToolResult`
type carrying value, unit, methodology and inputs on every computed value. That
is a wide refactor of `blocks/load.py`, `health/trend.py` and
`health/recovery.py`, and it is not needed here: the question is what the model
was *given* this turn, and that is already knowable from the assembled prompt
and the tool results. Provenance on computed values stays available and unbuilt.

**Three channels, deliberately not merged.** `pacer-ai` shipped with one and an
athlete saying "my LTHR is 165 bpm" made the bot fail three times and answer
with nothing, because the scanner had no channel for a number he had supplied
himself. They are separate lists here so the argument for each stays legible,
and so a number from the athlete can never be laundered into a tool's inputs.

**Zone numbers are not checked, on purpose.** "Ride Z2" is a label rather than a
measurement, the digit is not a claim about his physiology, and requiring `2` to
appear in a tool result would fail every honest prescription. What is checked is
the watts or the heart rate quoted *beside* the zone, which is the part that can
be invented.

**Percentages are not checked either**, for now. Adherence, compliance and
intensity factors are all quoted as percentages from rollups that do reach the
prompt, and the false positive rate in early testing was not worth the coverage.
Recorded here rather than left implicit, because a scanner whose gaps are
undocumented reads as complete.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Small whole numbers are ordinary prose — "give it 2 more weeks", "the first
# three rides" — and requiring them to be attributed would fail honest
# sentences. The same reasoning and the same bar as `review.voice`, which had it
# first. Whole numbers only: a decimal is never incidental in a coaching
# message, it is a rate, a weight or an intensity.
FREE_INTEGER_MAX = 12

# Units that make a number a claim about the athlete's body or his training
# load. A number without one of these is prose and is not checked.
#
# `%` is absent and so is `zone`; see the module docstring.
_UNIT_AFTER = r"(?:w|watts?|bpm|kg|kilos?|kilograms?|tss)"
_UNIT_BEFORE = r"(?:ctl|atl|tsb|ftp|lthr|np)"

# number-then-unit: "250 watts", "250W", "85 TSS", "72.4 kg"
_NUMBER_THEN_UNIT = re.compile(
    rf"(?<![\w.])(\d+(?:\.\d+)?)\s*{_UNIT_AFTER}\b",
    re.IGNORECASE,
)
# unit-then-number: "CTL 42", "FTP of 250", "LTHR 165"
_UNIT_THEN_NUMBER = re.compile(
    rf"\b{_UNIT_BEFORE}\s*(?:of|is|at|was)?\s*(\d+(?:\.\d+)?)(?![\w.])",
    re.IGNORECASE,
)

# Any number at all, for the review's stricter policy.
_ANY_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w.])")


@dataclass
class Attribution:
    """What the model was legitimately given this turn.

    Three lists rather than one set. Merging them would lose the distinction
    that makes the second channel safe to have at all: a number the athlete
    supplied is his to repeat back and must never be treated as something a tool
    returned.
    """

    grounded: list[float] = field(default_factory=list)
    self_reported: list[float] = field(default_factory=list)

    def add_text(self, text: str) -> None:
        """Numbers from prompt text. Regex, because prose has no structure."""
        self.grounded.extend(_numbers_in_text(text))

    def add_tool_result(self, payload: str) -> None:
        """Numbers from a tool's return value, as JSON.

        Parsed and walked to genuine number leaves rather than regexed, which is
        the whole of `pacer-ai`'s third and final version of this. A digit run
        inside a string leaf — a timestamp, an external id, a note the athlete
        wrote — is then structurally invisible instead of being patched out case
        by case. A tool result that is not JSON contributes nothing rather than
        being scraped, because scraping it is the bug.
        """
        try:
            self.grounded.extend(_number_leaves(json.loads(payload)))
        except (TypeError, ValueError):
            log.debug("tool result was not JSON; contributing no attribution")

    def add_self_reported(self, text: str) -> None:
        """Numbers the athlete supplied, from his own messages only."""
        self.self_reported.extend(_numbers_in_text(text))

    def allows(self, claim: float) -> bool:
        """Is this figure attributable to something the model was given?

        Rounding is allowed and invention is not. A claim matches a value when
        that value rounds to it at the claim's own precision, so "129 kg"
        against a fitted 129.1 is the model rounding, while "131 kg" appears
        nowhere and is not.
        """
        decimals = _decimals(claim)
        for value in (*self.grounded, *self.self_reported):
            if round(value, decimals) == round(claim, decimals):
                return True
        return False


def _decimals(value: float) -> int:
    text = repr(float(value))
    if "e" in text or "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))


def _numbers_in_text(text: str) -> list[float]:
    found: list[float] = []
    for raw in _ANY_NUMBER.findall(text or ""):
        try:
            found.append(float(raw.replace(",", ".")))
        except ValueError:  # pragma: no cover - the pattern only matches numbers
            continue
    return found


def _number_leaves(node: Any) -> list[float]:
    """Every genuine number in a parsed JSON document.

    `bool` is excluded explicitly because it subclasses `int` in Python, so a
    `"data_unavailable": true` would otherwise attribute the figure 1.
    """
    if isinstance(node, bool):
        return []
    if isinstance(node, int | float):
        return [float(node)]
    if isinstance(node, dict):
        return [n for value in node.values() for n in _number_leaves(value)]
    if isinstance(node, list):
        return [n for item in node for n in _number_leaves(item)]
    return []


@dataclass(frozen=True)
class Claim:
    """One physiological figure the reply asserts."""

    value: float
    text: str


def claims_in(reply: str) -> list[Claim]:
    """Physiological figures the reply states, in both directions.

    Both patterns run over the whole reply rather than one taking precedence,
    because "his FTP is 250W" carries the figure in both forms and either alone
    would be a way past the check.
    """
    found: dict[float, Claim] = {}
    for pattern in (_NUMBER_THEN_UNIT, _UNIT_THEN_NUMBER):
        for match in pattern.finditer(reply or ""):
            value = float(match.group(1))
            found.setdefault(value, Claim(value=value, text=match.group(0).strip()))
    return list(found.values())


def unattributed(reply: str, attribution: Attribution) -> list[Claim]:
    """Every physiological figure in the reply that nothing accounts for.

    Empty means the reply is clean. This is the whole check; the decision about
    what to do with a non-empty result belongs to the caller, because it differs
    between shadow mode and enforcement.
    """
    loose: list[Claim] = []
    for claim in claims_in(reply):
        if claim.value.is_integer() and claim.value <= FREE_INTEGER_MAX:
            continue
        if attribution.allows(claim.value):
            continue
        loose.append(claim)
    return loose


def ungrounded_numbers(text: str, facts: str) -> list[float]:
    """The review's stricter policy, over the same attribution primitive.

    `review.voice` checks *every* substantial number rather than only the
    physiological ones, and it is right to: the review is assembled entirely
    from SQL, so there is no such thing as a figure in it that the facts did not
    supply. Sharing `Attribution.allows` rather than the policy is the point —
    two grounding implementations that can drift apart is how a rewording
    quietly loosens a gate.
    """
    attribution = Attribution()
    attribution.add_text(facts)

    loose: list[float] = []
    for value in _numbers_in_text(text):
        if value.is_integer() and value <= FREE_INTEGER_MAX:
            continue
        if attribution.allows(value):
            continue
        loose.append(value)
    return loose
