# Two rides were dropped because their files had no samples

**Found:** 3 August 2026, from a log line nobody had cause to read. Two files —
`2026-07-12-14-12-18.fit` and `2026-07-21-17-02-17.fit` — warned "FIT file
contained no record messages" on every poll pass since 12 and 21 July. Neither
activity was in the system.

**Cause, in two parts.**

*The rides were thrown away.* `archive.ingest_file` caught `UnparseableActivity`,
logged a warning and returned None. Migration 015 had already settled what to do
with an activity the system cannot describe, and settled it the other way: the
row stays, flagged `data_unavailable`, because FIT-12 counts sessions on a day
before concluding a prescription was skipped, so deleting the only evidence that
the athlete trained is what turns a session he did into one he is told he missed.
That reasoning was written down, tested, and reachable only from the upstream
path. The watched folder made the opposite decision in a different module.

*And the warning repeated for ever.* `scan` walks the folder every poll;
`keep_local` recognised the content hash but `ingest_file`'s early return needed
a `session_id`, which an unparseable file never gets. So each pass re-read,
re-parsed, re-failed and re-warned on bytes that cannot change. A warning that
repeats indefinitely is one that stops being read, which is why three weeks
passed.

**Fix:** `parse.from_fit` reads `file_id` and `session`, which every writer emits
and which survive when the record stream does not. A file with no records now
resolves three ways instead of one: a non-activity (`NotAnActivityFile` — a
settings, workout or course file, a non-event); an activity carrying a start time
(`samples_missing`, which becomes a `data_unavailable` session); or genuinely
undateable, which is still unparseable because FIT-10 forbids dating it by the
ingest clock. `fit_archive.unreadable_reason` records the judgement so the second
pass is silent. `tests/test_recordless_fit.py`.

**A hole this opened, closed in the same change.** `review.match` had no guard
against `data_unavailable`, so a lost ride that named its sport would have
claimed the day's prescription and `attach` would have frozen
`compliance: {completed: true}` with no deltas — indistinguishable from a session
that hit its target. `review.missed` already states the rule in prose ("not
missed, and not matched either: nothing here can say whether it was the
prescribed work") and had no code for it. It was unreachable while only the
upstream path set the flag, because a placeholder has no type and so gets a
discipline that matches nothing anyway.

## Eliminated

- **The files are corrupt.** The obvious reading of "contained no record
  messages", and it is wrong in a way that matters: fitdecode parsed both
  headers without complaint, and a genuinely corrupt file raises out of the
  reader and lands on the *other* branch, `not a readable FIT file`. These were
  well-formed files that were missing one message type. Corruption would have
  meant recovering the files; this meant reading the part that survived.

- **The parser should fall back to the device's own totals.** `session` carries
  `total_elapsed_time` and an average power, so a file with no records still has
  numbers in it. Rejected on FIT-03 and on `review.weekly`, which promises that
  an unreadable session's time and distance are *short by whatever those were*.
  Sourcing the duration from a summary field would make that sentence false and
  put a device-derived aggregate in a column that means "computed from samples".
  The row records that a ride happened and when. It says nothing else, which is
  the true thing to say.

- **Ignore them: two files in three weeks is noise.** This is what the previous
  behaviour amounted to and it is the thing migration 015 argues against at
  length. It is also not stable — the cost is not two rides, it is that every
  future truncated file is silently absent, and the failure state is a rest day.

- **Re-download them from intervals.icu.** Would have worked for these two if
  the platform had them, and fixes nothing: FIT-14 exists precisely so the
  watched folder works when the platform is unreachable or the integration is
  gone. A repair that requires upstream is not a repair of this path.

- **Delete the files so the warning stops.** Considered only to be named: FIT-15
  says the archive is never pruned, and these are the only surviving copies of
  those two activities. A better parser makes them readable later, which is
  exactly what happened.

## What made it hard to see

**The symptom was in the wrong register.** A `log.warning` in a poller is where
an operator looks when something is already suspected. Nothing raised, no test
failed, no invariant broke, and the athlete's plan showed two ordinary rest days
in July.

**The correct reasoning existed and was out of reach.** Everything needed to
handle this was already built — the flag, the index, the weekly review's separate
count, the missed-check's branch, the coach's prompt block asking about it. The
defect was that one of the two ingest paths could not produce the row they all
read. This is the fourth time in this repository that a decision has been made
twice in two modules; it is the first time the two answers were opposites rather
than drifting variants.

**One file's absence looks like the other file's absence.** A settings file and a
truncated ride produce the same exception and the same log line, and only one of
them is a missing activity. Until the parser could tell them apart, no amount of
reading the log would have distinguished "the device left junk in the folder"
from "a ride is gone".
