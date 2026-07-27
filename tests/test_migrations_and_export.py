"""Migrations and the nightly export: MEM-12, SPEC-01, SEC-01."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import psycopg

from coach import migrate
from coach.memory import export, facts

REPO = Path(__file__).resolve().parents[1]


def test_migrations_are_idempotent(conn: psycopg.Connection) -> None:
    """Re-running on a migrated database applies nothing."""
    assert migrate.run(conn) == []


def test_every_design_table_exists(conn: psycopg.Connection) -> None:
    """SPEC-01: a reviewer can map the design's tables to the implementation.

    The list is exactly what docs/memory-design.md section 4 defines, after v2.1
    scoped it to the memory subsystem.
    """
    expected = {
        "fact_keys",
        "facts",
        "fact_events",
        "notes",
        "rollups",
        "prescriptions",
        "adjustment_events",
        "conversation_state",
        "pending_writes",
        "feeds",
        "recall_tests",
    }
    with conn.cursor() as cur:
        cur.execute("select tablename from pg_tables where schemaname = 'public'")
        present = {row["tablename"] for row in cur.fetchall()}
    assert expected <= present, f"missing: {sorted(expected - present)}"


def test_prescriptions_session_fk_landed_in_p03(conn: psycopg.Connection) -> None:
    """P00 deferred this column because `sessions` did not exist yet; 005 adds it.

    Asserting the constraint and not just the column is the point: an integer
    named session_id that pointed at nothing would satisfy FIT-05's queries right
    up until a session was deleted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select ccu.table_name as target
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on kcu.constraint_name = tc.constraint_name
            join information_schema.constraint_column_usage ccu
              on ccu.constraint_name = tc.constraint_name
            where tc.table_name = 'prescriptions'
              and tc.constraint_type = 'FOREIGN KEY'
              and kcu.column_name = 'session_id'
            """
        )
        row = cur.fetchone()
    assert row is not None, "prescriptions.session_id has no foreign key"
    assert row["target"] == "sessions"


def test_key_namespace_matches_the_design(conn: psycopg.Connection) -> None:
    """Design section 5: seven namespaces with the stated half lives."""
    with conn.cursor() as cur:
        cur.execute("select distinct category, decay_days, safety from fact_keys order by category")
        rows = cur.fetchall()

    half_lives = {r["category"]: r["decay_days"] for r in rows}
    assert half_lives == {
        "constraint": None,
        "profile": 365,
        "goal": 90,
        "availability": 30,
        "prefs": 120,
        "equipment": 180,
        "physiology": 42,
    }
    assert all(r["safety"] for r in rows if r["category"] == "constraint")
    assert not any(r["safety"] for r in rows if r["category"] != "constraint")


def test_five_feeds_are_tracked(conn: psycopg.Connection) -> None:
    """OBS-05: the five inbound feeds that carry a staleness threshold."""
    with conn.cursor() as cur:
        cur.execute("select name, stale_after_hours from feeds order by name")
        rows = cur.fetchall()
    assert {r["name"] for r in rows} == {
        "activities",
        "fit_archive",
        "wellness",
        "body_mass",
        "calendar",
    }
    thresholds = {r["name"]: r["stale_after_hours"] for r in rows}
    assert thresholds["wellness"] == 48
    assert thresholds["body_mass"] == 288  # 12 days, matching HLTH-15
    assert thresholds["calendar"] == 24


def test_export_is_readable_markdown(conn: psycopg.Connection, tmp_path: Path) -> None:
    """MEM-12: a human readable export of the active fact set."""
    facts.state_constraint(
        conn, "constraint.injury_history", ["L4/L5"], reason="seed", confirmed=True
    )
    facts.ratify(conn, "goal.target_weight_kg", 72.0, "stated", reason="seed")
    conn.commit()

    path = export.write(conn, tmp_path)
    body = path.read_text()

    assert path.exists()
    assert "# Active facts" in body
    assert "constraint.injury_history" in body
    assert "(safety)" in body
    assert "goal.target_weight_kg" in body
    assert "## constraint" in body and "## goal" in body


def test_export_flags_low_confidence(conn: psycopg.Connection, tmp_path: Path) -> None:
    fact = facts.ratify(conn, "availability.days", ["mon"], "observed", reason="seed")
    with conn.cursor() as cur:
        cur.execute("update facts set confidence = 0.25 where id = %s", (fact.id,))
    conn.commit()

    body = export.render(conn)
    assert "Low confidence" in body
    assert "⚠" in body


def test_no_credentials_in_the_repository() -> None:
    """SEC-01: a repository scan finds no credentials.

    Checks tracked files only, so a developer's own .env is out of scope by
    construction rather than by luck.
    """
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()

    assert ".env" not in tracked
    assert ".env.example" in tracked

    # An assignment to a secret-looking name whose value is long enough to be a
    # real credential rather than a placeholder or an empty template slot.
    secret = re.compile(
        r"(API_KEY|_TOKEN|_SECRET|PASSWORD|BOT_TOKEN)\s*=\s*['\"]?([A-Za-z0-9_\-]{16,})",
        re.IGNORECASE,
    )
    placeholders = {"changeme", "your", "example", "placeholder", "redacted"}

    suspicious = []
    for name in tracked:
        path = REPO / name
        # This file necessarily contains the patterns it searches for.
        if not path.is_file() or path.suffix in {".png", ".jpg", ".pdf"} or "tests/" in name:
            continue
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            match = secret.search(line)
            if match and not any(p in match.group(2).lower() for p in placeholders):
                suspicious.append(f"{name}:{lineno}")
    assert not suspicious, f"possible credentials: {suspicious}"


def test_no_oauth_anywhere(conn: psycopg.Connection) -> None:
    """SEC-04: the codebase contains no OAuth client or token refresh logic."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text().lower()
        for marker in ("oauth", "refresh_token", "authorization_code"):
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert not offenders, offenders
