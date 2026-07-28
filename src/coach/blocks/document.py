"""The block document, and its history.

BLOCK-01: "A training block is a versioned markdown document with goals,
constraints and a week by week plan. Retrieving a block returns current content
and full version history."

BLOCK-02: "The agent rewrites the block rather than regenerating it from
scratch. Diffs between versions are localised, not wholesale replacements."

BLOCK-02 is a behavioural requirement about the agent, and the only way to make
it checkable is to keep every version so a reviewer can diff two rows. A
changelog would be a claim about a diff that nobody can verify. :func:`rewrite`
therefore takes a section and replaces that section, which makes a localised
diff the easy path and a wholesale replacement the deliberate one.

BLOCK-05 lives here too: a block's goals must carry a fitness preservation goal
alongside the weight goal. The seed states the reason in the athlete's own
words — "weight that stays off; lean mass retention is the mechanism" — so a
block that optimises weight alone is optimising the wrong thing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from coach.memory import facts as factmod
from coach.memory import keys as keymod

log = logging.getLogger(__name__)

# BLOCK-05. The two goals every block carries, and the fact key each comes from.
WEIGHT_GOAL_KEY = "goal.target_weight_kg"
PRESERVATION_GOAL_KEY = "goal.fitness_preservation"

# The sections a block document is made of. Fixed so a rewrite can target one,
# which is what makes BLOCK-02's localised diff the natural outcome.
SECTIONS = ("goals", "constraints", "physiology", "plan", "review")


class MissingGoal(ValueError):
    """BLOCK-05: a block needs both a weight goal and a preservation goal."""


@dataclass(frozen=True)
class Version:
    version: int
    content: str
    reason: str
    author: str
    created_at: Any


@dataclass(frozen=True)
class Block:
    id: int
    title: str
    goals: dict[str, Any]
    starts_on: date
    weeks: int
    status: str
    content: str
    history: list[Version]

    @property
    def version(self) -> int:
        return self.history[0].version if self.history else 0


def goals_from_facts(conn: psycopg.Connection) -> dict[str, Any]:
    """BLOCK-05: assemble the block's goals from what is actually known.

    Read from facts rather than passed in, so a block cannot be created with a
    goal the memory store does not hold. Raises when the preservation goal is
    absent, because a block that optimises weight alone is optimising the wrong
    thing and silently omitting it is how that happens.
    """
    weight = factmod.active_for(conn, WEIGHT_GOAL_KEY)
    preservation = factmod.active_for(conn, PRESERVATION_GOAL_KEY)

    if preservation is None:
        raise MissingGoal(
            f"{PRESERVATION_GOAL_KEY} is not an active fact. BLOCK-05 requires a fitness "
            "preservation goal alongside the weight goal, and the review checks both."
        )
    if weight is None:
        raise MissingGoal(
            f"{WEIGHT_GOAL_KEY} is not an active fact, so there is no weight goal to "
            "preserve fitness alongside."
        )

    return {
        "target_weight_kg": weight.value,
        "fitness_preservation": preservation.value,
    }


def render_constraints(conn: psycopg.Connection) -> str:
    """The constraints section, verbatim from the safety facts (SAFE-01).

    Copied into the document rather than referenced, because the document is
    read by a human deciding whether the block is safe, and a reference is
    something a reader has to go and check.
    """
    vocabulary = keymod.load_all(conn)
    safety = [f for f in factmod.active(conn) if vocabulary[f.key].safety]
    if not safety:
        return "None recorded."

    lines = []
    for fact in sorted(safety, key=lambda f: f.key):
        values = fact.value if isinstance(fact.value, list) else [fact.value]
        for value in values:
            lines.append(f"- {value}")
    return "\n".join(lines)


def create(
    conn: psycopg.Connection,
    title: str,
    starts_on: date,
    content: str,
    reason: str = "block created",
    weeks: int = 4,
    goals: dict[str, Any] | None = None,
) -> int:
    """Create a block and its first version. BLOCK-03 fixes the length at four."""
    resolved = goals if goals is not None else goals_from_facts(conn)
    if "fitness_preservation" not in resolved:
        raise MissingGoal("BLOCK-05: a block's goals must include fitness_preservation")

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into blocks (title, goals, starts_on, weeks) values (%s, %s, %s, %s) "
            "returning id",
            (title, Jsonb(resolved), starts_on, weeks),
        )
        block_id = cur.fetchone()["id"]
        cur.execute(
            "insert into block_versions (block_id, version, content, reason) "
            "values (%s, 1, %s, %s)",
            (block_id, content, reason),
        )
    return block_id


def get(conn: psycopg.Connection, block_id: int) -> Block | None:
    """BLOCK-01: current content and full version history, in one read."""
    with conn.cursor() as cur:
        cur.execute("select * from blocks where id = %s", (block_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "select version, content, reason, author, created_at from block_versions "
            "where block_id = %s order by version desc",
            (block_id,),
        )
        history = [Version(**v) for v in cur.fetchall()]

    return Block(
        id=row["id"],
        title=row["title"],
        goals=row["goals"],
        starts_on=row["starts_on"],
        weeks=row["weeks"],
        status=row["status"],
        content=history[0].content if history else "",
        history=history,
    )


def active(conn: psycopg.Connection) -> Block | None:
    with conn.cursor() as cur:
        cur.execute("select id from blocks where status = 'active' limit 1")
        row = cur.fetchone()
    return get(conn, row["id"]) if row else None


def activate(conn: psycopg.Connection, block_id: int) -> None:
    """Make this the active block, retiring whichever was.

    One active block at a time is enforced by a partial unique index, so this
    has to close the old one in the same transaction as opening the new.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update blocks set status = 'completed' where status = 'active'")
        cur.execute("update blocks set status = 'active' where id = %s", (block_id,))


def rewrite(
    conn: psycopg.Connection,
    block_id: int,
    section: str,
    content: str,
    reason: str,
    author: str = "coach",
) -> int:
    """BLOCK-02: replace one section and keep the rest.

    Returns the new version number. The whole-document alternative exists as
    :func:`replace`, which is deliberately the longer thing to type.
    """
    current = get(conn, block_id)
    if current is None:
        raise ValueError(f"no block {block_id}")
    return replace(conn, block_id, _swap(current.content, section, content), reason, author)


def replace(
    conn: psycopg.Connection,
    block_id: int,
    content: str,
    reason: str,
    author: str = "coach",
) -> int:
    """Write a new version wholesale. BLOCK-08's restructure path."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "select coalesce(max(version), 0) + 1 as next from block_versions where block_id = %s",
            (block_id,),
        )
        version = cur.fetchone()["next"]
        cur.execute(
            "insert into block_versions (block_id, version, content, reason, author) "
            "values (%s, %s, %s, %s, %s)",
            (block_id, version, content, reason, author),
        )
    return version


def _swap(document: str, section: str, content: str) -> str:
    """Replace one `## Section` and its body, leaving every other byte alone.

    Appends the section when it is absent rather than failing, because a block
    written before a section existed should gain it on the first rewrite rather
    than needing a migration.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(section)}\s*$.*?(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    replacement = f"## {section}\n\n{content.strip()}\n\n"
    if pattern.search(document):
        return pattern.sub(replacement, document, count=1)
    return document.rstrip() + "\n\n" + replacement


def section_of(document: str, section: str) -> str:
    """Read one section back out, for tests and for the review."""
    match = re.search(
        rf"^##\s+{re.escape(section)}\s*$(.*?)(?=^##\s|\Z)",
        document,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def diff_size(before: str, after: str) -> int:
    """How many lines changed between two versions.

    BLOCK-02's acceptance is that diffs are localised rather than wholesale, and
    "localised" needs a number to be testable. Counting changed lines is crude
    and it is enough: a section rewrite touches a handful, a regeneration
    touches nearly all of them.
    """
    old, new = before.splitlines(), after.splitlines()
    common = set(old) & set(new)
    return len([line for line in old if line not in common]) + len(
        [line for line in new if line not in common]
    )
