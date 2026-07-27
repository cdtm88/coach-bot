-- P02: the nightly consolidation pass.
--
-- Requirements: CONS-01 to CONS-10, SAFE-02, SAFE-03.

-- CONS-01: the job logs a run row with counts for each input.
-- CONS-08 and CONS-10 both key off the local date, so it is the primary key:
-- consolidation runs at most once per date, and re-running is an upsert.
create table consolidation_runs (
  consolidated_on  date primary key,
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,
  status           text not null default 'running'
                   check (status in ('running','succeeded','failed')),
  -- Input counts, per CONS-01.
  messages_in      int not null default 0,
  telemetry_in     int not null default 0,
  pending_in       int not null default 0,
  active_facts_in  int not null default 0,
  -- Outcome counts.
  diffs_proposed   int not null default 0,
  diffs_applied    int not null default 0,
  diffs_rejected   int not null default 0,
  -- OBS-08: at most one retry. attempt 2 that fails waits for the next night.
  attempts         int not null default 1 check (attempts <= 2),
  error            text
);

create index consolidation_runs_status on consolidation_runs (status, consolidated_on desc);
