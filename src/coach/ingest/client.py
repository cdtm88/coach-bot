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

log = logging.getLogger(__name__)

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

    def activities(self, oldest: date, newest: date | None = None) -> list[dict[str, Any]]:
        """FIT-05 backfill and FIT-11 reconcile both read this.

        `oldest` is required upstream: omitting it returns 422 with a named error
        rather than defaulting to everything.
        """
        return self._get(
            f"/athlete/{self.athlete_id}/activities",
            oldest=oldest.isoformat(),
            newest=newest.isoformat() if newest else None,
        ).json()

    def activity(self, activity_id: str) -> dict[str, Any]:
        return self._get(f"/activity/{activity_id}").json()

    def original_file(self, activity_id: str) -> bytes | None:
        """FIT-03: the file as uploaded, not the platform's regenerated one.

        Returns None when there is no original to fetch, which upstream documents
        for Strava activities and which is also true of anything created manually.
        The caller falls back to streams rather than to a derived aggregate.
        """
        try:
            return self._get(f"/activity/{activity_id}/file").content
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

    # --- writes --------------------------------------------------------

    def upload_file(self, data: bytes, filename: str) -> dict[str, Any]:
        """FIT-16: replay a locally archived file back upstream."""
        response = self._client.post(
            f"/athlete/{self.athlete_id}/activities",
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
