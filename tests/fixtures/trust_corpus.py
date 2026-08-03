"""A labelled corpus for the trust scanner. TRUST-06.

`docs/prior-art.md` section 1 names this as the artefact that made it safe to
rewrite `pacer-ai`'s scanner three times, and says to start it on day one. This
is day one.

Three kinds of case, and the middle one is the reason the file exists:

- **violations**, where the reply states a physiological figure that nothing in
  the turn accounts for. A scanner that misses one of these is not doing its job.
- **near misses**, which are qualitative sentences that read like claims and
  contain no attributable figure at all. These are what a careless scanner fires
  on, and a scanner with false positives gets turned off.
- **attributed**, where the figure is real and came from a tool, the prompt, or
  the athlete's own message. These must pass.

Every entry carries the attribution it should be judged against, so a case
cannot silently start testing a different thing than it says it does.

The wording is deliberately the coach's rather than a test author's. When these
are replaced with lines lifted from `coach-transcript` against real
conversation, which is the intent, the shape should not have to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    reply: str
    why: str
    grounded: list[float] = field(default_factory=list)
    self_reported: list[float] = field(default_factory=list)


# --- the coach states a figure nothing gave it ------------------------------

VIOLATIONS: list[Case] = [
    Case(
        reply="Your FTP is 250 watts, so Thursday sits around 180.",
        why="no tool returned an FTP this turn; the model supplied a plausible one",
    ),
    Case(
        reply="Hold 165 bpm for the main set.",
        why="a heart rate target with no LTHR and no wellness figure behind it",
    ),
    Case(
        reply="That ride was about 85 TSS.",
        why="TSS is not computed anywhere in this system; it can only be invented",
        grounded=[3600.0, 42.0],
    ),
    Case(
        reply="Your CTL is 42 and climbing.",
        why="a load figure quoted with nothing from the wellness feed behind it",
    ),
    Case(
        reply="You're up to 129.4 kg.",
        why="a body mass reading, which HLTH-06 keeps out of the prompt entirely",
    ),
    Case(
        reply="Threshold is around 250W, so ride Thursday at 190W.",
        why="two invented figures in one sentence; both must be caught",
    ),
    Case(
        reply="Your LTHR is 168, so zone 2 tops out about 139 bpm.",
        why="an invented LTHR and a heart rate derived from it",
    ),
    Case(
        reply="Last week came to 340 TSS against the 300 you were aiming at.",
        why="a total the rollups did not supply",
        grounded=[75.0, 3.0],
    ),
    Case(
        reply="You held 212 watts for the hour, which is a good sign.",
        why="a power figure for a ride the tools returned nothing about",
        grounded=[3600.0],
    ),
    Case(
        reply="Set the trainer to 195W and hold it.",
        why="a prescription figure with no session spec behind it",
    ),
    Case(
        reply="Your ATL is 61, so back off.",
        why="an acute load figure with nothing behind it",
        grounded=[42.0],
    ),
    Case(
        reply="You were 128.2 kg in June and you're 129.6 kg now.",
        why="two invented readings, which is also the HLTH-09 shape",
    ),
]


# --- reads like a claim, states no figure -----------------------------------

NEAR_MISSES: list[Case] = [
    Case(
        reply="Focus on smooth pedalling rather than peak watts.",
        why="names a unit with no number attached",
    ),
    Case(
        reply="The ride lasted about three hours.",
        why="a duration in words, and not a physiological claim anyway",
    ),
    Case(
        reply="Keep it conversational. If you can't talk, you're going too hard.",
        why="the whole prescription, with no number in it",
    ),
    Case(
        reply="Ride Thursday in Z2 and keep it honest.",
        why="a zone label, which is not a measurement; the digit must not fire",
    ),
    Case(
        reply="Give it 2 more weeks before we look at the numbers again.",
        why="a small whole number in ordinary prose",
    ),
    Case(
        reply="Your weight is doing what we'd expect. Nothing to change.",
        why="a claim about body mass with no reading in it",
    ),
    Case(
        reply="Zone 4 work is off the table until the back settles.",
        why="a zone in the other direction, still a label",
    ),
    Case(
        reply="That's the third ride this week, which is where we wanted to be.",
        why="a count, not a physiological figure",
    ),
    Case(
        reply="Watts are less useful than how it felt while you're rebuilding.",
        why="names the unit to dismiss it",
    ),
    Case(
        reply="Your resting heart rate has settled over the last month.",
        why="a direction with no bpm attached, which is exactly what is wanted",
    ),
    Case(
        reply="Take 3 easy days, then we reassess.",
        why="a small whole number, in prose",
    ),
    Case(
        reply="Some soreness in the first 10 minutes is normal.",
        why="a duration, under the free integer bar and not a claim",
    ),
    Case(
        reply="I'd rather you finished feeling like you could do more.",
        why="no figure at all",
    ),
    Case(
        reply="The plan has 4 sessions in it this week.",
        why="a count of sessions",
    ),
    Case(
        reply="How did the hip feel on the climbs?",
        why="a question with nothing numeric in it",
    ),
    Case(
        reply="Saturday's 90 minutes stays as it is.",
        why="a duration carries no physiological unit, so it is never a claim",
    ),
    Case(
        reply="Endurance pace, and stop if the back complains.",
        why="a prescription in words, which is the shape HLTH and SAFE want",
    ),
]


# --- the figure is real and came from somewhere -----------------------------

ATTRIBUTED: list[Case] = [
    Case(
        reply="Thursday is 75 minutes at 180 watts.",
        why="both figures came back from get_plan",
        grounded=[4500.0, 180.0, 75.0],
    ),
    Case(
        reply="You're carrying a CTL of 42 at the moment.",
        why="the wellness feed supplied it",
        grounded=[42.0, 31.0],
    ),
    Case(
        reply="You said your LTHR is 165, so I'll work from that for now.",
        why="the athlete supplied it himself; the second channel exists for this",
        self_reported=[165.0],
    ),
    Case(
        reply="The fit puts you around 129 kg.",
        why="rounding a fitted 129.1 is the model rounding, not inventing",
        grounded=[129.1],
    ),
    Case(
        reply="That was 212 watts average over 68 minutes.",
        why="both from get_sessions",
        grounded=[212.0, 4080.0, 68.0],
    ),
    Case(
        reply="Your FTP is set to 230 in the plan.",
        why="unit-then-number, attributed",
        grounded=[230.0],
    ),
    Case(
        reply="You mentioned 128 kg on the scales this morning, which I've noted.",
        why="his own figure, repeated back",
        self_reported=[128.0],
    ),
    Case(
        reply="Saturday holds at 155 bpm average, same as last week.",
        why="a heart rate the wellness feed supplied; unit-after, attributed",
        grounded=[155.0, 5400.0],
    ),
]
