# ADR-0029: ML-5 (appeal success) is not trained yet

**Status:** Accepted · **Date:** 2026-11-11 · **Milestone:** M6.3

## Context

- M6.3 lists *"ML-2 denial category, ML-3 recovery, ML-5 appeal success"*.
  ADR-0027 built ML-2 and ML-3.
- SRS 9.1, ML-5: binary classifier; input *"denial + appeal type"*; output
  *P(win)*.
- DOC-02 BR-M11-07 (MUST): *"Appeal outcomes MUST be recorded and used to
  estimate future appeal success probability."*
- What exists today (M5.7, ADR-0017):
  - an appeal (`appeal`), each version of its letter, and the signature
    that sent it;
  - no appeal **type** (reconsideration, redetermination, external review,
    or any other level);
  - no appeal **outcome**: nothing records whether a sent appeal was won,
    partly won or lost;
  - no appeals in the synthetic corpus at all. Appeals are created only
    when an operator works the Denial Workbench.

## Decision

**ML-5 is not trained, and is not registered.** A model with no labels is
not a model. One trained on outcomes invented for the purpose would learn
only the invention (CLAUDE.md section 8), and the registry would call it
ACCEPTED.

Before ML-5 can be built, three things must exist. Each is its own piece of
work:

1. **The appeal type**, recorded on the appeal when it is drafted: ML-5's
   other input.
2. **The appeal outcome**, recorded when it is known (BR-M11-07). The
   decision this needs is *where it comes from*:
   - the payer's reprocessing of the original claim on a later 835 (CLP
     status 22 or a new adjudication for the same claim): automatic, but
     not always distinguishable from a corrected resubmission;
   - an operator recording the decision letter: certain, but manual.
3. **Outcomes to learn from.** The synthetic generator would have to send
   appeals for some denials and decide their outcomes. Whatever rule it
   used is all ML-5 could learn (CLAUDE.md section 8). Such a corpus would
   prove the pipeline, as it does for ML-1 to ML-3, never the model.

Until then:

- The Denial Workbench keeps showing what it has: similar past denials for
  the pattern and how many were resolved. Never a probability of winning.
- `serving.SPECS` has no ML-5 entry, so nothing can ask for its score.

## Consequences

- M6.3 is complete for ML-2 and ML-3. ML-5 is deferred, with its
  prerequisites named.
- **Open question for the sponsor:** whether the synthetic generator should
  simulate appeals and their outcomes so that the ML-5 pipeline can be built
  and tested end to end, knowing the model can only learn the generator's
  rule.
- Rollback: nothing to roll back. Nothing was built.
