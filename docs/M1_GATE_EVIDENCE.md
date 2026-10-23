# M1 gate evidence — X12 ingestion

CLAUDE.md section 5: *"Parser bugs corrupt training data silently and
permanently. Slow down here."*

This file covers M1.1 (tokenizer and envelope), M1.2 (835 parser with CAS
unrolling), M1.3 (balance assertion and quarantine), M1.4 (RAW archive before
parse), M1.5 (837 generator and parser), M1.8 (CORE schema and loaders) and
M1.9 (resubmission chaining) and M1.10 (reversals and recoupments). M1.6 and
M1.7 are not in this change.

---

## M1.1 — no uncaught exception on any input

The acceptance condition is a property, not a list of cases: *"Parsers: no
uncaught exception on any input"* (CLAUDE.md section 7.1). The reason it has to
be a property is DR-10 — a quarantined file must name the assertion it failed,
and an unnamed exception names nothing, so it cannot be quarantined.

Four Hypothesis strategies, 300 examples each:

| Strategy | What it reaches |
|---|---|
| arbitrary bytes | the entry checks |
| text from an EDI alphabet | delimiter detection, segment splitting |
| every slice of a valid file | truncation mid-CAS, mid-composite, between ST and SE |
| single-character corruption | one bad digit in an amount, a broken delimiter |

A 3,000-example probe recorded where they land:

```
{'parsed': 0, 'named_refusal': 3000, 'UNNAMED': 0}
```

**Control removed** — the `InvalidOperation` handler around money parsing
replaced with a bare `Decimal(text)`, the obvious "it's only a Decimal"
simplification:

```
E       decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>]
src/rcm/ingestion/x12/remittance.py:88: InvalidOperation
1 failed, 8 passed
```

Hypothesis found a corruption turning a valid amount into one `Decimal` cannot
read, and the unnamed exception escaped. **Restored:** `9 passed`.

---

## M1.2 — checked against a parser that shares no code

A parser agreeing with itself is not evidence. `tools/synthetic/validate.py`
re-reads the emitted text with an independent implementation written before
this one, so disagreement between them is a finding rather than a coincidence.

Every 835 in the corpus, compared file by file rather than in aggregate — two
offsetting errors cancel in a total:

```
files cross-checked : 211
claims              : 619
service lines       : 1025
adjustments         : 1304
multi-CAS lines     : 279
CAS groups          : {'CO': 1045, 'PR': 259}
unbalanced files    : 1 -> ['835_UNBALANCED_quarantine_test_100537.txt']
MISMATCHES          : 0
```

279 multi-CAS lines is the number that matters. A parser that keeps only the
first reason of each CAS segment passes every single-reason test in the suite
and fails only here.

### A discrepancy that was explained rather than tolerated

The first version of `test_parsed_totals_reconcile_exactly_with_the_generator`
failed: 1025 parsed service lines against 1018 in the manifest. The easy fix
was `>=`. Instead:

```
parsed, excluding the quarantine fixture : {'lines': 1018, 'adj': 1297, 'claims': 616}
the quarantine fixture alone             : {'lines': 7,    'adj': 7,    'claims': 3}
claim_lines.csv rows                     : 1018
```

The deliberately unbalanced file is written outside the normal claim flow — it
exists to be structurally wrong — so its rows never reach the CSV the manifest
counts. 1018 + 7 = 1025, and the parser was right. The test now excludes that
file explicitly and asserts exact equality on the rest, which is a stronger
check than the one that failed.

---

## A gate that had never run

Adding the first real code under `src/rcm/ingestion/x12` was supposed to
activate the 90% coverage floor from CLAUDE.md section 7.1. It reported:

```
skip  rcm/ingestion/x12        0 statements -- still a stub
```

while the same run printed 89–94% line coverage for the three modules in it.

`coverage.xml` writes filenames **relative to its `<source>` root**. With
`source = ["src/rcm"]` they read `ingestion/x12/tokenizer.py`, and
`tools/check_coverage_floor.py` was matching against `rcm/ingestion/x12`. It
had matched nothing since the day it was written, and said so in language that
read like a pass.

Fixed by resolving each filename through the report's own `<source>` elements.
Then the failure mode itself was made detectable: a package with code on disk
but zero statements in the report is now a **failure**, because "this package
is a stub" and "this script cannot find this package" look identical in a
coverage report and are opposite conclusions.

Both failure modes demonstrated:

```
# the original bug: a prefix that matches nothing, on a package that has code
FAIL  rcm/ingestion/x12_WRONGPATH has code on disk but 0 statements in coverage.xml.
      The coverage report and this script disagree about paths; the gate is not running.
exit=1

# genuine under-coverage: only the envelope tests run
FAIL  rcm/ingestion/x12         68.2%  (330/484)
exit=1

# the real suite
OK    rcm/ingestion/x12         95.7%  (463/484)
exit=0
```

---

## M1.4 — the archive is written before the parser runs

PD-9 is an ordering claim, so it is tested by breaking the thing that comes
second. `test_raw_archived_before_parse` feeds the loader bytes no parser can
read and then asserts the bytes are still retrievable from RAW:

```
load outcome     : QUARANTINED
assertion        : IR-01: file parses as X12
read_archived(...) == the original bytes
raw_file rows    : 1
remittance rows  : 0
```

The tempting shape — parse, and keep the file if it worked — loses exactly the
files worth keeping. The file that crashed the parser is the one nobody can
reproduce, and the payer will not resend it, because as far as they are
concerned they already did.

Archive writes are atomic and the result is immutable: a temporary file in the
destination directory, `fsync`, then `Path.replace`, then mode `0o444`. A
partial write therefore never appears under a content-addressed name, and
`test_nothing_partial_is_left_behind` asserts no stray temporary survives a
failure.

One case deliberately refuses to guess. Same hash, different byte length is not
a duplicate — it is either a hash collision or a corrupted read, and neither has
a safe default — so `archive()` raises `ComplianceError` rather than choosing
(`test_a_hash_collision_with_a_different_size_stops_rather_than_guesses`).

---

## M1.3 — unbalanced files quarantine whole, never partially

Two assertions can stop a load, and each names itself in `raw_file`, as DR-10
requires:

```
BR-M9-01: BPR02 == sum(CLP04) - sum(PLB)      the file's money must add up
IR-02:    SVC02 == SVC03 + sum(CAS)           each service line must reconcile
IR-04:    ST02 is unique within an interchange
```

`test_835_must_balance_or_quarantine` asserts the outcome is `QUARANTINED` and
that **zero** CORE rows were written — not "fewer rows", zero. A partial load is
worse than a rejection, because the rows that did land look like facts.

`RawFileStatus` has three members and deliberately lacks a fourth:

```python
ARCHIVED / LOADED / QUARANTINED      # and no PARTIALLY_LOADED
```

There is no status for a half-loaded file because there is no code path that can
produce one; adding the name would invite one.

---

## M1.8 — the constraints, each proven in both directions

A test can be deleted and a loader can be rewritten in a hurry. A `NOT NULL`
cannot be worked around by code that did not think about it. CLAUDE.md
section 7.5 asks for the negative to be proven, so each constraint was dropped
inside a transaction, the forbidden row re-attempted, and the transaction rolled
back — the schema was never left altered:

```
PROVEN     PD-3  cas_group_code NOT NULL
PROVEN     PD-3  CHECK cas_group_code_is_valid                       (group 'XX' refused)
PROVEN           CHECK reason_code_is_present                        (empty CARC refused)
PROVEN     PD-6  CHECK claim_level_adjustments_have_no_service_line
PROVEN     DR-10 CHECK quarantine_names_its_assertion
PROVEN     PD-14 UNIQUE uq_raw_file_content_hash
PROVEN     PD-8  FK remittance.payer_id -> payer

all constraints proven
```

Each line means: *with* the constraint the row was refused with an
`IntegrityError`, and *without* it the same row was accepted. A constraint that
cannot be shown to reject anything is decoration.

The PD-8 case took two attempts and the first result was wrong in an
instructive way. Dropping the foreign key did not let the bad row in, which
would read as "the constraint is redundant". The row was in fact being refused
by `uq_remittance_raw_file_id_transaction_control_number`, because the proof
reused one `raw_file` and left the control number at its default. The proof was
measuring the wrong constraint. Giving the second row its own control number
isolated the foreign key, which then behaved as claimed.

That detour exposed a genuinely untested path, described next.

### A bulk 835, which the corpus never produces

`transaction_control_number` is what separates two remittances inside one file,
and every one of the 211 corpus files carries exactly one transaction set:

```
transaction sets (ST*835) per file:  211 files x 1
```

So the multi-transaction shape — ordinary in real bulk remittances — was
supported by the parser and the schema and exercised by nothing.
`test_two_transaction_sets_in_one_file_load_as_two_remittances` now covers it,
asserting against the database rather than the parser, because the question is
whether CORE can hold the result: one `raw_file`, two `remittance` rows with
distinct control numbers, each carrying its own BPR02, and each claim attached
to the transaction set that paid it.

**Control removed** — the loader truncated to the first transaction set, the
plausible simplification for someone who has only ever seen single-set files:

```
>           assert totals["remittance"] == 2
E           assert 1 == 2
1 failed
```

**Restored:** `13 passed`, and `git diff` on the loader is empty.

### IR-04 — a bug this found, in the code about to ship

Covering the multi-set shape raised the next question: what does a file do if two
transaction sets *repeat* a control number? A payer could plausibly send one, and
ST02 is the only thing keeping the two remittances apart. Measured rather than
assumed:

```
two transaction sets, both ST02 = 0001

UNHANDLED : IntegrityError
DETAIL    : duplicate key value violates unique constraint
            "uq_remittance_raw_file_id_transaction_control_number"
  raw_file       0
  remittance     0
  claim_payment  0
```

`raw_file 0` is the serious part. The uniqueness violation escaped
`load_remittance_file` as a bare `IntegrityError`, and the rollback that
followed took the `raw_file` row with it. The bytes were on disk, in the
archive, with **nothing in the database recording that they existed or why they
had failed**. The file was neither loaded nor quarantined — PD-9 and DR-10 both
broken, PD-18's fail-closed-with-a-name broken too, and a batch loader would
have aborted on the file rather than setting it aside.

Fixed by asserting it where it can name itself, in the assert section ahead of
the balance checks, rather than discovering it as a database error during the
load:

```
OUTCOME   : QUARANTINED
ASSERTION : IR-04: ST02 is unique within an interchange
  raw_file       1
  remittance     0
  claim_payment  0
  status         [('QUARANTINED', 'IR-04: ST02 is unique within an interchange')]
```

**Control removed** — the IR-04 check deleted, leaving the database to catch it:

```
E       IntegrityError: duplicate key value violates unique constraint
        "uq_remittance_raw_file_id_transaction_control_number"
        [parameters: ... 'transaction_control_number': '0001' ...]
FAILED test_a_repeated_transaction_control_number_is_quarantined
```

**Restored:** `13 passed`.

This is the second time in M1 that writing a test for an untested *shape* —
rather than for a behaviour already believed correct — found a real defect. The
first was the 1025-vs-1018 reconciliation in M1.2.

---

## The M1 gate

> *"Ingest the full synthetic corpus. Every file balances or quarantines.
> Reprocessing the entire corpus changes nothing (idempotency). Report: files
> processed, rows loaded, quarantine count, and the reprocessing diff (must be
> empty)."*

Against a real PostgreSQL 16, as the application role:

```
files processed                 211
  pass 1  LOADED                210
  pass 1  QUARANTINED             1   BR-M9-01: BPR02 == sum(CLP04) - sum(PLB)
  pass 2  ALREADY_LOADED        210
  pass 2  QUARANTINED             1   (re-evaluated, still refused, still 0 rows)

rows after pass 1   raw_file 211  remittance 210  claim_payment 616
                    service_line 1018  adjustment 1297  provider_adjustment 6
rows after pass 2   identical
reprocessing diff   {}  (empty)
```

Every count reconciles with the generator's own manifest, which was written by
code that shares nothing with the loader:

| | loaded | manifest |
|---|---|---|
| claims | 616 | 616 |
| service lines | 1018 | 1018 |
| adjustment rows | 1297 | 1297 |
| provider adjustments | 6 | 6 (`recouped_claims`) |

The quarantined file is the one the generator writes to be structurally wrong,
and it contributed **0** rows (`test_the_quarantined_file_contributed_no_rows`).
There is a `raw_file` row for it, which is the point — the bytes are kept and
the reason is named.

Read back out of the database rather than out of the parser:

```
multi-reason service lines      279     PD-6, not collapsed
NULL cas_group_code               0     PD-3
CAS groups present        {'CO': 1038, 'PR': 259}
payers, keyed on identifier       8     PAYER00431 .. PAYER87726   PD-8
```

279 is the number that matters for PD-6: a loader that kept only the first
reason per CAS segment would pass every single-reason test in the suite and
fail only here. And the eight payers are keyed on the payer's own identifier —
`REF*2U`, else `REF*EV`, else `TRN03`, falling back to the interchange sender,
**never** the free-text name from `N1*PR`. An earlier draft of the loader keyed
on that name, which is a PD-8 violation that would have silently merged distinct
payers sharing a trading name.

---

## The coverage floor was measuring one suite

Adding the CORE models made the floor fail:

```
OK    rcm/ingestion/x12         95.6%  (473/495)
FAIL  rcm/domain                 0.0%  (0/138)
```

`rcm/domain` is almost entirely table definitions and constraints, exercised by
the integration and security suites against a real database. The floor ran in
the unit job, which has no database. So the gate was reporting 0% on code where
every statement is in fact covered:

```
unit + property only        rcm/domain    0.0%  (0/138)    FAIL
all four suites combined    rcm/domain  100.0%  (138/138)  OK
```

The threshold is still 90% and the guarded list is still the three packages
CLAUDE.md section 7.1 names. What changed is what gets measured: each suite now
writes its own coverage data file, and a `coverage-floor` job combines all four
before checking. Per-suite files also keep each run honest — pytest-cov erases
its own file at the start, so a deleted test stops counting instead of lingering
in an appended total.

The combined gate was checked for permissiveness, because a gate that was just
made easier to pass is the thing to distrust:

```
# partial data -- what the old arrangement measured
FAIL  rcm/domain                 0.0%  (0/138)        exit=1

# only the security suite's data
FAIL  rcm/ingestion/x12          0.0%  (0/495)
FAIL  rcm/domain                 0.0%  (0/138)        exit=1

# a data file missing entirely
::error::coverage data missing for: security           exit=1
```

That last one is a new guard in the job. Coverage data files are dotfiles, and
`actions/upload-artifact` skips dotfiles unless told otherwise — so without the
guard a renamed artifact would have left the floor silently measuring a subset
of the suites and passing.

### What this gate still cannot see

Worth recording, because the number is reassuring and should not be:

```
all four suites                           rcm/ingestion/x12  97.2%
all 45 dedicated X12 unit tests deleted   rcm/ingestion/x12  91.7%   still OK
```

Deleting every one of the parser's own unit tests leaves x12 above the 90% floor,
because the corpus load drives the parser end to end and incidentally touches
most of its lines. Line coverage is therefore a weak gate on the
highest-care package in the project: it would notice the parser losing *all* its
tests only if the integration suite went too.

Two things do currently catch that, and neither is coverage: `tools/run_suite.py`
fails if `tests/property` is empty while `src/rcm/ingestion/x12` has code, and
the independent cross-check in M1.2 compares the parser against
`tools/synthetic/validate.py` file by file. Raising the floor is not the fix —
CLAUDE.md fixes it at 90% — but if a stronger gate is wanted here, mutation
testing on `rcm/ingestion/x12` is the honest version of what coverage is being
asked to do, and that is a decision rather than something to slip in.

---

## M1.5 — the round trip, and what a round trip does not prove

The acceptance condition is *"generate → parse → equals source claim"*. Stated
as a property rather than a fixture, over arbitrary claims:

```
test_generate_parse_equals_source_claim            300 examples
test_the_round_trip_is_stable                      300 examples
test_the_resubmission_chain_survives_the_round_trip 300 examples
test_no_modifier_is_ever_added                     300 examples
```

`test_the_round_trip_is_stable` is the one that earns its place. A round trip
that converges only on the second pass is a round trip with a defect that
happens to cancel; asserting `build(parse(build(x))) == build(x)` catches an
element the generator writes and the parser ignores, which plain equality of
claims does not.

But a round trip is self-agreement, and both halves of one module can share a
misreading of the format and reproduce it perfectly. So the parser is also
checked against 325 files written by `tools/synthetic/x12.py` — written first,
sharing no code — against the CSVs that generator wrote from the claim objects
*before* serialising them:

```
claim object ---> X12 text ---> parsed claim
         \----> CSV row ----------^
```

```
files                   325      transactions          325
claims                  616      service lines        1018
diagnoses               972      modifiers             222
resubmissions            16      parse failures          0
round-trip failures       0      header mismatches       0
                                 line mismatches         0
```

Compared claim by claim and line by line, never in totals. Two offsetting
errors cancel in a sum, which the 835 corpus test learned the expensive way.
Every count equals the generator's manifest: 616 claims, 1018 lines, 16
resubmissions.

### No uncaught exception on any input

The same contract the 835 parser is held to (CLAUDE.md section 7.1), because an
unnamed exception cannot be quarantined — DR-10 requires a quarantined file to
name the assertion it failed.

```
EDI-shaped text, 3000 examples          named_refusal 3000   UNNAMED 0
slices of a valid generated 837, 3000   named_refusal 3000   UNNAMED 0
```

Zero accidental successes in either probe, which is what makes the numbers
worth printing: a fuzz suite that quietly parses a third of its inputs is
measuring something other than refusal.

### Nine controls, each removed once

```
PD-7   generator drops REF*F8                      CAUGHT
PD-7   parser ignores REF*F8                       CAUGHT
       diagnoses sorted instead of kept in order   CAUGHT
       delimiter in data no longer refused         CAUGHT
PD-18  dependent loop silently read as subscriber  CAUGHT
       N3/N4 dispatched on tag alone               CAUGHT
       modifiers sorted instead of transcribed     CAUGHT
       generator tidies modifier order             CAUGHT
       SE segment count off by one                 CAUGHT
```

The source was restored from a copy afterwards and `git diff` on it is empty.

### A test that could not fail

The modifier mutation did **not** fail on the first run:

```
       modifiers sorted instead of transcribed     NOT CAUGHT -- test is vacuous
```

Two reasons, both worth recording. The fixture asserting modifier order used
`HC:99213:25:59`, and `("25", "59")` sorts to itself, so a parser that sorted
its modifiers passed it. And the corpus could not cover the gap either:

```
corpus lines with >= 2 modifiers : 0
```

All 222 modifiers in the corpus sit alone on their line, so modifier *order* is
not exercised by the corpus at any volume. The fixture now uses `59:25:76`,
which does not sort to itself, and a second test carries that order through the
generator as well. Both mutations are caught.

This is the argument for prove-the-negative in one example. The test existed,
named the right invariant, passed, and verified nothing.

### Two findings from reading rather than running

**`PRV*BI` is not the rendering provider.** The first corpus comparison
reported 19 taxonomy mismatches, which looked like a parser defect. It was the
comparison that was wrong: `PRV*BI*PXC` sits in loop 2000A and carries the
*billing* provider's taxonomy, written once per file, while
`claim_headers.csv` records a per-claim `taxonomy_code` — the *rendering*
provider's specialty, which belongs in loop 2310B (`PRV*PE`) and which the
synthetic generator does not emit. Rendering-provider specialty is therefore
not recoverable from these files at all, and anything needing it must read the
CSV. `test_taxonomy_is_the_billing_providers_not_the_rendering_providers`
asserts both halves of that, including that the CSV genuinely disagrees, so
that if the corpus ever gains loop 2310B the test fails and the comparison can
be tightened.

**X12 has no escape mechanism.** A `*` inside a surname ends the element, and
every element after it shifts by one. The file stays syntactically valid and
describes a different claim, so nothing downstream raises. The only two options
are to strip the character or to refuse; the generator refuses, because
silently rewriting a patient's name to make a file well formed is a worse
outcome than a submission that stops and says why.

```
test_a_delimiter_in_data_is_refused_not_silently_corrupted   300 examples
```

### Refused rather than guessed

An `HL*23` dependent loop — the patient is someone other than the subscriber —
raises the new `UnsupportedX12Error` rather than being read with the
subscriber's demographics. The result of guessing would be a well-formed claim
about the wrong patient, which no later stage could detect because nothing
about it looks wrong.

The error is deliberately distinct from `MalformedX12Error`: the two mean
opposite things to whoever receives the alert. A malformed file is the sender's
defect and the fix is to ask them to resend; an unsupported one is our gap.

Adding it immediately failed a gate built in M0:

```
AssertionError: undocumented error codes: ['unsupported-x12']
AssertionError: docs/errors.md is stale. Regenerate:
      python -m tools.gen_error_docs > docs/errors.md
```

Which is the error taxonomy requirement (M0.6) doing exactly its job on the
first new error class added since.

### Shared element parsers

`parse_amount`, `parse_date` and the optional forms moved out of the 835 parser
into `rcm/ingestion/x12/fields.py`. The 837 parser needs the identical reading
of money and dates, and a second implementation that rounded or defaulted
differently would produce a reconciliation failure that looks like a payer
problem. `parse_quantity` is new and returns `Decimal` rather than `int`,
because SV104 is legitimately fractional — anaesthesia minutes and drug units —
and reading it as an integer bills 1.5 units of a drug as 1.

---

## M1.9 — the number BR-DATA-03 exists to protect

    BR-DATA-03 (MUST): Resubmissions MUST be chained to the original claim,
    not stored as independent claims. Failure to do this inflates volume and
    distorts the denial rate.

Measured on the full corpus, both transaction types loaded under one client:

```
submissions   616        denied submissions   107        17.37%
claims        600        denied claims         91        15.17%
```

The wrong number is 2.2 points higher than the right one — a 14% relative
overstatement, in the direction that makes the operation look worse than it is.
Both figures are honest counts of real rows. Nothing downstream can tell them
apart, which is why this is a schema problem rather than a reporting one.

Resubmitting a denied claim does not deny it twice. `DenialRate` reports both
grains and names the wrong one `per_submission`, because anyone reconciling
against a previous vendor's numbers needs it to explain the difference, and
someone looking at it labelled is far less likely to recompute it by accident
than someone who does not know it exists.

### The chain is stored twice, deliberately

`original_claim_id` is the edge BR-DATA-03 and SRS section 5.2 both describe.
`root_claim_id` is that chain already resolved, so counting distinct claims is
`COUNT(DISTINCT root_claim_id)` rather than a recursive CTE.

The redundancy buys the thing that matters: a query that forgets the chain
returns the same answer as one that remembers. The alternative leaves every
future counting query one forgotten CTE away from reporting submissions, and
that query will look right. Same trade already made for `client_id` under
PD-20 — a correctness property should not depend on every future author
remembering to write a join.

Three CHECK constraints stop the two representations drifting, each proven by
dropping it inside a transaction, re-attempting the forbidden row, and rolling
back, so the schema was never left altered:

```
PROVEN   PD-7     CHECK claim_does_not_replace_itself
PROVEN   PD-7     CHECK root_agrees_with_the_chain (original rooted elsewhere)
PROVEN   PD-7     CHECK root_agrees_with_the_chain (resubmission rooted at itself)
PROVEN   PD-7     CHECK attempt_agrees_with_the_chain
PROVEN   PD-14    UNIQUE uq_claim_client_submitter_id
PROVEN   PD-7     FK claim.original_claim_id -> claim
PROVEN   BR-M6-01 CHECK line_has_a_diagnosis_pointer
PROVEN            CHECK units_are_positive
PROVEN            CHECK procedure_code_is_present
```

A resubmission rooted at itself is the shape that matters: it counts as a
separate claim in exactly the query meant to collapse it.

### A constraint that was not constraining

`line_has_a_diagnosis_pointer` was first written as
`array_length(diagnosis_pointers, 1) >= 1`. On an empty array
`array_length(x, 1)` returns **NULL**, a CHECK that evaluates to NULL passes,
and the constraint silently permitted exactly the row it existed to forbid:

```
E       Failed: DID NOT RAISE IntegrityError
FAILED test_a_line_must_carry_a_diagnosis_pointer
```

`cardinality()` returns 0 for an empty array and the constraint now fires. The
test caught it only because it asserts the refusal rather than assuming it —
a test that had merely inserted a good row would have passed all the way to
production.

### Two bugs the loader tests found

**A quarantined file still wrote rows.** The first loader validated and
inserted in one pass, so a quarantine partway through left the claims added
before it pending in the session. The file ended up marked `QUARANTINED` with
CORE rows behind it — the partial load DR-10 exists to prevent, wearing the
right status.

**A resubmission could not see its original in the same file.** The session
runs with `autoflush=False`, deliberately: PD-2 depends on nothing reaching the
database outside an explicit transaction boundary. So a claim added moments
earlier was invisible to the `SELECT` that looks for the parent, and a file
carrying both an original and its resubmission quarantined under IR-05.

Both are fixed by the same change: resolve and validate every claim in the file
*before* writing any row, consulting the batch's own planned claims before the
database. One pass that plans, one that writes.

### An unknown original is refused, not guessed

A `REF*F8` naming a claim this platform has not loaded quarantines the file
under `IR-05` rather than loading it as an original. Treating the orphan as a
new claim is the tempting recovery and it is precisely the failure BR-DATA-03
names: volume rises, the denial rate moves, and every number stays internally
consistent.

Files do legitimately arrive out of order, so the recovery matters: the bytes
are in RAW, reprocessing is idempotent, and re-presenting the file once the
original has landed loads it normally.
`test_an_out_of_order_file_succeeds_once_its_original_arrives` asserts that
round trip. A quarantine that resolves itself on retry is a far better failure
than a wrong number nobody can see.

### What counts as a denial

DOC-02's glossary: *"Payer refusal to pay a line or claim, with a reason
code."* So a line is denied when the payer paid nothing on it **and** gave a
reason — both halves. A line paid at zero with no reason code is not an
adjudicated denial, and a reason code on a line that was still paid is an
adjustment, not a refusal.

That rule reproduces the generator's own `is_denied` flag exactly, with no
disagreement in either direction:

```
denied lines by this rule   135     generator's is_denied = Y   135
claims by this rule         107     generator                   107
not-denied lines            883     of which paid > 0           883
                                    of which carry a CARC         0
```

It is a structural signal, not a taxonomy. Classifying *why* a claim was denied
and whether it was preventable is M4.8's work; this only answers "how often",
on the right denominator.

### Nine controls, each removed once

```
PD-7   chain dropped entirely                        CAUGHT
PD-7   root set to the parent, not the parent's root CAUGHT
IR-05  orphan loaded as a new claim                  CAUGHT
       same-file chain resolved from the database    CAUGHT
IR-07  charge reconciliation dropped                 CAUGHT
IR-08  diagnosis pointer assertion dropped           CAUGHT
       denial rate counts submissions                CAUGHT
PD-20  client scope dropped from the denial rate     CAUGHT
       a reason code alone counts as a denial        CAUGHT
```

`git diff` on both mutated files is empty afterwards.

### Two more tests that could not fail

Two of those nine passed under mutation on the first run, and both were the
test's fault rather than the control's.

**The contractual-adjustment test.** It asserted that a reason code on a paid
line is not a denial — but its fixture emitted no `CAS` segment at all on a
paid line, so it was really asserting that a line with *no* adjustment is not a
denial, which nothing was in danger of getting wrong. The fixture now has a
`partial` outcome: charged 200, paid 150, 50 written off under CO-45.

**The client-scoping test.** With the two clients using different claim ids,
the join to `claim_payment` excluded the other client's rows anyway, so
dropping the client filter on `claim` changed nothing. Both clients now use
`CLM1` — which is also the realistic case, two practices each numbering their
claims from 1 — and the control becomes observable.

That is three vacuous tests found across M1.5 and M1.9 by the same method. Each
one existed, named the right invariant, passed, and verified nothing.

---

## M1.10 — money moving backwards

Two mechanisms, and they are not the same event:

```
reversal     CLP02 = 22. The payer re-adjudicates a claim it already paid and
             sends a later remittance negating the original posting. The
             claim's outcome changed retroactively.
recoupment   A PLB entry with reason WO. The adjudication stands; the payer
             reduces a *later* cheque to recover the money (EC-47).
```

Both reopen the claim. Only one changes its outcome, which is why they are
counted apart rather than summed: a reversal means the adjudication was wrong
and the claim may need reworking, a recoupment means it stood and the cash
moved. Collapsing them loses the only thing that tells an operator which.

    BR-M9-04 (MUST): Reversals (claim status 22) MUST reverse the original
    posting and re-open the claim, not create a new negative claim.

Both are matched on the payer's own control number, which DOC-02 singles out
for exactly this: *"The payer's claim number (CLP07) is how you match
resubmissions and reversals. Store it."* It is the only handle that survives a
resubmission, where the submitter's claim id changes and the payer's does not.

### The corpus had no reversals at all

CLAUDE.md section 8 requires the synthetic generator to produce *"reversals,
recoupments"*, and `tools/synthetic/__init__.py` says it does. It never had:

```
grep -rn "revers" tools/synthetic/    -> a docstring, a reference CSV row,
                                         and an unrelated reversed(payload)
CLP02 = 22 in the corpus              -> 0
config keys                           -> recoupment_rate, no reversal_rate
```

So M1.10's headline requirement had no volume coverage, and every reversal
test would have been a fixture test — the same weakness recorded against
modifier order in M1.5, where the corpus turned out to contain no line with
two modifiers.

The generator now emits reversals as separate, later 835 files with the
original's amounts mirrored. The mirroring is what keeps them valid: `-charge
== -paid + -adjustments` holds exactly when the original held, so each file
balances under BR-M9-01 and each line under IR-02 without special-casing.

```
reversal files        8        all parse, all balance
CLP02                 22       on every one
BPR02                 negative on every one
CSV-derived counts    unchanged: 616 claims, 1018 lines, 1297 adjustments
```

The independent validator was taught about them too, and now *requires* them:

```
check(reversal_files > 0, "no reversal files - BR-M9-04 would be untested at volume")
```

That check is the one that would have caught the original gap. Reversal rows
are excluded from the CSV reconciliation for the same reason the quarantine
fixture is — a reversal re-states a claim the CSVs already counted once, so
folding it in double-counts every reversed claim, which is the arithmetic
error BR-DATA-03 forbids arriving from the remittance side.

### At corpus volume

```
claim_payment rows        624
  of which reversals        8      all 8 linked to the posting they reverse
provider_adjustment         6
  recoupments (WO)          6      all 6 linked to the payment they claw back
claims reopened            14      8 reversed + 6 recouped

claim rows                616      unchanged by M1.10
distinct claims           600      unchanged
negative claims             0      BR-M9-04
```

"No new negative claim" is structural rather than remembered: the remittance
loader never writes a `claim` row at all. Claims come from 837s. The test
asserts it anyway, because the next person to touch this will not know that.

### A latent bug in the denial rate

A reversal's amounts are the original's negated, and **zero negates to zero**.
A line paid nothing originally is still paid nothing once negated, and its
adjustments come back negated too — so to a query reading service lines, a
reversal is indistinguishable from a denial. The corpus has exactly one such
line:

```
reversal lines with paid = 0 and an adjustment : 1
claims only reachable through that posting     : 1
denial rate before the fix                     : 15.17%
denial rate after                              : 15.17%
```

The rate does not move, because that line belongs to a claim already counted
as denied. That is coincidence, not safety. The fix excludes reversal postings
from the denied set, and the test that proves it had to be rewritten twice
before it could fail — see below.

### Eight controls, each removed once

```
BR-M9-04  reversal link dropped                        CAUGHT
          reversal matched on claim id, not CLP07      CAUGHT
          reversals not excluded when finding original CAUGHT
PD-20     reversal links across clients                CAUGHT
EC-47     recoupment link dropped                      CAUGHT
          any PLB reason counts as a recoupment        CAUGHT
          reversal postings counted as denials         CAUGHT
          generator stops emitting reversals           CAUGHT
```

And the constraints, dropped inside a transaction and rolled back:

```
PROVEN  BR-M9-04 CHECK posting_does_not_reverse_itself
PROVEN  BR-M9-04 CHECK only_a_reversal_reverses_a_posting
PROVEN  BR-M9-04 FK claim_payment.reverses -> claim_payment
PROVEN  EC-47    FK provider_adjustment.recoups -> claim_payment
```

Alembic does not compare CHECK constraints, so both were written into the
migration by hand. Declared on the model alone they would have lived in the
ORM and never reached the database — the shape of defect M1.9 found in
`line_has_a_diagnosis_pointer`. They were also named bare rather than with the
`ck_<table>_` prefix: the metadata's naming convention adds it, and passing
the full name produced `ck_claim_payment_ck_claim_payment_...`, truncated with
a hash, matching nothing the model declares.

### Four more tests that could not fail

Half the mutations passed on the first run. Every one was the test's fault.

| Test | Why it could not fail |
|---|---|
| matched on CLP07 | Payment and reversal shared a claim id, so matching on either column passed. Now the reversal carries the *resubmitted* id and only the control number can link them. |
| reversals excluded from the search | While the original payment is present it is also the earliest posting, so ordering alone picks it. A new test drops the payment entirely: without the exclusion the second reversal links to the first, reading as "this reversal was itself reversed", which is a re-payment. |
| interest is not a recoupment | The interest PLB carried no reference, so it was rejected for being unreferenced rather than for its reason code. It now names a claim in PLB03-2. |
| reversal is not a denial | Both postings were loaded, so the claim was already counted and counting it twice changed nothing. The reversal now arrives for a claim whose original remittance was never loaded — which is what happens whenever history starts mid-stream — so the reversal is the only evidence. |

Seven vacuous tests have now been found this way across M1.5, M1.9 and M1.10.
Every one named the right invariant, passed, and verified nothing. The pattern
is consistent enough to be worth stating plainly: a test written alongside the
code that satisfies it tends to assert the shape of that code rather than the
rule, and the only reliable way to tell is to break the code and watch.

---

## Full gate at this commit

```
lint                 OK
format               76 files already formatted
mypy strict          no issues found in 32 source files
module boundaries    3 contracts kept, 0 broken
unit                 180 passed
property              17 passed
integration           66 passed   (against a real Postgres 16)
security              73 passed   (against a real Postgres 16, as rcm_app)
coverage floor       rcm/ingestion/x12  98.1%  (912/930)    rcm/domain  100.0% (196/196)
migrations           alembic check clean; 0004 and 0005 both downgrade and
                     re-apply cleanly, grants intact
prime directives     PD-16, PD-17, S-11, T20 all OK
later-stage guard    no un-wired stage
bandit               no medium or high findings
```

Invariant tests from CLAUDE.md section 7.2 satisfied across M1 so far:

```
test_cas_group_code_required                tests/security/test_core_constraints.py
test_multi_cas_adjustments_not_merged       tests/security/test_core_constraints.py
test_service_line_grain_not_collapsed       tests/integration/test_remittance_loading.py
test_835_must_balance_or_quarantine         tests/integration/test_remittance_loading.py
test_raw_archived_before_parse              tests/integration/test_remittance_loading.py
test_reprocessing_835_is_idempotent         tests/integration/test_remittance_loading.py
test_resubmission_chains_to_original        tests/unit/test_x12_claim.py
test_modifier_25_never_auto_applied         tests/unit/test_x12_claim.py
test_modifier_59_never_auto_applied         tests/unit/test_x12_claim.py
test_resubmission_chains_to_original        tests/integration/test_claim_loading.py
test_denial_rate_excludes_resubmissions     tests/integration/test_denial_rate.py
test_reversal_reopens_original_claim        tests/integration/test_reversals.py
test_reprocessing_a_reversal_is_idempotent  tests/integration/test_reversals.py
```

Both PD-7 invariants are now satisfied. `test_resubmission_chains_to_original`
appears twice on purpose: once at the parser, where it asserts `REF*F8`
survives reading, and once at CORE, where it asserts the chain reaches the
database. The first passing while the second failed is exactly the state M1.5
left the platform in.

### Still open

- A quarantined file re-presented is re-evaluated and quarantined again rather
  than reporting `ALREADY_QUARANTINED`. It writes no rows and creates no second
  `raw_file`, so the idempotency requirement holds, but the outcome is less
  informative than it could be.
- `transaction_control_number` is `NOT NULL` with a `''` server default, which
  exists only so hand-written SQL in tests need not supply it. The collision it
  used to invite is now caught by IR-04 before the database sees it, and ST02
  is mandatory in X12 with `SE02 == ST02` already asserted, so the default is
  unreachable from a parseable file — but it is still a default standing in for
  a value the loader always supplies.
- 837 dependent loops (`HL*23`) are refused rather than read. Loop 2010CA and
  the 2310 rendering-provider loops are both unparsed, so rendering provider,
  referring provider and service facility are not available from an 837 yet.
  None of the corpus needs them; a real payer's files will.
- `claim.practice_identifier` and `subscriber_member_id` are text rather than
  foreign keys. The SRS schema references `practice(practice_id)` and
  `patient(patient_id)`; neither table exists until M3, so adding those keys is
  an expand-migrate step once it does.
- The denial signal is structural — paid nothing, with a reason code. It
  reproduces the generator's flag exactly, but classifying *why* a claim was
  denied, and whether it was preventable, is M4.8.
- Claims and remittances are joined on the claim number the submitter sent and
  the payer echoed in CLP01. Reversals and recoupments match on CLP07 instead,
  which is the stronger key; the claim-to-remittance join has not been moved
  onto it, so a resubmitted claim still matches by the id it was sent under.
- `CR` (correction and reversal) remains the one CAS group code that is valid
  and never appears in a loaded row. Reversals mirror the original
  adjustments under their original group codes, which is the common payer
  convention; emitting `CR` instead would have lit up the code path by
  inventing payer behaviour.
- A claim whose only posting is a reversal counts as adjudicated for the
  denial-rate denominator. BR-STATE-05 says final outcome metrics should wait
  `[TBD-50]` days post-DOS to allow for reversals, and that maturity gate is
  M2.6 rather than something to approximate here.
- The corpus chains only two deep. Three-deep chains are covered by fixtures
  (`test_three_submissions_of_one_claim_count_as_one_claim`), not by volume.
- Modifier order is unexercised by the corpus, which contains no line with two
  modifiers. Fixtures cover it; volume does not.
- `rcm_app` can still write `alembic_version` (recorded at the M0 gate, in
  `docs/M0_GATE_EVIDENCE.md`). Unchanged by this milestone.

