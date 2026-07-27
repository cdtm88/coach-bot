"""The controlled key vocabulary and its value typing.

MEM-01: keys live in ``fact_keys`` and a write to an absent key is rejected by
the foreign key. MEM-14: values are validated against the declared value_type
before any write, never coerced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg


class UnknownKey(ValueError):
    """MEM-01: the key is not in the controlled vocabulary."""


class WrongValueType(ValueError):
    """MEM-14: the value does not match the type declared in fact_keys."""


@dataclass(frozen=True)
class FactKey:
    key: str
    category: str
    value_type: str
    decay_days: int | None
    safety: bool


def load(conn: psycopg.Connection, key: str) -> FactKey:
    with conn.cursor() as cur:
        cur.execute(
            "select key, category, value_type, decay_days, safety from fact_keys where key = %s",
            (key,),
        )
        row = cur.fetchone()
    if row is None:
        raise UnknownKey(
            f"{key!r} is not in fact_keys. Adding a key is a migration, deliberately, "
            "so the extraction pass cannot widen its own namespace."
        )
    return FactKey(**row)


def load_all(conn: psycopg.Connection) -> dict[str, FactKey]:
    with conn.cursor() as cur:
        cur.execute("select key, category, value_type, decay_days, safety from fact_keys")
        return {row["key"]: FactKey(**row) for row in cur.fetchall()}


def _is_date(value: object) -> bool:
    if isinstance(value, date | datetime):
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate(fact_key: FactKey, value: object) -> None:
    """Raise WrongValueType unless the value matches the declared type (MEM-14).

    Booleans are checked before numbers because ``bool`` is a subclass of ``int``
    in Python, and silently accepting ``True`` for a number would be exactly the
    coercion this requirement forbids.
    """
    vt = fact_key.value_type
    ok: bool
    if vt == "boolean":
        ok = isinstance(value, bool)
    elif vt == "number":
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif vt == "text":
        ok = isinstance(value, str)
    elif vt == "date":
        ok = _is_date(value)
    elif vt == "list":
        ok = isinstance(value, list)
    elif vt == "object":
        ok = isinstance(value, dict)
    else:  # pragma: no cover - the check constraint makes this unreachable
        raise WrongValueType(f"unknown value_type {vt!r} on key {fact_key.key!r}")

    if not ok:
        raise WrongValueType(
            f"key {fact_key.key!r} declares value_type {vt!r}, "
            f"got {type(value).__name__}: {value!r}"
        )
