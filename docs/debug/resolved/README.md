# Resolved defects

One document per defect that took real work to find. `docs/prior-art.md`
section 6 names this as the third of three planning artefacts worth keeping
from the archived repositories, and the reason is narrow:

> a resolved-debug directory where each document lists the hypotheses that were
> *eliminated*, which is what makes them re-readable later

The eliminated hypotheses are the point. A document that only records the
answer is a changelog entry, and the commit message already is one. What is
expensive to rediscover is the four plausible explanations that were wrong, and
the evidence that ruled each out, because the next defect in the same area will
propose them again.

## When to write one

Not for every fix. Write one when the cause was not the first place you looked:
when a hypothesis you were confident about turned out to be wrong, when the
symptom pointed somewhere other than the fault, or when the thing that misled
you is still in the codebase and will mislead the next reader too.

A one line fix can deserve a document and a large refactor can deserve none.

## Shape

```markdown
# What the symptom was

**Found:** date, and how — a live deployment, a test, a review.
**Cause:** one or two sentences.
**Fix:** the commit or PR.

## Eliminated

- **Hypothesis.** Why it was plausible, and what ruled it out.

## What made it hard to see

The property of the code or the tooling that hid it. This is the part that
generalises.
```

Name files by symptom rather than by cause: the symptom is what someone will be
searching for when they hit it again.
