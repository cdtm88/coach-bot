# The coach answered "yes, move it" with no record of what it had offered

**Found:** 3 August 2026, while tracing every outbound path in the codebase to
check a claim in `docs/prior-art.md` about delivery ledgers. Not by a failing
test, and no test could have failed: nothing asserted the property.

**Cause:** `telegram.bot.record_reply` had exactly one caller, inside
`bot.drain`. The scheduler's sender called `transport.Telegram.send` directly
with no database write, so the morning message, the evening follow-up and the
Sunday review were never written to `messages`. `runtime.turn._history` builds
the model's conversation from `messages`, so none of the three was ever in the
coach's own context.

**Fix:** PR #38. `coach.notify.outbox` is now the one door a proactive message
goes out through.

## Eliminated

- **The history query filters them out.** Plausible: `_history` has a
  deliberately load-bearing `or role = 'coach'` clause, added because replies
  are written with `processed_at` null and an earlier filter had hidden them.
  Ruled out by inserting a `role = 'coach'` row by hand and watching it appear.
  The query was right; there was nothing for it to return.

- **The scheduler is not running the jobs.** Plausible, because P09's rules
  turned out to be unreachable in exactly this way and the two were found in the
  same pass. Ruled out by `scheduled_runs`, which shows the jobs claiming their
  dates and succeeding. They ran. They just left no trace.

- **The messages are there but on the wrong chat id.** Plausible: `record_reply`
  reads `TELEGRAM_ALLOWED_CHAT_ID` with a default of `0`, so a missing variable
  would file them under a chat nobody reads. Ruled out by counting rows with no
  chat filter at all: there were none.

- **It is the known `scheduled_runs` retry window.** This was the hypothesis the
  investigation started from, taken from `docs/prior-art.md`, and it was a real
  but much smaller problem. Ruled out as the *cause* because it explains a
  duplicate message and not a missing row. Fixed in the same change, and the
  document was corrected for overstating it.

## What made it hard to see

**Every component worked.** The scheduler claimed correctly, the notification
functions returned the right text, the transport sent it, and the athlete
received it. The defect lived in the seam, and the seam was nobody's phase.
This is the fourth instance in this project of a phase that is built, tested and
wired to nothing; `docs/state-of-build.md` lists the others.

**The absence was invisible from both ends.** From the scheduler's side a
message was sent. From the conversation's side the athlete had simply replied
to nothing in particular, which reads as him changing the subject rather than
as a fault.

**The test that would have caught it is a property test, not a unit test.** No
test of `morning_job` could see it, because the function did exactly what it
said. The assertion that had to exist is "a proactive message appears in the
next turn's history", which spans two modules that no single test file owned.
`tests/test_outbox.py` now states it in those terms.
