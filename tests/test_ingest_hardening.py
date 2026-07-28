"""Regressions for the seven defects found by reviewing ingest against the API.

Each test here exists because something was wrong and the existing suite could
not see it. The gzip case is the clearest example of why: every fixture built a
plain FIT file, so a parser that could not read a compressed one passed 224
tests while being unable to read anything the live service actually serves.

Defect ids match the integration specification dated 28 July 2026.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from coach.ingest import activities, archive, parse, service, webhook  # noqa: E402
from conftest_fit import gzipped  # noqa: E402
from ingest_harness import Upstream  # noqa: E402
from test_ingest import DUBAI, SECRET, activity, payload, ride_fit, uploaded  # noqa: E402

ANALYZED_AT = "2026-07-27T22:30:00Z"


# --- D1: the downloaded file is gzipped -------------------------------------


def test_a_gzipped_fit_file_parses() -> None:
    """The shape the live endpoint actually returns."""
    plain = ride_fit()
    parsed = parse.from_fit(gzipped(plain))
    assert parsed.avg_power_w == 150.0
    assert parsed.sample_count > 0


def test_gzipped_and_plain_bytes_parse_identically() -> None:
    """Compression is transport, so it must not change a single parsed value."""
    plain = ride_fit()
    assert parse.from_fit(gzipped(plain)) == parse.from_fit(plain)


def test_decompression_is_idempotent() -> None:
    """Safe to call at every boundary, which is why it is called at several."""
    plain = ride_fit()
    assert parse.decompressed(plain) == plain
    assert parse.decompressed(gzipped(plain)) == plain
    assert parse.decompressed(parse.decompressed(gzipped(plain))) == plain


def test_bytes_that_only_look_gzipped_are_left_alone() -> None:
    """A false positive on the magic number must not lose the payload."""
    liar = b"\x1f\x8b" + b"not actually deflate stream"
    assert parse.decompressed(liar) == liar


# --- D2: the hash has to agree across both ingest paths ---------------------


def test_the_content_hash_ignores_compression() -> None:
    """FIT-04's content half. Hashing as-received would give one ride two keys."""
    plain = ride_fit()
    assert parse.content_hash(gzipped(plain)) == parse.content_hash(plain)


def test_one_ride_through_both_paths_is_one_session(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The duplicate FIT-04 exists to prevent, reproduced end to end.

    The webhook path receives the file gzipped, the watched folder receives it
    plain. Before the hash was taken after decompression these produced two
    hashes and therefore two sessions for a single ride.
    """
    plain = ride_fit()

    # Webhook path: the platform hands over compressed bytes.
    activities.ingest(conn, activity("i1001"), DUBAI, file_bytes=gzipped(plain))
    conn.commit()

    # Watched folder: the same ride, dropped as a plain file.
    dropped = tmp_path / "same-ride.fit"
    dropped.write_bytes(plain)
    archive.ingest_file(conn, dropped, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1, "the same ride produced two session rows"


# --- D3: derived fields are provisional until the platform says otherwise ----


def test_an_upload_lands_with_its_derived_fields_marked_provisional(
    conn: psycopg.Connection,
) -> None:
    """ACTIVITY_UPLOADED fires before analysis completes, so `analyzed` is null."""
    result = activities.ingest(conn, activity("i1001"), DUBAI, file_bytes=ride_fit())
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "select analyzed_at, derived_provisional from sessions where id = %s",
            (result.session_id,),
        )
        row = cur.fetchone()
    assert row["analyzed_at"] is None
    assert row["derived_provisional"] is True


def test_an_analysis_event_refreshes_the_derived_block(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """D3: the numbers read at trigger time were provisional and never updated.

    The refresh must change the platform's block and clear the provisional flag
    without touching a single parsed column, because those came from samples and
    have nothing to learn from a later read.
    """
    provisional = activity("i1001", icu_training_load=55)
    final = activity("i1001", icu_training_load=71, analyzed=ANALYZED_AT)

    client = Upstream([provisional], {"i1001": ride_fit()})
    service.receive(conn, payload(uploaded()))
    service.drain(conn, client, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select id, avg_power_w, derived from sessions")
        before = cur.fetchone()
    assert before["derived"]["icu_training_load"] == 55

    client.upstream = [final]
    service.receive(conn, payload(dict(uploaded(), type="ACTIVITY_ANALYZED")))
    service.drain(conn, client, DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select * from sessions")
        after = cur.fetchone()

    assert after["id"] == before["id"], "the refresh created a second session"
    assert after["derived"]["icu_training_load"] == 71, "derived block not refreshed"
    assert after["analyzed_at"] is not None
    assert after["derived_provisional"] is False
    # FIT-03: parsed values are ours and must be untouched by a platform refresh.
    assert after["avg_power_w"] == before["avg_power_w"]


def test_the_refresh_writes_no_second_review(conn: psycopg.Connection, sandbox: Path) -> None:
    """The ride was reviewed when it landed. Analysis finishing is not new news."""
    client = Upstream([activity("i1001")], {"i1001": ride_fit()})
    notes: list[str] = []

    def writer(_context: dict[str, Any]) -> str:
        notes.append("note")
        return "note"

    service.receive(conn, payload(uploaded()))
    service.drain(conn, client, DUBAI, writer)
    conn.commit()
    assert len(notes) == 1

    client.upstream = [activity("i1001", analyzed=ANALYZED_AT)]
    service.receive(conn, payload(dict(uploaded(), type="ACTIVITY_ANALYZED")))
    service.drain(conn, client, DUBAI, writer)
    conn.commit()
    assert len(notes) == 1, "the analysis refresh generated a second review"


def test_an_analysis_event_for_an_unknown_activity_is_harmless(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """Ordering is not guaranteed; analysis can arrive before we have the ride."""
    client = Upstream([activity("i1001", analyzed=ANALYZED_AT)], {"i1001": ride_fit()})
    service.receive(conn, payload(dict(uploaded(), type="ACTIVITY_ANALYZED")))
    handled = service.drain(conn, client, DUBAI)
    conn.commit()

    assert handled[0].session_id is None
    assert "no local session" in handled[0].skipped
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0


# --- D4: the route answers before it works ----------------------------------


def test_the_route_makes_no_upstream_call_at_all(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PERF-03: intervals.icu treats a slow response as a failure and retries it.

    The handler is therefore allowed one database write and nothing else. This
    asserts against the fake upstream's call log, which is empty until the drain
    runs.
    """
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    ep = endpoint(client)

    status, body = ep.post(payload(uploaded()))

    assert status == 200
    assert body == {"queued": 1}
    assert client.calls == [], "the request path went to the network before answering"
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0, "the request path did the work"

    ep.drain()
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1, "the drain did not do the work either"


def test_a_delivery_is_queued_pending_not_recorded_as_done(
    conn: psycopg.Connection, webhook_secret: str
) -> None:
    """Seeing a delivery and having handled it are different facts."""
    service.receive(conn, payload(uploaded()))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select status, processed_at from webhook_deliveries")
        row = cur.fetchone()
    assert row["status"] == "pending"
    assert row["processed_at"] is None


def test_a_failed_delivery_is_retried_rather_than_swallowed(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The bug the replay guard used to cause, and the reason for the queue.

    A delivery was marked accepted before the work was attempted. When the work
    then failed, the upstream's redelivery collided with that record and was
    discarded as a replay, so the ride was never ingested by the webhook path at
    all and nothing anywhere said so.
    """

    class Broken(Upstream):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.fail = True

        def activity(self, activity_id: str) -> dict[str, Any]:
            if self.fail:
                raise RuntimeError("upstream had a moment")
            return super().activity(activity_id)

    client = Broken([activity("i1001")], {"i1001": ride_fit()})
    service.receive(conn, payload(uploaded()))
    conn.commit()

    handled = service.drain(conn, client, DUBAI)
    conn.commit()
    assert "failed" in handled[0].skipped
    with conn.cursor() as cur:
        cur.execute("select status, attempts from webhook_deliveries")
        row = cur.fetchone()
    assert row["status"] == "pending", "a failed delivery was not left retryable"
    assert row["attempts"] == 1

    # The next pass succeeds and the ride lands. Nothing was lost.
    client.fail = False
    handled = service.drain(conn, client, DUBAI)
    conn.commit()
    assert handled[0].session_id is not None
    with conn.cursor() as cur:
        cur.execute("select status from webhook_deliveries")
        assert cur.fetchone()["status"] == "done"


def test_a_delivery_that_keeps_failing_stops_being_retried(
    conn: psycopg.Connection, webhook_secret: str
) -> None:
    """A poisoned delivery must not spin forever; the reconcile is the backstop."""
    service.receive(conn, payload(uploaded()))
    conn.commit()
    delivery_id = None
    for _ in range(6):
        claimed = webhook.claim(conn, 10)
        if not claimed:
            break
        delivery_id = claimed[0]["id"]
        webhook.finish(conn, delivery_id, ok=False, reason="still broken")
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("select status, attempts from webhook_deliveries where id = %s", (delivery_id,))
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 5


# --- D5: replay protection covered only activity events ---------------------


def calendar_event(
    updated: list[str], deleted: list[str] | None = None, ts: str = "2026-07-28T09:00:00+00:00"
) -> dict[str, Any]:
    return {
        "type": "CALENDAR_UPDATED",
        "athlete_id": "i653843",
        "timestamp": ts,
        "events": [{"id": e} for e in updated],
        "deleted_events": [{"id": e} for e in (deleted or [])],
        "oauth_client_id": "app123",
    }


def test_a_replayed_calendar_delivery_is_dropped(
    conn: psycopg.Connection, webhook_secret: str
) -> None:
    """D5: these bypassed the replay index entirely because they carry no activity.

    The index was partial on a non-null activity id, and a calendar payload has
    none, so every delivery inserted a fresh row. Harmless while non-trigger
    events were discarded, and a duplicate-application bug the moment PLAN-06
    starts acting on them.
    """
    body = payload(calendar_event(["e1", "e2"]))
    assert service.receive(conn, body) == 1
    assert service.receive(conn, body) == 0, "the replayed calendar delivery was queued again"

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from webhook_deliveries")
        assert cur.fetchone()["n"] == 1


def test_two_different_calendar_deliveries_are_both_kept(
    conn: psycopg.Connection, webhook_secret: str
) -> None:
    """Deduplication must not collapse genuinely distinct changes."""
    assert service.receive(conn, payload(calendar_event(["e1"]))) == 1
    assert service.receive(conn, payload(calendar_event(["e2"]))) == 1
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from webhook_deliveries")
        assert cur.fetchone()["n"] == 2


def test_every_delivery_gets_a_replay_key(conn: psycopg.Connection, webhook_secret: str) -> None:
    """No event type is exempt, which is what lets the index be total."""
    service.receive(conn, payload(uploaded(), calendar_event(["e1"])))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("select delivery_key from webhook_deliveries")
        keys = [r["delivery_key"] for r in cur.fetchall()]
    assert len(keys) == 2
    assert all(k for k in keys), "a delivery was stored with no replay key"
    assert len(set(keys)) == 2


# --- D6: the calendar payload has a different shape -------------------------


def test_the_nested_calendar_shape_is_parsed() -> None:
    """D6: reading it with the activity shape yielded an event with no content."""
    events = webhook.parse(payload(calendar_event(["e1", "e2"], deleted=["e3"])))
    assert len(events) == 1
    change = events[0].calendar
    assert change is not None
    assert sorted(change.updated) == ["e1", "e2"]
    assert change.deleted == ["e3"]
    # PLAN-06 needs this to tell the athlete's edits from the coach's own echo.
    assert change.oauth_client_id == "app123"


def test_an_activity_event_carries_no_calendar_block() -> None:
    """The two shapes stay distinct rather than one being coerced into the other."""
    events = webhook.parse(payload(uploaded()))
    assert events[0].calendar is None
    assert events[0].external_ref == "i1001"


def test_a_calendar_delete_and_update_of_one_event_are_distinguished() -> None:
    """Upstream can race these, so both arrays must survive parsing separately."""
    events = webhook.parse(payload(calendar_event([], deleted=["e9"])))
    change = events[0].calendar
    assert change is not None and change.updated == [] and change.deleted == ["e9"]


# --- D7: two small omissions ------------------------------------------------


def test_the_activity_list_sends_an_explicit_limit() -> None:
    """An unstated server default can truncate a backfill window with no error."""
    import httpx

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    from coach.ingest import client as clientmod

    with httpx.Client(transport=transport, base_url=clientmod.BASE) as http:
        api = clientmod.Intervals(api_key="k", client=http)
        api.activities(datetime(2026, 1, 1, tzinfo=UTC).date())

    assert seen.get("limit") == str(clientmod.ACTIVITY_PAGE_LIMIT)
    assert seen.get("oldest") == "2026-01-01"


def test_the_original_file_is_decompressed_at_the_client_boundary() -> None:
    """So nothing downstream ever sees compressed bytes on the webhook path."""
    import httpx

    plain = ride_fit()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzipped(plain))

    from coach.ingest import client as clientmod

    with httpx.Client(transport=httpx.MockTransport(handler), base_url=clientmod.BASE) as http:
        api = clientmod.Intervals(api_key="k", client=http)
        assert api.original_file("i1001") == plain


# --- the migration has to survive the data the old schema permitted ----------


def test_migration_006_collapses_duplicates_the_old_index_allowed(
    conn: psycopg.Connection,
) -> None:
    """The upgrade path, not the fresh-install path.

    005's replay index was partial on `external_ref is not null`, so calendar
    deliveries were never uniqueness checked and duplicates could accumulate.
    006 replaces it with a total unique index, which will not build over that
    history. A fresh test database is empty, so nothing in the suite would have
    caught this; it would have failed on the first real deployment and nowhere
    else.

    Simulated by inserting the duplicate directly and re-running the collapse,
    since the fixture database already has 006 applied.
    """
    with conn.transaction(), conn.cursor() as cur:
        # Two rows that differ only in id: exactly what 005 permitted.
        for _ in range(2):
            cur.execute(
                """
                insert into webhook_deliveries
                    (delivery_key, event_type, athlete_id, external_ref,
                     event_timestamp, accepted, status)
                values ('dupe-key', 'CALENDAR_UPDATED', 'i1', null, now(), true, 'done')
                on conflict do nothing
                """
            )
        cur.execute(
            """
            insert into webhook_deliveries
                (delivery_key, event_type, athlete_id, external_ref,
                 event_timestamp, accepted, status)
            values ('other-key', 'CALENDAR_UPDATED', 'i1', null, now(), true, 'done')
            """
        )

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from webhook_deliveries")
        assert cur.fetchone()["n"] == 2, "the unique index did not hold"

    # The collapse 006 runs, asserted to be idempotent and to spare distinct rows.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "delete from webhook_deliveries a using webhook_deliveries b "
            "where a.delivery_key = b.delivery_key and a.id > b.id"
        )
    with conn.cursor() as cur:
        cur.execute("select delivery_key from webhook_deliveries order by delivery_key")
        assert [r["delivery_key"] for r in cur.fetchall()] == ["dupe-key", "other-key"]


def test_the_replay_index_is_total_not_partial(conn: psycopg.Connection) -> None:
    """A partial index is what let calendar deliveries through unchecked."""
    with conn.cursor() as cur:
        cur.execute("select indexdef from pg_indexes where indexname = 'webhook_deliveries_replay'")
        row = cur.fetchone()
    assert row is not None, "the replay index is missing"
    assert " WHERE " not in row["indexdef"].upper(), f"still partial: {row['indexdef']}"
