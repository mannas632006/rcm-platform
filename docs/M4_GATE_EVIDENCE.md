# M4 gate evidence — rules engine

CLAUDE.md section 5, GATE M4: *"Replay the synthetic denial corpus. Show
measured first-pass clean-claim rate before and after rules are active.
Demonstrate the feedback loop closing on at least one denial pattern."*

How the rules engine works is set out in ADR-0007 to ADR-0014. This file has
the measurements.

---

## How it was measured

```
python -m tools.synthetic --config tools/synthetic/config/config.yaml   # full corpus
python -m tools.load_corpus                                              # into a cleared database
python -m tools.m4_gate                                                  # the gate
```

`tools/m4_gate.py` does four things, in order:

1. **Reference data.** It loads the generator's NCCI pairs as a PTP release
   (10 pairs) and its payer authorisation rules as payer registry entries
   (80 entries, verified that day). This is what a team maintains by hand
   (DOC-02 9).
2. **The loop.** It runs `rcm.work.denials.classify_and_route` over every
   denial: classification (M4.8), then the feedback loop (M4.9).
3. **Review in the rules reviewer's place**, recorded as
   `gate:m4-rules-reviewer`.
   - The cutoff is the median submission date, 2025-01-13.
   - A candidate is promoted to ADVISORY only if, on claims submitted before
     the cutoff, it flagged something and its false-positive rate is within
     the 10% cap (TBD-45).
   - Every step goes through the M4.5 lifecycle.
   - Candidates are drafted ADVISORY, so none can reach BLOCKING.
4. **Replay.** Each first submission is evaluated against the published
   ruleset, with NCCI and the registry, and nothing is written.
   - A denied claim counts as caught only when acting findings name **every**
     cause it was denied for.
   - A paid claim that gets flagged is counted as review burden.

**First-pass clean-claim rate** = first submissions paid with no denied line
÷ first submissions with an outcome, adjudicated or rejected. "After" assumes
a caught claim is corrected before it is sent.

The **out-of-time** view is the honest one: rules are judged on claims
before the cutoff and measured on claims after it.

Run on 2026-11-03 against a cleared database, after loading the full corpus.
The corpus had 34,773 claims (34,000 originals and 773 resubmissions) in
12,173 files, with 1 quarantined by design. The gate exited with code 0 in
37m51s, at commit `70df808` plus this tool.

### Loop activity

| | Count |
|---|---|
| Denied lines classified | 7,834 |
| Unmapped refusal codes | 0 |
| Candidates drafted and backtested | 610 |
| Denials joining an existing candidate | 5,661 |
| No claim rule possible (recorded reason) | 1,113 |
| Not preventable (logged) | 450 |
| Candidates promoted after review | 63 (all Authorisation) |
| Candidates held: false-positive rate over the cap, or no pre-cutoff evidence | 547 (206 eligibility, 186 medical necessity, 155 coding) |

## First-pass clean-claim rate

| View | Claims | Before | After |
|---|---|---|---|
| In-sample (all claims) | 33,337 | **79.11%** | **88.52%** |
| **Out-of-time** (from 2025-01-13) | 16,731 | **78.36%** | **87.69%** |

Out of time, by denial cause:

| Cause | Denied | Caught |
|---|---|---|
| Coding | 1,442 | 1,173 |
| Registration/Eligibility | 537 | 0 |
| Authorisation | 393 | 392 |
| Medical Necessity | 272 | 0 |
| Contractual/Non-covered | 225 | 0 (not preventable) |
| Timely Filing | 187 | 0 (no claim rule: PD-15 prioritisation) |
| Duplicate | 8 | 0 (no claim rule) |

Paid claims also flagged: **0**.

### Where the gain comes from

All 1,560 out-of-time catches are made by reference data alone: NCCI catches
the bundling denials (CARC 97), and the registry catches the authorisation
denials (CARC 197). The promoted learned rules add **no** catches beyond
them. That is not a failure of the loop. The registry was seeded from the
same payer authorisation rules the generator uses, so the learned rules and
the registry cover exactly the same claims. Read the 9.3-point gain as the
value of the NCCI and registry checks, not of the learned rules.

## The feedback loop closing

The loop drafted every candidate from denials alone: no registry, no NCCI,
no hand-written rule. Of those, 63 Authorisation patterns (payer × CPT,
claim without an authorisation) met the review bar on pre-cutoff claims. On
claims submitted **after** the cutoff, those learned rules:

- flagged **567** later denied lines of the pattern they were drafted from;
- flagged **0** paid claims.

| Example | Denials answered | Before cutoff | Later caught | Later false |
|---|---|---|---|---|
| `FB_AUTH_60054_29881_27F4D924` | 36 | 15 | 19 | 0 |
| `FB_AUTH_60054_45380_B12D981E` | 43 | 18 | 19 | 0 |
| `FB_AUTH_87726_90837_0C832198` | 41 | 25 | 15 | 0 |

The full table of 63 rows is in the tool's output. Each row is a denial
pattern that was drafted, backtested, reviewed, promoted and then caught
when it recurred. That is SRS 8.4's loop, end to end. The loop recovered,
from denials alone, the payer authorisation rules a registry owner would
otherwise have to write down by hand.

## What this does not show, stated plainly

- **Synthetic denials contain only the patterns the generator programmed**
  (CLAUDE.md §8). Authorisation is deterministic there, so the learned rules
  are perfect on it. Real payers are noisier.
- **Broad candidates did not generalise, and were held.** That covers every
  coding, eligibility and medical-necessity candidate. Their false-positive
  rates on payer × CPT alone were far above 10%, because those denials turn
  on facts a payer × CPT rule cannot see: a terminated policy, a diagnosis
  pairing, a missing modifier 25. This is the guardrail working. Sharpening
  those rules is a reviewer's job.
- **269 coding denials stay uncaught out of time** (1,442 − 1,173). Most are
  CO-4: an E/M billed with a same-day procedure and no modifier 25. A rule
  may *flag* that. It may never *add* the modifier (PD-16, ADR-0011).
  Writing that flag is the clearest next rule for a reviewer.
- **"After" assumes every caught claim is corrected before it is sent.**
  The checks route each finding to a named queue (AUTH_PROBLEM,
  SCRUB_EXCEPTION); working those queues is M5.

## Invariant tests for M4

| Directive | Test | Status |
|---|---|---|
| PD-16 | `test_modifier_25_never_auto_applied`, `test_modifier_59_never_auto_applied` | pass |
| FR-RULES-06 | `test_rule_promotion_requires_backtest` | pass |
| BR-REG-02 | `test_stale_payer_rule_cannot_block` | pass |
| FR-RULES-07 | `test_every_preventable_denial_gets_a_rule_or_a_reason`, `test_a_preventable_denial_drafts_a_candidate_rule` | pass |
| BR-TAX-01 | `test_every_denial_maps_to_one_category` | pass |
| PD-5 | `test_rules_override_model_on_conflict` | **not written: M6.** No model exists yet. The LEARNED layer refuses hand-written rules and holds nothing until ML-1 lands, so a test now could only show a conflict that cannot occur. |
