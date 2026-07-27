-- P01: conversation persistence and model call accounting.
--
-- Requirements: CHAT-01, CHAT-08, MODEL-01, VOICE-02, OBS-01.

-- Every inbound and outbound message. CHAT-08 needs this to process an outage
-- backlog exactly once; consolidation reads a day's worth of it.
create table messages (
  id                  bigserial primary key,
  chat_id             bigint not null,
  -- Telegram's own id, unique per chat. The partial unique index below makes
  -- replaying an update after a restart a no-op rather than a duplicate.
  telegram_message_id bigint,
  role                text not null check (role in ('athlete','coach')),
  body                text not null,
  -- VOICE-02: consolidation weights a transcript differently from typed text.
  modality            text not null default 'text'
                      check (modality in ('text','voice','system')),
  occurred_at         timestamptz not null,
  processed_at        timestamptz,
  created_at          timestamptz not null default now()
);

create unique index messages_one_per_telegram_id
  on messages (chat_id, telegram_message_id)
  where telegram_message_id is not null;

create index messages_occurred_at on messages (occurred_at desc);
create index messages_unprocessed on messages (occurred_at)
  where processed_at is null and role = 'athlete';

-- MODEL-01: routing is recorded per call. OBS-01 and OBS-07 read this table;
-- P12 builds the daily cost query and the spend cap on top of it.
create table model_calls (
  id                 bigserial primary key,
  purpose            text not null
                     check (purpose in ('chat','consolidation','session_review',
                                        'transcription','recall_test')),
  model              text not null,
  routed_from        text,          -- set when MODEL-03 fell back
  input_tokens       int not null default 0,
  output_tokens      int not null default 0,
  cache_read_tokens  int not null default 0,
  cache_write_tokens int not null default 0,
  cost_usd           numeric(10,6) not null default 0,
  latency_ms         int,
  created_at         timestamptz not null default now()
);

create index model_calls_created_at on model_calls (created_at desc);
create index model_calls_purpose_day on model_calls (purpose, created_at);

-- CHAT-11: one interruption per conversation. A conversation is a run of
-- messages with no long gap; the budget is claimed at most once within it.
create table interruptions (
  id           bigserial primary key,
  kind         text not null
               check (kind in ('safety_confirmation','outlier_confirmation',
                               'body_mass_gap','pending_mention','verification')),
  ref          text,               -- fact key or reading id the interruption is about
  claimed_at   timestamptz not null default now(),
  delivered    boolean not null default false
);

create index interruptions_claimed_at on interruptions (claimed_at desc);
