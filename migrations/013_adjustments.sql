-- P09: bounded mid-week adjustment authority. ADJ-01 to ADJ-08.
--
-- `adjustment_events` has existed since P00 and P08 already writes to it. This
-- adds the one thing P09 needs from it and the one table it needs of its own.

-- ADJ-05: "at most one autonomous restructure per week, and never the same
-- prescription twice." That has to be countable, and counting it by trigger name
-- would mean keeping a list in code of which names are autonomous — a list that
-- drifts the first time someone adds a rule. The authority is a property of the
-- change, so it goes on the row.
--
-- 'automatic' is P09's alone. The others are what already writes here: P08's
-- PLAN-04 placement and the athlete's own upstream edits, plus 'review' for what
-- P10's Sunday review will apply once it exists.
alter table adjustment_events add column authority text not null default 'automatic'
  check (authority in ('automatic', 'athlete', 'calendar', 'review'));

comment on column adjustment_events.authority is
  'ADJ-05. Who made this change. Only ''automatic'' counts against the one '
  'autonomous restructure per week — an athlete edit (PLAN-12) or a calendar '
  'placement (PLAN-04) is not the coach spending its authority.';

-- The default is 'automatic' because P09 is the overwhelming writer and a
-- default of anything else would make its inserts wordier for no reason. The two
-- existing writers are backfilled and then say so explicitly in code.
update adjustment_events set authority = 'calendar' where trigger = 'calendar_conflict';
update adjustment_events set authority = 'athlete' where trigger = 'athlete_edit';

create index adjustment_events_automatic_week on adjustment_events (authority, created_at)
  where authority = 'automatic';

-- Compliance, stored at the moment it is computed rather than recomputed on
-- demand. `review.attach` has always calculated it and returned it without
-- keeping it, which was fine while the only reader was the review being written
-- in the same call.
--
-- P09 cannot work that way, for a reason that is not about convenience: an
-- automatic `ease` **rewrites the target spec**. Recomputing compliance after an
-- adjustment would compare what the athlete rode against the reduced target
-- instead of what was actually asked of them, so the figure would improve every
-- time the coach downgraded something. ADJ-07 wants the stored reason rather than
-- a reconstruction, and this is the same principle one level down: what was
-- prescribed at the time is a fact about the past.
--
-- It also gives ADJ-03's overperformance rule a history to count, which it cannot
-- have if the number only exists during one function call.
alter table prescriptions add column compliance jsonb;

comment on column prescriptions.compliance is
  'FIT-07 and ADJ-01. Duration and intensity deltas against what was prescribed, '
  'frozen when the session was matched. Not recomputed: an ADJ-02 downgrade '
  'rewrites the spec, and a compliance figure that improved whenever the coach '
  'eased a session would be measuring the wrong thing.';

-- ADJ-03 and ADJ-05: what could not happen now and belongs to the Sunday review.
-- REV-04: "Proposed upgrades and deferred adjustments are surfaced here for a
-- decision. Deferred items from ADJ-03 and ADJ-05 appear."
--
-- Not `pending_writes`: that queue is for facts awaiting consolidation's conflict
-- matrix (CONS-06), and a training proposal is neither a fact nor something the
-- matrix knows how to resolve. Sharing the table would mean consolidation had to
-- learn to skip rows it cannot handle.
create table deferred_adjustments (
  id              bigserial primary key,
  prescription_id bigint references prescriptions(id) on delete cascade,
  session_id      bigint references sessions(id) on delete set null,

  -- The rule that fired, from coach.adjust.triggers. Text rather than an enum
  -- because the rule set is code's to define and a migration per new rule would
  -- be friction with no safety in it.
  trigger         text not null,

  -- Why it was deferred rather than applied: which requirement held it back.
  -- ADJ-03 (an upgrade), ADJ-05 (the week's restructure is spent), ADJ-04
  -- (beyond this week), ADJ-08 (the signal was not there).
  deferred_by     text not null,

  -- What the trigger wanted to do, and what it saw. Same shape as
  -- adjustment_events so the review can render both the same way.
  proposal        jsonb not null,
  evidence        jsonb not null,

  status          text not null default 'pending'
                  check (status in ('pending', 'accepted', 'declined', 'expired')),

  -- The local week this belongs to. The review reads a week at a time, and a
  -- proposal about a week that has passed is history rather than a decision.
  for_week        date not null,

  created_at      timestamptz not null default now(),
  resolved_at     timestamptz
);

create index deferred_adjustments_pending on deferred_adjustments (for_week, status)
  where status = 'pending';

-- ADJ-05's second clause: "never the same prescription twice". One pending
-- deferral per prescription per trigger is enough — a rule that fires twice for
-- the same session is the same information arriving again, and the review should
-- see it once.
create unique index deferred_adjustments_one_per_prescription
  on deferred_adjustments (prescription_id, trigger)
  where status = 'pending';

comment on table deferred_adjustments is
  'ADJ-03, ADJ-05 and REV-04. Adjustments the trigger rules proposed and the '
  'authority bounds would not let happen autonomously. The Sunday review reads '
  'these and the athlete decides. Nothing here has changed a prescription.';
