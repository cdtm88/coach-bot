"""P09: letting session data reshape the remaining week, within bounded authority.

ADJ-01 to ADJ-08. Three modules and the split is the point:

* :mod:`triggers` — what the data says happened, and what it suggests doing.
  Knows nothing about whether it is allowed.
* :mod:`authority` — whether that may happen autonomously. Knows nothing about
  cycling.
* :mod:`apply` — doing it, recording it, and deciding whether to say so.

**The governing asymmetry, and why it is a whole module.** Design section 10:
"Downgrades are automatic. Upgrades are not." Shortening, easing or moving a
session later fails safe. Adding load, adding sessions or raising intensity waits
for the Sunday review, "given a de-trained starting point and a spinal history"
— autonomous load increases are the one place this system could actually hurt the
athlete.

That is why :mod:`authority` exists rather than each rule policing itself. A rule
that both proposes and approves its own change is a rule where a mistake in the
proposal becomes a mistake in the training, and every new rule would be a fresh
chance to get the bound wrong. Here the bound is checked once, in code that has
never heard of intervals, against the load figure GYM-08 defines.

**The absence trap** is the other reason for the separation. A missing activity is
ambiguous — skipped, ridden outdoors, failed to sync, or a broken watcher — and
`coach.ingest.review.missed` already resolves it against the recovery and load
signal, shipping `safe_to_act` for exactly this. ADJ-08 is enforced in
:mod:`authority` and not in a rule, because "ask rather than act" is a statement
about authority rather than about what happened.
"""
