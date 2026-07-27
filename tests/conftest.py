"""Test fixtures.

Every test runs against a real Postgres. The P00 invariants are database
invariants — a partial unique index, a foreign key, a check constraint,
transaction rollback — so a mocked store would test nothing that matters.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from coach import migrate

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://coach@/postgres?host=/tmp/pgrun&port=55432",
)


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    """Create one migrated database, then clone it per test.

    Migrating once and using it as a template keeps each test isolated without
    paying for the schema every time.
    """
    name = f"coach_template_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')

    url = ADMIN_URL.replace("/postgres?", f"/{name}?")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        migrate.run(conn)
        conn.commit()

    yield name

    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def conn(template_db: str) -> Iterator[psycopg.Connection]:
    name = f"coach_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'create database "{name}" template "{template_db}"')

    url = ADMIN_URL.replace("/postgres?", f"/{name}?")
    connection = psycopg.connect(url, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.close()
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')
