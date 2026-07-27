"""Naturalness checks.

CHAT-03 (never narrate a memory operation), CHAT-04 (at most one question per
message) and CHAT-10 (plain conversational text, median under 120 words) are
behavioural requirements, so the real test is the regression suite in
:mod:`tests`. These functions are what that suite asserts with, and what the
turn loop uses to catch a violation before it reaches the athlete.

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
    return found
