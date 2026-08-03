# A FIT file with no samples could mean two opposite things

**Found:** 3 August 2026, from a log line nobody had cause to read. Two files —
`2026-07-12-14-12-18.fit` and `2026-07-21-17-02-17.fit` — warned "FIT file
contained no record messages" on every poll pass since 12 and 21 July.

**They were not lost rides, and the first version of this fix was wrong because
it assumed they were.** Dumping the two files' frames settled it:

| file | size | records | `session.start_time` | timer |
| --- | --- | --- | --- | --- |
| `2026-07-12-14-12-18.fit` | 1533 B | 0 | none | 0.0 s |
| `2026-07-12-17-02-02.fit` | 201 KB | 1815 | 13:02:52 | 1815 s |
| `2026-07-21-17-02-17.fit` | 1533 B | 0 | none | 0.0 s |
| `2026-07-21-17-02-24.fit` | 479 KB | 4339 | 13:14:30 | 4339 s |

Both stubs are Zwift abandoned starts: `file_id.type` is `activity`,
`manufacturer` is `zwift`, there is a session, a lap and an activity message,
and every number in all three is zero. On 21 July the real ride's file is
written **seven seconds later**. Nothing was ever missing; both days' rides
ingested normally.

**Cause.** Two things, and the second is what the first concealed.

*Nothing could tell an abandoned start from a lost ride.* Both arrive as a FIT
file with no record messages, and `parse.from_fit` raised the same
`UnparseableActivity` for either. That mattered in both directions. Migration
015 had already settled what a ride whose data is missing is worth — the row
stays, flagged `data_unavailable`, because FIT-12 counts sessions on a day
before concluding a prescription was skipped — and `archive.ingest_file` decided
the same question the opposite way, in a different module, by dropping the file.
A genuinely truncated ride would have been silently discarded. None has happened
yet, so this half was a latent defect rather than a live one.

*And the warning repeated for ever.* `scan` walks the folder every poll;
`keep_local` recognised the content hash but `ingest_file`'s early return needs
a `session_id`, which a file yielding no session never gets. So every pass
re-read, re-parsed, re-failed and re-warned on bytes that cannot change. This
half was live for three weeks, and it is the reason nobody looked: a warning
that repeats indefinitely is one that stops being read.

**Fix:** `parse.from_fit` reads `file_id.type` and the `session` message, which
survive when the record stream does not, and a record-less file now resolves
four ways instead of one — not an activity at all (`NotAnActivityFile`), an
activity that never ran (`AbandonedActivity`), an activity whose samples were
lost (`samples_missing` → a `data_unavailable` session), or undateable, which
stays unparseable because FIT-10 forbids the ingest clock.
`fit_archive.unreadable_reason` records the judgement so the second pass is
silent, and `poll` reports the standing count because the log deliberately goes
quiet. `tests/test_recordless_fit.py`.

**A hole this opened and closed in the same change.** `review.match` had no
guard against `data_unavailable`, so a lost ride that named its sport would have
claimed the day's prescription and `attach` would have frozen
`compliance: {completed: true}` with no deltas — indistinguishable from a session
that hit its target. `review.missed` already states the rule in prose ("not
missed, and not matched either: nothing here can say whether it was the
prescribed work") and had no code for it. It was unreachable while only the
upstream path set the flag, because a placeholder has no type and so gets a
discipline that matches nothing anyway.

## Eliminated

- **They are corrupt files.** The obvious reading of "contained no record
  messages", and wrong in a way that matters: fitdecode parsed both without
  complaint, and a genuinely corrupt file raises out of the reader onto the
  *other* branch, `not a readable FIT file`. These were well-formed files
  missing one message type, which is a different problem with a different fix.

- **They are the athlete's rides and the samples were lost.** Held for about an
  hour, and the first implementation was built on it. Killed by the frame dump:
  a truncated ride would carry a `start_time` and a substantial timer, and these
  carry neither, and each has a fully recorded sibling minutes away. Had it
  shipped, the two days would have gained `data_unavailable` sessions asserting
  the athlete trained — on days he was already credited for — and each would
  have suppressed FIT-12's missed check and inflated the weekly review's "no
  usable file" count. **The failure mode of the fix was worse than the defect.**

- **Date them by `file_id.time_created`.** The obvious way to place a file whose
  session says nothing, and it is exactly what makes the above happen: Zwift
  stamps `time_created` on abandoned starts too. It says a file was made, not
  that a ride happened. Not read at all now, and there is a test for that.

- **The parser should fall back to the device's own totals.** `session` carries
  `total_elapsed_time`, distance and an average power, so a file with no records
  still has numbers in it. Rejected on FIT-03 and on `review.weekly`, which
  promises that an unreadable session's time and distance are *short by whatever
  those were*. The timer is read to decide whether a ride occurred, which writes
  nothing; storing it as the ride's duration is the thing FIT-03 forbids.

- **Ignore them: two files in three weeks is noise.** What the previous
  behaviour amounted to, and it is stable only by luck. The cost is not two
  rides — it is that the next truncated file is silently absent and the failure
  state is a rest day.

- **Delete the files so the warning stops.** Named only to rule out: FIT-15 says
  the archive is never pruned, and a file that cannot be read today is still the
  only copy of whatever it holds.

## What made it hard to see

**The symptom was in the wrong register.** A `log.warning` in a poller is where
an operator looks when something is already suspected. Nothing raised, no test
failed, no invariant broke.

**Two opposite defects produce the identical log line.** A settings file, an
abandoned start and a truncated ride all read as "contained no record messages",
and only one of them is a missing activity. No amount of reading the log
distinguishes them, which is why the fix had to start by parsing the files
rather than by reasoning about them.

**The correct reasoning existed and was out of reach.** Everything needed for
the truncated-ride case was already built — the flag, the index, the weekly
review's separate count, the missed check's branch, the coach's prompt block.
The defect was that one of the two ingest paths could not produce the row they
all read.

**A plausible diagnosis is not a diagnosis.** "Two files with no records, two
missing rides" fits every fact that was visible from the repository, and it is
wrong. What settled it was thirty seconds of reading the actual bytes on the
actual box, and nothing short of that would have.
