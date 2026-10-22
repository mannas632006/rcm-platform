# M1 gate evidence — X12 ingestion

CLAUDE.md section 5: *"Parser bugs corrupt training data silently and
permanently. Slow down here."*

This file covers M1.1 (tokenizer and envelope), M1.2 (835 parser with CAS
unrolling), M1.3 (balance assertion and quarantine), M1.4 (RAW archive before
parse), M1.5 (837 generator and parser) and M1.8 (CORE schema and loaders).
M1.6, M1.7, M1.9 and M1.10 are not in this change.

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

## Full gate at this commit

```
lint                 OK
format               76 files already formatted
mypy strict          no issues found in 32 source files
module boundaries    3 contracts kept, 0 broken
unit                 180 passed
property              17 passed
integration           22 passed   (against a real Postgres 16)
security              54 passed   (against a real Postgres 16, as rcm_app)
coverage floor       rcm/ingestion/x12  97.9%  (900/919)    rcm/domain  100.0%
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
```

`test_denial_rate_excludes_resubmissions` is the remaining PD-7 invariant. It
needs a denial rate to exclude them from, so it lands with M1.9 where the chain
reaches CORE.

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
- Parsed claims do not reach CORE. There is no claim table yet — M1.9
  (resubmission chaining) and M3 (claim lifecycle) are where `original_claim_id`
  stops being a field on a parsed object and becomes a foreign key.
- Modifier order is unexercised by the corpus, which contains no line with two
  modifiers. Fixtures cover it; volume does not.
- `rcm_app` can still write `alembic_version` (recorded at the M0 gate, in
  `docs/M0_GATE_EVIDENCE.md`). Unchanged by this milestone.

