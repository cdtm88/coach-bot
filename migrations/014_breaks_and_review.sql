-- P10. Breaks acquire teeth, and the review acquires somewhere to record that
-- it ran. Small, because most of P10 reads tables that already exist.

-- BREAK-02: a prescription inside a break is suspended rather than missed.
--
-- The distinction is the whole requirement. 'missed' is an adherence event and
-- feeds the ADJ-01 triggers; 'cancelled' is the athlete declining a session.
-- Neither is true of a session that was never going to happen because the
-- athlete was in Italy, and calling it either would penalise adherence for a
-- break the coach itself agreed to.
alter table prescriptions drop constraint if exists prescriptions_status_check;
alter table prescriptions add constraint prescriptions_status_check
  check (status in ('planned', 'adjusted', 'completed', 'missed', 'cancelled', 'suspended'));

comment on column prescriptions.status is
  'planned/adjusted are live; completed and missed are adherence outcomes; '
  'cancelled is the athlete declining; suspended is BREAK-02, excluded from '
  'adherence entirely rather than counted as a miss.';

-- BREAK-03: the re-entry proposal is made once per break, at the next review.
--
-- Recorded on the break rather than derived, because "has the athlete already
-- been offered a re-entry for this break" cannot be answered from the
-- prescriptions: a proposal the athlete ignored looks exactly like one that was
-- never made, and the review runs every week.
alter table breaks add column if not exists re_entry_proposed_on date;

comment on column breaks.re_entry_proposed_on is
  'BREAK-03: the local date the reduced re-entry was proposed. Null means it '
  'has not been; set means the review has said its piece and should not repeat.';

-- REV-05 stores the review as a note of kind ''review'', which 003 already
-- allows, and appends to the block document, which 010 already holds. Nothing
-- new is needed for either. What is needed is the ability to ask "which week
-- was this review about", so the note's occurred_on carries the week ending
-- date and this index makes the lookup exact rather than a scan.
create index if not exists notes_reviews_by_week
  on notes (occurred_on desc) where kind = 'review';
