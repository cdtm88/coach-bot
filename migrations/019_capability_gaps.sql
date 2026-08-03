-- TRUST-05: the way out, so "never invent a number" is actionable.
--
-- `docs/prior-art.md` section 1 records the reasoning, and it is the least
-- obvious part of the whole trust model: a bare prohibition with no alternative
-- is what pushes a model into inventing something. `pacer-ai` gave its model a
-- callable `log_capability_gap` that records the gap and returns a fixed
-- user-safe sentence rather than a figure, and the difference between that and
-- a stern prompt line was measurable.
--
-- What is recorded here is the internal side: what was asked for, and what the
-- coach could not answer from. It is a backlog of the questions the athlete
-- actually asks that the system cannot yet answer, which is a more honest
-- source of requirements than guessing.
--
-- The discipline `pacer-ai` names and this keeps: the internal reason goes to
-- the database only and never into the reply. A message explaining which
-- methodology was unavailable is a message about the system rather than about
-- his training.

create table capability_gaps (
  id          bigserial primary key,
  -- What he wanted to know, in the model's words.
  asked_for   text not null,
  -- Why it could not be answered. Internal only.
  reason      text not null,
  -- The exchange it happened in, so `coach-transcript --turn` reads back the
  -- whole conversation around it rather than this row alone.
  turn_id     uuid,
  created_at  timestamptz not null default now()
);

create index capability_gaps_created_at on capability_gaps (created_at desc);
