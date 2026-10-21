# M1 gate evidence — X12 ingestion

CLAUDE.md section 5: *"Parser bugs corrupt training data silently and
permanently. Slow down here."*

This file covers M1.1 (tokenizer and envelope) and M1.2 (835 parser with CAS
unrolling). M1.3 onward are not in this change.

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

## Full gate at this commit

```
lint                 OK
format               63 files already formatted
mypy strict          no issues found in 26 source files
module boundaries    3 contracts kept, 0 broken
unit                 121 passed
property             9 passed
security             42 passed   (against a real Postgres 16)
integration          2 passed    (against a real Postgres 16)
coverage floor       rcm/ingestion/x12  95.7%
prime directives     PD-16, PD-17, S-11, T20 all OK
bandit               no medium or high findings
```

### Still open

- `read_remittances` filters by ST01, so an 837-only interchange returns an
  empty tuple rather than an error. That is correct for a caller asking for
  remittances and wrong for a loader that was handed the file it did not
  expect; the loader (M1.4) is where that distinction belongs.
- Claim-level CAS is parsed and kept separate from line-level, but nothing yet
  consumes it. The CORE schema in M1.8 is where it stops being inert.
