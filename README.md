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
| [`docs/intervals-api.md`](docs/intervals-api.md) | What the intervals.icu API actually does, verified, with dates |
| [`docs/prd-review.md`](docs/prd-review.md) | Record of the v2.1 review and what it changed |

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
Fix the losing document in the same change (SPEC-02).

## Status

**P00 to P04 are built.** The memory store and its invariants, the
conversational agent, nightly consolidation, activity ingest with session
reviews, and now macros from MacroLog with the body mass trend read from
intervals.icu wellness. 333 tests, all against a real Postgres.

The live checks that gated P04 were run on 28 July 2026 and the headline is that
**the wellness feed carries no weight at all.** Nothing feeds body mass until
MacroLog's HealthBridge writes it, which is the athlete's to build and outside
this repository. So the trend pipeline is complete, tested and empty: every
threshold is asserted on seeded data and the empty series is the first case in
the suite, so the first real reading starts a trend with no code change.

**P05 is next** and is now small — P04 stored every wellness field while it was
reading the feed, so what remains is the recovery deviation and the missing
session cross check.

Ingest needs no webhook. Zwift rides arrive through a watched folder with no API
call at all; everything else arrives on a poll whose interval is configurable.
The webhook receiver is built and idle because registering an app requires a
person at intervals.icu to approve one, and that was not worth blocking on.

Later phases are in `docs/prd.md` section 4.

## Layout

```
migrations/        numbered SQL, applied on boot
prompts/persona.md the coach's voice, written from docs/seed/
seeds/athlete.json the initial facts, each traced to the source transcript
scripts/
  verify_intervals.py  the live-account checks; V2 and V3a run, V1 gates P08
  dev-db.sh            throwaway Postgres for the suite
src/coach/
  config.py        environment only; no credential defaults (SEC-01)
  clock.py         local day and week boundaries (TZ-01/02/03)
  db.py            connections
  migrate.py       the boot-time runner
  seed.py          one-time memory seed from seeds/athlete.json
  memory/
    keys.py        controlled vocabulary and value typing (MEM-01, MEM-14)
    facts.py       supersession, provenance, audit, decay
    notes.py       episodic archive with full text search (MEM-07)
    state.py       working memory and the pending queue (MEM-09, CONS-06)
    context.py     per turn assembly and the shedding order (MEM-10/11/13)
    export.py      the nightly markdown fact export (MEM-12)
  ingest/
    parse.py       samples in, computed values out; never a derived aggregate
    client.py      the intervals.icu API, basic auth, rate limit headers
    activities.py  an upstream activity to a session row
    archive.py     the permanent local FIT archive; contains no delete (FIT-15)
    review.py      prescription matching, compliance, reviews, missed sessions
    reconcile.py   the poll and the bulk backfill
    service.py     the pipeline every ingest path calls
    webhook.py     the receiver and its delivery queue; idle without an app
    server.py      the process: routes, poll, wellness, sweep, queue worker
  health/
    macros.py      per-meal macros from MacroLog, idempotent on the meal id
    wellness.py    the wellness read: body mass, and P05's recovery fields
    bodymass.py    readings, the outlier gate, the gap, the rollup
    trend.py       the weighted fit in SQL, and what it permits the coach to say
    breaks.py      is today inside a break; the rest of BREAK-* lands in P10
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
uv run pytest -q               # 333 passing
./scripts/dev-db.sh stop
```

`TEST_DATABASE_URL` overrides the connection if you would rather point at your
own instance.

## Two rules worth knowing before reading the code

**The system may reduce load autonomously and may never increase it.** Every
requirement touching prescriptions inherits this. It is why `ADJ-02` rejects any
generated change that raises computed weekly load.

**The coach is given permissions, not numbers.** Body mass is the clearest case:
the readings never enter the prompt, only a slope fitted in SQL and an explicit
list of what the evidence supports. A model handed readings will compare two of
them, which HLTH-09 forbids and which no amount of instruction reliably prevents.

**Safety facts are not probabilistic.** Injury and medical constraints load
verbatim into every prompt, never decay, and can only be written by the athlete
path in `facts.state_constraint` (SAFE-06). Consolidation cannot touch them;
attempting it is recorded as a rejected fact with its audit row.
