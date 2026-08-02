"""OBS-05: the last success stamp behind CHAT-09's staleness block.

One module rather than a private helper per feed, because the failure this
exists to prevent is a feed that nothing marks. `feeds` is seeded with five rows
at migration time and `render_staleness` reads every row that has not been
stamped inside its threshold — so a feed whose ingest path never calls in here
is not merely untracked, it is *reported as broken* on every turn, forever.
That is worse than not tracking it at all: the coach is told the activity feed
has never returned and hedges every number it gives, minutes after seventeen
activities came through that same client.

The meaning of a success is deliberately narrow and identical everywhere: the
source answered. Not that it carried anything, and not that the athlete did
anything. CHAT-09's own words are that absence of data is not evidence of
absence of activity, so a feed that is healthy but quiet must not read as stale.
`body_mass` is the documented exception — it is stamped from the reading rather
than the fetch, in :mod:`coach.health.wellness`, precisely because a working
wellness endpoint would otherwise hide a dead weight pipeline.
"""

from __future__ import annotations

import psycopg

# The five inbound feeds of OBS-05, seeded by migration 002.
ACTIVITIES = "activities"
FIT_ARCHIVE = "fit_archive"
WELLNESS = "wellness"
BODY_MASS = "body_mass"
CALENDAR = "calendar"


def record_success(conn: psycopg.Connection, name: str) -> None:
    """The feed answered just now, and whatever was wrong with it is not."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "update feeds set last_success_at = now(), last_error = null where name = %s",
            (name,),
        )


def record_error(conn: psycopg.Connection, name: str, error: str) -> None:
    """The feed failed. The last success stamp is left alone: it still happened."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("update feeds set last_error = %s where name = %s", (error, name))
