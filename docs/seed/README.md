# Seed content

## `coaching-conversation.md`

The source coaching exchange, 26 to 27 July 2026: baseline assessment through to
the first ramp test. Two things are derived from it, and it is the audit trail
for both.

**`prompts/persona.md`** is written from its voice, not its content. What the
persona encodes is how the coach reasons and speaks: verdict before reasoning,
measurement separated from estimate, sensitivity checks so a conclusion does not
rest on a guessed number, errors owned in the open. The transcript's own
correction table is the clearest example of that last one and is why the persona
has a section on being wrong.

**`seeds/athlete.json`** carries the facts it established, each with a `reason`
tracing back here. Apply it with `coach-seed` after migrating. Safety keys go
through the SAFE-06 athlete path; everything else is attributed to actor `rule`
so the audit trail distinguishes a seed from a nightly pass. Re-running is a
no-op for anything already current.

## Changing either one

Re-read the transcript first. The persona's rules are load-bearing for CHAT-03,
CHAT-04, CHAT-10, SAFE-01 and SAFE-05, and `tests/test_seed_and_persona.py`
asserts each of them is still present, so a rewrite for voice cannot silently
drop behaviour the requirements depend on.

Seeded values are ordinary facts once written. They decay, they can be
superseded by observation, and the store's normal rules apply. The one exception
is the constraints, which never decay and only the athlete can change.
