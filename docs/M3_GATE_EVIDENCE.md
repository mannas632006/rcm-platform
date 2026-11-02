# M3 gate evidence — claim lifecycle and work queues

CLAUDE.md section 5, GATE M3: *"Full synthetic claim lifecycle runs end to
end: created → scrubbed → submitted → acknowledged → remitted → posted or
denied. No claim ever rests in a non-terminal state without a next action
date."*

How claims move is set out in ADR-0006. This file has the measurements.

---

## How it was measured

```
make seed                      # generate + load the synthetic corpus
python -m tools.advance_claims # progression twice, then count
```

`tools/advance_claims.py` runs `progression.advance_all` for the corpus
client twice. The first run moves every claim as far as its evidence goes.
The second run must take no step. The tool then counts claims by state, work
items by kind, and claims that `claims_needing_action` reports with no next
action date. It exits non-zero unless the second run took 0 steps and no
claim is unscheduled.

Run on 2026-11-02 against a clean database, after the review fixes in
ADR-0006 (commit `c09ac01`). Exit code 0.

Run again on 2026-11-03 after the PR #13 review fixes (commit `21283e8`):
every number below is the same, and exit code 0. The new holds
(`rejected-after-acceptance`, `void-submission`, a rejection plus an 835 on
a replaced claim) found nothing in this corpus; their tests cover them.
Progression took 77m50s that time.

### Corpus loaded

| File kind | Files |
|---|---|
| 837 | 3,597 |
| 999 | 3,597 |
| 277CA | 3,528 |
| 835 | 1,451 |
| **Total** | **12,173**, 1 quarantined (the deliberately unbalanced 835) |

Claims in the database: **34,773**. Load time: 13m53s.

### Progression

| Measure | Value |
|---|---|
| Claims seen | 34,773 |
| Steps, first run | 334,900 |
| Steps, second run | **0** |
| Steps on record | 334,900 (one per step, none duplicated) |
| Held for a person | 0 |
| Problems raised (`DATA_QUALITY`) | 135 |
| Claims with no next action date | **0** |
| Run time (both runs) | 65m20s (75m16s before the 0016 index) |

On the second run, 34,054 claims were seen. The 719 `Replaced` claims are
final, so it skips them. It found 110 of the problems again and raised no new
work item for them: the count stayed at 135.

### Where the claims ended

| State | Claims | Kind | What brought it there |
|---|---|---|---|
| `Paid` | 25,983 | resting, 7-day follow-up | 835, no line denied |
| `PartiallyPaid` | 3,769 | resting, 7 days | 835, some lines denied |
| `Denied` | 1,752 | resting, 7 days | 835, every line denied |
| `Rejected` | 1,327 | 2 days | 277CA rejection (PD-4: not a denial) |
| `Replaced` | 719 | final | a later submission the payer took chains to it by REF*F8 |
| `Submitted` | 665 | 2 days | 837 only, no payer reply yet |
| `InAdjudication` | 558 | 30 days | 277CA accepted, no 835 yet, or held on a problem below |
| **Total** | **34,773** | | |

### Problems held for a person

| Problem | Claims |
|---|---|
| `835-patient-share-only-line` | 135 |
| every other problem in ADR-0006 | 0 |

All 135 are lines paid nothing whose only reasons are patient
responsibility (PR). Before the review they were counted as denials. Whether
they are denials is M4.8's call, made in the analytics code and both dbt
models together. Until then each claim waits in `InAdjudication` with its
next action date.

### Compared with the first run

The review changed four rules, and the counts moved with them:

- `Replaced` 773 → 719: an original is replaced only once the payer takes
  the replacement. The other 54 stay live until it does.
- `InAdjudication` 448 → 558, `PartiallyPaid` 3,843 → 3,769: the
  patient-share-only claims above are held instead of decided.
- `Denied` 1,738 → 1,752, `Rejected` 1,324 → 1,327, `Submitted` 664 → 665:
  originals that are no longer `Replaced` rest where their own evidence
  puts them.

Every lifecycle stage named by the gate is reached: created (`Registered`),
scrubbed (`Scrubbing` → `ReadyToSubmit`), submitted, acknowledged
(`InAdjudication` / `Rejected`), and remitted (`Paid`, `PartiallyPaid` or
`Denied`). Every claim not in a final state has a next action date.

### Review

Every claim that moved has one open `CLAIM_STEP_REVIEW` work item for the
submitter, **34,773** in all. That is the owner's "done automatically but
visible" (ADR-0006 Decision 3). Every step also wrote a `decision_record`
(`PROGRESS`, `progression-v1`), 334,900 in all (PD-1).

---

## Found while measuring

The first progression build limited evidence to files loaded *after* the
837. That handled a resubmission that reuses its CLM01. But
`uq_claim_client_submitter_id` already rules that case out: the loader keeps
one claim per CLM01 per client. The limit did harm on its own terms. A 277CA
or 835 that arrives before its 837 was ignored, so the claim stayed
`Submitted` for good.

The limit is removed. `test_evidence_that_arrives_before_the_claim_still_counts`
covers it. Against the old code it fails:

```
E       AssertionError: assert <ClaimState.SUBMITTED: 'Submitted'> is <ClaimState.DENIED: 'Denied'>
1 failed, 14 deselected
```

**Open, outside M3:** a frequency-7 replacement that reuses its original's
CLM01 is skipped by the claim loader as a resend (`claim_loader.py`, the
`_find_claim` check). Real practices do send these. Handling them needs a
change to the claim key, so it belongs with M4.

**Open, outside M3:** the synthetic generator writes CLP02 `2` ("processed
as secondary") on primary claims that carry a patient share
(`tools/synthetic/generate.py`). Progression does not read CLP02, so this
does not touch the numbers above. It should be fixed before anything relies
on CLP02.

---

## Invariant tests (M3)

| Test | File |
|---|---|
| `test_claim_never_rests_without_next_action` | `tests/unit/test_claim_state.py`, `tests/integration/test_claim_lifecycle.py` |
| `test_concurrent_transitions_do_not_interleave` | `tests/integration/test_claim_lifecycle.py` |
| `test_two_operators_cannot_hold_one_item` | `tests/integration/test_work_queue.py` |
| `test_deadline_beats_priority_score` | `tests/unit/test_prioritisation_rules.py` |
| `test_primary_failure_fails_over_to_secondary`, `test_both_failing_raises_a_named_error`, `test_both_down_loses_nothing_and_recovers` | M3.7 suites |
| `test_no_advanced_claim_rests_without_a_next_action` | `tests/integration/test_claim_progression.py` |
