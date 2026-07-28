-- P07: training blocks, gym programming, and the constraint gate.
--
-- Requirements: BLOCK-01 to BLOCK-08, GYM-01 to GYM-08, SAFE-04.
--
-- This is the first phase that writes rather than reads, so it is the first
-- phase where the governing asymmetry has teeth: the system may reduce load
-- autonomously and may never increase it autonomously. Everything below is
-- shaped to make an unsafe prescription impossible to store rather than
-- unlikely to be generated.

-- BLOCK-01: a training block is a versioned markdown document with goals,
-- constraints and a week by week plan.
create table blocks (
  id           bigserial primary key,
  title        text not null,

  -- BLOCK-05: the block's goals, which must carry a fitness preservation
  -- constraint alongside the weight goal. Enforced in code at generation, not
  -- here, because "carries a fitness preservation goal" is a statement about
  -- content rather than about shape.
  goals        jsonb not null default '{}'::jsonb,

  -- BLOCK-03: four weeks. Stored rather than assumed so a future block of a
  -- different length is a data change and not a code change.
  starts_on    date not null,
  weeks        int not null default 4 check (weeks > 0),

  status       text not null default 'draft'
               check (status in ('draft', 'active', 'completed', 'abandoned')),
  created_at   timestamptz not null default now()
);

create index blocks_starts_on on blocks (starts_on desc);
create unique index blocks_one_active on blocks (status) where status = 'active';

-- BLOCK-01: "Retrieving a block returns current content and full version
-- history." BLOCK-02: "the agent rewrites the block rather than regenerating it
-- from scratch", and diffs between versions are localised.
--
-- Storing every version rather than the current text plus a changelog is what
-- makes BLOCK-02 checkable. A reviewer can diff two rows; a changelog is a
-- claim about a diff that nobody can verify.
create table block_versions (
  id          bigserial primary key,
  block_id    bigint not null references blocks(id) on delete cascade,
  version     int not null,
  content     text not null,
  -- Why this rewrite happened, in one line. Read back when the athlete asks why
  -- the block changed.
  reason      text not null,
  author      text not null default 'coach' check (author in ('coach', 'athlete')),
  created_at  timestamptz not null default now()
);

create unique index block_versions_one_per_version on block_versions (block_id, version);
create index block_versions_latest on block_versions (block_id, version desc);

-- P00 deferred this: `prescriptions.block_id` was a bare integer because
-- `blocks` did not exist. This is its phase.
--
-- Deleting a block deletes its prescriptions, which is correct — a prescription
-- without a block is an orphan that PLAN-05 would sweep upstream anyway.
alter table prescriptions
  add constraint prescriptions_block_id_fkey
  foreign key (block_id) references blocks(id) on delete cascade;

-- GYM-08 and BLOCK-07. The load a prescription is planned to cost, on the one
-- scale that covers both disciplines. Stored on the row rather than recomputed,
-- because BLOCK-07's ceiling is checked against the *planned* figure and a
-- coefficient change must not retroactively rewrite what a past week was
-- allowed to be.
alter table prescriptions add column planned_load numeric;

comment on column prescriptions.planned_load is
  'GYM-08 units. Cycling: intensity factor squared times duration hours times 100. '
  'Gym: RPE times duration minutes times the configured coefficient. One ceiling '
  'covers both (GYM-05, BLOCK-07, ADJ-02).';

-- GYM-03: "An exercise library supports substitution so a blocked or
-- unavailable movement is swapped rather than dropped."
--
-- `movement_pattern` is the column GYM-02 turns on. A constraint excludes a
-- *pattern*, not a name — "no loaded hip hinge" has to block an exercise nobody
-- thought to list — so the pattern is the unit of exclusion and the name is
-- only how a human recognises the row.
create table exercises (
  id               bigserial primary key,
  name             text not null unique,
  movement_pattern text not null,

  -- What it needs. GYM-03 substitutes on this as well as on constraints: the
  -- athlete trains in a small apartment gym, so "unavailable" is as common a
  -- reason to swap as "excluded".
  equipment        text not null default 'bodyweight',

  -- Other names the same movement goes by, so a constraint written in the
  -- athlete's words matches the row. Lower case, matched as substrings.
  aliases          text[] not null default '{}',

  -- Roughly how demanding the movement is on the spine under load. Not used to
  -- exclude anything — constraints do that — but it is what orders the
  -- substitution candidates so a swap lands on something gentler rather than on
  -- whatever happened to be next in the table.
  spinal_load      int not null default 1 check (spinal_load between 0 and 3),

  created_at       timestamptz not null default now()
);

create index exercises_pattern on exercises (movement_pattern);

-- SAFE-04 and GYM-02: "A prescription containing an excluded pattern is blocked
-- before publish and logged."
--
-- Logged *here*, as a row, rather than to a log file. A blocked prescription is
-- evidence about the programme — it means the generator wanted something the
-- athlete cannot do — and the Sunday review should be able to read it back.
create table constraint_blocks (
  id            bigserial primary key,
  block_id      bigint references blocks(id) on delete cascade,
  discipline    text not null,
  planned_for   date,
  -- What was going to be prescribed.
  movement      text not null,
  pattern       text,
  -- Which constraint stopped it, verbatim from the fact.
  constraint_text text not null,
  -- What was prescribed instead, if anything (GYM-03).
  substituted_with text,
  created_at    timestamptz not null default now()
);

create index constraint_blocks_block on constraint_blocks (block_id, created_at desc);
