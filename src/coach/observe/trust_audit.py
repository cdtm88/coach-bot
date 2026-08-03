"""TRUST-07's gate: replay the scanner over recorded turns. `coach-trust-audit`.

Enforcement is off until the scanner shows zero false positives on real
transcripts, and the corpus behind it is thirty-odd cases somebody invented.
A hand-written corpus proves the scanner does what its author meant. It cannot
say whether the coach's actual voice trips it, and that is the only question
enforcement turns on: a false positive under enforcement costs the athlete a
legitimate answer and gives him no way to know why.

**This needs no new traffic.** OBS-10 records the system blocks, the message
array and the response for every call, and OBS-12's `turn_id` groups the calls
of one exchange. So the evidence for the gate is already on disk, back to the
retention window, and a hit is reproducible without anyone re-running a
conversation. `runtime.turn._check_trust` says as much in its own docstring;
this is the command that collects on it.

**It replays, it does not re-derive.** The attribution set is built by
`trust.attribution_for`, the same function the live turn calls, and the verdict
comes from `trust.unattributed`, the same function that would block the reply.
A separate implementation here would report a false positive rate for a scanner
that is not the one running.

**It cannot tell you whether a hit is a false positive.** That is a judgement
about whether the number was really available, and it needs a person who knows
what the coach should have said. What this does is put the claim, the sentence
it appeared in and the nearest grounded values side by side, so the judgement
takes seconds rather than a database session.

    coach-trust-audit                      every chat turn on record
    coach-trust-audit --last 200           the most recent 200
    coach-trust-audit --on 2026-08-03      one day
    coach-trust-audit --quiet              the rate only, no cases
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from coach import db
from coach.agent import trust
from coach.observe import transcript

# Enough of the sentence around a claim to judge it, without printing the whole
# reply for every hit. `--full` prints the reply.
CONTEXT_CHARS = 140

# How many grounded values to offer beside a flagged claim. The point is to show
# a near miss where there is one -- "said 250, the tools had 248" is a different
# defect from "said 250, nothing near it exists" -- not to dump the set.
NEAREST = 3


@dataclass
class Hit:
    """One claim the scanner would not let through, with what it needs judging."""

    turn_id: str
    at: Any
    claim: str
    value: float
    context: str
    nearest: list[float]
    reply: str

    def describe(self, full: bool = False) -> str:
        near = ", ".join(f"{v:g}" for v in self.nearest) or "nothing"
        lines = [
            f"  {self.at}  turn {self.turn_id}",
            f"    claim:   {self.claim!r}",
            f"    nearest: {near}",
        ]
        lines.append(f"    reply:   {self.reply}" if full else f"    context: ...{self.context}...")
        return "\n".join(lines)


@dataclass
class Report:
    turns: int = 0
    flagged: int = 0
    unreadable: int = 0
    hits: list[Hit] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return (self.flagged / self.turns) if self.turns else 0.0


def _exchanges(calls: list[transcript.Call]) -> list[list[transcript.Call]]:
    """Group calls into turns, preserving order.

    Keyed the way `transcript.fetch` keys its window, so a call with no turn id
    is its own exchange rather than being lumped with its neighbours.
    """
    grouped: dict[str, list[transcript.Call]] = {}
    for call in calls:
        grouped.setdefault(call.turn_id or f"call:{call.call_id}", []).append(call)
    return list(grouped.values())


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    """Every tool result payload in a message array, in order.

    The live turn adds these one round at a time as they return. The last call
    of an exchange carries all of them, so reading that one is equivalent and
    does not depend on reconstructing which round produced what.
    """
    payloads: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                payloads.append(str(block.get("content", "")))
    return payloads


def _nearest(value: float, attribution: trust.Attribution) -> list[float]:
    pool = sorted({*attribution.grounded, *attribution.self_reported})
    return sorted(pool, key=lambda v: abs(v - value))[:NEAREST]


def _context(reply: str, claim: str) -> str:
    at = reply.find(claim)
    if at < 0:  # pragma: no cover - the claim came from this reply
        return reply[:CONTEXT_CHARS]
    start = max(0, at - CONTEXT_CHARS // 2)
    return reply[start : at + len(claim) + CONTEXT_CHARS // 2].replace("\n", " ")


def audit(calls: list[transcript.Call]) -> Report:
    """Run the scanner over recorded exchanges as though each were live.

    The first call of an exchange carries the history the turn opened with,
    which is what the live path snapshots; later calls carry corrections and
    tool results appended since, and reading those as the athlete's own words
    is exactly what `attribution_for` refuses to do.
    """
    report = Report()

    for exchange in _exchanges(calls):
        usable = [c for c in exchange if not c.missing_payload]
        if not usable:
            report.unreadable += 1
            continue

        first, last = usable[0], usable[-1]
        reply = (last.response or {}).get("text") or ""
        if not reply.strip():
            # No prose to check: the exchange ended in a tool call, or the
            # payload holds a call that never answered.
            continue

        report.turns += 1

        attribution = trust.attribution_for(first.system, first.messages)
        for payload in _tool_results(last.messages):
            attribution.add_tool_result(payload)

        loose = trust.unattributed(reply, attribution)
        if not loose:
            continue

        report.flagged += 1
        for claim in loose:
            report.hits.append(
                Hit(
                    turn_id=last.turn_id or f"call:{last.call_id}",
                    at=last.created_at,
                    claim=claim.text,
                    value=claim.value,
                    context=_context(reply, claim.text),
                    nearest=_nearest(claim.value, attribution),
                    reply=reply,
                )
            )

    return report


def render(report: Report, quiet: bool = False, full: bool = False) -> str:
    if not report.turns and not report.unreadable:
        return (
            "No turns on record to audit. The ledger fills from the next "
            "conversation onward; nothing before OBS-10 shipped can be replayed."
        )

    out = [
        f"{report.turns} turn(s) replayed, {report.flagged} flagged "
        f"({report.rate:.1%}), {sum(1 for _ in report.hits)} claim(s) in total."
    ]
    if report.unreadable:
        out.append(f"{report.unreadable} exchange(s) had no recorded payload and were skipped.")

    if not report.flagged:
        out.append("")
        out.append(
            "Nothing was flagged. On a corpus this size that is evidence the scanner "
            "does not fire on the coach's ordinary voice, which is what TRUST-07 asks "
            "for before enforcement. It is not evidence that it catches a fabrication; "
            "tests/fixtures/trust_corpus.py is the other half."
        )
        return "\n".join(out)

    if quiet:
        return "\n".join(out)

    out.append("")
    out.append(
        "Each case below is a claim the scanner would have blocked. Judge it: is the "
        "figure one the coach was really given (a false positive, and enforcement "
        "would have cost a good answer), or one it produced (a true positive)?"
    )
    out.append("")
    for hit in report.hits:
        out.append(hit.describe(full=full))
        out.append("")
    return "\n".join(out).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coach-trust-audit",
        description=(
            "Replay the TRUST-03 scanner over recorded turns (OBS-10) to find out "
            "whether it fires on the coach's real voice. Reads only; changes nothing."
        ),
    )
    parser.add_argument("--last", type=int, help="the most recent N exchanges")
    parser.add_argument("--on", type=date.fromisoformat, help="everything on a date, UTC")
    parser.add_argument(
        "--purpose",
        default="chat",
        help="which calls to audit; defaults to chat, the only path the scanner guards",
    )
    parser.add_argument("--quiet", action="store_true", help="the rate only, without the cases")
    parser.add_argument("--full", action="store_true", help="print whole replies, not a window")
    args = parser.parse_args(argv)

    with db.connect() as conn:
        calls = transcript.fetch(conn, last=args.last, on=args.on, purpose=args.purpose)

    print(render(audit(calls), quiet=args.quiet, full=args.full))
    return 0


def audit_connection(conn: psycopg.Connection, **filters: Any) -> Report:
    """The whole pass against an open connection. For tests and for a REPL."""
    return audit(transcript.fetch(conn, **filters))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
