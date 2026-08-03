# Every completed ride left its prescription open

**Found:** 3 August 2026, while wiring P09 on instruction. Not by a failing test
and not by a symptom anyone reported: the visible effect was a training plan in
which nothing was ever ticked off, which reads as an athlete who is not
following it.

**Cause:** `review.match` and `review.attach` had two callers in the whole of
`src/`. One was `ingest.service.on_activity`, whose only caller is the webhook
drain, which is built, tested and idle. The other was `logbook.capture`, the
chat path for gym and golf. The live poll path called `review.review` alone. So
a ride that arrived through the poll or the watched folder — which is every ride
on the running deployment — was reviewed but never matched.

Consequences, in order of how long they would have taken to notice: sessions
kept a null `prescription_id`; prescriptions stayed `planned` for ever;
compliance was never computed or frozen; the Sunday review's adherence had
nothing to report; and every P09 rule would have read a compliance figure that
did not exist, had P09 been running, which it was not.

**Fix:** `ingest.service.finish`, one shared tail doing match, freeze, review
and adjust in that order, called by both ingest paths. `tests/test_p09_wiring.py`.

**The backlog:** `coach-reconcile` (`ingest/backfill.py`), a one-off. The fix
closes the loop from the next ride onward and can do nothing for the rides
already past, because `poll` only considers sessions with `reviewed_at is null`
and every affected session was reviewed. Dry run by default; `--apply` writes.
Deletable once it has been run.

## Eliminated

- **The matcher is too strict.** The obvious first theory, and it has a real
  basis: `match` requires an *unmatched* prescription and prefers the platform's
  `paired_event_id`, so a Zwift file arriving through the watched folder has
  only the date-and-discipline fallback to go on. Ruled out by calling `match`
  directly against a seeded session and prescription, which returned the right
  id immediately. The matcher was never reached.

- **The sweep is closing them as missed before the poll can match them.** Also
  plausible: the FIT-12 grace window and the poll interval are unrelated
  clocks, and a race there would look exactly like this. Ruled out by reading
  `review.missed`, which has an explicit guard — a prescription with any session
  on the same day is reported "unmatched rather than missed". The sweep was not
  the cause; it was the reason nobody noticed, because it kept the rows in a
  state that looks deliberate.

- **`reconcile.run` matches and something later unmatches them.** Ruled out by
  grepping every writer of `sessions.prescription_id`: there is exactly one, in
  `review.attach`. Nothing unsets it because nothing sets it.

- **It only affects the watched folder, not the API poll.** Worth checking
  because the two arrive by different routes and only one involves a network
  call. Ruled out by reading `service.poll`: both routes converge on the same
  `_unreviewed` loop, and that loop was the thing missing three of its four
  steps. Both were equally affected.

## What made it hard to see

**The failure state is indistinguishable from a true one.** An open prescription
with a session on the same day is exactly what the database looks like when the
athlete rode something other than what was planned. There is no error, no log
line, and no invariant broken. Every phase-level test passed because each
function did its own job correctly.

**Two paths, one of them dormant, and the complete one was the dormant one.**
`on_activity` does all four steps and reads as the canonical ingest path. It is
the one you find first, it is the one the tests exercise, and it is the one that
does not run. The path that runs is shorter and looks like a subset on purpose,
so nothing about reading it suggests something is missing.

**The phase that would have caught it was itself unreachable.** P09 reads the
compliance this bug prevents from existing, so a working P09 would have surfaced
it within a week. Both defects have the same shape and they hid each other:
looking for the caller of `adjust.pass_.run` is what led here.
