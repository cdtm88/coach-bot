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
from coach.ingest import server, service  # noqa: E402
from conftest_fit import build_fit  # noqa: E402
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


class Upstream:
    """A fake intervals.icu that counts what was asked of it.

    The counts are the point: PERF-03 is a latency requirement, and the only
    thing this system controls about its latency is how many times it goes to the
    network per activity.
    """

    def __init__(self, activities_: list[dict[str, Any]], files: dict[str, bytes] | None = None):
        self.upstream = activities_
        self.files = files or {}
        self.last_limit = type("L", (), {"exhausted": False})()
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
        self._record("activities")
        return self.upstream

    def activity(self, activity_id: str) -> dict[str, Any]:
        self._record("activity")
        for candidate in self.upstream:
            if candidate["id"] == activity_id:
                return candidate
        raise KeyError(activity_id)

    def original_file(self, activity_id: str) -> bytes | None:
        self._record("original_file")
        return self.files.get(activity_id)

    def streams(self, activity_id: str) -> list[dict[str, Any]]:
        self._record("streams")
        return []


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the archive and watch folders inside the test's own directory."""
    monkeypatch.setenv("COACH_FIT_ARCHIVE", str(tmp_path / "archive"))
    monkeypatch.setenv("COACH_FIT_WATCH", str(tmp_path / "inbox"))
    return tmp_path


def connector(conn: psycopg.Connection):
    """Hand the handler the test's own connection, without closing it.

    The handler runs on another thread, so it cannot open its own connection to
    the test database and still see the test's uncommitted fixture rows.
    """
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield conn

    return connect


@pytest.fixture
def endpoint(conn: psycopg.Connection, sandbox: Path):
    """A real HTTP server on a real port, torn down after the test."""
    started: dict[str, Any] = {}

    def build(client: Upstream, write_note=service.no_review):
        httpd = server.serve(connector(conn), client, DUBAI, write_note, port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started["httpd"] = httpd
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield build

    if "httpd" in started:
        started["httpd"].shutdown()
        started["httpd"].server_close()


def post(url: str, body: Any, raw: bytes | None = None) -> tuple[int, dict[str, Any]]:
    data = raw if raw is not None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# --- FIT-01 / SEC-02 at the endpoint ---------------------------------------


def test_an_upload_webhook_produces_a_reviewed_session(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-01 end to end: a POST to the route leaves a session and a review."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    url = endpoint(client, lambda _context: "Solid tempo. Hold that cadence next time.")

    status, body = post(f"{url}{server.ROUTE}", payload(uploaded()))
    assert status == 200
    assert body["handled"] == 1
    assert body["sessions"], "the webhook produced no session"

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
    url = endpoint(Upstream([activity()], {"i1001": ride_fit()}))

    status, _ = post(f"{url}{server.ROUTE}", payload(uploaded(), secret="wrong"))
    assert status == 401

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0, "a rejected payload created a session"


def test_the_rejection_says_nothing_useful(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that failed the check learns only that it failed."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    url = endpoint(Upstream([]))
    _, body = post(f"{url}{server.ROUTE}", payload(uploaded(), secret="wrong"))
    assert body == {"error": "rejected"}
    assert "secret" not in json.dumps(body).lower()


def test_a_redelivered_webhook_over_http_creates_no_second_session(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-02: the retry intervals.icu makes on a slow response is safe."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    url = endpoint(client)

    first = post(f"{url}{server.ROUTE}", payload(uploaded()))
    second = post(f"{url}{server.ROUTE}", payload(uploaded()))
    assert first[0] == 200 and second[0] == 200
    assert second[1]["handled"] == 0, "the replay was acted on"

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 1


def test_a_non_trigger_event_is_recorded_but_not_ingested(
    conn: psycopg.Connection, endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-01: ACTIVITY_ANALYZED is known, logged, and not the trigger."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})
    url = endpoint(client)

    analyzed = dict(uploaded(), type="ACTIVITY_ANALYZED")
    status, _ = post(f"{url}{server.ROUTE}", payload(analyzed))
    assert status == 200
    assert client.calls == [], "a non-trigger event went upstream"

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from sessions")
        assert cur.fetchone()["n"] == 0
        cur.execute("select event_type from webhook_deliveries")
        assert cur.fetchone()["event_type"] == "ACTIVITY_ANALYZED"


def test_an_unknown_route_is_not_the_webhook(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    url = endpoint(Upstream([]))
    assert post(f"{url}/", payload(uploaded()))[0] == 404
    assert post(f"{url}/webhook", payload(uploaded()))[0] == 404


def test_a_body_that_is_not_json_is_a_400_not_a_crash(
    endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    url = endpoint(Upstream([]))
    assert post(f"{url}{server.ROUTE}", None, raw=b"not json at all")[0] == 400
    assert post(f"{url}{server.ROUTE}", None, raw=b'["a list"]')[0] == 400


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
    url = endpoint(Upstream([]))
    host, port = url.removeprefix("http://").split(":")

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
    url = endpoint(Upstream([]))
    assert post(f"{url}{server.ROUTE}", None, raw=b"")[0] == 413


def test_health_answers_without_a_secret(endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    url = endpoint(Upstream([]))
    with urllib.request.urlopen(f"{url}/health", timeout=10) as response:
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

    handled = service.on_webhook(conn, client, payload(uploaded()), DUBAI)

    assert len(handled) == 1
    assert client.calls == ["activity", "original_file"], client.calls


def test_a_missing_original_file_costs_one_extra_call_and_no_more(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-03's fallback: streams, then stop. Never a third attempt."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], files={})  # no original available

    service.on_webhook(conn, client, payload(uploaded()), DUBAI)
    assert client.calls == ["activity", "original_file", "streams"], client.calls


# --- FIT-15: the webhook path archives what it downloads --------------------


def test_a_downloaded_file_lands_in_the_archive(
    conn: psycopg.Connection, sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIT-15: upstream deleting the activity later must not lose the bytes."""
    monkeypatch.setenv("INTERVALS_WEBHOOK_SECRET", SECRET)
    client = Upstream([activity()], {"i1001": ride_fit()})

    service.on_webhook(conn, client, payload(uploaded()), DUBAI)

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


def test_a_failing_tick_does_not_end_the_loop(conn: psycopg.Connection, sandbox: Path) -> None:
    """FIT-01's backstop is worthless if one unreachable upstream stops it."""
    attempts: list[int] = []

    class Broken(Upstream):
        def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
            attempts.append(1)
            raise RuntimeError("upstream is down")

    stop = threading.Event()
    thread = threading.Thread(
        target=server.ticker,
        args=(connector(conn), Broken([]), DUBAI, service.no_review, stop),
        kwargs={"interval_s": 1},
        daemon=True,
    )
    thread.start()
    # Two ticks at a one second interval; the loop must survive the first.
    for _ in range(300):
        if len(attempts) >= 2:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=5)

    assert len(attempts) >= 2, "the loop stopped after a failing tick"


def test_the_backstop_interval_is_six_hours() -> None:
    """FIT-01 names the interval, so it is asserted rather than assumed."""
    from coach.ingest import reconcile

    assert reconcile.INTERVAL_HOURS == 6
    assert server.ticker.__defaults__[-1] == 6 * 3600


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
