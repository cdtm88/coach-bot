"""P07: training blocks, gym programming, and the constraint gate.

BLOCK-01 to BLOCK-08, GYM-01 to GYM-08, SAFE-04. The first phase that writes.

Four modules, in the order a prescription passes through them:

* :mod:`coach.blocks.constraints` decides what the athlete may not be asked to
  do. Everything else defers to it.
* :mod:`coach.blocks.library` picks the movement, and substitutes when the first
  choice is blocked or unavailable (GYM-03).
* :mod:`coach.blocks.load` puts cycling and gym on one scale (GYM-08) and holds
  the weekly ceiling (BLOCK-07).
* :mod:`coach.blocks.document` versions the block markdown (BLOCK-01, BLOCK-02),
  and :mod:`coach.blocks.generate` produces the prescriptions.

**The governing asymmetry starts mattering here.** Up to P06 the system only
read; from here it prescribes, and the PRD's rule is that it may reduce load
autonomously and may never increase it autonomously. Two consequences run
through this package: a validation that cannot be completed fails closed rather
than open, and a load ceiling is checked before a prescription is stored rather
than after.
"""
