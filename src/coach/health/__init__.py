"""P04: nutrition intake and body mass.

Two feeds and one gate.

* :mod:`coach.health.macros` stores what MacroLog posts, at per-meal
  granularity (HLTH-01 to HLTH-03).
* :mod:`coach.health.wellness` reads the intervals.icu wellness feed, which is
  where body mass arrives from (HLTH-04) and where P05's recovery fields will be
  read from.
* :mod:`coach.health.trend` fits the 28 day weight trend in SQL and decides what
  the coach is permitted to claim from it (HLTH-06 to HLTH-10, HLTH-16).

The gate is the part worth understanding. Every threshold in the PRD's weight
trend confidence table is computed, stored on the rollup and rendered into the
prompt as a permission rather than left to the model's judgement — because a
model that has the numbers will state a direction from two readings, and
HLTH-07 says it may not.
"""
