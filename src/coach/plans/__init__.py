"""P08: publishing prescriptions upstream, and noticing when the athlete edits.

PLAN-01 to PLAN-12. The first phase that writes to a system the athlete also
writes to, which is the whole of its difficulty: everything here has to survive
the calendar being edited underneath it.

Four modules, and the split is by direction rather than by requirement:

* :mod:`events` — a prescription as an upstream event. Shape only, no I/O.
* :mod:`workout` — the native workout text for a structured session (PLAN-09).
* :mod:`publish` — outbound. Upsert, and never into busy time (PLAN-01, PLAN-04).
* :mod:`sync` — inbound. Athlete edits back into the local row (PLAN-06, PLAN-12).
* :mod:`sweep` — outbound deletion. Orphans, on the nightly pass (PLAN-05).

**The one thing V1 changed.** The upstream documentation says `external_id`
matching and the `oauth_client_id` filter both apply to "events created by your
application", and a personal API key has no application: V1 found
`oauth_client_id` null on everything the coach creates, with `created_by_id` set
to the athlete's own id. So there is no way to ask upstream "which of these are
mine". Recognising our own events is done by the `external_id` pattern in
:mod:`events`, and it is the reason that pattern is precise rather than a loose
prefix — a sweep that deletes is not the place for a generous match.
"""
