"""What the athlete may not be asked to do.

SAFE-04: prescriptions are validated against active constraints before being
written or scheduled. GYM-02: any movement pattern excluded by a constraint is
never prescribed, and an attempt is blocked before publish and logged.

**A constraint excludes a pattern, not a name.** "No loaded hip hinge" has to
stop an exercise nobody thought to list, so matching runs against the movement
pattern first and the exercise name second. Matching names alone would let a
kettlebell swing through a hinge restriction because the word "deadlift" is
absent from it.

**An unreadable constraint blocks generation.** This is the decision in this
module worth arguing with, so it is stated plainly: if a constraint phrase
matches nothing in the vocabulary, gym generation refuses rather than proceeding
as though the athlete were unconstrained. The alternative — ignore what we could
not parse — means a constraint the athlete stated in his own words silently
stops applying, and he would have no way to notice. The PRD's governing
asymmetry says the system fails toward less, and less here means "no session"
rather than "a session against a restriction nobody read".

The constraints themselves are safety facts. They load verbatim into every
prompt (SAFE-01), never decay (SAFE-03) and can only be written by the athlete
(SAFE-06). Nothing in this module writes one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import psycopg

from coach.memory import facts as factmod
from coach.memory import keys as keymod

log = logging.getLogger(__name__)

# The controlled vocabulary of movement patterns, with the words a person
# actually uses for each. A constraint is read against this; anything it says
# that lands nowhere here is reported rather than ignored.
#
# The three trunk patterns are deliberately not one pattern. The athlete's
# restrictions rule out flexion and loaded rotation while anti-extension and
# anti-rotation are precisely what a repaired disc wants, so collapsing them
# into "core" would ban the useful half along with the harmful half.
PATTERN_WORDS: dict[str, tuple[str, ...]] = {
    "hinge": ("hinge", "hip hinge", "deadlift", "rdl", "swing", "good morning"),
    "squat": ("squat",),
    "lunge": ("lunge", "split squat"),
    "push_horizontal": ("bench", "chest press", "push up", "press up"),
    "push_vertical": ("overhead press", "shoulder press", "ohp", "overhead"),
    "pull_horizontal": ("row",),
    "pull_vertical": ("pull up", "pullup", "chin up", "pulldown", "lat pull"),
    "carry": ("carry", "farmer", "suitcase"),
    "core_flexion": ("sit up", "situp", "crunch", "spinal flexion", "flexion"),
    "core_rotation": ("twist", "rotation", "russian twist"),
    "core_antiextension": ("plank", "dead bug", "bird dog", "anti-extension"),
    "core_antirotation": ("pallof", "side plank", "anti-rotation"),
    "hip_abduction": ("clamshell", "abduction", "lateral walk", "monster walk"),
    "hip_extension": ("glute bridge", "hip thrust", "hip extension"),
    "calf": ("calf", "heel raise"),
}

# Words that mean "this is a restriction" rather than naming a movement. A
# constraint is a sentence, and the parts that are not movements are the parts
# that make it a sentence.
_NOISE = re.compile(
    r"\b("
    r"no|not|avoid|never|without|any|all|the|a|an|and|or|of|for|with|to|is|are|be|"
    r"heavy|light|loaded|unloaded|barbell|dumbbell|kettlebell|band|banded|machine|"
    r"bodyweight|weighted|max|maximal|maximum|"
    r"pending|review|around|week|weeks|because|produces|highest|lumbar|shear|common|"
    r"gym|pattern|technique|errors|expensive|withheld|until|cleared|per|his|him|he|"
    r"conventional|single|leg|other|activity|"
    r"lift|lifts|lifting|movement|movements|exercise|exercises|session|sessions"
    r")\b",
    re.IGNORECASE,
)

# Punctuation and connectives a constraint sentence is split on, so
# "no sit-ups or crunches" is read as two things rather than one phrase.
_SPLIT = re.compile(r"[,;.()]|\bor\b|\band\b", re.IGNORECASE)


class ConstraintNotUnderstood(RuntimeError):
    """A constraint names something outside the vocabulary.

    Raised rather than logged and swallowed. See the module docstring: a
    constraint that cannot be read is not a constraint that does not apply.
    """


@dataclass(frozen=True)
class Exclusion:
    """One thing the athlete may not do, and the words that said so."""

    pattern: str | None
    movement: str | None
    source: str

    def describes(self, pattern: str, name: str) -> bool:
        if self.pattern is not None and self.pattern == pattern:
            return True
        return self.movement is not None and self.movement in name.lower()


@dataclass
class Constraints:
    """Everything the active safety facts forbid, resolved once."""

    exclusions: list[Exclusion] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def patterns(self) -> set[str]:
        return {e.pattern for e in self.exclusions if e.pattern}

    def blocks(self, pattern: str, name: str) -> Exclusion | None:
        """The exclusion that forbids this movement, if any."""
        for exclusion in self.exclusions:
            if exclusion.describes(pattern, name):
                return exclusion
        return None

    def require_readable(self) -> None:
        """Fail closed on anything the vocabulary could not account for."""
        if self.unreadable:
            raise ConstraintNotUnderstood(
                "these constraint phrases match no known movement or pattern, so gym "
                "programming cannot prove a session respects them: "
                + "; ".join(repr(p) for p in self.unreadable)
                + ". Add the movement to the exercise library or the pattern vocabulary "
                "rather than rewording the athlete's constraint."
            )


def load(conn: psycopg.Connection, known_movements: set[str] | None = None) -> Constraints:
    """Read the active safety facts and resolve them into exclusions.

    `known_movements` is the exercise library's names, so a constraint naming a
    specific movement is readable even when it names no pattern — "no barbell
    deadlifts" resolves as the hinge pattern, but a constraint naming only a
    movement the library knows still counts as read.
    """
    vocabulary = keymod.load_all(conn)
    result = Constraints()

    for fact in factmod.active(conn):
        if not vocabulary[fact.key].safety:
            continue
        for phrase in _phrases(fact.value):
            result.sources.append(phrase)
            found = _resolve(phrase, known_movements or set())
            if found:
                result.exclusions.extend(found)
            elif _names_something(phrase):
                # It named a movement-shaped thing that is not in the
                # vocabulary. That is the case that must not pass silently.
                result.unreadable.append(phrase)

    return result


def _phrases(value: object) -> list[str]:
    """A safety fact's value as individual restriction phrases.

    Constraint facts are lists of sentences. Each is split on connectives so
    "no sit-ups or crunches" produces two phrases and each can be matched
    independently.
    """
    items = value if isinstance(value, list) else [value]
    phrases = []
    for item in items:
        if not isinstance(item, str):
            continue
        for part in _SPLIT.split(item):
            cleaned = part.strip()
            if cleaned:
                phrases.append(cleaned)
    return phrases


def _matcher(word: str) -> re.Pattern[str]:
    """A regex for one vocabulary word, tolerant of how people actually write.

    Three variations, all of which appear in the athlete's own constraints:
    hyphenation ("sit-ups"), spacing ("sit up", "situp") and plurals ("crunches",
    "deadlifts"). A plain substring match missed `no sit-ups` against `sit up`
    and refused to generate anything — correct behaviour on a vocabulary that
    was too literal, which is the wrong half to fix.

    Word boundaries are kept rather than squashing the text, so the three letter
    entries stay honest: without them `row` matches inside `narrow` and every
    pulling movement disappears from the programme.
    """
    body = re.escape(word).replace(r"\ ", r"[\s\-]?")
    return re.compile(rf"\b{body}(?:e?s)?\b", re.IGNORECASE)


_MATCHERS: dict[str, tuple[re.Pattern[str], ...]] = {
    pattern: tuple(_matcher(word) for word in words) for pattern, words in PATTERN_WORDS.items()
}


def _resolve(phrase: str, known_movements: set[str]) -> list[Exclusion]:
    """Every pattern and movement this phrase excludes."""
    found: list[Exclusion] = []

    for pattern, matchers in _MATCHERS.items():
        if any(matcher.search(phrase) for matcher in matchers):
            found.append(Exclusion(pattern=pattern, movement=None, source=phrase))

    for movement in known_movements:
        if _matcher(movement).search(phrase):
            found.append(Exclusion(pattern=None, movement=movement, source=phrase))

    return found


def _names_something(phrase: str) -> bool:
    """Does this phrase look like it names a movement at all?

    A constraint fact carries prose as well as restrictions — "recovery
    complete, occasional stiffness", "discharged by physio" — and treating
    every clause as an unreadable movement would block generation on a
    perfectly ordinary injury history. So a phrase counts as naming something
    only once the sentence-scaffolding words are removed and something is left.

    This is the one place the fail-closed rule is softened, and it is softened
    on the side of *reading* rather than of ignoring: a phrase with a residue is
    treated as a movement we failed to understand, not as prose.
    """
    residue = _NOISE.sub(" ", phrase).strip(" -")
    residue = re.sub(r"[^a-z\s-]", " ", residue.lower()).strip()
    if not residue:
        return False
    # Prose about the body or the clinic, not about a movement. These are the
    # words that actually appear in an injury history.
    prose = {
        "recovery",
        "complete",
        "occasional",
        "stiffness",
        "discharged",
        "physio",
        "physiotherapist",
        "cleared",
        "golf",
        "repair",
        "surgery",
        "disc",
        "herniated",
        "november",
        "december",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
    }
    words = {w for w in residue.split() if len(w) > 2}
    return bool(words - prose)


def check(
    conn: psycopg.Connection,
    pattern: str,
    name: str,
    constraints: Constraints | None = None,
) -> Exclusion | None:
    """SAFE-04: is this movement permitted? Returns the exclusion, or None."""
    resolved = constraints if constraints is not None else load(conn)
    return resolved.blocks(pattern, name)


def record_block(
    conn: psycopg.Connection,
    block_id: int | None,
    discipline: str,
    movement: str,
    pattern: str | None,
    exclusion: Exclusion,
    planned_for: object = None,
    substituted_with: str | None = None,
) -> int:
    """GYM-02 and SAFE-04: log the refusal as a row, not a log line.

    A blocked prescription is evidence about the programme — the generator
    wanted something the athlete cannot do — and the Sunday review should be
    able to read it back rather than the fact living in a rotated log file.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into constraint_blocks
                (block_id, discipline, planned_for, movement, pattern,
                 constraint_text, substituted_with)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                block_id,
                discipline,
                planned_for,
                movement,
                pattern,
                exclusion.source,
                substituted_with,
            ),
        )
        row = cur.fetchone()
    log.info("blocked %s (%s): %s", movement, pattern, exclusion.source)
    return row["id"]
