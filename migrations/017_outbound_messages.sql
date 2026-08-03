-- Everything the coach says without being asked, written down like everything
-- else it says.
--
-- `telegram.bot.record_reply` had exactly one caller, inside `bot.drain`, so
-- only a reply to an athlete message was ever written to `messages`. The
-- scheduler's sender went straight to the transport with no database write at
-- all, which meant the morning message, the evening follow-up and the Sunday
-- review existed nowhere the system could read them. `runtime.turn._history`
-- selects from `messages`, so none of the three was ever in the model's
-- context: the coach could offer to move Thursday's session at 21:00 and have
-- no record of the offer when the athlete answered "yes, do that". This is the
-- same shape as the defect fixed in PR #35, where the coach could write the
-- plan and never read it.
--
-- `kind` labels what a message was. `period_key` is the claim on a period: one
-- morning message per local date, one follow-up per local date, one review per
-- Sunday. Both columns live on `messages` rather than in a delivery ledger of
-- their own, so that the conversation history and the record of what was sent
-- are the same row and cannot come to disagree about what the coach said.
--
-- The unique index is partial on `period_key` and not on `kind`, because not
-- every proactive message is once-per-period. ADJ-06's adjustment notice is
-- event-driven and already has its own idempotency in
-- `adjustment_events.announced`; it wants the label and the history row without
-- claiming a period.
--
-- `scheduled_runs` is not made redundant by this. That ledger decides whether a
-- job *runs*; this one decides whether a message is *sent*. They differ in
-- exactly the case that motivated the column: `scheduler.claim` re-claims a job
-- whose status is 'failed' while attempts remain, so a job that sent its
-- message and then failed afterwards would send it a second time. The claim
-- below is taken before the post, so the retry re-runs the job and says
-- nothing.

alter table messages add column kind text;
alter table messages add column period_key text;

create unique index messages_one_per_period
  on messages (kind, period_key)
  where period_key is not null;

create index messages_kind on messages (kind) where kind is not null;
