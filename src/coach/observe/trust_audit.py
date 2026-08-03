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

# Below this many replayed turns, a clean run is reported as thin rather than as
# an answer. Roughly a week of conversation for one athlete. This is a judgement
# about a result not being a fluke and not a statistical claim, and it is named
# here rather than left to the reader because a percentage over four turns reads
# exactly like a percentage over four hundred.
THIN_EVIDENCE = 30

# The migration that created `model_call_payloads`. Named so an empty table can
# be dated: a ledger with nothing in it because no conversation has happened
# since it shipped is the expected state on the day it ships, and a ledger with
# nothing in it after a week of talking is an outage. Those need opposite
# responses and the table looks identical in both.
PAYLOAD_MIGRATION = "018_call_ledger.sql"


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
    # When the skipped exchanges happened, and when the ledger's earliest
    # payload was written. Together these say whether a payload is missing
    # because the call predates OBS-10 or because the write failed, which are
    # a non-event and an outage and were previously indistinguishable.
    skipped_first: Any = None
    skipped_last: Any = None
    ledger_from: Any = None
    # When OBS-10's table was created, and how many calls have been made since.
    # An empty ledger with no calls behind it has recorded nothing because there
    # was nothing to record.
    ledger_live_from: Any = None
    calls_since_ledger: int = 0

    @property
    def rate(self) -> float:
        return (self.flagged / self.turns) if self.turns else 0.0

    @property
    def predate_the_ledger(self) -> bool:
        """Every skipped exchange is older than the oldest payload on record."""
        if self.ledger_from is None or self.skipped_last is None:
            return False
        return self.skipped_last < self.ledger_from


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


def ledger_starts(conn: psycopg.Connection) -> Any:
    """When the earliest recorded payload's call happened, or None if there are none.

    The one thing the audit cannot get from the calls it was handed: whether a
    missing payload is a call that predates OBS-10 or a write that failed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select min(c.created_at) as at from model_call_payloads p "
            "  join model_calls c on c.id = p.call_id"
        )
        return (cur.fetchone() or {}).get("at")


def ledger_live_from(conn: psycopg.Connection) -> Any:
    """When `model_call_payloads` was created, from the migration that made it.

    `schema_migrations.applied_at` is the only record of when this deployment
    gained the ledger, and without it an empty table cannot be told from a
    broken one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select applied_at from schema_migrations where filename = %s",
            (PAYLOAD_MIGRATION,),
        )
        row = cur.fetchone()
    return row["applied_at"] if row else None


def calls_since(conn: psycopg.Connection, moment: Any, purpose: str | None = None) -> int:
    """How many model calls have happened since the ledger existed.

    Zero means the ledger has had nothing to record, which is the expected state
    on the day it ships and is indistinguishable from an outage in the table
    itself.
    """
    if moment is None:
        return 0
    clause = " and purpose = %s" if purpose else ""
    params: list[Any] = [moment] + ([purpose] if purpose else [])
    with conn.cursor() as cur:
        cur.execute(
            f"select count(*) as n from model_calls where created_at >= %s{clause}",
            params,
        )
        return int((cur.fetchone() or {"n": 0})["n"])


def audit(calls: list[transcript.Call], ledger_from: Any = None) -> Report:
    """Run the scanner over recorded exchanges as though each were live.

    The first call of an exchange carries the history the turn opened with,
    which is what the live path snapshots; later calls carry corrections and
    tool results appended since, and reading those as the athlete's own words
    is exactly what `attribution_for` refuses to do.
    """
    report = Report(ledger_from=ledger_from)

    for exchange in _exchanges(calls):
        usable = [c for c in exchange if not c.missing_payload]
        if not usable:
            report.unreadable += 1
            at = exchange[-1].created_at
            report.skipped_first = min(filter(None, [report.skipped_first, at]), default=None)
            report.skipped_last = max(filter(None, [report.skipped_last, at]), default=None)
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


def _skipped_note(report: Report) -> str:
    """Why exchanges were skipped, in the terms that decide what to do about it.

    OBS-11 allows a cost row with no payload, so one is a non-event. All of them
    is not, and the two were previously reported with the same sentence.
    """
    span = ""
    if report.skipped_first is not None:
        span = f", from {report.skipped_first} to {report.skipped_last}"
    head = f"{report.unreadable} exchange(s) had no recorded payload and were skipped{span}."

    if report.ledger_from is None:
        # Three ways to have an empty payload table, and they need opposite
        # responses. The first version of this offered only the two that are
        # faults and told the deployment to go looking for an outage on the day
        # the ledger shipped, when the true answer was that nobody had spoken to
        # the coach since.
        if report.ledger_live_from is None:
            return (
                f"{head}\n"
                f"The payload table has no migration recorded ({PAYLOAD_MIGRATION} is "
                "absent from schema_migrations), so OBS-10 has not been applied on this "
                "deployment. Run the migrate service and try again."
            )
        if not report.calls_since_ledger:
            return (
                f"{head}\n"
                f"The ledger has been in place since {report.ledger_live_from} and no "
                "model calls have been made since. Nothing is wrong: it has had nothing "
                "to record. Every exchange above predates it and cannot be recovered. "
                "Talk to the coach and run this again."
            )
        return (
            f"{head}\n"
            f"{report.calls_since_ledger} call(s) have been made since the ledger was "
            f"created at {report.ledger_live_from}, and not one wrote a payload. Every "
            "write is failing -- `_record_payload` swallows its exception by design, so "
            "check the process log for 'could not record the payload'. Until that is "
            "fixed there is nothing for this command to audit."
        )
    if report.predate_the_ledger:
        return (
            f"{head}\n"
            f"All of them are older than the earliest payload on record ({report.ledger_from}), "
            "so they are calls made before OBS-10 shipped rather than a fault. They cannot "
            "be recovered; the ledger fills from that point onward."
        )
    return (
        f"{head}\n"
        f"The ledger has payloads from {report.ledger_from}, so these are not simply "
        "calls that predate it. A payload write is failing for some calls and not "
        "others; the process log records 'could not record the payload' for each."
    )


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
        out.append(_skipped_note(report))

    # Nothing replayed is not a pass, and must never be printed as one. This
    # said "nothing was flagged ... evidence the scanner does not fire on the
    # coach's ordinary voice" after examining zero turns, which is the shape of
    # every other defect found this week: an output that reads as a result.
    if not report.turns:
        out.append("")
        out.append(
            "Nothing was replayed, so this run says nothing about the scanner either "
            "way. It is not a pass. TRUST-07's gate needs turns with payloads behind "
            "them; fix the above and run it again."
        )
        return "\n".join(out)

    if not report.flagged:
        out.append("")
        out.append(
            f"Nothing was flagged across {report.turns} turn(s). That is evidence the "
            "scanner does not fire on the coach's ordinary voice, which is the half of "
            "TRUST-07 this command can answer. It is not evidence that it catches a "
            "fabrication; tests/fixtures/trust_corpus.py is the other half."
        )
        if report.turns < THIN_EVIDENCE:
            out.append("")
            out.append(
                f"{report.turns} turn(s) is a thin sample. {THIN_EVIDENCE} is the floor "
                "this command will call unremarkable -- roughly a week of conversation "
                "for one athlete -- and it is a judgement about not being a fluke, not a "
                "statistical claim. Under that, a clean run is worth re-running later "
                "rather than acting on."
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
        report = audit(calls, ledger_from=ledger_starts(conn))
        report.ledger_live_from = ledger_live_from(conn)
        report.calls_since_ledger = calls_since(conn, report.ledger_live_from, args.purpose)

    print(render(report, quiet=args.quiet, full=args.full))
    return 0


def audit_connection(conn: psycopg.Connection, **filters: Any) -> Report:
    """The whole pass against an open connection. For tests and for a REPL."""
    report = audit(transcript.fetch(conn, **filters), ledger_from=ledger_starts(conn))
    report.ledger_live_from = ledger_live_from(conn)
    report.calls_since_ledger = calls_since(conn, report.ledger_live_from, filters.get("purpose"))
    return report


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
