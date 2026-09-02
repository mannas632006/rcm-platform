# Edge Case Register — coverage tracker

Satisfies **NFR-TEST-02** (SRS §14): every edge case in `docs/02_Domain_and_Functional.md`
§13 has a corresponding test case, tracked to completion.

Priority is taken from **DOC-02 §13**, which is authoritative. CLAUDE.md §7.3 lists 31
High-priority cases; DOC-02 marks 32. The difference is **EC-40**, resolved in favour of
DOC-02 — see [ADR-0002](../docs/decisions/ADR-0002-edge-case-priority-discrepancy.md).

**Status values:** `NOT_STARTED` · `COVERED` (test exists and passes) ·
`DEFERRED` (with a reason and a linked milestone — never a bare skip; CLAUDE.md §7.5).

| Priority | Count |
|---|---|
| High | 32 |
| Med | 32 |
| Low | 6 |
| **Total** | **70** |

| ID | Module | Edge case | Priority | Milestone | Test | Status |
|---|---|---|---|---|---|---|
| EC-01 | M1 | Provider moves practice mid-enrolment | Med | — | — | NOT_STARTED |
| EC-02 | M1 | Locum tenens billing | Med | — | — | NOT_STARTED |
| EC-03 | M1 | Group vs individual enrolment mismatch | Med | — | — | NOT_STARTED |
| EC-04 | M1 | Retroactive effective date → held claim backlog | High | — | — | NOT_STARTED |
| EC-05 | M1 | Enrolled with payer but not the plan product | High | — | — | NOT_STARTED |
| EC-06 | M2 | Two active plans, primary unclear | High | — | — | NOT_STARTED |
| EC-07 | M2 | Newborn on mother's ID | Med | — | — | NOT_STARTED |
| EC-08 | M2 | Name change mid-treatment | Low | — | — | NOT_STARTED |
| EC-09 | M2 | Expired card, active coverage | Med | — | — | NOT_STARTED |
| EC-10 | M2 | Self-pay with retroactive coverage | Med | — | — | NOT_STARTED |
| EC-11 | M2 | Minor, divorced parents, disputed coverage | Med | — | — | NOT_STARTED |
| EC-12 | M2 | Workers' comp mis-registered as commercial | High | — | — | NOT_STARTED |
| EC-13 | M3 | Retroactive termination | High | — | — | NOT_STARTED |
| EC-14 | M3 | Active but service excluded | High | — | — | NOT_STARTED |
| EC-15 | M3 | Uninformative 271 | High | — | — | NOT_STARTED |
| EC-16 | M3 | Medicare Advantage vs Medicare confusion | High | — | — | NOT_STARTED |
| EC-17 | M3 | Medicaid as payer of last resort | Med | — | — | NOT_STARTED |
| EC-18 | M4 | Retrospective auth after emergency | High | — | — | NOT_STARTED |
| EC-19 | M4 | Intra-operative procedure change | High | — | — | NOT_STARTED |
| EC-20 | M4 | Auth expires before rescheduled DOS | High | — | — | NOT_STARTED |
| EC-21 | M4 | Payer change mid-episode | Med | — | — | NOT_STARTED |
| EC-22 | M4 | Auth held by referring, not rendering provider | Med | — | — | NOT_STARTED |
| EC-23 | M4 | Authorised units exceeded | Med | — | — | NOT_STARTED |
| EC-24 | M5 | Contradictory documentation | High | — | — | NOT_STARTED |
| EC-25 | M5 | Procedure documented, completion unclear | High | — | — | NOT_STARTED |
| EC-26 | M5 | Provider code conflicts with documentation | High | — | — | NOT_STARTED |
| EC-27 | M5 | Bilateral: modifier 50 vs two lines | Med | — | — | NOT_STARTED |
| EC-28 | M5 | Unlisted procedure requiring narrative | Med | — | — | NOT_STARTED |
| EC-29 | M5 | Documented but non-covered | Med | — | — | NOT_STARTED |
| EC-30 | M5 | Copy-forward inflating complexity | High | — | — | NOT_STARTED |
| EC-31 | M6 | Missing fee schedule entry | Med | — | — | NOT_STARTED |
| EC-32 | M6 | Service spanning midnight | Low | — | — | NOT_STARTED |
| EC-33 | M6 | Global surgical period overlap | High | — | — | NOT_STARTED |
| EC-34 | M6 | Professional/technical component split | Med | — | — | NOT_STARTED |
| EC-35 | M6 | Insurance changed between DOS and billing | High | — | — | NOT_STARTED |
| EC-36 | M7 | Rule correct generally, wrong for one payer | High | — | — | NOT_STARTED |
| EC-37 | M7 | NCCI edit with valid modifier bypass | High | — | — | NOT_STARTED |
| EC-38 | M7 | Legitimate same-day duplicate (76/77) | Med | — | — | NOT_STARTED |
| EC-39 | M7 | Rule blocks claim near filing deadline | High | — | — | NOT_STARTED |
| EC-40 | M7 | Payer changes requirement overnight | High | — | — | NOT_STARTED |
| EC-41 | M8 | Duplicate from retry | High | — | — | NOT_STARTED |
| EC-42 | M8 | Paper-only claim type | Low | — | — | NOT_STARTED |
| EC-43 | M8 | Attachment required | Med | — | — | NOT_STARTED |
| EC-44 | M8 | Partial batch acceptance | Med | — | — | NOT_STARTED |
| EC-45 | M8 | Misrouted by payer ID ambiguity | Med | — | — | NOT_STARTED |
| EC-46 | M9 | Bulk 835 across many claims | High | — | — | NOT_STARTED |
| EC-47 | M9 | Recoupment against current payment | High | — | — | NOT_STARTED |
| EC-48 | M9 | Payment without 835 | Med | — | — | NOT_STARTED |
| EC-49 | M9 | 835 without payment | Med | — | — | NOT_STARTED |
| EC-50 | M9 | Interest payment | Low | — | — | NOT_STARTED |
| EC-51 | M9 | Capitated zero-payment lines | Med | — | — | NOT_STARTED |
| EC-52 | M9 | Paper EOB requiring OCR | Med | — | — | NOT_STARTED |
| EC-53 | M9 | Control number reuse → misposting | High | — | — | NOT_STARTED |
| EC-54 | M10 | 277 says paid, no funds | High | — | — | NOT_STARTED |
| EC-55 | M10 | Not on file despite 277CA acceptance | High | — | — | NOT_STARTED |
| EC-56 | M10 | Repeated pending without reason | Med | — | — | NOT_STARTED |
| EC-57 | M10 | Claim under appeal double-worked | Med | — | — | NOT_STARTED |
| EC-58 | M10 | Balance below pursuit threshold | Low | — | — | NOT_STARTED |
| EC-59 | M11 | Generic CO-16 without specifying RARC | High | — | — | NOT_STARTED |
| EC-60 | M11 | Auth denial despite valid auth | High | — | — | NOT_STARTED |
| EC-61 | M11 | Denial contradicts prior eligibility response | High | — | — | NOT_STARTED |
| EC-62 | M11 | Conflicting denial reasons on one claim | Med | — | — | NOT_STARTED |
| EC-63 | M11 | Appeal won, payment not received | Med | — | — | NOT_STARTED |
| EC-64 | M11 | Denial after filing window closed | High | — | — | NOT_STARTED |
| EC-65 | M11 | Different denial on resubmission | Med | — | — | NOT_STARTED |
| EC-66 | M12 | Patient overpayment → refund obligation | High | — | — | NOT_STARTED |
| EC-67 | M12 | Deceased patient | Med | — | — | NOT_STARTED |
| EC-68 | M12 | Disputed balance | Med | — | — | NOT_STARTED |
| EC-69 | M12 | Minor with guarantor | Low | — | — | NOT_STARTED |
| EC-70 | M12 | Retroactive Medicaid eliminating balance | Med | — | — | NOT_STARTED |
