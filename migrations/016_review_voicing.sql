-- The Sunday review gets a model call of its own, so `model_calls.purpose`
-- needs a value for it.
--
-- REV-01 posts the review into the chat and, until now, what it posted was the
-- assembly: six labelled sections in a fixed order, one per line, whether or
-- not any of them had anything to report. Every figure in it was right and the
-- whole thing read like a form, because it was one.
--
-- Voicing is a separate call rather than a wider brief for the chat purpose,
-- and the reason is the accounting rather than the routing. OBS-01 costs per
-- call and OBS-07 caps the day's spend; a weekly call at high effort that
-- landed in the same bucket as conversation would make the chat line jump every
-- Sunday for a reason nobody could see in the table. Its own purpose makes it
-- one row a week that says what it is.
--
-- No backfill: existing rows are all pre-existing purposes and stay valid.
alter table model_calls drop constraint model_calls_purpose_check;

alter table model_calls add constraint model_calls_purpose_check
  check (purpose in ('chat','consolidation','session_review',
                     'transcription','recall_test','review'));
