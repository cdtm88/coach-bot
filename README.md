# coach-bot

AI Cycling Coach · single-user Telegram coaching agent

## Start here

**[`docs/state-of-build.md`](docs/state-of-build.md)** — what is done, what is
next, and the environment gotchas that will otherwise cost you an hour. Read it
first in a new session.

## Documents

| Document | Authoritative on |
| --- | --- |
| [`docs/state-of-build.md`](docs/state-of-build.md) | Current state, next actions, working agreements |
| [`docs/prd.md`](docs/prd.md) | Scope, requirements, acceptance, phase completion, open items |
| [`docs/memory-design.md`](docs/memory-design.md) | Memory tiers, schema, key namespace, provenance, conflict matrix |
| [`docs/setup.md`](docs/setup.md) | Accounts, credentials, tunnel, infrastructure |
| [`docs/deploy.md`](docs/deploy.md) | The command sequence for a first deployment, and how to verify the backup |
| [`docs/intervals-api.md`](docs/intervals-api.md) | What the intervals.icu API actually does, verified, with dates |
| [`docs/prd-review.md`](docs/prd-review.md) | Record of the v2.1 review and what it changed |
| [`docs/prior-art.md`](docs/prior-art.md) | The three archived predecessors: what was taken from them, and the traps they paid for |
| [`docs/seed/`](docs/seed/) | The source coaching conversation. The audit trail for every seeded fact and for the persona's voice |

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
Fix the losing document in the same change (SPEC-02).

## Status

**P00 to P09 are built, and the system runs.** The memory store and its
invariants, the conversational agent, nightly consolidation, activity ingest with
session reviews, macros from MacroLog with the body mass trend read from
intervals.icu wellness, recovery deviation computed against the athlete's own
baseline, the calendar feeds, and now four week training blocks with cycling and
gym prescriptions behind a constraint gate. 630 tests, all against a real
Postgres.

The live checks that gated P04 were run on 28 July 2026 and the headline is that
**the wellness feed carries no weight at all.** Nothing feeds body mass until
MacroLog's HealthBridge writes it, which is the athlete's to build and outside
this repository. So the trend pipeline is complete, tested and empty: every
threshold is asserted on seeded data and the empty series is the first case in
the suite, so the first real reading starts a trend with no code change.

**P08** publishes prescriptions to the intervals.icu calendar, keeps them out of
busy evenings, sweeps its own orphans nightly, and accepts the athlete's own edits
back into the local plan. Two live checks shaped it: a personal API key has no
application identity, so the sweep keys on an `external_id` pattern rather than the
documented `oauth_client_id`; and the workout-text repeat line must carry no
leading dash, or a 3x set silently renders once. `docs/state-of-build.md` has both.

**P09** lets a session reshape the rest of that week, and only downward.
Shortening, easing and moving later happen on their own; adding load, adding
sessions and raising intensity wait for the Sunday review. The rules propose, a
separate module decides whether they may, and there is no word in the action
vocabulary for an increase — so a rule cannot ask for one.

Three processes run it: `coach-ingest` for every inbound feed, `coach-agent` for
the conversation, `coach-scheduler` for the nightly jobs. Until recently only the
first existed — the phases were merged and tested against injected clients and
transports, and nobody's phase owned the wiring at the seams. `src/coach/runtime/`
is that wiring, and it decides nothing: every rule it applies already had a home.

The scheduler runs four nightly jobs at the athlete's local 03:00: consolidation,
then PLAN-05's orphan sweep, then confidence decay, then the fact export. Consolidation is the reason
the memory is self correcting rather than append only — `coach/consolidation/`
holds the pass, and the model appears at exactly one step of it, proposing
candidate diffs. What lands is decided in code.

Ingest needs no webhook. Zwift rides arrive through a watched folder with no API
call at all; everything else arrives on a poll whose interval is configurable.
The webhook receiver is built and idle because registering an app requires a
person at intervals.icu to approve one, and that was not worth blocking on.

Later phases are in `docs/prd.md` section 4.

## Layout

```
migrations/        numbered SQL, applied on boot (001 to 013)
Dockerfile         two stages; deps from the committed uv.lock, uv not shipped
docker-compose.yml six services; the tunnel is opt-in behind a profile
prompts/persona.md the coach's voice, written from docs/seed/
seeds/athlete.json the initial facts, each traced to the source transcript
scripts/
  verify_intervals.py  the live-account checks; V1, V2, V3a and V4 all run
  dev-db.sh            throwaway Postgres for the suite
  backup.sh            MEM-12's pg_dump half; runs in the postgres image
src/coach/
  config.py        environment only; no credential defaults (SEC-01)
  clock.py         local day and week boundaries, and the configured zone (TZ-01/02/03)
  db.py            connections
  feeds.py         last success per inbound feed; what CHAT-09 reads (OBS-05)
  migrate.py       the boot-time runner
  seed.py          one-time memory seed from seeds/athlete.json
  memory/          P00: the store
    keys.py        controlled vocabulary and value typing (MEM-01, MEM-14)
    facts.py       supersession, provenance, audit, decay, the SAFE-06 path
    notes.py       episodic archive with full text search (MEM-07)
    state.py       working memory and the pending queue (MEM-09, CONS-06)
    context.py     per turn assembly and the shedding order (MEM-10/11/13)
    export.py      the nightly markdown fact export (MEM-12)
  agent/           P01: the conversation
    persona.py     the versioned system prompt (CHAT-02)
    prompt.py      per turn context assembly; what the coach is told, and when
    tools.py       the eight tool surface and its dispatch (CHAT-06)
    naturalness.py the behavioural checks: narration, questions, diagnosis, HLTH-09
    interruptions.py  one interruption per conversation, claimed by priority (CHAT-11)
  llm/             model routing and accounting
    client.py      streaming, token accounting, cost per call (OBS-01)
    router.py      light model for chat, heavy for consolidation (MODEL-01/02/03)
  telegram/
    bot.py         allowlist, backlog catch-up, message persistence. No transport yet
  consolidation/   P02: the nightly pass
    pipeline.py    read the day, emit diffs, ratify what the matrix allows
    propose.py     the one step the model appears at: candidate diffs (CONS-02)
    conflict.py    the conflict resolution matrix, in code not in the model (CONS-03)
  ingest/          P03: activities
    parse.py       samples in, computed values out; never a derived aggregate
    client.py      the intervals.icu API, basic auth, rate limit headers
    activities.py  an upstream activity to a session row
    archive.py     the permanent local FIT archive; contains no delete (FIT-15)
    review.py      prescription matching, compliance, reviews, missed sessions
    reconcile.py   the poll and the bulk backfill
    service.py     the pipeline every ingest path calls
    webhook.py     the receiver and its delivery queue; idle without an app
    server.py      the process: routes, activity poll, wellness, calendar, sweep
  health/          P04 and P05: intake, body mass, recovery
    macros.py      per-meal macros from MacroLog, idempotent on the meal id
    wellness.py    the wellness read, and the fields it deliberately never stores
    bodymass.py    readings, the outlier gate, the gap, the rollup
    trend.py       the weighted fit in SQL, and what it permits the coach to say
    recovery.py    the local deviation; the platform's score never an input
    breaks.py      is today inside a break; the rest of BREAK-* lands in P10
  calendars/       P06: busy time
    feed.py        secret iCal feeds; the URL is never stored and never logged
    availability.py  busy blocks to observed availability, through consolidation
  blocks/          P07: programming
    constraints.py the gate: what the athlete may not be asked to do (SAFE-04)
    library.py     the exercise library and substitution (GYM-03)
    load.py        one scale for cycling and gym, and the weekly ceiling
    document.py    the versioned block markdown (BLOCK-01, BLOCK-02)
    generate.py    a plan to prescriptions, validated before anything is written
  plans/           P08: the planned calendar, upstream
    events.py      a prescription as an upstream event; who owns one (PLAN-02)
    workout.py     native workout text for a structured session (PLAN-09/10)
    publish.py     the upsert, and never into busy time (PLAN-01, PLAN-04)
    sweep.py       nightly orphan removal; the only thing that deletes (PLAN-05)
    sync.py        the athlete's own edits, back into the plan (PLAN-06, PLAN-12)
  adjust/          P09: what a session does to the rest of the week
    triggers.py    the fixed rule set; proposes, decides nothing (ADJ-01)
    authority.py   the bounds: reduce only, this week, once (ADJ-02/04/05/08)
    apply.py       the change, the record, and the 12h notice (ADJ-06/07)
    pass_.py       the entry point ingest calls
  review/          P10: the Sunday review
    weekly.py      the sections, assembled in SQL; the record and the message
    voice.py       the record said in the coach's voice, and the numbers guard
  notify/          P10: the two messages a day the coach sends unasked
    daily.py       what today is (NOTIF-01); one follow-up, never a chase (NOTIF-02)
    outbox.py      the one door a message he did not ask for goes out through
    charts.py      the images that go with them
  logbook/         P10: a gym session closed from chat alone (LOG-*)
    capture.py     what was lifted, into a session that counts toward load
  runtime/         the wiring: what makes the phases into running processes
    models.py      the only place an Anthropic client is built, and the spend guard
    transport.py   the only place that talks to Telegram
    turn.py        one inbound message to one sent reply
    agent.py       the conversational process (coach-agent)
    scheduler.py   the nightly process: consolidation, decay, export (coach-scheduler)
  observe/         P15: reading a model call back after the fact (OBS-10 to OBS-14)
    transcript.py  what was sent and what came back, as an exchange (coach-transcript)
tests/             the acceptance criteria, as assertions
```

## Development

Tests run against a real Postgres, because the invariants under test are
database invariants — a partial unique index, a foreign key, a check
constraint, transaction rollback. A mocked store would test nothing that
matters.

```bash
uv sync --extra dev
./scripts/dev-db.sh start      # prints TEST_DATABASE_URL; export it
uv run pytest -q               # 802 passing
./scripts/dev-db.sh stop
```

`TEST_DATABASE_URL` overrides the connection if you would rather point at your
own instance.

## What to know before reading the code

Five rules run through every phase. They are here rather than in the design
document because they explain why the code is shaped as it is, and each one was
paid for at least once.

**The system may reduce load autonomously and may never increase it.** Every
requirement touching prescriptions inherits this. It is why `ADJ-02` rejects any
generated change that raises computed weekly load.

**The coach is given permissions, not numbers.** Body mass is the clearest case:
the readings never enter the prompt, only a slope fitted in SQL and an explicit
list of what the evidence supports. A model handed readings will compare two of
them, which HLTH-09 forbids and which no amount of instruction reliably prevents.

**The platform's opinion sits beside our arithmetic, never inside it.** FIT-03
says intervals.icu's derived fields are stored alongside parsed values and never
substituted for them, and the same rule governs recovery: the deviation is built
from measured signals, and Whoop's readiness score is shown next to it rather
than summed into it.

**A rule that cannot be verified fails closed.** P07's constraint gate refuses to
generate a gym session when a constraint phrase matches nothing it understands,
rather than programming as though the athlete were unconstrained. That is the
governing asymmetry applied to ambiguity: the system fails toward less.

**Safety facts are not probabilistic.** Injury and medical constraints load
verbatim into every prompt, never decay, and can only be written by the athlete
path in `facts.state_constraint` (SAFE-06). Consolidation cannot touch them;
attempting it is recorded as a rejected fact with its audit row.
