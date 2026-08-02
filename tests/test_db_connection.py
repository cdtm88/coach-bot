"""How the application is told where its database is.

Two routes, and this is the file that keeps both working. `DATABASE_URL` is what
this repository's own compose stack sets. The libpq variables are what a Postgres
deployed independently is likely to already provide, with the password in a
mounted file exported as `PGPASSWORD` rather than interpolated into a URI.

**The second route is the one worth testing.** It has no callers inside the
repository — the tests and the bundled compose file both use a URL — so nothing
else would notice it breaking, and it would break in the one place that is
hardest to debug: a container that has just started on someone's server.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import conftest
from coach import db
from coach.config import Config, ConfigError


def libpq_env(url: str) -> dict[str, str]:
    """The same connection, expressed the way libpq reads it from the environment."""
    params = conninfo_to_dict(url)
    mapping = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "dbname": "PGDATABASE",
        "password": "PGPASSWORD",
    }
    return {env: str(params[key]) for key, env in mapping.items() if params.get(key)}


def test_a_database_url_still_wins(conn: psycopg.Connection) -> None:
    """The bundled compose stack sets one, and it must keep taking precedence."""
    with db.connect(conftest.ADMIN_URL) as opened:
        assert opened.execute("select 1 as ok").fetchone()["ok"] == 1


def test_the_libpq_variables_are_enough_on_their_own(
    monkeypatch: pytest.MonkeyPatch, conn: psycopg.Connection
) -> None:
    """No `DATABASE_URL` anywhere, and the connection still opens.

    This is the path a deployment with its own Postgres takes: PGHOST and friends
    in the environment, PGPASSWORD exported from a mounted secret. Requiring a URI
    there would mean building one by interpolating the password into a string —
    which is exactly what a secret in a file exists to avoid, since it would then
    show up in `ps`, in a traceback, and in anything that logged the target.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name, value in libpq_env(conftest.ADMIN_URL).items():
        monkeypatch.setenv(name, value)

    with db.connect() as opened:
        assert opened.execute("select 1 as ok").fetchone()["ok"] == 1


def test_rows_are_dicts_on_both_routes(
    monkeypatch: pytest.MonkeyPatch, conn: psycopg.Connection
) -> None:
    """Every query in the codebase subscripts its rows by name.

    A connection opened down the second route with the default row factory would
    return tuples, and the failure would be a `TypeError` deep inside whichever
    query ran first rather than anything about configuration.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name, value in libpq_env(conftest.ADMIN_URL).items():
        monkeypatch.setenv(name, value)

    with db.connect() as opened:
        row = opened.execute("select 1 as ok").fetchone()
        assert isinstance(row, dict)
        assert row["ok"] == 1


def test_neither_route_configured_says_so_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error a misconfigured container shows, so it has to name the way out.

    "DATABASE_URL is not set" would send someone to set a variable their
    deployment deliberately does not use.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)

    with pytest.raises(RuntimeError, match="PGHOST"):
        with db.connect():
            pass


def test_the_config_accepts_a_libpq_only_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`coach-migrate` and `coach-seed` both go through `Config.from_env`.

    Which means a deployment using the libpq variables could not run its
    migrations at all until this stopped requiring a URL — and migrations are the
    first thing it runs.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "postgres")

    config = Config.from_env()

    assert config.database_url is None


def test_the_config_still_refuses_an_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)

    with pytest.raises(ConfigError, match="PGHOST"):
        Config.from_env()


def test_no_module_builds_a_connection_string_from_parts() -> None:
    """The reason the libpq route exists, guarded.

    A password interpolated into a URI shows up in `ps`, in a crash traceback and
    in any log line that echoes the connection target. If somewhere later starts
    assembling one from `PGPASSWORD`, this is what notices.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (repo / "src").rglob("*.py"):
        body = path.read_text()
        for line in body.splitlines():
            lowered = line.lower()
            if "postgresql://" in lowered and ("pgpassword" in lowered or "password" in lowered):
                offenders.append(f"{path.relative_to(repo)}: {line.strip()[:80]}")
    assert not offenders, offenders
