"""Human readable export of the active fact set.

MEM-12: a nightly job writes this alongside a pg_dump. docs/setup.md calls
reading it once a month "the cheapest way to catch memory drift", which only
holds if it is genuinely readable, so this is prose and tables rather than a
JSON dump under another extension.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg

from coach.config import VERIFICATION_THRESHOLD
from coach.memory import facts as factmod
from coach.memory import keys as keymod


def render(conn: psycopg.Connection, now: datetime | None = None) -> str:
    """Render every active fact as markdown, grouped by category."""
    now = now or datetime.now(UTC)
    vocabulary = keymod.load_all(conn)
    active = factmod.active(conn)

    lines = [
        "# Active facts",
        "",
        f"Exported {now.isoformat(timespec='seconds')}. {len(active)} active facts.",
        "",
        "Safety constrained keys are marked. They never decay and only the athlete",
        "can change them (SAFE-03, SAFE-06).",
        "",
    ]

    by_category: dict[str, list[factmod.Fact]] = {}
    for fact in active:
        by_category.setdefault(vocabulary[fact.key].category, []).append(fact)

    for category in sorted(by_category):
        lines.append(f"## {category}")
        lines.append("")
        lines.append("| Key | Value | Provenance | Confidence | Last confirmed |")
        lines.append("| --- | --- | --- | --- | --- |")
        for fact in sorted(by_category[category], key=lambda f: f.key):
            safety = " (safety)" if vocabulary[fact.key].safety else ""
            flag = " ⚠" if fact.confidence < VERIFICATION_THRESHOLD else ""
            lines.append(
                f"| `{fact.key}`{safety} | {fact.value!r} | {fact.provenance} "
                f"| {fact.confidence}{flag} | {fact.last_confirmed_at:%Y-%m-%d} |"
            )
        lines.append("")

    flagged = [f for f in active if f.confidence < VERIFICATION_THRESHOLD]
    if flagged:
        lines += [
            "## Low confidence",
            "",
            f"{len(flagged)} fact(s) below {VERIFICATION_THRESHOLD}, marked ⚠ above. These are",
            "candidates for natural verification (CONS-08), not an audit list to work through.",
            "",
        ]

    return "\n".join(lines)


def write(conn: psycopg.Connection, directory: Path, now: datetime | None = None) -> Path:
    """Write the export to ``directory``, dated. Returns the path written."""
    now = now or datetime.now(UTC)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"facts-{now:%Y-%m-%d}.md"
    path.write_text(render(conn, now))
    return path
