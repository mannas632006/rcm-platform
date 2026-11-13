# ADR-0030: The edge-case register: what is covered, what is deferred, and why

**Status:** Accepted · **Date:** 2026-11-12 · **Requirement:** NFR-TEST-02

## Context

- NFR-TEST-02 (SRS 14): every edge case in DOC-02 section 13 has a test case,
  tracked to completion in `tests/EDGE_CASES.md`.
- The register was last touched at M4. It showed 6 cases covered and 64 not
  started, though tests written since covered several more.
- DOC-02 names each case in a sentence and does not say what the platform
  must do about it. What "covered" means had to be decided.
- The build plan (CLAUDE.md section 5, M0–M6) contains DOC-02's modules M7
  to M11 (scrubbing, submission, posting, follow-up, denials) and the X12
  parsers. It does not contain M1 to M6 (credentialing, registration,
  eligibility, prior authorisation, coding, charge entry) or M12 (patient
  billing). 40 of the 70 cases belong to those seven modules.

## Decision

### Covered means met, and shown

A case is `COVERED` only when the platform meets it somewhere and a passing
test shows what it does there. Where that is narrower than the case as
DOC-02 words it, the register row says so. 22 cases are covered.

### Deferred means not handled

A case whose module is not built, or that needs a real connection or a
sponsor's decision, is `DEFERRED`. The row gives the reason and where the
specifications place the work. Nothing in the platform acts on a deferred
case. 48 cases are deferred, 21 of them High.

| Why deferred | Cases | Count |
|---|---|---|
| No credentialing module (DOC-02 M1) | EC-01 to EC-05 | 5 |
| No registration module (M2) | EC-06 to EC-12 | 7 |
| No eligibility module (M3): 271s are parsed, not stored | EC-13 to EC-17, EC-61 | 6 |
| No prior-authorisation module (M4) | EC-18 to EC-23 | 6 |
| Coding is out of scope (M5; CLAUDE.md sections 2 and 12) | EC-24 to EC-30 | 7 |
| No charge-entry module (M6) | EC-31, EC-33, EC-34, EC-35 | 4 |
| No patient-billing module (M12) | EC-66 to EC-70 | 5 |
| Needs a real channel: paper, attachments | EC-42, EC-43 | 2 |
| Needs data the platform does not receive: bank deposits, paper EOBs | EC-48, EC-52 | 2 |
| No 276/277 status enquiry (SRS Stage 4, tasks 4.3 and 4.4) | EC-54, EC-56 | 2 |
| Appeal outcomes are not recorded (ADR-0029) | EC-63 | 1 |
| Payer terms the registry does not hold | EC-64 | 1 |

### Readings chosen where a case could be read two ways

- **EC-49, "835 received without payment".** Read as the zero-payment 835,
  which BR-M9-07 requires to be processed and the generator emits under this
  id. The other reading, an 835 whose funds never arrive, needs deposit data
  and is EC-48's reason for deferral.
- **EC-45, misrouting on payer ID ambiguity.** The platform's part is that
  an ambiguous payer is never resolved by a guess. Routing inside a
  clearinghouse cannot be tested here.
- **EC-38, a repeat procedure (76/77).** A line-edit MUE reads each line
  alone, so a repeat on its own line passes. A date-of-service MUE counts
  the day's units whatever the modifier, so a repeat past it goes to a
  person. Nothing merges the lines or removes the modifier.
- **EC-59, CO-16 with no RARC.** The denial takes the category CARC 16
  allows (ADR-0012) and the line keeps the remark codes the payer sent. A
  RARC does not refine the category; a person does.
- **EC-32, a service spanning days.** Covered at ingestion only: the line is
  read and dated from its first day. Which date and how many units to bill
  is charge entry's, and is not built.

### Two changes to behaviour

Both were found by writing the tests.

1. **An 837 line with a date range was refused (EC-32).** `DTP*472*RD8` is
   how a service running past midnight is sent. The parser read a range on
   the claim and refused one on a line, which quarantined the whole file.
   It now dates the line from the range's first day, as it does the claim.
2. **An authorisation denial on a claim that carried an authorisation
   drafted a rule that could not have caught it (EC-60).** The candidate for
   the category asks whether an authorisation is on the claim, and one was.
   The feedback loop now records NO_RULE with that reason (FR-RULES-07), and
   the denial is in the workbench to appeal. The loop's logic version is
   `feedback-v2`. The synthetic corpus has no such denial (the generator
   sends CARC 197 only when no authorisation is on the claim), so the GATE
   M4 measurements are unchanged.

## Open questions for the sponsor

- **EC-64.** Each payer's window for a corrected claim and for an appeal
  (BR-M11-03). With them the Denial Workbench can say a correction is out
  of time and track appeal deadlines. Without them it cannot.
- **EC-54, EC-56.** Whether to build the 277 status answers (paid, pending,
  not found) against the mock clearinghouse now, ahead of a real connection.
- **EC-63.** Where an appeal's outcome comes from (ADR-0029's question).

## Consequences

- The register has no case left `NOT_STARTED`. It is honest about the 48 it
  does not handle.
- The modules the deferred cases wait for are outside M0–M6. Building any of
  them is new scope with its own gate.
- **Rollback:** revert the commit. No migration. Decisions recorded under
  `feedback-v2` stay readable; the version on each says which logic made it.
