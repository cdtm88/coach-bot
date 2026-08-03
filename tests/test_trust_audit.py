"""TRUST-07's gate, replayed over the OBS-10 ledger. `coach-trust-audit`.

The scanner ships in shadow and `COACH_TRUST_ENFORCE` stays off until it shows
zero false positives on real transcripts. The corpus in `tests/fixtures/` is
hand-written, which proves the scanner does what its author meant and says
nothing about whether the coach's own voice trips it.

The load-bearing test here is `test_the_replay_agrees_with_the_live_turn`. An
audit that built its attribution set differently would report a false positive
rate for a scanner that is not the one running, which is the failure this
repository has now had four of.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from coach.agent import trust  # noqa: E402
from coach.llm import client as llmmod  # noqa: E402
from coach.observe import transcript, trust_audit  # noqa: E402
from test_call_ledger import SYSTEM, FakeClient, FakeMessage  # noqa: E402

TURN = "6f1a9c4e-0d3b-4f8a-9c21-5b7e8d2f1a30"


def record(
    conn: psycopg.Connection,
    reply: str,
    messages: list[dict[str, Any]] | None = None,
    system: list[dict[str, Any]] | None = None,
    turn_id: str | None = TURN,
    purpose: str = "chat",
) -> Any:
    """One call, through the real ledger writer, so the payload is a real payload."""
    return llmmod.complete(
        FakeClient([FakeMessage(reply)]),
        purpose,
        system if system is not None else SYSTEM,
        messages if messages is not None else [{"role": "user", "content": "how did it look?"}],
        conn=conn,
        turn_id=turn_id,
    )


def audited(conn: psycopg.Connection, **filters: Any) -> trust_audit.Report:
    return trust_audit.audit_connection(conn, **filters)


# --- the replay is the live scanner, not a second one -------------------------


def test_the_replay_agrees_with_the_live_turn(conn: psycopg.Connection) -> None:
    """Both build the attribution set through `trust.attribution_for`.

    Asserted as an identity rather than by comparing two verdicts, because two
    verdicts agreeing on one example is what a drifted pair looks like right up
    until it matters.
    """
    system = [{"type": "text", "text": "His FTP is 168 W."}]
    history = [{"role": "user", "content": "my LTHR is 165"}]

    live = trust.attribution_for(system, history)
    record(conn, "Sitting at 168 W then.", messages=history, system=system)
    replayed = trust.attribution_for(
        transcript.fetch(conn)[0].system, transcript.fetch(conn)[0].messages
    )

    assert replayed.grounded == live.grounded
    assert replayed.self_reported == live.self_reported


def test_a_grounded_reply_is_not_flagged(conn: psycopg.Connection) -> None:
    record(conn, "You held 168 W, which is right on it.", system=[{"text": "His FTP is 168 W."}])

    report = audited(conn)

    assert report.turns == 1
    assert report.flagged == 0
    assert report.hits == []


def test_an_invented_figure_is_flagged(conn: psycopg.Connection) -> None:
    record(conn, "Your FTP is 250 W now.", system=[{"text": "His FTP is 168 W."}])

    report = audited(conn)

    assert report.flagged == 1
    assert [h.claim for h in report.hits] == ["250 W"]


def test_a_number_the_athlete_supplied_is_his_to_repeat(conn: psycopg.Connection) -> None:
    """TRUST-02's second channel, which pacer-ai shipped without."""
    record(
        conn,
        "165 bpm it is then.",
        messages=[{"role": "user", "content": "my LTHR is 165"}],
        system=[{"text": "No threshold on record."}],
    )

    assert audited(conn).flagged == 0


# --- what only the ledger can supply ------------------------------------------


def test_a_figure_a_tool_returned_is_grounded(conn: psycopg.Connection) -> None:
    """The tool result lives in the *next* call's messages, not the first's.

    This is the whole reason the replay reads two calls of an exchange rather
    than one, and getting it wrong would flag every figure the coach looked up.
    """
    record(conn, "", messages=[{"role": "user", "content": "what was my CTL?"}])
    record(
        conn,
        # A claim the scanner really matches. Written as "Your CTL is 42." at
        # first, which the scanner did not see at all, so the test passed
        # whether or not tool results were read. That is what surfaced the
        # sentence-final blind spot fixed in the same change.
        "Your CTL 42 is holding steady.",
        messages=[
            {"role": "user", "content": "what was my CTL?"},
            {"role": "assistant", "content": [{"type": "tool_use", "name": "get_load"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": '{"ctl": 42.0}'}
                ],
            },
        ],
    )

    report = audited(conn)

    assert report.turns == 1, "three calls sharing a turn id are one exchange"
    assert report.flagged == 0


def test_a_tool_result_is_never_read_as_something_he_said(conn: psycopg.Connection) -> None:
    """The laundering path: tool results arrive as `role: user` blocks.

    Counting them as self-reported would be harmless here and catastrophic for
    `propose_fact`, which is why `attribution_for` reads string content only.
    """
    history = [
        {"role": "user", "content": "how am I doing?"},
        {"role": "user", "content": [{"type": "tool_result", "content": '{"ctl": 42.0}'}]},
    ]

    attribution = trust.attribution_for([], history)

    assert attribution.self_reported == []


def test_a_retry_correction_is_not_read_as_something_he_said(
    conn: psycopg.Connection,
) -> None:
    """A correction is appended as `role: user` too, and it quotes the claim.

    Reading the last call's history rather than the first would let a flagged
    number ground itself through the very message that complained about it.
    """
    record(conn, "", messages=[{"role": "user", "content": "how am I doing?"}])
    record(
        conn,
        "Your FTP is 250 W.",
        messages=[
            {"role": "user", "content": "how am I doing?"},
            {"role": "assistant", "content": "Your FTP is 250 W."},
            {"role": "user", "content": "You stated 250 W and nothing supports it."},
        ],
    )

    assert audited(conn).flagged == 1


# --- the report ----------------------------------------------------------------


def test_a_near_miss_is_shown_beside_the_claim(conn: psycopg.Connection) -> None:
    """ "Said 250, the tools had 248" is a different defect from "nothing near it"."""
    record(conn, "Hold 250 W.", system=[{"text": "Recent normalised power 248 W."}])

    hit = audited(conn).hits[0]

    assert 248.0 in hit.nearest


def test_the_context_shows_the_sentence_not_the_whole_reply(conn: psycopg.Connection) -> None:
    reply = "Good session. " * 30 + "Your FTP is 250 W. " + "Rest up. " * 30
    record(conn, reply, system=[{"text": "His FTP is 168 W."}])

    hit = audited(conn).hits[0]

    assert "250 W" in hit.context
    assert len(hit.context) < len(reply)
    assert hit.reply == reply, "the full reply is kept for --full"


def test_a_clean_audit_says_what_it_does_and_does_not_prove(conn: psycopg.Connection) -> None:
    """The report must not read as "the scanner works"."""
    record(conn, "You held 168 W.", system=[{"text": "His FTP is 168 W."}])

    rendered = trust_audit.render(audited(conn))

    assert "0 flagged" in rendered.replace("1 turn(s) replayed, ", "0 turn(s) replayed, 0 flagged")
    assert "not evidence that it catches a fabrication" in rendered
    assert "trust_corpus" in rendered


def test_an_empty_ledger_says_so_rather_than_reporting_zero(conn: psycopg.Connection) -> None:
    """A 0% false positive rate over no turns is not a gate anyone should pass."""
    rendered = trust_audit.render(audited(conn))

    assert "No turns on record" in rendered
    assert "0.0%" not in rendered


def test_quiet_gives_the_rate_without_the_conversation(conn: psycopg.Connection) -> None:
    record(conn, "Your FTP is 250 W.", system=[{"text": "His FTP is 168 W."}])

    rendered = trust_audit.render(audited(conn), quiet=True)

    assert "1 flagged" in rendered
    assert "250 W" not in rendered, "quiet must not print the athlete's conversation"


def test_a_call_with_no_payload_is_counted_not_silently_dropped(
    conn: psycopg.Connection,
) -> None:
    """OBS-11 allows a cost row with no payload. An audit must not call that clean."""
    record(conn, "Your FTP is 250 W.", system=[{"text": "His FTP is 168 W."}])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from model_call_payloads")

    report = audited(conn)

    assert report.turns == 0
    assert report.unreadable == 1
    assert "no recorded payload" in trust_audit.render(report)


def test_a_tool_only_call_is_not_counted_as_a_turn(conn: psycopg.Connection) -> None:
    """An exchange that ended in a tool call has no prose to scan."""
    record(conn, "")

    report = audited(conn)

    assert report.turns == 0
    assert report.flagged == 0


def test_only_chat_is_audited_by_default(conn: psycopg.Connection) -> None:
    """The scanner guards `runtime.turn`. Consolidation is not behind it."""
    record(conn, "Your FTP is 250 W.", system=[{"text": "His FTP is 168 W."}], purpose="chat")
    record(
        conn,
        "Your FTP is 300 W.",
        system=[{"text": "His FTP is 168 W."}],
        purpose="consolidation",
        turn_id=None,
    )

    report = audited(conn, purpose="chat")

    assert report.turns == 1
    assert [h.claim for h in report.hits] == ["250 W"]
