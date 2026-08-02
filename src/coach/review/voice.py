"""The review, said in the coach's voice rather than assembled in his name.

REV-01 posts the review into the chat, and for as long as the review was
assembled and posted by the same function, what landed in the chat was a form.
Six labelled sections, one per line, in a fixed order, whether or not any of
them had anything in them. It was accurate and nobody would mistake it for a
message from a person.

**The split is deliberate and it only runs one way.** `weekly.build` computes
every figure in SQL and hands over finished text (MEM-08). This module may
reorder that text, cut it, and say it differently. It may not add a number to
it, and `_grounded` below enforces that rather than asking for it — the whole
value of a deterministically assembled review is lost the moment the thing that
rewrites it is also allowed to do arithmetic.

**Falling back is the normal case, not the error case.** No API key, no client,
a model call that fails, a reply that fails the guard: all of them post
`Review.message()`, which is a decent structured message on its own. The voiced
version is an improvement on a working message, so nothing here is allowed to
cost the athlete his Sunday review.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg

from coach.agent import persona
from coach.llm import client as llmmod
from coach.review.weekly import Review

log = logging.getLogger(__name__)

PURPOSE = "review"

# A voiced review that comes back longer than this has stopped summarising. The
# deterministic message is around a thousand characters at its fullest, and the
# instruction is to be shorter than that, so the ceiling is generous on purpose:
# it catches a model that has started writing an essay, not one that ran long.
MAX_CHARS = 2400

# Numbers below this are ordinary prose — "a couple", "the first two weeks" —
# and requiring them to appear in the facts would fail honest sentences. Above
# it, a number the facts do not contain was invented.
FREE_INTEGER_MAX = 12

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


TASK = """\
Below is this week's review for Christian, assembled from the database. Every
figure in it was computed in SQL and is correct.

Your job is to say it the way you would say it, and nothing else.

Rules, in order of importance:

1. Use only the facts given. Do not add a number, a date, a session, a weight,
   or a conclusion that is not below. If a figure is not there, you do not know
   it. You may drop anything you judge not worth his time.
2. Do not carry across any sentence that is written to you rather than to him.
   The facts below are an internal briefing: anything phrased as what you may
   say, must not say, or should never do is an instruction, and repeating it to
   him is a mistake. State what is true, not what you are permitted to state.
3. Keep the question at the end exactly as it is, and ask nothing else. One
   question mark in the whole message.
4. Keep anything under "Waiting on you", because those are his decisions to
   make. Say them as decisions, under that heading.
5. Do not restate the Goals section. He set those goals, they have not moved,
   and he does not need them read back to him. They are there so you know what
   the week was for. Use them to explain why something mattered, never as a
   thing to announce.

This is a review of a week of training, so it covers, in this order:

- **The effort.** What he actually did: sessions, time, distance. Lead with
  this. It is the part of the week that was his.
- **Whether it matched the plan.** Adherence, and what the gap was if there was
  one. Say what happened, not how you feel about it.
- **What the body did with it**, if anything in weight, recovery or intake is
  worth a sentence. Skip whichever is not.
- **Next week, and the session that carries it.** Name the day and the session.
  This is the part he acts on, so it goes near the end where it is remembered.

Then one line on why next week's work is worth doing. Ground it in what the
numbers actually show and in what the block is building, not in enthusiasm. A
true reason to do Wednesday's session is motivating; being told he is doing
well is not, and he will discount everything around it. If this week gives you
nothing honest to point at, leave the line out entirely. No flattery, no doom,
no exclamation marks, and never congratulate him for turning up.

Shape it like this, because it is read on a phone:

- One short opening line: the week in a sentence.
- A blank line, then the substance, in short paragraphs of one to three
  sentences with a blank line between them. Lead each with what it is about.
  Three or four of these, not eight.
- "Waiting on you:" and the decisions, if there are any.
- A blank line, then the question, on its own.

Write it as prose in your own voice. No bold, no markdown headings, no bullet
points except under "Waiting on you". No em dashes. Do not open by greeting him
and do not close by summarising what you just said.

If the week was quiet, say so plainly and briefly, and go straight to what is
coming. Do not pad it and do not manufacture a talking point out of an empty
section. A quiet week plus next week's key session is a complete review.

Reply with the message and nothing else. No preamble, no sign-off, no note
about what you changed.

--- the week's facts ---

{facts}
"""


def _grounded(text: str, facts: str) -> bool:
    """Every substantial number in the reply came from the facts.

    Substring rather than equality, and deliberately lenient in that direction:
    "129 kg" against a fact sheet holding "129.1 kg" is the model rounding, which
    is fine, while "131 kg" appears nowhere and is not. The failure this catches
    is invention, not imprecision.
    """
    for match in _NUMBER.findall(text):
        if match in facts:
            continue
        # A comma decimal is a formatting choice, not a different number.
        if match.replace(",", ".") in facts:
            continue
        try:
            if float(match.replace(",", ".")) <= FREE_INTEGER_MAX:
                continue
        except ValueError:
            pass
        log.warning("voiced review quoted %r, which is not in the facts", match)
        return False
    return True


def check(text: str, facts: str) -> str | None:
    """The reply, or None with a reason logged.

    Every one of these is a way the voiced message could be worse than the
    assembled one, and there is no partial credit: a message that fails any of
    them is discarded whole rather than patched, because a repaired message is
    one nobody wrote.
    """
    said = text.strip()
    if not said:
        log.warning("voiced review came back empty")
        return None
    if len(said) > MAX_CHARS:
        log.warning("voiced review ran to %d characters; discarding", len(said))
        return None
    # REV-03, and the reason this is checked rather than trusted: the count is a
    # property the assembly guarantees, and voicing is the only step that could
    # take it away.
    if said.count("?") != 1:
        log.warning("voiced review asked %d questions; discarding", said.count("?"))
        return None
    if not _grounded(said, facts):
        return None
    return said


def say(
    review: Review,
    client: Any | None,
    conn: psycopg.Connection | None = None,
) -> str:
    """The review as a message. Voiced if that works, assembled if it does not.

    `conn` is for the MODEL-01 call record and is genuinely optional — the
    review has already been stored by the time this runs, and a voicing call
    that cannot be accounted for is still better than a form.
    """
    fallback = review.message()
    if client is None:
        return fallback

    facts = review.render()
    try:
        completion = llmmod.complete(
            client,
            purpose=PURPOSE,
            system=[{"type": "text", "text": persona.load()}],
            messages=[{"role": "user", "content": TASK.format(facts=facts)}],
            conn=conn,
        )
    except Exception:
        # Including the persona file being absent, which is a deployment fault
        # and still must not cost the review.
        log.exception("voicing the review failed; posting the assembled one")
        return fallback

    said = check(completion.text, facts)
    if said is None:
        return fallback
    return said
