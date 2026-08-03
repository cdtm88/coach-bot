# State of the build

> Read this first in a new session. It says what is done, what is next, and what
> the environment does that will otherwise waste your time.
>
> Last updated 30 July 2026, at `main` after PR #16, with P09 on a branch.
> V1 and V4 have both been run against the live account.

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
| — | The runtime: `coach-agent`, `coach-scheduler`, OBS-07's stop (PR #12) | merged |
| — | P02's proposer, so consolidation actually runs (CONS-02, PR #13) | merged |
| P08 | Publishing upstream and athlete edit detection (PLAN-01 to PLAN-12) | merged (PR #16) |
| P09 | Bounded mid-week adjustment authority (ADJ-01 to ADJ-08) | merged (PR #17) |
| P10 | The weekly review and the daily rhythm (REV, NOTIF, BREAK, NUT, LOG) | built |
| — | Docker image, compose stack, MEM-12's pg_dump sidecar (PR #18) | merged |
| — | libpq connection route, for a Postgres deployed separately (PR #19) | merged |
| — | The four defects that only appear once deployed (PR #20) | merged |
| — | What three archived attempts were worth: `docs/prior-art.md` (PR #38) | merged |
| — | The outbox: proactive messages were sent and never recorded (PR #38) | merged |
| P15 | The model call ledger: what was sent, what came back, readable (OBS-10 to OBS-14) | built |
| — | `CLAUDE.md`, the resolved-debug directory, and the corrected zone tables | built |
| P16 | The trust layer, in shadow (TRUST-01 to TRUST-08) | built |
| — | P09 wired, and the matching defect underneath it | built |
| — | A FIT file with no samples: a lost ride, an abandoned start, or neither | built |

1045 tests, all against a real Postgres. Schema is at migration 020; the
scheduler's own ledger is created on first use rather than as a migration,
because it is process bookkeeping rather than part of the memory design.

**P00 to P09 are built and the runtime is wired.** P08 publishes prescriptions to
the intervals.icu calendar and accepts the athlete's edits back; P09 lets a
session reshape the rest of that week, downward only.

**And it is deployed.** Since 2 August the stack runs on the athlete's own
server against a Postgres it did not deploy, and a message to the bot gets a
reply. That is the first time the whole path has existed at once: Telegram, the
allowlist, the database, the model, and back.

**P10 closes the loop.** The coach now speaks first: a morning message, one
evening follow-up when a session has left no trace, and a review every Sunday.
Gym sessions and golf rounds are captured from chat and land in the same rollups
a Garmin upload would. Breaks suspend the plan rather than scoring it as missed.

Next is **P11** — collapsing the running containers, described under "What runs".

Two soak gates are waiting on the athlete and neither blocks anything: HealthBridge
writing body mass, and the secret iCal addresses in `CALENDAR_ICS_URLS`. Both
pipelines are built, tested and idle.

## What runs

Three processes, all console scripts:

| Process | What it does |
| --- | --- |
| `coach-ingest` | Every inbound feed: two HTTP routes, the activity poll, wellness, calendar, planned-calendar sync, the sweep, and the webhook drain. |
| `coach-agent` | Telegram long poll, one turn per backlog. |
| `coach-scheduler` | The nightly jobs on the athlete's local 03:00: consolidation, PLAN-05's orphan sweep, decay, the fact export. |

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

### P14: six containers, and how many of them should exist

Deployed, the stack is six containers: `coach-db`, `coach-db-backup`,
`coach-migrate`, `coach-agent`, `coach-ingest`, `coach-scheduler`. For a
single-user system on one box that is a lot of moving parts to look at, and the
ask is to get it to one. Worth being precise about which of the five merges are
real, because they are not equally good.

**`coach-migrate` is not a process.** It runs once, exits 0, and exists as a
container only because `depends_on: service_completed_successfully` is the
ordering primitive Compose gives you. It cannot simply be folded into the other
three: three containers each applying migrations at boot is three racing
writers, and the ledger's primary key would turn that into two crashes and one
success. It disappears *for free* the moment the three become one, because then
there is a single startup sequence to put it in. This merge is pure gain.

**The three long-running processes can merge, and the cost is real but bounded.**
They are separate today because they fail independently: a wedged Telegram long
poll must not stop activity ingest. Merging them means a supervisor — and the
code is already shaped for it, since `runtime.agent.serve` takes a
`threading.Event` and the scheduler and ingest loops are both interruptible. The
work is not starting three threads; it is making a thread that dies get noticed,
restarted, and logged, which is precisely the job Docker's `restart:
unless-stopped` does for free today. Doing it worse than Docker does it is the
failure mode to design against, so the acceptance test is a killed loop, not a
happy start.

What it buys: one log stream instead of three, one restart, and roughly two
thirds of the memory back — three CPython interpreters each holding the same
imports is the single largest waste in the current deployment.

**Postgres should stay out of it.** Folding `coach-db` in is where "one
container" stops being tidying and starts being a different system. The database
here was deployed independently, with its own conventions, its own backup
sidecar and its own retention; PR #19 exists because the *application* adapted to
that rather than the other way round. Putting Postgres in the coach's container
would couple `pgdata`'s lifetime to an application restart and make every future
image rebuild a database event. `coach-db-backup` could reasonably fold into
`coach-db` as a cron inside it, but that is the database stack's call and not
this project's.

So the honest target is **three** — `coach-db`, `coach-db-backup` and `coach` — and
six to three is where nearly all the benefit is. One is achievable and is not
recommended; if it is wanted anyway, it should be a deliberate decision recorded
here rather than a consequence of this phase.

Acceptance, when it is written:

- One application container. Migrations run once, before anything else starts,
  and a failure there stops the container rather than starting the loops
  against an empty schema, which is exactly what PR #20 had to fix.
- Each loop restarts on its own after a crash, without taking the others down,
  and the restart is visible in the log rather than silent.
- `docker stop` drains and exits inside the grace period, with the Telegram
  offset unadvanced for any turn that did not complete.
- The three console scripts still exist and still work, because running one loop
  alone is how they are debugged.

### P16: the trust layer, and why it is not enforcing yet

TRUST-01 to TRUST-08. The largest gap `docs/prior-art.md` named: this repository
states the trust model in `prompts/persona.md` and in the PRD, and nothing
between the model's output and Telegram inspected what it had actually said.

**It ships in shadow, and that is the decision rather than an unfinished
edge.** `agent/trust.py` records every physiological figure the coach states
with nothing behind it, and blocks none of them until `COACH_TRUST_ENFORCE` is
set. The scanner is tuned against invented examples; a corpus built from real
conversation has not existed until now, because until P15 there was no way to
read a real conversation back. A false positive under enforcement costs the
athlete a legitimate answer with no way for him to know why, which is how a
guard gets switched off. The order is: run shadow, read the hits back through
`coach-transcript`, add them to `tests/fixtures/trust_corpus.py`, then enforce.

**The cheaper design.** `pacer-ai` reached this through a `ToolResult` type
carrying value, unit, methodology and inputs on every computed value, which
here would mean refactoring `blocks/load.py`, `health/trend.py` and
`health/recovery.py`. It is not necessary: the question is what the model was
*given* this turn, and that is already knowable from the assembled prompt and
the tool results. Provenance on computed values stays available and unbuilt.

**Three channels, deliberately not merged.** Tool and prompt figures; figures
the athlete supplied himself; small whole numbers that are ordinary prose.
`pacer-ai` shipped with one channel and an athlete saying "my LTHR is 165 bpm"
made the bot fail three times and answer with nothing. Both are snapshotted
before the model loop, so a retry cannot poison either, and so a tool result
arriving later cannot be read as something the athlete said.

**Two things are deliberately not checked, and they are written down.** A zone
number is a label rather than a measurement — "ride Z2" is an honest
prescription and requiring `2` to be attributed would fail every one of them —
and percentages are left alone for now because adherence, compliance and
intensity factors are all quoted as percentages and the false positive rate was
not worth the coverage. A scanner whose gaps are undocumented reads as complete.

**HLTH-09 did not move.** It would have been tidy to fold "never compare two
body mass readings" behind the same gate, and it would have weakened it: that
rule is enforced today through the naturalness retry, and putting it behind a
shadow flag would have turned an enforced rule into a logged one. The trust
scanner covers body mass figures additionally, through the `kg` unit.

`review/voice.py` keeps its stricter policy — every number, not only the
physiological ones, because the review is assembled entirely in SQL — and now
expresses it over the same matching primitive. Two grounding implementations
that can drift apart is how a rewording quietly loosens a gate, which is the
failure the `trend.describe` split was built to avoid.

### P15: reading a turn back

OBS-10 to OBS-14, and built out of phase order on purpose. `model_calls` has
recorded the shape of every call since P01 and none of its content, so "why did
the coach say that" had no answer: the system prompt is assembled per turn from
facts that change nightly and cannot be reconstructed afterwards, and the tool
results that actually shaped a reply were never stored anywhere at all.

**It cost no new call sites.** Everything hangs off `llm.client.complete`, which
is already the only place a model is called, so consolidation, the session
review and the Sunday voicing were covered without being touched. That is the
same property that made OBS-01 true by construction rather than by discipline,
used a second time.

**Three decisions worth knowing without reading it.**

`model_call_payloads` is a second table rather than columns on `model_calls`.
The cost table is on the hot path — `runtime.models.spent_today` sums it before
every turn — and widening a row scanned by a daily aggregate to carry tens of
kilobytes of JSON would make the cheapest query in the system the dearest.
Payloads are also the first thing anyone prunes, and pruning a column means
rewriting the cost row rather than deleting a different one.

**The payload write is outside the cost row's transaction, and swallows.** A
call that happened must be billed whether or not a copy of it survived, so
OBS-01 and OBS-07 cannot be made to depend on OBS-10 succeeding. A `model_calls`
row with no payload is a normal outcome, and `coach-transcript` says so rather
than printing an empty prompt — an unrecorded prompt and an empty one are
different facts.

**`turn_id` is what makes it readable.** One athlete message can produce three
calls: ask for a tool, ask for another, answer. Before this they were three rows
with adjacent timestamps and nothing joining them. `runtime.turn.respond` mints
one because it is the boundary of an exchange, and the naturalness retry belongs
to the same exchange as the reply it is retrying. Scheduled jobs get none: a job
is a call and not a conversation, and inventing an id would have the ledger
claim one that never happened.

`coach-transcript` is the fifth console script and the point of the phase. A
ledger that needs SQL across two tables and hand-decoded JSON is one nobody
reads on the day it matters. `--last N` counts *exchanges* rather than rows, so
a turn that used three tools does not eat the window and no reply is ever
printed without the question above it.

Retention resolves PRD open item 8, which had been open since the PRD was
written: 90 days, then pruned nightly, configurable. `model_calls` is never
pruned. A malformed window falls back to the default rather than being read as
"prune everything", because a typo that silently emptied the ledger is not a
failure mode worth having.

### P10: the coach speaks first

Everything before this was reactive — the athlete wrote, or a file arrived, and
the system responded. P10 is the first phase where the coach initiates, and
almost all of its difficulty is in *not* doing so.

**NOTIF-02 is a list of conditions under which a message would be wrong.** An
activity already landed; a break is running; nothing was prescribed; or the
platform recorded training load for the day with no activity attached, which
means the ride happened and the upload did not. `load_recorded_on` returns three
values and only `True` suppresses — `None` is the feed having nothing for the
day, which is the coach not knowing rather than the athlete not training. Both
branches have a test, because a `None` read as either boolean is a bug that
looks correct.

**The scheduler had to grow up.** `due()` hardcoded 03:00 and always targeted
yesterday. That is right for consolidation and wrong for everything here, so a
job now carries a `Schedule` — hour, optional weekday, and whether it covers
today or yesterday — and the date a job is *about* stays the ledger key, so a
job about today cannot collide with yesterday's row. NOTIF-02's "fires once" is
that ledger rather than any flag, which is why `follow_up` can be a pure
function of the day.

**Suspension is a third prescription status.** 'missed' feeds the ADJ-01
triggers and depresses adherence; 'cancelled' says the athlete declined. Neither
is true of a week the coach agreed to. And break days leave the adherence
*denominator*, not just the numerator — counting a suspended session as
offered-and-not-taken would be exactly as wrong as counting it as a miss.

**NUT-03 falls out of the grouping rather than being enforced.** A day with no
meals produces no row in the per-day subquery, so `avg` never sees it. Counting
it as zero would take extra code. Coverage travels with every average anyway,
because excluding gap days is right and misleading on its own — two well-fed
days out of seven should not read as a well-fed week.

**Coming back from a break is a proposal, not an application.** The governing
asymmetry lets the system reduce load unasked; a re-entry increases it from zero,
so it is the athlete's decision by construction. The ladder's numbers are
invented and say so; the tests assert the property the PRD actually fixes, which
is that every step starts below the pre-break week and rises.

**The review had to be split into a record and a message.** The first live
review posted the assembly verbatim, and it was six labelled sections in a fixed
order, five of them reporting that nothing had happened. Worse, the weight
section was `trend.render`, which is the *permission block the prompt carries* —
so the athlete's weekly summary from his coach contained "You may report this
figure if asked for it" and "Do not call a plateau or change the programme on
weight evidence". The system talking to itself in front of him.

They are two artefacts now. `Review.render` is the record: every section, keyed
by the Sunday, written to the note and the block, because "no intake was logged
in the week of 2 August" is a fact about that week and something later may need
it. `Review.message` is what he reads, and it collapses the empty sections into
one line. `trend.describe` is the athlete-facing twin of `trend.render`, walking
the same ladder off the same `Claims` so that rewording cannot loosen a gate —
the existing threshold-drift test caught the first attempt reaching past
`Claims` to `fit.n`, which is exactly the drift the split risks.

**Then the review turned out to be answering the wrong question.** It reported
Load — training stress on GYM-08's combined scale — and never once said what he
did. "440 over the week" is the right number for setting next week's ceiling and
is not a week anyone recognises. `Effort` is new and reads `sessions`: how many
times he went out, for how long, how far, counted from what actually landed
rather than from what was prescribed, because a ride nobody asked for still cost
him the time. `data_unavailable` sessions are counted and flagged rather than
dropped (FIT-15) — a zero there would be a lie about the athlete rather than
about the data.

`Ahead` is the other half. A review that only looks backwards is a report; what
makes one worth reading on a Sunday evening is the session it points at. It
reads the seven days after the Sunday and names the heaviest, ranked by
`blocks.load.of_spec` — the same function BLOCK-07's ramp limit is enforced
with, so the session named is the one the week is actually built around. A model
asked to pick would pick differently on identical weeks, and "which session
matters" should not be re-litigated every Sunday. It is also the one absence
that stays in the message rather than collapsing into the quiet line: nothing
prescribed for next week means nobody has written it yet, and burying that is
how he finds out on Tuesday.

**Goals became record-only, which is a third state and not a deletion.** They
are his own two year targets, stated by him, unchanged since the block opened,
and they were the longest section in the message — so the review led with the
one thing in it he was already certain of. They stay in the record and therefore
in the facts the voicing call is given, because the coach has to know what the
week was for before he can say why it mattered. Knowing it and reciting it are
different things, and `record_only` is that difference. It also does not count
as an absence: "Nothing on stated goals this week" would be the review
complaining to the athlete about its own configuration.

**Voicing puts the model back on the output path, one way only.** `voice.say`
takes the finished assembly and the persona and asks for it in the coach's own
words. It may reorder, cut and rephrase; it may not add a number, and `_grounded`
enforces that rather than requesting it — every figure in the reply has to appear
in the facts, and a rounding is allowed where an invention is not. It also
re-asserts REV-03's single question, because one question is a property the
assembly guarantees and voicing is the only step that could take it away. Any
failure — no client, no key, a bad reply — posts the assembled message, which is
a decent message on its own. Improving a working review must not be able to cost
him the review.

**Two bugs the tests found, both mine.** `store` marked a break's re-entry as
proposed after the early return for "no active block", so a deployment without
one would re-propose the same re-entry every Sunday. And `push_upstream` called
`create_manual_activity` on a client whose method is `create_manual` — the fake
answered to the wrong name and every test in the file passed. There is now a
test asserting the fake and the real client share the surface.

### P09: the asymmetry, in three modules

ADJ-01 to ADJ-08. The split is the design rather than tidiness:

* `adjust/triggers.py` — what the data says, and what it suggests. Knows nothing
  about whether it is allowed.
* `adjust/authority.py` — whether that may happen autonomously. Knows nothing
  about cycling.
* `adjust/apply.py` — doing it, recording it, and deciding whether to say so.

A rule that approved its own change would make every mistake in a proposal a
mistake in the training, and every new rule a fresh chance to get the bound
wrong. So the bound is checked once, by code that has never heard of intervals,
against GYM-08's combined load figure.

**ADJ-02 is enforced by the vocabulary, not by a check.** A rule may ask for
`shorten`, `ease`, `move_later`, `convert_to_rest` or `note` — there is no word
for an increase, so a rule cannot propose one. The load comparison in
`authority` is the belt behind that, and it rejects at *warning* level because a
rule proposing an increase is a bug in the rule rather than a normal outcome.

**Deferring is not failing.** ADJ-03 and ADJ-05 both send the proposal to the
Sunday review, and `deferred_adjustments` is the table REV-04 will read. Only a
load *increase* is rejected outright. Design section 10's reasoning for ADJ-05 is
worth keeping in view: "repeated triggering means the block is wrong, which is a
conversation, not a rule" — so the second trigger in a week becomes that
conversation rather than being suppressed.

**One thing this phase had to fix in an earlier one.** `review.attach` computed
compliance and returned it without storing it, which was fine while the only
reader was the review written in the same call. P09 cannot work that way: an
`ease` **rewrites the target spec**, so recomputing compliance afterwards would
compare the ride against the reduced target and the figure would improve every
time the coach downgraded something — the rules would be reading a number their
own actions had moved. `prescriptions.compliance` is now frozen at match time.

`adjustment_events.authority` is new for the same class of reason: ADJ-05 counts
autonomous restructures, and P08 writes to that table too. A PLAN-04 placement or
an athlete's own edit is not the coach spending its authority, and counting them
would silence the coach for a week because the athlete rescheduled something.

### P08, and the two live checks it needed

V1 settled the shape: `oauth_client_id` is null under a personal API key, so there
is no way to ask upstream which events are ours. `coach.plans.events.is_ours`
matches an exact `external_id` pattern instead — exact and not a prefix, because
PLAN-05 *deletes* what it claims and the athlete's own events are on the same
calendar.

**V4 caught a one-character bug that no unit test could have.** PLAN-09 publishes
native workout text and PLAN-10 forbids the coach generating files, so the whole
requirement rests on the platform compiling our text correctly. It does — but the
repeat line must not carry a leading dash. `- 3x` is parsed as an unrecognised
step and **silently dropped**: a 3x set renders once, 1260s arriving for a 1980s
session, with no error from anywhere. `3x` renders as `<IntervalsT Repeat="3">`.

The first version of V4 then scored the *fixed* output as still broken, because
`IntervalsT` carries no `Duration` attribute — only `OnDuration`, `OffDuration`
and `Repeat` — so summing `Duration` alone counted a correct set as a third of its
length. Worth stating because it nearly caused a working feature to be rewritten:
the check has to understand both encodings, and it now does.

Three PLAN decisions worth knowing without reading the code:

* **PLAN-04 moves within the evening, never across days.** BLOCK chose the weekday
  against observed availability; shifting Thursday's intervals to Friday would be
  re-planning rather than accommodation. If the evening is genuinely full the
  session is reported unplaceable and the *rest of the block still publishes* — a
  hole the athlete can be told about beats a block that refused to go up.
* **The athlete's edit wins.** PLAN-12 says the two must never diverge and does not
  say which side yields. Moving a session in the app is a decision, and the
  alternative is the coach republishing over it every cycle. The coach's view is
  expressed as evidence for the next block instead.
* **PLAN-05 will not sweep the past.** A past planned event is what an activity was
  paired against and what the athlete did or failed to do. Sweeping it would delete
  a record to tidy a calendar nobody is looking at.

**One thing P08 does not do, and it is BLOCK's rather than PLAN's.** No generator
emits a step list. P07 produces steady sessions plus a ramp-test flag, and
choosing a ramp protocol is a training decision. So the structured path (PLAN-09)
is built, tested and verified live, and every session the current block content
produces takes the unstructured path (PLAN-11) — which is correct for steady
endurance and gym work. Wiring a generator to emit steps is a BLOCK change.

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

**P09 now runs, and fixing it uncovered something larger.** *Recorded 3 August
2026: found while tracing the outbound message paths, wired the same day on the
athlete's instruction.*

The original finding was the fourth instance of the failure this project keeps
meeting. `adjust.pass_.run` is reached from one place, `ingest.service.on_activity`,
and only when its `adjust` flag is true; `adjust=True` appeared nowhere in
`src/`. `on_activity` itself had one caller — `_handle_delivery`, on the webhook
path, which is built, tested and **idle**. So ADJ-01 to ADJ-08 were unreachable
twice over.

**The larger thing was underneath it.** The live poll path did not call
`on_activity` at all: it called `review.review` alone. `review.match` and
`review.attach` had two callers in the whole of `src/`, one on the idle webhook
path and one in `logbook.capture`. So **no ride ingested by the running
deployment was ever matched to its prescription.** Sessions kept a null
`prescription_id`, prescriptions stayed 'planned' indefinitely, compliance was
never frozen, and every ADJ rule would have read a figure that did not exist.

The FIT-12 sweep is why this was invisible rather than loud. A prescription with
a session on the same day is reported "unmatched rather than missed", so nothing
was ever wrongly called a miss — it stayed open instead, which reads as a plan
nobody is following rather than as a bug.

The fix is one shared tail. `service.finish` does match, freeze, review and
adjust in that order, and both ingest paths call it. Two call sites that must
agree about the order of four operations is precisely the seam this project
keeps finding defects in.

`adjust=True` now appears exactly once in `src/`, in the thread `ingest.server.main`
starts, and `tests/test_p09_wiring.py` asserts that it does — the half
`tests/test_adjust.py:894` could never assert, since a test that the switch
exists is not a test that anything turns it on. ADJ-06's notice goes through an
`Outbox`, so a message telling the athlete his Thursday was shortened is
recorded as something the coach said rather than vanishing into the transport.

**The rides already past need `coach-reconcile`, once.** The fix closes the loop
from the next ride onward and can do nothing for the backlog: `poll` only
considers sessions with `reviewed_at is null`, and every affected session was
reviewed. So `ingest/backfill.py` is a one-off command that matches them, and it
is deletable once it has been run.

Two things about it are worth knowing before running it. It is a **dry run
unless given `--apply`**, because it moves prescriptions to 'completed' and
freezes compliance against them, which changes past weeks' adherence in the
Sunday review. And the dry run is the real pass **inside a rolled-back
transaction** rather than a read-only imitation: `review.match` answers from
stored state, so a read-only plan would offer one prescription to every ride on
that day and promise what the apply could not keep. Simulating and discarding
solves that without a second copy of the matching rules, which would have been
the fourth place in this repository where two implementations of one thing have
to agree.

It reviews nothing and adjusts nothing. Re-reviewing would write a second note
and spend a model call per session to repeat what was said at the time, and
running P09's rules over months of history is the backfill case `finish` guards
against by parameter.

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

### V1 — external_id scoping. RUN 30 July. P08 is unblocked.

```bash
uv run python scripts/verify_intervals.py v1
```

Creates a probe calendar event, reads it back, upserts it again, deletes it. Ran
clean against the live account and answered both questions:

* **`oauth_client_id` is null**, as suspected. `created_by_id` is the athlete's
  own id, so a personal key has no application identity at all. The documented
  scoping rule — "events created by your application" — does not apply to us, and
  **PLAN-05's orphan sweep cannot filter on it.** It must use an `external_id`
  prefix convention.
* **Upsert on `external_id` matches**, one event and not two. So PLAN-02 has its
  idempotency key: writing the same prescription twice updates rather than
  duplicating.
* **Bulk-delete accepts an `external_id` filter** and reported
  `{"eventsDeleted":1}`. Not one of the questions asked, and the most useful
  answer of the three: the sweep has a working delete primitive keyed on the
  same field the write is keyed on.

So the shape of P08 is settled. One `external_id` namespace, prefixed so the
coach's own events are identifiable without an application identity; upsert to
publish; bulk-delete to sweep.

**One thing this did not establish**, and PLAN-05 should not assume it: the
delete filtered on an *exact* `external_id`, not a prefix. Whether the filter
accepts a prefix or wildcard is unknown. It does not block the phase — the coach
writes the events, so it knows the exact ids it created and can delete by them —
but a sweep written as "delete everything under my prefix" in one call is
unverified, and should be built as "delete the ids I hold" until someone checks.

## Open items

| # | Item | Blocks | State |
| --- | --- | --- | --- |
| 1 | Where does the weight in intervals.icu come from? | — | resolved: nowhere, the field is absent |
| 2 | Who builds HealthBridge, is it needed? | P04 validation | resolved: yes, needed, and it is the athlete's |
| 3 | Which wellness fields does the Whoop link populate? | — | resolved: six of seven, `hrvSDNN` never |
| 4 | Verify no activity gaps after the Strava disconnect | — | open, and now runnable: the key works and the poll covers Strava-sourced rides, so this is a comparison nobody has done rather than a blocker |
| 5 | Manual activity endpoint | — | resolved |
| 6 | Transcription | P01 polish | open, needs a decision |
| 7 | Does bulk-delete match an `external_id` *prefix*, or only an exact value? | — | open, and P08 does not need it: exact-match deletion is enough because the coach holds the ids it wrote |
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
CLAUDE.md                what will otherwise waste an hour or ship a defect
docs/prd.md              scope, requirements, acceptance, phase plan, open items
docs/prior-art.md        what three archived attempts were worth
docs/debug/resolved/     defects that took work to find, and what was eliminated
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
src/coach/plans/        P08: publishing upstream, the sweep, athlete edits
src/coach/observe/      P15: reading a model call back. `coach-transcript`

The README's layout section lists every module with one line on what it is for.
```

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
