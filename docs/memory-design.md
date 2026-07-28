# AI Cycling Coach: Memory Subsystem Design

> Binding on implementation. Read with `docs/prd.md` (scope and acceptance) and `docs/setup.md` (credentials and infrastructure).
> On conflict: this document wins on schema and memory semantics.


A short term to long term memory architecture that self corrects from telemetry, self updates on a nightly consolidation cycle, and asks the athlete for almost nothing.

Design document. Precedes all ingest phases in the existing PRD. Written for Claude Code to implement against.

## 1. Design principles

1. **Load, do not retrieve.** Single user, a few hundred standing facts. Everything that matters fits in the prompt. Retrieval applies only to the episodic archive.
2. **Behaviour outranks statements.** Most memory updates should originate from telemetry, not from the athlete typing. This is the primary mechanism for minimal directed input.
3. **Supersede, never update.** Every change writes a new row and closes the old one. History is the audit trail.
4. **The model proposes, code decides.** Conflict resolution is deterministic. The LLM emits candidate diffs; SQL and application logic decide what lands.
5. **Consolidation beats in-turn capture.** A nightly pass that reads the whole day is more reliable than a model deciding mid sentence whether something is worth keeping.
6. **Safety facts are not probabilistic.** Injury and medical constraints load verbatim every turn, never decay, and can only be changed by an explicit statement from the athlete.
7. **Memory is invisible when it works.** No narration of writes, no confirmation rituals, no admin surface.
8. **Trends, not readings.** Body mass is reasoned on as a time weighted trend, never as a single value or a difference between two readings. A single weigh in carries close to a kilo of noise, so a coach that reacts to one gives bad guidance and loses trust. Gaps widen the uncertainty; they are never treated as a lapse. This is a memory failure as much as a coaching one: the fact worth storing is the slope, not the number.

## 2. Architecture

### Tier 1: working memory (volatile)

The live conversation plus a single row of session state maintained by the fast model on every turn: rolling summary, open threads, what was said today that has not been consolidated, current topic. Nothing here is treated as true.

The nightly job clears `today_uncommitted` and regenerates `rolling_summary`, `open_threads` and `last_topic` from the day it has just consolidated. It does not empty the row: the continuity note is assembled from those fields, and CHAT-05 requires the coach to open from the last open thread rather than cold.

### Tier 2: consolidation (nightly, heavier model)

Runs at 03:00. Reads the day's messages, the day's telemetry deltas, the pending queue and the current active fact set, and emits structured diffs. This is the only writer to long term memory. Everything durable passes through here.

### Tier 3: long term store (durable, typed)

Five stores, each with a different loading strategy:

* **Facts.** Typed key and value rows with provenance, confidence and supersession. Always loaded.
* **Derived rollups.** Computed by SQL nightly: 7 and 28 day load, weight trend slope, adherence rate, recovery deviation. Always loaded. The model never does arithmetic over raw rows.
* **Block document.** One markdown doc, versioned. Current week always loaded.
* **Episodic notes.** Day summaries, coach observations, archived reviews. Postgres full text search, loaded on demand only.
* **Event tables.** Sessions, health samples, recovery, calendar. Reached through tools, never preloaded.

**Why not a memory framework.** mem0, Zep and Letta abstract away exactly the things this design depends on: controlled key namespaces, provenance typing, deterministic supersession and a partial unique index guaranteeing one active row per key. You would spend more time defeating the abstraction than writing the schema.

## 3. Context assembly per turn

What is in the prompt on an ordinary message

| Component | Loading | Approx tokens | Notes |
| --- | --- | --- | --- |
| Constraints | Always, verbatim | 300 | Top of system prompt. Never summarised. |
| Profile and goals | Always | 250 | Active rows only |
| Availability and prefs | Always | 350 | Confidence shown to the model |
| Derived rollups | Always | 400 | Precomputed, never raw rows |
| Block document | Current week plus block goals | 700 | Full block on request |
| Continuity note | Always | 200 | Last topic and open threads |
| Pending mention | When one is queued | 60 | Max one per conversation |
| Feed staleness | When a feed is stale | 40 | Blocks false inference from absence |
| Episodic recall | On demand, FTS | 0 to 1500 | Tool call, not preload |

Steady state is roughly 2.3k to 3.8k tokens of context before conversation history. Comfortably affordable at daily use.

## 4. Data model

This section is authoritative for the memory subsystem: facts, audit, episodic notes, prescriptions, adjustments, working memory, feeds and verification. It is not the schema for the whole system. The ingest tables — sessions, blocks and their versions, derived rollups, messages, macros, wellness and calendar events — are defined at the phase that introduces them, and the PRD's governance table says so.

One consequence: `prescriptions.session_id` below references `sessions`, which P00 does not create. The column and its foreign key are added in P03 alongside the sessions table, so the P00 migration stands alone.

### Facts

```
create table fact_keys (
  key         text primary key,
  category    text not null,
  value_type  text not null,
  decay_days  int,
  safety      boolean not null default false
);

create table facts (
  id                bigserial primary key,
  key               text not null references fact_keys(key),
  value             jsonb not null,
  provenance        text not null
                    check (provenance in ('stated','observed','computed','inferred')),
  confidence        numeric(3,2) not null default 1.00,
  status            text not null default 'active'
                    check (status in ('active','superseded','rejected')),
  valid_from        timestamptz not null default now(),
  valid_to          timestamptz,
  superseded_by     bigint references facts(id),
  source_ref        text,
  last_confirmed_at timestamptz not null default now(),
  mention_pending   boolean not null default false,
  mention_expires   timestamptz,
  created_at        timestamptz not null default now()
);

create unique index facts_one_active_per_key
  on facts (key) where status = 'active';
```

The partial unique index makes contradictory active state impossible at the database level rather than a judgement call for the model. Unknown keys are rejected by the foreign key, so the extraction pass cannot invent a namespace.

### Audit

```
create table fact_events (
  id          bigserial primary key,
  fact_id     bigint not null references facts(id),
  action      text not null,   -- created | superseded | rejected | confirmed | decayed
  reason      text not null,   -- model supplied rationale or rule name
  actor       text not null,   -- consolidation | in_turn | rule | athlete
  evidence    jsonb,
  created_at  timestamptz not null default now()
);
```

### Episodic notes

```
create table notes (
  id          bigserial primary key,
  kind        text not null
              check (kind in ('day_summary','observation','review','block_archive')),
  body        text not null,
  occurred_on date not null,
  refs        jsonb,
  tsv         tsvector generated always as (to_tsvector('english', body)) stored,
  created_at  timestamptz not null default now()
);
create index notes_tsv_idx on notes using gin(tsv);
```

### Prescriptions and adjustments

```
create table prescriptions (
  id            bigserial primary key,
  block_id      bigint not null,
  planned_for   timestamptz not null,
  discipline    text not null,
  spec          jsonb not null,       -- duration, intensity, route, purpose
  status        text not null default 'planned'
                check (status in ('planned','adjusted','completed','missed','cancelled')),
  calendar_event_id text,
  -- session_id added in P03 with the sessions table:
  --   alter table prescriptions add column session_id bigint references sessions(id);
  created_at    timestamptz not null default now()
);

create table adjustment_events (
  id              bigserial primary key,
  prescription_id bigint not null references prescriptions(id),
  trigger         text not null,     -- rule name from the trigger table
  evidence        jsonb not null,    -- session id, whoop metrics, deltas
  before_spec     jsonb not null,
  after_spec      jsonb not null,
  announced       boolean not null default false,
  created_at      timestamptz not null default now()
);
```

### Working memory and queue

```
create table conversation_state (
  id                boolean primary key default true check (id),
  rolling_summary   text,
  open_threads      jsonb,
  today_uncommitted jsonb,
  last_topic        text,
  updated_at        timestamptz not null default now()
);

create table pending_writes (
  id          bigserial primary key,
  proposal    jsonb not null,
  origin      text not null,      -- in_turn | consolidation | feed
  status      text not null default 'pending',
  created_at  timestamptz not null default now()
);
```

### Feeds and verification

```
create table feeds (
  name              text primary key,
  last_success_at   timestamptz,
  stale_after_hours int not null,
  last_error        text
);

create table recall_tests (
  id        bigserial primary key,
  question  text not null,
  expect    text not null,
  matcher   text not null default 'contains',  -- contains | regex | model
  last_run  timestamptz,
  last_pass boolean
);
```

## 5. Key namespace

Controlled vocabulary. Anything outside it is rejected at write time.

The decay column is `decay_days` in the DDL and it is a **half life**, not a lifetime. Confidence follows `floor + (1 - floor) * 0.5 ^ (days_since_confirmation / decay_days)` with the floor at **0.20**, so it halves toward the floor and never reaches zero. An availability fact unconfirmed for 90 days sits at 0.30; at a year it is approaching 0.20 and is still active. Facts do not expire. PRD CONS-07 states the same curve.

| Namespace | Examples | Typical provenance | Half life |
| --- | --- | --- | --- |
| constraint.\* | injury limits, movement restrictions, medical flags | stated only | Never decays |
| profile.\* | height, sport, training age | stated | 365 days |
| goal.\* | target weight, Alpe target, milestone dates | stated | 90 days |
| availability.\* | weekday minutes, training days, blackouts | stated then observed | 30 days |
| prefs.\* | session types disliked, notification timing, coach tone | stated, inferred | 120 days |
| equipment.\* | trainer, bike, gym access type | stated | 180 days |
| physiology.\* | FTP, max HR, threshold HR | computed, inferred | 42 days |

## 6. Write path

### In turn (cheap, provisional)

Only fires on an explicit instruction or a direct correction. Writes to `pending_writes`, never to `facts`. Not narrated to the athlete. A same day correction supersedes an earlier pending row rather than queuing twice.

### The athlete safety path (the one exception)

An explicit athlete statement of a constraint writes the safety fact **directly**, bypassing `pending_writes` and consolidation, with provenance `stated` and actor `athlete`. The coach restates the constraint and the athlete confirms before it lands.

This exists because the rest of the design would otherwise make safety facts write-once: consolidation is the only ratifier and consolidation is forbidden from touching safety keys, so an injury reported after the initial seed could never be recorded. It is the only direct writer outside consolidation, it can write nothing but safety keys, and it is the only place the system asks for confirmation. PRD SAFE-06 governs.

### Nightly consolidation (authoritative)

1. Gather the day's messages, telemetry deltas, pending writes, active facts.
2. Heavier model emits candidate diffs as strict JSON: key, new value, provenance, reason, evidence refs.
3. Validate against `fact_keys`. Unknown keys, wrong value types and any write to a `safety` key are rejected and logged. Safety keys change only through the athlete path above.
4. Apply the conflict matrix in code. The model's opinion on precedence is discarded.
5. Write supersessions and `fact_events` rows.
6. Recompute derived rollups.
7. Write the day summary note; embed nothing, index for FTS.
8. Set `mention_pending` on any fact where observed data contradicted a stated one.
9. Decay confidence on unconfirmed facts by the category half life of section 5, asymptotic to the 0.20 floor.
10. Run the recall regression suite and the contradiction linter. Alert only on failure.

## 7. Conflict resolution matrix

Deterministic. Implemented in code, not prompted.

| Situation | Resolution | Athlete involvement |
| --- | --- | --- |
| Same key, same provenance | Most recent wins | None |
| Stated vs observed, behavioural key | Observed wins | Mentioned once in passing |
| Stated vs observed, intent key (goals) | Stated wins | None |
| Inferred vs measured | Measured wins, silently | None |
| Any change to a safety key | Rejected. Only the athlete safety path of section 6 can write one | Explicit statement, then confirmation |
| Ambiguous or low evidence | Held in pending, resolved conversationally or aged out at 14 days | Possible aside |

## 8. Self correction behaviours

### Behaviour overrides statements

The largest source of updates and it needs nothing from the athlete. Stated availability of six training days against three weeks of four session data updates `availability.days` to observed. The block adapts on the next Sunday cycle.

### The mention once rule

When an observed fact supersedes a stated one, consolidation sets `mention_pending` with a 72 hour expiry. Behaviour:

* At most one pending mention is injected into any single conversation.
* It must be delivered as an aside inside a message the coach was sending anyway. Never a standalone message, never a question, never a notification.
* Phrased as an assertion that invites correction, for example noting that weeknights are being treated as 45 minutes now, rather than asking whether they are.
* Marked delivered once used. If no natural opening occurs within 72 hours it expires silently; the fact change stands regardless.

### Implicit correction detection

Consolidation looks for negation of a coach assertion, statements inconsistent with an active fact, and behavioural evidence contradicting a stated fact. Each yields a diff carrying a reason string, not a silent overwrite.

### Decay with contextual reconfirmation

Past its half life a fact loses confidence rather than expiring, on the curve in section 5. Facts below 0.50 enter the prompt flagged as worth verifying naturally, and the coach folds one into a relevant exchange. There is never an audit list.

### One interruption per conversation

The mention once rule, the verification candidate, the outlier confirmation and the body mass gap mention are four separate one-per-conversation allowances, and left alone they compose into four interruptions in a single conversation while each reports compliance. They share a single budget instead, with a priority order set by PRD CHAT-11. Feed staleness shapes what the coach reasons from and is never itself an interruption.

## 9. Naturalness rules for the agent

* Reference memory only where it changes what is said. Recall for its own sake reads as surveillance.
* Never narrate a memory operation.
* One question per message, maximum.
* Open from the continuity note, not cold.
* Voice notes are transcribed and treated as ordinary messages, tagged with modality so consolidation can weight them.
* Persona lives in a versioned system prompt file, seeded from the July 2026 coaching conversation.

## 10. Feeds

> Rationale, not specification. This section explains why the feed, planning and adjustment rules are shaped as they are. The rules themselves are the FIT, HLTH, RECOV, CALR, PLAN, ADJ and LOG requirements in the PRD, which governs on scope. Where this section and the PRD diverge, the PRD wins and this section gets fixed.

All authentication is by API key or secret URL. No OAuth anywhere, and no third party health export app.

| Feed | Mechanism | Cadence | Carries | Stale after |
| --- | --- | --- | --- | --- |
| Activities (in) | intervals.icu webhook on upload, API key basic auth, original file pulled | On upload, 6h reconcile | Zwift and any device connected upstream | 7d |
| FIT archive (in) | Local watched folder, first class path and the only copy upstream cannot delete | On file arrival | Every ride, retained permanently | 7d |
| Wellness (in) | intervals.icu wellness endpoint, fed by the Whoop link | Hourly | Sleep, resting HR, HRV, recovery, respiration, SpO2, plus the platform's CTL/ATL load curves. No activities. | 48h |
| Body mass (in) | MacroLog HealthBridge writes to intervals.icu wellness; the coach reads it back | On app launch | 2 to 3 readings per week, read as a time weighted trend over 28 days | 12d |

**Verified 28 July 2026: the wellness feed carries no `weight` at all**, on any of the 22 days read. So the body mass row above describes a pipeline whose upstream half does not exist yet — HealthBridge is required rather than optional, and it is outside this repository. The read side is built and idle. Note also that the body mass feed is stale 12 days after a *reading*, not after a successful fetch: one call serves two feeds and a healthy wellness endpoint returning no weight must not reset the weight clock. HLTH-15 additionally keeps body mass out of the generic staleness block entirely, so the coach's one weigh in mention has a single owner.
| Macros (in) | MacroLog posts per meal to the coach ingest endpoint | On meal log | Calories, protein, carbs, fat at per meal granularity | n/a |
| Planning (out) | intervals.icu planned workouts, same API key | On block change | Structured sessions, delivered on to Zwift by the platform | n/a |
| Manual activities (out) | Gym and golf written back as manual activities | On conversational capture | Keeps the training load chart complete. Lower priority. | n/a |
| Calendar | Google secret iCal feed URLs, read only | Every 6h | Busy time including golf, work and travel | 24h |
| Conversation | Telegram text and transcribed voice notes | On receipt | Gym sessions, golf rounds, everything a sensor cannot see | n/a |

**The load curves are why the wellness feed is not only about recovery.** `atlLoad` records the training load the platform attributed to a day, and it is populated whether or not an activity reached us. RECOV-06 turns on that: load recorded with no local activity means the upload is missing, not the session. Its third state matters as much as the first two — an absent wellness row is not a recorded zero, and the code distinguishes them.

**Hard rule.** Absence of data is never evidence of absence of activity. If a feed is stale past its threshold, the fact is injected into the prompt and the coach asks rather than inferring a missed session or a stalled weight trend.

**Single upstream, and it can delete.** Activities, wellness and planning all run through intervals.icu, so one broken link takes three feeds down together. Worse, disconnecting an upstream integration causes that source’s data to be deleted upstream. The local FIT archive is therefore first class rather than redundant: it is the only copy that cannot be taken away, and it can restore upstream through the upload endpoint.

### Planning write rules

* **One writable surface.** The coach writes planned workouts to intervals.icu and nowhere else. Calendars are read only, so no bug can touch a real appointment.
* **Structured, not prose.** Sessions that are structured publish as machine readable steps with power targets, which is what allows the platform to render and deliver them to Zwift. Endurance rides, gym and golf publish with duration and purpose only.
* **Idempotency.** Every prescription carries a stable coach id on the planned event. Block changes update or cancel that event rather than creating a second one. Orphans are cleaned up nightly.
* **Never overwrite the athlete.** Busy time from the calendar feeds is a read only input. The scheduler places sessions in free time, and where none exists it shortens or moves the session rather than double booking.
* **Edits are memory input.** If a planned session is moved or deleted upstream, that is observed evidence. Two moves of the same weekday slot updates `availability.*`, and the local prescription updates to match so the two never diverge.
* **Feed lag is expected.** Google publishes iCal feeds on a cache, so a commitment added an hour ago may be invisible. Scheduling is therefore advisory and the weekly review confirms the week ahead.

### Mid week adjustment authority

FIT files may trigger changes to the remaining week without waiting for the Sunday review. Authority is bounded, and the boundary is asymmetric.

### The asymmetry

**Downgrades are automatic. Upgrades are not.** Shortening, easing or moving a session later can happen autonomously; that direction fails safe. Adding load, adding sessions or raising intensity waits for the Sunday review, where the full block picture and a conversation are available. Given a de-trained starting point and a spinal history, autonomous load increases are the one place this system could actually hurt you.

Trigger rules, evaluated on FIT ingest

| Observed | Action | Authority |
| --- | --- | --- |
| Session well over prescribed intensity or duration | Ease or shorten the next hard session | Automatic |
| Abandoned early, or power fade against prescription | Downgrade the next hard session to endurance | Automatic |
| Recovery flag from Whoop plus a poor session | Convert next session to rest or easy, move the hard work later in the week | Automatic |
| Session completed short or easy | Note it, leave the week alone | No compensatory loading |
| Sustained overperformance | Propose a block progression | Sunday review only |
| No file where a session was prescribed | Cross check the recovery and load signal before concluding anything | Ask, do not assume |

### Bounds on any automatic change

* Never increases planned weekly load above the block plan.
* Never schedules on a day marked rest, and never moves a session into busy calendar time.
* Touches the current week only. Anything further out belongs to the Sunday review.
* Maximum one autonomous restructure per week, and never the same session twice. Repeated triggering means the block is wrong, which is a conversation, not a rule.
* A move inside 12 hours of the original start sends a Telegram message. Everything else is silent, with the reason written into the calendar event description.
* Every change writes an `adjustment_events` row, so asking why Thursday moved returns a real answer.

**The absence trap.** A missing FIT file is ambiguous: skipped, ridden outdoors, failed to sync, or a broken watcher. Resolve it against the recovery and load signal before acting, and only after the late upload grace window has passed. Load recorded with no activity means the upload is missing, not the session. No load and no activity means it genuinely did not happen, and even then the coach asks rather than silently restructuring.

## 11. Infrastructure and setup

Memory lives in Postgres. Not files, not a vector store, not the Telegram history. Every fact, note, session and rollup is a row.

### Recommended shape

One Docker compose stack on the homelab:

```
postgres:17     # the memory, one named volume
coach-bot       # Telegram long polling + Claude API + tools
scheduler       # consolidation 03:00, weekly review, calendar and reconcile jobs
ingest          # webhook receiver, macro endpoint, FIT watcher, upstream client
cloudflared     # tunnel for the two inbound routes
```

Schema migrations as numbered SQL files applied on boot. Nightly `pg_dump` plus a markdown export of the active fact set into existing backups. Expected size after several years is a few hundred megabytes, dominated by raw session streams rather than memory.

### The one non obvious prerequisite

Two routes need to be publicly reachable over HTTPS: the macro ingest endpoint, because MacroLog posts to it from the phone on mobile data, and the intervals.icu activity webhook. A Cloudflare tunnel covers both without opening a port. Nothing else is exposed, and never the database.

### Accounts and credentials needed

* Telegram bot token, plus the numeric chat id locked in an allowlist so nobody else can talk to it.
* intervals.icu athlete id and API key, plus the webhook signing secret. One credential covers activities, wellness and planning.
* Google Calendar secret iCal feed URLs, one per calendar. Treated as bearer secrets: environment only, never logged.
* A permanent local directory for the FIT archive, included in backups.
* MacroLog holds the same intervals.icu key in its config file for body mass writes, and posts macros to the coach ingest endpoint with a shared secret header. No third party export app is involved.
* Anthropic API key, with per call cost logging as already specced.

### Alternative: Supabase

Viable and would give managed backups plus SQL access from anywhere. The trade is that ingest still runs at home, so you end up with two places to reason about instead of one. Take it only if remote access to the data matters more than a single stack.

## 12. Verification

* **Recall regression suite.** Thirty fixed questions with expected answers, run after every consolidation. Covers current state, historical state and change reasons. Alerts to an admin chat on failure only.
* **Contradiction linter.** SQL assertions: one active row per key, valid ranges coherent, supersession pointers resolve, every fact carries a created event and every superseded fact a superseded event, no safety key with non stated provenance or an actor other than `athlete`, no active fact below the 0.20 confidence floor, nothing under 0.50 presented as certain.
* **Audit on demand.** Asking the coach what it thinks it knows about a topic returns active facts, provenance, when each last changed and what it replaced.
* **Nightly dump.** Postgres backup plus a human readable markdown export of the full active fact set.

## 13. Phase membership and acceptance

Set by `docs/prd.md` section 4, which governs on scope and acceptance. This document does not define phases.

The memory subsystem described here is built in P00 (the store and its invariants), P01 (context assembly and the naturalness rules) and P02 (consolidation, the conflict matrix and decay). The mid week adjustment authority in section 10 is P09, not P00. An earlier revision of this section listed all of that as phase 00 acceptance; it was a pre-v2 plan and has been removed rather than left to mislead.

## 14. Open items

Held in `docs/prd.md` section 5, which is the single register. Two former entries here are settled and worth recording:

* **Superseded: direct Whoop integration.** An earlier revision noted that Whoop developer access is free and its v2 API available under OAuth 2.0. PRD v2 closed this off: RECOV-03 forbids any Whoop client and SEC-04 forbids OAuth anywhere in the system. Whoop reaches the coach only through intervals.icu wellness. If a metric turns out to be missing from that feed, RECOV-02 drops it from the deviation calculation — adding a direct integration is not an available remedy.
* **Resolved: mid week rewriting** is permitted, driven by FIT data, bounded by the downgrade only asymmetry in section 10.

Design document. Revised alongside PRD v2; supersedes the daily memory confirmation approach previously proposed. Implement before any ingest phase.
