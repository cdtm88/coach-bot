-- P00: the memory subsystem.
--
-- Implements docs/memory-design.md section 4, which is binding on schema.
-- Requirements: MEM-01 to MEM-14.
--
-- Two deliberate departures from the design listing, both recorded in the
-- design document itself:
--   * prescriptions.session_id is added in P03 alongside the sessions table,
--     so this migration stands alone.
--   * prescriptions.block_id carries no foreign key until P07 creates blocks.

-- MEM-01: the controlled key vocabulary. A fact whose key is absent here is
-- rejected by the foreign key on facts, so the extraction pass cannot invent
-- a namespace.
create table fact_keys (
  key         text primary key,
  category    text not null
              check (category in ('constraint','profile','goal','availability',
                                  'prefs','equipment','physiology')),
  value_type  text not null
              check (value_type in ('text','number','boolean','date','list','object')),
  -- Half life in days for confidence decay (CONS-07), null for keys that never
  -- decay. Design section 5 sets the value per namespace.
  decay_days  int check (decay_days is null or decay_days > 0),
  safety      boolean not null default false,
  -- SAFE-03: safety keys never decay, so the two columns cannot disagree.
  constraint safety_keys_never_decay check (not safety or decay_days is null)
);

comment on column fact_keys.decay_days is
  'Half life, not a lifetime. Confidence follows floor + (1-floor) * 0.5 ^ (age/decay_days).';

create table facts (
  id                bigserial primary key,
  key               text not null references fact_keys(key),
  value             jsonb not null,
  -- MEM-04
  provenance        text not null
                    check (provenance in ('stated','observed','computed','inferred')),
  -- MEM-05
  confidence        numeric(3,2) not null default 1.00
                    check (confidence >= 0 and confidence <= 1),
  status            text not null default 'active'
                    check (status in ('active','superseded','rejected')),
  valid_from        timestamptz not null default now(),
  valid_to          timestamptz,
  superseded_by     bigint references facts(id),
  source_ref        text,
  last_confirmed_at timestamptz not null default now(),
  mention_pending   boolean not null default false,
  mention_expires   timestamptz,
  created_at        timestamptz not null default now()
);

-- MEM-02: contradictory active state is impossible at the database level rather
-- than a judgement call for the model.
create unique index facts_one_active_per_key
  on facts (key) where status = 'active';

create index facts_key_valid_from on facts (key, valid_from desc);

-- MEM-06: every change to a fact leaves an audit row.
create table fact_events (
  id          bigserial primary key,
  fact_id     bigint not null references facts(id),
  action      text not null
              check (action in ('created','superseded','rejected','confirmed','decayed')),
  reason      text not null,   -- model supplied rationale or rule name
  actor       text not null
              check (actor in ('consolidation','in_turn','rule','athlete')),
  evidence    jsonb,
  created_at  timestamptz not null default now()
);

create index fact_events_fact_id on fact_events (fact_id, created_at);

-- MEM-07: episodic notes, retrieved on demand only (MEM-10).
create table notes (
  id          bigserial primary key,
  kind        text not null
              check (kind in ('day_summary','observation','review','block_archive')),
  body        text not null,
  occurred_on date not null,
  refs        jsonb,
  tsv         tsvector generated always as (to_tsvector('english', body)) stored,
  created_at  timestamptz not null default now()
);
create index notes_tsv_idx on notes using gin(tsv);
create index notes_occurred_on on notes (occurred_on desc);

-- CONS-09: one day_summary per qualifying date, enforced rather than trusted.
create unique index notes_one_day_summary_per_date
  on notes (occurred_on) where kind = 'day_summary';

-- MEM-08: derived rollups are computed in SQL and always loaded. The table and
-- its job exist from P00 so the shape is fixed and the model never does
-- arithmetic; individual metrics populate as their feeds land in P03 to P05.
create table rollups (
  as_of              date primary key,
  load_7d            numeric,
  load_28d           numeric,
  weight_trend_slope numeric,   -- kg per week, fitted per HLTH-06
  weight_reading_n   int,       -- drives the HLTH claim thresholds
  adherence_rate     numeric,
  recovery_deviation numeric,   -- against the athlete's own 28 day baseline
  gym_session_count  int,
  gym_mean_rpe       numeric,
  computed_at        timestamptz not null default now()
);

-- Prescriptions and adjustments.
create table prescriptions (
  id            bigserial primary key,
  block_id      bigint not null,      -- FK added in P07 with the blocks table
  planned_for   timestamptz not null,
  discipline    text not null,
  spec          jsonb not null,       -- duration, intensity, route, purpose
  status        text not null default 'planned'
                check (status in ('planned','adjusted','completed','missed','cancelled')),
  calendar_event_id text,
  -- session_id added in P03:
  --   alter table prescriptions add column session_id bigint references sessions(id);
  created_at    timestamptz not null default now()
);

create index prescriptions_planned_for on prescriptions (planned_for);

create table adjustment_events (
  id              bigserial primary key,
  prescription_id bigint not null references prescriptions(id),
  trigger         text not null,     -- rule name from the trigger table
  evidence        jsonb not null,    -- session id, recovery metrics, deltas
  before_spec     jsonb not null,
  after_spec      jsonb not null,
  announced       boolean not null default false,
  created_at      timestamptz not null default now()
);

-- MEM-09: working memory. One row, rewritten per turn. Consolidation clears
-- today_uncommitted and regenerates the continuity fields; it never empties
-- the row, because CHAT-05 opens from open_threads.
create table conversation_state (
  id                boolean primary key default true check (id),
  rolling_summary   text,
  open_threads      jsonb,
  today_uncommitted jsonb,
  last_topic        text,
  updated_at        timestamptz not null default now()
);

insert into conversation_state (id) values (true);

-- CONS-06: the only route from a chat turn into long term memory, ratified by
-- consolidation. The one exception is the SAFE-06 athlete safety path, which
-- writes facts directly and never lands here.
create table pending_writes (
  id          bigserial primary key,
  proposal    jsonb not null,
  origin      text not null
              check (origin in ('in_turn','consolidation','feed')),
  status      text not null default 'pending'
              check (status in ('pending','ratified','rejected','expired')),
  created_at  timestamptz not null default now()
);

create index pending_writes_status on pending_writes (status, created_at);

-- OBS-05: staleness is tracked for the five inbound feeds that carry a
-- threshold. Outbound writes and the conversation are not feeds here.
create table feeds (
  name              text primary key,
  last_success_at   timestamptz,
  stale_after_hours int not null,
  last_error        text
);

-- OBS-02: the recall regression suite.
create table recall_tests (
  id        bigserial primary key,
  question  text not null,
  expect    text not null,
  matcher   text not null default 'contains'
            check (matcher in ('contains','regex','model')),
  last_run  timestamptz,
  last_pass boolean
);
