-- OBS-10 to OBS-14: what the model was actually sent, and what it actually said.
--
-- `model_calls` has recorded the shape of every call since P01 — purpose, model,
-- tokens, cost, latency — and none of its content. That is the right table for
-- OBS-01 and the wrong one for answering "why did the coach say that". Reading a
-- turn back was impossible: the system prompt is assembled per turn from facts
-- that change nightly, so it cannot be reconstructed after the fact, and the
-- tool results that shaped a reply were never anywhere at all.
--
-- **A second table rather than more columns, for three reasons.** `model_calls`
-- is on the hot path — `runtime.models.spent_today` sums it before every turn —
-- and widening a row that is scanned by a daily aggregate to carry tens of
-- kilobytes of JSON would make the cheapest query in the system the most
-- expensive. Payloads are also the first thing anyone prunes, and pruning a
-- column means rewriting the cost row rather than deleting a different one.
-- And a payload write that fails must not take the cost row with it: OBS-01 and
-- OBS-07 are accounting requirements, and a call that happened must be billed
-- whether or not we managed to keep a copy of it.
--
-- So the foreign key runs payload -> call and the payload is optional. A
-- `model_calls` row with no payload is a normal outcome and not a broken
-- invariant; `on delete cascade` means pruning a call, if that ever happens,
-- cannot leave an orphan.
--
-- `turn_id` is what makes this readable. One athlete message can produce three
-- `model_calls` rows — the first asks for a tool, the second asks for another,
-- the third answers — and before this they were three unrelated rows with
-- adjacent timestamps and nothing joining them. A turn that reads back as one
-- exchange is the difference between logging calls and being able to audit a
-- conversation. Nullable, because the scheduler's own calls (consolidation, the
-- Sunday voicing) are not turns and should not have one invented for them.

alter table model_calls add column turn_id uuid;

create index model_calls_turn on model_calls (turn_id) where turn_id is not null;

create table model_call_payloads (
  call_id     bigint primary key references model_calls (id) on delete cascade,
  -- Exactly as sent: the system blocks including their cache_control markers,
  -- the full message array including tool results, and the tool schemas that
  -- were on offer. A prompt reconstructed later is not evidence of anything.
  system      jsonb not null,
  messages    jsonb not null,
  tools       jsonb,
  -- Text, stop reason and any tool uses. The reply is what the athlete saw;
  -- the tool uses are what the trust scanner will later check it against.
  response    jsonb not null,
  created_at  timestamptz not null default now()
);

-- OBS-14 prunes on age, so the sweep reads this rather than joining back to
-- `model_calls.created_at` for a date it already has.
create index model_call_payloads_created_at on model_call_payloads (created_at);
