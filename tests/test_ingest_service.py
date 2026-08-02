"""P03 acceptance for the parts that run: the webhook route and the backstop tick.

The modules in test_ingest.py are libraries. FIT-01 is not satisfied by a library
— it names a trigger and a six hourly backstop, and until something answers an
HTTP request and something else runs on a timer, neither exists. These tests
drive a real socket against a real database so that "the endpoint verifies the
secret" is a fact about the endpoint and not about a function the endpoint
happens to call today.

PERF-03's five minute budget is asserted here as a round trip count rather than a
wall clock. Timing a fake upstream measures nothing; the number of sequential
calls to a real one is what actually decides whether the budget is met.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from coach.ingest import client as clientmod  # noqa: E402
from coach.ingest import server, service  # noqa: E402
from conftest_fit import build_fit  # noqa: E402
from ingest_harness import Upstream, connector, post  # noqa: E402
from test_ingest import (  # noqa: E402
    DUBAI,
    SECRET,
    START,
    activity,
    payload,
    prescribe,
    ride_fit,
    uploaded,
)

# --- FIT-01 / SEC-02 at the endpoint ---------------------------------------


def test_an_upload_webhook_produces_a_reviewed_session(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-01 end to end: a POST to the route leaves a session and a review."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    ep = endpoint(client, lambda _context: "Solid tempo. Hold that cadence next time.")

    status, body, handled = ep.post_and_drain(payload(uploaded()))
    assert status == 200
    assert body == {"queued": 1}, "the route did more than acknowledge"
    assert [h.session_id for h in handled if h.session_id], "the drain produced no session"

    with conn.cursor() as cur:
        cur.execute("select external_ref, reviewed_at, avg_power_w from sessions")
        row = cur.fetchone()
    assert row["external_ref"] == "i1001"
    assert row["reviewed_at"] is not None
    # FIT-03: 150 is the mean of the samples. 999 is icu_average_watts.
    assert float(row["avg_power_w"]) == 150.0


def test_the_endpoint_rejects_a_wrong_secret(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-02 at the boundary, not one layer in."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([activity()], {"i1001": ride_fit()}))

    status, _, _ = ep.post_and_drain(payload(uploaded(), secret="wrong"))
    assert status == 401

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0, "a rejected payload created a session"


def test_the_rejection_says_nothing_useful(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that failed the check learns only that it failed."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    _, body = ep.post(payload(uploaded(), secret="wrong"))
    assert body == {"error": "rejected"}
    assert "secret" not in json.dumps(body).lower()


def test_a_redelivered_webhook_over_http_creates_no_second_session(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-02: the retry intervals.icu makes on a slow response is safe."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    ep = endpoint(client)

    first = ep.post_and_drain(payload(uploaded()))
    second = ep.post_and_drain(payload(uploaded()))
    assert first[0] == 200 and second[0] == 200
    assert second[1] == {"queued": 0}, "the replay was queued"
    assert second[2] == [], "the replay was acted on"

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1


def test_a_non_trigger_event_is_recorded_but_not_ingested(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-01: ACTIVITY_ANALYZED is known, logged, and not the trigger."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    ep = endpoint(client)

    analyzed = dict(uploaded(), type="ACTIVITY_ANALYZED")
    status, _, _ = ep.post_and_drain(payload(analyzed))
    assert status == 200

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0
        cur.execute("select event_type from webhook_deliveries")
        assert cur.fetchone()["event_type"] == "ACTIVITY_ANALYZED"


def test_an_unknown_route_is_not_the_webhook(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    assert post(f"{ep.url}/", payload(uploaded()))[0] == 404
    assert post(f"{ep.url}/webhook", payload(uploaded()))[0] == 404


def test_a_body_that_is_not_json_is_a_400_not_a_crash(
    endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    assert post(f"{ep.url}{server.ROUTE}", None, raw=b"not json at all")[0] == 400
    assert post(f"{ep.url}{server.ROUTE}", None, raw=b'["a list"]')[0] == 400


def test_an_oversized_body_is_refused_before_it_is_read(
    endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbounded Content-Length read would be a memory hole.

    Sent as a raw socket announcing a huge body and then sending none of it. If
    the server answers at all it can only have decided from the header, which is
    the property worth having: a caller cannot make this process allocate a
    gigabyte by claiming it is about to send one.
    """
    import socket

    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    host, port = ep.url.removeprefix("http://").split(":")

    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            f"POST {server.ROUTE} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {server.MAX_BODY_BYTES + 1}\r\n"
            f"\r\n".encode()
        )
        status_line = sock.recv(64).decode(errors="replace")

    assert "413" in status_line, status_line


def test_an_absent_body_is_refused_too(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    assert post(f"{ep.url}{server.ROUTE}", None, raw=b"")[0] == 413


def test_health_answers_without_a_secret(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    ep = endpoint(Upstream([]))
    with urllib.request.urlopen(f"{ep.url}/health", timeout=10) as response:
        assert response.status == 200


# --- PERF-03 ---------------------------------------------------------------


def test_the_hot_path_makes_two_upstream_calls(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PERF-03: the five minute budget is spent on round trips, so count them.

    One read of the activity for its `icu_` fields, one download of the original
    file. A third call would mean something re-fetched what it was already given,
    which is the regression this exists to catch.
    """
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})

    service.receive(conn, payload(uploaded()))
    handled = service.drain(conn, client, DUBAI)

    assert len(handled) == 1
    assert client.calls == ["activity", "original_file"], client.calls


def test_a_missing_original_file_costs_one_extra_call_and_no_more(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-03's fallback: streams, then stop. Never a third attempt."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], files={})  # no original available

    service.receive(conn, payload(uploaded()))
    service.drain(conn, client, DUBAI)
    assert client.calls == ["activity", "original_file", "streams"], client.calls


# --- FIT-15: every path archives what it downloads --------------------------


def test_the_poll_archives_what_it_downloads(conn: psycopg.Connection, sandbox: Path) -> None:
    """FIT-15 on the path that ingests almost everything.

    Only the webhook drain used to archive. Without a registered app the drain
    never runs, so the poll fetched each original to parse it and dropped the
    bytes on the floor — leaving the permanent archive holding nothing but what
    the watched folder happened to see, on a system whose whole reason for
    keeping a local copy is that upstream can delete its own.
    """
    (sandbox / "inbox").mkdir()
    service.poll(conn, Upstream([activity()], {"i1001": ride_fit()}), DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select path, external_ref, session_id from fit_archive")
        row = cur.fetchone()
    assert row is not None, "the poll downloaded the original and threw it away"
    assert row["external_ref"] == "i1001"
    assert row["session_id"] is not None
    assert Path(row["path"]).read_bytes() == ride_fit()


def test_the_poll_and_the_folder_do_not_archive_the_same_bytes_twice(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The same ride reaching both paths is one archive row and one session."""
    inbox = sandbox / "inbox"
    inbox.mkdir()
    (inbox / "2026-07-27-181000.fit").write_bytes(ride_fit())

    service.poll(conn, Upstream([activity()], {"i1001": ride_fit()}), DUBAI)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from fit_archive")
        assert cur.fetchone()["n"] == 1
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1
        cur.execute("select name from sessions")
        assert cur.fetchone()["name"] == "Tempus Fugit"


def test_a_downloaded_file_lands_in_the_archive(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-15: upstream deleting the activity later must not lose the bytes."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})

    service.receive(conn, payload(uploaded()))
    service.drain(conn, client, DUBAI)

    with conn.cursor() as cur:
        cur.execute("select path, external_ref, session_id from fit_archive")
        row = cur.fetchone()
    assert row is not None, "the downloaded original was never archived"
    assert row["external_ref"] == "i1001"
    assert row["session_id"] is not None
    assert Path(row["path"]).read_bytes() == ride_fit()


def test_archiving_the_same_activity_twice_keeps_one_row(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})

    service.on_activity(conn, client, activity(), DUBAI)
    service.on_activity(conn, client, activity(), DUBAI)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from fit_archive")
        assert cur.fetchone()["n"] == 1


# --- FIT-01's backstop: the tick -------------------------------------------


def test_the_tick_reconciles_scans_and_ages_out(conn: psycopg.Connection, sandbox: Path) -> None:
    """One pass does all three, in an order where the missed check sees the rides.

    The prescription here is old enough to be past the grace window and has no
    session, so it is missed; the ride the reconcile picks up is on a different
    day, so it cannot rescue it.
    """
    (sandbox / "inbox").mkdir()
    (sandbox / "inbox" / "dropped.fit").write_bytes(build_fit(START, power=[150] * 120))

    prescribe(conn, datetime(2026, 6, 1, 18, 0, tzinfo=UTC))

    client = Upstream([activity()], {"i1001": ride_fit()})
    result = service.tick(
        conn,
        client,
        DUBAI,
        now=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        lookback_days=60,
    )

    assert result["reconciled"] == 1, "the reconcile leg did nothing"
    assert len(result["scanned"]) == 1, "the watched folder was not scanned"
    assert result["missed"] == 1, "the old prescription was not aged out"


def test_the_tick_does_not_review_backfilled_history(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """FIT-09 survives the tick: the note writer is never reached for history."""
    from coach.ingest import reconcile

    client = Upstream([activity("i1", start_local="2026-01-05T08:00:00")], {"i1": ride_fit()})
    reconcile.run(conn, client, DUBAI, date(2026, 1, 1), backfill=True)
    conn.commit()

    calls: list[dict[str, Any]] = []

    def writer(context: dict[str, Any]) -> str:
        calls.append(context)
        return "note"

    service.tick(
        conn, Upstream([]), DUBAI, now=datetime(2026, 7, 28, tzinfo=UTC), write_note=writer
    )
    assert calls == [], "a backfilled session was reviewed by the tick"


def test_a_failing_poll_does_not_end_the_loop(conn: psycopg.Connection, sandbox: Path) -> None:
    """With no webhook the poll is the only thing that notices a ride.

    A loop that exits on a transient upstream error is the coach going silent
    with nothing anywhere saying why.
    """
    attempts: list[int] = []

    class Broken(Upstream):
        def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
            attempts.append(1)
            raise RuntimeError("upstream is down")

    stop = threading.Event()
    thread = threading.Thread(
        target=server.poller,
        args=(connector(conn), Broken([]), DUBAI, service.no_review, stop),
        kwargs={"interval_s": 1},
        daemon=True,
    )
    thread.start()
    for _ in range(300):
        if len(attempts) >= 2:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=5)

    assert len(attempts) >= 2, "the loop stopped after a failing pass"


def test_the_default_cadences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two minutes to poll, six hours to sweep, unless the environment says otherwise."""
    from coach.ingest import reconcile

    monkeypatch.delenv("COACH_POLL_INTERVAL_S", raising=False)
    monkeypatch.delenv("COACH_SWEEP_INTERVAL_S", raising=False)
    assert reconcile.poll_interval_s() == 120
    assert reconcile.sweep_interval_s() == 6 * 3600


def test_the_cadences_are_environment_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The right interval depends on a rate limit only a live key can reveal."""
    from coach.ingest import reconcile

    monkeypatch.setenv("COACH_POLL_INTERVAL_S", "45")
    monkeypatch.setenv("COACH_SWEEP_INTERVAL_S", "900")
    assert reconcile.poll_interval_s() == 45
    assert reconcile.sweep_interval_s() == 900


def test_a_reckless_interval_is_floored_not_obeyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling every second would burn the daily quota before lunch.

    The failure would look like intervals.icu being broken rather than like a
    configuration mistake, which is why this is a floor and not a free number.
    """
    from coach.ingest import reconcile

    monkeypatch.setenv("COACH_POLL_INTERVAL_S", "1")
    assert reconcile.poll_interval_s() == 30
    monkeypatch.setenv("COACH_SWEEP_INTERVAL_S", "5")
    assert reconcile.sweep_interval_s() == 300


def test_a_nonsense_interval_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not stop ingest; it warns and uses the default."""
    from coach.ingest import reconcile

    monkeypatch.setenv("COACH_POLL_INTERVAL_S", "two minutes please")
    assert reconcile.poll_interval_s() == 120


# --- FIT-12 through the service layer --------------------------------------


def test_a_ride_on_the_day_stops_the_tick_marking_it_missed(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The load cross check, exercised where it actually runs."""
    planned = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)
    prescribe(conn, planned)

    client = Upstream([activity()], {"i1001": ride_fit()})
    result = service.tick(conn, client, DUBAI, now=planned + timedelta(hours=30), lookback_days=60)

    assert result["missed"] == 0, "a prescription was missed on a day with a ride"


# --- ingest without a webhook -----------------------------------------------


def test_a_dropped_file_is_ingested_and_reviewed_with_no_api_call(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The path that needs no registered app, no webhook and no credential.

    A Zwift ride synced from the local Activities directory lands here. This is
    the primary ingest path for Zwift now, and the assertion that matters is the
    call count: zero. It keeps working when intervals.icu is unreachable, and it
    sees the file before intervals.icu does.
    """
    inbox = sandbox / "inbox"
    inbox.mkdir()
    (inbox / "zwift-ride.fit").write_bytes(ride_fit())

    client = Upstream([])  # upstream has nothing and is never asked for anything
    notes: list[str] = []
    result = service.poll(conn, client, DUBAI, lambda _c: notes.append("n") or "Solid session.")
    conn.commit()

    assert len(result["scanned"]) == 1, "the dropped file was not ingested"
    assert len(result["reviewed"]) == 1, "the dropped file produced no review"
    assert notes == ["n"]
    assert client.calls == ["activities"], (
        f"the folder path should cost one list call at most, made {client.calls}"
    )

    with conn.cursor() as cur:
        cur.execute("select source, avg_power_w, reviewed_at from sessions")
        row = cur.fetchone()
    assert row["source"] == "local_file"
    assert float(row["avg_power_w"]) == 150.0
    assert row["reviewed_at"] is not None


def test_the_poll_asks_for_a_narrow_window(conn: psycopg.Connection, sandbox: Path) -> None:
    """Running every couple of minutes, a fortnight of rows per pass buys nothing.

    The wide window belongs to the backfill and to a manual reconcile, not to a
    loop that runs seven hundred times a day.
    """
    windows: list[date] = []

    class Watching(Upstream):
        def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
            windows.append(oldest)
            return []

    service.poll(conn, Watching([]), DUBAI)
    assert windows, "the poll made no list call"
    span = (date.today() - windows[0]).days
    assert span == service.POLL_LOOKBACK_DAYS == 2, f"asked for {span} days"


def test_the_sweep_makes_no_upstream_call(conn: psycopg.Connection) -> None:
    """It only reads prescriptions and sessions, so it costs nothing upstream."""
    result = service.sweep(conn, DUBAI, datetime(2026, 7, 28, 12, 0, tzinfo=UTC))
    assert result == {"missed": 0}


# --- OBS-05 / CHAT-09: the feeds the poll is responsible for stamping --------


def _feed(conn: psycopg.Connection, name: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select last_success_at, last_error from feeds where name = %s", (name,))
        return cur.fetchone()


def test_a_successful_poll_stamps_the_feeds_it_read(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """OBS-05 names five inbound feeds; two of them run through this poll.

    Nothing used to stamp either, and `feeds` is seeded with all five at
    migration time, so CHAT-09 read them as never-successful on every turn
    forever — the coach hedged its numbers minutes after seventeen activities had
    come through that same client.
    """
    inbox = sandbox / "inbox"
    inbox.mkdir()
    (inbox / "zwift-ride.fit").write_bytes(ride_fit())

    service.poll(conn, Upstream([activity()], {"i1001": ride_fit()}), DUBAI)
    conn.commit()

    assert _feed(conn, "activities")["last_success_at"] is not None
    assert _feed(conn, "fit_archive")["last_success_at"] is not None


def test_a_quiet_week_is_not_a_broken_feed(conn: psycopg.Connection, sandbox: Path) -> None:
    """The feed answered. It carried nothing because nothing happened.

    CHAT-09's own words are that absence of data is not evidence of absence of
    activity, so a rest week must not be reported to the coach as a dead feed.
    """
    (sandbox / "inbox").mkdir()
    service.poll(conn, Upstream([]), DUBAI)
    conn.commit()

    assert _feed(conn, "activities")["last_success_at"] is not None
    assert _feed(conn, "fit_archive")["last_success_at"] is not None


def test_an_unreachable_upstream_records_the_error_and_no_success(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    (sandbox / "inbox").mkdir()

    class Broken(Upstream):
        def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
            raise clientmod.IntervalsError("503 from intervals.icu")

    service.poll(conn, Broken([]), DUBAI)
    conn.commit()

    feed = _feed(conn, "activities")
    assert feed["last_success_at"] is None
    assert "503" in feed["last_error"]


def test_a_watched_folder_that_disappeared_is_an_error_not_a_success(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The archive is stale when the sync stops mounting, not when riding stops."""
    service.poll(conn, Upstream([]), DUBAI)  # the inbox is deliberately not created
    conn.commit()

    feed = _feed(conn, "fit_archive")
    assert feed["last_success_at"] is None
    assert "does not exist" in feed["last_error"]


def test_the_coach_does_not_call_a_working_feed_dead(
    conn: psycopg.Connection, sandbox: Path
) -> None:
    """The end of the chain, and the reason the two above matter.

    CHAT-09 renders straight into the system prompt, so a feed nothing stamps is
    not merely untracked: it is asserted to the coach as never having returned.
    """
    from coach.agent import prompt

    (sandbox / "inbox").mkdir()
    service.poll(conn, Upstream([activity()], {"i1001": ride_fit()}), DUBAI)
    conn.commit()

    rendered = prompt.render_staleness(conn, datetime.now(UTC))
    assert "activities" not in rendered
    assert "fit_archive" not in rendered
