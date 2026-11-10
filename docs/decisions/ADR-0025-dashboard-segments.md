# ADR-0025: Dashboard segments by client and by payer

**Status:** Accepted · **Date:** 2026-11-10 · **Milestone:** M5.11

## Context

- M5.11 asks for dashboards that can be broken down by payer and by client.
- UR-19: *"Every dashboard supports segmentation by payer and by client.
  Aggregates hide the concentration where problems actually live."*
- The dashboard (M5.11, pulled forward) summed every figure over all of the
  caller's clients and payers. There was nothing to segment with.

## Decision

`GET /api/v1/dashboard?client_id=&payer_id=`. Both parameters are optional
and can be combined.

**A client segment narrows the caller's own scope.**

- `principal.require_client` refuses a client outside the caller's scope with
  the ordinary 403. Its body is the same as any denial, so the call cannot be
  used to find out which clients exist (PD-20).
- For a client in scope, the principal is replaced by one scoped to that
  client alone. Every scoped query then runs unchanged on the narrowed
  principal: the work counts' `scoped_query` included. No second filtering
  path exists to drift from the first.
- **Negative shown:** with the check removed, a caller scoped to client A got
  client B's figures (`200 != 403`).

**A payer segment is a `payer_id` (PD-8), never a name.** It reaches every
figure:

- the denial rate (`denial_rate(..., payer_id=)`), first-pass, and claims by
  state, through `Claim.payer_id`;
- the work counts (`queue_summaries(..., payer_id=)`) and the steps awaiting
  review, through the claim the item or step belongs to.

A work item with no claim belongs to no payer, so it is counted in the total
and in no payer's figures.

**What can be chosen.** `segments` in the response lists:

- the caller's clients;
- the payers with at least one claim in those clients, sorted by name.

The response also echoes the segment shown (`client_id`, `payer_id`).

**The screen:**

- A Payer selector, and a Client selector when there is more than one client
  to choose between.
- The choice lives in the URL (`?client=…&payer=…`), so a segmented view can
  be shared (UR-22).
- The header says what the figures cover.
- While another segment loads, the previous figures stay on screen with
  `aria-busy`, so the controls do not jump.
- An empty segment says so, rather than claiming the caller has no clients.

**Clients have no names yet.** The platform stores no client record until
onboarding (SRS §13.8) creates one. Until then the selector shows the start
of each client's id: "Client 5eed0000".

## Consequences

- Tests:
  - `tests/integration/test_api_insight.py`: by client, by payer (including
    work with no claim), and the invariant
    `test_a_client_outside_my_scope_cannot_be_segmented_to`;
  - `DashboardScreen.test.tsx`: 3 new tests.
- GATE M5 suite: 20 passed. The dashboard scan includes the segment controls.
- **Rollback:** drop the parameters and the selectors. The response's extra
  fields are additive. No migration is involved.
