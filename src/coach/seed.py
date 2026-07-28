"""Seed the memory store from a reviewed data file.

docs/setup.md step 10. This is the one time facts enter the store without a
conversation behind them, so it is deliberately a separate, reviewable artefact
rather than something the agent does: a human reads seeds/athlete.json, checks it
against the source transcript, and runs this.

Safety keys go through the SAFE-06 athlete path with confirmation, because that
is the only path that can write them and the seed is the athlete stating them.
Everything else goes through the consolidation path with actor `rule`, which
records in fact_events that a seed rather than a nightly pass put it there.

Idempotent: a key whose active value already equals the seed value is left alone,
so re-running does not manufacture a supersession chain out of nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from coach.config import Config
from coach.db import connect
from coach.memory import facts as factmod
from coach.memory import notes as notemod

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SEED = REPO / "seeds" / "athlete.json"


class SeedError(RuntimeError):
    """The seed file is malformed or refers to something that does not exist."""


def load(path: Path = DEFAULT_SEED) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SeedError(f"{path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise SeedError(f"{path} is not valid JSON: {exc}") from exc

    for section in ("constraints", "facts"):
        if not isinstance(data.get(section), list):
            raise SeedError(f"{path} is missing a {section!r} list")
    return data


def apply(conn: psycopg.Connection, data: dict[str, Any]) -> dict[str, int]:
    """Write the seed. Returns counts of what changed."""
    counts = {"constraints": 0, "facts": 0, "notes": 0, "unchanged": 0}

    # Safety keys first, so that a prompt assembled at any point during the seed
    # already carries the constraints rather than acquiring them last.
    for entry in data["constraints"]:
        existing = factmod.active_for(conn, entry["key"])
        if existing is not None and existing.value == entry["value"]:
            counts["unchanged"] += 1
            continue
        factmod.state_constraint(
            conn,
            entry["key"],
            entry["value"],
            reason=f"seeded from {data.get('_source', 'the seed file')}: {entry['reason']}",
            confirmed=True,
        )
        counts["constraints"] += 1

    for entry in data["facts"]:
        existing = factmod.active_for(conn, entry["key"])
        if existing is not None and existing.value == entry["value"]:
            counts["unchanged"] += 1
            continue
        factmod.ratify(
            conn,
            entry["key"],
            entry["value"],
            entry["provenance"],
            reason=f"seeded: {entry['reason']}",
            actor="rule",
            confidence=Decimal("1.00"),
        )
        counts["facts"] += 1

    for entry in data.get("notes", []):
        occurred_on = date.fromisoformat(entry["occurred_on"])
        if entry["kind"] == "day_summary":
            notemod.upsert_day_summary(conn, entry["body"], occurred_on)
        elif not _note_exists(conn, entry["kind"], occurred_on, entry["body"]):
            notemod.add(conn, entry["kind"], entry["body"], occurred_on)
        else:
            counts["unchanged"] += 1
            continue
        counts["notes"] += 1

    conn.commit()
    return counts


def _note_exists(conn: psycopg.Connection, kind: str, occurred_on: date, body: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from notes where kind = %s and occurred_on = %s and body = %s",
            (kind, occurred_on, body),
        )
        return cur.fetchone() is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the memory store from a data file.")
    parser.add_argument("--file", type=Path, default=DEFAULT_SEED)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    data = load(args.file)

    with connect(config.database_url) as conn:
        counts = apply(conn, data)

    log.info(
        "seeded %d constraint(s), %d fact(s), %d note(s); %d already current",
        counts["constraints"],
        counts["facts"],
        counts["notes"],
        counts["unchanged"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
