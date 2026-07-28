"""The exercise library, and substitution.

GYM-03: "An exercise library supports substitution so a blocked or unavailable
movement is swapped rather than dropped." The acceptance is specific — removing
a movement's equipment yields a substitute, not an empty slot.

Two reasons a movement gets swapped, and they are not the same reason:

* **Blocked.** A constraint forbids it (GYM-02). The substitute must come from a
  *different* pattern, because the pattern is what was excluded. Swapping a
  barbell deadlift for a kettlebell swing would satisfy the letter of "no
  barbell deadlifts" and violate the hinge restriction it was written to express.
* **Unavailable.** The equipment is not there. The substitute should stay in the
  *same* pattern, because the training intent is the pattern and only the
  implement has changed.

Getting those the wrong way round is the failure mode this module exists to
prevent, so they are separate functions rather than one function with a flag.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import psycopg

from coach.blocks import constraints as constraintmod

log = logging.getLogger(__name__)

# What the athlete's small apartment gym actually has. Configuration rather than
# code because it changes when he does — the seed records the apartment gym as a
# deliberate adherence choice until roughly 105 kg, so this will change once.
DEFAULT_EQUIPMENT = ("bodyweight", "band", "dumbbell")

# When a whole pattern is excluded, what to reach for instead. Ordered by how
# closely the substitute preserves the training intent. A hinge is not
# replaceable by anything that loads the spine, which is the point of the
# restriction, so it falls back to hip extension and single leg work.
PATTERN_FALLBACKS: dict[str, tuple[str, ...]] = {
    "hinge": ("hip_extension", "lunge", "squat"),
    "squat": ("lunge", "hip_extension"),
    "lunge": ("squat", "hip_extension"),
    "core_flexion": ("core_antiextension", "core_antirotation"),
    "core_rotation": ("core_antirotation", "core_antiextension"),
    "push_vertical": ("push_horizontal",),
    "push_horizontal": ("push_vertical",),
    "pull_vertical": ("pull_horizontal",),
    "pull_horizontal": ("pull_vertical",),
    "carry": ("core_antirotation",),
}


def available_equipment() -> tuple[str, ...]:
    raw = os.environ.get("COACH_GYM_EQUIPMENT")
    if not raw:
        return DEFAULT_EQUIPMENT
    items = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    return items or DEFAULT_EQUIPMENT


@dataclass(frozen=True)
class Exercise:
    id: int
    name: str
    movement_pattern: str
    equipment: str
    aliases: list[str]
    spinal_load: int


def _row(row: dict) -> Exercise:
    return Exercise(
        id=row["id"],
        name=row["name"],
        movement_pattern=row["movement_pattern"],
        equipment=row["equipment"],
        aliases=list(row["aliases"] or []),
        spinal_load=row["spinal_load"],
    )


def all_exercises(conn: psycopg.Connection) -> list[Exercise]:
    with conn.cursor() as cur:
        cur.execute("select * from exercises order by movement_pattern, spinal_load, name")
        return [_row(r) for r in cur.fetchall()]


def known_movements(conn: psycopg.Connection) -> set[str]:
    """Every name and alias the library knows, for the constraint reader."""
    names: set[str] = set()
    for exercise in all_exercises(conn):
        names.add(exercise.name.lower())
        names.update(alias.lower() for alias in exercise.aliases)
    return names


def in_pattern(
    conn: psycopg.Connection, pattern: str, equipment: tuple[str, ...] | None = None
) -> list[Exercise]:
    """Every available exercise in a pattern, gentlest on the spine first."""
    have = equipment if equipment is not None else available_equipment()
    with conn.cursor() as cur:
        cur.execute(
            "select * from exercises where movement_pattern = %s and equipment = any(%s) "
            "order by spinal_load, name",
            (pattern, list(have)),
        )
        return [_row(r) for r in cur.fetchall()]


def choose(
    conn: psycopg.Connection,
    pattern: str,
    constraints: constraintmod.Constraints,
    equipment: tuple[str, ...] | None = None,
) -> Exercise | None:
    """The best permitted, available movement in a pattern.

    Returns None when the pattern is excluded outright or nothing available sits
    in it — the caller then asks for a substitute rather than this function
    quietly returning something from elsewhere.
    """
    for exercise in in_pattern(conn, pattern, equipment):
        if constraints.blocks(exercise.movement_pattern, exercise.name) is None:
            return exercise
    return None


def substitute(
    conn: psycopg.Connection,
    pattern: str,
    constraints: constraintmod.Constraints,
    equipment: tuple[str, ...] | None = None,
) -> Exercise | None:
    """GYM-03: a swap for a blocked or unavailable pattern.

    Only the declared fallbacks, and None when every one of them is also
    excluded. That last part was a wider search once — take anything permitted
    and available rather than leave a slot empty — and it was wrong. Asked for a
    squat with squat, lunge and hip extension all excluded, it returned a calf
    raise: a movement that satisfies "not empty" and preserves nothing about the
    session's purpose.

    GYM-03's acceptance is "removing a movement's *equipment* yields a
    substitute", and that case is served by :func:`choose` within the same
    pattern. When a whole pattern and every near neighbour is excluded, the
    truthful answer is that there is nothing appropriate — and the
    `constraint_blocks` row records it as a refusal with no substitution, which
    is a thing the Sunday review can act on. A nonsense movement in the session
    is a thing nobody notices.
    """
    for fallback in PATTERN_FALLBACKS.get(pattern, ()):
        found = choose(conn, fallback, constraints, equipment)
        if found is not None:
            return found
    return None


def resolve(
    conn: psycopg.Connection,
    pattern: str,
    constraints: constraintmod.Constraints,
    equipment: tuple[str, ...] | None = None,
) -> tuple[Exercise | None, constraintmod.Exclusion | None]:
    """Pick a movement for a pattern, substituting if it has to.

    Returns the exercise and the exclusion that forced a substitution, so the
    caller can log the block with the constraint that caused it (GYM-02).
    """
    direct = choose(conn, pattern, constraints, equipment)
    if direct is not None:
        return direct, None

    # Why the pattern produced nothing: excluded, or simply not stocked.
    blocked_by = None
    for exercise in in_pattern(conn, pattern, ()) or all_exercises(conn):
        if exercise.movement_pattern != pattern:
            continue
        found = constraints.blocks(exercise.movement_pattern, exercise.name)
        if found is not None:
            blocked_by = found
            break

    return substitute(conn, pattern, constraints, equipment), blocked_by
