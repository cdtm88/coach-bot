"""The intervals.icu API client.

RECOV-01 and the live spec agree on authentication: HTTP basic with the literal
username `API_KEY` and the personal key as the password. That is the whole of it.

SEC-04 rules out the token-exchange alternative. A test in
tests/test_migrations_and_export.py enforces that by scanning src/ for the
vocabulary of that mechanism, so this file does not name it even to say it is
absent; docs/intervals-api.md carries the explanation instead. The scan stays a
plain substring match with no exemptions to argue about.

Athlete id `0` resolves to whoever owns the key on every path that takes one, so
that is the default. It removes a configuration value that can drift out of step
with the key it is paired with.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from coach.ingest import parse

log = logging.getLogger(__name__)

# The activity list has an undocumented server side default. Asking for a number
# means a wide backfill window truncates visibly or not at all, rather than
# silently returning whatever the default happens to be that week.
ACTIVITY_PAGE_LIMIT = 500

BASE = "https://intervals.icu/api/v1"
USERNAME = "API_KEY"

# The API reports two windows on every response. Read them rather than guessing a
# safe interval; the backfill in particular will hit them if anything does.
LIMIT_HEADER = "X-RateLimit-Limit"
REMAINING_HEADER = "X-RateLimit-Remaining"


class IntervalsError(RuntimeError):
    """An upstream call failed."""


class RateLimited(IntervalsError):
    """The 15 minute or daily window is exhausted."""


@dataclass
class RateLimit:
    window_remaining: int | None = None
    daily_remaining: int | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> RateLimit:
        raw = headers.get(REMAINING_HEADER)
        if not raw:
            return cls()
        parts = [p.strip() for p in raw.split(",")]
        try:
            return cls(int(parts[0]), int(parts[1]) if len(parts) > 1 else None)
        except ValueError:
            return cls()

    @property
    def exhausted(self) -> bool:
        return (self.window_remaining is not None and self.window_remaining <= 0) or (
            self.daily_remaining is not None and self.daily_remaining <= 0
        )


class Intervals:
    def __init__(
        self,
        api_key: str | None = None,
        athlete_id: str = "0",
        client: httpx.Client | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("INTERVALS_API_KEY")
        if not key:
            raise IntervalsError("INTERVALS_API_KEY is not set. See docs/setup.md step 6.")
        self.athlete_id = athlete_id or os.environ.get("INTERVALS_ATHLETE_ID") or "0"
        self._client = client or httpx.Client(
            base_url=BASE, auth=(USERNAME, key), timeout=60.0, follow_redirects=True
        )
        self.last_limit = RateLimit()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Intervals:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> httpx.Response:
        response = self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        self.last_limit = RateLimit.from_headers(response.headers)
        if response.status_code == 429:
            raise RateLimited(f"rate limited on {path}")
        if response.status_code >= 400:
            raise IntervalsError(f"{response.status_code} from {path}: {response.text[:200]}")
        return response

    # --- reads ---------------------------------------------------------

    def activities(
        self, oldest: date, newest: date | None = None, limit: int = ACTIVITY_PAGE_LIMIT
    ) -> list[dict[str, Any]]:
        """FIT-05 backfill and FIT-11 reconcile both read this.

        `oldest` is required upstream: omitting it returns 422 with a named error
        rather than defaulting to everything.

        `limit` is sent explicitly. Left off, the window is capped by a server
        default that is not in the spec, so a backfill chunk could come back
        truncated with no error and the missing rides would look like rides that
        never happened. A full page is a signal the window was too wide, which is
        why the caller checks for it.
        """
        rows = self._get(
            f"/athlete/{self.athlete_id}/activities",
            oldest=oldest.isoformat(),
            newest=newest.isoformat() if newest else None,
            limit=limit,
        ).json()
        if len(rows) >= limit:
            log.warning(
                "activity list for %s..%s returned a full page of %d; the window may be "
                "truncated. Narrow the chunk.",
                oldest,
                newest,
                limit,
            )
        return rows

    def activity(self, activity_id: str) -> dict[str, Any]:
        return self._get(f"/activity/{activity_id}").json()

    def original_file(self, activity_id: str) -> bytes | None:
        """FIT-03: the file as uploaded, not the platform's regenerated one.

        Returns None when there is no original to fetch, which upstream documents
        for Strava activities and which is also true of anything created manually.
        The caller falls back to streams rather than to a derived aggregate.

        The response is served gzipped. Decompressed here so that everything
        downstream — the parser, the content hash, the archive — sees the same
        plain FIT bytes the watched folder produces.
        """
        try:
            return parse.decompressed(self._get(f"/activity/{activity_id}/file").content)
        except IntervalsError as exc:
            log.info("no original file for %s: %s", activity_id, exc)
            return None

    def streams(self, activity_id: str) -> list[dict[str, Any]]:
        return self._get(
            f"/activity/{activity_id}/streams", types="watts,heartrate,cadence,distance,altitude"
        ).json()

    def wellness(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        """RECOV-01. Present here because it shares the client and the auth."""
        return self._get(
            f"/athlete/{self.athlete_id}/wellness",
            oldest=oldest.isoformat(),
            newest=newest.isoformat(),
        ).json()

    def events(self, oldest: date, newest: date) -> list[dict[str, Any]]:
        """The planned calendar over a window. PLAN-05, PLAN-06 and PLAN-12.

        Returns *everything* on the calendar, not only what the coach created —
        the athlete's own races and notes come back too. Filtering to ours is the
        caller's job and `coach.plans.events.is_ours` is how, because V1 settled
        that the documented `oauth_client_id` filter is null under a personal API
        key and cannot be used.
        """
        return self._get(
            f"/athlete/{self.athlete_id}/events",
            oldest=oldest.isoformat(),
            newest=newest.isoformat(),
        ).json()

    # --- writes --------------------------------------------------------

    def upsert_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """PLAN-01 and PLAN-02: publish planned workouts, keyed on `external_id`.

        `upsert=true` is what makes PLAN-02's "changing a prescription twice
        leaves exactly one planned event" a property of the API rather than
        something we maintain: an `external_id` already present is updated in
        place. V1 confirmed it against the live account under a personal key.

        Returns the created or updated events, which carry the upstream ids —
        needed because PLAN-07's `paired_event_id` is an upstream id and has to
        resolve back to a prescription.

        An empty list is not sent. Upstream's response to one is undocumented and
        a call that cannot accomplish anything is not worth finding out with.
        """
        if not events:
            return []
        response = self._client.post(
            f"/athlete/{self.athlete_id}/events/bulk",
            params={"upsert": "true"},
            json=events,
        )
        self.last_limit = RateLimit.from_headers(response.headers)
        if response.status_code >= 400:
            raise IntervalsError(
                f"{response.status_code} publishing {len(events)} event(s): {response.text[:200]}"
            )
        return response.json()

    def delete_events(self, external_ids: list[str]) -> int:
        """PLAN-05's orphan removal. Returns how many upstream says it deleted.

        Deletes by exact `external_id`, one entry per id. V1 verified the exact
        form and returned `{"eventsDeleted": 1}`; whether the filter also accepts
        a prefix or a wildcard is **unverified**, so the caller passes the ids it
        holds rather than a pattern. See `docs/state-of-build.md` open item 7.
        """
        if not external_ids:
            return 0
        response = self._client.put(
            f"/athlete/{self.athlete_id}/events/bulk-delete",
            json=[{"external_id": eid} for eid in external_ids],
        )
        self.last_limit = RateLimit.from_headers(response.headers)
        if response.status_code >= 400:
            raise IntervalsError(
                f"{response.status_code} deleting {len(external_ids)} event(s): "
                f"{response.text[:200]}"
            )
        body = response.json() if response.content else {}
        return int(body.get("eventsDeleted", 0)) if isinstance(body, dict) else 0

    def upload_file(
        self, data: bytes, filename: str, external_id: str | None = None
    ) -> dict[str, Any]:
        """FIT-16: replay a locally archived file back upstream.

        `external_id` is sent so the restored activity is recognisable when it
        comes back through ingest. Without it a FIT-16 restore looks like a brand
        new ride, and FIT-17's matching has nothing to key on.
        """
        response = self._client.post(
            f"/athlete/{self.athlete_id}/activities",
            params={"external_id": external_id} if external_id else None,
            files={"file": (filename, data, "application/octet-stream")},
        )
        self.last_limit = RateLimit.from_headers(response.headers)
        if response.status_code >= 400:
            raise IntervalsError(
                f"{response.status_code} uploading {filename}: {response.text[:200]}"
            )
        return response.json()

    def create_manual(self, activity: dict[str, Any]) -> dict[str, Any]:
        """LOG-07: verified present in the live spec, takes JSON."""
        response = self._client.post(f"/athlete/{self.athlete_id}/activities/manual", json=activity)
        self.last_limit = RateLimit.from_headers(response.headers)
        if response.status_code >= 400:
            raise IntervalsError(
                f"{response.status_code} creating manual activity: {response.text[:200]}"
            )
        return response.json()
