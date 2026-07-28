"""Macro ingest from MacroLog: HLTH-01, HLTH-02, HLTH-03, SEC-02.

The endpoint is exercised over a real socket rather than by calling the handler,
because the parts most likely to be wrong are the parts a direct call skips: the
header the secret arrives in, the status code a rejection returns, and the fact
that a malformed body with a good secret must not look like a bad secret.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from coach.health import macros

DUBAI = ZoneInfo("Asia/Dubai")
SECRET = "macrolog-shared-secret"


@pytest.fixture
def macro_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("MACRO_INGEST_SECRET", SECRET)
    return SECRET


def meal(external_id: str, eaten_at: str, **fields: object) -> dict[str, object]:
    return {"id": external_id, "eaten_at": eaten_at, **fields}


def rows(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from meals order by eaten_at")
        return cur.fetchall()


# --- HLTH-02: per-meal granularity ------------------------------------------


def test_a_day_with_four_meals_stores_four_rows(conn: psycopg.Connection) -> None:
    """HLTH-02's acceptance, exactly as written."""
    payload = {
        "meals": [
            meal("m1", "2026-07-28T07:30:00+04:00", name="breakfast", kcal=520, protein_g=38),
            meal("m2", "2026-07-28T12:45:00+04:00", name="lunch", kcal=740, protein_g=52),
            meal("m3", "2026-07-28T16:00:00+04:00", name="snack", kcal=210, protein_g=20),
            meal("m4", "2026-07-28T20:15:00+04:00", name="dinner", kcal=810, protein_g=61),
        ]
    }
    result = macros.apply(conn, payload, DUBAI)

    assert result.stored == 4
    stored = rows(conn)
    assert len(stored) == 4
    assert {r["local_date"].isoformat() for r in stored} == {"2026-07-28"}
    assert [float(r["protein_g"]) for r in stored] == [38, 52, 20, 61]


def test_totals_are_a_query_over_the_meals_not_a_stored_aggregate(
    conn: psycopg.Connection,
) -> None:
    """HLTH-02 and MEM-08: the day total is derivable, and derived in SQL."""
    macros.apply(
        conn,
        {
            "meals": [
                meal("m1", "2026-07-28T07:30:00+04:00", kcal=500, protein_g=40),
                meal("m2", "2026-07-28T19:30:00+04:00", kcal=700, protein_g=50),
            ]
        },
        DUBAI,
    )
    totals = macros.daily_totals(conn, "2026-07-28", "2026-07-28")
    assert len(totals) == 1
    assert totals[0]["meals"] == 2
    assert float(totals[0]["kcal"]) == 1200
    assert float(totals[0]["protein_g"]) == 90


def test_the_local_day_comes_from_the_configured_timezone(conn: psycopg.Connection) -> None:
    """TZ-01: a meal at 01:00 Dubai belongs to that Dubai day, not the UTC one."""
    macros.apply(conn, {"meals": [meal("late", "2026-07-28T01:00:00+04:00")]}, DUBAI)
    stored = rows(conn)[0]
    assert stored["eaten_at"] == datetime.fromisoformat("2026-07-28T01:00:00+04:00")
    assert stored["local_date"].isoformat() == "2026-07-28"  # 21:00 UTC on the 27th


# --- HLTH-03: idempotency and deletion --------------------------------------


def test_replaying_a_payload_creates_no_duplicate(conn: psycopg.Connection) -> None:
    """HLTH-03: idempotent on the meal id."""
    payload = {"meals": [meal("m1", "2026-07-28T12:45:00+04:00", kcal=740)]}
    first = macros.apply(conn, payload, DUBAI)
    second = macros.apply(conn, payload, DUBAI)

    assert (first.stored, first.updated) == (1, 0)
    assert (second.stored, second.updated) == (0, 1)
    assert len(rows(conn)) == 1


def test_a_corrected_meal_updates_in_place(conn: psycopg.Connection) -> None:
    """The phone edits a meal; the row changes rather than doubling."""
    macros.apply(conn, {"meals": [meal("m1", "2026-07-28T12:45:00+04:00", kcal=740)]}, DUBAI)
    macros.apply(conn, {"meals": [meal("m1", "2026-07-28T12:45:00+04:00", kcal=615)]}, DUBAI)

    stored = rows(conn)
    assert len(stored) == 1
    assert float(stored[0]["kcal"]) == 615


def test_a_deletion_in_macrolog_removes_the_row(conn: psycopg.Connection) -> None:
    """HLTH-03: a delete propagates."""
    macros.apply(
        conn,
        {
            "meals": [
                meal("m1", "2026-07-28T12:45:00+04:00", kcal=740),
                meal("m2", "2026-07-28T20:15:00+04:00", kcal=810),
            ]
        },
        DUBAI,
    )
    result = macros.apply(conn, {"deleted": ["m1"]}, DUBAI)

    assert result.deleted == 1
    assert [r["external_id"] for r in rows(conn)] == ["m2"]


def test_deleting_something_absent_is_not_an_error(conn: psycopg.Connection) -> None:
    """A phone retrying a delete after a dropped response must not fail."""
    assert macros.apply(conn, {"deleted": ["never-existed"]}, DUBAI).deleted == 0


# --- shapes and malformed input ---------------------------------------------


def test_a_bare_meal_object_is_accepted(conn: psycopg.Connection) -> None:
    """The simplest client posts one meal, not a list containing one meal."""
    result = macros.apply(conn, meal("solo", "2026-07-28T12:45:00+04:00", kcal=400), DUBAI)
    assert result.stored == 1


def test_one_bad_meal_does_not_lose_the_rest_of_the_batch(conn: psycopg.Connection) -> None:
    """A phone back from an outage sends everything it has."""
    result = macros.apply(
        conn,
        {
            "meals": [
                meal("good-1", "2026-07-28T07:30:00+04:00"),
                {"eaten_at": "2026-07-28T12:45:00+04:00"},  # no id
                meal("good-2", "2026-07-28T20:15:00+04:00"),
            ]
        },
        DUBAI,
    )
    assert result.stored == 2
    assert len(result.errors) == 1
    assert len(rows(conn)) == 2


def test_a_naive_timestamp_is_refused_rather_than_guessed(conn: psycopg.Connection) -> None:
    """The ambiguity is a whole day at the boundary, so guessing is worse."""
    result = macros.apply(conn, {"meals": [meal("m1", "2026-07-28T12:45:00")]}, DUBAI)
    assert result.stored == 0
    assert result.errors and "eaten_at" in result.errors[0]


def test_a_malformed_meals_field_is_malformed_not_rejected(conn: psycopg.Connection) -> None:
    """Distinct exceptions so the route can answer 400 rather than 401."""
    with pytest.raises(macros.Malformed):
        macros.apply(conn, {"meals": "breakfast"}, DUBAI)


def test_unknown_fields_survive_in_the_payload_column(conn: psycopg.Connection) -> None:
    """A field added on the phone is not lost waiting for a server deploy."""
    macros.apply(
        conn,
        {"meals": [meal("m1", "2026-07-28T12:45:00+04:00", kcal=400, sodium_mg=900)]},
        DUBAI,
    )
    assert rows(conn)[0]["payload"]["sodium_mg"] == 900


# --- SEC-02 and HLTH-01: the authenticated endpoint --------------------------


def test_verify_refuses_when_no_secret_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset secret refuses everything rather than accepting everything."""
    monkeypatch.delenv("MACRO_INGEST_SECRET", raising=False)
    with pytest.raises(macros.Rejected):
        macros.verify("anything")


def test_verify_rejects_a_missing_header(macro_secret: str) -> None:
    """An absent header is rejected exactly like a wrong one."""
    with pytest.raises(macros.Rejected):
        macros.verify(None)
    with pytest.raises(macros.Rejected):
        macros.verify("wrong")
    macros.verify(SECRET)  # does not raise


def test_the_endpoint_accepts_a_signed_payload(conn, endpoint, macro_secret) -> None:
    """HLTH-01 end to end, over a socket."""
    from ingest_harness import Upstream

    running = endpoint(Upstream([]))
    status, reply = running.post_macros(
        {"meals": [meal("m1", "2026-07-28T12:45:00+04:00", kcal=740, protein_g=52)]},
        secret=SECRET,
    )

    assert status == 200
    assert reply["stored"] == 1
    assert len(rows(conn)) == 1


def test_the_endpoint_rejects_an_unsigned_payload(conn, endpoint, macro_secret) -> None:
    """HLTH-01's acceptance: a payload without the shared secret is rejected."""
    from ingest_harness import Upstream

    running = endpoint(Upstream([]))
    status, reply = running.post_macros({"meals": [meal("m1", "2026-07-28T12:45:00+04:00")]})

    assert status == 401
    assert reply == {"error": "rejected"}
    assert rows(conn) == []


def test_the_endpoint_answers_400_not_401_for_a_signed_but_broken_body(
    conn, endpoint, macro_secret
) -> None:
    """A correct secret must never produce a credential error."""
    from ingest_harness import Upstream

    running = endpoint(Upstream([]))
    status, _ = running.post_macros({"meals": "breakfast"}, secret=SECRET)
    assert status == 400


def test_the_webhook_secret_does_not_open_the_macro_route(
    conn, endpoint, macro_secret, webhook_secret
) -> None:
    """Two clients, two secrets. One leaking must not open the other's door."""
    from ingest_harness import Upstream

    running = endpoint(Upstream([]))
    status, _ = running.post_macros(
        {"meals": [meal("m1", "2026-07-28T12:45:00+04:00")]}, secret=webhook_secret
    )
    assert status == 401
