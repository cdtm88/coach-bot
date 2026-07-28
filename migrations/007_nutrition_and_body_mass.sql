-- P04: macros from MacroLog, body mass from intervals.icu wellness.
--
-- Requirements: HLTH-01 to HLTH-16.
--
-- One finding shapes this migration. A wellness read across the last 21 days on
-- the live account on 28 July 2026 returned 13 populated days and `weight` null
-- on every one of them (open item 1). So nothing currently feeds body mass, the
-- readings table below stays empty until MacroLog's HealthBridge writes to
-- wellness, and every threshold in HLTH-07 to HLTH-16 has to hold on an empty
-- series as naturally as on a full one. That is why the claim gate is a computed
-- table rather than a rule the coach is asked to remember.

-- HLTH-01 and HLTH-02: per-meal granularity, not daily aggregates. MacroLog
-- posts meals as they are logged; a day with four meals stores four rows.
create table meals (
  -- HLTH-03: MacroLog's own meal id is the idempotency key. Replaying a payload
  -- updates in place, and a delete upstream deletes the row here.
  external_id   text primary key,

  eaten_at      timestamptz not null,
  -- TZ-01: the local day the meal belongs to, decided by the athlete's
  -- configured timezone rather than by UTC. Stored so the daily rollups in P10
  -- do not re-derive an offset on every query.
  local_date    date not null,

  name          text,
  kcal          numeric,
  protein_g     numeric,
  carbs_g       numeric,
  fat_g         numeric,
  fibre_g       numeric,

  -- Whatever else MacroLog sent. Kept whole so a field added on the phone is not
  -- silently dropped by a server that has not been redeployed.
  payload       jsonb not null default '{}'::jsonb,

  received_at   timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index meals_local_date on meals (local_date desc, eaten_at);

-- RECOV-01 and HLTH-04: one row per date, mirroring the wellness feed.
--
-- Note what is absent: there is no body fat column. HLTH-14 excludes body fat
-- from v1 entirely, and a column nobody fills is how an excluded metric quietly
-- becomes available. Adding it later is a migration, deliberately.
create table wellness (
  local_date    date primary key,

  -- HLTH-04. Null on every day of the live account as of 28 July 2026.
  weight_kg     numeric,

  -- RECOV-02's six fields. Recorded absent rather than defaulted, because a
  -- null here means "the feed did not carry it" and a zero would mean "the
  -- athlete did not sleep".
  sleep_secs    int,
  sleep_score   numeric,
  resting_hr    int,
  hrv           numeric,
  -- Populated 0/22 days on the live account: the Whoop link fills `hrv` and
  -- leaves this null, so RECOV-02 drops it from the deviation. Stored anyway,
  -- because the column is how we would notice it starting to arrive.
  hrv_sdnn      numeric,
  readiness     numeric,
  respiration   numeric,
  spo2          numeric,

  -- A connected provider can resync over an API-written value. `locked` is the
  -- upstream flag that stops it. Mirrored so we can see which days upstream
  -- considers frozen without a second read.
  locked        boolean not null default false,

  raw           jsonb not null default '{}'::jsonb,
  fetched_at    timestamptz not null default now()
);

-- HLTH-04: body mass readings, sourced from the wellness feed and never from
-- HealthKit. A separate table from `wellness` because a reading carries state
-- the feed does not: HLTH-11 holds an outlier out of the trend until it has been
-- confirmed once, and that status has to live somewhere that is not a mirror of
-- upstream.
create table body_mass_readings (
  id            bigserial primary key,
  local_date    date not null unique,
  weight_kg     numeric not null check (weight_kg > 0),

  -- 'wellness' is the only path HLTH-04 permits for a measurement. 'stated' is
  -- the athlete telling the coach a number in conversation, which is evidence
  -- about a reading rather than a reading, and is excluded from the fit.
  source        text not null default 'wellness'
                check (source in ('wellness', 'stated')),

  -- HLTH-11: an outlier waits for one light confirmation before it enters the
  -- trend. Only 'accepted' rows are fitted.
  status        text not null default 'accepted'
                check (status in ('accepted', 'pending_confirmation', 'rejected')),
  -- Why it was held, so the confirmation can be asked in one sentence rather
  -- than recomputed.
  outlier_delta numeric,
  confirmed_at  timestamptz,

  created_at    timestamptz not null default now()
);

create index body_mass_readings_local_date on body_mass_readings (local_date desc);
create index body_mass_readings_accepted on body_mass_readings (local_date desc)
  where status = 'accepted';

-- HLTH-13: a scheduled break suppresses weigh in prompting entirely.
--
-- Introduced here rather than in P10 because HLTH-13 is a P04 requirement and a
-- suppression rule with nothing to read is a rule that has never run. BREAK-01
-- to BREAK-04 extend this table in P10; the columns they need are already here
-- so that phase adds behaviour rather than schema.
create table breaks (
  id          bigserial primary key,
  kind        text not null check (kind in ('holiday', 'travel', 'illness')),
  starts_on   date not null,
  -- BREAK-01: optional, and BREAK-04 makes it inert for illness — an illness
  -- break never auto-resumes, so an end date on one is a note rather than a
  -- trigger.
  ends_on     date,
  reason      text,
  ended_at    timestamptz,
  created_at  timestamptz not null default now(),
  constraint breaks_end_after_start check (ends_on is null or ends_on >= starts_on)
);

create index breaks_window on breaks (starts_on desc);

-- MEM-08: the weight trend is computed in SQL and read from here. The model
-- never fits a line, and never quotes a rate it derived itself.
--
-- HLTH-08 requires a rate to be stated as a range rather than a point estimate,
-- so the range is stored alongside the slope. Storing only the slope would leave
-- the model to invent the uncertainty, which is the failure the requirement
-- exists to prevent.
alter table rollups add column weight_trend_low     numeric;  -- kg/week, low end
alter table rollups add column weight_trend_high    numeric;  -- kg/week, high end
alter table rollups add column weight_span_days     int;      -- first to last reading
alter table rollups add column weight_weekday_bias  boolean;  -- HLTH-10
alter table rollups add column weight_weeks_covered int;      -- HLTH-16 and NUT-04
alter table rollups add column weight_latest_kg     numeric;  -- HLTH-07: reportable alone

comment on column rollups.weight_trend_slope is
  'kg per week, fitted per HLTH-06 over a 28 day window with exponential recency weights.';
comment on column rollups.weight_trend_low is
  'HLTH-08: low end of the rate range. Never quote weight_trend_slope as a point estimate.';
