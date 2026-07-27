# Seed content

## `coaching-conversation.md` — required before P01 ships

CHAT-02 requires the persona to be seeded from the source coaching conversation,
and `docs/setup.md` step 10 asks for it to be reviewed before loading because it
"sets the tone for years". It is **open item 9** in `docs/prd.md` and it is the
one thing P01 needs that this repository cannot supply for itself.

Commit the conversation here as `coaching-conversation.md`, then rewrite
`prompts/persona.md` from it — keeping the behavioural rules that are already
there, since those are what the CHAT, SAFE and naturalness requirements test
against. `coach.agent.persona.is_seeded()` returns False until the `TO BE
SEEDED` marker is gone, and a test asserts the scaffold is still flagged so this
does not get forgotten.

Nothing else in the phase is blocked: the prompt assembles, the tools dispatch,
the naturalness suite runs, and the interruption budget holds — all against the
scaffold. What is missing is voice.
