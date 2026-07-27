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
    """
    url = database_url or os.environ["DATABASE_URL"]
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn
