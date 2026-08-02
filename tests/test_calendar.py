"""Calendar feeds: CALR-01 to CALR-06, and PLAN-08.

Every test builds a real ICS document and runs it through the real parser. The
things most likely to be wrong here are the things a hand-built fixture dict
would skip: an RRULE with an EXDATE, an all-day event's implicit end, a floating
time with no zone, and a DECLINED attendee.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from coach.agent import prompt, tools
from coach.calendars import availability, feed

REPO = Path(__file__).resolve().parents[1]
DUBAI = ZoneInfo("Asia/Dubai")
TODAY = date(2026, 7, 28)  # a Tuesday
SECRET_URL = "https://calendar.google.com/calendar/ical/abc123secret/private-9f8e7d/basic.ics"


def ics(*events: str, name: str = "Work") -> bytes:
    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
            f"X-WR-CALNAME:{name}",
            *events,
            "END:VCALENDAR",
        ]
    )
    return body.encode()


def event(
    uid: str,
    start: str,
    end: str,
    summary: str = "Meeting",
    extra: list[str] | None = None,
) -> str:
    return "\r\n".join(
        [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;TZID=Asia/Dubai:{start}",
            f"DTEND;TZID=Asia/Dubai:{end}",
            f"SUMMARY:{summary}",
            *(extra or []),
            "END:VEVENT",
        ]
    )


def stamp(day: date, hour: int, minute: int = 0) -> str:
    return f"{day:%Y%m%d}T{hour:02d}{minute:02d}00"


def transport(body: bytes | None = None, status: int = 200) -> httpx.Client:
    """An httpx client that answers from memory, without a network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def one_feed(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CALENDAR_ICS_URLS", SECRET_URL)
    return SECRET_URL


def stored(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("select * from calendar_events order by starts_at")
        return cur.fetchall()


# --- CALR-01: from a URL in the environment alone ----------------------------


def test_events_appear_from_a_url_in_the_environment(conn, one_feed) -> None:
    """CALR-01's acceptance, exactly as written."""
    body = ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 19, 30), "Dinner"))
    results = feed.sync(conn, DUBAI, TODAY, client=transport(body))

    assert len(results) == 1 and results[0].ok
    rows = stored(conn)
    assert len(rows) == 1
    assert rows[0]["summary"] == "Dinner"
    assert rows[0]["busy"]
    assert rows[0]["local_date"] == TODAY


def test_the_feed_is_named_from_the_calendar_not_the_url(conn, one_feed) -> None:
    """X-WR-CALNAME is the calendar's own label and leaks nothing."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(name="Personal")))
    with conn.cursor() as cur:
        cur.execute("select id, name from calendar_feeds")
        row = cur.fetchone()
    assert row["name"] == "Personal"
    assert row["id"] == feed.feed_id(SECRET_URL)


def test_a_feed_that_failed_then_succeeded_is_one_feed(conn, one_feed) -> None:
    """The identity is the fingerprint, not the name.

    A failed fetch has no document to read X-WR-CALNAME from, so it falls back to
    a positional label; the next success names it "Work". Keyed on the name those
    are two feeds, and the second collides on the fingerprint.
    """
    feed.sync(conn, DUBAI, TODAY, client=transport(None))
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(name="Work")))

    with conn.cursor() as cur:
        cur.execute("select id, name from calendar_feeds")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Work"


def test_several_urls_each_become_a_feed(conn, monkeypatch) -> None:
    monkeypatch.setenv("CALENDAR_ICS_URLS", f"{SECRET_URL}, https://example.com/other.ics")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        name = "Work" if "abc123" in str(request.url) else "Golf"
        return httpx.Response(200, content=ics(name=name))

    feed.sync(conn, DUBAI, TODAY, client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert len(calls) == 2
    with conn.cursor() as cur:
        cur.execute("select name from calendar_feeds order by position")
        assert [r["name"] for r in cur.fetchall()] == ["Work", "Golf"]


def test_no_urls_configured_is_not_an_error(conn, monkeypatch) -> None:
    monkeypatch.delenv("CALENDAR_ICS_URLS", raising=False)
    assert feed.sync(conn, DUBAI, TODAY) == []


# --- CALR-06: the URLs are bearer secrets ------------------------------------


def test_the_url_is_never_stored(conn, one_feed) -> None:
    """CALR-06. A secret in a column is a secret in the nightly pg_dump."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))

    with conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public' and table_name like 'calendar%'"
        )
        columns = [(r["table_name"], r["column_name"]) for r in cur.fetchall()]

    for table, column in columns:
        with conn.cursor() as cur:
            cur.execute(f"select {column}::text as v from {table}")  # noqa: S608 - test only
            for row in cur.fetchall():
                assert SECRET_URL not in (row["v"] or "")
                assert "abc123secret" not in (row["v"] or "")


def test_a_failing_fetch_does_not_put_the_url_in_the_error(conn, one_feed) -> None:
    """httpx puts the request URL in its exception messages by default."""
    results = feed.sync(conn, DUBAI, TODAY, client=transport(None))

    assert not results[0].ok
    assert "abc123secret" not in results[0].error
    assert "calendar.google.com" not in results[0].error

    with conn.cursor() as cur:
        cur.execute("select last_error from calendar_feeds")
        assert "abc123secret" not in (cur.fetchone()["last_error"] or "")


def test_an_http_error_does_not_put_the_url_in_the_error(conn, one_feed) -> None:
    """`raise_for_status` would have; that is why it is not used."""
    results = feed.sync(conn, DUBAI, TODAY, client=transport(b"nope", status=404))

    assert not results[0].ok
    assert "abc123secret" not in results[0].error
    assert "404" in results[0].error


def test_no_log_line_contains_a_feed_url(conn, one_feed, caplog) -> None:
    """CALR-06's acceptance, exactly as written."""
    import logging

    with caplog.at_level(logging.DEBUG):
        feed.sync(conn, DUBAI, TODAY, client=transport(None))
        feed.sync(
            conn,
            DUBAI,
            TODAY,
            client=transport(ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 19)))),
        )

    for record in caplog.records:
        assert "abc123secret" not in record.getMessage()
        assert SECRET_URL not in record.getMessage()


def test_the_fingerprint_cannot_be_reversed(one_feed) -> None:
    fingerprint = feed.fingerprint(SECRET_URL)
    assert len(fingerprint) == 64
    assert "abc123secret" not in fingerprint
    assert feed.fingerprint(SECRET_URL) == fingerprint  # stable across calls


# --- CALR-04: declined and cancelled are not busy ----------------------------


def test_a_cancelled_event_does_not_block(conn, one_feed) -> None:
    """CALR-04: seeded declined event does not block scheduling."""
    body = ics(event("gone", stamp(TODAY, 18), stamp(TODAY, 20), "Cancelled", ["STATUS:CANCELLED"]))
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    rows = stored(conn)
    assert len(rows) == 1
    assert not rows[0]["busy"]


def test_a_declined_invitation_does_not_block(conn, one_feed) -> None:
    body = ics(
        event(
            "declined",
            stamp(TODAY, 18),
            stamp(TODAY, 20),
            "Someone else's meeting",
            ["ATTENDEE;PARTSTAT=DECLINED:mailto:athlete@example.com"],
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))
    assert not stored(conn)[0]["busy"]


def test_an_accepted_invitation_does_block(conn, one_feed) -> None:
    body = ics(
        event(
            "accepted",
            stamp(TODAY, 18),
            stamp(TODAY, 20),
            "A meeting he is going to",
            ["ATTENDEE;PARTSTAT=ACCEPTED:mailto:athlete@example.com"],
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))
    assert stored(conn)[0]["busy"]


def test_an_event_marked_free_does_not_block(conn, one_feed) -> None:
    """The third case CALR-04 implies without naming.

    A birthday reminder is on the calendar and marked TRANSPARENT. Treating it
    as busy would cost the athlete an evening a week to his own notifications.
    """
    body = ics(
        event(
            "bday", stamp(TODAY, 9), stamp(TODAY, 10), "Someone's birthday", ["TRANSP:TRANSPARENT"]
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))
    assert not stored(conn)[0]["busy"]


def test_a_non_blocking_event_is_kept_rather_than_dropped(conn, one_feed) -> None:
    """The verdict is stored beside its inputs so it can be explained."""
    body = ics(event("gone", stamp(TODAY, 18), stamp(TODAY, 20), "Cancelled", ["STATUS:CANCELLED"]))
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    row = stored(conn)[0]
    assert row["status"] == "CANCELLED"
    assert not row["busy"]


# --- recurrence ---------------------------------------------------------------


def test_a_weekly_commitment_expands_into_occurrences(conn, one_feed) -> None:
    """The most common shape in a real calendar, and the one a naive parser drops.

    The rule is anchored four weeks back rather than this week, because a
    recurrence has no occurrences before its DTSTART and the look back is half of
    what this expansion is for.
    """
    monday = TODAY - timedelta(days=TODAY.weekday() + 28)
    body = ics(
        event(
            "weekly",
            stamp(monday, 19),
            stamp(monday, 21),
            "Five a side",
            ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    rows = stored(conn)
    assert len(rows) >= 7  # four weeks back plus three forward
    assert {r["uid"] for r in rows} == {"weekly"}
    assert len({r["recurrence_id"] for r in rows}) == len(rows)
    assert all(r["local_date"].weekday() == 0 for r in rows)


def test_an_excluded_week_is_not_an_occurrence(conn, one_feed) -> None:
    """EXDATE is how a single week of a recurring commitment is cancelled."""
    monday = TODAY - timedelta(days=TODAY.weekday())
    skipped = monday + timedelta(days=7)
    body = ics(
        event(
            "weekly",
            stamp(monday, 19),
            stamp(monday, 21),
            "Five a side",
            [
                "RRULE:FREQ=WEEKLY;BYDAY=MO",
                f"EXDATE;TZID=Asia/Dubai:{stamp(skipped, 19)}",
            ],
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    assert skipped not in {r["local_date"] for r in stored(conn)}


def test_an_all_day_event_blocks_the_whole_day(conn, one_feed) -> None:
    """A bare DATE has no end time; a zero length block at midnight is wrong."""
    body = ics(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:holiday",
                f"DTSTART;VALUE=DATE:{TODAY:%Y%m%d}",
                f"DTEND;VALUE=DATE:{TODAY + timedelta(days=1):%Y%m%d}",
                "SUMMARY:Flight to London",
                "END:VEVENT",
            ]
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    row = stored(conn)[0]
    assert row["all_day"]
    assert (row["ends_at"] - row["starts_at"]) >= timedelta(days=1)
    assert availability.evening_blocked_minutes(conn, TODAY, DUBAI) >= 300


def test_a_floating_time_is_read_in_the_athletes_zone(conn, one_feed) -> None:
    """TZ-03: the configured zone governs, whatever the feed says or omits."""
    body = ics(
        "\r\n".join(
            [
                "BEGIN:VEVENT",
                "UID:floating",
                f"DTSTART:{stamp(TODAY, 18)}",
                f"DTEND:{stamp(TODAY, 19)}",
                "SUMMARY:No timezone on this one",
                "END:VEVENT",
            ]
        )
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    row = stored(conn)[0]
    assert row["starts_at"].astimezone(DUBAI).hour == 18
    assert row["local_date"] == TODAY


# --- CALR-02: cadence, horizon and idempotency -------------------------------


def test_re_fetching_rewrites_rather_than_appends(conn, one_feed) -> None:
    body = ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 19, 30)))
    feed.sync(conn, DUBAI, TODAY, client=transport(body))
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    assert len(stored(conn)) == 1


def test_an_event_removed_upstream_stops_blocking(conn, one_feed) -> None:
    """Without the delete, CALR-04 keeps blocking time for a dead meeting."""
    two = ics(
        event("keep", stamp(TODAY, 18), stamp(TODAY, 19)),
        event("gone", stamp(TODAY, 20), stamp(TODAY, 21)),
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(two))
    assert len(stored(conn)) == 2

    feed.sync(
        conn, DUBAI, TODAY, client=transport(ics(event("keep", stamp(TODAY, 18), stamp(TODAY, 19))))
    )
    assert [r["uid"] for r in stored(conn)] == ["keep"]


def test_a_failed_fetch_does_not_empty_the_calendar(conn, one_feed) -> None:
    """An outage read as "no commitments" would hand the scheduler a free week."""
    feed.sync(
        conn, DUBAI, TODAY, client=transport(ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 19))))
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(None))

    assert len(stored(conn)) == 1


def test_history_outside_the_window_survives_a_fetch(conn, one_feed) -> None:
    """CALR-03 needs weeks that have happened; a forward-only sweep deletes them.

    On the *configured* feed. This used to hang the old event off a feed id that
    was never in `CALENDAR_ICS_URLS`, which tested the window rule through a
    second rule that has since changed: an unconfigured feed is now removed
    outright, so the fixture would have proved the window rule by relying on a
    feed nothing had subscribed to. See `test_a_feed_no_longer_configured_is_forgotten`.
    """
    old = TODAY - timedelta(days=60)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into calendar_feeds (id, name, url_fingerprint, position) values "
            "(%s, 'Work', 'x', 0) on conflict do nothing",
            (feed.feed_id(SECRET_URL),),
        )
        cur.execute(
            "insert into calendar_events (feed, uid, summary, starts_at, ends_at, "
            "local_date, busy) values (%s, 'ancient', 'Old', %s, %s, %s, true)",
            (
                feed.feed_id(SECRET_URL),
                datetime.combine(old, datetime.min.time(), tzinfo=DUBAI),
                datetime.combine(old, datetime.min.time(), tzinfo=DUBAI) + timedelta(hours=1),
                old,
            ),
        )

    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))
    assert "ancient" in {r["uid"] for r in stored(conn)}


def test_the_fetch_history_records_every_attempt(conn, one_feed) -> None:
    """CALR-02: "fetch history shows no gap longer than 6 hours" needs a history."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))
    feed.sync(conn, DUBAI, TODAY, client=transport(None))

    with conn.cursor() as cur:
        cur.execute("select ok, error from calendar_fetches order by started_at")
        rows = cur.fetchall()
    assert [r["ok"] for r in rows] == [True, False]
    assert rows[1]["error"]


def test_the_cadence_is_configurable_and_floored(monkeypatch) -> None:
    assert feed.interval_s() == feed.DEFAULT_INTERVAL_S
    monkeypatch.setenv("COACH_CALENDAR_INTERVAL_S", "3600")
    assert feed.interval_s() == 3600
    monkeypatch.setenv("COACH_CALENDAR_INTERVAL_S", "5")
    assert feed.interval_s() == 900


def test_the_horizon_reaches_three_weeks_forward(conn, one_feed) -> None:
    """CALR-02: a rolling 21 day horizon."""
    far = TODAY + timedelta(days=20)
    beyond = TODAY + timedelta(days=40)
    body = ics(
        event("soon", stamp(far, 18), stamp(far, 19)),
        event("later", stamp(beyond, 18), stamp(beyond, 19)),
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    assert {r["uid"] for r in stored(conn)} == {"soon"}


def test_the_feed_health_row_tracks_success(conn, one_feed) -> None:
    """OBS-05: the calendar feed is one of the five carrying a threshold."""
    feed.sync(conn, DUBAI, TODAY, client=transport(None))
    with conn.cursor() as cur:
        cur.execute("select last_success_at, last_error from feeds where name = 'calendar'")
        row = cur.fetchone()
    assert row["last_success_at"] is None and row["last_error"]

    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))
    with conn.cursor() as cur:
        cur.execute("select last_success_at, last_error from feeds where name = 'calendar'")
        row = cur.fetchone()
    assert row["last_success_at"] is not None and row["last_error"] is None


# --- CALR-03: observed availability through consolidation ---------------------


def busy_evenings(weekday: int, weeks: int, hours: tuple[int, int] = (18, 21)) -> list[str]:
    """One evening commitment on the same weekday, `weeks` weeks running."""
    anchor = TODAY - timedelta(days=(TODAY.weekday() - weekday) % 7)
    events = []
    for week in range(weeks):
        day = anchor - timedelta(weeks=week)
        events.append(
            event(f"{weekday}-{week}", stamp(day, hours[0]), stamp(day, hours[1]), "Commitment")
        )
    return events


def test_three_evening_commitments_produce_an_observed_proposal(conn, one_feed) -> None:
    """CALR-03's acceptance: a week with evening commitments updates availability."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(*busy_evenings(weekday=2, weeks=3))))
    queued = availability.observe(conn, TODAY, DUBAI)

    assert queued
    with conn.cursor() as cur:
        cur.execute("select proposal, origin, status from pending_writes order by id")
        rows = cur.fetchall()

    blackouts = [r for r in rows if r["proposal"]["key"] == "availability.blackouts"]
    assert blackouts
    assert "wednesday" in blackouts[0]["proposal"]["value"]
    assert blackouts[0]["proposal"]["provenance"] == "observed"
    assert blackouts[0]["origin"] == "feed"


def test_the_proposal_is_not_a_fact(conn, one_feed) -> None:
    """CONS-06: only the SAFE-06 path writes outside consolidation.

    An observation from a feed is exactly the kind of evidence the conflict
    matrix exists to arbitrate, so it waits for the night.
    """
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(*busy_evenings(weekday=2, weeks=3))))
    availability.observe(conn, TODAY, DUBAI)

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from facts where key like 'availability%'")
        assert cur.fetchone()["n"] == 0
        cur.execute("select status from pending_writes")
        assert all(r["status"] == "pending" for r in cur.fetchall())


def test_a_single_occurrence_is_not_a_pattern(conn, one_feed) -> None:
    """One Tuesday is a Tuesday."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(*busy_evenings(weekday=2, weeks=1))))
    availability.observe(conn, TODAY, DUBAI)

    with conn.cursor() as cur:
        cur.execute(
            "select proposal from pending_writes where proposal->>'key' = 'availability.blackouts'"
        )
        for row in cur.fetchall():
            assert "wednesday" not in row["proposal"]["value"]


def test_a_short_call_does_not_cost_the_evening(conn, one_feed) -> None:
    """Thirty minutes at six is not a lost training session."""
    anchor = TODAY - timedelta(days=(TODAY.weekday() - 2) % 7)
    events = [
        event(
            f"short-{w}",
            stamp(anchor - timedelta(weeks=w), 18),
            stamp(anchor - timedelta(weeks=w), 18, 30),
        )
        for w in range(3)
    ]
    feed.sync(conn, DUBAI, TODAY, client=transport(ics(*events)))

    patterns = {
        p.name: p
        for p in availability.weekday_patterns(conn, TODAY - timedelta(days=28), TODAY, DUBAI)
    }
    assert not patterns["wednesday"].usually_blocked


def test_overlapping_meetings_are_not_counted_twice(conn, one_feed) -> None:
    """Two calls at the same time consume one evening, not two."""
    body = ics(
        event("a", stamp(TODAY, 18), stamp(TODAY, 19)),
        event("b", stamp(TODAY, 18, 15), stamp(TODAY, 18, 45)),
    )
    feed.sync(conn, DUBAI, TODAY, client=transport(body))

    assert availability.evening_blocked_minutes(conn, TODAY, DUBAI) == 60


def test_no_calendar_data_produces_no_proposal(conn) -> None:
    """CALR-05: an unread feed is not a clear diary."""
    assert availability.observe(conn, TODAY, DUBAI) == []
    assert availability.weekday_patterns(conn, TODAY - timedelta(days=28), TODAY, DUBAI) == []


# --- CALR-05: lag is expected -------------------------------------------------


def test_the_context_says_what_was_published_not_what_is_true(conn, one_feed) -> None:
    """CALR-05: a commitment added today and absent from the feed must not
    cause a false claim of availability."""
    feed.sync(
        conn,
        DUBAI,
        TODAY,
        client=transport(ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 20), "Dinner"))),
    )

    rendered = availability.context(conn, TODAY, DUBAI)
    assert "Dinner" in rendered
    assert "lags" in rendered
    assert "confirm" in rendered


def test_an_empty_calendar_is_hedged_rather_than_asserted(conn, one_feed) -> None:
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))

    rendered = availability.context(conn, TODAY, DUBAI)
    assert "cache" in rendered
    assert "clear diary" in rendered


def test_an_unread_calendar_says_nothing_at_all(conn) -> None:
    """With no fetch ever, there is nothing to hedge — the feed is simply absent,
    which CHAT-09's staleness block already covers."""
    assert availability.context(conn, TODAY, DUBAI) == ""


def test_the_calendar_reaches_the_prompt(conn, one_feed) -> None:
    feed.sync(
        conn, DUBAI, TODAY, client=transport(ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 20))))
    )

    assembled = prompt.assemble(conn, datetime(2026, 7, 28, 9, 0, tzinfo=DUBAI), tz=DUBAI)
    assert "calendar" in assembled.names()
    assert "THE WEEK AHEAD" in assembled.render()


# --- CHAT-06: the tool ---------------------------------------------------------


def test_get_calendar_is_no_longer_deferred(conn, one_feed, monkeypatch) -> None:
    monkeypatch.setenv("COACH_TZ", "Asia/Dubai")
    feed.sync(
        conn,
        DUBAI,
        TODAY,
        client=transport(ics(event("e1", stamp(TODAY, 18), stamp(TODAY, 20), "Dinner"))),
    )

    result = tools.dispatch(
        conn,
        "get_calendar",
        {"since": TODAY.isoformat(), "until": (TODAY + timedelta(days=7)).isoformat()},
    )

    assert "get_calendar" not in tools.DEFERRED
    assert result["busy"][0]["summary"] == "Dinner"
    assert "cache" in result["caveat"]


# --- PLAN-08: no write path to Google -----------------------------------------


def test_no_write_path_to_google_exists() -> None:
    """PLAN-08's acceptance, scanned rather than asserted by inspection."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        for lineno, text in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.(post|put|patch|delete)\s*\(", text) and re.search(
                r"google|calendar|ical", text, re.I
            ):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {text.strip()}")
    assert not offenders, offenders


def test_the_calendar_client_only_reads() -> None:
    """The module exposes no function that could write an event upstream."""
    source = (REPO / "src" / "coach" / "calendars" / "feed.py").read_text()
    assert "http.get(" in source
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source


# --- The per-feed row, which was never written -------------------------------


def test_a_fetch_records_itself_on_the_feed_row(conn, one_feed) -> None:
    """`record_fetch` keyed the update on the name and was handed the id.

    A feed's name is what it is *called* — X-WR-CALNAME, or `calendar-N` when the
    fetch failed and there was no document to read one from. The id is a hash of
    the URL. They agree essentially never, so `last_fetch_at`, `last_success_at`
    and `last_error` were blank for every feed forever, while `calendar_fetches`
    beside them filled up correctly. The live deployment showed exactly that: a
    feed that had demonstrably failed, with nothing on its row to say so.
    """
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))

    with conn.cursor() as cur:
        cur.execute("select last_fetch_at, last_success_at, last_error from calendar_feeds")
        row = cur.fetchone()
    assert row["last_fetch_at"] is not None, "the fetch left no trace on the feed"
    assert row["last_success_at"] is not None
    assert row["last_error"] is None


def test_a_failure_lands_on_the_feed_row_and_keeps_the_last_success(conn, one_feed) -> None:
    """An outage must not erase the fact that the feed once worked."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))
    with conn.cursor() as cur:
        cur.execute("select last_success_at from calendar_feeds")
        succeeded_at = cur.fetchone()["last_success_at"]

    feed.sync(conn, DUBAI, TODAY, client=transport(None))

    with conn.cursor() as cur:
        cur.execute("select last_success_at, last_error from calendar_feeds")
        row = cur.fetchone()
    assert row["last_error"] is not None, "the failure left no trace on the feed"
    assert row["last_success_at"] == succeeded_at, "a failure erased the last success"
    assert "abc123secret" not in row["last_error"]


# --- What the error is allowed to say ----------------------------------------


def test_a_web_page_says_so_rather_than_naming_an_exception(conn, one_feed) -> None:
    """The live failure, exactly: a sign-in page served with HTTP 200.

    Google's share menu offers four addresses and only one is the iCal one. The
    old text — "could not parse (ValueError)" — was true and told the athlete
    nothing he could act on.
    """
    page = b'<!doctype html><html lang="en-US"><head><base href="https://accounts.google.com/">'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    results = feed.sync(
        conn, DUBAI, TODAY, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert not results[0].ok
    assert "text/html" in results[0].error
    assert ".ics" in results[0].error, "the error should name the address to look for"
    assert "abc123secret" not in results[0].error
    assert "accounts.google.com" not in results[0].error, "CALR-06: no URL, not even theirs"


# --- webcal:// is what every calendar app hands you ---------------------------


def test_a_webcal_url_is_fetched_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """iCloud's "Public Calendar" share gives webcal:// and nothing else.

    httpx refuses the scheme outright, so an Apple calendar produced
    `UnsupportedProtocol` and no events. It is https with a scheme that tells the
    OS to subscribe rather than browse.
    """
    monkeypatch.setenv(
        "CALENDAR_ICS_URLS", "webcal://p01-caldav.icloud.com/published/2/abc123secret"
    )
    urls = feed.configured_urls()
    assert urls == ["https://p01-caldav.icloud.com/published/2/abc123secret"]


def test_the_scheme_does_not_change_a_feeds_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pasting the same calendar either way must not make it two feeds."""
    monkeypatch.setenv("CALENDAR_ICS_URLS", "webcal://example.com/cal.ics")
    as_webcal = feed.feed_id(feed.configured_urls()[0])
    monkeypatch.setenv("CALENDAR_ICS_URLS", "https://example.com/cal.ics")
    assert feed.feed_id(feed.configured_urls()[0]) == as_webcal


def test_an_ordinary_https_url_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALENDAR_ICS_URLS", SECRET_URL)
    assert feed.configured_urls() == [SECRET_URL]


# --- Feeds that are no longer configured -------------------------------------


def _orphan(conn: psycopg.Connection, feed_id: str = "old-feed") -> None:
    """A feed the athlete used to subscribe to, with busy time it published."""
    when = datetime.combine(TODAY, datetime.min.time(), tzinfo=DUBAI) + timedelta(hours=18)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into calendar_feeds (id, name, url_fingerprint, position) "
            "values (%s, 'Old work', %s, 1)",
            (feed_id, f"fingerprint-{feed_id}"),
        )
        cur.execute(
            "insert into calendar_events (feed, uid, summary, starts_at, ends_at, "
            "local_date, busy) values (%s, 'stale', 'Standup', %s, %s, %s, true)",
            (feed_id, when, when + timedelta(hours=1), TODAY),
        )


def test_a_feed_no_longer_configured_is_forgotten(conn, one_feed) -> None:
    """Swap one calendar for another and the old one's meetings used to stay.

    `store` only refreshes feeds that are still configured, so occurrences from a
    dropped feed were never revisited and never expired. PLAN-04 went on
    scheduling around meetings from a calendar the athlete had unsubscribed from,
    with nothing in the system able to say where they came from.
    """
    _orphan(conn)
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))

    with conn.cursor() as cur:
        cur.execute("select id from calendar_feeds")
        assert [r["id"] for r in cur.fetchall()] == [feed.feed_id(SECRET_URL)]
    assert "stale" not in {r["uid"] for r in stored(conn)}, "the old calendar still blocks time"


def test_the_configured_feed_is_not_pruned_when_its_fetch_fails(conn, one_feed) -> None:
    """Configured is configured. An outage is not an unsubscription."""
    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))
    feed.sync(conn, DUBAI, TODAY, client=transport(None))

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from calendar_feeds")
        assert cur.fetchone()["n"] == 1


def test_an_empty_configuration_prunes_nothing(conn, monkeypatch) -> None:
    """The dangerous direction, and the reason this is not a plain reconcile.

    A compose file that loses the variable, an `.env` that fails to load, a typo
    in the name: each reads as zero configured feeds. Treating that as "he
    unsubscribed from everything" would delete every calendar he has on a
    configuration slip. Stale busy time is recoverable; this would not be.
    """
    _orphan(conn)
    monkeypatch.delenv("CALENDAR_ICS_URLS", raising=False)

    assert feed.sync(conn, DUBAI, TODAY) == []

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from calendar_feeds")
        assert cur.fetchone()["n"] == 1, "an unset variable deleted the athlete's calendars"


def test_pruning_takes_the_fetch_history_with_it(conn, one_feed) -> None:
    """History for a feed nothing can name again is the orphan being removed."""
    _orphan(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("insert into calendar_fetches (feed, ok, events) values ('old-feed', true, 3)")

    feed.sync(conn, DUBAI, TODAY, client=transport(ics()))

    with conn.cursor() as cur:
        cur.execute("select count(*) as n from calendar_fetches where feed = 'old-feed'")
        assert cur.fetchone()["n"] == 0
