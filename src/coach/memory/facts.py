"""The fact store: supersession, provenance, audit and decay.

Two write paths reach ``facts``, and only two:

* :func:`ratify` is consolidation. It is the authoritative writer for everything
  except safety keys, which it may not touch (SAFE-02).
* :func:`state_constraint` is the athlete safety path (SAFE-06). It writes safety
  keys and nothing else, directly, with actor ``athlete``.

A chat turn reaches neither. It writes to ``pending_writes`` and waits for the
night (CONS-06). Without SAFE-06 the two rules above would compose into a
deadlock in which no constraint could ever be recorded after the initial seed,
which is why the exception exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from coach.config import CONFIDENCE_FLOOR
from coach.memory import keys as keymod

log = logging.getLogger(__name__)

PROVENANCE = ("stated", "observed", "computed", "inferred")
ACTORS = ("consolidation", "in_turn", "rule", "athlete")

# Design section 7: behavioural keys take observed over stated; intent keys take
# stated over observed. Conflict resolution is executed here, in code, and the
# model's opinion on precedence is discarded (CONS-03).
INTENT_CATEGORIES = frozenset({"goal"})


class SafetyKeyViolation(PermissionError):
    """SAFE-02: only the SAFE-06 athlete path may write a safety key."""


class NotASafetyKey(ValueError):
    """SAFE-06 writes safety keys and nothing else."""


class ConfirmationRequired(ValueError):
    """SAFE-06: the athlete confirms the restated constraint before it lands."""


@dataclass(frozen=True)
class Fact:
    id: int
    key: str
    value: Any
    provenance: str
    confidence: Decimal
    status: str
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: int | None
    source_ref: str | None
    last_confirmed_at: datetime
    mention_pending: bool
    mention_expires: datetime | None


def _row_to_fact(row: dict[str, Any]) -> Fact:
    return Fact(**{f: row[f] for f in Fact.__dataclass_fields__})


_SELECT = """
select id, key, value, provenance, confidence, status, valid_from, valid_to,
       superseded_by, source_ref, last_confirmed_at, mention_pending, mention_expires
from facts
"""


def active(conn: psycopg.Connection) -> list[Fact]:
    """Every active fact. MEM-10 loads standing memory in full on every turn."""
    with conn.cursor() as cur:
        cur.execute(_SELECT + "where status = 'active' order by key")
        return [_row_to_fact(r) for r in cur.fetchall()]


def active_for(conn: psycopg.Connection, key: str) -> Fact | None:
    with conn.cursor() as cur:
        cur.execute(_SELECT + "where key = %s and status = 'active'", (key,))
        row = cur.fetchone()
    return _row_to_fact(row) if row else None


def history(conn: psycopg.Connection, key: str) -> list[Fact]:
    """Every value this key has held, newest first.

    CHAT-07: asking what the coach holds on a topic returns the active value,
    its provenance, when it last changed, and what it replaced.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT + "where key = %s order by valid_from desc, id desc", (key,))
        return [_row_to_fact(r) for r in cur.fetchall()]


def _log_event(
    conn: psycopg.Connection,
    fact_id: int,
    action: str,
    reason: str,
    actor: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """MEM-06: every fact change leaves an audit row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into fact_events (fact_id, action, reason, actor, evidence)
            values (%s, %s, %s, %s, %s)
            """,
            (fact_id, action, reason, actor, Jsonb(evidence) if evidence else None),
        )


def _record_rejection(
    conn: psycopg.Connection,
    key: str,
    value: Any,
    provenance: str,
    reason: str,
    actor: str,
    evidence: dict[str, Any] | None = None,
) -> int:
    """Write the refused attempt as a rejected fact plus its audit row.

    SAFE-02 requires a rejected write to be logged to fact_events with actor and
    reason, and fact_events.fact_id is not nullable. The 'rejected' status exists
    for exactly this, and a rejected row does not collide with the partial unique
    index, which covers active rows only.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into facts (key, value, provenance, status, valid_to)
            values (%s, %s, %s, 'rejected', now())
            returning id
            """,
            (key, Jsonb(value), provenance),
        )
        fact_id = cur.fetchone()["id"]
    _log_event(conn, fact_id, "rejected", reason, actor, evidence)
    return fact_id


def _supersede(
    conn: psycopg.Connection,
    fact_key: keymod.FactKey,
    value: Any,
    provenance: str,
    actor: str,
    reason: str,
    evidence: dict[str, Any] | None,
    source_ref: str | None,
    confidence: Decimal | float,
) -> Fact:
    """Close the current active row and open a new one, atomically.

    MEM-03: the close, the insert and the pointer are one transaction. The
    partial unique index of MEM-02 permits exactly one active row per key, so the
    old row must be closed before the new one is inserted; the pointer is set
    afterwards, once the new id exists. An interruption at any point rolls the
    whole thing back and leaves the prior row active.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "select id from facts where key = %s and status = 'active' for update",
            (fact_key.key,),
        )
        prior = cur.fetchone()

        if prior is not None:
            cur.execute(
                """
                update facts set status = 'superseded', valid_to = now()
                where id = %s
                """,
                (prior["id"],),
            )

        cur.execute(
            """
            insert into facts (key, value, provenance, confidence, source_ref)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (fact_key.key, Jsonb(value), provenance, Decimal(str(confidence)), source_ref),
        )
        new_id = cur.fetchone()["id"]

        if prior is not None:
            cur.execute(
                "update facts set superseded_by = %s where id = %s",
                (new_id, prior["id"]),
            )
            _log_event(conn, prior["id"], "superseded", reason, actor, evidence)

        _log_event(conn, new_id, "created", reason, actor, evidence)

    fact = active_for(conn, fact_key.key)
    assert fact is not None  # noqa: S101 - just written inside the transaction
    return fact


def ratify(
    conn: psycopg.Connection,
    key: str,
    value: Any,
    provenance: str,
    reason: str,
    actor: str = "consolidation",
    evidence: dict[str, Any] | None = None,
    source_ref: str | None = None,
    confidence: Decimal | float = 1.00,
) -> Fact:
    """Write a fact. The consolidation path (CONS-06).

    Raises :class:`SafetyKeyViolation` for a safety key, recording the refused
    attempt first so the audit trail shows what was tried and by whom (SAFE-02).
    """
    if provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}, got {provenance!r}")
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of {ACTORS}, got {actor!r}")

    fact_key = keymod.load(conn, key)  # MEM-01

    if fact_key.safety:
        _record_rejection(
            conn,
            key,
            value,
            provenance,
            reason=f"safety key: consolidation may not write {key!r} (SAFE-02)",
            actor=actor,
            evidence=evidence,
        )
        conn.commit()
        log.warning("rejected write to safety key %s by %s", key, actor)
        raise SafetyKeyViolation(
            f"{key!r} is safety constrained. Only the athlete path (SAFE-06) may write it."
        )

    keymod.validate(fact_key, value)  # MEM-14

    return _supersede(
        conn, fact_key, value, provenance, actor, reason, evidence, source_ref, confidence
    )


def state_constraint(
    conn: psycopg.Connection,
    key: str,
    value: Any,
    reason: str,
    confirmed: bool,
    evidence: dict[str, Any] | None = None,
) -> Fact:
    """The athlete safety path (SAFE-06).

    The one direct writer outside consolidation, the one exception to CONS-06,
    and the one place the system asks for confirmation. It writes safety keys and
    nothing else, always with provenance ``stated`` and actor ``athlete``.

    ``confirmed`` is the athlete agreeing to the coach's restatement of the
    constraint. Passing False raises rather than writing.
    """
    fact_key = keymod.load(conn, key)

    if not fact_key.safety:
        raise NotASafetyKey(
            f"{key!r} is not safety constrained. Ordinary facts go through consolidation."
        )
    if not confirmed:
        raise ConfirmationRequired(
            f"{key!r} needs the athlete to confirm the restated constraint before it lands."
        )

    keymod.validate(fact_key, value)

    return _supersede(
        conn,
        fact_key,
        value,
        provenance="stated",
        actor="athlete",
        reason=reason,
        evidence=evidence,
        source_ref=None,
        confidence=Decimal("1.00"),
    )


def confirm(conn: psycopg.Connection, key: str, reason: str, actor: str = "consolidation") -> Fact:
    """Reset decay on a fact that evidence has just reconfirmed (CONS-07)."""
    fact = active_for(conn, key)
    if fact is None:
        raise ValueError(f"no active fact for {key!r}")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update facts set last_confirmed_at = now(), confidence = 1.00 where id = %s",
            (fact.id,),
        )
        _log_event(conn, fact.id, "confirmed", reason, actor)
    result = active_for(conn, key)
    assert result is not None  # noqa: S101
    return result


def decayed_confidence(age_days: float, half_life_days: int | None) -> Decimal:
    """The CONS-07 curve: ``floor + (1 - floor) * 0.5 ** (age / half_life)``.

    A null half life never decays (SAFE-03 safety keys). Confidence asymptotes to
    the floor and never reaches zero, so a fact loses standing without ever
    silently vanishing.
    """
    if half_life_days is None:
        return Decimal("1.00")
    if age_days <= 0:
        return Decimal("1.00")
    decayed = CONFIDENCE_FLOOR + (Decimal(1) - CONFIDENCE_FLOOR) * Decimal(
        str(0.5 ** (age_days / half_life_days))
    )
    return decayed.quantize(Decimal("0.01"))


def apply_decay(conn: psycopg.Connection, actor: str = "consolidation") -> int:
    """Recompute confidence on every active fact. Step 9 of the nightly pipeline.

    Returns the number of facts whose stored confidence moved.
    """
    vocabulary = keymod.load_all(conn)
    changed = 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _SELECT + "where status = 'active'",
        )
        rows = cur.fetchall()
        for row in rows:
            fact_key = vocabulary[row["key"]]
            if fact_key.decay_days is None:
                continue  # SAFE-03
            age = (datetime.now(row["last_confirmed_at"].tzinfo) - row["last_confirmed_at"]).days
            target = decayed_confidence(age, fact_key.decay_days)
            if target == row["confidence"]:
                continue
            cur.execute("update facts set confidence = %s where id = %s", (target, row["id"]))
            _log_event(
                conn,
                row["id"],
                "decayed",
                f"unconfirmed for {age} days against a {fact_key.decay_days} day half life",
                actor,
            )
            changed += 1
    return changed


def verification_candidate(conn: psycopg.Connection, threshold: Decimal) -> Fact | None:
    """The single lowest confidence fact worth verifying naturally (CONS-08).

    One, not a list. The caller still has to fit it inside the CHAT-11
    interruption budget, which a mention or an outlier confirmation may already
    be holding.
    """
    with conn.cursor() as cur:
        cur.execute(
            _SELECT
            + """
            where status = 'active' and confidence < %s
            order by confidence asc, last_confirmed_at asc
            limit 1
            """,
            (threshold,),
        )
        row = cur.fetchone()
    return _row_to_fact(row) if row else None
