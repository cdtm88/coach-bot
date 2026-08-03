"""The ingest process: three loops and an HTTP route.

* `poller` — the primary ingest path. Asks intervals.icu what is new and scans
  the watched folder, every `COACH_POLL_INTERVAL_S`. Without a registered app
  there is no webhook, so nothing pushes a ride at us and PERF-03's budget is met
  by asking often enough.
* `sweeper` — ages out prescriptions nothing satisfied, every
  `COACH_SWEEP_INTERVAL_S`. Separate because an 18 hour grace window has nothing
  to say to a question asked every two minutes.
* `worker` — drains the webhook queue. Idle unless a webhook is configured.
* `wellness_poller` — reads the intervals.icu wellness feed, which is where body
  mass (HLTH-04) and recovery (RECOV-01) arrive from. Hourly by default: the
  feed is written by a phone syncing overnight, so asking every two minutes
  would spend rate limit on an answer that changes once a day.
* `calendar_poller` — reads the secret iCal feeds (CALR-01), six hourly per
  CALR-02. Nothing to do with intervals.icu and no API key involved; it runs
  here because this is the process that owns the inbound feeds.
* The two routes, which only ever acknowledge.

That last point is not an optimisation. intervals.icu retries any non-2xx with
exponential backoff and treats a slow response as a failure, so if the webhook is
ever switched on, every download, parse and review has to happen after the
response is already on the wire.

The server is `http.server` from the standard library rather than a framework.
This endpoint serves exactly one caller, behind a tunnel, on one route, and its
entire job is to hand a JSON body to :mod:`coach.ingest.service` and return a
status code. A framework would add a dependency and a configuration surface
without adding anything this has to do.

SEC-02 lives one layer down: nothing here decides whether a payload is genuine,
because the check belongs with the replay record it is paired with. The handler
cannot accidentally skip it — there is no path from a request into the queue that
does not go through :func:`coach.ingest.service.receive`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import clock
from coach.calendars import availability as calavailmod
from coach.calendars import feed as calfeedmod
from coach.health import macros as macromod
from coach.health import wellness as wellnessmod
from coach.ingest import client as clientmod
from coach.ingest import reconcile as reconcilemod
from coach.ingest import service
from coach.ingest import webhook as webhookmod
from coach.notify import charts as chartmod
from coach.plans import sync as plansyncmod

log = logging.getLogger(__name__)

ROUTE = "/webhook/intervals"

# HLTH-01. The only other route the tunnel exposes, and the only one MacroLog
# knows about. Named for its client rather than for its payload, because the next
# thing MacroLog posts will be posted to a sibling of this rather than folded in.
MACRO_ROUTE = "/macrolog/meals"

# The wellness feed changes once a day, so it is read on its own slow clock
# rather than on the two minute activity poll.
DEFAULT_WELLNESS_INTERVAL_S = 3600

# `db.connect` is a context manager factory; every caller here opens one
# connection per request or per tick and lets the exit commit it.
Connect = Callable[[], AbstractContextManager[psycopg.Connection]]

# Nothing legitimate is anywhere near this large; the largest real payload is a
# handful of events. Reading an unbounded Content-Length from the network would
# be a memory exhaustion hole regardless of who is meant to be calling.
MAX_BODY_BYTES = 1 << 20


def make_handler(
    connect: Connect,
    wake: Callable[[], None] = lambda: None,
    tz: ZoneInfo | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to its dependencies.

    Note what it does *not* take: no API client, no note writer. The handler
    cannot reach upstream or call a model even by accident, because it holds
    nothing capable of it. That is PERF-03 enforced structurally rather than by
    remembering to keep the route thin. The timezone is the one addition and it
    is inert — TZ-01 needs it to decide which local day a meal belongs to, and a
    ZoneInfo cannot make a network call.

    `wake` nudges the worker so a queued delivery is picked up immediately rather
    than on the next scheduled pass. Latency comes from that nudge; correctness
    does not depend on it.
    """
    zone = tz or ZoneInfo(os.environ.get("COACH_TZ", "UTC"))

    class Handler(BaseHTTPRequestHandler):
        server_version = "coach-bot"

        def log_message(self, format: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), format % args)

        def _reply(self, code: int, body: dict[str, Any], close: bool = False) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if close:
                # The refusals below answer without draining the request body, so
                # the connection cannot be reused — there are unread bytes on it
                # that would be read as the next request line.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict[str, Any] | None:
            """Read and parse the request body, or answer and return None."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reply(400, {"error": "bad Content-Length"})
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                # Ruled on from the header alone, before a byte of the body is
                # read. Reading it first to answer politely is the exhaustion the
                # limit exists to prevent.
                self._reply(413, {"error": "body missing or too large"}, close=True)
                return None

            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, {"error": "body is not JSON"})
                return None
            if not isinstance(payload, dict):
                self._reply(400, {"error": "body is not an object"})
                return None
            return payload

        def do_POST(self) -> None:  # noqa: N802 - the stdlib's naming, not ours
            route = self.path.rstrip("/")
            if route == MACRO_ROUTE:
                self._macros()
                return
            if route != ROUTE:
                self._reply(404, {"error": "no such route"})
                return

            payload = self._body()
            if payload is None:
                return

            try:
                with connect() as conn:
                    queued = service.receive(conn, payload)
            except webhookmod.Rejected as exc:
                # SEC-02. Deliberately uninformative: a caller that failed the
                # secret check learns only that it failed.
                log.warning("rejected webhook: %s", exc)
                self._reply(401, {"error": "rejected"})
                return
            except Exception:
                # A 500 makes intervals.icu retry with backoff. Safe, because
                # nothing was queued: the enqueue is the only write on this path.
                log.exception("webhook enqueue failed")
                self._reply(500, {"error": "internal error"})
                return

            # PERF-03: answered without a network call or a model call having
            # happened. Everything downloaded, parsed and reviewed is the
            # drain's job, and it runs after this response is on the wire.
            self._reply(200, {"queued": queued})
            wake()

        def _macros(self) -> None:
            """HLTH-01: MacroLog posts meals here.

            Unlike the webhook route this one does the work inline. It is a
            database write with no upstream call and no model call in it, so
            there is nothing for a queue to buy — and MacroLog is our own client
            on a phone, which would rather be told the meal landed than be told
            it was queued.
            """
            payload = self._body()
            if payload is None:
                return

            try:
                macromod.verify(self.headers.get(macromod.SECRET_HEADER))
            except macromod.Rejected as exc:
                # SEC-02. Deliberately uninformative, as on the webhook route.
                log.warning("rejected macro payload: %s", exc)
                self._reply(401, {"error": "rejected"})
                return

            try:
                with connect() as conn:
                    result = macromod.apply(conn, payload, zone)
            except macromod.Malformed as exc:
                # Authenticated but unusable. Answered distinctly from a failed
                # secret so a client with a correct secret is not sent looking
                # for a credential problem it does not have.
                self._reply(400, {"error": str(exc)})
                return
            except Exception:
                log.exception("macro ingest failed")
                self._reply(500, {"error": "internal error"})
                return

            self._reply(
                200,
                {
                    "stored": result.stored,
                    "updated": result.updated,
                    "deleted": result.deleted,
                    "errors": result.errors,
                },
            )

        def _reply_html(self, code: int, body: str) -> None:
            payload = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming, not ours
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/health":
                self._reply(200, {"ok": True})
                return

            # NOTIF-04: charts are pages, not chat embeds. Unauthenticated on
            # purpose — the page shows load figures and a trend slope with no
            # name and no identifiers, and a link the athlete has to authenticate
            # to open is a link they will not open from a phone.
            if path.startswith("/charts/"):
                kind = path.rsplit("/", 1)[-1]
                today = clock.local_day(datetime.now(UTC), zone)
                try:
                    with connect() as conn:
                        code, body = chartmod.page(conn, kind, today)
                except Exception:
                    log.exception("chart %s failed", kind)
                    self._reply_html(500, "<!doctype html><p>Could not draw that.")
                    return
                self._reply_html(code, body)
                return

            self._reply(404, {"error": "no such route"})

    return Handler


def serve(
    connect: Connect,
    wake: Callable[[], None] = lambda: None,
    host: str = "127.0.0.1",
    port: int = 8080,
    tz: ZoneInfo | None = None,
) -> ThreadingHTTPServer:
    """Bind and return the server without serving. The caller decides the loop.

    Loopback by default, because that is the safe answer when this runs directly
    on a host: the tunnel is what makes the routes reachable and binding wider
    would only widen the exposure.

    **Under compose it has to be `0.0.0.0`, and that is not a loosening.**
    `cloudflared` is a separate container with its own network namespace, so the
    coach's loopback is not the tunnel's — bound to 127.0.0.1 inside the
    container, the routes are reachable by nothing at all and MacroLog's posts
    would never arrive. What keeps the exposure narrow there is the absence of a
    `ports:` stanza: 0.0.0.0 inside a container with no published port means the
    compose network and nowhere else. `COACH_INGEST_HOST` is how the deployment
    says so, and `docker-compose.yml` sets it with that reasoning attached.
    """
    return ThreadingHTTPServer((host, port), make_handler(connect, wake, tz))


def worker(
    connect: Connect,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str],
    stop: threading.Event,
    nudge: threading.Event,
    idle_s: float = 30.0,
) -> None:
    """Drain the delivery queue. The half of ingest that is allowed to be slow.

    Woken by the HTTP handler so a ride is processed seconds after it lands, and
    otherwise polling on `idle_s` so a delivery queued while this was busy is
    still picked up without a second nudge.
    """
    while not stop.is_set():
        nudge.wait(idle_s)
        nudge.clear()
        if stop.is_set():
            break
        try:
            with connect() as conn:
                handled = service.drain(conn, client, tz, write_note)
            if handled:
                log.info("drained %d deliveries", len(handled))
        except Exception:
            log.exception("drain failed; the queue keeps the work for the next pass")


def _loop(
    name: str, stop: threading.Event, interval_s: int, body: Callable[[], dict[str, Any]]
) -> None:
    """Run `body` on a fixed cadence until `stop` is set.

    The interval is measured from the start of each pass, so a slow pass shortens
    the wait rather than adding to it and the cadence does not drift.

    One failure must never end the loop. Without a webhook these loops are the
    only thing that notices a new ride, so a loop that exits on a transient error
    is the coach going quiet with nothing to say why.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = body()
            if any(result.values()):
                log.info("%s: %s", name, result)
        except Exception:
            log.exception("%s failed; retrying next interval", name)
        stop.wait(max(1.0, interval_s - (time.monotonic() - started)))


def poller(
    connect: Connect,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str],
    stop: threading.Event,
    interval_s: int | None = None,
    adjust: bool = False,
    send: Callable[[str], None] | None = None,
) -> None:
    """The fast loop: find new activities and new files, match and review them.

    This is the primary ingest path, not a backstop. With no registered app there
    is no webhook, so nothing pushes a new ride at us and PERF-03's five minute
    budget has to be met by asking often enough.

    `adjust` is what turns P09 on, and it is a parameter rather than a constant
    because a backfill must not restructure anything and because running the
    loop without it is how the rules are debugged. `main` passes True; see
    `service.finish` for what runs and in what order.
    """
    every = interval_s if interval_s is not None else reconcilemod.poll_interval_s()
    log.info("polling every %ds, adjustments %s", every, "on" if adjust else "off")

    def once() -> dict[str, Any]:
        with connect() as conn:
            return service.poll(conn, client, tz, write_note, adjust=adjust, send=send)

    _loop("poll", stop, every, once)


def sweeper(
    connect: Connect,
    tz: ZoneInfo,
    stop: threading.Event,
    interval_s: int | None = None,
) -> None:
    """The slow loop: age out prescriptions nothing satisfied (FIT-12).

    Separate from the poll because an 18 hour grace window has nothing to say to
    a question asked every two minutes.
    """
    every = interval_s if interval_s is not None else reconcilemod.sweep_interval_s()

    def once() -> dict[str, Any]:
        with connect() as conn:
            return service.sweep(conn, tz, datetime.now(UTC))

    _loop("sweep", stop, every, once)


def wellness_poller(
    connect: Connect,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    stop: threading.Event,
    interval_s: int | None = None,
) -> None:
    """Read wellness on its own clock (HLTH-04, RECOV-01).

    Slower than the activity poll on purpose. The feed is written by a phone
    syncing overnight and by a Whoop link that publishes once a day, so a two
    minute cadence would spend rate limit re-reading yesterday. The window is
    wide and the upsert idempotent (RECOV-05), which means a missed pass costs
    nothing and a late-arriving provider fill-in is picked up regardless.
    """
    every = interval_s if interval_s is not None else _wellness_interval_s()

    def once() -> dict[str, Any]:
        with connect() as conn:
            synced = wellnessmod.sync(conn, client, wellnessmod.local_today(tz))
        return {
            "wellness_days": synced.days,
            "body_mass_readings": synced.readings,
            "held_for_confirmation": synced.held,
            "errors": synced.errors,
        }

    _loop("wellness", stop, every, once)


def _wellness_interval_s() -> int:
    return reconcilemod.env_interval(
        "COACH_WELLNESS_INTERVAL_S", DEFAULT_WELLNESS_INTERVAL_S, floor=300
    )


def calendar_poller(
    connect: Connect,
    tz: ZoneInfo,
    stop: threading.Event,
    interval_s: int | None = None,
) -> None:
    """Read the calendar feeds and derive observed availability (CALR-02, CALR-03).

    Six hourly by default. Google publishes these on a cache, so asking more
    often buys nothing — CALR-05 is written on the assumption that the lag is
    survived rather than defeated.
    """
    every = interval_s if interval_s is not None else calfeedmod.interval_s()

    def once() -> dict[str, Any]:
        today = wellnessmod.local_today(tz)
        with connect() as conn:
            results = calfeedmod.sync(conn, tz, today)
            # CALR-03: proposals, not writes. Consolidation ratifies them.
            queued = calavailmod.observe(conn, today, tz)
        return {
            "feeds": len(results),
            "events": sum(len(r.occurrences) for r in results),
            "failed": [r.feed for r in results if not r.ok],
            "availability_proposals": len(queued),
        }

    _loop("calendar", stop, every, once)


def plan_poller(
    connect: Connect,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    stop: threading.Event,
    interval_s: int | None = None,
) -> None:
    """Notice athlete edits to the planned calendar (PLAN-06, PLAN-12).

    Its own clock, and a slow one. This is the only loop reading a calendar the
    athlete edits by hand, and PLAN-12's acceptance is "within one sync" rather
    than immediately — a session moved this afternoon does not need to be
    reconciled this minute, and the push notification that would tell us sooner
    needs a registered application, which SEC-04 rules out.

    Deliberately read-only upstream. Publishing is a block-change action and the
    sweep is the nightly pass's; a loop that both read and wrote this calendar
    could fight with the athlete inside a single interval.
    """
    every = interval_s if interval_s is not None else plansyncmod.interval_s()

    def once() -> dict[str, Any]:
        with connect() as conn:
            result = plansyncmod.run(conn, client, datetime.now(UTC), tz)
        return {
            "edits": result.count,
            "cancelled": len(result.deleted_upstream),
            "availability_proposals": len(result.queued),
        }

    _loop("plans", stop, every, once)


def _notifier() -> Callable[[str], None] | None:
    """ADJ-06's sender, bound to the allowlisted chat, or nothing.

    Returns a one-argument callable so `adjust.apply.execute` keeps its existing
    signature and never learns that Telegram exists. What it is bound to is an
    `Outbox`, so the notice is recorded as something the coach said before it is
    sent, and `period_key` is left unset: an adjustment is event-driven and its
    idempotency is `adjustment_events.announced`, not a once-per-day claim.
    """
    try:
        from coach.notify import outbox as outboxmod
        from coach.runtime import transport
        from coach.telegram import bot as botmod

        allowlist = botmod.Allowlist()
        telegram = transport.Telegram()
    except Exception as exc:  # noqa: BLE001 - the ride matters more than the message
        log.warning("ADJ-06 notices not wired: %s", exc)
        return None

    box = outboxmod.Outbox(lambda text: telegram.send(allowlist.chat_id, text))

    def send(text: str) -> None:
        # A connection per notice rather than one held open: this is called from
        # inside a poll pass that already has one, but `execute` is given a
        # transport rather than a connection by design, and reaching back for
        # the caller's would make the seam worse than the extra connect.
        from coach import db

        with db.connect() as conn:
            box.send(conn, text, kind="adjustment")

    return send


def main() -> None:
    """Run the webhook route and the backstop loop together."""
    from coach import db

    logging.basicConfig(level=os.environ.get("COACH_LOG_LEVEL", "INFO"))
    tz = ZoneInfo(os.environ.get("COACH_TZ", "Asia/Dubai"))
    port = int(os.environ.get("COACH_INGEST_PORT", "8080"))

    client = clientmod.Intervals()
    stop = threading.Event()
    nudge = threading.Event()

    # No review writer wired yet: the note generation call belongs to the agent
    # and is threaded through when the two processes are joined. Until then this
    # ingests, matches and adjusts without spending a model call.
    write_note = service.no_review

    # ADJ-06's notice, through the outbox so it lands in `messages` like every
    # other thing the coach says. A bare transport here would repeat the defect
    # PR #38 fixed: the athlete would be told his Thursday had been shortened
    # and the coach would have no record of having said so.
    #
    # Allowed to be absent. A missing Telegram token should cost the notice and
    # not the ingest loop, which is the same reasoning the scheduler applies to
    # its own sender.
    send = _notifier()

    threads = [
        # The webhook drain. Idle unless a webhook is actually configured, which
        # it is not without a registered app; harmless and ready if that changes.
        threading.Thread(
            target=worker,
            args=(db.connect, client, tz, write_note, stop, nudge),
            daemon=True,
            name="drain",
        ),
        # The primary ingest path while there is no webhook, and the only place
        # P09 runs. `adjust=True` appears here and nowhere else in `src/`.
        threading.Thread(
            target=poller,
            args=(db.connect, client, tz, write_note, stop),
            kwargs={"adjust": True, "send": send},
            daemon=True,
            name="poll",
        ),
        threading.Thread(target=sweeper, args=(db.connect, tz, stop), daemon=True, name="sweep"),
        # HLTH-04 and RECOV-01. Its own clock, because the wellness feed changes
        # once a day and the activity poll runs every two minutes.
        threading.Thread(
            target=wellness_poller,
            args=(db.connect, client, tz, stop),
            daemon=True,
            name="wellness",
        ),
        # CALR-01 and CALR-02. No credential beyond the secret URLs themselves.
        threading.Thread(
            target=calendar_poller,
            args=(db.connect, tz, stop),
            daemon=True,
            name="calendar",
        ),
        # PLAN-06 and PLAN-12: the athlete's own edits to the planned calendar.
        # Hourly, and read-only upstream — publishing and the orphan sweep are
        # not this loop's.
        threading.Thread(
            target=plan_poller,
            args=(db.connect, client, tz, stop),
            daemon=True,
            name="plans",
        ),
    ]
    for thread in threads:
        thread.start()

    host = os.environ.get("COACH_INGEST_HOST", "127.0.0.1")
    httpd = serve(db.connect, nudge.set, host=host, port=port, tz=tz)
    log.info("ingest listening on %s:%s, routes %s and %s", host, port, ROUTE, MACRO_ROUTE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        nudge.set()  # release the worker from its wait so it sees the stop
        httpd.server_close()
        client.close()
