"""The ingest process: an HTTP route, a queue worker, and a periodic tick.

FIT-01 needs the first and the last. The webhook is the trigger and the six
hourly reconcile is the backstop for whatever it drops. The worker in between is
what PERF-03 needs: intervals.icu retries any non-2xx with exponential backoff
and treats a slow response as a failure, so the route may only acknowledge, and
every download, parse and review happens after the response is on the wire.

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

from coach.ingest import client as clientmod
from coach.ingest import reconcile as reconcilemod
from coach.ingest import service
from coach.ingest import webhook as webhookmod

log = logging.getLogger(__name__)

ROUTE = "/webhook/intervals"

# `db.connect` is a context manager factory; every caller here opens one
# connection per request or per tick and lets the exit commit it.
Connect = Callable[[], AbstractContextManager[psycopg.Connection]]

# Nothing legitimate is anywhere near this large; the largest real payload is a
# handful of events. Reading an unbounded Content-Length from the network would
# be a memory exhaustion hole regardless of who is meant to be calling.
MAX_BODY_BYTES = 1 << 20


def make_handler(
    connect: Connect, wake: Callable[[], None] = lambda: None
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to its dependencies.

    Note what it does *not* take: no API client, no timezone, no note writer. The
    handler cannot reach upstream or call a model even by accident, because it
    holds nothing capable of it. That is PERF-03 enforced structurally rather
    than by remembering to keep the route thin.

    `wake` nudges the worker so a queued delivery is picked up immediately rather
    than on the next scheduled pass. Latency comes from that nudge; correctness
    does not depend on it.
    """

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

        def do_POST(self) -> None:  # noqa: N802 - the stdlib's naming, not ours
            if self.path.rstrip("/") != ROUTE:
                self._reply(404, {"error": "no such route"})
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reply(400, {"error": "bad Content-Length"})
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                # Ruled on from the header alone, before a byte of the body is
                # read. Reading it first to answer politely is the exhaustion the
                # limit exists to prevent.
                self._reply(413, {"error": "body missing or too large"}, close=True)
                return

            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict):
                self._reply(400, {"error": "body is not an object"})
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

        def do_GET(self) -> None:  # noqa: N802 - the stdlib's naming, not ours
            if self.path.rstrip("/") == "/health":
                self._reply(200, {"ok": True})
                return
            self._reply(404, {"error": "no such route"})

    return Handler


def serve(
    connect: Connect,
    wake: Callable[[], None] = lambda: None,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Bind and return the server without serving. The caller decides the loop.

    Bound to loopback by default: the tunnel is what makes it reachable, so
    binding to every interface would only widen the exposure.
    """
    return ThreadingHTTPServer((host, port), make_handler(connect, wake))


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


def ticker(
    connect: Connect,
    client: clientmod.Intervals,
    tz: ZoneInfo,
    write_note: Callable[[dict[str, Any]], str],
    stop: threading.Event,
    interval_s: int = reconcilemod.INTERVAL_HOURS * 3600,
) -> None:
    """FIT-01's backstop loop. Runs until `stop` is set.

    One tick failing must not end the loop; a reconcile that cannot reach
    upstream should be retried in six hours, not abandoned until someone notices
    the process is quiet.
    """
    while not stop.is_set():
        started = time.monotonic()
        try:
            with connect() as conn:
                result = service.tick(conn, client, tz, datetime.now(UTC), write_note)
            log.info("tick: %s", result)
        except Exception:
            log.exception("tick failed; retrying next interval")
        stop.wait(max(1.0, interval_s - (time.monotonic() - started)))


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
    # ingests and matches without spending a model call.
    write_note = service.no_review

    threads = [
        threading.Thread(
            target=worker,
            args=(db.connect, client, tz, write_note, stop, nudge),
            daemon=True,
            name="drain",
        ),
        threading.Thread(
            target=ticker,
            args=(db.connect, client, tz, write_note, stop),
            daemon=True,
            name="backstop",
        ),
    ]
    for thread in threads:
        thread.start()

    httpd = serve(db.connect, nudge.set, port=port)
    log.info("ingest listening on %s%s", port, ROUTE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        nudge.set()  # release the worker from its wait so it sees the stop
        httpd.server_close()
        client.close()
