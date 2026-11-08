# ADR-0022: Claim search behind the top bar

**Status:** Accepted · **Date:** 2026-11-09 · **Milestone:** M5 (found
while preparing GATE M5)

## Context

- Since M5.1, the top bar's box has read "Search claim ID or payer", and
  the screen behind it said claim search would come with M5.5. M5.5
  shipped, and the screen still said "not available yet".
- Looking a claim up is how most payer calls and patient questions start.
  GATE M5 counts it among the five highest-volume journeys (ADR-0023), so
  the gap had to close first.

## Decision

`GET /api/v1/claims?q=` (`rcm.work.claim_search`).

- **What matches:**
  - a claim number that starts with the text, ignoring case;
  - a payer whose id is the text;
  - a payer whose name contains the text.
- **Length:** the text must be 2 to 80 characters.
- **Literal matching.** LIKE wildcards are escaped, so `CL%` matches
  nothing.
- **Never logged.** A search can be a member's name typed into the wrong
  box (PD-13).
- **Scope.** Results go through `scoped_query` (PD-20). Another client's
  claim is never a result.
- **Audited.** Dates of service are listed, so the read is audited (PD-2).
- **Each submission is a row,** with its attempt number. A resubmission is
  the same claim (PD-7), and the operator picks the one they mean.
- **Order and cap:** newest service first, at most 50 results. When there
  are more, `truncated` is set and the screen asks the operator to narrow
  the search.
- **Screen states:**
  - too short to search;
  - searching (a skeleton);
  - a named failure (never shown as "no matches");
  - no matches;
  - the results.
- **Not searched:** member and patient names. A member search is a
  PHI-matching feature with its own masking and audit questions, and it is
  not needed to work a claim.

## Consequences

- Tests:
  - `tests/integration/test_claim_search.py`: 9 tests, including that
    another client's claims never appear (`test_another_clients_claim_is_never_a_result`);
  - `frontend/src/screens/SearchScreen.test.tsx`: 5 tests.
- **Rollback:** remove the route and restore the placeholder screen. No
  migration is involved.
