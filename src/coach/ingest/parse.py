"""Parsing activity files and streams into values we computed ourselves.

FIT-03: the original activity file is downloaded and parsed, and intervals.icu
derived fields are stored alongside but never substituted for parsed values.

That requirement has teeth here because the platform has no undecorated average
power. Its average is `icu_average_watts`, a derived field like the rest. So
either we compute the average from samples or the session has no parsed average
at all. Everything in :class:`Parsed` is arithmetic over samples this module read.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# gzip's magic number. intervals.icu serves original activity files compressed —
# the cookbook's own example writes the response to `activity.fit.gz`.
GZIP_MAGIC = b"\x1f\x8b"

# Normalised power is a 30 second rolling average raised to the fourth power,
# averaged, then the fourth root. Below this many samples the rolling window is
# longer than the ride and the number would be meaningless.
NP_WINDOW_S = 30


class UnparseableActivity(ValueError):
    """The file or stream payload could not be read as an activity."""


class NotAnActivityFile(UnparseableActivity):
    """A perfectly readable FIT file that does not describe an activity.

    FIT is a container format and a device writes far more than rides into it:
    settings, courses, workouts, daily monitoring. Dropping one of those in the
    watched folder is not a fault and there is nothing to ingest, which is a
    different answer from "this was a ride and the file is broken". Separated
    because the two get handled differently — see :func:`coach.ingest.archive.
    ingest_file`, where one becomes a session and the other becomes a note in
    the archive saying not to try again.
    """


class AbandonedActivity(UnparseableActivity):
    """An activity file describing a session that never ran.

    Zwift writes one of these every time a ride is started and immediately
    ended: a complete, valid, ~1.5 KB activity file whose session records no
    elapsed time, no distance and no power. Two were sitting in the deployment's
    watched folder, and the real rides for both days are separate files — one of
    them written seven seconds later.

    Distinct from a ride whose samples were lost, which this path must record
    rather than drop (migration 015). Recording *this* would be the mirror
    error: a `data_unavailable` row asserting the athlete trained, on a day he
    has already been credited for, which then suppresses FIT-12's missed check
    for that day.
    """


@dataclass
class Parsed:
    """Values computed from samples. Never populated from an `icu_` field."""

    started_at: datetime | None = None
    duration_s: int | None = None
    distance_m: float | None = None
    elevation_m: float | None = None
    avg_power_w: float | None = None
    np_power_w: float | None = None
    max_power_w: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_cadence: float | None = None
    sample_count: int = 0
    # What the file says it is, in FIT's own vocabulary ('cycling',
    # 'virtual_activity'). Not a value computed from samples, but the file's own
    # statement about itself, and the only thing a watched folder ingest has to
    # go on when there is no platform to ask.
    sport: str | None = None
    sub_sport: str | None = None
    # What the file says it *is*, from `file_id`: 'activity', 'workout',
    # 'course', 'settings'. Distinct from `sport`, which says what was done.
    # A file that carries no `file_id` leaves this None, which reads as "did not
    # say" rather than "said it was an activity" — every fixture written before
    # this existed is in that position.
    file_type: str | None = None
    # An activity file that carries no samples at all. Not a parse failure: the
    # athlete rode, and the file that survived says only when. Everything in
    # this dataclass except `started_at` is therefore None, and the session
    # built from it is FIT-15's `data_unavailable` — see migration 015 for why
    # that row exists rather than nothing.
    samples_missing: bool = False
    source_kind: str = "unknown"
    warnings: list[str] = field(default_factory=list)


def _enum_name(raw: object) -> str | None:
    """A FIT enum as its name, or None if the file left it unset.

    fitdecode resolves a known enum to its name and leaves an unknown one as the
    raw integer. An integer is not a sport, so it reads as absent rather than as
    a value nothing can interpret.
    """
    return raw.strip().lower() or None if isinstance(raw, str) else None


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def normalised_power(power: list[float]) -> float | None:
    """NP over a per-second power series.

    Returns None rather than a wrong number when the series is shorter than the
    rolling window; a 20 second effort has no meaningful NP.
    """
    clean = [float(p) for p in power if p is not None]
    if len(clean) < NP_WINDOW_S:
        return None
    rolling = []
    total = sum(clean[:NP_WINDOW_S])
    rolling.append(total / NP_WINDOW_S)
    for i in range(NP_WINDOW_S, len(clean)):
        total += clean[i] - clean[i - NP_WINDOW_S]
        rolling.append(total / NP_WINDOW_S)
    fourth = sum(r**4 for r in rolling) / len(rolling)
    return round(fourth**0.25, 1)


def _summarise(
    power: list[float],
    hr: list[float],
    cadence: list[float],
    distance_m: float | None,
    elevation_m: float | None,
    started_at: datetime | None,
    duration_s: int | None,
    kind: str,
) -> Parsed:
    parsed = Parsed(
        started_at=started_at,
        duration_s=duration_s,
        distance_m=distance_m,
        elevation_m=elevation_m,
        sample_count=max(len(power), len(hr), len(cadence)),
        source_kind=kind,
    )

    if power:
        parsed.avg_power_w = round(_mean(power) or 0, 1)
        parsed.max_power_w = max(power)
        parsed.np_power_w = normalised_power(power)
        if parsed.np_power_w is None:
            parsed.warnings.append(
                f"fewer than {NP_WINDOW_S} power samples; normalised power not computed"
            )
    if hr:
        mean_hr = _mean(hr)
        parsed.avg_hr = round(mean_hr) if mean_hr is not None else None
        parsed.max_hr = round(max(hr))
    if cadence:
        # Zwift's own average includes freewheeling seconds, which the source
        # coaching conversation records as having produced a wrong conclusion
        # about grinding. Zero-cadence samples are excluded here for that reason.
        pedalling = [c for c in cadence if c and c > 0]
        if pedalling:
            parsed.avg_cadence = round(_mean(pedalling) or 0, 1)

    return parsed


def decompressed(data: bytes) -> bytes:
    """Gunzip if the bytes are gzipped, otherwise return them unchanged.

    Sniffing the magic number rather than branching on which endpoint produced
    the bytes is deliberate. httpx strips `Content-Encoding: gzip` transparently
    but leaves a gzipped *payload* alone, and the two are indistinguishable to
    the caller, so a rule based on the endpoint would be right only by luck.
    Sniffing is correct under both.

    Idempotent, so it is safe to call at every boundary rather than exactly one.
    """
    if not data.startswith(GZIP_MAGIC):
        return data
    try:
        return gzip.decompress(data)
    except OSError as exc:
        # Starts with the magic number but will not inflate. Treat as opaque
        # rather than guessing; the caller's parse will fail with a clearer error.
        log.warning("bytes look gzipped but did not inflate: %s", exc)
        return data


def from_fit(data: bytes) -> Parsed:
    """Parse a FIT file. FIT-03 and FIT-14 both arrive here."""
    import fitdecode

    data = decompressed(data)

    power: list[float] = []
    hr: list[float] = []
    cadence: list[float] = []
    started_at: datetime | None = None
    last_timestamp: datetime | None = None
    distance_m: float | None = None
    elevation_gain = 0.0
    last_altitude: float | None = None
    sport: str | None = None
    sub_sport: str | None = None
    file_type: str | None = None
    session_start: datetime | None = None
    session_seconds: float | None = None

    def _aware(raw: object) -> datetime | None:
        if not isinstance(raw, datetime):
            return None
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)

    try:
        with fitdecode.FitReader(io.BytesIO(data)) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue

                def value(name: str, f: object = frame) -> object:
                    return f.get_value(name) if f.has_field(name) else None  # type: ignore[attr-defined]

                # The `sport` message carries it, and so does `session`. Taking
                # the first that appears rather than the last: a multisport file
                # would otherwise be named by whichever leg finished, and its
                # first leg is at least a leg it actually contains.
                if frame.name in ("sport", "session"):
                    sport = sport or _enum_name(value("sport"))
                    sub_sport = sub_sport or _enum_name(value("sub_sport"))

                # Read for the sake of a file with no records at all. `file_id`
                # is the only message that says what kind of file this is, and
                # `session` is what says whether a ride happened and when. Used
                # to decide *existence*, never to fill a column — see
                # `_no_samples`, where FIT-03 is argued in full.
                #
                # `file_id.time_created` is deliberately not read. It says a
                # file was made, which is not the same claim, and on the live
                # deployment the difference is the whole question: Zwift stamps
                # it on abandoned starts too.
                if frame.name == "file_id":
                    file_type = file_type or _enum_name(value("type"))
                if frame.name == "session":
                    session_start = session_start or _aware(value("start_time"))
                    if session_seconds is None:
                        raw = value("total_timer_time")
                        raw = value("total_elapsed_time") if raw is None else raw
                        session_seconds = float(raw) if isinstance(raw, int | float) else None

                if frame.name != "record":
                    continue

                ts = value("timestamp")
                if isinstance(ts, datetime):
                    ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
                    started_at = started_at or ts
                    last_timestamp = ts

                for name, sink in (("power", power), ("heart_rate", hr), ("cadence", cadence)):
                    v = value(name)
                    if v is not None:
                        sink.append(float(v))

                d = value("distance")
                if d is not None:
                    distance_m = float(d)

                alt = value("altitude") or value("enhanced_altitude")
                if alt is not None:
                    alt = float(alt)
                    if last_altitude is not None and alt > last_altitude:
                        elevation_gain += alt - last_altitude
                    last_altitude = alt
    except Exception as exc:  # noqa: BLE001 - any malformed file lands here
        raise UnparseableActivity(f"not a readable FIT file: {exc}") from exc

    if not (power or hr or cadence or started_at):
        return _no_samples(file_type, session_start, session_seconds, sport, sub_sport)

    duration = None
    if started_at and last_timestamp:
        duration = int((last_timestamp - started_at).total_seconds())

    parsed = _summarise(
        power,
        hr,
        cadence,
        distance_m,
        round(elevation_gain, 1) if elevation_gain else None,
        started_at,
        duration,
        "fit",
    )
    parsed.sport, parsed.sub_sport, parsed.file_type = sport, sub_sport, file_type
    return parsed


def _no_samples(
    file_type: str | None,
    session_start: datetime | None,
    session_seconds: float | None,
    sport: str | None,
    sub_sport: str | None,
) -> Parsed:
    """A FIT file with no record messages, and which of four things that means.

    The distinction that matters is not "can this be read" but **did an activity
    happen**, and a file with no samples can answer that either way.

    *Not an activity at all.* A settings, workout or course file, which a device
    or a sync tool will happily leave in the same folder. Nothing to ingest and
    nothing wrong; :class:`NotAnActivityFile`.

    *An activity that never ran.* Zwift writes a complete 1.5 KB activity file
    every time a ride is started and abandoned: `file_id.type` is `activity`,
    there is a `session` and a `lap` and an `activity` message, and every number
    in them is zero. On the live deployment the file at 21 July 13:02:17 is one
    of these and the real ride begins **seven seconds later** in its own file.
    :class:`AbandonedActivity`, because recording it would claim the athlete
    trained on a day he is already correctly credited for — and worse, a
    `data_unavailable` row suppresses FIT-12's missed check for that day.

    Told apart by the session's own account of itself rather than by size or
    filename: it names no `start_time` and declares no elapsed time. A ride
    whose samples were lost mid-write still has both.

    *An activity whose samples did not survive.* The case migration 015 settled
    and settled the other way from how this path used to behave: **the row stays
    and says what it is**, because dropping it is what turns a session the
    athlete did into one FIT-12 reports he skipped. Returns a Parsed carrying
    the start time and nothing else, flagged `samples_missing`.

    FIT-10 is satisfied because the time is the session's own and not the ingest
    clock. FIT-03 is satisfied by omission: `session` also holds total elapsed
    time, distance and average power, and none of it reaches a column, because a
    `data_unavailable` session's contract with the weekly review is that its
    time and distance are *missing* rather than sourced elsewhere. Reading the
    duration to decide whether a ride occurred is a different act from storing
    it as the ride's duration, and only the second is what FIT-03 forbids.

    *Neither.* No records, and no session naming when it began. Nothing to date
    it by, and FIT-10 forbids inventing one.
    """
    if file_type is not None and file_type != "activity":
        raise NotAnActivityFile(f"a FIT {file_type} file, which is not an activity")

    # Zero is the file stating outright that the ride lasted no time — the
    # positive claim, so it is tested first. Zwift's stub omits `start_time`
    # *as well*, and answering "undateable" for it would be true but useless:
    # the file is not a ride we failed to place, it is a ride that never ran.
    #
    # None is a device that did not say, which is unknown rather than zero and
    # disqualifies nothing. Do not scale a null.
    if session_seconds is not None and session_seconds <= 0:
        raise AbandonedActivity("a session recording no elapsed time; a start that was abandoned")

    # No session message at all, or one that never says when it began. Nothing
    # here is evidence that an activity happened, only that a file exists.
    if session_start is None:
        raise UnparseableActivity("FIT file contained no record messages and no session start")

    parsed = Parsed(
        started_at=session_start, source_kind="fit", samples_missing=True, file_type=file_type
    )
    parsed.sport, parsed.sub_sport = sport, sub_sport
    parsed.warnings.append("no record messages; the activity is dated but has no samples")
    return parsed


def from_streams(streams: list[dict], started_at: datetime | None = None) -> Parsed:
    """Parse the streams endpoint's JSON into the same shape.

    Used when the original file is unavailable. intervals.icu does not serve
    original files for Strava activities, and the same is true of anything
    ingested without one, so this keeps FIT-03's "parsed" column populated from
    per-sample data rather than falling back to a derived aggregate.
    """
    by_type = {s.get("type"): s.get("data") or [] for s in streams}

    def series(name: str) -> list[float]:
        return [float(v) for v in by_type.get(name, []) if v is not None]

    power, hr, cadence = series("watts"), series("heartrate"), series("cadence")
    if not (power or hr or cadence):
        raise UnparseableActivity("streams payload carried no usable series")

    distance = by_type.get("distance") or []
    altitude = by_type.get("altitude") or []
    gain = sum(
        max(0.0, float(b) - float(a))
        for a, b in zip(altitude, altitude[1:], strict=False)
        if a is not None and b is not None
    )

    # Duration is the sample count because the streams endpoint serves one sample
    # per second. That is an assumption about the feed rather than arithmetic, so
    # it is worth naming: if the endpoint ever served a different rate, this would
    # be wrong in a way no test here would notice. The FIT path does not share the
    # assumption — it subtracts timestamps.
    duration_s = max(len(power), len(hr), len(cadence)) or None

    return _summarise(
        power,
        hr,
        cadence,
        float(distance[-1]) if distance else None,
        round(gain, 1) if gain else None,
        started_at,
        duration_s,
        "streams",
    )


def content_hash(data: bytes) -> str:
    """FIT-04: the content half of deduplication.

    Hashed after decompression, which is what makes the two ingest paths agree.
    The webhook downloads a gzipped original and the watched folder gets a plain
    FIT; hashing the bytes as received would give one ride two hashes and
    therefore two session rows, which is precisely the duplicate FIT-04 exists to
    prevent.
    """
    return hashlib.sha256(decompressed(data)).hexdigest()
