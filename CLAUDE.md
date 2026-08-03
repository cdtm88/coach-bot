# Working in this repository

Short by design. The five rules at the foot of `README.md` explain why the code
is shaped as it is; this is what will otherwise waste an hour or ship a defect.

`docs/prior-art.md` records what three archived attempts cost to learn. The
most transferable part of it is the negative case: `training-tracker`'s
`AGENTS.md` earned its keep because for every integration point it said what
*not* to do, and that is what most of this file is.

## Read first

`docs/state-of-build.md` says what is done, what is next, and what the
environment does that will otherwise cost you. Read it before anything else.

On conflict: `docs/memory-design.md` wins on schema and memory semantics,
`docs/prd.md` wins on scope and acceptance, `docs/setup.md` wins on credentials
and infrastructure. `docs/prd.md` section 5 is the only open items register.

## The one that keeps happening

**Name the real caller, not just the test.** Five times now a phase has been
built, tested green, and wired to nothing:

- eight phases merged with nothing constructing an `anthropic.Anthropic` and
  nothing calling `api.telegram.org`
- `plans.publish` had no caller, so prescriptions never reached the calendar
- `record_reply` had one caller, so the coach never heard its own morning
  message
- `blocks.generate.rewrite_from` implements BLOCK-08 and nothing calls it
- `adjust.pass_.run` needed `adjust=True`, which appeared nowhere in `src/`, so
  P09 never ran. Fixed 3 August 2026, and underneath it: `review.match` had no
  caller on the live path either, so **no ride was ever matched to its
  prescription** on the running deployment

A unit test proves a function works. It cannot prove anything reaches it. When
you add an entry point, a hook or a flag, grep for its caller in `src/` and say
in the PR what that caller is. `tests/test_adjust.py:894` asserts the `adjust`
flag exists and defaults to False, which is a test that the switch is installed,
not that anything turns it on; `tests/test_p09_wiring.py` is the other half.

**The tell is a symptom that reads as normal.** The sweep reported those
prescriptions "unmatched rather than missed", which is correct behaviour and
looks like a plan nobody is following. Nothing errored, nothing was logged, and
no test failed. When a phase's output is a *status quo* rather than an error,
assert the caller exists.

## Do not

**Do not put a number the model produced into storage.** Aggregates are
recomputed from atomic fields. `write_session_events` computes `planned_load`
itself for this reason.

**Do not let a model claim `computed` provenance.** `conflict.MEASURED` counts
computed as a measurement, so it would promote an inference over a real reading.
`conflict.MODEL_PROVENANCE` is the allowed set, at every door.

**Do not add a threshold without testing both of its boundaries.** A plateau
rule here was dead because a 28 day window spans 27 days, and its mirror in
`pace` always fired. `test_zones.py` checks every band at both ends.

**Do not put readings in the prompt where a claim will do.** Body mass enters as
a slope fitted in SQL plus a list of permitted claims. If you change one thing
in `health/trend.py`, do not change that.

**Do not scale a null.** `None` times 0.8 is `None`, not `0.0`. Null is unknown.

**Do not write a secret to the database.** CALR-06 keeps the iCal URLs out
entirely, because a secret in a column is a secret in the nightly `pg_dump`.
`model_call_payloads` is the newest place this could go wrong and has a test.

**Do not reword the athlete's own constraint to make it parse.** Widen the
matcher. `blocks/constraints.py` refuses to generate rather than proceed as
though he were unconstrained.

**Do not leave a failing test.** `pacer-ai` carried nine through half the
project and nobody could read the suite at a glance. Fix or delete, same day.

**Do not use `curl` against `api.github.com`.** It returns nothing through the
proxy. Use the `mcp__github__*` tools. This has cost time twice.

**Do not trust a cached CI read.** `get_workflow_run` has returned
`in_progress` for a run that finished an hour earlier. Pass `filter: latest` or
re-request before concluding anything about CI.

## Verifying

```bash
./scripts/dev-db.sh start     # Postgres dies on container churn; this brings it back
uv run pytest -q              # ~3 minutes, real Postgres, 961 tests
uv run ruff check . && uv run ruff format --check .
```

**Run the whole suite, not the file you changed.** The last two phases each had
a green new file and a broken one elsewhere: a test fake that did not accept a
new keyword, and a test that enumerates the console scripts.

The suite never reads `.env`. It uses `TEST_DATABASE_URL` and throwaway
databases, so it passes with no credentials at all. A test that needs a
credential has been written wrong.

## Writing it down

**Correct the losing document in the same change** (SPEC-02). When a
requirement stops describing the system, amend it and say so. Two claims in
`docs/prior-art.md` were wrong and were corrected in place, with the date and
the cause, rather than quietly rewritten.

**A resolved open item keeps its reasoning.** See `docs/prd.md` section 5:
each entry records what was decided, when, and what the fallback was.

**Decisions are cited from code.** `MEM-01`, `PLAN-05`, `OBS-12` in a docstring
means any line traces back to the requirement that produced it.

**The em dash rule is about the coach's voice, not yours.**
`prompts/persona.md` forbids them in what the athlete reads. Docstrings, docs
and commit messages use them freely and always have. Do not "fix" the codebase
for this.

**Never print a credential** — not in tool output, a commit, or a PR body.
`.env` is gitignored and stays that way.
