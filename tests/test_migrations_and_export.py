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


def _example_env() -> dict[str, str]:
    """`.env.example` parsed the way a dotenv loader would.

    Raises rather than skipping a malformed line, which is the point: a line that
    does not parse is one a reader would copy into `.env` and lose.
    """
    entries: dict[str, str] = {}
    for lineno, raw in enumerate((REPO / ".env.example").read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f".env.example:{lineno} is neither comment nor assignment: {line!r}"
        key, value = line.split("=", 1)
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key), f".env.example:{lineno} bad key {key!r}"
        entries[key] = value
    return entries


def test_env_example_documents_every_variable_the_code_reads() -> None:
    """A variable src/ reads and the template omits is a silent misconfiguration.

    Every one of these is read with a default, so an omission does not fail on
    startup — it runs with the wrong value. `COACH_TZ` is the sharp case: absent,
    `clock.configured_tz` falls back to UTC and TZ-01's day and week boundaries
    are quietly computed in the wrong zone for an athlete in Dubai. This caught
    exactly that, after a docs edit landed mid-line and ate the assignment.
    """
    read = set()
    for path in (REPO / "src").rglob("*.py"):
        read |= set(re.findall(r'os\.environ[.a-z]*[(\[]"([A-Z][A-Z0-9_]*)"', path.read_text()))

    assert read, "the scan found no environment reads at all, so it is not testing anything"
    missing = sorted(read - _example_env().keys())
    assert not missing, f".env.example does not document: {missing}"


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


# The one permitted occurrence of the banned substring, and why.
#
# `oauth_client_id` is a field name on the intervals.icu CALENDAR_UPDATED
# payload. PLAN-06 has to tell the athlete's calendar edits from the coach's own
# writes echoing back, and that field is how upstream labels the difference.
# Reading a key out of a JSON body is not a token exchange, so banning it would
# be the scan enforcing its own vocabulary rather than SEC-04.
#
# The exemption is exactly this token. Anything else containing the substring
# still fails, and the two markers that actually describe a token exchange have
# no exemption at all.
ALLOWED_OAUTH_TOKEN = "oauth_client_id"

# Bounded on both sides. A plain string replace would also blank the prefix of
# `oauth_client_identifier`, leaving a fragment with no marker in it, and the
# exemption would be defeatable by anyone who appended a character.
_EXEMPT = re.compile(rf"\b{ALLOWED_OAUTH_TOKEN}\b")


def _scannable(text: str) -> str:
    return _EXEMPT.sub("", text.lower())


def test_no_oauth_anywhere(conn: psycopg.Connection) -> None:
    """SEC-04: the codebase contains no OAuth client or token refresh logic."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        scannable = _scannable(path.read_text())
        for marker in ("oauth", "refresh_token", "authorization_code"):
            if marker in scannable:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert not offenders, offenders


def test_the_oauth_exemption_does_not_let_a_real_client_through() -> None:
    """The narrow exemption above must not become a general one.

    Without this, someone could satisfy the scan by naming a variable
    `oauth_client_id_handler` and the guard would be silently defeated.
    """
    for hostile in (
        "oauth_client = OAuth2Session()",
        "token = refresh_token(...)",
        "grant_type = 'authorization_code'",
        "oauth_client_identifier = 1",  # not the exempt token, must still fail
        "myoauth_client_id = 1",  # nor is this
    ):
        assert any(
            m in _scannable(hostile) for m in ("oauth", "refresh_token", "authorization_code")
        ), f"the scan would have missed: {hostile}"


def test_the_permitted_field_name_really_is_permitted() -> None:
    """The other direction: the exemption has to actually exempt something."""
    assert "oauth" not in _scannable("calendar.oauth_client_id")
