# ADR-0008: NCCI edits are versioned reference data, checked in Layer 3

**Status:** Accepted · **Date:** 2026-11-03 · **Milestone:** M4 (M4.3)

## Context

SRS §8.2 puts NCCI edits in Layer 3 (CLINICAL). CMS publishes them
quarterly as two tables:

- procedure-to-procedure (PTP) pairs: a column-2 code is not paid with its
  column-1 code on the same date, unless the edit allows a bypass modifier
  and one applies;
- medically unlikely edits (MUE): the most units of a code payable per line
  or per date of service.

A pair test spans two lines, and an MUE test sums units across lines. The
condition language of ADR-0007 tests one line at a time, so it cannot
express either.

## Decision 1: built-in checks, run in their layer

NCCI is a built-in check: code that receives the claim's facts and returns
findings, run in Layer 3 after that layer's rules. `evaluate` takes the
checks beside the rules, so a backtest runs the same code as a live scrub.

| Edit | Finding when | Line flagged |
|---|---|---|
| PTP, modifier indicator `0` | both codes on one date | the column-2 line |
| PTP, indicator `1` | both codes on one date, and no NCCI-associated modifier on the column-2 line | the column-2 line |
| PTP, indicator `9` | never: CMS marks the edit not applicable | — |
| MUE, adjudication indicator `1` | one line's units exceed the limit | that line |
| MUE, indicators `2` / `3` | the units of the code on one date, summed, exceed it | the code's first line that date |

A finding with indicator `1` asks a person whether the services were
distinct. The system never adds the modifier: 25, 59 and X{EPSU} are
refused for automation (PD-16, DOC-02 BR-HITL).

An edit applies on a date of service from its effective date up to, but not
including, its deletion date. A line with no date of service, where the
claim has none either, is `INDETERMINATE` (ADR-0007 Decision 5).

NCCI findings are `BLOCKING`. They name the edit in a `reference` field, for
example `ncci-ptp PTP:2026Q1 45385/45380 mi=1`.

## Decision 2: a release is loaded whole, or not at all

`ncci_release` holds one row per loaded file: kind, label, the file's
SHA-256 and its row count. `ncci_ptp_edit` and `ncci_mue` hold the rows.

- A malformed row refuses the whole file, by row number. Nothing is partly
  loaded.
- Loading the same file again changes nothing.
- A different file under a label already loaded is refused.
- All three tables are insert-only by grant, like rulesets.

NCCI files are public CMS reference data, not inbound transactions, so they
are not put in the X12 raw archive (PD-9). The stored rows, with the
source's SHA-256, are the record.

## Decision 3: the ruleset pins the NCCI releases

Publishing a ruleset records the latest loaded PTP and MUE releases, and
they are part of its digest. A scrub uses exactly the releases its ruleset
names, so `ruleset-v<n>` still says everything that evaluated a claim
(FR-RULES-01). A ruleset published before any NCCI release is loaded has
none, and says so.

## Limits, for later

- Edits are checked within one claim. CMS applies PTP edits across claims
  from the same provider for the same patient and date. That needs a
  cross-claim view, which comes with the backtest harness (M4.4).
- Some commercial payers do not apply NCCI (EC-36). Payer exemptions come
  with the payer registry (M4.10).

## Rollback

`alembic downgrade 0017` while no ruleset names an NCCI release.
