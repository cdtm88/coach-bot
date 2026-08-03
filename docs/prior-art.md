# Prior art: three archived attempts, and what survives them

Three earlier attempts at this system are being archived. This document is what
was taken out of them before the lights went off. It exists because the
expensive part of those repositories was never the code: it was the sports
science constants that had to be checked against a source, the API behaviours
that had to be discovered by calling them, and the handful of design mistakes
that were only visible after they had been made.

Everything below is either a gap in this repository, a fact worth having, or a
trap worth not repeating. Where a claim comes from a file in one of the archived
repositories the path is given, so the assertion can be checked against the
original before it is trusted.

| Repository | Shipped | Shape | Reached |
| --- | --- | --- | --- |
| `cdtm88/training-tracker` | 12 April 2026 | ~2,100 lines of stdlib Python, three flat JSON files, an external Telegram agent | v1.0, 146 tests, 5 phases |
| `cdtm88/pace` | abandoned 15 June 2026 | Next.js, Drizzle, Neon Postgres, Claude for session generation | 4 of 6 phases, 84 tests |
| `cdtm88/pacer-ai` | 10 July 2026 | React PWA, FastAPI, Supabase, Anthropic SDK with native tool use | v1.0, 13 phases, 525 tests, audited |

`pacer-ai` is the one that got furthest and it is the source of most of what
follows. `pace` died on an integration it never de-risked. `training-tracker`
is small but its rule engine is the cleanest statement of the coaching logic
anyone wrote.

## 1. The trust model, which this repository states but does not enforce

This is the single largest gap and it is worth building.

`prompts/persona.md` tells the coach to separate measurement from estimate, to
give real numbers, and never to compare two body mass readings. `docs/prd.md`
puts the same idea architecturally: the coach is given permissions, not numbers.
Both are correct and neither is enforced at runtime. Nothing between the model's
output and Telegram inspects what it actually said.

`pacer-ai` found that a prompt is not sufficient, and wrote down why
(`.planning/research/PITFALLS.md:105`):

> Adding "never emit physiological numbers" to the system prompt reduces but
> does not eliminate hallucinated numbers. The system prompt is a soft
> constraint. Models trained on sports content have strong prior knowledge of
> FTP values and will emit plausible numbers even when instructed otherwise.

Its answer was three mechanisms that cooperate, none of which is a prompt line.

**Every computed value carries its own provenance.** `backend/sports_science/types.py`
is sixteen lines and is the keystone:

```python
class ToolResult(BaseModel):
    value: Any
    unit: str
    methodology: str
    inputs: dict
    model_config = {"frozen": True}
```

Every tool returns this. `pmc.py` returns `methodology="Banister PMC EWMA
CTL_TC=42 ATL_TC=7"` alongside the number and the inputs it was derived from.
Three things follow. The coach can cite a method without inventing the
attribution. Any number is reproducible from its audit row alone. And a refusal
becomes a normal return value rather than an exception: `value=None` with a
`methodology` explaining why, which is how "the ride was too short to score" is
expressed.

This repository has provenance on *facts* (`memory/facts.py`, and the
`provenance` field threaded through `agent/tools.py`) but nothing equivalent on
*computed* values. `blocks/load.py`, `health/trend.py` and `health/recovery.py`
all return bare numbers.

**A scanner sits between the model and the transport.**
`backend/agent/trust.py` matches physiological numbers in the assistant's text
in two directions, number-then-unit (`250 watts`, `85 TSS`) and
unit-then-number (`Zone 4`, `CTL 42`), then checks each one against the values
that actually came back from tools this turn. It went through three versions
and the progression is the lesson:

- v1 was a raw substring check, so `"250"` was attributed by the presence of
  `2500`, `0.250`, or any timestamp digit run.
- v2 used a boundary-aware token regex plus a float tolerance of `0.01`.
- v3 parses the tool result as JSON and compares only against genuine number
  leaves, so digits inside a string leaf such as a timestamp are structurally
  invisible rather than patched case by case. `bool` is excluded explicitly
  because it subclasses `int`.

The second half matters as much. A user saying "my LTHR is 165 bpm" made the
bot fail three times and return an empty message, because the scanner had no
channel for a number the athlete supplied himself
(`.planning/debug/resolved/onboarding-lthr-selfreport-trust-violation.md`). The
fix was a second, structurally separate allowlist built only from `role=="user"`
string content, snapshotted once before the loop so a retry cannot poison it,
and never permitted to reach a tool's inputs. The two channels are deliberately
not merged into one list, so the security argument for each stays legible.

**The model gets a legitimate way out.** `backend/sports_science/capability_gap.py`
gives it a callable `log_capability_gap`, which records the gap and returns a
fixed user-safe sentence rather than a number. A bare prohibition with no
alternative is what pushes a model into inventing something; a named escape
hatch is what makes "never fabricate" actionable. Note the discipline that the
internal method name goes to the database only and never into the reply.

For this repository the shape transfers directly, and Telegram raises the stakes
rather than lowering them: plain text has no interface affordance showing where
a number came from. `agent/naturalness.py` already runs behavioural checks on
the reply, so it is the natural home. HLTH-09, which forbids comparing two body
mass readings, is exactly the kind of rule a scanner can enforce and a prompt
can only request.

Worth taking with it: `tests/agent/fixtures/trust_corpus.py` is a labelled
corpus of twelve violations, sixteen qualitative near misses ("focus on smooth
pedalling rather than peak watts", "the ride lasted about three hours") and
eight attributed pairs, with aggregate tests asserting a zero false positive and
zero false negative rate. That corpus is what made it safe to rewrite the
scanner three times. Start it on day one and seed it from real transcripts.

### The related trap: laundering through tool inputs

`pacer-ai` learned this twice and it is subtle. The scanner guarded outputs
only, so the model could invent a number, pass it into a tool call as an
*input*, have the tool echo it back, and have the scanner then treat it as
attributed (`08-RESEARCH.md:543`). The fix was to strip six inputs from the
model's arguments entirely and re-source them server side, with the tool
description telling the model plainly that "any value you supply for these keys
is discarded". Note also that Anthropic does not enforce
`additionalProperties: false`, so the model can emit keys that were never
declared and the strip has to be unconditional rather than schema-dependent.

Earlier and worse: the model had `user_id` in a tool schema, had no way to know
the real value, and guessed `"new_user"` and `"user_001"`, with the result that
**no onboarding profile was ever persisted in production**.

The rule is that a tool argument the server can derive must never come from the
model. This repository's tool surface is mostly retrieval and proposal so the
exposure is narrower, but `propose_fact` accepts a key and value from the model
and `physiology.ftp_watts` is a writable key.

*Confirmed on 3 August 2026, and one thing was open.* The path is guarded rather
than bare: an in-turn proposal reaches `pending_writes` and never `facts`, and
what the nightly proposer does with it is re-emit under its own schema. But
`consolidation.propose` narrowed the provenance enum to exclude `computed` — the
one value of MEM-04's four that `conflict.MEASURED` counts as a measurement —
and `agent.tools.propose_fact` was still offering all four at the other door. No
`computed` fact could actually be created that way, because the proposer's
narrow enum stands between them. It was a schema describing a system that does
not exist, which is how the next door gets built wrong. The constant now lives
in `conflict` beside the rule that requires it, and a test walks every
model-facing schema rather than the two that were known about.

## 2. Sports science worth porting rather than re-deriving

All of `pacer-ai`'s constants live in one auditable file,
`backend/sports_science/constants.py`, which is itself the right decision. The
values below were checked against a source at the time.

**Power zones, Coggan and Allen, as a fraction of FTP.** Z1 active recovery
0.00 to 0.55, Z2 endurance 0.55 to 0.75, Z3 tempo 0.75 to 0.90, Z4 threshold
0.90 to 1.05, Z5 VO2max 1.05 to 1.20, Z6 anaerobic capacity 1.20 to 1.50, Z7
neuromuscular 1.50 and above. Membership is greater-or-equal on the lower bound
and strictly less than the upper, except Z7. The exclusive upper bound is
deliberate: inclusive on both puts exactly 75 percent of FTP in two zones at
once.

**Heart rate zones, as a fraction of LTHR.** Z1 0.00 to 0.68, Z2 0.68 to 0.83,
Z3 0.83 to 0.94, Z4 0.94 to 1.05, Z5 1.05 and above.

This one is a correction and it matters here. The original constants were
Friel-style boundaries (81, 90, 94, 100) while the methodology string claimed
Coggan and Allen. The in-file comment records the consequence: the Zone 2
ceiling drops from 0.90 to 0.83, which is "materially gentler for a
deconditioned, back-flagged beginner". That describes this athlete. There is a
second warning attached (`08-RESEARCH.md:347`): aggregator and calculator sites
transcribe these inconsistently, quoting 91 or 95 percent for the Z4 lower
bound. Trust Coggan and TrainingPeaks, not a calculator site.

**LTHR from a reported max heart rate**: ratio 0.875, the midpoint of the
commonly cited 85 to 90 percent heuristic. Explicitly low confidence, and the
tool description instructs the model to say so.

**Normalized power.** Clip at three times FTP before the rolling mean, falling
back to a 600 W cap when FTP is unknown. Thirty second rolling mean assuming
1 Hz. Fourth power, mean, fourth root. Two implementation facts are easy to get
wrong and both are recorded as pitfalls: the spike filter must run *before* the
rolling mean, and **zeros must be included because coasting counts**. Excluding
zeros inflates average power and can produce NP below AP, which then breaks TSS.
For scale, a single one second 1500 W spike in an hour ride moves NP by roughly
3 to 5 W after the fourth power step.

**TSS and IF.** `IF = NP / FTP`, and
`TSS = (duration_s * NP * IF) / (FTP * 3600) * 100`. Guards worth copying: no
score below 600 seconds, `None` rather than an error when the sample array is
shorter than the rolling window, `0.0` rather than a division error on an
all-zero array, and a data quality warning when IF exceeds 1.05 on a ride over
an hour, which usually means a stale FTP rather than a heroic ride.

**PMC.** `CTL_TC = 42`, `ATL_TC = 7`, alphas `1 - exp(-1/42)` and
`1 - exp(-1/7)`. The one that is genuinely easy to get wrong:
**`tsb = prev_ctl - prev_atl`**, using yesterday's values, because TSB is form
today. And `PMC_MIN_DAYS = 28` before TSB is shown at all, because for the first
six weeks CTL and ATL grow together, TSB reads near zero, and displaying it
actively masks accumulating fatigue.

**Passive FTP estimation without a test.** Two parameter critical power,
`P(t) = CP + W'/t` (Morton 1996), fitted with `scipy.optimize.curve_fit`,
physiological bounds `CP` in [50, 500] and `W'` in [1000, 100000]. Quality
effort filter is at least 180 seconds and at least 85 percent of the best
current estimate, minimum four such efforts, and below that it returns no number
at all with `confidence="insufficient_data"` rather than a bad one. Confidence
bands: 4 to 6 low, 7 to 11 medium, 12 and above high.

The two-pass filter is the hard-won part. A loose duration-only first pass
produces a rough CP which then anchors the 85 percent threshold, because without
it every rider was filtered against a flat 150 W floor "that a deconditioned
beginner effort set could never clear". This repository sets FTP from a ramp
test every four weeks, which is better evidence, but a passive estimate between
tests is a genuine addition and it degrades honestly.

Two caveats their own backlog records and this repository should not inherit:
FTP was taken as CP with no discount, which errs high and therefore errs unsafe;
and the model was fed whole-ride duration and average power rather than
mean-maximal efforts, which is not what the model is for.

**Load and progression.** A CTL ramp ceiling of 8 points per week. Where back
issues are flagged, additionally cap the increase at 10 percent of current CTL,
but with a floor of 2.0 so a cold-start athlete is not permanently stalled at
zero. That floor is the kind of detail that only shows up once someone starts at
zero.

`training-tracker` reached the same idea from the other end with a blunter rule
that is arguably better suited to this athlete: flag when weekly cycling minutes
rise more than 10 percent week over week, with two guards. A zero prior week
never flags, so returning from a rest week is not penalised. And exactly 10
percent does not flag, which is asserted by name in
`test_training_recommender.py:273`.

**Recovery week.** `pacer-ai`'s four week base mesocycle makes week four a
recovery week at 0.6 of the duration, a 40 percent volume reduction, with RPE
dropped to 3. Week one is always conservative regardless of what the data
supports: endurance only, RPE 3, capped at 45 minutes, and **power targets
suppressed entirely even when FTP confidence is high**.

## 3. Patterns worth adopting

**Recompute the whole series rather than stepping it.** `backend/pmc_recompute.py`
replaced a design that applied one EWMA step per upload. The catalogue of what
was wrong with stepping is worth reading in full
(`.planning/research/APP-REVIEW-260703.md:14`): CTL and ATL never decayed on
rest days because no row was written for a day with no ride; the row was dated
today rather than the ride's own start time, so a retroactive upload corrupted
the series; a second upload on one day double-stepped the decay and overwrote
rather than summed; and the day counter incremented per upload rather than per
calendar day.

The replacement sums TSS by ride date, walks every calendar day from the first
ride to today filling gaps with zero, and bulk upserts on a unique key so
re-running is free. It is about sixty lines and it makes backfill, correction
and FTP-change re-derivation all fall out for nothing.

This repository does not compute a load series today, since `ctl` and `atl` come
off the wellness feed and sit beside our arithmetic rather than inside it, which
is the right call. The transferable half is the gap-fill: **any decaying metric
needs a row on days when nothing happened, or it does not decay.** That applies
directly to anything built on `blocks/load.py`.

**Idempotent delivery, keyed on the period.** *Corrected on 3 August 2026, while
building the fix. The claim first written here was wrong and is worth reading
alongside what is actually true.*

What this said was that there is no delivery ledger and that the two outbound
messages a day have no idempotency key. There is one and they do:
`runtime/scheduler.run_due` calls `claim(conn, name, target)` for every job it
runs, and `scheduled_runs` is keyed `primary key (job, local_date)`. The morning
message, the evening follow-up and the Sunday review are all covered by it. The
error came from reading `notify/daily.py`, which indeed records nothing, without
following the job wrapper up into the scheduler that calls it.

The residual exposure is real but much narrower than stated, and it is the gap
between "the job ran" and "the message went out". `claim` re-claims a job whose
status is `failed` while attempts remain, so a job that posted its message and
then failed on a later line is run again and posts a second time. That is the
case `training-tracker` actually hit — its scheduler retried three times and the
athlete got the same Saturday message three times — and its fix
(`training_recommender.py:452`) is a `delivered_at` marker set by delivery and
reset when the period key changes.

Here that landed as `kind` and `period_key` columns on `messages` with a partial
unique index, claimed before the post and released if the post throws, rather
than as a separate `sent_messages` table. One row, so the conversation history
and the record of what was sent cannot disagree about what the coach said.

**The much larger thing this document missed.** `telegram.bot.record_reply` had
exactly one caller, inside `bot.drain`, and the scheduler's sender went straight
to the transport with no database write. So none of P10's three proactive
messages was ever written to `messages` at all, and `runtime.turn._history`
reads from `messages` — meaning the coach could offer at 21:00 to move
Thursday's session and have no record of the offer when the athlete answered
"yes, move it". Not a delivery-ledger problem; a memory problem wearing a
delivery-ledger disguise, and the same shape as the plan the coach could write
and never read (PR #35). Both are fixed by the same column.

**Never deliver a stale artefact silently.** *Also corrected: this exposure does
not exist here.* `training-tracker` ran the generator and the deliverer as two
scheduled jobs half an hour apart, so a failed generator meant last week's
advice was sent as though it were current. Its `AGENTS.md:91` requires comparing
`generated_at` against today before sending. This repository does not have the
shape that makes it possible: `morning_job`, `follow_up_job` and `review.run`
each build and send inside one call, so there is no window in which a stale
artefact could be picked up by a second job.

The deeper lesson still holds and is why the check is unnecessary rather than
missing: two schedulers racing on shared state, with a hand tuned thirty minute
safety margin documented as a guess, is the wrong shape. Generate and send
together.

**A computed value carries its confidence, and consumers branch on the
confidence rather than on `None`.** `pacer-ai` gates hard on this:
`plan.py:103` refuses power targets while FTP confidence is `insufficient_data`
or `low`, so the session row holds `None` rather than a number nobody should
act on, and `tss_display_ready` stays false for 28 days. The athlete is told
"RPE 3, conversational" for weeks and the bot only starts quoting watts when the
tool says it may. That is the same asymmetry this repository already applies to
load, extended to evidence.

**Priority-stack the advice and cap it.** `training-tracker`'s single best
coaching decision. Four rules can fire at once and produce contradictory advice,
so it assigned a total order (pain, then missed physio, then load spike, then
step up) and took the top two (`training_recommender.py:263`). The risk had been
predicted before the build in its own `CONCERNS.md`: "the output could be
contradictory". If a model is doing the writing, feed it the top two flags
rather than all of them.

**Two layers of validation on generated sessions, kept apart.** `pace` used Zod
for structure and bounds, then a completely separate pure function for
physiological safety with a tighter ceiling, and wrote down why
(`03-RESEARCH.md:384`): "collapsing them into one schema embeds coaching logic
in the type system, making it harder to audit". The equivalent here is a
Pydantic model for shape plus a standalone `validate_session_safety` with its
own tests, which is close to what `blocks/constraints.py` already does for the
gym. The point is to keep the coaching rule outside the type.

**Never persist an aggregate the model computed.** `pace` caught the model
asserting `totalDurationSec: 1` while its own blocks summed to 13,200 seconds,
and every downstream consumer would have inherited the lie. The first fix
checked the model's arithmetic; the design that landed discards the model's
number and recomputes from the atomic fields
(`src/lib/actions/session.ts:142`). Assume any number the model can restate is
wrong.

## 4. Integration facts, verified at the time

Only what is not already in `docs/intervals-api.md`.

**FIT parsing.** Use `fitdecode`, not `fitparse`, which has had no release since
2023 and is marked inactive. On a truncated upload `fitparse` crashes on CRC
validation while `fitdecode` with `error_handling='warn'` recovers partial data.
Always use `frame.get_value(name, fallback=None)` rather than direct field
access.

Duration must come from timestamps, not sample count:
`(last_ts - first_ts).total_seconds() + 1`. Smart recording devices, Garmin
auto-pause and variable rate Wahoo units emit fewer than one record per second,
so counting samples undercounts duration and distorts both NP and TSS.

The ride date must come from the file, preferring the `session` message's
`start_time` and falling back to the first record timestamp, never the upload
date. Elevation and speed have two field names each: read `enhanced_altitude`
and `enhanced_speed` first, fall back to `altitude` and `speed`, because
different firmware populates one, the other, or both.

Zwift specifics, which matter because Zwift is this athlete's platform. Indoor
rides carry no GPS, so anything assuming `position_lat` exists will fail. Zwift
marks pauses as `power=0` rather than as gaps, and those zeros must be kept in
the NP array. Zwift emits developer-specific fields that surface as unknown
names and must not crash the parse. And Zwift reports systematically higher
average and normalized power, and lower coasting time, than a head unit reading
the same trainer, which is a processing difference rather than a parse bug but
does mean FTP estimated from Zwift files runs slightly high.

Worth copying: log the field names of the first record frame once per parse, so
a device-specific difference shows up in the log rather than as silent zeros.

**ZWO, which is community reverse engineered and has no official schema.**
`Power` is a decimal fraction of FTP, not watts, so `Power="150"` means 15,000
percent and is dangerous rather than merely wrong. `<sportType>` must be exactly
`bike`; `ride` or `cycling` fail the import silently with nothing shown to the
user. Omit `Cadence` entirely rather than writing `0`, which displays a zero rpm
target. `<textevent>` must be a child of its segment, not a sibling.
`ftpoverride` is not universally honoured. Float artefacts such as
`0.8999999999999999` may be rejected, so pin the precision. And escape `&`
before `<` or the escaping double-applies.

The acceptance test is an actual import into Zwift. Schema validation is not
sufficient, because every failure mode above is silent.

**Anthropic.** The prompt cache minimum is 1,024 tokens and it fails **silently**
below that, with no error. `pace` deliberately padded its system prompt with
real coaching content to stay above the threshold and verified the cache by
logging `cache_read_input_tokens`. The `system` parameter must be an array;
the plain string form silently drops `cache_control`. Any mutation before the
cache breakpoint busts it, so "today is {date}" in the system prompt is a cache
miss on every call, which is an argument for the split this repository already
uses in `agent/prompt.py`.

Also: instructing a model not to wrap JSON in code fences does not work, and
`pace`'s defensive strip then had its own bug, a regex that matched ` ```json `
but not a bare ` ``` `, which silently rejected valid output. Use tool use or
structured output rather than parsing prose.

**Strava, which is why `pace` died.** API access requires a paid developer
subscription (`ROADMAP.md:142`). Beyond that: access tokens expire every six
hours and every refresh returns a new refresh token that invalidates the old
one, so two concurrent refreshes permanently break the stored credential. A 401
means revoked and is not retryable. Granted scope can be narrower than
requested and surfaces later as a confusing 403. Rate limits are 100 per fifteen
minutes and 1,000 per day, and **over-limit requests still count against the
daily total**, so retrying on 429 digs deeper. New applications are
single-athlete until Strava approves an increase, which is not guaranteed.

This vindicates the decision already recorded in `docs/prd.md` open item 4 to
stay off Strava. It is also the general lesson: `pace`'s core value was stated
as generate, ride, log, "all three or none of it matters", and the log third
lost its mechanism after four phases of work built on the assumption it would
exist. Validate an integration's access model, price and approval path before
building on it.

## 5. Traps

**A rule that reads a field nothing writes.** `training-tracker`'s
highest-priority coaching rule backed off on `pain_level > 5`. The parser
hardcoded `"pain_level": None` on every activity and nothing else ever wrote it,
so the rule could never fire in production. No phase noticed the dangling
dependency, and the same was true of `intensity`, hardcoded to `"low"`.

This repository is mostly immune by construction, because the empty body mass
series is the first case in its own test suite and the README says so plainly.
That is the correct handling of the same situation. The generalisation is worth
keeping anyway: for every rule, name the ingest path that writes its inputs and
have a test that walks message to store to decision.

The positive form of this is the more useful one. Subjective pain and effort are
the highest value signals available over a chat interface and they cannot be
regexed out of prose reliably. Ask for them.

**Passing its own test is not the same claim as being reachable.**
`pacer-ai`'s top retrospective lesson. Two endpoints were marked satisfied in
phase 3 on the strength of existing and having a unit test, and had **no caller
of any kind for seven phases**. Only a milestone-level integration audit found
it, because phase-level verification structurally cannot see it. The checklist
item they added: for every new endpoint or hook, name its real caller, not just
its test.

**The whole product loop can be broken while every test is green.** Worse than
the above and from the same repository: generated plans were never persisted.
`plan.py` returned sessions with `plan_id: None` and there were zero inserts
into the `sessions` table anywhere, grep-verified. Today, agenda, export and
adaptation all read an empty table. Alongside it, an FTP key mismatch meant
every estimated FTP was silently discarded and every athlete stayed on the
150 W placeholder forever. Both survived five phases of verification.

**A guard that can never fire, and its mirror.** A rule that refused to apply a
change when more than 30 percent of sessions moved by more than a day was dead,
because the generator moved every session by exactly one day. Fixed, it then
always fired, making the auto-apply branch unreachable. This repository has met
this exact failure once already: the plateau threshold that could never trigger
because a 28 day window spans 27 days, recorded in `docs/state-of-build.md`
under working agreements. Test a threshold at both boundaries.

**Scaling a null into a zero.** Multiplying a `None` TSS target by 0.8 wrote
`0.0`, which permanently disabled compliance for that session. Null is unknown,
not zero. `training-tracker` states the same rule for durations
(`activity_parser.py:221`) and it is worth holding generally.

**Storage shape silently redefining a domain term.** `training-tracker` merged
same-day activities keyed by type, so a day held at most one activity per type,
so "sessions per week" quietly meant "days per week" everywhere. Model sessions
as rows and derive the aggregates.

**A human verification gate the agent can approve.** `pace` recorded a
`checkpoint:human-verify` step that was auto-approved by the executor in
`--auto` mode. A gate the agent can pass on the human's behalf is not a gate.

**Tests that cannot fail.** `pace` shipped `expect(MESSAGE).toBe("...")`
comparing a local constant to itself, flagged in review and never fixed.
`pacer-ai` went the other way and wrote a meta-test that seeds a deliberately
violating file to prove its import-boundary check can actually catch one, with
the docstring "a test that always passes is not verification".

**A persistent red baseline destroys the signal.** `pacer-ai` carried nine
failing tests through the entire second half of the project, each correctly
deferred by scope rules, with the net effect that nobody could read the suite at
a glance. `pace` carried ten TypeScript errors across four phases. Fix or delete
immediately.

**Regret-tier complexity.** Google Calendar OAuth in `pacer-ai` was built across
one phase, deferred in another, given a whole renumbered phase for production
verification, then deleted entirely, leaving an unused table, a dead column and
stale environment variables. It was doomed from the start for a reason worth
knowing: Google refresh tokens expire after seven days while the consent screen
is in testing status, and sync then fails silently with `invalid_grant`. This
repository reads iCal feeds instead, which is the cheaper and more durable
choice.

Playwright in CI is the other one. It was added, failed reproducibly on real
runners, and was removed. Of 33 failures in the last report, 22 were stale tests
and 7 were mock infrastructure bugs. One was a genuine finding.

## 6. What not to carry

The planning apparatus. All three repositories used the same phase-based
framework, with `CONTEXT`, `RESEARCH`, `PATTERNS`, `PLAN`, `SUMMARY`, `REVIEW`,
`VERIFICATION`, `UAT` and `VALIDATION` documents per phase. `pace` produced
roughly 12,000 lines of planning markdown against 3,000 lines of source and its
`STATE.md` records the ending: `stopped_at: context exhaustion at 75%`. Its own
milestone audit found 3 of 11 requirements clean and 8 orphaned, and concluded
"the gaps are artifact gaps, not functional gaps". A process that generates
artifact gaps requiring an audit to detect is generating work rather than
confidence.

Three parts of it did earn their keep and this repository has equivalents for
two. Numbered decisions cited from code comments, so any line traces to the
decision that produced it: `pacer-ai` has `D-05` and `TRUST-07` in docstrings
throughout, and this repository does the same with `MEM-01` and `PLAN-05`. An
assumptions and open questions log where each entry is explicitly resolved with
its reasoning and a named fallback: `docs/prd.md` section 5 is this, and better,
because it records the date and the evidence. The third is a resolved-debug
directory where each document lists the hypotheses that were *eliminated*, which
is what makes them re-readable later; there is no equivalent here and it would
be cheap.

The one harness artifact genuinely missing is a `CLAUDE.md`. All three
predecessors had one and this repository has none. `training-tracker`'s
`AGENTS.md` is the best of them, and the reason is that it enumerates negative
cases: for each integration point it gives authority, trigger, **do not trigger**,
steps, boundaries and examples. Its do-not list is the valuable part, ruling out
questions, future plans and habitual references as activity logs, and it carries
corrections learned the hard way ("the function is named `generate_summary`, not
`get_summary`; never use `get_summary`, it does not exist"). The equivalent here
is short: the five rules already at the foot of `README.md`, the environment
gotchas from `docs/state-of-build.md`, and the document precedence order.

Also leave behind: flat JSON as a store, regex parsing of natural language when
a model with a schema will do it better, and `training-tracker`'s split between
hyphenated and underscored copies of every module, which existed only because a
kebab-case file naming convention was allowed to outrank Python's import system.

## 7. Where the originals are

`github.com/cdtm88/pacer-ai` at `73e1ab9`, `github.com/cdtm88/pace` at
`19fbc3d`, `github.com/cdtm88/training-tracker` at `9bcfb54`. Archiving keeps
them readable. Every path cited above resolves in those trees.

The three documents worth re-reading before writing any integration code, rather
than trusting this summary, are `pacer-ai/.planning/research/PITFALLS.md`,
`pace/.planning/research/PITFALLS.md`, and
`pacer-ai/.planning/research/APP-REVIEW-260703.md`.
