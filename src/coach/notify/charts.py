"""NOTIF-04: charts are links to rendered pages, never chat embeds.

The requirement reads like a delivery-mechanism preference and is not. Telegram
renders an image inline, at whatever size the client picks, with no axes the
athlete can interrogate and no way to look at last month instead. A link is a
page that can carry the figures beside the picture, and — the part that matters
here — the picture is drawn from the *rollups*, so what the chart shows and what
the coach says cannot disagree.

**SVG generated here, not a charting library.** Two reasons. The page has to be
self-contained because it is served by the same tiny handler that takes
MacroLog's posts, and a chart with three series and no interactivity is about
forty lines of path arithmetic. Pulling in matplotlib to render a PNG would add
a heavyweight dependency to a container whose entire job is to be up.

**The link needs no authentication and carries no secret**, so it deliberately
shows nothing a passer-by could not guess: load numbers and a weight trend with
no name, no date of birth, and no identifiers. `PUBLIC_BASE_URL` is a tunnel
hostname that is already effectively public.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg

log = logging.getLogger(__name__)

KINDS = ("load", "weight")

WIDTH = 720
HEIGHT = 240
PAD = 32


@dataclass(frozen=True)
class Series:
    label: str
    points: list[tuple[date, Decimal]]

    @property
    def empty(self) -> bool:
        return len(self.points) < 2


def link(kind: str, base_url: str | None = None) -> str:
    """The URL to send. NOTIF-04's acceptance is that this one works."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    root = (base_url or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{root}/charts/{kind}"


def series(conn: psycopg.Connection, kind: str, as_of: date, days: int = 90) -> Series:
    """Read the chart's data from the same rollups the coach quotes.

    Not from the sessions or the readings. A chart fitted independently would be
    a second implementation of the trend, and the first time it disagreed with
    the review nobody would know which to believe.
    """
    since = as_of - timedelta(days=days)
    column = {"load": "load_7d", "weight": "weight_trend_slope"}[kind]
    # The column name is interpolated because it cannot be a parameter, and it
    # is safe because it comes from the literal map above rather than from the
    # caller's `kind` — an unknown kind raises a KeyError before reaching here.
    with conn.cursor() as cur:
        cur.execute(
            f"select as_of, {column} as value from rollups "
            f"where as_of between %s and %s and {column} is not null order by as_of",
            (since, as_of),
        )
        rows = cur.fetchall()
    label = {"load": "7 day load", "weight": "weight trend (kg/week)"}[kind]
    return Series(label=label, points=[(r["as_of"], r["value"]) for r in rows])


def _path(points: list[tuple[date, Decimal]]) -> str:
    xs = [p[0].toordinal() for p in points]
    ys = [float(p[1]) for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    span_x = (x1 - x0) or 1
    span_y = (y1 - y0) or 1

    def place(x: int, y: float) -> tuple[float, float]:
        px = PAD + (x - x0) / span_x * (WIDTH - 2 * PAD)
        py = HEIGHT - PAD - (y - y0) / span_y * (HEIGHT - 2 * PAD)
        return round(px, 1), round(py, 1)

    coords = [place(x, y) for x, y in zip(xs, ys, strict=True)]
    return "M " + " L ".join(f"{px},{py}" for px, py in coords)


def render(found: Series, as_of: date) -> str:
    """A whole page, self-contained. No script, no external stylesheet, no font."""
    if found.empty:
        body = f"<p>Not enough data yet for {found.label}.</p>"
    else:
        first, last = found.points[0], found.points[-1]
        body = (
            f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" role="img" '
            f'aria-label="{found.label}">'
            f'<path d="{_path(found.points)}" fill="none" stroke="currentColor" '
            'stroke-width="2"/>'
            "</svg>"
            f"<p>{first[0].isoformat()}: {float(first[1]):.1f}"
            f" &rarr; {last[0].isoformat()}: {float(last[1]):.1f}"
            f" &middot; {len(found.points)} points</p>"
        )
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{found.label}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:2rem auto;"
        "padding:0 1rem;color:#111;background:#fff}"
        "@media(prefers-color-scheme:dark){body{color:#eee;background:#111}}</style>"
        f"<h1>{found.label}</h1>{body}"
        f"<p><small>As of {as_of.isoformat()}. Drawn from the stored rollups, "
        "so this and the coach cannot disagree.</small></p>"
    )


def page(conn: psycopg.Connection, kind: str, as_of: date) -> tuple[int, str]:
    """Status and body for the HTTP route."""
    if kind not in KINDS:
        return 404, "<!doctype html><meta charset=utf-8><title>Not found</title><p>No such chart."
    return 200, render(series(conn, kind, as_of), as_of)
