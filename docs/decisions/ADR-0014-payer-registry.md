# ADR-0014: The payer registry is dated, versioned, and cannot block once stale

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.10)

## Context

- **DOC-02 9:** the payer rules registry is hand-maintained reference data
  that every module depends on. It decays: payers change rules without
  notice.
- **BR-REG-01:** every entry carries `last_verified_date` and `verified_by`.
- **BR-REG-02 / UR-18:** past `[TBD-42]` days an entry is stale, and must not
  silently drive a blocking decision until re-verified.
- **BR-REG-03:** changes are versioned, and claims record which registry
  version applied.
- `[TBD-42]` is unset. It is the labelled placeholder
  `payer_registry_staleness_days`, provisionally 180, owner Muhammad Anas.

## Decision 1: one row per version, never edited

`payer_registry_entry` (migration 0024) is insert-only by grant. An entry is
keyed by payer, plan type and CPT, each of the last two optionally `*`
("every"). A change is a new version with a reason. Re-verification
(`verify_entry`, S-07's verification action) is also a new version: the same
facts, a new date and verifier. So the history shows every change and every
time someone vouched for it.

A verification date in the future is refused, and so is an unnamed verifier
or a payer the platform does not know (PD-8: keyed on `payer_id`).

## Decision 2: the registry drives a check, as NCCI does

The first fact recorded is `prior_auth_required` per payer and procedure.
`RegistryCheck` runs in Layer 4 (PAYER_SPECIFIC) beside the rules, and flags
a line whose entry requires authorisation when the claim carries none,
routed to `AUTH_PROBLEM`.

- **The most specific entry wins:** the procedure's own over the payer-wide
  one, and a plan type's own over every plan's. The plan type is the payer's
  (`payer.plan_type`).
- **`prior_auth_required = None`** means "not recorded", never "not required".
- **Fresh:** BLOCKING. **Stale:** ADVISORY, and the finding's reference says
  it is stale and why. It is excluded from blocking, never silently.

The registry, not a rule, is what blocks here. A hand-written payer rule is
governed by the rule lifecycle and its backtest (M4.5). An entry is governed
by its verification date. The finding names the entry version
(`rule_version`), so the feedback loop counts it as covering authorisation
denials (ADR-0013).

## Decision 3: every scrub records the entries it read

`scrub_claim` puts the payer, plan type, CPT, version, verification date and
staleness of every entry its lines read into the `SCRUB` decision record
(BR-REG-03). `scrub_claim` takes `today` so a scrub can be replayed as of a
date; it defaults to the current date.

## Deferred, with reasons

- **BR-REG-04** (a denial attributable to a wrong entry triggers review). If
  an entry requires authorisation and the claim was denied for it anyway,
  the feedback loop already raises a failed check to the rules reviewer. The
  case where an entry says authorisation is *not* required and the payer
  denies for it needs a review queue on the registry itself. That belongs
  with S-07 (M5.10).
- **BR-REG-05** (a named accountable owner). This is organisational. Each
  version names its verifier, and the staleness threshold's owner is
  recorded with `[TBD-42]`.
- **The other DOC-02 9.1 attributes** (filing and appeal days, modifiers,
  frequency limits, channel). Each is added when a module first reads it.
  Timely filing stays on `payer.timely_filing_days`, which M3.6 already
  uses.

## Rollback

`alembic downgrade 0023` while no entry is recorded. With none recorded,
the check flags nothing, so removing it changes no verdict.
