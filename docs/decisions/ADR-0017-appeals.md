# ADR-0017: Appeals are template-drafted, person-reviewed, and signed by exact text

**Status:** Accepted · **Date:** 2026-11-06 · **Milestone:** M5 (M5.7 slice 2)

Builds on ADR-0016 (denial dispositions). Supersedes nothing.

## Context

- **UR-09 / BR-M11-04:** appeal letters are always reviewed by a person
  before they are sent, and no send path bypasses that review.
- **CLAUDE.md §3:** an LLM is never a primary decision-maker. Delivering an
  appeal to a payer (portal, fax, mail, X12 275) is a real connection, which
  makes it a human checkpoint (CLAUDE.md §12).
- The owner asked (2026-11-06) that choices be made on real-world practice
  without waiting, and recorded so testers can revisit them.

## Decision 1: the draft is a template with prompts only a person can fill

The draft states the claim number, the payer's claim control number, the
member ID, the date of service, each denied line with group and reason
together (PD-3), and an evidence checklist for the denial's category. It
leaves bracketed prompts: `[State why the denial should be reversed…]` and
`[Name, title and contact details…]`.

Signing is refused while any `[…]` prompt remains. An unreviewed draft
cannot be sent; `test_appeal_requires_human_signoff` fails without this check
(shown).

## Decision 2: signing binds to the exact text read

The signer names a version. It must be the current one: a version changed
after they read it cannot be sent under their name. The SHA-256 of the signed
text is recorded. `appeal`, `appeal_version` and `appeal_signoff` are
insert-only by grant (migration 0027). Any role with `appeal:sign` may sign,
which today is the Denial Specialist and the Ops Manager. The signer is the
reviewer UR-09 asks for.

## Decision 3: "send" records the appeal; delivery is a later adapter

Signing moves the claim `Denied → Appealing`, resolves the denial item as
`APPEALED`, and schedules the follow-up after `appeal_follow_up_days`. That
setting is an unregistered placeholder at **30**, the early end of payers'
typical 30–60-day response. Actual delivery to the payer waits for a real
channel.

## Consequences

- One open appeal per denial. Drafting again opens the existing draft.
- No appeal while a write-off of the same denial waits for approval.
- The appeal-level deadline (each payer's appeal window) is not tracked yet.
  The payer registry has no field for it.
- Appeal outcomes arrive as new 835s and run through the existing
  `Appealing → Paid / Denied` transitions.
