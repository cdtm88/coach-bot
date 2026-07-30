# State of the build

> Read this first in a new session. It says what is done, what is next, and what
> the environment does that will otherwise waste your time.
>
> Last updated 28 July 2026, at `main` after PR #11. Everything through P07
> is merged.

## What is merged

| Phase | Covers | State |
| --- | --- | --- |
| P00 | Memory store, supersession, provenance, audit, decay | merged |
| P01 | Conversational agent over Telegram with standing memory | merged |
| P02 | Nightly consolidation; memory self-corrects | merged |
| P03 | Activity ingest and session reviews (FIT-01 to FIT-17, SEC-02) | merged |
| — | Persona seed from the source transcript (open item 9) | merged |
| — | Seven ingest defect fixes found by reviewing against the live API (PR #7) | merged |
| — | Poll-based ingest; the webhook dependency is gone (PR #8) | merged |
| — | Handover doc and the three scripted live checks (PR #9) | merged |
| P04 | Macros from MacroLog, body mass from wellness (HLTH-01 to HLTH-16) | merged (PR #10) |
| P05 | Recovery from wellness (RECOV-01 to RECOV-06) | merged (PR #10) |
| P06 | Calendar feeds (CALR-01 to CALR-06, PLAN-08) | merged (PR #10) |
| P07 | Training blocks and gym programming (BLOCK, GYM, SAFE-04) | merged (PR #11) |

499 tests, all against a real Postgres. Schema is at migration 011; the
scheduler's own ledger is created on first use rather than as a migration,
because it is process bookkeeping rather than part of the memory design.

**M1, M2 and P07 are merged, and the system now runs.** Next is **P08** — publishing prescriptions to the
intervals.icu calendar and detecting athlete edits (PLAN-01 to PLAN-12). It is
the first phase that writes *upstream*, and it is the one V1 gates: **run
`scripts/verify_intervals.py v1` before starting it**, to find out whether
`oauth_client_id` is populated under a personal API key. If it is null,
PLAN-05's orphan sweep needs an `external_id` prefix convention instead, which
changes the shape of the phase rather than a detail inside it.

Two soak gates are waiting on the athlete and neither blocks P08: HealthBridge
writing body mass, and the secret iCal addresses in `CALENDAR_ICS_URLS`. Both
pipelines are built, tested and idle.

## What runs

Three processes, all console scripts:

| Process | What it does |
| --- | --- |
| `coach-ingest` | Every inbound feed: two HTTP routes, the activity poll, wellness, calendar, the sweep, and the webhook drain. |
| `coach-agent` | Telegram long poll, one turn per backlog. |
| `coach-scheduler` | The nightly jobs on the athlete's local 03:00. |

**This is new, and it closed a gap the phase table could not show.** Until 28
July `coach-ingest` was the only one. Eight phases were merged and 458 tests
passed, and none of it produced a coach the athlete could talk to: nothing
constructed an `anthropic.Anthropic`, nothing called `api.telegram.org`, and
`consolidation.pipeline` had no caller. `ANTHROPIC_API_KEY` and
`TELEGRAM_BOT_TOKEN` were configured and read by nobody.

No phase was at fault. Those requirements are behavioural and are tested against
injected clients and transports, which is the right way to test them. The wiring
at each seam was simply nobody's phase. `src/coach/runtime/` is where it lives
now, and it holds no opinions: every rule it applies — the interruption budget,
the naturalness checks, the conflict matrix — already had a home, and this calls
them in order.

### The consolidation proposer, now written

P02's remaining half. `coach/consolidation/propose.py` is the callable
`pipeline.run` always took and nothing ever supplied, and the scheduler now runs
consolidation, decay and the export in that order — decay against what the night
wrote rather than against yesterday's picture.

Three things about it are worth knowing without reading it:

* **The tool is forced.** CONS-02's "strict JSON" is `tool_choice`, not a request
  in the prompt. `llm.client.complete` grew a `tool_choice` parameter for this;
  conversation leaves it unset, because a coach that must call a tool before it
  may speak is not a coach.
* **The model cannot claim `computed` provenance.** MEM-04 has four values and
  MEM-08 reserves computed figures for SQL. `conflict.MEASURED` counts computed
  as measured, so a model labelling its own arithmetic `computed` would get an
  inference promoted over a real measurement. The proposer's schema narrows the
  enum to stated, observed and inferred; `pipeline.DIFF_SCHEMA` is untouched,
  because it describes what a diff *is* rather than what a model may assert.
* **The prompt is in code, not in `prompts/`.** CHAT-02 makes the persona a file
  so voice can change without a deploy. This is not voice — changing it changes
  what lands in long term memory, which should never move without a diff and a
  test.

Fixed while wiring it: `consolidation_job` never passed a timezone offset, so
`pipeline.gather` fell back to its default of zero and windowed a **UTC** day. In
Asia/Dubai that is four hours out — a message at 01:00 local was consolidated
into the wrong day or not at all, which is exactly what TZ-01 exists to prevent.
The offset is now taken for the day being consolidated rather than for today, so
a DST boundary cannot shift the window either.

### What is still not wired

**Streaming to Telegram.** PERF-01 measures time to first token and the turn loop
accepts an `on_text` callback, but the agent does not pass one — Telegram has no
partial-message API worth the complexity, so the reply is sent whole. Revisit if
PERF-01's p95 becomes a real complaint rather than a number.

### One requirement built early, on purpose

OBS-07's daily spend cap is a P12 requirement. The *stop* is implemented now, in
`runtime.models.check_spend`, because this is the change that first lets the
system call a model on a loop and a runaway before P12 would be a real bill. The
check is a query against `model_calls`, which P01 already writes.

What is implemented is the stop and the coach saying it is capped. The
notification and the wider OBS-07 acceptance are still P12's. A test asserts the
single construction site for the client, so the guard has nowhere to be routed
around.

## How ingest actually works now

This changed on 28 July and the older documents describe the previous design if
you read them carelessly. Three paths, no webhook:

1. **The watched folder** (`COACH_FIT_WATCH`) is the primary path for Zwift. A
   `.fit` file dropped there is ingested with no API call, no credential and no
   upstream involvement at all. It sees a ride before intervals.icu does and
   keeps working when intervals.icu does not.
2. **The poll** (`COACH_POLL_INTERVAL_S`, default 120s) covers every other
   source. One API call per pass; a file downloads only when something new
   appears.
3. **The webhook receiver** is built, tested and idle. Webhooks need an app that
   only intervals.icu staff can create, and that dependency was judged not worth
   blocking ingest on. Turning it on later is configuration, not code.

`coach-ingest` runs all three plus the slow sweep
(`COACH_SWEEP_INTERVAL_S`, default 6h) that ages out missed sessions.

## How body mass works, and why it looks over-built for an empty table

The HLTH requirements are mostly not about storage. They are about what the coach
is allowed to *say*: a direction needs three readings, a rate needs six across
three weeks and must be quoted as a range, a plateau needs a reading in each of
four weeks, and one reading moving is never worth a sentence. A model handed a
list of readings will honour none of that, because the arithmetic is trivial and
the restraint is not.

So the readings never enter the prompt. What enters is a slope and a range fitted
in SQL (`coach.health.trend`), plus an explicit list of which claims the current
evidence supports. HLTH-09 then costs nothing to obey — there is no pair of
readings in the context to compare. If you change one thing here, do not change
that.

Three consequences worth knowing before you touch it:

* **The fit is weighted least squares in one SQL statement**, not a moving
  average. The series is irregular by design (HLTH-05 sets a rate, not a
  schedule), and a moving average over an irregular series silently overweights
  whichever week had four readings.
* **Every threshold lives in `coach.health.trend`** and nowhere else, because the
  PRD says no requirement outside its table may state its own bar. A test scans
  `src/` for reading-count comparisons elsewhere and fails on one.
* **Body mass is excluded from the generic stale feed block** (HLTH-15), so the
  body mass block is the only thing in the prompt that can say the feed is
  silent. That is why it renders even at zero readings: with both absent,
  silence reads as a stable weight.

Macros are the simpler half. MacroLog posts per meal to `/macrolog/meals` with a
shared secret in an `X-Coach-Secret` header — a different secret from the
intervals.icu webhook, deliberately. Meals are stored per meal, never as daily
totals; the daily total is a query.

## How recovery works

Same shape, one rule doing the work. RECOV-04 says the deviation is computed
"against the athlete's own 28 day baseline, **not against platform derived
scores**", and the second half is the one that is easy to lose. `readiness` is
Whoop's recovery percentage arriving through intervals.icu, and a composite that
included it would be a rebadged Whoop score wearing the word "local".

So the deviation is built from measured signals only — HRV, resting HR, sleep
duration, respiration, SpO2 — each standardised against the athlete's own
trailing 28 days of that same field and oriented so positive means better than
his baseline. `readiness` is stored and shown to the coach beside the local
figure, labelled as the platform's opinion, with nowhere to enter the arithmetic
from. That is the FIT-03 rule applied to wellness.

Two behaviours that look like edge cases and are not. A field the feed withholds
is dropped rather than defaulted, which is how `hrvSDNN` needs no special
handling at all. And a baseline with no variance produces no deviation rather
than a division — worth knowing, because a fixture with identical values every
day will silently test the refusal path instead of the one you meant.

## How the calendar read works

Three things about it are worth knowing before changing anything.

**Recurrence is parsed, not approximated.** A weekly commitment arrives as one
VEVENT with an RRULE, plus EXDATEs for the weeks it was cancelled and
RECURRENCE-ID overrides for the weeks it moved. `icalendar` and
`recurring-ical-events` do that expansion; a hand-rolled loop would miss the
athlete's standing Tuesday, which is worse than having no calendar at all.

**The window looks backwards as well as forwards.** CALR-02 says a rolling 21 day
horizon and is written from the scheduler's point of view, but CALR-03 derives
*observed* availability from busy blocks, and that is a claim about weeks that
have already happened. So the fetch covers 28 days back as well, and the delete
that propagates a cancellation is bounded by that window — an unbounded delete
would take CALR-03's evidence with it every six hours.

**The feed's identity is the URL fingerprint, not its name.** A failed fetch has
no document to read `X-WR-CALNAME` from and falls back to a positional label; the
next success names it "Work". Keyed on the name those are two feeds and the second
collides. Keyed on the fingerprint it is one feed that has learned its name.

CALR-06 is stricter here than it looks. The secret URL is never written to the
database at all — a secret in a column is a secret in the nightly pg_dump — and
`httpx` logs the request URL at INFO on every call, so a redacting filter is
installed on that library's logger. Being careful in our own log lines would not
have been enough.

## How block generation works, and the one rule that is stricter than the PRD

P07 is the first phase that writes, so it is the first where the governing
asymmetry has teeth: the system may reduce load autonomously and may never
increase it autonomously.

**The model writes the shape, the code writes the rows.** A plan says "Thursday,
gym, lower body, RPE 7". It never names an exercise, because GYM-02 is not a rule
a model can be trusted to remember on a Thursday in week three. The movement is
chosen from the library, against the constraints, or it is not prescribed at all.

**Nothing is written until the whole block validates.** A generation that
breaches the BLOCK-07 ramp in week four must not leave weeks one to three in the
database, so prescriptions are built in memory, checked, and inserted in one
transaction. That is also what makes BLOCK-08's restructure safe.

**An unreadable constraint refuses to generate.** This is the decision to argue
with if you are going to argue with one. If a constraint phrase matches nothing
in the movement vocabulary, gym generation raises rather than proceeding as
though the athlete were unconstrained. Ignoring what we could not parse means a
constraint he stated in his own words silently stops applying and he has no way
to notice. The PRD does not require this; GYM-02 says an excluded pattern is
*never* prescribed, and this is what that costs.

It nearly bit immediately, which is the point: the first run refused on **his own
seeded constraint**, because "no sit-ups" did not match the vocabulary entry
"sit up". The fix was to widen the matcher for hyphens and plurals. The fix is
never to reword the athlete's constraint.

**The trunk is three patterns, not one.** Flexion and loaded rotation are
excluded by his restrictions; anti-extension and anti-rotation are exactly what a
repaired disc wants. Collapsing them into "core" would have banned the useful
half along with the harmful half.

**A substitution that preserves nothing is worse than a gap.** The wide fallback
in `library.substitute` was removed after it answered a blocked squat with a calf
raise. GYM-03's acceptance is about the *equipment* case, which same-pattern
substitution serves; when a pattern and all its near neighbours are excluded, the
truthful answer is that there is nothing appropriate, recorded as a
`constraint_blocks` row with no substitution.

## What the live checks found

V2 and V3a were run on 28 July 2026, and a fuller field census followed while
building P05. Full results are in `docs/intervals-api.md`; the ones that change
what gets built:

**There is no weight in the wellness feed.** Not stale, not repeated — absent, on
all 22 days read. So HealthBridge is required (open item 2, resolved), it is the
athlete's to build, and until it exists the body mass half of P04 has a working
pipeline with nothing in it. That is by design rather than by accident: every
threshold is tested on seeded data and the empty series is asserted first, so the
first real reading starts a trend with no code change. It does mean **P04's
validation gate cannot start yet**, which is tracked and does not block P05.

**`hrvSDNN` never arrives** while the other six RECOV-02 fields do (13 of 22
days). RECOV-02 drops it from the deviation, which is the requirement working
rather than a bug. Open item 3, resolved.

**`tempWeight` is populated on every day and must never be used.** This is the
trap this project came closest to walking into, because it is the obvious fix for
the weight trend being empty. Across 22 days it carried two distinct values one
kilogram apart, alternating between them — a carried-forward stand-in the
platform keeps so power-to-weight arithmetic has a number, not a measurement
series. `tempRestingHR` behaves the same way. Fitting HLTH-06's trend on it would
draw a confident line through two numbers and present it as data. Both are named
in `wellness.NEVER_STORED` with a test.

**`atlLoad` is a real load signal**, populated on all 22 days and zero on eight
of them. That is what makes RECOV-06's cross check a distinction rather than a
hypothetical: load recorded with no local activity means the upload is missing,
not the session.

Two smaller things worth not rediscovering. The activity file arrives with
`Content-Encoding: gzip` already stripped by httpx, so the bytes are plain FIT on
arrival — the sniff handles it either way. And **no rate limit headers are
returned at all**, so the 120 second poll interval stands on arithmetic rather
than measurement.

### V3b — the wellness write. **Probably do not run this.**

The premise is gone. It tests whether a connected provider resyncs over an API
written weight, and this account has no provider writing weight. Unlocking a day
has no documented API path, so running the locked half is a one way door opened
to answer a question the account no longer poses. Let HealthBridge write without
`locked`, and revisit only if a value is ever seen to revert.

### V1 — external_id scoping. Writes, self-cleaning. Blocks P08 only.

```bash
uv run python scripts/verify_intervals.py v1
```

Creates a probe calendar event, reads it back, upserts it again, deletes it.
Answers whether `oauth_client_id` is populated under a personal API key — the
documented scoping rule is written for OAuth clients, and if it is null then
PLAN-05's orphan sweep must use an `external_id` prefix convention instead.

**Now urgent: P08 is the next phase.** This was "not urgent, P08 is a long way
off" until 28 July. It writes, but it cleans up after itself, and the answer
changes the shape of PLAN-05 rather than a detail inside it — so it is cheaper to
run first than to discover halfway through.

## Open items

| # | Item | Blocks | State |
| --- | --- | --- | --- |
| 1 | Where does the weight in intervals.icu come from? | — | resolved: nowhere, the field is absent |
| 2 | Who builds HealthBridge, is it needed? | P04 validation | resolved: yes, needed, and it is the athlete's |
| 3 | Which wellness fields does the Whoop link populate? | — | resolved: six of seven, `hrvSDNN` never |
| 4 | Verify no activity gaps after the Strava disconnect | — | open, and now runnable: the key works and the poll covers Strava-sourced rides, so this is a comparison nobody has done rather than a blocker |
| 5 | Manual activity endpoint | — | resolved |
| 6 | Transcription | P01 polish | open, needs a decision |
| 7 | Spend caps | — | resolved: $3/day, $60/month |
| 8 | Raw message retention period | — | open, needs a decision |
| 9 | Persona seed | — | resolved |
| 10 | Do webhooks exist? | — | resolved: yes, but they need an app |
| 11 | Register the OAuth app | — | **deferred by decision**, no longer blocking |

## Environment gotchas that will otherwise cost you an hour

**Environment variables are injected at container start.** Setting one in the
environment configuration does not reach a session that is already running. If
`INTERVALS_API_KEY` is not visible, start a new session rather than debugging it.

**`curl` to `api.github.com` returns nothing through the proxy.** Use the
`mcp__github__*` tools for every GitHub read. A polling loop built on curl will
silently report nothing forever, which has already wasted time twice in this
project.

**Postgres dies on container churn.** `./scripts/dev-db.sh start` brings it back;
the data directory survives. A wall of connection errors across the whole suite
almost always means this rather than a code fault. If it says the lock file
already exists, the server is already running and the second start is the thing
that failed, not the first.

**The test suite never reads `.env`.** It uses `TEST_DATABASE_URL` and its own
throwaway databases, so it passes with no credentials at all. A test that needs a
credential is a test that has been written wrong.

## Working agreements established in this project

**`.env` is gitignored and must stay that way.** Check `git status` shows nothing
before committing. The keys currently in use are development keys the athlete
intends to rotate before productionising.

**Never print a credential.** Not in tool output, not in a commit, not in a PR
body. `scripts/verify_intervals.py` is written so its output is safe to paste
anywhere.

**Correct the documents rather than working around them.** When a requirement no
longer describes the system, amend it and say so in the change — FIT-01 was
amended when polling replaced the webhook rather than left quietly false, and the
plateau row of the weight trend table was amended in P04 when it turned out to
state a span that a 28 day window makes unreachable. SPEC-02 requires fixing the
losing document in the same change.

**A rule that can never fire is not a strict rule, it is a broken one.** The
plateau threshold above read as conservative and was in fact dead: no plateau
could ever have been called and no programme change on weight evidence proposed.
It survived a PRD review because "weekly coverage over 4 weeks" is obviously
right until you notice a 28 day window holds 28 dates and therefore spans 27
days. Worth checking any threshold you write against the window it is measured
in.

**External review is evidence, not instruction.** An integration specification
reviewed on 28 July reported seven defects; six were real and one was wrong,
because it was working from a stale API snapshot. Acting on it wholesale would
have made the docs less accurate. Verify each claim against the code or the live
spec before acting.

## Where things live

```
docs/prd.md              scope, requirements, acceptance, phase plan, open items
docs/memory-design.md    memory tiers, schema, provenance, conflict matrix
docs/setup.md            accounts, credentials, tunnel, the folder sync
docs/intervals-api.md    what the API actually does, verified, with dates
docs/prd-review.md       the v2.1 review and what it changed
docs/seed/               the source coaching conversation; the audit trail for
                         every seeded fact and for the persona's voice
scripts/verify_intervals.py   the checks above
scripts/dev-db.sh        throwaway Postgres for the suite

src/coach/memory/       P00: the store, supersession, provenance, decay
src/coach/agent/        P01: prompt assembly, the tool surface, naturalness
src/coach/consolidation/  P02: the nightly pass and the conflict matrix
src/coach/ingest/       P03: activities, the archive, reviews, the process
src/coach/health/       P04 and P05: macros, body mass, recovery
src/coach/calendars/    P06: the iCal feeds and observed availability
src/coach/blocks/       P07: the constraint gate, the library, load, generation

The README's layout section lists every module with one line on what it is for.
```

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
