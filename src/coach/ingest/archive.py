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
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from coach import feeds as feedmod
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


def archive_folder() -> Path:
    return Path(os.environ.get("COACH_FIT_ARCHIVE", "var/fit-archive"))


def keep_original(
    conn: psycopg.Connection, activity_id: str, data: bytes, session_id: int | None
) -> None:
    """FIT-15: keep the downloaded original, keyed to the upstream activity.

    Every path that downloads a file calls this, and that is the requirement
    rather than tidiness. Without a registered app the poll is the primary ingest
    path, so a copy that only the webhook drain made was a copy of almost
    nothing: the poll fetched the bytes to parse them and dropped them on the
    floor, and the archive that exists because upstream can delete its own data
    held only the files that arrived through the watched folder.

    Stored decompressed. intervals.icu serves the original gzipped, so the file
    written under `<id>.fit` was a gzip stream wearing a FIT extension — and
    FIT-16 reads these back out and posts them to the upload endpoint, where the
    name is what says how to read the bytes. `size_bytes` was the compressed
    length against a hash of the uncompressed content, which is two answers to
    one question.
    """
    canonical = parse.decompressed(data)
    folder = archive_folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{activity_id}.fit"
    if not path.exists():
        path.write_bytes(canonical)
    register(conn, path, canonical)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set session_id = coalesce(session_id, %s), "
            "external_ref = coalesce(external_ref, %s) where sha256 = %s",
            (session_id, activity_id, parse.content_hash(canonical)),
        )


def keep_local(conn: psycopg.Connection, path: Path, data: bytes) -> Discovered:
    """FIT-15 for the watched folder: keep a copy, not a pointer.

    `register` used to record the inbox path, which is a directory the athlete
    owns and tidies. So for every file arriving this way the "permanent archive
    that is never pruned" held no copy at all — only a row pointing at somebody
    else's file, and `restore` reading it back would raise the moment that file
    was moved or deleted. The archive existed for files it did not have.

    Copied under its own name where that is free. Where it is not, and the bytes
    differ, the sha is appended rather than the file being skipped: `register`
    inserts `on conflict (path) do nothing`, so a sync tool that reuses one
    filename for successive activities — which is exactly what a mounted device
    does — silently left every file after the first unarchived.
    """
    sha = parse.content_hash(data)
    known = _known(conn, sha)
    if known is not None:
        return Discovered(path, sha, len(data), known["session_id"], already_known=True)

    # Stored decompressed for the same reason `keep_original` is: the extension
    # says FIT and FIT-16 posts these back to an endpoint that reads the name.
    canonical = parse.decompressed(data)
    folder = archive_folder()
    folder.mkdir(parents=True, exist_ok=True)

    target = folder / path.name
    if target.exists() and parse.content_hash(target.read_bytes()) != sha:
        target = folder / f"{path.stem}-{sha[:12]}{path.suffix}"
    if not target.exists():
        target.write_bytes(canonical)

    return register(conn, target, canonical)


def ingest_file(
    conn: psycopg.Connection, path: Path, tz: ZoneInfo, data: bytes | None = None
) -> int | None:
    """FIT-14: a file dropped locally becomes a session with no upstream involved.

    The activity metadata that the webhook path gets from the API is absent here,
    so everything comes from the file itself. That is the point: this path works
    when intervals.icu is unreachable or the integration is gone.
    """
    payload = data if data is not None else path.read_bytes()
    discovered = keep_local(conn, path, payload)
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

    # The file's own sport, which is the only thing here that knows. Falling back
    # to a ride only when the file never says, and saying so, because that branch
    # is a guess and a guess should be visible in the log rather than in the data
    # alone.
    kind = actmod.type_of_file(parsed)
    if kind is None:
        kind = actmod.UNDECLARED_LOCAL_TYPE
        log.info("%s declares no sport; ingesting it as %s", path, kind)

    # Synthesised to match what the upstream path builds, so both routes converge
    # on one ingest function rather than two that drift.
    #
    # The stem goes in as a fallback and not as `name`. This path routinely lands
    # on a row the poll already created from the same bytes, and a filename is
    # not a correction to the title the platform gave the ride.
    synthetic = {
        "id": None,
        "type": kind,
        "start_date_local": parsed.started_at.isoformat(),
    }
    result = actmod.ingest(
        conn, synthetic, tz, file_bytes=payload, source="local_file", fallback_name=path.stem
    )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set session_id = %s where sha256 = %s",
            (result.session_id, discovered.sha256),
        )
    return result.session_id


def scan(conn: psycopg.Connection, folder: Path, tz: ZoneInfo) -> list[int]:
    """Ingest every unseen FIT file in the watched folder.

    OBS-05's `fit_archive` feed is stamped here, and it is the readability of the
    folder that is being stamped rather than the arrival of a file. A fortnight
    with no rides must not read as a broken archive — a sync that stopped
    mounting the directory must.
    """
    if not folder.exists():
        log.info("watched folder %s does not exist yet", folder)
        feedmod.record_error(conn, feedmod.FIT_ARCHIVE, f"watched folder {folder} does not exist")
        return []

    ingested = []
    for path in sorted(folder.rglob("*")):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        session_id = ingest_file(conn, path, tz)
        if session_id is not None:
            ingested.append(session_id)

    feedmod.record_success(conn, feedmod.FIT_ARCHIVE)
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
    """FIT-16: loop an archived file back through the upload endpoint.

    The upload carries the coach marker keyed to the local session, so when the
    restored ride returns through the webhook FIT-17 matches it to the row that
    already exists instead of creating a second one. A restore that produced a
    duplicate would be a strange way to repair an archive.
    """
    with conn.cursor() as cur:
        cur.execute("select path, session_id from fit_archive where id = %s", (archive_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no archive row {archive_id}")

    path = Path(row["path"])
    marker = f"{actmod.COACH_MARKER}{row['session_id']}" if row["session_id"] is not None else None
    result = client.upload_file(path.read_bytes(), path.name, external_id=marker)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update fit_archive set restored_at = %s, external_ref = coalesce(%s, external_ref) "
            "where id = %s",
            (datetime.now(UTC), result.get("id"), archive_id),
        )
    return result
