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
    sport: str | None = None,
    sub_sport: str | None = None,
) -> bytes:
    """A FIT file with one record message per second.

    `sport` is optional because a file that declares none is its own case: the
    watched folder path has to decide what to call it, and the fixtures that
    predate the sport message go on exercising that branch by leaving it off.
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.sport_message import SportMessage
    from fit_tool.profile.profile_type import Sport, SubSport

    n = max(len(power or []), len(heart_rate or []), len(cadence or []), len(altitude or []))
    if n == 0:
        raise ValueError("build_fit needs at least one series")

    builder = FitFileBuilder(auto_define=True)
    base_ms = int(start.timestamp() * 1000)

    if sport is not None:
        message = SportMessage()
        message.sport = Sport[sport.upper()]
        if sub_sport is not None:
            message.sub_sport = SubSport[sub_sport.upper()]
        builder.add(message)

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


def build_recordless_fit(
    start: datetime | None = None,
    file_type: str = "ACTIVITY",
    sport: str | None = None,
    sub_sport: str | None = None,
    with_file_id: bool = True,
    with_session: bool = True,
) -> bytes:
    """A FIT file with a header and a summary but no record stream.

    Two real things wear this shape and they need opposite handling, so both are
    buildable here. An aborted ride keeps `file_id.type = activity` and a
    `session` that says when and what — Zwift and Garmin both write those before
    the samples are flushed. A settings, workout or course file has the same
    absence of records and was never an activity at all.

    `with_file_id` off is the third case and not a hypothetical: nothing in the
    FIT spec obliges a writer to be reachable, and every fixture in this suite
    predating `file_id` is a file that declares no type.
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import FileType, Sport, SubSport

    builder = FitFileBuilder(auto_define=True)
    stamp = int(start.timestamp() * 1000) if start else None

    if with_file_id:
        ident = FileIdMessage()
        ident.type = FileType[file_type.upper()]
        if stamp is not None:
            ident.time_created = stamp
        builder.add(ident)

    if with_session and stamp is not None:
        session = SessionMessage()
        session.start_time = stamp
        # The device's own totals. Written because a real file has them, and
        # asserted nowhere in the session row: FIT-03 and the `data_unavailable`
        # contract both say these must not be read.
        session.total_elapsed_time = 3600
        if sport is not None:
            session.sport = Sport[sport.upper()]
            if sub_sport is not None:
                session.sub_sport = SubSport[sub_sport.upper()]
        builder.add(session)

    return bytes(builder.build().to_bytes())
