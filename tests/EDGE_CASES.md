# Edge Case Register — coverage tracker

Satisfies **NFR-TEST-02** (SRS §14): every edge case in `docs/02_Domain_and_Functional.md`
§13 has a corresponding test case, tracked to completion.

Priority is taken from **DOC-02 §13**, which is authoritative. CLAUDE.md §7.3 lists 31
High-priority cases; DOC-02 marks 32. The difference is **EC-40**, resolved in favour of
DOC-02 — see [ADR-0002](../docs/decisions/ADR-0002-edge-case-priority-discrepancy.md).

**Status values:** `NOT_STARTED` · `COVERED` (test exists and passes) ·
`DEFERRED` (with a reason and a linked milestone — never a bare skip; CLAUDE.md §7.5).

**Where it stands (2026-11-12, ADR-0030):** 22 covered, 48 deferred, none
not started. Of the 32 High cases, 11 are covered and 21 deferred.

- **Covered** means the case is met where the platform meets it, by a test
  that passes. Where that is narrower than the case as DOC-02 words it, the
  row says so.
- **Deferred** means the case belongs to a module the M0–M6 build plan does
  not contain, or needs a connection or a decision that is a human
  checkpoint. The row gives the reason and where the specifications place
  the work. A deferred case is not handled: nothing in the platform acts on
  it yet.
- The `Module` column is DOC-02's (M1–M12). The `Milestone` column is the
  CLAUDE.md task (M0–M6) that covers the case, or for a deferred case where
  the specifications place it.

| Priority | Count |
|---|---|
| High | 32 |
| Med | 32 |
| Low | 6 |
| **Total** | **70** |

| ID | Module | Edge case | Priority | Milestone | Test | Status |
|---|---|---|---|---|---|---|
| EC-01 | M1 | Provider moves practice mid-enrolment | Med | DOC-01 4.1, Phase 4 | No credentialing module: the platform holds no record of a provider's enrolment with a payer (plan product, effective dates, group or individual) to test a claim against | DEFERRED |
| EC-02 | M1 | Locum tenens billing | Med | DOC-01 4.1, Phase 4 | No credentialing module: the platform holds no record of a provider's enrolment with a payer (plan product, effective dates, group or individual) to test a claim against | DEFERRED |
| EC-03 | M1 | Group vs individual enrolment mismatch | Med | DOC-01 4.1, Phase 4 | No credentialing module: the platform holds no record of a provider's enrolment with a payer (plan product, effective dates, group or individual) to test a claim against | DEFERRED |
| EC-04 | M1 | Retroactive effective date → held claim backlog | High | DOC-01 4.1, Phase 4 | No credentialing module: the platform holds no record of a provider's enrolment with a payer (plan product, effective dates, group or individual) to test a claim against | DEFERRED |
| EC-05 | M1 | Enrolled with payer but not the plan product | High | DOC-01 4.1, Phase 4 | No credentialing module: the platform holds no record of a provider's enrolment with a payer (plan product, effective dates, group or individual) to test a claim against | DEFERRED |
| EC-06 | M2 | Two active plans, primary unclear | High | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-07 | M2 | Newborn on mother's ID | Med | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-08 | M2 | Name change mid-treatment | Low | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-09 | M2 | Expired card, active coverage | Med | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-10 | M2 | Self-pay with retroactive coverage | Med | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-11 | M2 | Minor, divorced parents, disputed coverage | Med | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-12 | M2 | Workers' comp mis-registered as commercial | High | SRS 15.7, Stage 5 | No registration module: claims arrive assembled in the PM's 837, and the platform holds no patient, coverage or guarantor record | DEFERRED |
| EC-13 | M3 | Retroactive termination | High | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against. The parser already keeps coverage as a period, so the date of service can be asked about once responses are stored (`unit/test_x12_acknowledgements.py::test_coverage_is_a_period_not_a_state`) | DEFERRED |
| EC-14 | M3 | Active but service excluded | High | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against | DEFERRED |
| EC-15 | M3 | Uninformative 271 | High | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against | DEFERRED |
| EC-16 | M3 | Medicare Advantage vs Medicare confusion | High | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against | DEFERRED |
| EC-17 | M3 | Medicaid as payer of last resort | Med | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against | DEFERRED |
| EC-18 | M4 | Retrospective auth after emergency | High | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-19 | M4 | Intra-operative procedure change | High | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-20 | M4 | Auth expires before rescheduled DOS | High | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-21 | M4 | Payer change mid-episode | Med | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-22 | M4 | Auth held by referring, not rendering provider | Med | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-23 | M4 | Authorised units exceeded | Med | SRS 15.8, Stage 6 | No prior-authorisation module: a claim carries an authorisation number (REF*G1) and nothing else about it -- no dates, units, procedure or holder | DEFERRED |
| EC-24 | M5 | Contradictory documentation | High | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-25 | M5 | Procedure documented, completion unclear | High | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-26 | M5 | Provider code conflicts with documentation | High | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-27 | M5 | Bilateral: modifier 50 vs two lines | Med | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-28 | M5 | Unlisted procedure requiring narrative | Med | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-29 | M5 | Documented but non-covered | Med | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-30 | M5 | Copy-forward inflating complexity | High | SRS 15.9, Stage 7 | Coding is out of scope: no clinical documentation enters the platform, and coding automation needs its own gate (CLAUDE.md sections 2 and 12, PD-17) | DEFERRED |
| EC-31 | M6 | Missing fee schedule entry | Med | SRS 15.7, Stage 5 | No charge-entry module: claims arrive priced and assembled, and the platform holds no fee schedule, surgical history or coverage history | DEFERRED |
| EC-32 | M6 | Service spanning midnight | Low | M1.5 | `unit/test_x12_claim.py::test_a_service_spanning_days_is_dated_from_its_first_day`. At ingestion only: an 837 line carrying a date range is read and dated from its first day. Charge entry's own choice of date and units needs DOC-02's M6, which is not built | COVERED |
| EC-33 | M6 | Global surgical period overlap | High | SRS 15.7, Stage 5 | No charge-entry module: claims arrive priced and assembled, and the platform holds no fee schedule, surgical history or coverage history | DEFERRED |
| EC-34 | M6 | Professional/technical component split | Med | SRS 15.7, Stage 5 | No charge-entry module: claims arrive priced and assembled, and the platform holds no fee schedule, surgical history or coverage history | DEFERRED |
| EC-35 | M6 | Insurance changed between DOS and billing | High | SRS 15.7, Stage 5 | No charge-entry module: claims arrive priced and assembled, and the platform holds no fee schedule, surgical history or coverage history | DEFERRED |
| EC-36 | M7 | Rule correct generally, wrong for one payer | High | M4.2 | `unit/test_rule_evaluation.py::test_a_rule_wrong_for_one_payer_leaves_that_payer_out`; scoping a rule to its payers: `::test_a_rule_scoped_to_another_payer_does_not_see_the_claim` | COVERED |
| EC-37 | M7 | NCCI edit with valid modifier bypass | High | M4.3 | `unit/test_ncci.py::test_a_valid_modifier_bypass_passes`; no bypass where the edit allows none: `test_a_bypass_is_no_use_when_the_edit_allows_none` | COVERED |
| EC-38 | M7 | Legitimate same-day duplicate (76/77) | Med | M4.3, M4.7 | `unit/test_ncci.py::test_a_repeat_procedure_on_its_own_line_is_not_flagged`, `::test_a_repeat_over_a_per_day_mue_goes_to_a_person`; `unit/test_auto_correction.py::test_deduplicating_modifiers_never_merges_a_repeat_procedure` | COVERED |
| EC-39 | M7 | Rule blocks claim near filing deadline | High | M3.6 | `integration/test_prioritisation.py::test_a_scrubber_block_near_the_deadline_is_pinned_first`; the rule itself: `unit/test_prioritisation_rules.py::test_deadline_beats_priority_score` | COVERED |
| EC-40 | M7 | Payer changes requirement overnight | High | M4.10 | `integration/test_payer_registry_entries.py::test_a_requirement_changed_overnight_is_what_the_next_scrub_reads`; an entry nobody re-verified cannot block: `::test_stale_payer_rule_cannot_block` | COVERED |
| EC-41 | M8 | Duplicate from retry | High | M3.7 | `integration/test_submission_outbox.py::test_a_retried_enqueue_with_the_same_key_is_one_submission`, `::test_a_sent_row_is_never_sent_again`, `::test_a_lost_response_goes_to_a_person_and_is_never_resent`; `security/test_outbox_constraints.py::test_one_idempotency_key_is_one_submission` | COVERED |
| EC-42 | M8 | Paper-only claim type | Low | With the first real clearinghouse | No paper channel: the only way out is the clearinghouse adapter, a mock. Needs a payer-registry fact for paper-only claim types and a print-and-mail step | DEFERRED |
| EC-43 | M8 | Attachment required | Med | With the first real clearinghouse | No attachment channel (X12 275, portal or fax): each is a real connection (CLAUDE.md section 12) | DEFERRED |
| EC-44 | M8 | Partial batch acceptance | Med | M1.6, M3 | `integration/test_claim_progression.py::test_a_partly_accepted_batch_moves_each_claim_on_its_own_verdict`; `integration/test_acknowledgement_loading.py::test_a_277ca_loads_a_verdict_per_claim`; a 999 partly accepted: `unit/test_x12_acknowledgements.py::test_partially_accepted_is_not_acceptance` | COVERED |
| EC-45 | M8 | Misrouted by payer ID ambiguity | Med | M1.7 | `integration/test_payer_registry.py::test_a_spelling_claimed_by_two_payers_raises_a_candidate`, `::test_an_unknown_name_resolves_to_nothing_rather_than_the_closest`, `::test_a_conflict_does_not_reassign_the_alias`. The platform's part: an ambiguous payer is never resolved by a guess. Routing inside a clearinghouse is not the platform's to test | COVERED |
| EC-46 | M9 | Bulk 835 across many claims | High | M1.2 | `integration/test_remittance_loading.py::test_two_transaction_sets_in_one_file_load_as_two_remittances`; full-corpus load in `integration/test_corpus_load.py` | COVERED |
| EC-47 | M9 | Recoupment against current payment | High | M1.10 | `integration/test_reversals.py::test_a_recoupment_links_to_the_payment_it_claws_back`, `::test_a_recoupment_creates_no_claim_and_no_negative_claim` | COVERED |
| EC-48 | M9 | Payment without 835 | Med | SRS 15.7, Stage 5 | No bank or EFT deposit feed: a deposit cannot be seen, so one with no 835 cannot be found. Needs deposit data to match on the trace number | DEFERRED |
| EC-49 | M9 | 835 without payment | Med | M1.3, M3 | `integration/test_claim_progression.py::test_the_full_path_ends_denied` (BR-M9-07: an 835 paying nothing is loaded for the denial it carries). Read as the zero-payment 835, as the generator does; an 835 whose funds never arrive needs deposit data, see EC-48 | COVERED |
| EC-50 | M9 | Interest payment | Low | M1.10 | `integration/test_reversals.py::test_an_interest_payment_is_not_a_recoupment` | COVERED |
| EC-51 | M9 | Capitated zero-payment lines | Med | M3, M4.8 | `integration/test_claim_progression.py::test_a_capitated_line_paid_nothing_is_not_a_denial`; `unit/test_denial_taxonomy.py::test_a_payment_adjustment_is_not_a_refusal` | COVERED |
| EC-52 | M9 | Paper EOB requiring OCR | Med | SRS 15.7, Stage 5 | No OCR intake for paper EOBs; its accuracy target [TBD-39] is undecided | DEFERRED |
| EC-53 | M9 | Control number reuse → misposting | High | M1.3 | `integration/test_remittance_loading.py::test_a_repeated_transaction_control_number_is_quarantined` | COVERED |
| EC-54 | M10 | 277 says paid, no funds | High | SRS 15.6, Stage 4 (4.3, 4.4) | No 276/277 status enquiry: sending a 276 needs a clearinghouse connection (CLAUDE.md section 12). Until then a 277 carrying this answer is stopped by name and gives no claim an outcome (`integration/test_acknowledgement_loading.py::test_a_status_enquiry_answer_is_not_read_as_a_verdict`) | DEFERRED |
| EC-55 | M10 | Not on file despite 277CA acceptance | High | M3 | `integration/test_claim_progression.py::test_a_claim_not_on_file_after_acceptance_goes_to_a_person` | COVERED |
| EC-56 | M10 | Repeated pending without reason | Med | SRS 15.6, Stage 4 (4.3, 4.4) | No 276/277 status enquiry: sending a 276 needs a clearinghouse connection (CLAUDE.md section 12). Until then a 277 carrying this answer is stopped by name and gives no claim an outcome (`integration/test_acknowledgement_loading.py::test_a_status_enquiry_answer_is_not_read_as_a_verdict`) | DEFERRED |
| EC-57 | M10 | Claim under appeal double-worked | Med | M5.7 | `integration/test_denial_appeal.py::test_a_claim_under_appeal_is_not_worked_a_second_time`, `::test_a_sent_appeal_is_not_sent_again` | COVERED |
| EC-58 | M10 | Balance below pursuit threshold | Low | M3.6 | `unit/test_prioritisation_rules.py::test_small_balances_go_to_bulk_handling`; `integration/test_prioritisation.py::test_bulk_work_is_kept_out_of_the_individual_queue`. The threshold is [TBD-46], a placeholder | COVERED |
| EC-59 | M11 | Generic CO-16 without specifying RARC | High | M4.8 | `integration/test_denial_classification.py::test_a_generic_co_16_is_classified_and_shows_what_the_payer_said`. The category is the one the CARC allows; a RARC does not refine it yet, a person does (ADR-0012) | COVERED |
| EC-60 | M11 | Auth denial despite valid auth | High | M4.9 | `integration/test_denial_feedback.py::test_an_auth_denial_on_a_claim_that_carried_one_drafts_no_rule` | COVERED |
| EC-61 | M11 | Denial contradicts prior eligibility response | High | DOC-01 4.1, Phase 1 | No eligibility module: a 271 is parsed but not stored (`integration/test_acknowledgement_loading.py::test_a_271_is_parsed_and_reported_but_not_stored`), so there is no response to hold a claim or a denial against | DEFERRED |
| EC-62 | M11 | Conflicting denial reasons on one claim | Med | M4.8 | `unit/test_denial_taxonomy.py::test_a_line_with_several_reasons_gets_exactly_one_category`, `test_a_tie_goes_to_the_earliest_reason` (ADR-0012 Decision 3) | COVERED |
| EC-63 | M11 | Appeal won, payment not received | Med | SRS 15.8, Stage 6 (ML-5) | Appeal outcomes are not recorded (ADR-0029, BR-M11-07), so a won appeal cannot be told from a pending one. A sent appeal does carry a follow-up date whatever its outcome (`integration/test_denial_appeal.py::test_signing_binds_to_the_exact_text_read`) | DEFERRED |
| EC-64 | M11 | Denial after filing window closed | High | Needs the owner's payer terms | The payer record holds the original filing term only. The windows for a corrected claim and for an appeal (BR-M11-03) are not recorded, so the Denial Workbench cannot say a correction is out of time | DEFERRED |
| EC-65 | M11 | Different denial on resubmission | Med | M1.9, M4.8 | `integration/test_denial_classification.py::test_a_resubmission_denied_for_another_reason_keeps_both_lessons`; `integration/test_denial_rate.py::test_a_claim_denied_at_every_attempt_counts_once` | COVERED |
| EC-66 | M12 | Patient overpayment → refund obligation | High | SRS 15.8, Stage 6 | No patient-billing module: no statement, patient payment or guarantor record exists. The one rule built is PD-19 (`integration/test_denial_patient_responsibility.py::test_co_group_never_billed_to_patient`) | DEFERRED |
| EC-67 | M12 | Deceased patient | Med | SRS 15.8, Stage 6 | No patient-billing module: no statement, patient payment or guarantor record exists. The one rule built is PD-19 (`integration/test_denial_patient_responsibility.py::test_co_group_never_billed_to_patient`) | DEFERRED |
| EC-68 | M12 | Disputed balance | Med | SRS 15.8, Stage 6 | No patient-billing module: no statement, patient payment or guarantor record exists. The one rule built is PD-19 (`integration/test_denial_patient_responsibility.py::test_co_group_never_billed_to_patient`) | DEFERRED |
| EC-69 | M12 | Minor with guarantor | Low | SRS 15.8, Stage 6 | No patient-billing module: no statement, patient payment or guarantor record exists. The one rule built is PD-19 (`integration/test_denial_patient_responsibility.py::test_co_group_never_billed_to_patient`) | DEFERRED |
| EC-70 | M12 | Retroactive Medicaid eliminating balance | Med | SRS 15.8, Stage 6 | No patient-billing module: no statement, patient payment or guarantor record exists. The one rule built is PD-19 (`integration/test_denial_patient_responsibility.py::test_co_group_never_billed_to_patient`) | DEFERRED |
