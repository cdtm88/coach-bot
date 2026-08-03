"""CONS-02's proposer: the one place a model touches long term memory.

`test_consolidation.py` already proves what the pipeline does with a proposal.
This suite is about the half that was missing — how a proposal is asked for, and
what happens when the model answers badly. Everything here runs against a real
Postgres and a fake model.

The distinction the whole file turns on: the proposer decides *what is asked and
what is accepted as an answer*. It decides nothing about what lands. Several
tests below exist to keep it that way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.consolidation import conflict, pipeline, propose
from coach.memory import facts, keys, state
from coach.runtime import scheduler

DAY = date(2026, 7, 20)
NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
DUBAI = ZoneInfo("Asia/Dubai")


# --- fakes -------------------------------------------------------------------


@dataclass
class FakeUse:
    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeCall:
    """One scripted model answer: the tool calls it makes, and any prose."""

    tool_uses: list[FakeUse] = field(default_factory=list)
    text: str = ""


class FakeModel:
    """Replaces `coach.llm.client.complete` and records how it was called.

    The recording is the point in several tests below: what the proposer *asks*
    for is as much a requirement as what it does with the answer.
    """

    def __init__(self, *answers: FakeCall):
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        client,
        purpose,
        system,
        messages,
        tools=None,
        conn=None,
        on_text=None,
        tool_choice=None,
    ):
        from coach.llm.client import Completion

        self.calls.append(
            {
                "purpose": purpose,
                "system": system,
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "conn": conn,
            }
        )
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return Completion(
            text=answer.text,
            model="claude-opus-5",
            purpose=purpose,
            stop_reason="tool_use" if answer.tool_uses else "end_turn",
            tool_uses=list(answer.tool_uses),
            input_tokens=1000,
            output_tokens=200,
        )


def proposal(diffs: list[dict] | None = None, **over: Any) -> dict[str, Any]:
    body = {
        "diffs": diffs if diffs is not None else [],
        "day_summary": "A quiet day.",
        "rolling_summary": "Nothing outstanding.",
        "open_threads": [],
        "last_topic": "the week ahead",
    }
    body.update(over)
    return body


def answering(*bodies: dict[str, Any]) -> FakeModel:
    """A model that calls the tool correctly, with each body in turn."""
    return FakeModel(*[FakeCall([FakeUse(propose.TOOL_NAME, b)]) for b in bodies])


def use(monkeypatch: pytest.MonkeyPatch, model: FakeModel) -> FakeModel:
    monkeypatch.setattr("coach.consolidation.propose.llmmod.complete", model)
    return model


def _message(conn: psycopg.Connection, body: str, at: datetime = NOW) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into messages (chat_id, role, body, occurred_at) values (1, 'athlete', %s, %s)",
            (body, at),
        )


# --- what the proposer asks for ----------------------------------------------


def test_the_tool_is_forced(conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONS-02: strict JSON is a property of the call, not a hope about the prompt.

    Asking nicely for JSON and parsing what comes back is the version of this
    that fails at 3am. The tool is named in `tool_choice`, so the only shape the
    model can answer in is the schema.
    """
    model = use(monkeypatch, answering(proposal()))
    _message(conn, "Felt good today.")

    propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    call = model.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": propose.TOOL_NAME}
    assert [t["name"] for t in call["tools"]] == [propose.TOOL_NAME]


def test_it_routes_as_consolidation(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MODEL-01: the heavier model handles consolidation, and the router decides.

    Asserted on the purpose rather than the model name: which model serves
    `consolidation` is MODEL-02's environment variable, and pinning the name here
    would make this test fail on a legitimate config change.
    """
    model = use(monkeypatch, answering(proposal()))
    _message(conn, "Rode.")

    propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    assert model.calls[0]["purpose"] == "consolidation"


def test_the_connection_is_passed_so_the_call_is_billed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-01: every model call is costed, including the ones nobody sees.

    The nightly pass is the easiest call in the system to forget to account for,
    because no one is waiting on it — and `llm.client._record` writes the row only
    when it is given a connection. So the requirement here is "hand over the
    connection"; that `complete` then writes the row is `test_context.py`'s.

    Asserted this way and not by counting `model_calls` rows because the fake
    *replaces* `complete`, which is where the accounting lives. A test that
    stubbed out the recorder and then checked for its output would be asserting
    on its own stub.
    """
    model = use(monkeypatch, answering(proposal()))
    _message(conn, "Rode.")

    propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    assert model.calls[0]["conn"] is conn


def test_the_vocabulary_comes_from_the_database(conn: psycopg.Connection) -> None:
    """MEM-01: the model is told which keys exist, rather than left to guess.

    Without this every invented key costs a rejected diff and a warning line. The
    rejection in `apply_diffs` is still the guarantee — this is what stops it
    being the normal case.
    """
    rendered = propose.render_vocabulary(keys.load_all(conn))

    assert "availability.weekday_minutes" in rendered
    assert "`goal.target_weight_kg`" in rendered
    # The value type has to travel with the key: MEM-14 does not coerce, so a
    # model that guesses text for a number key has produced a rejected diff.
    assert "(number," in rendered


def test_safety_keys_are_shown_as_forbidden(conn: psycopg.Connection) -> None:
    """SAFE-02, in the prompt as well as in the matrix.

    Consolidation may not write a safety key and `conflict.resolve` enforces
    that. Hiding the keys entirely would be worse than naming them: the model
    needs to know a constraint exists to reason about the day around it, and
    telling it plainly that it cannot write one is cheaper than a rejected diff.
    """
    rendered = propose.render_vocabulary(keys.load_all(conn))
    safety = [k for k, v in keys.load_all(conn).items() if v.safety]

    assert safety, "the fixture has no safety keys, so this proves nothing"
    for key in safety:
        assert f"`{key}`" in rendered
    assert "SAFETY" in rendered


def test_the_day_reaches_the_model(conn: psycopg.Connection) -> None:
    """CONS-01's four inputs all render, with the ids evidence refs need."""
    _message(conn, "Knee is still sore on the stairs.")
    facts.ratify(conn, "availability.days", ["mon", "wed"], "stated", reason="seed")
    state.queue_write(
        conn,
        {
            "key": "prefs.session_types_disliked",
            "value": ["long intervals"],
            "provenance": "stated",
            "reason": "said so in passing",
        },
        origin="in_turn",
    )

    rendered = propose.render_inputs(pipeline.gather(conn, DAY, timedelta(0)))

    assert "Knee is still sore on the stairs." in rendered
    assert "[message " in rendered
    assert "prefs.session_types_disliked" in rendered
    assert "availability.days" in rendered
    assert "[fact " in rendered


def test_an_empty_day_says_so_rather_than_omitting_the_section(conn: psycopg.Connection) -> None:
    """A missing heading reads as a truncated prompt; "None." reads as a fact.

    The model has to be able to tell "no telemetry arrived" from "telemetry was
    not included", because the first is evidence about the day and the second is
    a bug it should not try to work around.
    """
    rendered = propose.render_inputs(pipeline.gather(conn, DAY, timedelta(0)))

    assert "## Telemetry and observations\nNone recorded for this day." in rendered
    assert "## Active facts\nNone yet." in rendered


def test_the_instructions_are_cached_and_the_day_is_not(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stable prefix is the instructions and the vocabulary; the day is not.

    Same reasoning as `prompt.as_system_blocks`: the breakpoint goes on what
    changes on a deploy, not on what changes nightly. Putting the day in the
    system block would invalidate the cache every night and buy nothing.
    """
    model = use(monkeypatch, answering(proposal()))
    _message(conn, "Rode.")

    propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    system = model.calls[0]["system"]
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Rode." not in system[0]["text"]
    assert "Rode." in model.calls[0]["messages"][0]["content"]


# --- what it accepts as an answer --------------------------------------------


def test_a_tool_call_becomes_the_proposal(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = proposal(
        [
            {
                "key": "availability.weekday_minutes",
                "value": 45,
                "provenance": "observed",
                "reason": "four weeknight sessions averaged 44 minutes",
                "evidence": {"sessions": [1, 2, 3, 4]},
                "confidence": 0.8,
            }
        ]
    )
    use(monkeypatch, answering(body))
    _message(conn, "Managed 45 minutes again.")

    result = propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    assert result == body


def test_prose_instead_of_a_tool_call_is_malformed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode that matters, and it must raise the type CONS-02 retries.

    A model that explains its diffs in prose rather than calling the tool has
    produced nothing usable. Raising `MalformedProposal` is what puts it on the
    pipeline's retry path instead of crashing the run on an AttributeError three
    frames later.
    """
    use(monkeypatch, FakeModel(FakeCall(text="I think availability should change.")))
    _message(conn, "Rode.")

    with pytest.raises(pipeline.MalformedProposal, match="did not call"):
        propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))


def test_the_wrong_tool_is_malformed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named, not positional: `tool_uses[0]` would have accepted anything."""
    use(monkeypatch, FakeModel(FakeCall([FakeUse("get_context", {"scope": "all"})])))
    _message(conn, "Rode.")

    with pytest.raises(pipeline.MalformedProposal):
        propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))


def test_the_error_names_what_came_back(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run logs the error, and "malformed" alone is not diagnosable.

    OBS-04 wants last night answerable. The stop reason and the tools actually
    called are the difference between knowing the model refused and guessing.
    """
    use(monkeypatch, FakeModel(FakeCall([FakeUse("search_memory", {})], text="Hmm.")))
    _message(conn, "Rode.")

    with pytest.raises(pipeline.MalformedProposal) as caught:
        propose.build(object(), conn)(pipeline.gather(conn, DAY, timedelta(0)))

    assert "search_memory" in str(caught.value)
    assert "tool_use" in str(caught.value)


# --- the schema is the contract ----------------------------------------------


def test_the_model_may_not_claim_computed_provenance() -> None:
    """MEM-08, and the hole it would open in `conflict.resolve`.

    `conflict.MEASURED` counts `computed` as measured, so a model that labelled
    its own arithmetic `computed` would get an inference to supersede a real
    measurement and to beat a genuine `inferred` value. MEM-08 reserves computed
    figures for SQL. The narrowing is in the enum rather than the prose because
    a schema cannot be argued with.
    """
    enum = tool_provenance_enum()

    assert "computed" not in enum
    assert set(enum) == set(propose.MODEL_PROVENANCE)
    # The point of excluding it, stated as the test's own premise.
    assert "computed" in conflict.MEASURED


def tool_provenance_enum() -> list[str]:
    schema = propose.tool_schema()["input_schema"]
    return schema["properties"]["diffs"]["items"]["properties"]["provenance"]["enum"]


def test_no_model_facing_schema_anywhere_offers_computed() -> None:
    """The same rule, asserted across every door rather than at the one we knew.

    The narrowing above was stated only in `propose`, and the in-turn tool
    surface was left offering all four of MEM-04's values — a second way for a
    model to propose a fact, with the guard applied to one of them. Nothing
    carried that value through to a `facts` row, because an in-turn proposal is
    briefing material for the nightly proposer and the proposer re-emits under
    the narrow enum. It was still a schema describing a system that does not
    exist, and the next door would have had it to copy.

    Walking every schema rather than naming the two, so a ninth tool with a
    provenance field is covered on the day it is written.
    """
    from coach.agent import tools as toolmod

    def provenance_enums(node: object) -> list[list[str]]:
        found: list[list[str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "provenance" and isinstance(value, dict) and "enum" in value:
                    found.append(value["enum"])
                found.extend(provenance_enums(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(provenance_enums(item))
        return found

    enums = provenance_enums(toolmod.SCHEMAS) + provenance_enums(propose.tool_schema())

    assert enums, "no provenance enum found; this test has stopped testing anything"
    for enum in enums:
        assert "computed" not in enum
        assert set(enum) == set(conflict.MODEL_PROVENANCE)


def test_narrowing_the_enum_does_not_mutate_the_pipeline_schema() -> None:
    """`DIFF_SCHEMA` is module state, and editing it in place would leak.

    The pipeline's schema describes what a diff is; the proposer's describes what
    a model may assert. Without the copy, importing the proposer would silently
    narrow the pipeline for every other caller — and the test that catches it is
    cheaper than the night that would.
    """
    propose.tool_schema()

    assert pipeline.DIFF_SCHEMA["properties"]["diffs"]["items"]["properties"]["provenance"][
        "enum"
    ] == ["stated", "observed", "computed", "inferred"]


def test_the_schema_has_nowhere_to_put_a_precedence_claim() -> None:
    """CONS-03, enforced by shape rather than by instruction.

    `test_consolidation.py` proves a precedence claim has no effect if one
    arrives. This is the belt: the tool's schema forbids extra properties, so a
    model that tries to assert one gets a validation error at the API rather than
    a field that is quietly ignored.
    """
    items = propose.tool_schema()["input_schema"]["properties"]["diffs"]["items"]

    assert items["additionalProperties"] is False
    assert not {"precedence", "supersedes", "wins"} & set(items["properties"])


def test_the_instructions_never_ask_the_model_to_resolve_a_conflict() -> None:
    """The prompt and the code have to agree about whose job precedence is.

    A prompt that says "decide which value wins" would be inviting output the
    schema cannot carry and the matrix would discard — which is how a model ends
    up spending its reasoning on a question nobody reads the answer to.
    """
    text = propose.INSTRUCTIONS.lower()
    asking = ("which wins", "which value wins", "supersede", "precedence", "resolve the conflict")

    for phrase in asking:
        assert phrase not in text, f"the instructions ask for a precedence decision: {phrase!r}"


def test_the_instructions_say_empty_is_valid() -> None:
    """Most nights change nothing, and a model that feels obliged to produce a
    diff will invent one. That is the failure that corrupts memory quietly."""
    assert "empty" in propose.INSTRUCTIONS.lower()


# --- end to end through the pipeline -----------------------------------------


def test_a_proposed_diff_lands_through_the_real_pipeline(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam this whole change exists to close, exercised once end to end."""
    use(
        monkeypatch,
        answering(
            proposal(
                [
                    {
                        "key": "availability.weekday_minutes",
                        "value": 45,
                        "provenance": "observed",
                        "reason": "four weeknights at about 45 minutes",
                        "evidence": {"sessions": [1, 2]},
                        "confidence": 0.9,
                    }
                ],
                day_summary="Rode four weeknights, all short.",
            )
        ),
    )
    _message(conn, "Only had 45 minutes again.")

    result = pipeline.run(conn, DAY, propose.build(object(), conn))

    assert result.applied == 1
    landed = facts.active_for(conn, "availability.weekday_minutes")
    assert landed is not None and landed.value == 45
    assert landed.provenance == "observed"


def test_a_malformed_answer_is_retried_once_by_the_pipeline(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONS-02's retry, with the real proposer rather than a stub raising by hand.

    The first answer is prose, the second is a proper call. The run succeeds and
    the model was asked twice.
    """
    model = use(
        monkeypatch,
        FakeModel(
            FakeCall(text="Nothing structured here."),
            FakeCall([FakeUse(propose.TOOL_NAME, proposal(day_summary="Second time lucky."))]),
        ),
    )
    _message(conn, "Rode.")

    result = pipeline.run(conn, DAY, propose.build(object(), conn))

    assert len(model.calls) == 2
    assert result.applied == 0
    assert run_status(conn, DAY) == "succeeded"


def test_two_malformed_answers_fail_the_run_without_partial_writes(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONS-02: then logged as a failed run without partial writes."""
    use(monkeypatch, FakeModel(FakeCall(text="No.")))
    _message(conn, "Rode.")

    with pytest.raises(pipeline.MalformedProposal):
        pipeline.run(conn, DAY, propose.build(object(), conn))

    assert run_status(conn, DAY) == "failed"
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from notes where kind = 'day_summary'")
        assert cur.fetchone()["n"] == 0


def run_status(conn: psycopg.Connection, on: date) -> str:
    with conn.cursor() as cur:
        cur.execute("select status from consolidation_runs where consolidated_on = %s", (on,))
        return cur.fetchone()["status"]


def test_a_safety_diff_is_still_refused_with_a_real_proposer(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAFE-02. The prompt asks it not to; the matrix is why it cannot.

    Worth asserting through the proposer and not only through `apply_diffs`: the
    prompt is the only part of this path that a model can ignore, and the test
    that matters is the one where it does.
    """
    use(
        monkeypatch,
        answering(
            proposal(
                [
                    {
                        # A real safety key, so the refusal comes from the matrix
                        # rather than from the vocabulary check. An invented key
                        # is rejected as unknown, which proves something else.
                        "key": "constraint.movement_restrictions",
                        "value": [],
                        "provenance": "observed",
                        "reason": "deadlifted without complaint",
                        "evidence": {},
                        "confidence": 1.0,
                    }
                ]
            )
        ),
    )
    _message(conn, "Deadlifted fine today.")

    result = pipeline.run(conn, DAY, propose.build(object(), conn))

    assert result.applied == 0
    assert result.rejected == 1
    assert any("safety key" in r for r in result.reasons)


# --- the scheduler's side of the wiring --------------------------------------


def test_the_scheduler_windows_the_local_day(conn: psycopg.Connection) -> None:
    """TZ-01, and the bug the default offset would have shipped.

    `pipeline.gather` windows on `local midnight - tz_offset`, and its default of
    zero windows on a UTC day. In Asia/Dubai that is four hours out: a message
    sent at 01:00 local on the 20th is 21:00 UTC on the 19th, so a UTC window for
    the 20th misses it entirely and the athlete's late night is consolidated into
    a day that had already been closed.
    """
    at_1am_local = datetime(2026, 7, 20, 1, 0, tzinfo=DUBAI)
    _message(conn, "Can't sleep. Knee aching.", at=at_1am_local)

    seen: dict[str, Any] = {}

    def capture(inputs: pipeline.Inputs) -> dict[str, Any]:
        seen["bodies"] = [m["body"] for m in inputs.messages]
        return proposal()

    scheduler.consolidation_job(capture, DUBAI)(conn, DAY)

    assert seen["bodies"] == ["Can't sleep. Knee aching."]


def test_the_utc_default_would_have_missed_it(conn: psycopg.Connection) -> None:
    """The same day, gathered without the offset. This is the control.

    Kept because the test above passes for two different reasons — a correct
    offset, or a window so wide it catches everything — and only one of them is
    the fix.
    """
    _message(conn, "Can't sleep.", at=datetime(2026, 7, 20, 1, 0, tzinfo=DUBAI))

    assert pipeline.gather(conn, DAY, timedelta(0)).messages == []


def test_the_offset_is_the_consolidated_days_not_todays(conn: psycopg.Connection) -> None:
    """A zone with DST: the window must match the day it claims.

    Dubai has none, so this uses a zone that does. Consolidating a February day
    in July must use February's offset — reading `utcoffset()` off `now()` would
    silently shift every winter night by an hour once the clocks changed.
    """
    london = ZoneInfo("Europe/London")
    winter = date(2026, 2, 10)
    _message(conn, "Winter ride.", at=datetime(2026, 2, 10, 0, 30, tzinfo=london))

    seen: dict[str, Any] = {}

    def capture(inputs: pipeline.Inputs) -> dict[str, Any]:
        seen["bodies"] = [m["body"] for m in inputs.messages]
        return proposal()

    scheduler.consolidation_job(capture, london)(conn, winter)

    assert seen["bodies"] == ["Winter ride."]


def test_decay_after_consolidation_is_idempotent(conn: psycopg.Connection) -> None:
    """The scheduler runs both, and that has to be harmless.

    Consolidation's step 9 decays, and `decay_job` decays again in the same
    night. `apply_decay` recomputes from the curve rather than stepping down from
    the stored value, so the second pass changes nothing — which is what lets the
    standalone job cover the silent days the pipeline returns early on.
    """
    fact = facts.ratify(conn, "availability.days", ["mon"], "stated", reason="seed")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now() - interval '90 days' where id = %s",
            (fact.id,),
        )

    first = facts.apply_decay(conn)
    after_first = facts.active_for(conn, "availability.days")
    second = facts.apply_decay(conn)
    after_second = facts.active_for(conn, "availability.days")

    assert first == 1
    assert second == 0
    assert after_first is not None and after_second is not None
    assert after_first.confidence == after_second.confidence
    # CONS-07's own acceptance value: 30 day half life, unconfirmed for 90 days.
    assert after_second.confidence == Decimal("0.30")


def test_consolidation_runs_before_decay_and_the_export() -> None:
    """Order is the schedule, and `run_due` iterates insertion order.

    Decay should run against what the night just wrote, and the export should
    describe the state both left behind. Asserted on the dict because that dict
    *is* the ordering — there is no other declaration of it.
    """
    import inspect

    source = inspect.getsource(scheduler.main)
    body = source[source.index("jobs: dict") :]
    positions = [body.index(f'"{name}"') for name in ("consolidation", "decay", "export")]

    assert positions == sorted(positions)


def test_the_scheduler_no_longer_warns_that_nothing_is_wired() -> None:
    """The warning was honest when it was true. Leaving it would not be."""
    import inspect

    assert "no proposer is wired" not in inspect.getsource(scheduler)


# --- a shape check on the evidence field -------------------------------------


def test_evidence_survives_into_the_fact(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONS-02 asks for evidence refs, and they are only worth asking for if kept.

    `ratify` takes evidence and the proposer passes it through untouched. Without
    this the reason string would be the only trace of why a fact changed, and a
    reason is prose while evidence is a pointer.
    """
    use(
        monkeypatch,
        answering(
            proposal(
                [
                    {
                        "key": "physiology.ftp_watts",
                        "value": 210,
                        "provenance": "observed",
                        "reason": "ramp test",
                        "evidence": {"sessions": [77]},
                        "confidence": 0.95,
                    }
                ]
            )
        ),
    )
    _message(conn, "Did the ramp test.")

    pipeline.run(conn, DAY, propose.build(object(), conn))

    with conn.cursor() as cur:
        cur.execute(
            "select evidence from fact_events where fact_id = "
            "(select id from facts where key = 'physiology.ftp_watts' and status = 'active')"
            " order by id limit 1"
        )
        row = cur.fetchone()
    assert row is not None
    assert json.dumps(row["evidence"]) is not None and row["evidence"] == {"sessions": [77]}


def test_the_instructions_refuse_the_coachs_claims_about_the_system() -> None:
    """The loop that ran for a day on the live deployment.

    The coach told the athlete the calendar and activity feeds had never
    returned successfully; all five had answered inside the hour. Consolidation
    read the conversation, wrote it into the rolling summary and two open
    threads, and the next turn read it back as established and said it again.

    Nothing in the instructions distinguished a claim about the athlete from a
    claim about the system, and only one of those is what this pass is for.
    """
    text = propose.INSTRUCTIONS.lower()

    assert "claims about the system are not evidence" in text
    assert "open thread" in text
    assert "the athlete saying his sync is broken is different" in text


def test_the_instructions_refuse_to_record_a_retraction() -> None:
    """A month of correctly ingested rides was marked unverified on the strength
    of the coach apologising for a fabrication that had not happened."""
    assert "retractions get the same treatment" in propose.INSTRUCTIONS.lower()
