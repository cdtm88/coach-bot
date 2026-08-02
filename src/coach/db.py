"""Database access.

SEC-05: the connection is to a host inside the Docker network. The database port
is never published, and nothing here opens one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection with dict rows and autocommit off.

    Callers wrap writes in ``with conn.transaction():``. MEM-03 depends on the
    supersede pair being atomic, so implicit per-statement commits would be a
    correctness bug rather than a style choice.

    **Two ways to be told where the database is, and the second is not a
    fallback.** An explicit `DATABASE_URL` wins when it is set. When it is not,
    the connection is opened with no conninfo at all and libpq reads `PGHOST`,
    `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD` and `PGSSLMODE` from the
    environment itself.

    That second path exists because a deployment may well already have a Postgres
    with its own conventions — a password in a file mounted at
    `/run/secrets/...` and exported as `PGPASSWORD`, rather than a URI assembled
    from parts. Requiring a URI there would mean building one by interpolating
    the password into a string, which is the one thing a secret in a file is
    meant to avoid: it would then appear in `ps`, in a crash traceback, and in
    any log line that echoed the connection target.
    """
    url = database_url or os.environ.get("DATABASE_URL")
    if url:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            yield conn
        return

    if not os.environ.get("PGHOST"):
        raise RuntimeError(
            "no database configured: set DATABASE_URL, or the libpq variables "
            "PGHOST/PGUSER/PGDATABASE with PGPASSWORD. See docs/deploy.md."
        )
    with psycopg.connect(row_factory=dict_row) as conn:
        yield conn
