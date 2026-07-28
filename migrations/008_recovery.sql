-- P05: recovery from the intervals.icu wellness feed.
--
-- Requirements: RECOV-01 to RECOV-06. P04 landed RECOV-01, RECOV-02 and
-- RECOV-05 while it was reading the same feed for body mass; this migration
-- carries the columns RECOV-04's deviation and RECOV-06's cross check need.
--
-- The column list comes from a real read of the live feed on 28 July 2026 rather
-- than from the schema: 46 keys, of which 18 are populated on at least one day.
-- Two of those changed the design.

-- RECOV-02 lists three properties under "sleep" and P04 stored two of them.
alter table wellness add column sleep_quality numeric;

-- RECOV-06's load signal, and it is real: `atlLoad` was populated on all 22 days
-- read, zero on eight of them and non-zero on fourteen. That is exactly the
-- distinction the requirement turns on — load recorded with no activity means
-- the upload is missing, not the session.
--
-- `ctl` and `atl` are the platform's fitness and fatigue curves. Stored because
-- they are the context that makes a day's load readable, and never used as a
-- substitute for anything computed locally, per the FIT-03 rule.
alter table wellness add column ctl       numeric;
alter table wellness add column atl       numeric;
alter table wellness add column ctl_load  numeric;
alter table wellness add column atl_load  numeric;
alter table wellness add column ramp_rate numeric;

create index wellness_atl_load on wellness (local_date) where atl_load > 0;

-- RECOV-04: the deviation is computed locally from stored history and never
-- read off a platform score. `rollups.recovery_deviation` exists from P00; what
-- is added here is the evidence behind it, so a figure in a review can be traced
-- to the fields that produced it rather than taken on trust.
alter table rollups add column recovery_fields_used int;
alter table rollups add column recovery_components  jsonb;
alter table rollups add column recovery_baseline_n  int;

-- The platform's own recovery score, stored beside the local deviation and never
-- an input to it. This is the FIT-03 pattern applied to wellness: intervals.icu
-- derived values sit alongside parsed ones and are never substituted for them.
-- RECOV-04 says the deviation is computed "against the athlete's own 28 day
-- baseline, not against platform derived scores", and the only way to keep that
-- honest is for the platform's number to have nowhere to enter from.
alter table rollups add column platform_readiness numeric;

-- RECOV-06: the day's training load, copied onto the rollup so the missed
-- session cross check is one read.
alter table rollups add column day_load numeric;

comment on column rollups.recovery_deviation is
  'RECOV-04: mean standardised deviation of the athlete''s own measured signals '
  'against his own trailing 28 day baseline. Positive is better than baseline. '
  'Never derived from readiness, which is the platform''s opinion.';
comment on column rollups.platform_readiness is
  'Stored alongside, never an input to recovery_deviation (RECOV-04).';
