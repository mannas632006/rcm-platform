# ADR-0002 — EC-40 priority discrepancy between CLAUDE.md and DOC-02

- **Status:** Accepted
- **Date:** 2026-09-01
- **Context:** CLAUDE.md section 7.3, DOC-02 section 13

## Context

CLAUDE.md section 7.3 lists 31 edge cases to prioritise as High.
DOC-02 section 13, the edge case register itself, marks **32** as High.

The difference is **EC-40 — "Payer changes requirement overnight" (module M7)**,
marked High in DOC-02 and absent from the CLAUDE.md list.

## Decision

Treat all **32** as High. `tests/EDGE_CASES.md` carries DOC-02's priority as
authoritative.

## Rationale

CLAUDE.md section 13 sets precedence: on *what the system does*, DOC-02 and the SRS win;
on *how the work is done*, CLAUDE.md wins. An edge case register is a statement
about system behaviour, and the register is DOC-02's own artefact — the CLAUDE.md
list is a restatement of it. Where a restatement and its source disagree, the
source governs.

The asymmetry also matters: treating EC-40 as High costs one extra prioritised
test. Treating it as Medium risks shipping without coverage of a payer changing a
requirement without notice — which DOC-02 section 9 already identifies as the reason the
payer registry carries `last_verified_date` and a staleness rule (BR-REG-02, PD-10
in the rules engine's staleness gate). The two are the same hazard seen from
different angles.

## Consequences

- `tests/EDGE_CASES.md` shows 32 High, 32 Medium, 6 Low.
- No change to CLAUDE.md; it is the operating contract and is not edited to resolve
  a disagreement with the specification it points at.
