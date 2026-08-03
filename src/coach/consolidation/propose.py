"""Step 2 of the nightly pass: the model, and the only thing it is asked to do.

CONS-02. :mod:`coach.consolidation.pipeline` is the whole of consolidation
except for one parameter — `propose`, a callable that turns the day's inputs into
candidate diffs. This is that callable. Everything the pipeline does with the
result is already decided elsewhere.

**What the model is asked for, and what it is not.** It reads the day and reports
what the day supports: a key, a value, a provenance, a reason, the evidence, and
how sure it is. It is not asked which value should win. CONS-03 puts that in
:mod:`coach.consolidation.conflict`, and the schema below has no field in which a
precedence claim could even be expressed — so the prompt does not have to forbid
one. That is deliberate: a rule enforced by the shape of the output cannot be
talked out of.

**The prompt lives in code, not in a file.** CHAT-02 makes the persona a file so
the coach's voice can change without a deploy. This is not voice. Changing it
changes what lands in long term memory, which is the one thing in the system that
should never move without a diff and a test. `prompts/persona.md` is read by the
conversation; nothing reads this.

**The vocabulary is read from the database.** MEM-01 keeps the key namespace in
`fact_keys` and makes adding a key a migration. Rendering that table into the
prompt each night means the model is told exactly what exists, rather than being
left to guess and have every guess rejected by :func:`pipeline.apply_diffs`. The
rejection is still the guarantee; this is what stops it firing constantly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import anthropic
import psycopg

from coach.consolidation import conflict, pipeline
from coach.llm import client as llmmod
from coach.memory import keys as keymod

log = logging.getLogger(__name__)

TOOL_NAME = "propose_diffs"

# Provenance the model may claim. Defined in `conflict`, beside the rule that
# makes it necessary, because this is not the only door a model proposes through
# — `agent.tools.propose_fact` is the other, and it was offering all four.
# Re-exported here because this module's schema is where it is applied, and
# because `propose.MODEL_PROVENANCE` is the name the tests already use.
#
# Narrowed here rather than in `pipeline.DIFF_SCHEMA` because the pipeline's
# schema describes what a diff *is*; this describes what a model may assert.
MODEL_PROVENANCE = conflict.MODEL_PROVENANCE

# How much of the day to send. A day of messages is small; the cap is a
# runaway guard for a pathological backlog, not a budget.
MAX_MESSAGES = 200
MAX_FACTS = 400


def tool_schema() -> dict[str, Any]:
    """The forced tool. Its input schema *is* CONS-02's strict JSON contract."""
    schema = json.loads(json.dumps(pipeline.DIFF_SCHEMA))  # deep copy, plain data
    schema["properties"]["diffs"]["items"]["properties"]["provenance"]["enum"] = list(
        MODEL_PROVENANCE
    )
    return {
        "name": TOOL_NAME,
        "description": (
            "Report the candidate memory diffs the day's evidence supports, plus the "
            "day summary and continuity fields. Call this exactly once."
        ),
        "input_schema": schema,
    }


INSTRUCTIONS = """\
You are the nightly consolidation pass of a cycling coach's memory. You are not \
the coach and you are not talking to the athlete. Nothing you write here is ever \
shown to them as prose except the day summary.

Your job: read one day of the athlete's messages, telemetry and queued writes, \
and report what that day's evidence supports as changes to long term memory.

## What to propose

Propose a diff when the day gives real evidence that a fact's value should be \
different, or that a fact with no value yet now has one. Look for:

* An explicit statement of a fact, or a direct correction of one.
* Behaviour that contradicts an active stated fact — three weeks of four \
sessions against a stated six training days is the archetype.
* Negation of something the coach asserted.
* A statement inconsistent with an active fact.

Do not propose a diff to restate a fact that is already active and unchanged. \
That is not a correction and it produces a night of no-op writes.

## Provenance

This is the field that matters most, because code downstream resolves conflicts \
on it and you cannot see that resolution.

* `stated` — the athlete said it, in words, on this day.
* `observed` — behaviour or telemetry shows it, whether or not anyone said it.
* `inferred` — your reading of indirect signals. Honest guesses go here.

Never label your own reasoning `observed`, and never use `stated` for something \
you concluded rather than read. Provenance is a claim about where a value came \
from, not about how sure you are — confidence is the separate field for that.

## Confidence

Between 0 and 1, and it is load bearing: below 0.30 a diff is held for the \
athlete to confirm in conversation rather than applied. Use it honestly. A \
single ambiguous remark is weak evidence and should say so; a fortnight of \
consistent behaviour is not.

## Keys

Use only the keys listed below, spelled exactly. The list is the whole \
vocabulary — a key that is not on it does not exist and a diff naming one is \
discarded. If the day's evidence does not fit any key, that evidence is not a \
fact; leave it in the day summary instead.

Value types are given per key and are not coerced. A key typed `list` needs a \
list, not a sentence describing one.

## Safety keys

Keys marked SAFETY are listed so you can read them, and you may not write them. \
A constraint is recorded only when the athlete states it and confirms it, in \
conversation. A diff naming a safety key is rejected and the attempt is logged.

## The summaries

* `day_summary` — a few sentences, factual, in the third person. What happened, \
what was said, what changed. This one is durable and a person reads it.
* `rolling_summary` — the continuity note the coach opens from next time.
* `open_threads` — short labels for things genuinely unresolved. Not topics that \
came up; things left hanging.
* `last_topic` — a short label for where the conversation ended.

## Empty is a valid answer

Most days change nothing. A day with no evidence of any fact changing should \
return an empty `diffs` list and still write the summaries. Inventing a diff to \
look useful corrupts the memory this pass exists to protect.
"""


def render_vocabulary(vocabulary: dict[str, keymod.FactKey]) -> str:
    """The key namespace as the model sees it, from `fact_keys` (MEM-01)."""
    lines = ["## Available keys", ""]
    for key in sorted(vocabulary):
        fk = vocabulary[key]
        marks = " SAFETY — you may not write this" if fk.safety else ""
        lines.append(f"* `{key}` ({fk.value_type}, category {fk.category}){marks}")
    return "\n".join(lines)


def render_inputs(inputs: pipeline.Inputs) -> str:
    """The day, as a user turn.

    Ordinary prose rather than raw JSON. The model reads a conversation better
    than it reads a dump of one, and the ids it needs for evidence refs are the
    only structure that has to survive.
    """
    parts = [f"# The day: {inputs.consolidated_on.isoformat()}", ""]

    parts.append("## Messages")
    if inputs.messages:
        for msg in inputs.messages[:MAX_MESSAGES]:
            when = msg["occurred_at"].strftime("%H:%M")
            modality = msg.get("modality") or "text"
            tag = f" ({modality})" if modality != "text" else ""
            parts.append(f"[message {msg['id']}] {when} {msg['role']}{tag}: {msg['body']}")
        if len(inputs.messages) > MAX_MESSAGES:
            parts.append(f"...and {len(inputs.messages) - MAX_MESSAGES} more, omitted.")
    else:
        parts.append("None.")

    parts += ["", "## Telemetry and observations"]
    if inputs.telemetry:
        parts += [f"[note {row['id']}] {row['body']}" for row in inputs.telemetry]
    else:
        parts.append("None recorded for this day.")

    parts += ["", "## Writes queued in conversation, awaiting ratification"]
    if inputs.pending:
        for row in inputs.pending:
            proposal = row.get("proposal") or {}
            parts.append(
                f"[pending {row['id']}] {proposal.get('key')} = "
                f"{json.dumps(proposal.get('value'), default=str)} "
                f"(provenance {proposal.get('provenance')}, origin {row.get('origin')}): "
                f"{proposal.get('reason', '')}"
            )
    else:
        parts.append("None.")

    parts += ["", "## Active facts"]
    if inputs.active_facts:
        for fact in inputs.active_facts[:MAX_FACTS]:
            parts.append(
                f"[fact {fact.id}] {fact.key} = {json.dumps(fact.value, default=str)} "
                f"(provenance {fact.provenance}, confidence {fact.confidence})"
            )
    else:
        parts.append("None yet.")

    parts += [
        "",
        "Propose the diffs this day supports, and write the summaries. "
        f"Call {TOOL_NAME} exactly once.",
    ]
    return "\n".join(parts)


def _extract(completion: llmmod.Completion) -> dict[str, Any]:
    """The tool input, or a MalformedProposal naming what came back instead.

    Raising `pipeline.MalformedProposal` rather than a local error is what wires
    this into CONS-02's retry: the pipeline catches that type, calls the proposer
    a second time, and fails the run without partial writes if it happens again.
    """
    for use in completion.tool_uses:
        if use.name == TOOL_NAME:
            return dict(use.input)

    called = [u.name for u in completion.tool_uses]
    raise pipeline.MalformedProposal(
        f"the model did not call {TOOL_NAME} (stop_reason {completion.stop_reason!r}, "
        f"tools called {called}, {len(completion.text)} chars of text)"
    )


def build(
    client: anthropic.Anthropic, conn: psycopg.Connection
) -> Callable[[pipeline.Inputs], dict[str, Any]]:
    """Bind a proposer to a client and a connection.

    A factory because `pipeline.run` takes `Callable[[Inputs], Any]` and holds no
    model — the separation that makes every test in `test_consolidation.py` run
    without a network. This is the one place it is closed.

    The vocabulary is read once per call rather than once per process: a
    migration that adds a key should take effect on the next night, not the next
    restart.
    """

    def propose(inputs: pipeline.Inputs) -> dict[str, Any]:
        system = [
            # The instructions and the vocabulary are the stable prefix — they
            # change on a deploy or a migration, not nightly — so they carry the
            # cache breakpoint and the day itself falls after it.
            {
                "type": "text",
                "text": INSTRUCTIONS + "\n\n" + render_vocabulary(keymod.load_all(conn)),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        completion = llmmod.complete(
            client,
            "consolidation",
            system,
            [{"role": "user", "content": render_inputs(inputs)}],
            tools=[tool_schema()],
            conn=conn,
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        proposal = _extract(completion)
        log.info(
            "proposed %d diffs for %s",
            len(proposal.get("diffs", [])),
            inputs.consolidated_on,
        )
        return proposal

    return propose
