# ADR-0011: Auto-correction is a short list of fixes with one right answer

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.7)

## Context

- **BR-M7-03:** auto-correction is allowed only for deterministic fixes,
  "formatting, known field mappings". Anything needing judgment goes to a
  person.
- **PD-16 / FR-RULES-05:** modifiers 25 and 59 are never applied
  automatically. They are a recognised audit target.
- **SRS §8.2:** a blocking finding that can be auto-corrected is fixed and
  logged, and evaluation starts again from Layer 1.

## Decision 1: a fixed catalogue of fixes

A rule names its fix from a catalogue held in code. There is no free-form
fix. Each fix is formatting: it changes how a value is written, never what
it says.

| Fix | Applies to | Does |
|---|---|---|
| `UPPERCASE_MODIFIERS` | a line | `rt` → `RT` |
| `DEDUPLICATE_MODIFIERS` | a line | `RT, RT` → `RT`, keeping order |
| `STRIP_DIAGNOSIS_DECIMAL` | the claim | `J06.9` → `J069` (X12 carries no decimal point) |

A rule with `auto_correct: true` must name one of these, and a line fix
must sit on a line-level rule.

**No fix adds a modifier.** Choosing a modifier is a coding judgment. A rule
asking for `ADD_MODIFIER` is refused when it is written. For 25, 59 and
XE/XP/XS/XU the refusal is `ManualCodingRequiredError` (PD-16), so the
reason is named.

## Decision 2: the fixer checks itself

After every fix, the corrector compares each line's modifiers before and
after. A protected modifier (25, 59, XE, XP, XS, XU) that appears after a fix
raises `ManualCodingRequiredError`, whichever fix put it there. That is the
second line of defence behind the catalogue, and a test drives a rogue fix
into it.

## Decision 3: fix, re-run, give up after three

- Only a `BLOCKING` finding with an outcome of `FLAGGED` is auto-corrected.
- If every blocking finding has a fix, all of them are applied and the six
  layers run again from Layer 1.
- Otherwise the claim blocks and goes to a person, with the fixes already
  made kept.
- A fix that changes nothing ends the loop and blocks. So does a third round
  with blocking findings still open. A pair of rules that undo each other
  cannot loop forever.
- `SHADOW`, `ADVISORY` and `INDETERMINATE` findings are never fixed.

## Decision 4: a correction is written, audited and recorded

`scrub_claim` applies the corrections to the stored claim in its own
transaction, through `rcm.domain.claim_correction`:

- **Audit:** each change writes an `audit_event` naming the field, the line
  and the fix (PD-2).
- **No values in the audit:** the codes themselves are not put in
  `audit_event`.
- **Before and after:** the `SCRUB` `decision_record` lists every
  correction, with its before and after values (PD-1).

A scrub whose verdict rests on corrected facts therefore always leaves a
corrected claim. A PASS can never describe a claim the database still holds
uncorrected.

## Correction to ADR-0007

ADR-0007 said the facts carry no PHI. Read strictly, that is wrong. A
diagnosis code tied to a claim is health information about a person.

What is true:

- The facts carry no direct identifiers (name, member ID, date of birth).
- They are recorded only in `decision_record`, which sits in the access- and
  audit-controlled database.
- They never reach logs, metrics or error payloads (PD-13).

ADR-0007 is amended to say exactly that.

## Rollback

Set `auto_correct: false` on the rules, or deprecate them. Corrections
already made stay on the claim and in its records.
