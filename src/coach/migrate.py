"""Numbered SQL migrations, applied on boot.

docs/memory-design.md section 11: "Schema migrations as numbered SQL files
applied on boot." Each file runs once, inside a transaction, in filename order.
A failed migration rolls back and stops the boot rather than leaving a partly
applied schema.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import psycopg

from coach.config import Config
from coach.db import connect

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_LEDGER = """
create table if not exists schema_migrations (
  filename    text primary key,
  applied_at  timestamptz not null default now()
)
"""


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return migration files in filename order, which is numeric by convention."""
    return sorted(directory.glob("*.sql"))


def applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_LEDGER)
        cur.execute("select filename from schema_migrations")
        return {row["filename"] for row in cur.fetchall()}


def run(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the filenames applied."""
    done = applied(conn)
    conn.commit()

    newly_applied: list[str] = []
    for path in discover(directory):
        if path.name in done:
            continue
        log.info("applying migration %s", path.name)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute(
                "insert into schema_migrations (filename) values (%s)",
                (path.name,),
            )
        newly_applied.append(path.name)

    return newly_applied


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    with connect(config.database_url) as conn:
        newly_applied = run(conn)
    if newly_applied:
        log.info("applied %d migration(s): %s", len(newly_applied), ", ".join(newly_applied))
    else:
        log.info("schema up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
