"""Shared test doubles for the ingest suite.

Two modules drive the same running endpoint, so the stand-in upstream and the
server harness live here rather than in whichever test file happened to need
them first.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from coach.ingest import server, service


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


def connector(conn: psycopg.Connection):
    """Hand the handler the test's own connection, without closing it.

    The handler runs on another thread, so it cannot open its own connection to
    the test database and still see the test's uncommitted fixture rows.
    """

    @contextmanager
    def connect():
        yield conn

    return connect


def post(
    url: str,
    body: Any,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = raw if raw is not None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@dataclass
class Endpoint:
    """A running route plus the drain the worker thread would run.

    The two are separate on purpose: the request path only enqueues, so a test
    that posts and then inspects the database without draining is asserting
    exactly what the HTTP handler is responsible for and nothing more.
    """

    url: str
    drain: Callable[[], list[service.Handled]]

    def post(self, body: Any) -> tuple[int, dict[str, Any]]:
        return post(f"{self.url}{server.ROUTE}", body)

    def post_and_drain(self, body: Any) -> tuple[int, dict[str, Any], list[service.Handled]]:
        status, reply = self.post(body)
        return status, reply, (self.drain() if status == 200 else [])

    def post_macros(
        self, body: Any, secret: str | None = None, path: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        """HLTH-01: the MacroLog route, which authenticates on a header."""
        from coach.health import macros

        headers = {macros.SECRET_HEADER: secret} if secret is not None else None
        return post(f"{self.url}{path or server.MACRO_ROUTE}", body, headers=headers)


def start_endpoint(
    conn: psycopg.Connection,
    client: Upstream,
    tz: Any,
    write_note: Callable[[dict[str, Any]], str],
) -> tuple[Endpoint, Any]:
    """Bind a real server on an ephemeral port and serve it on a thread."""
    httpd = server.serve(connector(conn), port=0, tz=tz)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    endpoint = Endpoint(
        url=f"http://127.0.0.1:{httpd.server_address[1]}",
        drain=lambda: service.drain(conn, client, tz, write_note),
    )
    return endpoint, httpd
