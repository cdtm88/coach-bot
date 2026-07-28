"""Build real FIT files for the ingest tests.

FIT-03 and FIT-14 are about parsing an actual file, so the tests parse actual
files rather than a stand-in. fit-tool writes them; fitdecode reads them back.
"""

from __future__ import annotations

import gzip
from datetime import datetime


def gzipped(data: bytes) -> bytes:
    """Wrap FIT bytes the way intervals.icu serves an original file.

    The download endpoint returns gzip; the cookbook's own example writes the
    response to `activity.fit.gz`. Tests that build only plain files cannot see
    the difference, which is exactly how the missing decompression went unnoticed.
    """
    return gzip.compress(data)


def build_fit(
    start: datetime,
    power: list[int] | None = None,
    heart_rate: list[int] | None = None,
    cadence: list[int] | None = None,
    altitude: list[float] | None = None,
    distance_step_m: float = 8.0,
) -> bytes:
    """A FIT file with one record message per second."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.record_message import RecordMessage

    n = max(len(power or []), len(heart_rate or []), len(cadence or []), len(altitude or []))
    if n == 0:
        raise ValueError("build_fit needs at least one series")

    builder = FitFileBuilder(auto_define=True)
    base_ms = int(start.timestamp() * 1000)

    for i in range(n):
        record = RecordMessage()
        record.timestamp = base_ms + i * 1000
        record.distance = distance_step_m * i
        if power and i < len(power):
            record.power = power[i]
        if heart_rate and i < len(heart_rate):
            record.heart_rate = heart_rate[i]
        if cadence and i < len(cadence):
            record.cadence = cadence[i]
        if altitude and i < len(altitude):
            record.altitude = altitude[i]
        builder.add(record)

    return bytes(builder.build().to_bytes())
