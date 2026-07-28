# State of the build

> Read this first in a new session. It says what is done, what is next, and what
> the environment does that will otherwise waste your time.
>
> Last updated 28 July 2026, at `main` after PR #8.

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

256 tests, all against a real Postgres. Schema is at migration 006.

**Next phase is P04** (macros from MacroLog, body mass from wellness). It is
blocked only on V3a below, which takes about a minute with a key.

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

## What the API key unblocks

The key is the only thing standing between here and P04. Three checks, all
scripted — do not rewrite them:

```bash
uv run python scripts/verify_intervals.py all    # V2 and V3a, both read only
```

### V3a — wellness read. Resolves open items 1 and 3. **Do this first.**

Reads the last 21 days and prints a table plus a verdict. It answers:

* **Open item 1**: does `weight` move day to day, or repeat? If it repeats it is
  a static profile field, which is actively harmful — the coach would anchor on a
  stale number and never notice. If it moves, something already feeds it.
* **Open item 2** falls out of that: HealthBridge is needed only if weight is
  static or absent.
* **Open item 3**: which of the six RECOV-02 fields the Whoop link actually
  populates. Any field that is always null is dropped from the deviation per
  RECOV-02, which is expected behaviour rather than a bug.

**P04 cannot be designed properly until this is run.** It is one read.

### V2 — file encoding. Read only, blocks nothing.

Fetches one activity file and reports the first two bytes and the
`Content-Type` / `Content-Encoding` headers. Settles whether the gzip is
transport encoding (httpx strips it) or payload (httpx does not). The sniff in
`parse.decompressed` is correct under both, so this only tells us which failure
the tests should simulate.

It also prints the **rate limit headroom**, which decides whether
`COACH_POLL_INTERVAL_S=120` is sane. At 120s the poll costs about 720 calls a
day. Nobody has seen the real numbers yet.

### V1 — external_id scoping. Writes, self-cleaning. Blocks P08 only.

```bash
uv run python scripts/verify_intervals.py v1
```

Creates a probe calendar event, reads it back, upserts it again, deletes it.
Answers whether `oauth_client_id` is populated under a personal API key — the
documented scoping rule is written for OAuth clients, and if it is null then
PLAN-05's orphan sweep must use an `external_id` prefix convention instead.

Not urgent. P08 is a long way off.

### V3b — the wellness write. **Ask before running this.**

```bash
uv run python scripts/verify_intervals.py v3 --write --date 2026-06-01 --lock
```

Tests whether a provider resync overwrites an API-written weight and whether
`locked: true` prevents it. This decides how MacroLog writes body mass.

**There is no documented API path to unlock a day afterwards.** The script
refuses to run without an explicit `--date` for that reason. Pick a day that does
not matter, and get the athlete's agreement first. The read half (V3a) is
risk-free and answers open item 1 on its own.

## Open items

| # | Item | Blocks | State |
| --- | --- | --- | --- |
| 1 | Where does the weight in intervals.icu come from? | P04 | **run V3a** |
| 2 | Who builds HealthBridge, is it needed? | P04 | falls out of item 1 |
| 3 | Which wellness fields does the Whoop link populate? | P05 | **run V3a** |
| 4 | Verify no activity gaps after the Strava disconnect | — | needs a key; low urgency now that the poll covers Strava-sourced rides |
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
amended when polling replaced the webhook rather than left quietly false. SPEC-02
requires fixing the losing document in the same change.

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
scripts/verify_intervals.py   the three checks above
scripts/dev-db.sh        throwaway Postgres for the suite
```

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
