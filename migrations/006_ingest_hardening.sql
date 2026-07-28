-- Ingest hardening: the analysis window, and a delivery queue that is also the
-- replay guard.
--
-- Requirements touched: FIT-01, FIT-02, FIT-03, SEC-02, PERF-03.

-- FIT-03's derived half has a readiness signal we were ignoring.
-- `Activity.analyzed` is null until the platform finishes consolidating, and
-- ACTIVITY_UPLOADED fires before that. So the icu_ fields read at trigger time
-- are provisional. Recording when the platform finished, rather than assuming it
-- had, is what lets ACTIVITY_ANALYZED refresh them later without guessing which
-- rows need it.
alter table sessions add column analyzed_at timestamptz;
alter table sessions add column derived_provisional boolean not null default false;

-- The refresh sweep asks exactly one question: which rows are still carrying
-- provisional platform numbers.
create index sessions_derived_provisional on sessions (started_at desc)
  where derived_provisional;

-- FIT-02 and PERF-03 together. Two problems with the previous shape:
--
-- 1. The replay index was partial on `external_ref is not null`, and
--    external_ref comes from the activity object. A CALENDAR_UPDATED payload has
--    no activity, so every calendar delivery bypassed replay detection.
-- 2. A delivery was recorded as accepted before the work was attempted, so a
--    failed ingest could never be retried: the upstream's redelivery collided
--    with the record and was dropped as a replay.
--
-- Both are fixed by making the row a queue entry rather than a receipt.
-- `delivery_key` is a digest of the identifying fields, computed for every event
-- type rather than only the ones carrying an activity id, so the unique index no
-- longer needs to be partial.
alter table webhook_deliveries add column delivery_key text;
alter table webhook_deliveries add column status text not null default 'done'
  check (status in ('pending', 'running', 'done', 'failed', 'rejected'));
alter table webhook_deliveries add column payload jsonb;
alter table webhook_deliveries add column attempts int not null default 0;
alter table webhook_deliveries add column last_error text;
alter table webhook_deliveries add column processed_at timestamptz;

-- Existing rows predate the queue and are already handled; give them a key so
-- the not-null and the unique index below can both apply.
update webhook_deliveries
   set delivery_key = encode(
         sha256(
           (event_type || ':' || coalesce(external_ref, '') || ':' || event_timestamp::text)::bytea
         ),
         'hex'
       )
 where delivery_key is null;

alter table webhook_deliveries alter column delivery_key set not null;

-- The old index was partial on `external_ref is not null`, so rows without an
-- activity id — every calendar delivery — were never checked for uniqueness and
-- duplicates could accumulate. The new index is total, which means it will not
-- build over that history. Collapse it first, keeping the earliest of each set.
--
-- Safe to delete: these rows are a log of deliveries already handled, and the
-- duplicates are the same delivery recorded more than once rather than distinct
-- events. Everything they refer to is reachable from `sessions` or from the
-- reconcile.
delete from webhook_deliveries a
 using webhook_deliveries b
 where a.delivery_key = b.delivery_key
   and a.id > b.id;

drop index if exists webhook_deliveries_replay;
create unique index webhook_deliveries_replay on webhook_deliveries (delivery_key);

-- The worker claims oldest first, so a burst is drained in arrival order.
create index webhook_deliveries_pending on webhook_deliveries (received_at)
  where status in ('pending', 'running');
