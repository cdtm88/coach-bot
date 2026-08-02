"""Numbered SQL migrations, applied on boot.

docs/memory-design.md section 11: "Schema migrations as numbered SQL files
applied on boot." Each file runs once, inside a transaction, in filename order.
A failed migration rolls back and stops the boot rather than leaving a partly
applied schema.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg

from coach.config import Config
from coach.db import connect

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class MigrationsNotFound(RuntimeError):
    """No migration files where we looked.

    Its own type because the alternative is what actually happened: `glob`
    returning nothing, no file being pending, and the run reporting `schema up
    to date` on an empty database. Every process then started and failed one
    query later with `relation "messages" does not exist`, which points at the
    schema rather than at the boot step that was supposed to create it.
    """


def migrations_dir() -> Path:
    """Where the SQL lives, resolved at call time rather than at import.

    `parents[2]` is the repository root only while the package is imported from
    a checkout. Installed into site-packages — which is how the image ships,
    and the only way a copied virtualenv can work — it lands in the
    interpreter's `lib` directory, where there are no migrations and never will
    be. So the image sets `COACH_MIGRATIONS_DIR`, and this prefers it.

    The same trap has already been paid for twice: `agent/persona.py` and
    `seed.py` both resolve a default relative to the source tree, and both are
    given explicit paths in the deployment for exactly this reason.
    """
    override = os.environ.get("COACH_MIGRATIONS_DIR")
    return Path(override) if override else MIGRATIONS_DIR


_LEDGER = """
create table if not exists schema_migrations (
  filename    text primary key,
  applied_at  timestamptz not null default now()
)
"""


def discover(directory: Path | None = None) -> list[Path]:
    """Return migration files in filename order, which is numeric by convention.

    Empty is an error, not a result. There is no legitimate deployment of this
    system with nothing to apply.
    """
    directory = migrations_dir() if directory is None else directory
    found = sorted(directory.glob("*.sql"))
    if not found:
        raise MigrationsNotFound(
            f"no migrations in {directory}. Set COACH_MIGRATIONS_DIR to the "
            "directory holding the numbered .sql files; the image puts them in "
            "/app/migrations."
        )
    return found


def applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_LEDGER)
        cur.execute("select filename from schema_migrations")
        return {row["filename"] for row in cur.fetchall()}


def run(conn: psycopg.Connection, directory: Path | None = None) -> list[str]:
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
