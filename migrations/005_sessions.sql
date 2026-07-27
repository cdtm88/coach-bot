-- P03: activity ingest.
--
-- Requirements: FIT-01 to FIT-17, SEC-02.

-- FIT-03 is the shape of this table. Values we parsed from the original file sit
-- in typed columns; everything intervals.icu computed sits in `derived` and is
-- never read as a substitute. The platform's own average power is
-- icu_average_watts, a derived field, so there is no upstream parsed average to
-- fall back on: avg_power_w below is computed from the samples or it is null.
create table sessions (
  id             bigserial primary key,

  -- FIT-04: the upstream activity id, plus a hash of the file we parsed.
  external_ref   text,
  content_hash   text,
  source         text not null default 'intervals'
                 check (source in ('intervals', 'local_file', 'chat')),

  -- FIT-07 and FIT-08: discipline decides whether power analysis applies at all,
  -- and every device path lands here identically.
  discipline     text not null,
  activity_type  text,
  name           text,

  -- FIT-10: from the activity data, never from ingest time. local_date is
  -- generated for the day boundary rules in TZ-01 and stored so a query can use
  -- it without re-deriving the offset.
  started_at     timestamptz not null,
  local_date     date not null,

  -- Parsed by us, from samples.
  duration_s     int,
  distance_m     numeric,
  elevation_m    numeric,
  avg_power_w    numeric,
  np_power_w     numeric,
  max_power_w    numeric,
  avg_hr         int,
  max_hr         int,
  avg_cadence    numeric,
  sample_count   int,

  -- Computed by intervals.icu. Stored whole, read as their opinion.
  derived        jsonb not null default '{}'::jsonb,

  -- FIT-17: an activity this coach authored, returning through the webhook.
  coach_authored boolean not null default false,

  -- FIT-05: set when a prescription matched.
  prescription_id bigint references prescriptions(id),

  -- FIT-09: history loaded in bulk produces no reviews and no messages.
  backfilled     boolean not null default false,
  reviewed_at    timestamptz,

  created_at     timestamptz not null default now()
);

-- FIT-04: one session per upstream activity, and one per distinct file.
create unique index sessions_one_per_external_ref
  on sessions (external_ref) where external_ref is not null;
create unique index sessions_one_per_content_hash
  on sessions (content_hash) where content_hash is not null;

create index sessions_started_at on sessions (started_at desc);
create index sessions_local_date on sessions (local_date desc);
create index sessions_unreviewed on sessions (created_at)
  where reviewed_at is null and backfilled = false;

-- The P00 migration deferred this so it could stand alone. This is its phase.
alter table prescriptions add column session_id bigint references sessions(id);
create index prescriptions_session_id on prescriptions (session_id);

-- FIT-14 and FIT-15: the local archive is a first class ingest path and is
-- retained permanently. Rows are never deleted in response to an upstream
-- change; FIT-16 replays them back upstream from here.
create table fit_archive (
  id            bigserial primary key,
  path          text not null unique,
  sha256        text not null unique,
  size_bytes    bigint not null,
  external_ref  text,             -- filled in once matched upstream, if ever
  session_id    bigint references sessions(id),
  discovered_at timestamptz not null default now(),
  restored_at   timestamptz       -- FIT-16: last time this was pushed upstream
);

create index fit_archive_session on fit_archive (session_id);

-- FIT-02: replay safety. intervals.icu authenticates the callback with a shared
-- secret in the body rather than an HMAC signature, so replay protection has to
-- live here: the same event for the same activity at the same timestamp is
-- recorded once and ignored thereafter.
create table webhook_deliveries (
  id              bigserial primary key,
  event_type      text not null,
  athlete_id      text,
  external_ref    text,
  event_timestamp timestamptz not null,
  received_at     timestamptz not null default now(),
  accepted        boolean not null,
  reason          text
);

create unique index webhook_deliveries_replay
  on webhook_deliveries (event_type, external_ref, event_timestamp)
  where external_ref is not null;

create index webhook_deliveries_received on webhook_deliveries (received_at desc);
