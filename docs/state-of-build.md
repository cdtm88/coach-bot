# State of the build

> Read this first in a new session. It says what is done, what is next, and what
> the environment does that will otherwise waste your time.
>
> Last updated 28 July 2026, at `main` after PR #9 and the P04 branch.

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
| P04 | Macros from MacroLog, body mass from wellness (HLTH-01 to HLTH-16) | built |

333 tests, all against a real Postgres. Schema is at migration 007.

**Next phase is P05**, and it is now small. P04 read the wellness feed for body
mass and stored every field on it while it was there, which landed RECOV-01,
RECOV-02 and RECOV-05. What is left is RECOV-04, the recovery deviation against
the athlete's own 28 day baseline, and RECOV-06, using recovery and load to
disambiguate a missing session. Nothing blocks it.

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

## What the live checks found

V2 and V3a were run on 28 July 2026. Full results are in
`docs/intervals-api.md`; the two that change what gets built:

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

Not urgent. P08 is a long way off.

## Open items

| # | Item | Blocks | State |
| --- | --- | --- | --- |
| 1 | Where does the weight in intervals.icu come from? | — | resolved: nowhere, the field is absent |
| 2 | Who builds HealthBridge, is it needed? | P04 validation | resolved: yes, needed, and it is the athlete's |
| 3 | Which wellness fields does the Whoop link populate? | — | resolved: six of seven, `hrvSDNN` never |
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
scripts/verify_intervals.py   the three checks above
scripts/dev-db.sh        throwaway Postgres for the suite
```

On conflict: the design wins on schema and memory semantics, the PRD wins on
scope and acceptance, the setup guide wins on credentials and infrastructure.
