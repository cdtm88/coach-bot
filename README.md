# coach-bot

AI Cycling Coach · single-user Telegram coaching agent

## Documents

| Document | Authoritative on |
| --- | --- |
| [`docs/prd.md`](docs/prd.md) | Scope, requirements, acceptance, phase completion |
| [`docs/memory-design.md`](docs/memory-design.md) | Memory tiers, schema, key namespace, provenance, conflict matrix |
| [`docs/setup.md`](docs/setup.md) | Accounts, credentials, tunnel, infrastructure |
| [`docs/prd-review.md`](docs/prd-review.md) | Record of the v2.1 review and what it changed |

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
Fix the losing document in the same change (SPEC-02).

## Status

**P00, memory store and invariants.** Facts can be written, superseded and
audited; the invariants are enforced by the database rather than by convention.

Later phases are in `docs/prd.md` section 4.

## Layout

```
migrations/        numbered SQL, applied on boot
prompts/persona.md the coach's voice, written from docs/seed/
seeds/athlete.json the initial facts, each traced to the source transcript
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
tests/             the P00 acceptance criteria, as assertions
```

## Development

Tests run against a real Postgres, because the invariants under test are
database invariants — a partial unique index, a foreign key, a check
constraint, transaction rollback. A mocked store would test nothing that
matters.

```bash
uv venv && uv pip install -e ".[dev]"
./scripts/dev-db.sh start
.venv/bin/python -m pytest
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
