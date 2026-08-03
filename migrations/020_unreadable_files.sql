-- Why a file in the permanent archive produced no session.
--
-- FIT-15 keeps every file forever and never prunes, so `fit_archive` accumulates
-- rows whose `session_id` is null. Until now that column meant two different
-- things at once: "not ingested yet" and "cannot be ingested, ever". The second
-- was recorded nowhere.
--
-- The cost was not the missing record, it was the retry. `archive.scan` walks
-- the folder every poll and `ingest_file` re-reads, re-parses and re-fails on a
-- file whose bytes cannot change, logging the same warning each pass — on the
-- live deployment, two files doing this indefinitely since 12 and 21 July 2026.
-- A warning that repeats forever is one nobody reads, and reading it is what
-- would have shown that neither file was a ride at all.
--
-- Deliberately not a boolean. The reason is the operative fact: a settings file
-- is a non-event, a truncated ride is a ride that needs recovering from the
-- device, and a column that only says "no" cannot tell them apart six months
-- later.
alter table fit_archive
  add column unreadable_reason text,
  add column unreadable_at     timestamptz;

-- Small and read on every scan pass, once per file.
create index fit_archive_unreadable on fit_archive (sha256) where unreadable_reason is not null;
