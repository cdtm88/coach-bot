#!/usr/bin/env python
"""The four empirical checks against a live intervals.icu account.

Each one blocks something and none can be answered from the OpenAPI spec, which
is why they are here rather than settled in docs/intervals-api.md. Run them with
a real key and paste the output into that file with the date.

    uv run python scripts/verify_intervals.py v2      # read only
    uv run python scripts/verify_intervals.py v3      # read only by default
    uv run python scripts/verify_intervals.py v1      # writes, self cleaning
    uv run python scripts/verify_intervals.py v4      # writes, self cleaning
    uv run python scripts/verify_intervals.py all     # every read only check

Needs INTERVALS_API_KEY in the environment. Nothing here prints it, and nothing
writes it anywhere; the output is safe to paste into an issue or a chat.

Two of these write to the live account. Neither writes without an explicit flag,
and the one irreversible step refuses to run without a date you chose yourself.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from coach.ingest import client as clientmod  # noqa: E402
from coach.ingest import parse  # noqa: E402

MARKER = "coach:verify:1"


def _api() -> clientmod.Intervals:
    if not os.environ.get("INTERVALS_API_KEY"):
        sys.exit(
            "INTERVALS_API_KEY is not set.\n"
            "Get it from the bottom of https://intervals.icu/settings, put it in .env,\n"
            "and export it, or set it in your environment configuration."
        )
    return clientmod.Intervals()


def _rate_limit(api: clientmod.Intervals) -> None:
    """Report the headroom. Decides whether COACH_POLL_INTERVAL_S is sane."""
    limit = api.last_limit
    print("\n  rate limit after this call:")
    print(f"    15 minute window remaining : {limit.window_remaining}")
    print(f"    daily remaining            : {limit.daily_remaining}")
    if limit.daily_remaining:
        # The poll is one call per pass; the default cadence is 120s.
        per_day_at_120s = 720
        print(f"    a 120s poll costs ~{per_day_at_120s}/day")
        if limit.daily_remaining < per_day_at_120s * 2:
            print("    ^ TIGHT. Raise COACH_POLL_INTERVAL_S; the folder path is unaffected.")
        else:
            print("    ^ comfortable at the default cadence")


# --- V2 ---------------------------------------------------------------------


def v2(api: clientmod.Intervals) -> None:
    """Is the activity file gzip transport encoding, or gzip payload?

    httpx strips the first transparently and leaves the second alone. The sniff
    based fix in parse.decompressed is correct under both, so this blocks
    nothing; it tells us which failure the tests should simulate.
    """
    print("V2. File encoding on the wire")
    print("=" * 60)

    recent = api.activities(date.today() - timedelta(days=90))
    rides = [a for a in recent if a.get("type") in ("Ride", "VirtualRide")]
    if not rides:
        print("  no rides in the last 90 days; widen the window and retry")
        return

    activity = rides[0]
    activity_id = activity["id"]
    print(f"  activity   : {activity_id}  {activity.get('name')!r}")
    print(f"  source     : {activity.get('source')}")

    # Deliberately the raw response rather than client.original_file, which
    # decompresses. The point is to see what arrives.
    response = api._get(f"/activity/{activity_id}/file")  # noqa: SLF001
    body = response.content

    print(f"  status     : {response.status_code}")
    print(f"  Content-Type     : {response.headers.get('content-type')}")
    print(f"  Content-Encoding : {response.headers.get('content-encoding')}")
    print(f"  first two bytes  : {body[:2].hex()}  ({len(body)} bytes total)")

    gzipped = body.startswith(parse.GZIP_MAGIC)
    print()
    if gzipped:
        print("  VERDICT: gzip PAYLOAD. httpx did not strip it, so the sniff is doing")
        print("           real work. Tests must simulate compressed bytes.")
        plain = parse.decompressed(body)
        print(f"           decompresses to {len(plain)} bytes")
    else:
        print("  VERDICT: not gzipped on arrival. Either httpx stripped a")
        print("           Content-Encoding header, or the endpoint changed. The sniff")
        print("           is harmless either way, but the archive stores plain bytes.")

    try:
        parsed = parse.from_fit(body)
        print(f"  parses OK  : {parsed.sample_count} samples, avg {parsed.avg_power_w} W")
    except parse.UnparseableActivity as exc:
        print(f"  PARSE FAILED: {exc}")

    _rate_limit(api)


# --- V3 ---------------------------------------------------------------------


def v3_read(api: clientmod.Intervals) -> None:
    """Does `weight` move day to day, or is it a static profile field?

    Open item 1. If it repeats on every date it is copied from the profile and
    is actively harmful: the coach would anchor on a stale number and never
    notice. If it moves, something already feeds it and HealthBridge (open item
    2) is unnecessary.
    """
    print("V3a. Wellness read, last 21 days")
    print("=" * 60)

    today = date.today()
    rows = api.wellness(today - timedelta(days=21), today)
    if not rows:
        print("  no wellness rows at all")
        return

    print(f"  {len(rows)} rows\n")
    header = (
        f"  {'date':<12} {'weight':>8} {'rHR':>5} {'hrv':>6} {'sleep':>7} {'readi':>6} {'lock':>5}"
    )
    print(header)
    weights = []
    for row in sorted(rows, key=lambda r: str(r.get("id"))):
        weight = row.get("weight")
        if weight is not None:
            weights.append(float(weight))
        sleep_h = row.get("sleepSecs")
        print(
            f"  {str(row.get('id')):<12} "
            f"{('' if weight is None else f'{float(weight):.1f}'):>8} "
            f"{str(row.get('restingHR') or ''):>5} "
            f"{str(row.get('hrv') or ''):>6} "
            f"{('' if sleep_h is None else f'{sleep_h / 3600:.1f}h'):>7} "
            f"{str(row.get('readiness') or ''):>6} "
            f"{str(row.get('locked') or ''):>5}"
        )

    print("\n  --- open item 1 ---")
    distinct = sorted(set(weights))
    if not weights:
        print("  VERDICT: no weight on any day. Nothing feeds it; HealthBridge is needed")
        print("           to write body mass, and HLTH-04 has no source until then.")
    elif len(distinct) == 1:
        print(f"  VERDICT: weight is {distinct[0]} on every one of {len(weights)} days.")
        print("           STATIC PROFILE FIELD. Actively harmful: a trend fitted on this")
        print("           is a flat line the coach would believe. HealthBridge is needed.")
    else:
        print(
            f"  VERDICT: weight MOVES. {len(distinct)} distinct values across "
            f"{len(weights)} days, {min(weights)}..{max(weights)}."
        )
        print("           Something already feeds it. Open item 2 (HealthBridge) can")
        print("           probably be dropped; confirm what the source is.")

    print("\n  --- RECOV-02 field coverage (open item 3) ---")
    fields = (
        "sleepSecs",
        "sleepScore",
        "restingHR",
        "hrv",
        "hrvSDNN",
        "readiness",
        "respiration",
        "spO2",
    )
    for name in fields:
        present = sum(1 for r in rows if r.get(name) is not None)
        verdict = "populated" if present else "ALWAYS NULL -> dropped from the deviation"
        print(f"    {name:<12} {present:>3}/{len(rows)} days  {verdict}")

    _rate_limit(api)


def v3_write(api: clientmod.Intervals, target: str, lock: bool) -> None:
    """Does a provider resync overwrite an API written weight, and does `locked` stop it?

    Decides how MacroLog writes body mass. A forum report says an unlocked write
    reverts within minutes and `locked: true` prevents it, and that there is NO
    API PATH TO UNLOCK afterwards.
    """
    print(f"V3b. Wellness write to {target}, locked={lock}")
    print("=" * 60)
    if lock:
        print("  WARNING: locking is close to a one way door. There is no documented")
        print("           API path to unlock a day afterwards.")
        print()

    probe = 99.9
    body: dict[str, Any] = {"id": target, "weight": probe}
    if lock:
        body["locked"] = True

    response = api._client.put(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/wellness/{target}", json=body
    )
    print(f"  PUT status: {response.status_code}")
    if response.status_code >= 400:
        print(f"  FAILED: {response.text[:300]}")
        return

    readback = api.wellness(date.fromisoformat(target), date.fromisoformat(target))
    row = readback[0] if readback else {}
    print(f"  immediately after write: weight={row.get('weight')} locked={row.get('locked')}")
    print()
    print("  Now wait for a provider resync (an hour is usually enough, or force one")
    print("  from the Whoop side) and re-run:")
    print(f"    uv run python scripts/verify_intervals.py v3 --check-date {target}")
    print("  If the value survived, that is how body mass must be written.")


def v3_check(api: clientmod.Intervals, target: str) -> None:
    rows = api.wellness(date.fromisoformat(target), date.fromisoformat(target))
    row = rows[0] if rows else {}
    weight = row.get("weight")
    print(f"  {target}: weight={weight} locked={row.get('locked')}")
    if weight is not None and abs(float(weight) - 99.9) < 0.01:
        print("  VERDICT: the API written value SURVIVED the resync.")
    else:
        print("  VERDICT: the API written value was OVERWRITTEN. A write without")
        print("           `locked` cannot be trusted to persist.")


# --- V1 ---------------------------------------------------------------------


def v1(api: clientmod.Intervals, cleanup: bool = True) -> None:
    """Is `external_id` scoping meaningful for a personal API key?

    PLAN-02, PLAN-05 and PLAN-06 all rest on "events created by your
    application", which is defined for an OAuth client. A personal key has none,
    so oauth_client_id is probably null on everything we create. Blocks P08.
    """
    print("V1. External id scoping under an API key")
    print("=" * 60)

    when = (date.today() + timedelta(days=90)).isoformat() + "T06:00:00"
    event = {
        "category": "NOTE",
        "start_date_local": when,
        "name": "coach-bot verification probe",
        "description": "Created by scripts/verify_intervals.py. Safe to delete.",
        "external_id": MARKER,
    }

    print(f"  1. upsert one event dated {when}")
    created = api._client.post(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/events/bulk", params={"upsert": "true"}, json=[event]
    )
    print(f"     status {created.status_code}")
    if created.status_code >= 400:
        print(f"     FAILED: {created.text[:300]}")
        return

    day = date.fromisoformat(when[:10])
    found = api._get(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/events", oldest=day.isoformat(), newest=day.isoformat()
    ).json()
    mine = [e for e in found if e.get("external_id") == MARKER]
    print(f"  2. read back: {len(mine)} event(s) carrying our external_id")
    for e in mine:
        print(f"     id={e.get('id')}")
        print(f"     oauth_client_id = {e.get('oauth_client_id')!r}")
        print(f"     created_by_id   = {e.get('created_by_id')!r}")

    print("  3. upsert again with a changed name")
    api._client.post(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/events/bulk",
        params={"upsert": "true"},
        json=[dict(event, name="coach-bot verification probe (updated)")],
    )
    again = api._get(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/events", oldest=day.isoformat(), newest=day.isoformat()
    ).json()
    mine_again = [e for e in again if e.get("external_id") == MARKER]
    print(f"     now {len(mine_again)} event(s) — 1 means upsert matched, 2 means it duplicated")

    print()
    if mine and mine[0].get("oauth_client_id") is None:
        print("  VERDICT: oauth_client_id is NULL under an API key, as suspected.")
        print("           PLAN-05's orphan sweep cannot filter on it and must use an")
        print("           external_id prefix convention instead.")
    elif mine:
        print("  VERDICT: oauth_client_id IS populated. The documented scoping applies.")

    if len(mine_again) == 1:
        print("           Upsert on external_id works: one event, not two.")
    else:
        print(f"           UPSERT DID NOT MATCH: {len(mine_again)} events. PLAN-02 needs")
        print("           a different key.")

    if cleanup:
        print("\n  4. cleanup: bulk-delete by external_id")
        deleted = api._client.put(  # noqa: SLF001
            f"/athlete/{api.athlete_id}/events/bulk-delete", json=[{"external_id": MARKER}]
        )
        print(f"     status {deleted.status_code}, returned {deleted.text[:100]}")

    _rate_limit(api)


# --- V4 ---------------------------------------------------------------------

WORKOUT_MARKER = "coach:verify:workout"

# The step list V4 publishes. Chosen so the rendered file is checkable rather than
# plausible: three distinct power targets, a ramp, and a repeat whose count and
# durations are all different numbers, so a zwo that dropped or flattened any one
# of them cannot still look right.
PROBE_STEPS = [
    {"section": "Warmup", "duration_s": 600, "ramp_pct": (50, 70)},
    {
        "section": "Main",
        "repeat": 3,
        "steps": [
            {"duration_s": 240, "power_pct": 105},
            {"duration_s": 120, "power_pct": 55},
        ],
    },
    {"section": "Cooldown", "duration_s": 300, "power_pct": 45},
]


def v4(api: clientmod.Intervals, cleanup: bool = True) -> None:
    """Does native workout text compile into a zwo with the intervals we meant?

    PLAN-09's acceptance criterion, and the only one in P08 that a fake cannot
    answer: "the platform renders a published session as a valid zwo with the
    intended intervals". PLAN-10 forbids us generating the file, so the whole
    requirement rests on the platform doing it correctly from our text.
    """
    from coach.plans import workout as workoutmod

    print("V4. Native workout text compiles to a zwo (PLAN-09, PLAN-10)")
    print("=" * 60)

    text = workoutmod.render(PROBE_STEPS)
    print("  the text we publish:")
    for line in text.splitlines():
        print(f"    {line}")

    when = (date.today() + timedelta(days=91)).isoformat() + "T06:00:00"
    event = {
        "category": "WORKOUT",
        "start_date_local": when,
        "type": "Ride",
        "name": "coach-bot workout probe",
        "description": text,
        "external_id": WORKOUT_MARKER,
        "moving_time": 1980,
    }

    print(f"\n  1. publish one structured event dated {when}")
    created = api.upsert_events([event])
    event_id = created[0].get("id") if created else None
    print(f"     upstream id {event_id}")

    if not event_id:
        print("     FAILED: no id returned, so there is nothing to download.")
        return

    print("  2. download it back as zwo")
    response = api._client.get(  # noqa: SLF001
        f"/athlete/{api.athlete_id}/events/{event_id}/download.zwo"
    )
    print(f"     status {response.status_code}, {len(response.content)} bytes")

    if response.status_code >= 400:
        print(f"     FAILED: {response.text[:300]}")
    else:
        _report_zwo(response.text)

    if cleanup:
        print("\n  3. cleanup: bulk-delete by external_id")
        deleted = api.delete_events([WORKOUT_MARKER])
        print(f"     eventsDeleted = {deleted}")

    _rate_limit(api)


def _report_zwo(body: str) -> None:
    """Say what the platform produced, and whether it is what we asked for.

    **Total ridden time is the real test, not the element count.** A zwo may express
    a repeat either way — three expanded blocks, or one block with a `Repeat`
    attribute — and both are correct. What is not correct is a 3x set arriving as a
    1x, and the only thing that catches that regardless of encoding is the duration
    summing to what we asked for. `PROBE_STEPS` is 1980 seconds; a file totalling
    660 has silently dropped two thirds of the session.
    """
    import re
    import xml.etree.ElementTree as ET

    expected = _expected_seconds(PROBE_STEPS)

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        print(f"     NOT VALID XML: {exc}")
        print(f"     first 200 chars: {body[:200]!r}")
        return

    print(f"     parses as XML, root <{root.tag}>")
    elements = [el for el in root.iter() if el.tag not in {root.tag, "workout"}]
    print(f"     elements: {', '.join(sorted({el.tag for el in elements})) or 'none'}")

    powers = sorted({m for m in re.findall(r'Power\w*="([\d.]+)"', body)})
    print(f"     distinct power attributes: {len(powers)} -> {powers[:8]}")

    # A repeat comes back as `<IntervalsT Repeat="3" OnDuration=.. OffDuration=..>`
    # and carries **no** `Duration` attribute. An earlier version of this check
    # skipped anything without one, so it scored a correctly rendered set as a
    # third of its length and reported a shrunk session that was fine. Both
    # shapes are summed here.
    total = 0
    for el in elements:
        try:
            repeat = int(el.get("Repeat") or 1)
            on = int(el.get("OnDuration") or 0)
            off = int(el.get("OffDuration") or 0)
            if on or off:
                total += repeat * (on + off)
            elif el.get("Duration") is not None:
                total += repeat * int(float(el.get("Duration")))
        except ValueError:
            continue

    print(f"     total duration: {total}s (we asked for {expected}s)")

    print()
    if total == expected and len(powers) >= 3:
        print("  VERDICT: the platform compiled our text into a structured file with")
        print("           the intervals we meant. PLAN-09 and PLAN-10 hold: the coach")
        print("           sends a step list and never a file.")
    elif total and total < expected:
        print(f"  VERDICT: THE SESSION SHRANK. {total}s arrived against {expected}s sent.")
        print("           Something dropped, most likely the repeat count — a 3x set")
        print("           rendered as 1x would be ridden a third as hard. Do not trust")
        print("           PLAN-09 until this is understood. Full file below.")
        print("\n".join(f"       {line}" for line in body.splitlines()))
    else:
        print(f"  VERDICT: cannot confirm. {total}s parsed against {expected}s sent.")
        print("           Read the file below rather than assuming either way.")
        print("\n".join(f"       {line}" for line in body.splitlines()))


def _expected_seconds(steps: list[dict[str, Any]]) -> int:
    """What `PROBE_STEPS` should add up to, from the step list rather than a constant.

    Computed so editing the probe cannot leave a stale expectation behind — the
    number in the verdict is derived from the same thing that was published.
    """
    total = 0
    for step in steps:
        if "repeat" in step:
            total += int(step["repeat"]) * _expected_seconds(step.get("steps") or [])
        else:
            total += int(step.get("duration_s") or 0)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("check", choices=["v1", "v2", "v3", "v4", "all"])
    ap.add_argument("--write", action="store_true", help="v3: perform the wellness write")
    ap.add_argument("--lock", action="store_true", help="v3: send locked=true (NOT REVERSIBLE)")
    ap.add_argument("--date", help="v3 --write: the day to write to. Pick one you do not need.")
    ap.add_argument("--check-date", help="v3: re-read this date and report whether it survived")
    ap.add_argument("--no-cleanup", action="store_true", help="v1/v4: leave the probe event behind")
    args = ap.parse_args()

    with _api() as api:
        if args.check == "v2":
            v2(api)
        elif args.check == "v1":
            v1(api, cleanup=not args.no_cleanup)
        elif args.check == "v4":
            v4(api, cleanup=not args.no_cleanup)
        elif args.check == "v3":
            if args.check_date:
                v3_check(api, args.check_date)
            elif args.write:
                if not args.date:
                    sys.exit(
                        "v3 --write needs --date YYYY-MM-DD.\n"
                        "Pick a day you do not care about: with --lock there is no\n"
                        "documented way to unlock it afterwards."
                    )
                v3_write(api, args.date, lock=args.lock)
            else:
                v3_read(api)
        else:
            v2(api)
            print("\n")
            v3_read(api)
            print("\n  (v1, v4 and the v3 write half are not in `all`: they write.)")


if __name__ == "__main__":
    main()
