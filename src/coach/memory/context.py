"""Per turn context assembly.

MEM-10: standing memory loads in full every turn; only episodic notes are
retrieved on demand. MEM-11: the assembled context stays under 4,000 tokens
excluding conversation history, counting preloaded content plus any tool results
returned in the same turn. MEM-13: when the budget would be exceeded, content
sheds in a fixed order and constraints are never touched.

The token counter is injectable. P00 ships a deterministic estimator so the
budget is testable without a network call; P01 passes the real one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from coach.config import CONTEXT_TOKEN_BUDGET

# MEM-13 shedding order: the first entry sheds first. Constraints and active
# facts are absent by design, because they are never shed.
SHED_ORDER = ("episodic_recall", "block_detail", "continuity_note")

# Never shed, never summarised (SAFE-01, MEM-13).
PROTECTED = frozenset({"constraints", "facts"})


def estimate_tokens(text: str) -> int:
    """A rough, deterministic stand-in for a real tokeniser.

    Four characters per token is close enough for budget accounting and, more
    importantly, it never varies between runs, so the MEM-11 assertion is a test
    rather than a flake.
    """
    return (len(text) + 3) // 4


@dataclass
class Component:
    name: str
    body: str
    tokens: int


@dataclass
class AssembledContext:
    components: list[Component]
    shed: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(c.tokens for c in self.components)

    def render(self) -> str:
        return "\n\n".join(c.body for c in self.components if c.body)

    def names(self) -> list[str]:
        return [c.name for c in self.components]


class BudgetExceeded(RuntimeError):
    """Everything sheddable was shed and the context is still over budget."""


def assemble(
    parts: dict[str, str],
    budget: int = CONTEXT_TOKEN_BUDGET,
    counter: Callable[[str], int] = estimate_tokens,
) -> AssembledContext:
    """Build the turn's context, shedding in the MEM-13 order if it overflows.

    ``parts`` maps component name to rendered body. Names in :data:`SHED_ORDER`
    are droppable; names in :data:`PROTECTED` are not. Anything else is kept but
    counted.

    Raises :class:`BudgetExceeded` if the protected content alone exceeds the
    budget, which is a real failure worth surfacing rather than silently
    truncating a safety constraint.
    """
    components = [Component(name, body, counter(body)) for name, body in parts.items()]
    result = AssembledContext(components=components)

    for name in SHED_ORDER:
        if result.tokens <= budget:
            break
        present = [c for c in result.components if c.name == name]
        if not present:
            continue
        result.components = [c for c in result.components if c.name != name]
        result.shed.append(name)

    if result.tokens > budget:
        protected = sum(c.tokens for c in result.components if c.name in PROTECTED)
        raise BudgetExceeded(
            f"context is {result.tokens} tokens against a budget of {budget} after shedding "
            f"{result.shed or 'nothing'}; {protected} of that is protected content that "
            "MEM-13 forbids dropping or summarising"
        )

    return result
