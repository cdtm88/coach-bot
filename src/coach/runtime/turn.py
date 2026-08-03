"""One inbound message to one sent reply.

This is the loop P01 described and nothing ran. It decides nothing itself: every
rule it applies already has a home, and the order below is the whole of its
contribution.

    1. Claim an interruption, or none.        agent.interruptions  (CHAT-11)
    2. Assemble the prompt within budget.     agent.prompt         (MEM-10/11/13, SAFE-01)
    3. Check the spend cap before dispatch.   runtime.models       (OBS-07, partial)
    4. Call the model.                        llm.client           (MODEL-01/03, OBS-01)
    5. Run any tools it asked for, and go     agent.tools          (CHAT-06)
       back to 4 until it stops asking.
    6. Check the reply against the            agent.naturalness    (CHAT-03/04/10,
       behavioural rules. Retry once.                               SAFE-05, HLTH-09)
    7. Record it.                             telegram.bot

**Step 6 retries rather than rewrites.** A reply that narrates a memory write or
asks two questions is regenerated with the violation named, once. If the second
attempt also fails it is sent anyway and the violation is logged — because
CHAT-03 and CHAT-04 are about what the coach says, and a coach that says nothing
because it could not phrase itself is a worse failure than a clumsy sentence.
VOICE-03 makes the same choice explicitly for transcription: never a silent drop.

**The tool loop is bounded.** A model that keeps asking for tools would otherwise
turn one message into an unbounded number of billed calls. The ceiling is low
because the tool surface is small; hitting it means something is wrong, and the
turn ends with what it has rather than continuing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import anthropic
import psycopg

from coach import clock, config
from coach.agent import interruptions, naturalness, prompt, tools, trust
from coach.health import bodymass
from coach.llm import client as llmmod
from coach.runtime import models
from coach.telegram import bot as botmod

log = logging.getLogger(__name__)

# Step 5's ceiling. Eight tools exist and a turn that genuinely needs more than a
# handful of calls has misunderstood the question.
MAX_TOOL_ROUNDS = 5

# What the athlete is told when OBS-07's cap has tripped. The requirement is
# explicit that the coach says it is capped rather than going silent.
CAPPED_REPLY = (
    "I have hit today's spending limit, so I am going to be quiet until it resets. "
    "Nothing is broken and nothing is lost, and I will pick this up tomorrow."
)

# TRUST-07, under enforcement only. A reply that still quotes a figure nothing
# supports after a retry is discarded, and this goes instead. It is a fallback
# and not a silence: OBS-07's reasoning applies here too, and a coach that says
# nothing is a worse failure than one that says it cannot answer yet.
UNGROUNDED_REPLY = (
    "I do not have a number I trust for that yet, so I would rather not guess at one. "
    "Ask me again once the next ride has landed and I will have something real to work "
    "from."
)


@dataclass
class Turn:
    """What one turn did. Every field is something a test can assert on."""

    reply: str = ""
    interruption: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    retried: bool = False
    capped: bool = False
    context_tokens: int = 0
    # TRUST-03. Physiological figures the reply stated that nothing in the turn
    # accounts for. Populated in shadow mode as well as under enforcement, which
    # is the whole point of shadow mode.
    unattributed: list[str] = field(default_factory=list)
    trust_enforced: bool = False
    # OBS-12. Minted here because this function is the boundary of an exchange:
    # every model call it causes, including the tool rounds and the naturalness
    # retry, belongs to the one message the athlete sent.
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def candidates(conn: psycopg.Connection, today: date) -> list[interruptions.Candidate]:
    """Everything eligible to be the one thing the coach raises (CHAT-11).

    Collected from the modules that own each kind rather than decided here.
    Body mass offers the outlier confirmation and the gap mention; the
    verification candidate and the pending mention are consolidation's, and are
    read off the facts they belong to.
    """
    offered = list(bodymass.candidates(conn, today))

    with conn.cursor() as cur:
        cur.execute(
            "select key from facts where status = 'active' and mention_pending "
            "and (mention_expires is null or mention_expires > now()) limit 1"
        )
        row = cur.fetchone()
    if row:
        offered.append(interruptions.Candidate("pending_mention", row["key"]))

    return offered


def respond(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    now: datetime,
    is_catch_up: bool = False,
    tz: Any = None,
    on_text: Callable[[str], None] | None = None,
) -> Turn:
    """Produce the coach's reply to a backlog of athlete messages.

    `messages` is what `coach.telegram.bot.drain` hands over: one message
    ordinarily, several after an outage. CHAT-08 makes the whole backlog one
    reply, which is why this takes a list and not a string.
    """
    zone = tz or clock.configured_tz()
    turn = Turn()

    claimed = interruptions.claim(conn, candidates(conn, clock.local_day(now, zone)), now)
    turn.interruption = claimed.kind if claimed else None

    assembled = prompt.assemble(conn, now, claimed_interruption=claimed, tz=zone)
    turn.context_tokens = assembled.tokens
    system = prompt.as_system_blocks(assembled)

    history = _history(conn, messages, is_catch_up)

    # TRUST-02. Snapshotted before the loop, so a retry cannot poison either
    # channel and so a tool result arriving later cannot be mistaken for
    # something the athlete said. Only string content is read: after a tool
    # round the history gains `role: user` entries carrying tool *results*, and
    # counting those as self-reported would be the laundering path with extra
    # steps.
    attribution = trust.Attribution()
    for block in system:
        attribution.add_text(block.get("text", ""))
    for message in history:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            attribution.add_self_reported(message["content"])

    try:
        models.check_spend(conn)
    except models.SpendCapReached as exc:
        # OBS-07: say it, do not go quiet. Recorded as a coach message like any
        # other so the next turn's continuity note knows what happened.
        log.warning("daily spend cap reached: %s", exc)
        turn.capped = True
        turn.reply = CAPPED_REPLY
        return turn

    turn.reply = _converse(conn, client, system, history, turn, on_text, attribution)

    _check_trust(conn, client, system, history, turn, on_text, attribution)

    turn.violations = naturalness.violations(turn.reply)
    if turn.violations:
        log.warning("reply violated %s; retrying once", turn.violations)
        turn.retried = True
        history.append({"role": "assistant", "content": turn.reply})
        history.append({"role": "user", "content": _correction(turn.violations)})
        retried = _converse(conn, client, system, history, turn, on_text)
        remaining = naturalness.violations(retried)
        if len(remaining) <= len(turn.violations):
            turn.reply, turn.violations = retried, remaining
        if turn.violations:
            # Sent anyway. A coach that says nothing because it could not phrase
            # itself is a worse failure than a clumsy sentence.
            log.error("reply still violates %s after a retry; sending it", turn.violations)

    return turn


def _check_trust(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    system: list[dict[str, Any]],
    history: list[dict[str, Any]],
    turn: Turn,
    on_text: Callable[[str], None] | None,
    attribution: trust.Attribution,
) -> None:
    """TRUST-03 and TRUST-07: did the reply state a figure nothing supports?

    **Shadow by default.** The scanner records and blocks nothing until
    `COACH_TRUST_ENFORCE` is set. A regex tuned against invented examples will
    have false positives that a corpus built from real transcripts has not seen
    yet, and a false positive under enforcement costs the athlete a legitimate
    answer with no way for him to know why. Shadow mode is how the corpus gets
    the evidence to earn enforcement, and `coach-transcript` is how it is read.

    **Under enforcement it retries once, then substitutes.** This is the one
    place the turn deliberately diverges from the naturalness posture above,
    which sends a clumsy sentence rather than nothing. A fabricated FTP is not a
    clumsy sentence: he would ride it. So the second failure discards the reply
    and sends :data:`UNGROUNDED_REPLY`, which is a fallback rather than a
    silence.
    """
    loose = trust.unattributed(turn.reply, attribution)
    if not loose:
        return

    turn.unattributed = [c.text for c in loose]
    enforcing = config.trust_enforced()
    turn.trust_enforced = enforcing

    if not enforcing:
        # Recorded, not blocked. The OBS-10 payload for this turn holds the
        # prompt and the tool results, so a hit here is a complete case for the
        # corpus without anyone reproducing anything.
        log.warning("TRUST (shadow): reply quoted %s with nothing behind it", turn.unattributed)
        return

    log.warning("TRUST: reply quoted %s with nothing behind it; retrying once", turn.unattributed)
    history.append({"role": "assistant", "content": turn.reply})
    history.append({"role": "user", "content": _trust_correction(loose)})
    retried = _converse(conn, client, system, history, turn, on_text, attribution)

    still = trust.unattributed(retried, attribution)
    if not still:
        turn.reply, turn.unattributed = retried, []
        return

    log.error(
        "TRUST: reply still quoted %s after a retry; substituting the fallback",
        [c.text for c in still],
    )
    turn.reply = UNGROUNDED_REPLY
    turn.unattributed = [c.text for c in still]


def _trust_correction(loose: list[trust.Claim]) -> str:
    """Names the figures rather than the fix.

    And names the way out. `docs/prior-art.md` section 1: a bare prohibition
    with no alternative is what pushes a model into inventing something, so this
    says what to do instead of quoting a number it does not have.
    """
    quoted = ", ".join(repr(c.text) for c in loose)
    return (
        f"That reply stated {quoted}, and nothing you were given this turn contains "
        "those figures. Do not repeat them. Say the same thing without the numbers, "
        "or call a tool that would give you real ones, or say plainly that you do not "
        "have that figure yet. Do not apologise for the correction or refer to it."
    )


def _converse(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    system: list[dict[str, Any]],
    history: list[dict[str, Any]],
    turn: Turn,
    on_text: Callable[[str], None] | None,
    attribution: trust.Attribution | None = None,
) -> str:
    """Call the model, run whatever tools it asks for, and return the text."""
    for _ in range(MAX_TOOL_ROUNDS):
        completion = llmmod.complete(
            client,
            "chat",
            system,
            history,
            tools=tools.SCHEMAS,
            conn=conn,
            on_text=on_text,
            turn_id=turn.turn_id,
        )
        if not completion.tool_uses:
            return completion.text

        history.append({"role": "assistant", "content": _assistant_blocks(completion)})
        results = []
        for use in completion.tool_uses:
            turn.tool_calls.append(use.name)
            payload = _dispatch(conn, use)
            # TRUST-02: a figure a tool actually returned is one the coach may
            # quote. Accumulated as the round trips happen rather than gathered
            # afterwards, because a later round's result must ground a figure
            # the reply states, and an earlier round's must too.
            if attribution is not None:
                attribution.add_tool_result(payload)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": payload,
                }
            )
        history.append({"role": "user", "content": results})

    log.error("tool loop hit %d rounds; ending the turn with what it has", MAX_TOOL_ROUNDS)
    return ""


def _dispatch(conn: psycopg.Connection, use: Any) -> str:
    """Run one tool call. A failing tool is a result, not an exception.

    Raising here would lose the whole turn to one bad argument. Telling the model
    the call failed lets it say so, or try something else, which is what a
    conversation does.
    """
    import json

    try:
        return json.dumps(tools.dispatch(conn, use.name, dict(use.input)), default=str)
    except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
        log.warning("tool %s failed: %s", use.name, exc)
        return json.dumps({"error": str(exc)})


def _assistant_blocks(completion: llmmod.Completion) -> list[dict[str, Any]]:
    """The assistant turn as blocks, so the tool results have something to answer.

    Text is included when there is any: a model that narrates before calling a
    tool has said something the next round should still see.
    """
    blocks: list[dict[str, Any]] = []
    if completion.text:
        blocks.append({"type": "text", "text": completion.text})
    for use in completion.tool_uses:
        blocks.append(
            {"type": "tool_use", "id": use.id, "name": use.name, "input": dict(use.input)}
        )
    return blocks


def _history(
    conn: psycopg.Connection, messages: list[dict[str, Any]], is_catch_up: bool
) -> list[dict[str, Any]]:
    """The conversation so far, with the new backlog at the end.

    Prior turns come from `messages`, which P01 persists for exactly this. The
    catch-up note is a system-shaped instruction inside the user turn rather than
    another system block, because the system prefix is what prompt caching keys
    on and this changes per turn.
    """
    with conn.cursor() as cur:
        # Answered athlete messages and every coach reply. The `or role = coach`
        # is load bearing: replies are written with `processed_at` null — nothing
        # processes them — so filtering on that alone gave the model a history of
        # its own questions with none of its own answers.
        cur.execute(
            "select role, body from messages "
            "where processed_at is not null or role = 'coach' "
            "order by occurred_at desc, id desc limit 20"
        )
        prior = list(reversed(cur.fetchall()))

    history: list[dict[str, Any]] = [
        {"role": "assistant" if row["role"] == "coach" else "user", "content": row["body"]}
        for row in prior
    ]

    body = "\n".join(m["body"] for m in messages)
    if is_catch_up:
        # CHAT-08: one catch-up response, not one per queued message.
        body = (
            "These arrived while you were offline. Answer them together, as one "
            "reply, without acknowledging the gap:\n\n" + body
        )
    history.append({"role": "user", "content": body})
    return history


def _correction(violations: list[str]) -> str:
    """The one retry's instruction. Names the rule rather than the fix.

    Telling the model what to write would put this module in the business of
    phrasing the coach's replies, which is the persona's job.
    """
    return (
        "That reply broke a rule you must follow: "
        + "; ".join(violations)
        + ". Say the same thing again without breaking it. Do not apologise for the "
        "correction or refer to it."
    )


def handle(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    send: Callable[[str], None],
    now: datetime,
    tz: Any = None,
) -> Turn | None:
    """Drain the backlog, answer it once, send the answer.

    Returns None when there was nothing to answer. The send happens inside
    `bot.drain`'s callback so the message is marked processed and recorded in the
    same pass that sent it — CHAT-08's "processed once" is a property of that
    ordering, not of this function remembering.
    """
    result: dict[str, Turn] = {}

    def respond_to(messages: list[dict[str, Any]], is_catch_up: bool) -> str:
        turn = respond(conn, client, messages, now, is_catch_up, tz)
        result["turn"] = turn
        if turn.reply:
            send(turn.reply)
        return turn.reply

    botmod.drain(conn, respond_to, now)
    return result.get("turn")
