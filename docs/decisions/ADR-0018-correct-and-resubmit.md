# ADR-0018: Correct & resubmit sends a chained replacement, scrubbed and queued in one step

**Status:** Accepted · **Date:** 2026-11-08 · **Milestone:** M5 (M5.7 slice 3)

Builds on ADR-0006 (claim progression), ADR-0011 (auto-correction),
ADR-0016 and ADR-0017. Supersedes nothing.

## Context

- **UR-10:** "Correct & resubmit" shows a field-level diff of the proposed
  change before commit.
- **PD-7 / BR-DATA-03:** a resubmission chains to the claim it replaces and
  is never a new claim. `load_claim_file` is where chaining happens: REF*F8
  names the original's CLM01.
- **IR-06:** submission goes through the transactional outbox.
- Claim rows hold no patient demographics. A complete 837 can only be
  rebuilt from the archived file (PD-9).
- Progression (ADR-0006) treats any claim short of `Submitted` as "an 837 on
  file was sent" and walks it to `Submitted`.
- DOC-02 §7 has `Denied → Correcting → Charged`, which reuses the row, and
  ADR-0006 already has `Correcting → Replaced`.
- The owner asked (2026-11-06) for real-world choices to be made without
  waiting, and recorded so testers can revisit them.

## Decision 1: the replacement is a new chained row; the original waits in `Correcting`

A row is one submission (DOC-02 data model, `claim` docstring). Reusing the
denied row for the corrected claim would overwrite what the payer refused,
and with it the record the denial was classified against. So:

- the original moves `Denied → Correcting`, with a follow-up date
  (`submission_ack_followup_days`);
- the replacement is a new `claim` row, frequency 7, `original_claim_id` =
  the original, `submission_attempt` + 1;
- once the payer takes the replacement (a 277CA acceptance or an 835),
  progression moves the original `Correcting → Replaced`. If the replacement
  is rejected, the original is still the live claim and keeps its follow-up.

`Correcting → Charged` stays in the machine but is not used by denial
corrections.

## Decision 2: the corrected 837 is the archived one with only the edits changed

The archived 837 is read back and the stored row's codes are laid over it,
since the scrubber may have corrected them (ADR-0011). The operator's edits
are applied, CLM01 becomes `<original>-R<attempt>` (CLM01 is unique per
client and at most 38 characters), CLM05-3 becomes `7`, and REF*F8 names
the original. The file is loaded with `load_claim_file`, so the
replacement's own bytes are archived in RAW and chaining has one code path.

The fields that can be edited: member ID, prior authorisation number,
diagnosis codes, and per line the procedure code, modifiers, units and
diagnosis pointers. **Charges are never edited.** A different charge is a
different bill. Each field is checked by name: CPT/HCPCS shape, at most four
two-character modifiers, ICD-10-CM shape, one to twelve diagnoses, pointers
that point at a diagnosis, and units above 0. A correction that changes
nothing is refused.

## Decision 3: scrubbed and queued in the same transaction, or refused

If the replacement were left at `Charged`, progression would call it sent
without sending it. So it is walked `Registered → … → Charged` (each step
noted "carried from <original>": eligibility, the service and its coding
are the original's), then `Scrubbing`, and is scrubbed against the latest
ruleset:

- **BLOCK:** the whole correction is refused and the blocking findings are
  named. Nothing moves, and the operator corrects again.
- **The scrubber would auto-correct something:** also refused, naming the
  fix. What is sent must be what the operator previewed.
- **PASS / ADVISE:** `ReadyToSubmit`, then `outbox.enqueue` → `Submitted`.

The denial item resolves as `CORRECTED_AND_RESUBMITTED`. The diff masks the
member ID to its last four characters (S-6), and error reasons name the
field, not the values (PD-13). The audit event names the fields changed,
never their values. The values are in the two
837s in RAW.

## Decision 4: who may do it

`denial:disposition` (Denial Specialist, Ops Manager), the same as the other
dispositions. One decision at a time (ADR-0017): a correction is refused
while a write-off waits for approval or an appeal is in draft.

## Open, for testers' review

- **REF*F8 content.** Under 005010X222A1, REF*F8 on a replacement should
  carry the *payer's* claim control number (CLP07, shown on the workbench
  as the payer control number), not our CLM01. The loader chains on CLM01
  (PD-7, M1.9). Before a real clearinghouse connection, the loader must
  chain on either value and the builder must write the payer's number. Today
  only the mock adapter receives these files.
- **Coding changes by a denial specialist.** Real teams often send code
  changes (procedure, modifier, diagnosis) through a certified coder. Today
  the specialist may make them, and the scrubber still applies PD-16 to
  anything automatic. A "coder review" step could be added.
- **No scrubber preview.** The preview shows the diff only. A block, or a
  fix the scrubber would make itself, is found at resubmit, named, and
  nothing is changed. Running the scrub inside a rolled-back savepoint at
  preview time would show it earlier.
- **Wrong-member denials (CO-31).** If the payer has no claim under the
  corrected member, there is nothing to replace. In practice a member-ID fix
  for "patient not identified" goes out as a new claim (frequency 1), not a
  replacement. Today every correction is a frequency-7 replacement.
- **Payers that refuse frequency 7.** Medicare Part B does not take
  professional replacement claims. Fixes there go through reopening or
  redetermination. A per-payer "corrected claims accepted" setting in the
  payer registry should refuse those cases by name.
- **Units changed, charge kept.** Charges are never edited, so changing
  units changes the price per unit. Real teams usually send that to charge
  entry. It may be better to refuse unit changes here.
- **Corrected-claim deadlines.** Payers often allow less time for a
  corrected claim than for timely filing (PD-15). This is not tracked yet.
- **A rejected replacement.** The replacement is its own claim row. A 999 or
  277CA rejection moves it to `Rejected` and opens a rejection item, which
  is worked on that row while the original waits in `Correcting` with its
  follow-up. Progression still assumes any claim before `Submitted` was
  sent (ADR-0006), so a replacement fixed by hand and moved back to
  `Charged` needs the same scrub-and-outbox step before it is resent. That
  is a later slice.
- **Clearing a prior authorisation.** The API takes an empty string to
  remove one. The screen treats a blank field as "unchanged", so it cannot
  remove one yet.
- **Refused attempts leave RAW files.** The replacement 837 is archived
  before it is scrubbed. A refusal rolls back its `raw_file` row, but the
  content-addressed file stays on disk, unreferenced.

## Consequences

- Denial rate and volume stay per claim (BR-DATA-03): the replacement
  shares `root_claim_id`.
- A correction that fails any step (validation, load, scrub, outbox
  suspension) leaves no rows behind. The original stays `Denied` and the
  item stays open.
- Rollback: remove the two routes. The new module adds no tables and no
  migration.
