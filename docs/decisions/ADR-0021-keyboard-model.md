# ADR-0021: The keyboard model, and taking and resolving from the queue

**Status:** Accepted · **Date:** 2026-11-09 · **Milestone:** M5 (M5.4)

## Context

- **SRS 10.9** sets the keys:
  - Global: `/`, `g q`, `g d`, `?`.
  - Queue: `j`/`k`, Enter, `x`, `a`, `r`, `f`, Esc.
  - Detail: `1` `2` `3`, `n`, `]`/`[`, Esc.
- **UR-35:** every mouse action in a queue can be done by keyboard.
- **UR-36:** `]` and `[` move through the queue without going back to the
  list.
- **UR-37:** no destructive action is bound to a single unmodified key.
- **WCAG 2.1.4:** single-character shortcuts must be able to be turned
  off. The "Shortcuts on/off" switch has existed since M5.1.
- Before this change only `/` existed. The API also had no way to take or
  resolve an item: the M3.3 lock rules (`rcm.work.queue`) were reached only
  through denial dispositions and automation.

## Decision

### API: three routes over the existing lock

All three go through `rcm.work.holding`. Each needs an Idempotency-Key, so
a retried keystroke acts once (PD-14).

- `POST /api/v1/work-items/{id}/take` assigns the item to me. Taking an
  item I already hold renews the lease. If my own lease has lapsed, the
  item is taken afresh. Someone else's hold is refused by name (409).
- `POST /api/v1/work-items/{id}/release` gives the item back. Only its
  holder may do this.
- `POST /api/v1/work-items/{id}/resolution` closes the item.
  - The outcome must be `NO_ACTION_NEEDED` or `DUPLICATE`, with a
    non-blank note (FR-WORK-04).
  - The item is taken first if nobody holds it.
  - **A denial is refused (422).** Writing off, appealing and correcting a
    denial all have consequences that the Denial Workbench enforces
    (BR-M11-05, UR-09, PD-7). Every other outcome belongs to the workflow
    that does what it names.
  - Resolving never lifts an automation suspension. That stays a separate,
    reviewed step (BR-EXC-03).
- **An escalated item is refused** by take and resolve (409). It waits for
  a supervisor, and no supervisor workflow exists yet.
- **The row is locked when it is first read.** Who holds it, whether the
  lease has lapsed and what state it is in are all decided on the locked
  row. A lapsed hold, mine or another operator's, is taken afresh by
  either route.

**Scope and role.** The item is read through the client scope (PD-20).
The caller must also hold both the queue's read permission and its new
`work` permission:

| Queue | Work permission |
|---|---|
| Claim queues | `claim:write` |
| Denial Workbench | `denial:disposition` |
| Rules Review | `rule:author` |
| Operations | `claim:submit` |

Operations holds compliance concerns, which suspend automation, so it is
worked only by the operations manager. An item outside the caller's
clients, or in a queue the role cannot work, reads as not found. Both
cases get the same answer.

### UI

- **The queue table is now an ARIA `grid`.** It is multi-selectable, and
  focus stays on the grid, with the active row as its
  `aria-activedescendant`.
  - Arrows, Home, End, Enter and Space work in the grid whether shortcuts
    are on or off. They are not single-character shortcuts.
  - `j`/`k` move the active row and bring focus to the grid.
  - `x` selects the row. Esc clears the selection.
  - `a` assigns the selected rows (or the active row) to me, and reports
    which could not be assigned, and why.
  - `r` opens the resolve dialog. On a denial, the dialog points to the
    workbench and offers no resolve button.
  - `f` focuses the filter.
  - A toolbar gives mouse users the same actions ("Assign to me",
    "Resolve…", "Clear selection").
- **Detail screens** take `?queue=&item=&assigned=` from the row that
  opened them (UR-22).
  - `]` and `[` walk the queue's cached rows in the server's order,
    skipping rows that have no screen to open.
  - Esc returns to the queue.
  - `1` `2` `3` jump to the panels:
    - Claim Detail: lines, history, decision audit;
    - Denial Workbench: write off, correct, appeal.
- **Global keys:** `g q` goes to My Work, `g d` to the dashboard, and `?`
  opens a dialog listing every key.
- **Shortcuts never fire** while typing, during an IME composition, inside
  a dialog, or when shortcuts are turned off.
- **UR-37 holds.** No key writes off, submits or appeals. `r` opens a
  dialog that needs an outcome and a reason.

### Not built: `n` (add note)

SRS 10.9 lists `n`, but the SRS data model has no work-item note. A
resolution note exists only at the moment of resolving. Adding a notes
entity is a schema decision for its own task. The help dialog lists only
the keys that work.

## Consequences

- Tests:
  - `tests/integration/test_work_item_holding.py`: 32 tests covering:
    - the lock, including lapsed leases;
    - scope, and each queue's work permission;
    - the denial refusal, proved by removing the guard, which makes the
      test fail;
    - the escalated refusal;
    - closed items;
    - retry safety on all three routes;
  - `frontend/src/screens/QueueKeyboard.test.tsx`: 14 tests, including the
    dialog's Tab trap with radios, no keys firing behind an open dialog,
    and a filter change clearing the selection.
- The queue table's role changed from `table` to `grid`, so tests now look
  for `grid` and `gridcell`. What they assert is unchanged.
- **Rollback:** remove the router and the hooks. No migration is involved.
