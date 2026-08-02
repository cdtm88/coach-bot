"""Fetching and storing busy time from Google's secret iCal feeds.

CALR-01: one URL per calendar, read from the environment. The secret URL is the
whole credential and nothing is negotiated to obtain it — SEC-04's scan forbids
naming the alternative even to say it is absent, exactly as in
:mod:`coach.ingest.client`.
CALR-02: fetched at least every six hours across a rolling 21 day horizon.
CALR-04: declined and cancelled events are excluded from busy time.
CALR-06: the URLs are bearer secrets and never reach a log line — or, here, a
database column, because a secret in a column is a secret in the nightly backup.

**Recurrence is parsed, not approximated.** A weekly commitment is the single
most common shape in a real calendar and it arrives as one VEVENT with an RRULE,
plus EXDATEs for the weeks it was cancelled and RECURRENCE-ID overrides for the
weeks it moved. Expanding that correctly is a solved problem with sharp edges —
DST transitions, EXDATE matching, all-day versus timed — so it uses
`recurring_ical_events` rather than a hand-rolled loop. A scheduler that misses
the athlete's standing Tuesday commitment is worse than no scheduler.

**Errors never carry the URL.** httpx puts the request URL in its exception
messages by default, so every failure path here is caught and re-described
against the feed's display name. That is CALR-06 enforced where it actually
breaks rather than only at the log call.

**And neither do httpx's own log lines.** This one is not ours to be careful
about: `httpx` logs `HTTP Request: GET <url> "HTTP/1.1 200 OK"` at INFO on every
request, so simply using the library publishes the secret to anyone running the
service at debug level. Being disciplined in this module cannot fix a log line
this module does not write, so a redacting filter is installed on that library's
logger instead. Found by the test for CALR-06's acceptance, which is the reason
that acceptance is worded as "no log line" rather than "no log line we wrote".
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import psycopg
import recurring_ical_events

from coach import clock
from coach import feeds as feedmod

log = logging.getLogger(__name__)


class _RedactFeedUrls(logging.Filter):
    """CALR-06: strip a configured feed URL out of any record on this logger.

    A filter rather than raising the library's log level, because silencing
    `httpx` entirely would also hide the intervals.icu request log that is
    genuinely useful and carries no secret — its auth is in a header.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not break logging
            return True
        for url in configured_urls():
            if url and url in message:
                record.msg = message.replace(url, "<calendar feed url redacted>")
                record.args = ()
                message = record.msg
        return True


_REDACTOR = _RedactFeedUrls()


def install_log_redaction() -> None:
    """Attach the redactor to the libraries that log a request URL.

    Idempotent, and called from every entry point that fetches, so a caller
    cannot get the network behaviour without the redaction.
    """
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if _REDACTOR not in logger.filters:
            logger.addFilter(_REDACTOR)


# CALR-02: the rolling horizon and the cadence.
HORIZON_DAYS = 21

# And a look back, which CALR-02 does not mention because it is written from the
# scheduler's point of view. CALR-03 derives *observed* availability from busy
# blocks — "a week with three evening commitments" — and that is a statement
# about weeks that have happened. A purely forward horizon would delete the
# evidence on every fetch and leave consolidation nothing to observe. Four weeks
# back, matching the window every other trend in this system is fitted over.
LOOKBACK_DAYS = 28

DEFAULT_INTERVAL_S = 6 * 3600

# CALR-04. A cancelled occurrence and a declined invitation are not busy time.
# `TRANSPARENT` is the third case the requirement implies without naming: an
# event the athlete marked "free" is on the calendar and does not block, and
# treating it as busy would lose him an evening a week to his own birthday
# reminders.
NOT_BUSY_STATUS = frozenset({"CANCELLED"})
NOT_BUSY_PARTICIPATION = frozenset({"DECLINED"})
FREE_TRANSPARENCY = "TRANSPARENT"


class FeedError(RuntimeError):
    """A feed could not be read. Never carries the URL (CALR-06)."""


@dataclass(frozen=True)
class Occurrence:
    """One block of time, already resolved from any recurrence rule."""

    uid: str
    recurrence_id: str
    summary: str | None
    starts_at: datetime
    ends_at: datetime
    local_date: date
    all_day: bool
    status: str | None
    participation: str | None
    transparency: str | None

    @property
    def busy(self) -> bool:
        """CALR-04: does this occurrence actually block scheduling?"""
        if (self.status or "").upper() in NOT_BUSY_STATUS:
            return False
        if (self.participation or "").upper() in NOT_BUSY_PARTICIPATION:
            return False
        if (self.transparency or "").upper() == FREE_TRANSPARENCY:
            return False
        return True


@dataclass
class Fetched:
    """One feed's result. `feed` is the stable id; `name` is for humans.

    Two fields rather than one because a failed fetch cannot know the calendar's
    name — there is no document to read X-WR-CALNAME from — while the id is
    derived from the URL and is known either way.
    """

    feed: str
    name: str
    occurrences: list[Occurrence] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def configured_urls() -> list[str]:
    """CALR-01: one URL per calendar, comma separated, from the environment.

    Returns the secrets themselves, so nothing may log this and nothing may
    persist it. Every caller in this module treats the return value as
    write-only: it goes into an HTTP client and nowhere else.
    """
    raw = os.environ.get("CALENDAR_ICS_URLS", "")
    return [_https(url.strip()) for url in raw.split(",") if url.strip()]


def _https(url: str) -> str:
    """`webcal://` is `https://` with a scheme that tells the OS to subscribe.

    Every calendar app hands out the webcal form — iCloud's "Public Calendar"
    share gives nothing else — and httpx refuses it as an unsupported protocol,
    which surfaced as `UnsupportedProtocol` and no calendar. Rewritten here
    rather than at the fetch so the feed's identity is the same whichever form
    was pasted; keyed on the raw string, one calendar would be two feeds.
    """
    for scheme in ("webcal://", "webcals://"):
        if url.lower().startswith(scheme):
            return "https://" + url[len(scheme) :]
    return url


def fingerprint(url: str) -> str:
    """A stable identity for a feed that cannot be turned back into its URL."""
    return hashlib.sha256(url.encode()).hexdigest()


def feed_id(url: str) -> str:
    """The primary key for a feed. Derived from the URL, reveals nothing of it."""
    return fingerprint(url)[:16]


def interval_s() -> int:
    """CALR-02's cadence. Floored at 15 minutes; Google publishes on a cache."""
    from coach.ingest import reconcile

    return reconcile.env_interval("COACH_CALENDAR_INTERVAL_S", DEFAULT_INTERVAL_S, 900)


def _display_name(parsed: icalendar.Calendar, position: int) -> str:
    """A human name for the feed, from the calendar itself rather than the URL.

    Google publishes `X-WR-CALNAME` on secret feeds, which is the calendar's own
    label — "Work", "Personal". Falling back to the position keeps a feed
    identifiable when the property is missing without reaching for the one string
    CALR-06 forbids.
    """
    name = parsed.get("X-WR-CALNAME")
    if name:
        cleaned = str(name).strip()
        if cleaned:
            return cleaned[:100]
    return f"calendar-{position + 1}"


def fetch(
    url: str,
    position: int,
    tz: ZoneInfo,
    today: date,
    horizon_days: int = HORIZON_DAYS,
    client: httpx.Client | None = None,
) -> Fetched:
    """Read one feed and expand it across the horizon.

    The URL goes in and nothing derived from it comes out. Both the success and
    the failure paths name the feed, never the address.
    """
    install_log_redaction()
    owned = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=True)
    identity = feed_id(url)
    name = f"calendar-{position + 1}"
    try:
        response = http.get(url)
        if response.status_code >= 400:
            # Deliberately not `response.raise_for_status()`: its message
            # includes the request URL, which is the secret.
            raise FeedError(f"{name}: HTTP {response.status_code}")
        body = response.content
        content_type = (response.headers.get("content-type") or "unknown").split(";")[0].strip()
    except FeedError as exc:
        return Fetched(feed=identity, name=name, error=str(exc))
    except httpx.HTTPError as exc:
        # httpx puts the URL in most of its exception messages. Only the class
        # name survives.
        return Fetched(feed=identity, name=name, error=f"{name}: {type(exc).__name__}")
    finally:
        if owned:
            http.close()

    try:
        parsed = icalendar.Calendar.from_ical(body)
        return Fetched(
            feed=identity,
            name=_display_name(parsed, position),
            occurrences=expand(parsed, tz, today, horizon_days),
        )
    except Exception as exc:  # noqa: BLE001 - one malformed feed must not stop the rest
        # The content type, and a plain sentence when the answer was a web page.
        # "could not parse (ValueError)" is true and unactionable: the live
        # failure was an `accounts.google.com` sign-in page served with HTTP 200,
        # because Google's share menu offers four addresses and only one of them
        # is the iCal one. Neither the type nor that sentence is derived from the
        # URL, so CALR-06 still holds.
        detail = f"{type(exc).__name__}; served {content_type}"
        if "html" in content_type:
            detail += (
                " — that address returns a web page, not a calendar. Use the "
                "secret address in iCal format, which ends in .ics"
            )
        return Fetched(feed=identity, name=name, error=f"{name}: could not parse ({detail})")


def window(
    tz: ZoneInfo, today: date, horizon_days: int = HORIZON_DAYS
) -> tuple[datetime, datetime]:
    """The span a fetch covers: four weeks back, `horizon_days` forward.

    Backward for CALR-03's observed availability, forward for CALR-02 and the
    scheduling PLAN-04 will do with it. It starts at midnight rather than at the
    fetch moment so an event that began this morning and is still running is
    inside it — a scheduler that only saw future events would place a session
    inside a meeting that started an hour ago.
    """
    start = datetime.combine(today - timedelta(days=LOOKBACK_DAYS), time.min, tzinfo=tz)
    end = datetime.combine(today, time.min, tzinfo=tz) + timedelta(days=horizon_days)
    return start, end


def expand(
    parsed: icalendar.Calendar,
    tz: ZoneInfo,
    today: date,
    horizon_days: int = HORIZON_DAYS,
) -> list[Occurrence]:
    """Every occurrence inside the window, recurrence already resolved."""
    start, end = window(tz, today, horizon_days)

    occurrences = []
    for event in recurring_ical_events.of(parsed).between(start, end):
        resolved = _occurrence(event, tz)
        if resolved is not None:
            occurrences.append(resolved)
    return occurrences


def _occurrence(event: Any, tz: ZoneInfo) -> Occurrence | None:
    starts = _moment(event.get("DTSTART"), tz, end_of_day=False)
    ends = _moment(event.get("DTEND") or event.get("DTSTART"), tz, end_of_day=True)
    if starts is None or ends is None:
        return None

    # An all day event arrives as a bare date. It blocks the day, so it is
    # widened to the local day rather than to a zero length block at midnight.
    all_day = not isinstance(getattr(event.get("DTSTART"), "dt", None), datetime)
    if all_day:
        starts = datetime.combine(starts.date(), time.min, tzinfo=tz)
        ends = max(ends, starts + timedelta(days=1))

    uid = str(event.get("UID") or "")
    if not uid:
        return None

    recurrence = event.get("RECURRENCE-ID")
    return Occurrence(
        uid=uid,
        # The instance identity. Using the start rather than the RECURRENCE-ID
        # property, because the expansion produces one event object per instance
        # and only the moved ones carry that property at all.
        recurrence_id=(starts.isoformat() if recurrence is not None or _repeats(event) else ""),
        summary=str(event.get("SUMMARY")) if event.get("SUMMARY") else None,
        starts_at=clock.to_utc(starts),
        ends_at=clock.to_utc(ends),
        local_date=clock.local_day(starts, tz),
        all_day=all_day,
        status=str(event.get("STATUS")) if event.get("STATUS") else None,
        participation=_participation(event),
        transparency=str(event.get("TRANSP")) if event.get("TRANSP") else None,
    )


def _repeats(event: Any) -> bool:
    return any(event.get(key) is not None for key in ("RRULE", "RDATE"))


def _moment(value: Any, tz: ZoneInfo, end_of_day: bool) -> datetime | None:
    """An ICS date or date-time as an aware datetime in the athlete's zone.

    TZ-03: a floating time — one the feed gave no zone for — is read in the
    athlete's configured zone rather than the server's. The configured zone
    governs regardless of what an upstream feed says.
    """
    raw = getattr(value, "dt", None)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=tz)
    moment = datetime.combine(raw, time.min, tzinfo=tz)
    return moment + timedelta(days=1) if end_of_day else moment


def _participation(event: Any) -> str | None:
    """The athlete's own PARTSTAT, for CALR-04.

    A secret iCal feed is one person's view of a calendar, so the first attendee
    carrying a PARTSTAT is his. Google usually omits declined events from the
    export entirely; this catches the cases where it does not rather than
    assuming the common path is the only one.
    """
    attendees = event.get("ATTENDEE")
    if attendees is None:
        return None
    for attendee in attendees if isinstance(attendees, list) else [attendees]:
        status = getattr(attendee, "params", {}).get("PARTSTAT")
        if status:
            return str(status)
    return None


# --- storage -----------------------------------------------------------------


def register(conn: psycopg.Connection, url: str, name: str, position: int) -> None:
    """Record that a feed exists, by fingerprint. The URL is not persisted.

    Upserting on the id rather than the name is what lets a feed be renamed —
    which happens on the first successful fetch after a failed one — without
    becoming a second feed and orphaning its events.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into calendar_feeds (id, name, url_fingerprint, position)
            values (%s, %s, %s, %s)
            on conflict (id) do update set
                name = excluded.name,
                position = excluded.position
            """,
            (feed_id(url), name, fingerprint(url), position),
        )


def store(
    conn: psycopg.Connection,
    feed: str,
    occurrences: list[Occurrence],
    covering: tuple[datetime, datetime] | None = None,
) -> int:
    """Upsert a feed's occurrences, then drop the ones that went away.

    The delete is what makes a cancellation propagate: an occurrence removed
    upstream simply stops appearing in the expansion, and CALR-04 would otherwise
    keep blocking time for a meeting that no longer exists.

    It is bounded by `covering` — the window this fetch actually looked at — so
    that history outside the window survives. Deleting everything the fetch did
    not produce would take CALR-03's evidence with it every six hours.
    """
    stamp = datetime.now(UTC)
    for occurrence in occurrences:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                insert into calendar_events
                    (feed, uid, recurrence_id, summary, starts_at, ends_at, local_date,
                     all_day, status, participation, transparency, busy, fetched_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (feed, uid, recurrence_id) do update set
                    summary = excluded.summary,
                    starts_at = excluded.starts_at,
                    ends_at = excluded.ends_at,
                    local_date = excluded.local_date,
                    all_day = excluded.all_day,
                    status = excluded.status,
                    participation = excluded.participation,
                    transparency = excluded.transparency,
                    busy = excluded.busy,
                    fetched_at = excluded.fetched_at
                """,
                (
                    feed,
                    occurrence.uid,
                    occurrence.recurrence_id,
                    occurrence.summary,
                    occurrence.starts_at,
                    occurrence.ends_at,
                    occurrence.local_date,
                    occurrence.all_day,
                    occurrence.status,
                    occurrence.participation,
                    occurrence.transparency,
                    occurrence.busy,
                    stamp,
                ),
            )

    if covering is not None:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "delete from calendar_events "
                " where feed = %s and starts_at >= %s and starts_at < %s and fetched_at < %s",
                (feed, covering[0], covering[1], stamp),
            )
    return len(occurrences)


def record_fetch(
    conn: psycopg.Connection, feed: str, ok: bool, events: int, error: str | None
) -> None:
    """CALR-02's history, and the per-feed state behind it.

    `feed` is the id, which is what both statements key on. The update used to
    say `where name = %s`, and a name is what a feed is *called* — read from the
    document's X-WR-CALNAME, or `calendar-N` when the fetch failed and there was
    no document to read. The id is a hash of the URL. They agree essentially
    never, so `last_fetch_at`, `last_success_at` and `last_error` were never
    written for any feed, and the one table that exists to say what happened to a
    calendar was permanently blank while the history table beside it filled up.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into calendar_fetches (feed, ok, events, error) values (%s, %s, %s, %s)",
            (feed, ok, events, error),
        )
        cur.execute(
            """
            update calendar_feeds set
                last_fetch_at = now(),
                last_success_at = case when %s then now() else last_success_at end,
                last_error = %s
             where id = %s
            """,
            (ok, error, feed),
        )


def sync(
    conn: psycopg.Connection,
    tz: ZoneInfo,
    today: date,
    horizon_days: int = HORIZON_DAYS,
    client: httpx.Client | None = None,
) -> list[Fetched]:
    """Read every configured feed once. CALR-01 and CALR-02.

    One feed failing does not stop the others: a work calendar being briefly
    unreachable must not lose the coach its view of the athlete's evenings.
    """
    urls = configured_urls()
    results = []
    for position, url in enumerate(urls):
        result = fetch(url, position, tz, today, horizon_days, client)
        register(conn, url, result.name, position)
        # A failed fetch stores nothing and deletes nothing. Treating an outage
        # as "the calendar is now empty" would hand the scheduler a free week.
        stored = (
            store(conn, result.feed, result.occurrences, window(tz, today, horizon_days))
            if result.ok
            else 0
        )
        record_fetch(conn, result.feed, result.ok, stored, result.error)
        if not result.ok:
            log.warning("calendar feed %s failed: %s", result.name, result.error)
        results.append(result)

    removed = prune(conn, [feed_id(url) for url in urls])
    if removed:
        log.info("removed %d calendar feed(s) no longer configured", removed)

    _record_feed_health(conn, results)
    return results


def prune(conn: psycopg.Connection, keep: list[str]) -> int:
    """Forget feeds that are no longer configured, and the busy time they published.

    Nothing used to remove a feed when its URL left `CALENDAR_ICS_URLS`. The row
    stayed, and so did its occurrences — and `store` only ever refreshes feeds
    that are still configured, so those occurrences were never revisited and
    never expired. Swap one calendar for another and PLAN-04 keeps scheduling
    around meetings from a calendar the athlete no longer subscribes to, for
    ever, with nothing in the system able to say where they came from.

    **An empty configuration prunes nothing.** That is the same rule the fetch
    already follows — "a failed fetch stores nothing and deletes nothing" —
    applied one level up. A compose file that loses the variable, an `.env` that
    fails to load, a typo in the name: each of those reads as zero configured
    feeds, and treating that as "the athlete unsubscribed from everything" would
    delete every calendar he has on a configuration slip. Stale busy time is a
    recoverable annoyance; silently discarding the lot is not.

    Cascades to `calendar_events` and `calendar_fetches`, which is CALR-02's
    history for a feed that no longer exists. Losing it is the point rather than
    a cost: the feed is gone, and history for a feed nothing can name again is
    the sort of orphan this function exists to remove.
    """
    if not keep:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from calendar_feeds where not (id = any(%s))", (keep,))
        return cur.rowcount


def _record_feed_health(conn: psycopg.Connection, results: list[Fetched]) -> None:
    """OBS-05: the `calendar` feed is healthy when every configured feed read."""
    if not results:
        return
    if all(result.ok for result in results):
        feedmod.record_success(conn, feedmod.CALENDAR)
    else:
        failed = ", ".join(r.name for r in results if not r.ok)
        feedmod.record_error(conn, feedmod.CALENDAR, f"failed: {failed}")
