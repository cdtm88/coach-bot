-- P08: publishing prescriptions upstream and detecting athlete edits.
-- PLAN-01 to PLAN-12.
--
-- Small, because most of what the phase needs already exists.
-- `prescriptions.calendar_event_id` has been there since P00 and unused; this is
-- the phase that fills it, and the comment below says what it now means.

-- PLAN-02: the stable coach id, as it was published. Derivable from the row id
-- and stored anyway, for one reason: PLAN-05 sweeps events whose prescription is
-- *gone*, and a deleted row cannot tell you what its external_id was. Without
-- this column the sweep would have to trust a parse of the upstream id back into
-- a row id, which is the same information with an extra chance to be wrong.
alter table prescriptions add column external_id text;

create unique index prescriptions_external_id on prescriptions (external_id)
  where external_id is not null;

comment on column prescriptions.external_id is
  'PLAN-02. The coach id carried in the upstream external_id field, written when '
  'the prescription is published. Prefixed so PLAN-05 can recognise its own '
  'events without oauth_client_id, which V1 found is null under a personal API '
  'key. Null means never published.';

comment on column prescriptions.calendar_event_id is
  'PLAN-01. The upstream intervals.icu event id returned by the bulk upsert. Not '
  'the key we publish on — external_id is (PLAN-02) — but stored because '
  'PLAN-07 pairs a completed activity to a plan through the upstream '
  'paired_event_id, which is an event id and has to resolve back to a row.';

-- PLAN-07: "matching uses the upstream pairing where available, falling back to
-- local date and discipline matching." This is the upstream pairing. Nullable
-- and expected to be null often: a ride uploaded with no planned workout on the
-- calendar has nothing to pair with, and the fallback is not a degraded path.
alter table sessions add column paired_event_id text;

create index sessions_paired_event_id on sessions (paired_event_id)
  where paired_event_id is not null;

comment on column sessions.paired_event_id is
  'PLAN-07. The platform''s own link from a completed activity to the planned '
  'event it satisfied. Joined to prescriptions.calendar_event_id. Null when the '
  'activity was not paired upstream, which is ordinary rather than an error.';

-- PLAN-06 and PLAN-12: what the athlete changed upstream, and what we did about
-- it. Not a new table: an athlete edit is an adjustment to a prescription, and
-- `adjustment_events` already records the shape — trigger, evidence, before and
-- after. The check constraint on that table is on nothing, so the trigger name
-- is the only thing that needs declaring, and it is declared here as a comment
-- rather than as an enum because ADJ owns that vocabulary and P08 is a guest.
comment on table adjustment_events is
  'ADJ-01 and PLAN-06. One row per change to a prescription, whatever caused it. '
  'trigger = ''athlete_edit'' is P08''s: the athlete moved or resized a planned '
  'event upstream and the local row was brought into line (PLAN-12). Counting '
  'those rows per weekday is how PLAN-06 turns repeated edits into observed '
  'availability evidence.';
