"""The MacroLog macro feed.

HLTH-01: an authenticated endpoint accepts per-meal macro payloads. HLTH-02:
stored at per-meal granularity, never as daily aggregates. HLTH-03: idempotent on
the meal id, and a deletion in MacroLog removes the row here.

SEC-02 sits at the top of :func:`receive` for the same reason it sits inside
:mod:`coach.ingest.webhook` rather than in the HTTP handler — the check belongs
with the write it guards, so no future route can reach the write without passing
it. The secret is a header here rather than a body field, because MacroLog is our
own client and can send one; intervals.icu is not and cannot.

**Why per meal rather than per day.** HLTH-02 is a storage requirement with a
coaching reason behind it. Protein distribution across the day is a real
coaching lever and a daily total erases it. Aggregating is something a rollup
can always do later; disaggregating is not.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Coach-Secret"

# Read off each meal object. Anything else the phone sends is kept whole in
# `payload` rather than dropped, so a field added on the client is not lost
# waiting for a server deploy.
NUMERIC_FIELDS = {
    "kcal": "kcal",
    "protein_g": "protein_g",
    "carbs_g": "carbs_g",
    "fat_g": "fat_g",
    "fibre_g": "fibre_g",
}


class Rejected(Exception):
    """The payload is not one this system will act on."""


@dataclass
class Received:
    stored: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.stored + self.updated + self.deleted


def _configured_secret(secret: str | None) -> str:
    value = secret if secret is not None else os.environ.get("MACRO_INGEST_SECRET")
    if not value:
        raise Rejected(
            "MACRO_INGEST_SECRET is not set. Without it every payload would be accepted, "
            "so macro ingest refuses to run rather than accepting anything."
        )
    return value


def verify(supplied: str | None, secret: str | None = None) -> None:
    """SEC-02 and HLTH-01: reject anything not carrying the shared secret.

    Constant time, and an absent header is rejected exactly like a wrong one so
    a request that simply omits it cannot slip through a truthiness check.
    """
    expected = _configured_secret(secret)
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise Rejected("macro ingest secret missing or incorrect")


class Malformed(Exception):
    """The payload authenticated but is not a shape this can apply.

    Distinct from :class:`Rejected` so the route can answer 400 rather than 401.
    Collapsing the two would tell a client with a correct secret that its secret
    was wrong, which is a debugging session nobody needs.
    """


def receive(
    conn: psycopg.Connection,
    payload: dict[str, Any],
    tz: ZoneInfo,
    supplied_secret: str | None = None,
    secret: str | None = None,
) -> Received:
    """Verify a payload and apply it. Returns what changed."""
    verify(supplied_secret, secret)
    return apply(conn, payload, tz)


def apply(conn: psycopg.Connection, payload: dict[str, Any], tz: ZoneInfo) -> Received:
    """Store meals and process deletions. Assumes the payload is authenticated.

    The payload carries meals to store and ids to delete, in one shape:

        {"meals": [{"id": "...", "eaten_at": "...", "protein_g": 42, ...}],
         "deleted": ["..."]}

    Both halves are optional, so the phone can send a single meal as it is
    logged, a batch after being offline, or a bare deletion, without three
    endpoints to keep in step.

    One bad meal in a batch is collected into `errors` rather than failing the
    batch. A phone that has been offline for a day sends everything it has, and
    losing eleven good meals to one malformed twelfth would be the worst
    available outcome.
    """
    result = Received()
    meals = payload.get("meals")
    if meals is None and isinstance(payload.get("id"), str):
        # A bare meal object, which is what the simplest client sends.
        meals = [payload]
    if meals is not None and not isinstance(meals, list):
        raise Malformed("`meals` must be a list")

    for meal in meals or []:
        if not isinstance(meal, dict):
            result.errors.append("meal entry is not an object")
            continue
        try:
            created = _store(conn, meal, tz)
        except Malformed as exc:
            result.errors.append(str(exc))
            continue
        if created:
            result.stored += 1
        else:
            result.updated += 1

    deleted = payload.get("deleted")
    if deleted is not None:
        if not isinstance(deleted, list):
            raise Malformed("`deleted` must be a list of meal ids")
        result.deleted = delete(conn, [str(i) for i in deleted])

    return result


def _store(conn: psycopg.Connection, meal: dict[str, Any], tz: ZoneInfo) -> bool:
    """Upsert one meal. Returns True when it was new (HLTH-03)."""
    external_id = meal.get("id") or meal.get("external_id")
    if not isinstance(external_id, str) or not external_id:
        raise Malformed("meal has no id, so it cannot be made idempotent")

    eaten_at = _timestamp(meal.get("eaten_at") or meal.get("logged_at"))
    if eaten_at is None:
        raise Malformed(f"meal {external_id!r} has no usable eaten_at")

    # TZ-01: the local day the meal belongs to is decided by the athlete's
    # configured timezone, not by the server's and not by UTC. A meal at 01:00
    # Dubai time belongs to that Dubai day.
    local_date = eaten_at.astimezone(tz).date()

    numbers = {column: _number(meal.get(key)) for key, column in NUMERIC_FIELDS.items()}
    columns = list(numbers)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""
            insert into meals (external_id, eaten_at, local_date, name, {", ".join(columns)},
                               payload, updated_at)
            values (%s, %s, %s, %s, {", ".join(["%s"] * len(columns))}, %s, now())
            on conflict (external_id) do update set
                eaten_at = excluded.eaten_at,
                local_date = excluded.local_date,
                name = excluded.name,
                {", ".join(f"{c} = excluded.{c}" for c in columns)},
                payload = excluded.payload,
                updated_at = now()
            returning (xmax = 0) as created
            """,
            (
                external_id,
                eaten_at,
                local_date,
                meal.get("name"),
                *numbers.values(),
                Jsonb(meal),
            ),
        )
        return bool(cur.fetchone()["created"])


def delete(conn: psycopg.Connection, external_ids: list[str]) -> int:
    """HLTH-03: a deletion in MacroLog propagates.

    A hard delete rather than a tombstone. The row is a copy of something the
    phone owns, so the phone's copy is the record and keeping a deleted meal
    here would put calories the athlete removed into a rollup he cannot see.
    """
    if not external_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("delete from meals where external_id = any(%s)", (external_ids,))
        return cur.rowcount


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp is ambiguous and the ambiguity is a whole day at the
        # boundary. Refusing is better than guessing at the athlete's offset.
        return None
    return parsed


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def daily_totals(conn: psycopg.Connection, since: Any, until: Any) -> list[dict[str, Any]]:
    """Per-day totals from the per-meal rows.

    Here so that HLTH-02's granularity is demonstrably not a limitation: the
    aggregate is a query over the stored rows, computed in SQL per MEM-08. NUT-01
    builds the 7 and 28 day averages on this in P10; nothing in P04 reads it into
    a response.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select local_date,
                   count(*)::int as meals,
                   sum(kcal) as kcal,
                   sum(protein_g) as protein_g,
                   sum(carbs_g) as carbs_g,
                   sum(fat_g) as fat_g,
                   sum(fibre_g) as fibre_g
              from meals
             where local_date between %s and %s
             group by local_date
             order by local_date
            """,
            (since, until),
        )
        return cur.fetchall()
