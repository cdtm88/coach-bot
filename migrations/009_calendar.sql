-- P06: calendar feeds.
--
-- Requirements: CALR-01 to CALR-06, and PLAN-08 by omission — there is no write
-- path to Google anywhere in this schema or the code above it.

-- CALR-06 is the reason this table looks the way it does. "Feed URLs are treated
-- as bearer secrets, stored in the environment and never logged." A secret URL
-- in a database column is a secret in the nightly pg_dump, in the MEM-12 backup
-- and in any query a future phase writes against this table, so **the URL is
-- never stored at all**.
--
-- What is stored is a fingerprint, which is enough to recognise a feed across
-- restarts and to notice one being replaced, and a display name taken from the
-- calendar's own X-WR-CALNAME. Neither can be turned back into a URL.
--
-- The **fingerprint is the identity and the name is display**, which is not the
-- obvious way round and is the way round that works. A feed that fails to fetch
-- has no X-WR-CALNAME to be named from, so it falls back to a positional label;
-- when it succeeds an hour later it is suddenly "Work". Keyed on the name, those
-- are two feeds and the second one collides on the fingerprint. Keyed on the
-- fingerprint, it is one feed that has learned its name.
create table calendar_feeds (
  id              text primary key,
  name            text not null,
  url_fingerprint text not null unique,
  -- Position in CALENDAR_ICS_URLS, so a feed whose name changes upstream is
  -- still recognisable as the same configured entry.
  position        int not null,
  last_fetch_at   timestamptz,
  last_success_at timestamptz,
  last_error      text,
  created_at      timestamptz not null default now()
);

-- CALR-02: "Fetch history shows no gap longer than 6 hours." History, not a last
-- known state — a single timestamp cannot show a gap, only a current age.
create table calendar_fetches (
  id          bigserial primary key,
  feed        text not null references calendar_feeds(id) on delete cascade,
  started_at  timestamptz not null default now(),
  ok          boolean not null,
  events      int not null default 0,
  -- Never a URL. CALR-06 applies to error text as much as to log lines, and an
  -- HTTP client's exception message carries the URL it was given by default.
  error       text
);

create index calendar_fetches_feed_time on calendar_fetches (feed, started_at desc);

-- CALR-01: busy time, one row per occurrence rather than one per event, because
-- a weekly commitment is 21 separate blocks across the CALR-02 horizon and a
-- scheduler needs them as blocks.
create table calendar_events (
  id            bigserial primary key,
  feed          text not null references calendar_feeds(id) on delete cascade,

  -- The ICS identity. `recurrence_id` distinguishes the instances of a repeating
  -- event; it is the empty string for a one-off so the unique index below does
  -- not have to be partial.
  uid           text not null,
  recurrence_id text not null default '',

  summary       text,
  starts_at     timestamptz not null,
  ends_at       timestamptz not null,
  -- TZ-01: the local day this occurrence belongs to.
  local_date    date not null,
  all_day       boolean not null default false,

  -- CALR-04's inputs, kept so a verdict can be explained rather than trusted.
  status        text,   -- CONFIRMED, TENTATIVE, CANCELLED
  participation text,   -- the athlete's own PARTSTAT: ACCEPTED, DECLINED, ...
  transparency  text,   -- OPAQUE blocks time, TRANSPARENT does not

  -- CALR-04's verdict: does this occurrence actually block scheduling?
  busy          boolean not null,

  fetched_at    timestamptz not null default now()
);

-- One row per occurrence per feed. Re-fetching rewrites rather than appends,
-- which is what makes the six hourly poll of CALR-02 free.
create unique index calendar_events_identity
  on calendar_events (feed, uid, recurrence_id);

create index calendar_events_window on calendar_events (starts_at, ends_at);
create index calendar_events_busy_day on calendar_events (local_date)
  where busy;
