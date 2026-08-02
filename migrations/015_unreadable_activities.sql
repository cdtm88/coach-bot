-- Activities the platform holds but will not hand over.
--
-- intervals.icu answers the activity list with a placeholder for anything it
-- syncs from Strava: an id, a start time, `"source": "STRAVA"` and a note
-- saying "STRAVA activities are not available via the API". No type, no name,
-- no duration, no samples, ever. On the live account these are the gym and golf
-- sessions Whoop writes to Strava — six of them in the last four weeks.
--
-- Ingest used to make an ordinary session out of each one, which said something
-- false in both directions: it claimed a session existed with discipline
-- 'other' and no numbers, and it gave the coach no way to tell that apart from
-- a real activity whose data had gone missing.
--
-- Dropping them instead would say something worse. FIT-12's missed check counts
-- sessions on the day before concluding a prescription was skipped, so deleting
-- the only evidence that the athlete trained is what turns a gym session he
-- actually did into a missed one.
--
-- So the row stays and says what it is. The flag is what the review path, the
-- missed verdict and the coach's context all read to distinguish "he did
-- something I cannot describe" from "he did nothing".
alter table sessions add column data_unavailable boolean not null default false;

-- The context block asks "is there an unreadable activity on a day with an
-- unmatched prescription", which is a lookup by date over a handful of rows in
-- a table that is mostly readable ones.
create index sessions_data_unavailable on sessions (local_date) where data_unavailable;
