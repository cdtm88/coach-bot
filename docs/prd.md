# AI Cycling Coach: Product Requirements

PRD v2.1. Supersedes the July 2026 PRD.

Read alongside `docs/memory-design.md`, which is binding on how memory is implemented, and `docs/setup.md`.

v2.1 resolves the findings in `docs/prd-review.md`: the safety write path (SAFE-06), the interruption budget (CHAT-11), the combined load unit (GYM-08), context shedding and value typing (MEM-13, MEM-14), the decay curve and confidence floor, a single weight trend threshold table, phase membership for four orphaned requirements, and a phase plan split into test gates and soak gates. The design and setup guides were corrected in the same change, per SPEC-02.

## 1. Documents and precedence

| Document | Authoritative on |
| --- | --- |
| `docs/prd.md` (this) | Scope, requirements, acceptance, phase completion |
| `docs/memory-design.md` | Memory tiers, schema, key namespace, provenance, conflict matrix, consolidation pipeline, adjustment authority |
| `docs/setup.md` | Accounts, credentials, tunnel, infrastructure |

**Precedence on conflict:** the design wins on schema and memory semantics, this PRD wins on scope and acceptance, the setup guide wins on credentials and infrastructure. Fix the losing document in the same change rather than working around it.

A design section is authoritative only within the design's own remit. Where a design section states scope — section 10 in particular, which restates feed behaviour, planning rules and adjustment authority — this PRD governs and the design section stands as rationale. The design never sets phase membership or acceptance.

### Which design section informs which requirements

| Design section | Authoritative on | Domains |
| --- | --- | --- |
| 2, 3 | Three tier model, per turn context assembly | MEM, CHAT |
| 4 | DDL for the memory subsystem: facts, audit, notes, prescriptions, adjustments, working memory, feeds, verification. Ingest tables are defined at the phase that introduces them. | MEM, CONS, PLAN, ADJ, OBS |
| 5 | Key namespace and decay half lives | MEM, CONS |
| 6 | Write path and consolidation pipeline | CONS |
| 7 | Conflict resolution matrix | CONS, RECOV, CALR |
| 8 | Self correction and the mention once rule | CONS, CHAT |
| 9 | Naturalness rules | CHAT, NOTIF |
| 10 | Rationale only. Feeds, planning writes and adjustment authority are specified by the FIT, PLAN and ADJ requirements below. | FIT, HLTH, RECOV, CALR, PLAN, ADJ, LOG |
| 11 | Infrastructure and credentials | SEC, setup guide |
| 12 | Verification | OBS |

## 2. Problem and intent

Coaching quality depends on continuity. A chat assistant restarts from nothing every session, so the athlete carries the memory burden and advice regresses to generic. The goal is a coach that accumulates an accurate picture over years, corrects itself from evidence rather than interrogation, and asks for almost nothing a sensor could tell it.

Single user. Cycling primary, gym programmed against movement constraints, gym and golf captured conversationally rather than through any feed.

**Governing asymmetry:** the system may reduce load autonomously and may never increase it autonomously. Every requirement touching prescriptions inherits this rule.

**Authentication:** one intervals.icu API key for activities, wellness and planning, plus read only iCal feed URLs for calendars. No OAuth anywhere, and no third party health export app.

**Data sources:** intervals.icu activities and wellness, a permanent local FIT archive, MacroLog for macros and body mass, read only calendar feeds, and the conversation itself.

## 3. Requirements

### Memory store

| ID | Requirement | Acceptance |
| --- | --- | --- |
| MEM-01 | Facts are stored as typed rows against a controlled key vocabulary in `fact_keys`. | A write to a key absent from fact_keys is rejected by foreign key and logged. |
| MEM-02 | A partial unique index guarantees at most one active row per key. | Attempting a second active row for a key raises a constraint violation. |
| MEM-03 | Changes supersede rather than update: the old row is closed, the new row written and the pointer set, all within one transaction. | After three changes to one key, the store returns one active row and two superseded rows in order. A supersede interrupted part way leaves the prior active row intact. |
| MEM-04 | Every fact carries provenance from stated, observed, computed or inferred. | A row cannot be inserted without a valid provenance value. |
| MEM-05 | Every fact carries a confidence value between 0 and 1 and a last_confirmed_at timestamp. | Both fields are non-null on every active row. |
| MEM-06 | Every fact change writes a fact_events row with action, reason, actor and evidence. | Asserted per row, not as an aggregate count: every facts row has a created event, and every superseded row has a superseded event naming what replaced it. |
| MEM-07 | Episodic notes are stored with a generated tsvector and a GIN index for full text search. | A search for a phrase present in a note returns that note in under 200ms. |
| MEM-08 | Derived rollups (7 and 28 day load, weight trend slope, adherence, recovery deviation) are computed in SQL, not by the model. | The rollup table is populated by a SQL job, and no aggregate figure in any response originates from model arithmetic over session rows. The get_sessions tool returns individual sessions for discussion, never as input to a total. |
| MEM-09 | Working memory is a single row of conversation state, rewritten per turn. Consolidation clears today_uncommitted and regenerates rolling_summary, open_threads and last_topic from the day it has just consolidated. It never empties the row, because CHAT-05 depends on those fields surviving the night. | After consolidation, today_uncommitted is empty and the continuity fields are populated. |
| MEM-10 | Standing memory is loaded in full on every turn; only episodic notes are retrieved on demand. | Assembled context contains every active fact. Episodic notes appear only as the result of a search_memory call. |
| MEM-11 | Per turn assembled context stays under 4,000 tokens excluding conversation history. The budget counts preloaded context plus any tool results returned within the same turn. | Measured token count logged per turn; p95 under 4,000. |
| MEM-12 | A nightly job exports the full active fact set to human readable markdown alongside a pg_dump. | Both artefacts exist and are dated after the most recent consolidation. |
| MEM-13 | Where the context budget would be exceeded, content sheds in a fixed order: episodic recall, then block detail beyond the current week, then the continuity note. Safety constrained facts and active facts are never shed and never summarised. | An oversized assembly sheds in the stated order and still carries every constraint verbatim. |
| MEM-14 | Fact values are validated against the value_type declared in fact_keys before any write. | A write of the wrong type for a key is rejected and logged, never coerced. |

### Consolidation and self correction

| ID | Requirement | Acceptance |
| --- | --- | --- |
| CONS-01 | A nightly job at 03:00 in the athlete's configured timezone reads the preceding local day's messages, telemetry deltas, pending writes and active facts. | Job logs a run row with counts for each input and the local date it consolidated. |
| CONS-02 | The consolidation model emits candidate diffs as strict JSON with key, value, provenance, reason and evidence refs. | Malformed output is rejected and retried once, then logged as a failed run without partial writes. |
| CONS-03 | Conflict resolution is executed in application code, not decided by the model. | Model output containing a precedence claim has no effect on which row wins. |
| CONS-04 | Observed facts supersede stated facts for behavioural keys; stated wins for intent keys. | Seeded contradiction on availability resolves to observed; seeded contradiction on goal resolves to stated. |
| CONS-05 | Measured values supersede inferred values silently for the same key. | A ramp test result replaces an inferred threshold with no message generated. |
| CONS-06 | In-turn writes land in pending_writes and are only ratified by consolidation. The single exception is the athlete safety statement path of SAFE-06. | No path exists from an ordinary chat turn to a direct facts insert. The only direct writer outside consolidation is SAFE-06, and it can write nothing but safety keys. |
| CONS-07 | Unconfirmed facts lose confidence by category half life. Confidence is `floor + (1 - floor) * 0.5 ^ (days_since_confirmation / decay_days)` with the floor at 0.20. Facts never expire and never silently vanish. | An availability fact (30 day half life) unconfirmed for 90 days is active at 0.30. At 365 days it is approaching 0.20 and still active. |
| CONS-08 | Facts below 0.50 confidence are flagged in context as candidates for natural verification, at most one per conversation and only within the interruption budget of CHAT-11. | Context carries at most one verification candidate regardless of how many qualify, and none where a higher priority interruption holds the budget. |
| CONS-09 | A day summary note is written for every day with at least one message or telemetry event. | Notes table contains one day_summary per qualifying date. |
| CONS-10 | Consolidation is idempotent: re-running for the same date produces no duplicate facts or notes. | Second run for a date results in zero new rows. |

### Conversational agent

| ID | Requirement | Acceptance |
| --- | --- | --- |
| CHAT-01 | The bot runs Telegram long polling and responds only to an allowlisted chat id. | A message from any other chat id is ignored and logged. |
| CHAT-02 | Persona is loaded from a versioned system prompt file at `prompts/persona.md`, seeded from the source coaching conversation committed at `docs/seed/coaching-conversation.md`. | Both files exist in the repository; changing the persona file changes behaviour without a code deploy. |
| CHAT-03 | The agent never narrates memory operations in ordinary conversation. | Judged against the narration probe set in the regression suite, which includes explicit invitations to narrate. No response asserts that anything was saved, noted, remembered or updated. |
| CHAT-04 | The agent asks at most one question per message, counting any request for information however it is punctuated. | Judged per message by the regression suite, which counts interrogatives and imperative requests for information rather than question marks. A compound question counts as one only where a single answer resolves it. |
| CHAT-05 | Conversation opens from the continuity note rather than cold. | First message of a new session references the last open thread when one exists. |
| CHAT-06 | Tools available: get_context, search_memory, propose_fact, log_session, get_sessions, update_block, get_calendar, write_session_events. | Each tool has a JSON schema and an integration test. |
| CHAT-07 | On request the agent returns what it holds on a topic including provenance, last change date and superseded values. | Asking about a topic with three historical values returns all three with dates. |
| CHAT-08 | Messages received while the bot was offline are processed once on restart without duplicate replies. | Simulated 6 hour outage produces one catch-up response, not one per queued message. |
| CHAT-09 | Stale feeds are surfaced in context so the agent asks rather than infers from absent data. | With the FIT feed stale, the agent does not assert a missed session. |
| CHAT-10 | Responses are plain conversational text suitable for a phone, without headers or bullet dumps unless asked. | Median response under 120 words. |
| CHAT-11 | A conversation carries at most one interruption, meaning one item the coach raises that the athlete did not. Priority order: safety confirmation, outlier confirmation (HLTH-11), body mass gap mention (HLTH-15), pending mention (design section 8), verification candidate (CONS-08). Feed staleness (CHAT-09) shapes the agent's reasoning and is never itself an interruption. The Sunday review is exempt; its single question is governed by REV-03. | A conversation qualifying for four interruptions carries exactly one, the highest priority. Counted across the conversation, not per category. |

### Media

Telegram accepts a photo or a document as readily as a sentence, and until now the
bot recognised neither. An unhandled type was dropped silently, which is
indistinguishable from the bot being down.

| ID | Requirement | Acceptance |
| --- | --- | --- |
| MEDIA-01 | Any message type the coach cannot yet act on is acknowledged rather than dropped. | Sending a sticker produces a reply saying so, not silence. |
| MEDIA-02 | Photographs are read and discussed. A screenshot of a training week, a food label, a gym machine. | A photo of a nutrition label produces a discussion of it in the same turn. |
| MEDIA-03 | Anything read from an image is provenance `stated`, never `measured` or `computed`. A screenshot is a claim about a number, not the number. | A figure taken from an image never reaches a rollup and never supersedes a measured fact. |
| MEDIA-04 | A `.fit` file sent in chat is ingested through the FIT-14 path, identically to one dropped in the watched folder. | A file sent in Telegram becomes a session with the same content hash it would have had from the folder. |
| MEDIA-05 | Files are size capped and type checked before download, and a rejection says which. | A 40 MB video is refused by size and named as such, without downloading it. |
| MEDIA-06 | Downloaded media is retained under the same rule as its kind: FIT files to the FIT-15 archive, images not retained beyond the turn. | An image is absent from disk after the conversation; a FIT file is in the archive. |
| MEDIA-07 | An image costs a model call at the higher token weight, so the OBS-07 spend cap counts it before the call rather than after. | A photo sent while at the daily cap is refused with the reason, and no call is made. |

### Safety

| ID | Requirement | Acceptance |
| --- | --- | --- |
| SAFE-01 | Safety constrained keys are flagged in fact_keys and load verbatim at the top of every prompt. | Removing all other context still leaves constraints present. |
| SAFE-02 | Consolidation cannot create, change or supersede a safety key; only the SAFE-06 path can. | A seeded consolidation attempt is rejected and written to fact_events with actor and reason. |
| SAFE-03 | Safety keys never decay and are never selected as verification candidates. | Confidence remains 1.00 after simulated 365 days. |
| SAFE-04 | Prescriptions are validated against active constraints before being written or scheduled. | A session violating a movement constraint is blocked and logged rather than published. |
| SAFE-05 | The agent does not diagnose; health signals are surfaced as observations with a recommendation to seek clinical input where warranted. | Reviewed against a fixed set of prompts in the regression suite. |
| SAFE-06 | An explicit athlete statement of a constraint writes the safety fact directly, outside consolidation, with provenance stated and actor athlete. The agent restates the constraint and the athlete confirms before it lands. This is the one confirmation ritual in the system and the one exception to CONS-06, because without it no constraint could ever be recorded after the initial seed. | A stated injury constraint is active within the same conversation, carries actor athlete in fact_events, and no other path can write a safety key. |

### Session ingest

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FIT-01 | Activity ingest runs on whichever path is fastest. The watched folder (FIT-14) is the primary path for Zwift and needs no upstream at all. A scheduled poll, interval configurable and defaulting to 2 minutes, covers every other source. The `ACTIVITY_UPLOADED` webhook is an optimisation that removes the poll when a registered app exists; not `ACTIVITY_ANALYZED`, which is held 60 seconds to consolidate events and would spend a fifth of the PERF-03 budget before work starts. Amended 28 July 2026: webhooks require an app that only intervals.icu staff can create, and that dependency is not worth blocking ingest on. See open item 11. | A new Zwift ride appears as a session row within 2 minutes, by any path. A ride from a non-Zwift source appears within one poll interval. |
| FIT-02 | Webhook payloads are verified against the shared secret carried in the body, in constant time, and are replay safe. There is no HMAC signature to check: intervals.icu authenticates the callback with a secret in the payload, so replay safety comes from the event timestamp and the FIT-04 deduplication rather than from the transport. | A payload with a wrong or absent secret is rejected and logged, and a replayed payload creates no second session. |
| FIT-03 | The original activity file is downloaded and parsed; intervals.icu derived fields are stored alongside but never substituted for parsed values. | Session row contains both parsed streams and the platform's derived load. |
| FIT-04 | Deduplication uses the intervals.icu activity id and a content hash. | Re-delivering a webhook creates no second session. |
| FIT-05 | Sessions are matched to a prescription by date and discipline where one exists, and compliance is computed. | Matched session sets prescription status to completed with duration and intensity deltas. |
| FIT-06 | A session review is generated covering compliance and one forward looking coaching note. | Review written as an observation note within 3 minutes of the session row landing, inside the 5 minute end to end budget of PERF-03. |
| FIT-07 | Golf and gym activities are logged as activity only, without power based analysis. | A golf activity produces a session row and no compliance calculation. |
| FIT-08 | Outdoor rides and activities from any connected device ingest through the same path as Zwift. | A non-Zwift activity produces an equivalent session row. |
| FIT-09 | Bulk backfill mode ingests history silently: session rows and rollups only, no reviews, no messages. | Loading the full history produces zero Telegram messages. |
| FIT-10 | Session date and time come from the activity data, never from ingest time. | An activity uploaded two days late is dated correctly. |
| FIT-11 | Reconcile detects activities present upstream but missing locally and backfills them. | Deleting a session row and running reconcile restores it. |
| FIT-12 | A prescribed session is only treated as missed after a late upload grace window of 18 hours past the local day end, and only then with a recovery and load cross check. | An activity uploaded the next morning is matched retrospectively and never marked missed. |
| FIT-13 | Derived rollups recompute after a bulk backfill rather than waiting for the nightly job. | Rollups are correct immediately after history loads. |
| FIT-14 | A local watched folder ingests FIT files as a first class path alongside the webhook, not as redundancy. | A file dropped locally produces a session row without any upstream involvement. |
| FIT-15 | The local FIT archive is retained permanently and is never pruned by upstream changes. | Disconnecting an upstream integration leaves the local archive intact. |
| FIT-16 | The local archive can restore upstream by looping files through the activity upload endpoint. | A deleted upstream activity is restorable from local files. |
| FIT-17 | Activities authored by the coach carry a marker and are matched to the existing local session on ingest rather than creating a second one. | A manually written gym session returning through the webhook produces no duplicate. |

### Nutrition and body mass ingest

| ID | Requirement | Acceptance |
| --- | --- | --- |
| HLTH-01 | An authenticated endpoint accepts per-meal macro payloads from MacroLog. No third party health export app is used anywhere. | A payload without the shared secret is rejected; no dependency on an external export app exists. |
| HLTH-02 | Macros are stored at per-meal granularity, not as daily aggregates. | A day with four meals stores four rows. |
| HLTH-03 | Macro ingest is idempotent on meal id, and a deletion in MacroLog removes the corresponding row. | Replaying a payload creates no duplicate; a delete propagates. |
| HLTH-04 | Body mass is read from intervals.icu wellness, never from Apple Health directly. | No HealthKit dependency exists on the server. |
| HLTH-05 | The target capture rate is 2 to 3 readings per week on non consecutive days. There is no fixed weigh in day and no requirement to hit the target in any given week. | Configuration expresses a target rate, not a schedule. |
| HLTH-06 | The trend is fitted over a 28 day window using a time weighted method that tolerates irregular spacing and gaps, and is refitted as each reading lands. | A fortnight gap degrades confidence without breaking the fit. |
| HLTH-07 | At least 3 readings are required before any directional claim. Below that the coach reports readings and makes no statement about direction. | Two readings produce no trend language. |
| HLTH-08 | A rate of loss figure requires at least 6 readings spanning 3 weeks or more, and is always stated as a range rather than a point estimate. | Any rate quoted carries an uncertainty range. |
| HLTH-09 | The coach never comments on a single reading moving up, and never compares two individual readings. | No output contains a claim built on one or two readings. |
| HLTH-10 | Where all readings in a window fall on the same weekday, the coach accounts for weekly rhythm bias rather than treating the trend as clean. | A single weekday sampling pattern is detected and flagged internally. |
| HLTH-11 | A reading far outside the established pattern is confirmed once, without interrogation, before it enters the trend. | An outlier prompts a single light question. |
| HLTH-12 | Missed readings never count against adherence, are never characterised as a lapse, and are never mentioned more than once. Confidence widens instead. | A fortnight of no readings produces at most one soft mention and no adherence penalty. |
| HLTH-13 | Scheduled breaks suppress weigh in prompts entirely, and the trend resumes on return without back filling or commentary on the gap. | A holiday produces no prompts and no catch up remarks. |
| HLTH-14 | Body fat percentage is excluded from v1. If added later it is trend only over 4 to 6 weeks, never quoted as a number and never a target. | No output contains a body fat figure. |
| HLTH-15 | If no reading has arrived for more than 12 days, the coach mentions it once, lightly, and not again until a reading resets the counter. This is the only weigh in prompt in the system, it consumes the CHAT-11 interruption budget, and the generic feed staleness mechanism never emits a body mass mention of its own. | A gap of any length produces exactly one mention. |
| HLTH-16 | Plateau and stall detection require 4 weeks of readings before any programme change is proposed on weight evidence alone. | A three week flat run produces no prescription change. |

**Weight trend confidence thresholds.** Every claim the coach makes about body mass draws from this table. No other threshold is used anywhere in the system, and no requirement outside it may state its own bar.

| Claim | Minimum readings | Minimum span | Requirement |
| --- | --- | --- | --- |
| Report an individual reading | 1 | none | HLTH-07 |
| Any statement of direction | 3 | none | HLTH-07 |
| A rate of loss, always as a range | 6 | 3 weeks | HLTH-08 |
| Plateau or stall, and any programme change on weight evidence alone | a reading in each of the four weeks of the HLTH-06 window | that window | HLTH-16 |
| The trend arbitrates against an energy balance estimate | a reading in each of the four weeks of the HLTH-06 window | that window | NUT-04 |

**Amended 28 July 2026.** The last two rows read "weekly coverage" over a
"4 weeks" span, and stated that way they could never be satisfied. HLTH-06 fixes
the fit at a 28 day window, which is 28 distinct dates and therefore a maximum
first-to-last span of 27 days, so a separate 28 day span bar was unreachable by
construction: a plateau could never have been called and no programme change on
weight evidence could ever have been proposed. The two conditions were always one
condition — a reading in each of the window's four weeks *is* four weeks of
readings — and the table now says so. The bar is unchanged in intent and is now
reachable. Found while implementing P04; `coach.health.trend` carries the same
note.

### Wellness feed

| ID | Requirement | Acceptance |
| --- | --- | --- |
| RECOV-01 | Wellness is read from the intervals.icu API using HTTP basic auth with the literal username API_KEY and the personal key as password. | A date range read returns wellness records without any OAuth flow. |
| RECOV-02 | Sleep, resting heart rate, HRV, recovery score, respiration and SpO2 are stored per day from the wellness feed. Any field the feed does not carry is recorded as absent, dropped from the RECOV-04 deviation calculation, and noted once in the phase notes. Adding a direct Whoop integration is never the remedy, per RECOV-03 and SEC-04. | Every field the feed carries is stored. A field it does not carry degrades the deviation calculation without failing ingest. |
| RECOV-03 | No direct Whoop integration exists. Whoop reaches the system only through intervals.icu wellness, which carries no activity data. | Codebase contains no Whoop API client. |
| RECOV-04 | Recovery deviation is computed against the athlete's own 28 day baseline, not against platform derived scores. | Rollup contains a deviation calculated locally from stored history. |
| RECOV-05 | Wellness reads are idempotent across overlapping date ranges. | Re-reading a fortnight creates no duplicate rows. |
| RECOV-06 | Recovery and load signal is used to disambiguate a missing session before any conclusion is drawn. | With load recorded and no activity, the system does not mark the session missed. |

### Calendar read

| ID | Requirement | Acceptance |
| --- | --- | --- |
| CALR-01 | Busy time is read from Google Calendar secret iCal feeds, one URL per calendar, with no OAuth flow. | Events appear in the calendar table from a URL in the environment alone. |
| CALR-02 | Feeds are fetched at least every 6 hours across a rolling 21 day horizon. | Fetch history shows no gap longer than 6 hours. |
| CALR-03 | Busy blocks derive observed availability facts through consolidation. | A week with three evening commitments updates availability with observed provenance. |
| CALR-04 | Declined and cancelled events are excluded from busy time. | Seeded declined event does not block scheduling. |
| CALR-05 | Feed publication lag is treated as expected: scheduling decisions are advisory and the weekly review confirms the week ahead. | A commitment added today and absent from the feed does not cause a false claim of availability. |
| CALR-06 | Feed URLs are treated as bearer secrets, stored in the environment and never logged. | No log line contains a feed URL. |

### Session planning

| ID | Requirement | Acceptance |
| --- | --- | --- |
| PLAN-01 | Prescriptions publish as planned workouts on the intervals.icu calendar using the same API key as ingest. | A generated block appears as planned events upstream. |
| PLAN-02 | Each planned event carries a stable coach id in the upstream `external_id` field, published through `POST /events/bulk?upsert=true`, which creates or updates and matches the id only against events this application created. | Changing a prescription twice leaves exactly one planned event. |
| PLAN-03 | Event description carries duration, intensity target, route where relevant, and the purpose of the session. | All present on every published event. |
| PLAN-04 | Sessions are never planned into busy time visible in the calendar feeds at the moment of scheduling. Commitments the feed has not yet published are governed by CALR-05. | Seeded conflict causes a move or shortening, not an overlap. |
| PLAN-05 | Orphan planned events carrying a coach id with no matching prescription are removed on the nightly pass. | Seeded orphan is gone after the job. |
| PLAN-06 | Athlete edits to planned events upstream are detected and recorded as observed evidence. | Moving a planned session twice on the same weekday updates availability with observed provenance. |
| PLAN-07 | Planned versus completed matching uses the upstream pairing where available, falling back to local date and discipline matching. | Compliance resolves correctly under both paths. |
| PLAN-08 | The system never writes to any Google calendar. | No write path to Google exists in the codebase. |
| PLAN-09 | Structured sessions publish as machine readable workout steps with duration and power targets, not prose descriptions, using native intervals.icu workout text in the event `description`. The API also accepts a base64 zwo, fit, mrc or erg file; the text form is preferred because it needs no file generation at all. | The platform renders a published session as a valid zwo with the intended intervals. The coach produces the step list and never the file, per PLAN-10. |
| PLAN-10 | Delivery of planned workouts into Zwift is handled by the upstream platform integration; the coach never generates workout files itself. | A session prescribed on the coach appears in Zwift without any file handling. |
| PLAN-11 | Unstructured sessions (endurance rides, gym, golf) publish with duration and purpose only and are not exported as workout files. | A steady endurance prescription publishes without a structured step list. |
| PLAN-12 | An athlete edit upstream updates the local prescription to match, so the two never diverge. | Moving a planned session upstream changes the local prescription date within one sync. |

### Training blocks

| ID | Requirement | Acceptance |
| --- | --- | --- |
| BLOCK-01 | A training block is a versioned markdown document with goals, constraints and a week by week plan. | Retrieving a block returns current content and full version history. |
| BLOCK-02 | The agent rewrites the block rather than regenerating it from scratch. | Diffs between versions are localised, not wholesale replacements. |
| BLOCK-03 | Blocks run four weeks and generate prescriptions for every planned session. | A new block produces prescriptions covering all four weeks. |
| BLOCK-04 | Prescriptions specify duration, intensity target, discipline, route where relevant, and purpose. | All fields non-null on publish. |
| BLOCK-05 | Block goals include a fitness preservation constraint alongside the weight goal. | Block document contains both, and the review checks both. |
| BLOCK-06 | A ramp test is prescribed in week one to establish measured threshold and maximum heart rate. | Test appears in the plan and its result supersedes inferred physiology facts. |
| BLOCK-07 | Weekly planned load, computed across both disciplines on the combined scale of GYM-08, never increases by more than a configured percentage against the prior week. | Generation is rejected if the limit is breached, including where the breach comes from added gym volume. |
| BLOCK-08 | The agent can restructure the entire remaining block, not only the coming week. | A restructure updates all remaining prescriptions and calendar events consistently. |

### Autonomous adjustment

| ID | Requirement | Acceptance |
| --- | --- | --- |
| ADJ-01 | FIT ingest evaluates a fixed set of trigger rules against the remaining week. | Each rule fires deterministically on seeded input. |
| ADJ-02 | Automatic changes may only reduce load: shorten, ease, move later, or convert to rest. Load is measured on the combined scale of GYM-08. | Any generated change that increases computed weekly load is rejected and logged. |
| ADJ-03 | Load increases, added sessions and intensity rises are deferred to the Sunday review. | Overperformance produces a review proposal, never an immediate change. |
| ADJ-04 | Automatic changes affect the current week only. | No prescription dated beyond the current week is modified outside the review. |
| ADJ-05 | At most one autonomous restructure per week, and never the same prescription twice. | Second trigger in the same week is queued for the review instead. |
| ADJ-06 | A change inside 12 hours of the original start sends a Telegram message; otherwise the reason is written to the calendar event only. | Both paths verified with seeded timings. |
| ADJ-07 | Every automatic change writes an adjustment_events row with trigger, evidence, before and after. | Asking why a session moved returns the stored reason, not a reconstruction. |
| ADJ-08 | A missing activity never triggers restructuring before the grace window has passed and the recovery and load signal has been checked. | With wellness unavailable, the system asks rather than acts. |

### Weekly review

| ID | Requirement | Acceptance |
| --- | --- | --- |
| REV-01 | A Sunday review runs on a schedule and posts into the chat. | Review appears at the configured time each week. |
| REV-02 | The review covers adherence, load, weight trend, recovery trend and progress against block goals. | All five sections present with figures from rollups. |
| REV-03 | The review asks about the coming week's commitments and anything likely to disrupt the plan. | One question asked, not a questionnaire. |
| REV-04 | Proposed upgrades and deferred adjustments are surfaced here for a decision. | Deferred items from ADJ-03 and ADJ-05 appear. |
| REV-05 | Review output is appended to the block document and stored as a review note. | Both artefacts written. |

### Voice input

| ID | Requirement | Acceptance |
| --- | --- | --- |
| VOICE-01 | Telegram voice notes are downloaded and transcribed into message text. | A 60 second note is transcribed and answered within 30 seconds. |
| VOICE-02 | Transcripts are tagged with modality so consolidation can weight them appropriately. | Modality field present on the message row. |
| VOICE-03 | Transcription failure produces a graceful fallback asking for text, never a silent drop. | Simulated failure results in a reply. |

### Notifications

| ID | Requirement | Acceptance |
| --- | --- | --- |
| NOTIF-01 | A morning message states the day's prescribed session, or confirms it is a rest day. | Message arrives daily at the configured local time. |
| NOTIF-02 | If a session was prescribed and nothing has been uploaded by 21:00 local, the coach checks the recovery and load signal first per RECOV-06, then follows up once as an offer rather than a chase. Load recorded with no activity means the upload is missing, not the session, and suppresses the follow up entirely. | Follow-up fires once, not repeatedly, not when an activity has already landed, and not when load was recorded without one. |
| NOTIF-03 | No nudges fire during a scheduled break. | A break suppresses both NOTIF-01 and NOTIF-02 for its duration. |
| NOTIF-04 | Charts are delivered as links to rendered HTML pages, not as chat embeds. | Any chart request returns a working link. |
| NOTIF-05 | Notification times are configurable without a code change. | Changing configuration moves the messages. |
| NOTIF-06 | Weigh in prompting is owned solely by the body mass rules and emits at most one mention per gap. No separate notification path exists, and none fires during a break. | A month without readings produces exactly one mention across the whole period. |

### Breaks

| ID | Requirement | Acceptance |
| --- | --- | --- |
| BREAK-01 | Scheduled breaks can be set for holidays, travel or illness, with a start date and an optional end date. Illness breaks are open ended by default, since BREAK-04 makes an end date inert for them. | A break can be created conversationally and is stored, with or without an end date. |
| BREAK-02 | During a break, prescriptions are suspended, planned events are cancelled upstream and adherence is not penalised. | Rollups exclude break days from adherence calculations. |
| BREAK-03 | On return the coach proposes a re-entry rather than resuming the block at full load. | A two week break produces a reduced re-entry proposal at the next review. |
| BREAK-04 | Illness breaks never auto-resume; resumption requires the athlete to say so. | Break end date passing does not itself restart prescriptions for an illness break. |

### Time and locale

| ID | Requirement | Acceptance |
| --- | --- | --- |
| TZ-01 | All scheduling, day boundaries and week boundaries use the athlete's configured local timezone. | A session ridden at 23:30 local is attributed to that local day. |
| TZ-02 | Timestamps are stored in UTC and rendered local. | Database inspection shows UTC; messages show local time. |
| TZ-03 | Travel across timezones does not shift the training week. | Configured timezone governs regardless of upstream data timezone. |

### Model routing

| ID | Requirement | Acceptance |
| --- | --- | --- |
| MODEL-01 | A lightweight model handles conversation turns; a heavier model handles consolidation and session analysis. | Routing is recorded per call. |
| MODEL-02 | Model choice is configurable per purpose without a code change. | Changing configuration changes the model used. |
| MODEL-03 | Router failures fall back to the heavier model rather than failing the turn. | Simulated failure still produces a reply. |

### Gym

| ID | Requirement | Acceptance |
| --- | --- | --- |
| GYM-01 | Gym sessions are prescribed with movement patterns, sets, reps and an RPE target. | A generated gym session contains all four for every movement. |
| GYM-02 | Programming is validated against active constraint facts; any movement pattern excluded by a constraint is never prescribed. | A prescription containing an excluded pattern is blocked before publish and logged. |
| GYM-03 | An exercise library supports substitution so a blocked or unavailable movement is swapped rather than dropped. | Removing a movement's equipment yields a substitute, not an empty slot. |
| GYM-04 | Gym load is tracked as session count, RPE and duration rather than tonnage. | Rollups contain gym session count, mean RPE and total computed load per week. |
| GYM-05 | Gym sessions count toward the weekly load ceiling alongside cycling, converted per GYM-08. | A week at the cycling ceiling cannot add a gym session without reducing elsewhere. |
| GYM-06 | Gym completion is captured conversationally per the LOG requirements; there is no gym data feed. | A gym prescription closes from chat alone. |
| GYM-07 | Gym sessions publish to the calendar with duration and purpose only and are never exported as workout files. | No gym prescription produces a structured workout export. |
| GYM-08 | Gym load converts onto the same scale as cycling load so one weekly ceiling covers both: session load is RPE multiplied by duration in minutes, scaled by a configured coefficient into the cycling load unit. The coefficient is configuration, not code. This is the unit of account for GYM-05, BLOCK-07 and ADJ-02. | A gym session and a cycling session of equal computed load are interchangeable against the weekly ceiling, and changing the coefficient changes that trade without a deploy. |

### Conversational activity capture

| ID | Requirement | Acceptance |
| --- | --- | --- |
| LOG-01 | Gym sessions are captured conversationally in Telegram, not through any data feed. | A completed gym session is recorded from chat alone. |
| LOG-02 | Gym capture elicits which prescribed movements were completed, including physio work, and how the session sat against active constraints. | Captured session contains movement level detail, not just a duration. |
| LOG-03 | Golf rounds are captured conversationally including whether walked or carted, since that determines the load. | A round without that detail prompts one question. |
| LOG-04 | Elicitation is conversational, one question at a time, never a form. | No capture turn contains more than one question. |
| LOG-05 | Conversationally captured sessions create local session rows and count toward weekly load and adherence. | A logged gym session appears in rollups. |
| LOG-06 | The coach writes a corresponding manual activity to intervals.icu so the training load chart stays complete. Lower priority: nothing depends on it. | A captured session appears upstream, or fails without affecting the local record. |
| LOG-07 | Gym sessions and golf rounds post upstream through `POST /api/v1/athlete/{id}/activities/manual`, verified present in the live spec on 27 July 2026 and documented in `docs/intervals-api.md`. It accepts JSON against the Activity schema, so the TCX multipart fallback this requirement once carried is dropped. | A session logged in chat appears upstream as an activity. |
| LOG-08 | Upstream write failure never blocks or delays the local record or the conversation. | Simulated upstream outage leaves capture working. |

### Nutrition guidance

| ID | Requirement | Acceptance |
| --- | --- | --- |
| NUT-01 | Logged macros are rolled up to daily totals and 7 and 28 day averages including protein. | Rollups match a manual calculation on seeded data. |
| NUT-02 | Protein adherence is computed against the target held in facts. | Changing the target changes the adherence figure without a code change. |
| NUT-03 | Days with no logged intake are excluded from averages rather than counted as zero. | A gap day does not depress the 7 day average. |
| NUT-04 | Energy balance is presented as an estimate with its uncertainty stated. Where it disagrees with the weight trend, the trend arbitrates, but only once the trend meets the arbitration threshold in the HLTH table. | A disagreement inside the first three weeks produces neither a programme change nor a claim about which is right. |
| NUT-05 | Nutrition guidance is general: targets, patterns and adjustments. Meal plans, recipes and per meal prescriptions are out of scope. | No output contains a day by day meal plan. |
| NUT-06 | The weekly review includes intake adherence alongside training and weight. | Review contains all three. |

### Specification governance

| ID | Requirement | Acceptance |
| --- | --- | --- |
| SPEC-01 | The memory implementation follows the schema, key namespace, provenance model and conflict matrix defined in docs/memory-design.md. The PRD defines what must be true; the design defines how. | A reviewer can map every table and column in the implementation to the design document. |
| SPEC-02 | Where implementation forces a deviation from the design, the design document is updated in the same change rather than left stale. | No merged change leaves the two documents in conflict. |
| SPEC-03 | All three documents live in the repository as markdown so they are readable during every phase, not just at kickoff. | docs/prd.md, docs/memory-design.md and docs/setup.md exist and are current. |
| SPEC-04 | Conflicts between documents resolve by precedence: design wins on schema and memory semantics, PRD wins on scope and acceptance, setup guide wins on credentials and infrastructure. | A seeded conflict is resolved by the stated rule rather than by judgement. |

### Observability

| ID | Requirement | Acceptance |
| --- | --- | --- |
| OBS-01 | Every model call logs tokens, model, purpose and cost. | Daily cost is queryable by purpose. |
| OBS-02 | A recall regression suite of at least 30 questions runs after every consolidation. | Suite results stored with pass or fail per question. |
| OBS-03 | A contradiction linter runs nightly asserting schema invariants. | Seeded violations are detected. |
| OBS-04 | Failures alert to the athlete's allowlisted chat; successes are silent. There is no separate admin chat, because SEC-03 permits exactly one chat id. | A passing night produces no message. |
| OBS-05 | Each inbound feed carrying a staleness threshold records last success and surfaces staleness: activities, FIT archive, wellness, body mass and calendar. Outbound writes and the conversation itself are not feeds for this purpose. | Feeds table accurate for all five. |
| OBS-06 | The nightly backup of MEM-12 is verified restorable. | A test restore reproduces both the database and the markdown fact export. |
| OBS-07 | A daily spend cap of USD 3.00 enforces a hard stop, configurable without a code change and independent of the USD 60 monthly alert at the provider. The daily figure is a runaway backstop sized well above ordinary daily spend, not the monthly budget divided by thirty, so a sustained overspend trips the monthly alert long before the daily stop ever fires. On trip the coach says it is capped rather than going silent. | Simulated runaway usage halts model calls the same day, notifies, and a subsequent message receives an explanation rather than no reply. Changing the cap changes the trip point without a deploy. |
| OBS-08 | Consolidation runs at most once per date and retries at most once on failure. | A failing run cannot loop; the second failure logs and waits for the next night. |
| OBS-09 | Any single call exceeding a configured token ceiling is rejected before dispatch rather than after billing. | An oversized context is caught pre-flight. |
| OBS-10 | Every model call records what was sent and what came back: the system blocks as sent, the full message array including tool results, the tool schemas on offer, and the reply with its stop reason and any tool uses. | A completed call has a payload whose prompt and reply round trip, cache markers intact. |
| OBS-11 | Recording a payload can never cost a call. The payload is written after the cost row and outside its transaction; a failure to write it is logged and swallowed. | With the payload table absent, the call still returns its reply and still writes its `model_calls` row. |
| OBS-12 | The several model calls one athlete message produces are joined by a turn id, so an exchange reads back as one conversation rather than as adjacent rows. Scheduled calls carry no turn id. | A turn with two tool rounds produces three call rows sharing one id; a nightly job's row has none. |
| OBS-13 | The ledger is readable without SQL: a command reconstructs an exchange, filtered by recency, date, turn or purpose. | `coach-transcript --last 1` prints the prompt, the tool round trips and the reply of the most recent exchange. |
| OBS-14 | Raw payloads are pruned after a configurable retention window, default 90 days. `model_calls` is never pruned, because OBS-01's cost history is what that table is for. | A payload past the window is deleted on the nightly pass and its cost row survives. A malformed window falls back to the default rather than pruning everything. |

### Non functional

| ID | Requirement | Acceptance |
| --- | --- | --- |
| PERF-01 | Chat responses begin streaming within 4 seconds at p95. | Measured over 100 turns. |
| PERF-02 | Consolidation completes within 10 minutes. | Measured over 30 nights. |
| PERF-03 | FIT ingest to session review within 5 minutes of file arrival. | Measured across 20 files. |
| SEC-01 | All secrets live in environment variables, never in the repository. | Repository scan finds no credentials. |
| SEC-02 | Inbound webhook and ingest endpoints verify a shared secret or provider signature. | Unsigned requests rejected. |
| SEC-03 | Only the allowlisted Telegram chat id can interact with the bot. | Verified with a second account. |
| SEC-04 | The system implements no OAuth flow. All upstream access at runtime is by API key or secret URL held in the environment. The one exception is a single human action outside the system: intervals.icu attaches webhooks to an OAuth application, so the app is authorised once in a browser and the resulting bearer token is pasted into the environment. intervals.icu issues no refresh tokens and the access token does not expire, so there is nothing to renew. | Codebase contains no OAuth client, redirect handler or token refresh logic. |
| SEC-05 | The database is not exposed to the public internet; only the tunnel exposes named ingest routes. | Port scan from outside shows no database port. |

## 4. Phase plan

Completion is split in two. **Implemented when** is a test gate: it passes on seeded or simulated data and it is the only thing that releases the next phase. **Validated when** is a soak gate: it needs real elapsed time, it is tracked per phase, and it never blocks downstream work. Keeping them apart matters, because the validation gates together run to roughly two months and would otherwise sit on the critical path.

### M1 Memory and conversation

| Phase | Goal | Requirements | Implemented when | Validated when |
| --- | --- | --- | --- | --- |
| P00 | Stand up the memory store and its invariants so that facts can be written, superseded and audited. Implement directly against docs/memory-design.md sections 4 to 7. | MEM-01 to MEM-14, SPEC-01 to SPEC-04, SEC-01, SEC-04, SEC-05 | Schema migrated, seeded contradiction rejected by the index, audit trail returns full history for a key, a supersede interrupted part way leaves the prior row active, a wrong typed write is rejected. | n/a |
| P01 | Make the coach converse naturally over Telegram with full standing memory in context. | CHAT-01 to CHAT-11, SAFE-01, SAFE-05, SAFE-06, TZ-01 to TZ-03, MODEL-01 to MODEL-03, SEC-03 | Regression suite passes on narration, one question per message and the interruption budget. Constraints present verbatim in every prompt, a stated constraint lands through SAFE-06, a second chat id is refused. | A week of real conversation with no narrated memory operation. |
| P02 | Make memory self correcting through nightly consolidation. | CONS-01 to CONS-10, SAFE-02, SAFE-03 | Seeded contradictions resolve per the matrix, re-running a night creates nothing new, simulated decay lands on the CONS-07 curve, a seeded safety write by consolidation is rejected. | n/a |

### M2 Sensing

| Phase | Goal | Requirements | Implemented when | Validated when |
| --- | --- | --- | --- | --- |
| P03 | Ingest activities from intervals.icu and produce session reviews against prescriptions. | FIT-01 to FIT-17, SEC-02 | Full history backfills silently, a new ride reviews inside the PERF-03 budget, replayed and unsigned webhooks rejected, reconcile restores a deleted session, a locally dropped file ingests without upstream involvement, a coach authored activity returns without duplicating. | n/a |
| P04 | Accept macros from MacroLog and read body mass from wellness, with no third party export app. | HLTH-01 to HLTH-16 | Per-meal macros land from MacroLog, seeded readings at irregular spacing fit a trend, and every row of the weight trend threshold table is enforced against seeded data. | Real readings arrive near the target rate and the coach makes no directional claim before three points exist. **Blocked on HealthBridge** (open item 2): the wellness feed carries no weight, so no real reading can arrive until MacroLog writes one. The implementation gate is unaffected and was met on 28 July 2026. |
| P05 | Read recovery from intervals.icu wellness. | RECOV-01 to RECOV-06 | Every wellness field the feed carries lands each morning with no OAuth client anywhere in the codebase, and a withheld field degrades the deviation without failing ingest. | n/a. RECOV-01, RECOV-02 and RECOV-05 landed with P04 — body mass and recovery are the same read. RECOV-03, RECOV-04 and RECOV-06 followed on the same branch. The deviation is standardised against the athlete's own trailing 28 days of each measured signal and takes no input from `readiness`, which is the platform's own score and is stored beside it instead. |
| P06 | Read calendar feeds so the coach knows the shape of the week including golf and commitments. | CALR-01 to CALR-06 | Seeded busy blocks produce observed availability facts through consolidation, declined events are excluded, and no log line contains a feed URL. | Observed availability facts appear after two weeks of real calendar data. Needs the athlete to paste his secret iCal addresses into `CALENDAR_ICS_URLS`; nothing else blocks it. |

### M3 Coaching

| Phase | Goal | Requirements | Implemented when | Validated when |
| --- | --- | --- | --- | --- |
| P07 | Generate and maintain four week blocks with concrete cycling and gym prescriptions. | BLOCK-01 to BLOCK-08, GYM-01 to GYM-08, SAFE-04 | A full block generates across both disciplines, constraint violating movements are blocked before publish, the ramp test supersedes inferred physiology, and a gym addition at the cycling ceiling is rejected by the combined load rule. | n/a. Note one implementation decision that goes beyond the requirements and is deliberately stricter than them: a constraint phrase the movement vocabulary cannot read **refuses gym generation** rather than being ignored. GYM-02 says an excluded pattern is never prescribed, and a constraint nobody could parse is not a constraint that does not apply. See `coach.blocks.constraints`. |
| P08 | Publish prescriptions to intervals.icu and detect athlete edits. | PLAN-01 to PLAN-12 | Sessions appear as planned workouts, structured ones render upstream as valid zwo without any file handling, edits round trip into observed availability and update the local prescription, no duplicates after ten changes. | n/a |
| P09 | Let session data reshape the remaining week within bounded authority. | ADJ-01 to ADJ-08 | Downgrades fire automatically, an attempted upgrade is rejected, missing files never restructure before the grace window and the load cross check. | n/a |
| P10 | Run the weekly review and the daily rhythm that close the loop. | REV-01 to REV-05, NOTIF-01 to NOTIF-06, BREAK-01 to BREAK-04, NUT-01 to NUT-06, LOG-01 to LOG-08 | A review generates with all five sections from rollups, daily nudges fire on seeded timings, a seeded break suppresses them, a gym session closes from chat alone and reaches the rollups. | Four consecutive Sunday reviews with real figures and block updates. |

### M4 Trust and polish

| Phase | Goal | Requirements | Implemented when | Validated when |
| --- | --- | --- | --- | --- |
| P11 | Accept voice notes as a first class input. | VOICE-01 to VOICE-03 | A 60 second note transcribes and answers inside the budget, and a simulated failure replies rather than dropping silently. | Post ride voice note produces a session discussion without typing. |
| P12 | Prove the system remembers correctly and know what it costs. | OBS-01 to OBS-09, PERF-01 to PERF-03 | Recall suite passes 100 percent on seeded data, daily cost is queryable by purpose, the spend cap halts and explains, an oversized context is rejected pre-flight. | Recall suite passes 100 percent for fourteen consecutive nights and the performance budgets hold over their stated sample sizes. |
| P13 | Accept images and files in the chat. | MEDIA-01 to MEDIA-07 | A photo is discussed in the turn it arrives, a `.fit` file sent in chat becomes a session, and an unsupported type is refused rather than dropped. | A screenshot and a FIT file both handled without leaving Telegram. |
| P15 | Make what the model was sent and what it said readable after the fact. | OBS-10 to OBS-14 | A turn with two tool rounds reads back as one exchange through `coach-transcript`, a payload failure costs neither the reply nor the cost row, and a payload past the retention window is pruned while its cost row survives. | Built out of order, ahead of P12 and P14, and deliberately. Two reasons. It is the only way to read a real conversation back, and P16's trust scanner needs a corpus labelled from real transcripts rather than invented ones — `docs/prior-art.md` is explicit that the corpus is what made rewriting that scanner safe. And it hangs entirely off `llm.client.complete`, which every call already goes through, so it costs no new call sites and can land whole. P12 keeps OBS-01 to OBS-09 and the recall suite. |

## 5. Open items

This is the only open items register. `docs/memory-design.md` section 14 points here rather than keeping its own list.

| # | Item | Blocks | Note |
| --- | --- | --- | --- |
| 1 | **Resolved. Where is the weight in intervals.icu coming from?** Nowhere. A 21 day wellness read on 28 July 2026 returned 22 rows, 13 of them populated, and `weight` null on every one. The question assumed it either moved or repeated; it does neither. **But `tempWeight` is populated on every day**, carrying two distinct values a kilogram apart and alternating between them — a carried-forward stand-in, not a measurement series. That is the harmful case this item was written to catch, arriving under a field name it did not anticipate, and it must never be wired into HLTH-04. | — | Verified 28 July 2026, `docs/intervals-api.md`. |
| 2 | **Resolved: HealthBridge is required.** Item 1 resolved toward building it. Body mass has no source until MacroLog writes it to intervals.icu wellness, so it is a prerequisite of P04's *validation* gate and belongs on the athlete's side of the setup guide's division of labour. It is not a prerequisite of P04's implementation gate, which is seeded data by design, and P04 is built and merged without it. Still out of scope for this repository. | P04 validation | The read side is built and idle: the moment a weight lands in wellness it becomes a reading and the trend starts. |
| 3 | **Resolved. Does the wellness feed populate all six RECOV-02 fields?** Six of seven arrive: `sleepSecs`, `sleepScore`, `restingHR`, `hrv`, `readiness`, `respiration`, `spO2` on 13 of 22 days. `hrvSDNN` is null on all 22 and is dropped from the RECOV-04 deviation, which is the behaviour RECOV-02 provides for rather than a defect. | — | Verified 28 July 2026. RECOV-03 and SEC-04 still bind: a direct Whoop integration is not a remedy. |
| 4 | **Verify no activity gaps after the Strava disconnect.** Compare the current activity list against the pre-disconnect snapshot and backfill anything missing from the local FIT archive through the upload endpoint. | P03 | |
| 5 | **Resolved. Manual activity endpoint** exists and takes JSON: `POST /api/v1/athlete/{id}/activities/manual`. LOG-07 simplified and the TCX fallback dropped. | — | Verified 27 July 2026 against the live spec. |
| 6 | **Transcription:** hosted API or local model on the homelab. Latency against privacy and running cost. | P11 | |
| 7 | **Resolved. Spend caps:** USD 3.00 daily hard stop in the coach, USD 60 monthly alert at the provider. Written into OBS-07 and setup step 5. | — | Revisit after the first month of OBS-01 data. The daily figure is a runaway backstop, not a thirtieth of the monthly, so the two are deliberately not proportional. |
| 8 | **Resolved on 3 August 2026. Raw payload retention: 90 days,** then pruned on the nightly pass, configurable through `COACH_PAYLOAD_RETENTION_DAYS`. Written into OBS-14. Long enough to investigate anything anyone realistically investigates and to build a trust corpus from real conversation; short enough that MEM-12's pg_dump does not carry a copy of the persona and the fact blocks for every call ever made. `model_calls` is never pruned, because OBS-01's cost history is what that table is for. | P15 | Forced by OBS-10, which is the first thing to write prompts down. **Still open, and narrower than this item was:** how long `messages` itself is kept, and whether consolidated days can be pruned. That is memory rather than observability, and consolidation is what makes it answerable. |
| 9 | **Resolved. Persona seed.** The source coaching conversation is committed at `docs/seed/coaching-conversation.md`; `prompts/persona.md` is written from it and `seeds/athlete.json` carries the facts it establishes. | — | The transcript is the audit trail for every seeded value. Re-read it before changing the persona's voice. |
| 10 | **Resolved. Webhooks exist**, outside the OpenAPI spec: registered in the app management UI (`/settings`, then Manage App), delivering `ACTIVITY_UPLOADED`, `CALENDAR_UPDATED` and `SPORT_SETTINGS_UPDATED`. FIT-01, FIT-02, PLAN-02 and PLAN-09 updated against what they actually do. | — | Verified 27 July 2026 from the official integration cookbook. **Activity webhooks are not delivered for Strava activities**, so reconnecting Strava would silently disable ingest. See item 4 and `docs/intervals-api.md`. |
| 11 | **Deferred, not blocking. Register the OAuth app if webhooks are ever wanted.** Webhooks attach to an app, and apps can only be created by emailing intervals.icu and waiting for a person, which also asks for a website URL, a logo and a privacy policy for what is a single user tool. Decided 28 July 2026 not to take that route for now. Ingest instead uses the watched folder for Zwift and a 2 minute poll for everything else, both of which need only the API key. The webhook receiver stays built and tested, so switching it on later is: email intervals.icu, point the webhook at the tunnel route, set `INTERVALS_WEBHOOK_SECRET`, and lengthen `COACH_POLL_INTERVAL_S`. No code change. Re-authorising discards the previous token, so redoing the consent later means updating `.env`. | none | FIT-01 amended to make the webhook an optimisation rather than the mechanism. |


181 requirements across 24 domains in 23 sections.
