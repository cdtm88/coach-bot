"""Naturalness checks.

CHAT-03 (never narrate a memory operation), CHAT-04 (at most one question per
message), CHAT-10 (plain conversational text, median under 120 words), SAFE-05
(observe, never diagnose) and HLTH-09 (never react to one body mass reading,
never compare two) are behavioural requirements, so the real test is the
regression suite in :mod:`tests`. These functions are what that suite asserts
with, and what the turn loop uses to catch a violation before it reaches the
athlete.

Both checks count meaning rather than punctuation. v2.1 rewrote CHAT-04's
acceptance for exactly this reason: "no two question marks" passes for two
questions in one sentence and for a question phrased as an imperative.
"""

from __future__ import annotations

import re

# CHAT-03. Phrasings that assert a memory operation happened. The regression
# suite probes with explicit invitations to narrate, so this is a backstop for
# the obvious forms rather than the whole test.
NARRATION = re.compile(
    r"\b("
    r"(i|i've|i have|that's|that is)\s+(now\s+)?"
    r"(saved|noted|logged|recorded|stored|remembered|updated)"
    r"|(saving|noting|logging|recording|storing|remembering|updating)\s+(that|this|it)"
    r"|(added|written)\s+(that|this|it)\s+to\s+(your|my)\s+"
    r"|i'?ll\s+(remember|note|record|save)\s+(that|this|it)"
    r"|(noted|logged|saved|recorded)\b\s*[.!]"
    r")",
    re.IGNORECASE,
)

# CHAT-04. An interrogative opener, or an imperative that requests information.
_INTERROGATIVE = re.compile(
    r"^\s*(how|what|when|where|which|who|why|are|is|was|were|do|does|did|can|could|"
    r"will|would|should|have|has|had|any|anything|everything\s+ok)\b",
    re.IGNORECASE,
)
_IMPERATIVE_REQUEST = re.compile(
    r"\b(tell me|let me know|talk me through|walk me through|give me a sense|"
    r"fill me in|remind me|describe)\b",
    re.IGNORECASE,
)


def narrates_memory(text: str) -> bool:
    """CHAT-03: does this response assert that something was saved or noted?"""
    return bool(NARRATION.search(text))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def count_questions(text: str) -> int:
    """CHAT-04: requests for information, however they are punctuated.

    A compound question joined by "or" counts once, because a single answer
    resolves it. Two separate asks in one sentence count twice.
    """
    total = 0
    for sentence in _sentences(text):
        asks = 0
        if sentence.endswith("?") or _INTERROGATIVE.match(sentence):
            asks = 1
        if _IMPERATIVE_REQUEST.search(sentence):
            asks = max(asks, 1)

        if asks:
            # A second independent ask inside the same sentence, joined by "and"
            # or a semicolon rather than "or". Each clause is tested the same way
            # the sentence was — an imperative request counts here too, or
            # "let me know how it went and tell me what the hip did" reads as one
            # question when it is plainly two.
            clauses = [c.strip() for c in re.split(r";|\band\b", sentence) if c.strip()]
            independent = sum(
                1
                for c in clauses
                if _INTERROGATIVE.match(c) or "?" in c or _IMPERATIVE_REQUEST.search(c)
            )
            asks = max(asks, independent)
        total += asks
    return total


# SAFE-05. The coach surfaces observations and points at a clinician; it does
# not name a condition or attribute a cause. Matching a diagnosis phrased as a
# hedge ("sounds like tendinitis") matters more than matching a flat assertion,
# because the hedge is the form it actually takes.

# A named condition: the medical suffixes, plus the structural injuries a coach
# is most likely to reach for.
_CONDITION = r"\w*(?:itis|osis|opathy|algia)\b|\b(?:tear|rupture|impingement|fracture)\b"
# Modifiers sit between the frame and the condition — "patellar tendinitis",
# "a grade two meniscus tear" — so the frame cannot require them to be adjacent.
_MODIFIER = r"(?:\w+[\s-]+){0,3}"

DIAGNOSIS = re.compile(
    r"\b(?:"
    rf"(?:sounds|looks|seems)\s+like\s+(?:an?\s+)?{_MODIFIER}(?:{_CONDITION})"
    rf"|(?:you|that)\s+(?:have|has|'ve\s+got)\s+(?:an?\s+)?{_MODIFIER}(?:{_CONDITION})"
    rf"|(?:that'?s|this\s+is)\s+(?:an?\s+)?{_MODIFIER}(?:{_CONDITION})"
    rf"|(?:probably|likely|classic|textbook)\s+{_MODIFIER}(?:{_CONDITION})"
    r"|i'?d\s+(?:diagnose|say\s+it'?s)\b"
    r")",
    re.IGNORECASE,
)

# The shape SAFE-05 does want: an observation, and a pointer to a clinician.
CLINICAL_REFERRAL = re.compile(
    r"\b(physio|physiotherapist|doctor|gp|clinician|medical|specialist|get it looked at)\b",
    re.IGNORECASE,
)


def diagnoses(text: str) -> bool:
    """SAFE-05: does this response name a condition rather than observe one?"""
    return bool(DIAGNOSIS.search(text))


def refers_clinically(text: str) -> bool:
    """Does it point at someone qualified to diagnose?"""
    return bool(CLINICAL_REFERRAL.search(text))


# HLTH-09. The coach never comments on a single reading moving up, and never
# compares two individual readings.
#
# This is a backstop, not the mechanism. The mechanism is that the readings are
# not in the context at all — :func:`coach.agent.prompt.render_body_mass` puts a
# fitted slope and a set of permissions there instead, so there is nothing to
# compare. What is caught here is the case where the athlete quotes his own two
# numbers in a message and the coach repeats them back as a comparison.

# An absolute body mass figure. A rate is not one, so rates are removed before
# counting: "between 0.30 and 0.45 kg per week" is exactly what HLTH-08 asks for
# and must not read as two readings.
_WEIGHT_FIGURE = re.compile(r"\d{1,3}(?:\.\d+)?\s*(?:kg|kilos?|kilograms?)\b", re.IGNORECASE)
_RATE_FIGURE = re.compile(
    r"\d{1,3}(?:\.\d+)?\s*(?:kg|kilos?|kilograms?)?\s*(?:per|a|/)\s*(?:week|wk|month|fortnight)",
    re.IGNORECASE,
)

_WEIGHT_SUBJECT = re.compile(
    r"\b(weight|weigh(?:ed|s|ing)?|weigh[-\s]?in|scales?|kg|kilos?|body\s*mass)\b",
    re.IGNORECASE,
)
_MOVEMENT = re.compile(
    r"\b(up|down|gained|lost|heavier|lighter|jumped|spiked|dropped|climbed)\b",
    re.IGNORECASE,
)
# A single occasion, as opposed to a window. "over the last month" is a trend and
# is allowed; "this morning" is one reading and is not.
_SINGLE_OCCASION = re.compile(
    r"\b(today|this\s+morning|yesterday|last\s+night|that\s+reading|this\s+(?:one|reading)|"
    r"since\s+yesterday|since\s+(?:your\s+)?last\s+(?:reading|weigh[-\s]?in))\b",
    re.IGNORECASE,
)


def compares_individual_readings(text: str) -> bool:
    """HLTH-09: two body mass figures set against each other in one sentence."""
    for sentence in _sentences(text):
        cleaned = _RATE_FIGURE.sub(" ", sentence)
        if len(_WEIGHT_FIGURE.findall(cleaned)) >= 2:
            return True
    return False


def reacts_to_single_reading(text: str) -> bool:
    """HLTH-09: a claim about weight moving, pinned to one occasion."""
    for sentence in _sentences(text):
        if (
            _WEIGHT_SUBJECT.search(sentence)
            and _MOVEMENT.search(sentence)
            and _SINGLE_OCCASION.search(sentence)
        ):
            return True
    return False


def word_count(text: str) -> int:
    return len(text.split())


def has_markup(text: str) -> bool:
    """CHAT-10: no headers or bullet dumps unless asked."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "- ", "* ", "• ")) or re.match(r"^\d+\.\s", stripped):
            return True
    return False


def violations(text: str) -> list[str]:
    """Every naturalness rule this response breaks. Empty means it is clean."""
    found = []
    if narrates_memory(text):
        found.append("CHAT-03: narrates a memory operation")
    questions = count_questions(text)
    if questions > 1:
        found.append(f"CHAT-04: asks {questions} questions")
    if has_markup(text):
        found.append("CHAT-10: contains headers or bullets")
    if diagnoses(text):
        found.append("SAFE-05: names a condition rather than observing one")
    if compares_individual_readings(text):
        found.append("HLTH-09: compares two individual body mass readings")
    if reacts_to_single_reading(text):
        found.append("HLTH-09: comments on a single body mass reading moving")
    return found
