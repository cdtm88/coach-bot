"""The wiring that makes the merged phases into a running system.

Eight phases were merged before this package existed and none of them was
wrong: their requirements are behavioural, and they are tested against injected
clients and transports, which is the right way to test behaviour. What no phase
asked for was the concrete thing at each seam. Three were missing, and this is
where they live:

* :mod:`coach.runtime.models` constructs the Anthropic client. It is the only
  place in the codebase that does, so the spend guard below has nowhere to route
  around.
* :mod:`coach.runtime.transport` talks to Telegram. It is the only place that
  does, and it holds no database connection and no model client — it moves
  strings, and everything that decides anything is elsewhere.
* :mod:`coach.runtime.turn` is the loop from an inbound message to a sent reply:
  assemble the prompt, claim an interruption, call the model, run tools, check
  the reply, record it.
* :mod:`coach.runtime.agent` and :mod:`coach.runtime.scheduler` are the two
  processes, alongside the existing `coach-ingest`.

**Nothing here decides anything a requirement covers.** Every rule already has a
home — the interruption budget is `agent.interruptions`, the naturalness checks
are `agent.naturalness`, the conflict matrix is `consolidation.conflict`. This
package's job is to call them in the right order and to have no opinions of its
own. Where it looks like it has one, that is a bug.
"""
