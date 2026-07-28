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

**P00 to P03 are merged.** The memory store and its invariants, the
conversational agent, nightly consolidation, and activity ingest with session
reviews. 256 tests, all against a real Postgres.

**P04 is next** and is blocked on one read against a live intervals.icu key:

```bash
uv run python scripts/verify_intervals.py all    # read only
```

That answers where body mass comes from, which decides how much of P04 exists.
See `docs/state-of-build.md` for what to do with the answer.

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
  verify_intervals.py  the three live-account checks that gate P04 and P08
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
    server.py      the process: route, poll loop, sweep loop, queue worker
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
uv run pytest -q               # 256 passing
./scripts/dev-db.sh stop
```

`TEST_DATABASE_URL` overrides the connection if you would rather point at your
own instance.

## Two rules worth knowing before reading the code

**The system may reduce load autonomously and may never increase it.** Every
requirement touching prescriptions inherits this. It is why `ADJ-02` rejects any
generated change that raises computed weekly load.

**Safety facts are not probabilistic.** Injury and medical constraints load
verbatim into every prompt, never decay, and can only be written by the athlete
path in `facts.state_constraint` (SAFE-06). Consolidation cannot touch them;
attempting it is recorded as a rejected fact with its audit row.
