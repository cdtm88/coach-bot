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
    source_kind: str = "unknown"
    warnings: list[str] = field(default_factory=list)


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

    try:
        with fitdecode.FitReader(io.BytesIO(data)) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name != "record":
                    continue

                def value(name: str, f: object = frame) -> object:
                    return f.get_value(name) if f.has_field(name) else None  # type: ignore[attr-defined]

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
        raise UnparseableActivity("FIT file contained no record messages")

    duration = None
    if started_at and last_timestamp:
        duration = int((last_timestamp - started_at).total_seconds())

    return _summarise(
        power,
        hr,
        cadence,
        distance_m,
        round(elevation_gain, 1) if elevation_gain else None,
        started_at,
        duration,
        "fit",
    )


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
