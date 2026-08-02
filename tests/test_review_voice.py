"""The Sunday review as a message rather than as a record.

The review's figures are tested in `test_review.py` and are not retested here.
What is tested here is everything between a correct figure and a message worth
reading: that the athlete is not shown the coach's own instructions, that a week
with nothing in it costs him two lines rather than six, and that voicing the
review can improve the wording without being able to change a number.

The last of those is the one with teeth. `weekly.build` computes in SQL so the
model cannot invent a figure (MEM-08), and putting a model back on the output
path would hand that property straight back if the guard were not real.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import conftest
from coach.blocks import document as blockmod
from coach.health import bodymass
from coach.health import trend as trendmod
from coach.review import voice, weekly
from test_review import prescribe, rollup

SUNDAY = date(2026, 8, 2)


class FakeModel:
    """Replaces `coach.llm.client.complete`, and records what it was asked."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def __call__(self, client, purpose, system, messages, conn=None, **kw):
        from coach.llm.client import Completion

        self.calls.append(
            {"purpose": purpose, "system": system, "messages": list(messages), "conn": conn}
        )
        return Completion(
            text=self.text,
            model="claude-opus-5",
            purpose=purpose,
            stop_reason="end_turn",
            input_tokens=2000,
            output_tokens=300,
        )


class ExplodingModel:
    def __call__(self, *a, **kw):
        raise RuntimeError("the API is down")


def session(
    conn: psycopg.Connection,
    on: date,
    duration_s: int | None = None,
    distance_m: int | None = None,
    unreadable: bool = False,
) -> None:
    """One completed activity on a day, however it got there."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into sessions (source, discipline, started_at, local_date, "
            "duration_s, distance_m, data_unavailable) "
            "values ('intervals', 'ride', %s, %s, %s, %s, %s)",
            (
                datetime.combine(on, datetime.min.time()).replace(hour=7, tzinfo=UTC),
                on,
                duration_s,
                distance_m,
                unreadable,
            ),
        )


def prescribe_ahead(
    conn: psycopg.Connection,
    on: date,
    duration_s: int,
    intensity_factor: float,
    purpose: str | None = None,
) -> None:
    """A planned session, with enough spec for `load.of_spec` to weigh it."""
    spec: dict[str, Any] = {"duration_s": duration_s, "intensity_factor": intensity_factor}
    if purpose:
        spec["purpose"] = purpose
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into prescriptions (block_id, planned_for, discipline, spec, status) "
            "values (%s, %s, 'ride', %s, 'planned')",
            (
                conftest.ensure_block(conn),
                datetime.combine(on, datetime.min.time()).replace(hour=18, tzinfo=UTC),
                Jsonb(spec),
            ),
        )


def weigh_in(conn: psycopg.Connection) -> None:
    """Four readings across the week, through the path that records them.

    `bodymass.record` rather than an insert, because HLTH-11 can hold a reading
    out of the fit and a test that wrote rows directly would be asserting
    against a trend the real path would not have produced.
    """
    for offset, kg in ((10, "128.4"), (7, "128.9"), (3, "129.0"), (0, "129.1")):
        bodymass.record(conn, SUNDAY - timedelta(days=offset), Decimal(kg))


# --- the leak: prompt instructions in the athlete's message ------------------


def test_the_weight_section_does_not_address_the_coach(conn: psycopg.Connection) -> None:
    """The bug this file exists for.

    `trend.render` is a permission block written to the model — "You may report
    this figure if asked for it", "Do not call a plateau" — and the review used
    to post it verbatim. The athlete read his coach reciting his coach's own
    instructions.
    """
    weigh_in(conn)

    body = weekly.weight_section(conn, SUNDAY).body

    assert "129.1 kg" in body
    for instruction in (
        "You may",
        "you may",
        "Do not",
        "Never comment",
        "if asked",
        "BODY MASS",
    ):
        assert instruction not in body


def test_no_section_addresses_the_coach(conn: psycopg.Connection) -> None:
    """The same check across the whole message, since any section could regress."""
    weigh_in(conn)
    prescribe(conn, SUNDAY - timedelta(days=2), "completed")
    rollup(conn, SUNDAY, load_7d=Decimal(420))

    said = weekly.build(conn, SUNDAY).message()

    for instruction in ("You may", "you may", "Do not ", "Never ", "Say that", "Quote the"):
        assert instruction not in said


def test_the_weight_line_still_obeys_the_claim_ladder(conn: psycopg.Connection) -> None:
    """Rewording it must not loosen it: four readings do not earn a rate.

    Two renderers off one fit is the drift risk the split introduces, so this
    asserts the new one against the same gate the old one used rather than
    against a hard-coded count.
    """
    weigh_in(conn)
    fit = trendmod.fit(conn, SUNDAY)
    claims = trendmod.Claims.of(fit)
    assert not claims.may_quote_rate  # the premise of the test

    said = trendmod.describe(fit, claims)

    assert "kg per week" not in said
    assert "too early to put a rate on it" in said


def test_a_silent_scale_says_so_rather_than_saying_nothing(conn: psycopg.Connection) -> None:
    """HLTH-15 from the athlete's side: silence reads as a stable weight."""
    fit = trendmod.fit(conn, SUNDAY)

    assert "no trend to read yet" in trendmod.describe(fit, trendmod.Claims.of(fit))


# --- structure: the wall of text --------------------------------------------


def test_a_quiet_week_is_one_line_not_six(conn: psycopg.Connection) -> None:
    """Nothing prescribed, no load, no readings, nothing logged, no block.

    Six labelled sections each reporting an absence is what the first review
    actually looked like. It is accurate and it is unreadable.
    """
    said = weekly.build(conn, SUNDAY).message()

    for title in ("Effort:", "Adherence:", "Load:", "Weight:", "Recovery:", "Intake:"):
        assert title not in said


def test_a_few_empty_sections_are_named(conn: psycopg.Connection) -> None:
    """Up to the enumeration cap, saying which is more useful than not."""
    session(conn, SUNDAY - timedelta(days=2), duration_s=3600, distance_m=30000)
    prescribe(conn, SUNDAY - timedelta(days=2), "completed")
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    weigh_in(conn)

    said = weekly.build(conn, SUNDAY).message()

    assert "Nothing on recovery scoring and logged intake this week." in said


def test_the_record_keeps_every_section_even_when_the_message_drops_it(
    conn: psycopg.Connection,
) -> None:
    """REV-02 is about what the review knows, not about what it says.

    "No intake was logged in the week of 2 August" is a fact about the week that
    something later may need. The absence of a line is not.
    """
    review = weekly.build(conn, SUNDAY)

    record = review.render()
    for title in weekly.SECTIONS:
        assert f"{title}:" in record


def test_a_section_with_something_in_it_is_kept(conn: psycopg.Connection) -> None:
    prescribe(conn, SUNDAY - timedelta(days=4), "completed")
    prescribe(conn, SUNDAY - timedelta(days=2), "missed")
    rollup(conn, SUNDAY, load_7d=Decimal(420))

    said = weekly.build(conn, SUNDAY).message()

    assert "Adherence: 1 of 2" in said
    assert "Load: 420 over the week" in said
    assert "Nothing recorded this week." in said  # and the rest collapsed


def test_the_sections_are_separated_so_they_can_be_read_on_a_phone(
    conn: psycopg.Connection,
) -> None:
    prescribe(conn, SUNDAY - timedelta(days=4), "completed")
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    weigh_in(conn)

    said = weekly.build(conn, SUNDAY).message()

    assert "\n\nLoad:" in said
    assert "\n\nWeight:" in said


def test_the_date_is_said_the_way_a_person_says_it(conn: psycopg.Connection) -> None:
    said = weekly.build(conn, SUNDAY).message()

    assert said.startswith("Week ending Sunday 2 August")
    assert "2026-08-02" not in said


def test_the_goals_are_recorded_and_never_recited(conn: psycopg.Connection) -> None:
    """He set them, they have not moved, and he does not need them read back.

    They stay in the record, and therefore in the facts the voicing call is
    given, because the coach has to know what the week was for before he can say
    why it mattered. Knowing and reciting are different things.
    """
    block_id = conftest.ensure_block(conn)
    blockmod.rewrite(
        conn,
        block_id,
        "goals",
        "Under 100 kg, and weight that stays off.\n\n"
        "The block's own job is narrower: build an aerobic base that does not "
        "exist yet.",
        reason="test",
    )
    blockmod.activate(conn, block_id)

    review = weekly.build(conn, SUNDAY)

    assert "Under 100 kg" in review.render()
    assert "The block's own job" in review.render()
    assert "Under 100 kg" not in review.message()
    assert "Goals:" not in review.message()


def test_an_absent_goals_section_is_not_reported_as_a_quiet_week(
    conn: psycopg.Connection,
) -> None:
    """Record-only is a third state, not a quiet one.

    "Nothing on stated goals this week" would be the review complaining to the
    athlete about its own configuration.
    """
    said = weekly.build(conn, SUNDAY).message()

    assert "goals" not in said.lower()


# --- the effort, which is the week he actually had --------------------------


def test_the_effort_section_counts_what_he_actually_did(conn: psycopg.Connection) -> None:
    """Load is training stress. "440 over the week" is not a week anyone had."""
    session(conn, SUNDAY - timedelta(days=4), duration_s=3600, distance_m=32000)
    session(conn, SUNDAY - timedelta(days=2), duration_s=5400, distance_m=48000)

    body = weekly.effort_section(conn, SUNDAY).body

    assert "2 sessions" in body
    assert "2h 30m" in body
    assert "80 km" in body


def test_effort_counts_a_session_nobody_prescribed(conn: psycopg.Connection) -> None:
    """This is the week he had, not the week he was given.

    A ride nobody asked for still cost him the time, and a review that counts
    only what it prescribed is reading its own homework.
    """
    session(conn, SUNDAY - timedelta(days=3), duration_s=3600, distance_m=30000)

    assert "1 session" in weekly.effort_section(conn, SUNDAY).body


def test_an_unreadable_file_is_counted_and_flagged(conn: psycopg.Connection) -> None:
    """FIT-15: he trained and the file did not survive.

    A zero here would be a lie about the athlete rather than about the data.
    """
    session(conn, SUNDAY - timedelta(days=2), duration_s=3600, distance_m=30000)
    session(conn, SUNDAY - timedelta(days=1), duration_s=None, unreadable=True)

    body = weekly.effort_section(conn, SUNDAY).body

    assert "2 sessions" in body
    assert "no usable file" in body


def test_the_effort_section_leads_the_message(conn: psycopg.Connection) -> None:
    """What he did comes before the plan's opinion of what he did."""
    session(conn, SUNDAY - timedelta(days=2), duration_s=3600, distance_m=30000)
    prescribe(conn, SUNDAY - timedelta(days=2), "completed")

    said = weekly.build(conn, SUNDAY).message()

    assert said.index("Effort:") < said.index("Adherence:")


# --- next week, and the session that carries it -----------------------------


def test_the_key_session_is_the_heaviest_one_prescribed(conn: psycopg.Connection) -> None:
    """A rule, not a judgement.

    `blocks.load.of_spec` is the same function BLOCK-07's ramp limit is enforced
    with, so the session named is the one the week is built around. A model
    asked to pick would pick differently on identical weeks.
    """
    prescribe_ahead(conn, SUNDAY + timedelta(days=1), duration_s=2700, intensity_factor=0.62)
    prescribe_ahead(
        conn,
        SUNDAY + timedelta(days=3),
        duration_s=5400,
        intensity_factor=0.85,
        purpose="threshold work",
    )
    prescribe_ahead(conn, SUNDAY + timedelta(days=5), duration_s=3600, intensity_factor=0.65)

    body = weekly.ahead_section(conn, SUNDAY).body

    assert "3 sessions prescribed" in body
    assert "Wednesday" in body
    assert "threshold work" in body


def test_the_week_ahead_stops_at_seven_days(conn: psycopg.Connection) -> None:
    """Next week, not the rest of the block."""
    prescribe_ahead(conn, SUNDAY + timedelta(days=3), duration_s=3600, intensity_factor=0.65)
    prescribe_ahead(conn, SUNDAY + timedelta(days=9), duration_s=7200, intensity_factor=0.9)

    assert "1 session prescribed" in weekly.ahead_section(conn, SUNDAY).body


def test_this_week_is_not_mistaken_for_next_week(conn: psycopg.Connection) -> None:
    """The boundary is the Sunday itself, which belongs to the week under review."""
    prescribe(conn, SUNDAY, "planned")

    assert "nothing prescribed for next week" in weekly.ahead_section(conn, SUNDAY).body


def test_an_empty_week_ahead_is_said_rather_than_buried(conn: psycopg.Connection) -> None:
    """The one absence that is not a quiet week.

    Nothing prescribed for next week means nobody has written it yet, and
    folding that into the "nothing happened" line is how he finds out on
    Tuesday.
    """
    said = weekly.build(conn, SUNDAY).message()

    assert "Ahead: nothing prescribed for next week yet." in said


def test_a_completely_empty_week_does_not_enumerate_its_own_absences(
    conn: psycopg.Connection,
) -> None:
    """Five things that did not happen is longer than "nothing happened"."""
    said = weekly.build(conn, SUNDAY).message()

    assert "Nothing recorded this week." in said


def test_the_message_asks_exactly_one_question(conn: psycopg.Connection) -> None:
    """REV-03 survives the restructure."""
    prescribe(conn, SUNDAY - timedelta(days=2), "missed")

    assert weekly.build(conn, SUNDAY).message().count("?") == 1


def test_decisions_keep_their_heading_in_the_message(conn: psycopg.Connection) -> None:
    """REV-04: what is waiting on the athlete stays marked as his to decide."""
    review = weekly.Review(
        week_ending=SUNDAY,
        sections=[weekly.Section("Load", "420 over the week.")],
        question=weekly.QUESTION,
        decisions=["shorten Thursday to 45 minutes"],
    )

    said = review.message()

    assert "Waiting on you:" in said
    assert "- shorten Thursday to 45 minutes" in said


# --- voicing ----------------------------------------------------------------


def test_the_review_is_voiced_through_the_model_when_there_is_one(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV-01, said rather than assembled."""
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    model = FakeModel("Quiet week. 420 of load and not much else.\n\nWhat is coming up?")
    monkeypatch.setattr("coach.review.voice.llmmod.complete", model)
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append, client=object())

    assert sent == ["Quiet week. 420 of load and not much else.\n\nWhat is coming up?"]
    assert model.calls[0]["purpose"] == "review"


def test_voicing_is_given_the_persona(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHAT-02: it is the same coach, so it is the same system prompt."""
    model = FakeModel("Nothing to report this week. What is coming up?")
    monkeypatch.setattr("coach.review.voice.llmmod.complete", model)

    weekly.run(conn, SUNDAY, send=[].append, client=object())

    system = "\n".join(b["text"] for b in model.calls[0]["system"])
    assert "cycling and strength coach" in system


def test_voicing_is_told_not_to_repeat_its_own_instructions(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The facts handed over are an internal briefing, and say so.

    The record still contains lines addressed to the coach, because the sections
    it is built from are shared with the prompt. Voicing has to know that.
    """
    model = FakeModel("Nothing to report. What is coming up?")
    monkeypatch.setattr("coach.review.voice.llmmod.complete", model)

    weekly.run(conn, SUNDAY, send=[].append, client=object())

    task = model.calls[0]["messages"][0]["content"]
    assert "written to you rather than to him" in task


def test_the_model_call_is_recorded(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MODEL-01 and OBS-01: the connection is passed so the row can be written."""
    model = FakeModel("Nothing to report. What is coming up?")
    monkeypatch.setattr("coach.review.voice.llmmod.complete", model)

    weekly.run(conn, SUNDAY, send=[].append, client=object())

    assert model.calls[0]["conn"] is conn


def test_the_review_purpose_is_accepted_by_the_ledger(conn: psycopg.Connection) -> None:
    """The check constraint on `model_calls.purpose` knows about it (016)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("insert into model_calls (purpose, model) values ('review', 'claude-opus-5')")
        cur.execute("select count(*)::int as n from model_calls where purpose = 'review'")
        assert cur.fetchone()["n"] == 1


# --- voicing may not change a number ----------------------------------------


def test_a_voiced_review_that_invents_a_number_is_discarded(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEM-08 is the reason the review is assembled in SQL at all.

    A model allowed to reword the output is allowed to reword it into a figure
    nobody can reproduce, unless something checks. This is that something.
    """
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    monkeypatch.setattr(
        "coach.review.voice.llmmod.complete",
        FakeModel("Solid week, 615 of load. What is coming up?"),
    )
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append, client=object())

    assert "615" not in sent[0]
    assert "420 over the week" in sent[0]


def test_rounding_a_figure_is_not_inventing_one(conn: psycopg.Connection) -> None:
    """ "129 kg" against a fact sheet holding "129.1 kg" is the model rounding."""
    facts = "Weight: 129.1 kg on 2026-08-02, from 11 reading(s) over 11 days."

    assert voice.check("He is 129 kg. What is coming up?", facts) is not None
    assert voice.check("He is 131 kg. What is coming up?", facts) is None


def test_small_whole_numbers_in_ordinary_prose_are_allowed() -> None:
    """ "give it 2 more weeks" is English, not a claim."""
    facts = "Load: 420 over the week."

    assert voice.check("Give it 2 more weeks. 420 of load. What next?", facts) is not None


def test_a_small_decimal_is_still_a_claim() -> None:
    """The hole the free-number bar had, and the one that mattered most.

    A decimal is never incidental in a coaching message: it is a rate, a weight,
    a percentage, an intensity factor. "0.35 kg per week" sits under any sane
    small-number bar and is exactly the figure HLTH-08 withholds until six
    readings across three weeks have earned it. Having the trend module refuse a
    rate and then letting the voicing step invent one would be worse than never
    having gated it at all.
    """
    facts = (
        "Weight: 128.9 kg on 2 August, from 11 readings over 10 days. "
        "The trend is falling, though it is too early to put a rate on it."
    )

    assert voice.check("Falling at 0.35 kg per week. What next?", facts) is None
    assert voice.check("128.9 kg and falling. What next?", facts) is not None


def test_a_voiced_review_that_asks_a_second_question_is_discarded(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV-03: one question is a property of the assembly, and voicing is the
    only step that could take it away."""
    monkeypatch.setattr(
        "coach.review.voice.llmmod.complete",
        FakeModel("How did the week feel? And what is coming up?"),
    )
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append, client=object())

    assert sent[0].count("?") == 1
    assert weekly.QUESTION in sent[0]


def test_an_empty_reply_is_discarded(conn: psycopg.Connection) -> None:
    assert voice.check("   ", "Load: 420 over the week.") is None


def test_a_reply_that_runs_to_an_essay_is_discarded() -> None:
    long = "The week went well and here is why. " * 200
    assert voice.check(long + " What next?", "") is None


# --- falling back is the normal case ----------------------------------------


def test_no_client_means_the_assembled_message(conn: psycopg.Connection) -> None:
    """Which is how the review is tested, and how it runs without an API key."""
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append)

    assert sent[0] == weekly.build(conn, SUNDAY).message()


def test_a_failed_model_call_still_posts_the_review(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voicing improves a working message. It may not cost him the review."""
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    monkeypatch.setattr("coach.review.voice.llmmod.complete", ExplodingModel())
    sent: list[str] = []

    weekly.run(conn, SUNDAY, send=sent.append, client=object())

    assert len(sent) == 1
    assert "420 over the week" in sent[0]


def test_the_record_is_the_assembled_one_whatever_was_said(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV-05 does not depend on the voice.

    What he was sent on a Sunday is recoverable from the chat. What was true on
    that Sunday has to be recoverable from the note.
    """
    rollup(conn, SUNDAY, load_7d=Decimal(420))
    monkeypatch.setattr(
        "coach.review.voice.llmmod.complete",
        FakeModel("Quiet one. What is coming up?"),
    )

    weekly.run(conn, SUNDAY, send=[].append, client=object())

    with conn.cursor() as cur:
        cur.execute("select body from notes where kind = 'review'")
        body = cur.fetchone()["body"]
    for title in weekly.SECTIONS:
        assert f"{title}:" in body


@pytest.fixture(autouse=True)
def _clean(conn: psycopg.Connection) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from breaks")
