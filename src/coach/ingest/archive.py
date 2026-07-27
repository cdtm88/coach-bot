"""The local FIT archive.

FIT-14: a watched folder is a first class ingest path alongside the webhook, not
redundancy. FIT-15: the archive is retained permanently and is never pruned by an
upstream change. FIT-16: it can restore upstream by replaying files.

There is deliberately no delete in this module. FIT-15 exists because
disconnecting an upstream integration causes that source data to be deleted
upstream, which the source coaching conversation records happening with Strava.
The archive is the copy that survives that, so nothing here removes a row or a
file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach.ingest import activities as actmod
from coach.ingest import parse

log = logging.getLogger(__name__)

SUFFIXES = (".fit", ".FIT")


@dataclass
class Discovered:
    path: Path
    sha256: str
    size: int
    session_id: int | None = None
    already_known: bool = False


def _known(conn: psycopg.Connection, sha256: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("select id, session_id from fit_archive where sha256 = %s", (sha256,))
        return cur.fetchone()


def register(conn: psycopg.Connection, path: Path, data: bytes) -> Discovered:
    """Record a file in the archive. Idempotent on content."""
    sha = parse.content_hash(data)
    existing = _known(conn, sha)
    if existing is not None:
        return Discovered(path, sha, len(data), existing["session_id"], already_known=True)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "insert into fit_archive (path, sha256, size_bytes) values (%s, %s, %s) "
            "on conflict (path) do nothing returning id",
            (str(path), sha, len(data)),
        )
    return Discovered(path, sha, len(data))


def ingest_file(
    conn: psycopg.Connection, path: Path, tz: ZoneInfo, data: bytes | None = None
) -> int | None:
    """FIT-14: a file dropped locally becomes a session with no upstream involved.

    The activity metadata that the webhook path gets from the API is absent here,
    so everything comes from the file itself. That is the point: this path works
    when intervals.icu is unreachable or the integration is gone.
    """
    payload = data if data is not None else path.read_bytes()
    discovered = register(conn, path, payload)
    if discovered.already_known and discovered.session_id:
        return discovered.session_id

    try:
        parsed = parse.from_fit(payload)
    except parse.UnparseableActivity as exc:
        log.warning("skipping %s: %s", path, exc)
        return None

    if parsed.started_at is None:
        log.warning("skipping %s: no timestamps, so FIT-10 cannot date it", path)
        return None

    # Synthesised to match what the upstream path builds, so both routes converge
    # on one ingest function rather than two that drift.
    synthetic = {
        "id": None,
        "type": "Ride",
        "name": path.stem,
        "start_date_local": parsed.started_at.isoformat(),
    }
    result = actmod.ingest(conn, synthetic, tz, file_bytes=payload, source="local_file")

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set session_id = %s where sha256 = %s",
            (result.session_id, discovered.sha256),
        )
    return result.session_id


def scan(conn: psycopg.Connection, folder: Path, tz: ZoneInfo) -> list[int]:
    """Ingest every unseen FIT file in the watched folder."""
    if not folder.exists():
        log.info("watched folder %s does not exist yet", folder)
        return []

    ingested = []
    for path in sorted(folder.rglob("*")):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        session_id = ingest_file(conn, path, tz)
        if session_id is not None:
            ingested.append(session_id)
    return ingested


def restorable(conn: psycopg.Connection, external_ref: str | None = None) -> list[dict[str, Any]]:
    """Archived files available to push back upstream (FIT-16)."""
    with conn.cursor() as cur:
        if external_ref:
            cur.execute(
                "select id, path, sha256, external_ref from fit_archive where external_ref = %s",
                (external_ref,),
            )
        else:
            cur.execute("select id, path, sha256, external_ref from fit_archive order by id")
        return cur.fetchall()


def restore(conn: psycopg.Connection, client: Any, archive_id: int) -> dict[str, Any]:
    """FIT-16: loop an archived file back through the upload endpoint."""
    with conn.cursor() as cur:
        cur.execute("select path from fit_archive where id = %s", (archive_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no archive row {archive_id}")

    path = Path(row["path"])
    result = client.upload_file(path.read_bytes(), path.name)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set restored_at = %s, external_ref = coalesce(%s, external_ref) "
            "where id = %s",
            (datetime.now(UTC), result.get("id"), archive_id),
        )
    return result
