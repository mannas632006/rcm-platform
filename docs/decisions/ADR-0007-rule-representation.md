# ADR-0007: Rules are versioned data, evaluated against a fixed list of facts

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.1, M4.2)

## Context

SRS §8.1: rules are "declarative, versioned, stored as data — not code
deployments". FR-RULES-01 and BR-M7-02: every evaluated claim records the
ruleset version that evaluated it. SRS §8.2 fixes six layers, in order.

The SRS shows a rule's shape but not its storage, what a condition may name,
or what happens when a claim lacks the value a condition asks about.

## Decision 1: a rule version never changes

`rule_version` holds one row per `(rule_id, version)`. Its content (layer,
severity, scope, condition, action, provenance) is never updated. A change is
a new version. Grants enforce it, as they do for the audit tables: the
application role may insert rule versions and update `status` only, and
cannot delete. So "which rule flagged this claim" always has one answer.
Rulesets are insert-only the same way.

The lifecycle `status` (FR-RULES-06: draft → backtest → shadow → advisory →
blocking → deprecated) is the one mutable column. It moves only through the
lifecycle functions (M4.5).

## Decision 2: a ruleset is a published snapshot

`ruleset` is an identity-numbered release; `ruleset_member` lists the rule
versions in it and the status each had when published. Publishing takes every
rule version in `SHADOW`, `ADVISORY` or `BLOCKING`, so a ruleset is exactly
what was live. Each ruleset carries a SHA-256 digest of its members.

Evaluation always uses the latest ruleset and records it as the
`decision_record` logic version, `ruleset-v<n>` (FR-RULES-01). Changing a
rule's status changes nothing until the next publish.

## Decision 3: rules are not client-scoped

Rules encode payer behaviour, and payers are shared reference data (the
`payer` table has no `client_id`). Rules are global, like the payer registry.
Claims, findings and decision records stay client-scoped (PD-20). A rule that
must apply to one client only can be scoped later by adding `client_id` to
`scope`; nothing in this design rules that out.

## Decision 4: conditions name facts from a fixed list

A condition is `all` / `any` / `not` over `{field, op, value}` leaves. Fields
come from a fixed list, each with a type:

| Field | Type |
|---|---|
| `claim.payer_id`, `claim.claim_frequency_code`, `claim.claim_filing_code`, `claim.place_of_service` | text |
| `claim.total_charge` | money |
| `claim.date_of_service` | date |
| `claim.has_prior_auth` | boolean |
| `claim.diagnoses`, `claim.procedure_codes` | list of text |
| `claim.line_count` | integer |
| `line.procedure_code` | text |
| `line.modifiers` | list of text |
| `line.units`, `line.charge_amount` | number, money |
| `line.service_date` | date |

A rule is checked when it is created, not when it runs: an unknown field, an
operator that does not fit the field's type, or a value of the wrong type is
refused by name. No field carries PHI (member ID, name, date of birth), so a
finding's inputs can be recorded in full (PD-13). Rules that need PHI, such as
member-ID patterns (Layer 2), come with the payer registry in M4.10, where
their reads can be audited (PD-2).

Operators: `eq`, `ne`, `in`, `not_in`, `contains`, `not_contains`, `gt`,
`gte`, `lt`, `lte`, `exists`, `not_exists`. There is no regex operator, so no
rule can hang the scrubber on a pathological pattern.

A rule whose condition names any `line.*` field is checked once per line;
otherwise once per claim. `scope.payer_id` limits the claims it sees, and
`scope.cpt` the lines.

## Decision 5: a missing fact is never read as "no problem"

If a condition needs a value the claim does not have (a line with no service
date, say), the rule's result is `INDETERMINATE`, not `false`. For a blocking
rule, that blocks like a finding and goes to a person (PD-18). `exists` and
`not_exists` are how a rule asks about absence on purpose.

## Decision 6: the six layers run in order, every time

Layers run `STRUCTURAL`, `DEMOGRAPHIC`, `CLINICAL`, `PAYER_SPECIFIC`,
`TEMPORAL`, `LEARNED`, in that order, and the order is a tested constant.
Every layer runs even after an earlier one blocks, so a person sees every
problem at once rather than one per resubmission. `LEARNED` is empty until
M6.

The verdict:

- `BLOCK`: any finding from a `BLOCKING` rule, or any `INDETERMINATE`
  result from one;
- `ADVISE`: findings from `ADVISORY` rules only;
- `PASS`: nothing.

`SHADOW` findings are recorded and never change the verdict. A rule acts as
blocking only when its `severity` and its `status` are both `BLOCKING`.

Auto-correction (`action.auto_correct: true`) is refused at creation until
M4.7 builds the deterministic fixes, so no rule can change a claim before
the PD-16 refusals exist.

## Consequences

- Migration 0017 adds `rule_version`, `ruleset`, `ruleset_member` and their
  write-once grants. Its downgrade refuses once a ruleset is published,
  because decision records cite them.
- One `decision_record` (`SCRUB`) per evaluation, with the ruleset version,
  the facts and every finding.
- Rollback: `alembic downgrade 0016` while no ruleset exists; nothing reads
  these tables outside `rcm.decision.rules`.
