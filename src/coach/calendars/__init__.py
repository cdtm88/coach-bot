"""P06: read only calendar feeds.

CALR-01 to CALR-06. Two modules:

* :mod:`coach.calendars.feed` fetches the secret iCal URLs, expands recurrence
  into occurrences, decides which of them actually block time, and stores them.
* :mod:`coach.calendars.availability` turns those blocks into observed
  availability facts, and into the context the coach reasons from.

Named `calendars` rather than `calendar` on purpose: the standard library owns
that name, and `icalendar` and `dateutil` both import it.

**Nothing here can write to Google.** PLAN-08 requires that, and it is delivered
by there being no write path rather than by a check — the client issues `GET` and
the module exposes no function that takes an event and a destination. A test
scans for one anyway, because "we did not write that code" is a weaker guarantee
than "the code cannot be written without the test failing".
"""
