# ADR-0004 — Lint scope for the synthetic data generator

**Status:** Accepted
**Date:** 2026-10-21
**Context:** M0.10, CLAUDE.md section 8

## Context

CLAUDE.md section 3 sets mypy to strict with "no exceptions granted without an
ADR", and the Ruff configuration requires annotations (`ANN`) everywhere and
forbids `print()` (`T201`) on the grounds that all output must pass through the
PHI scrubber.

The synthetic data generator brought in under M0.10 is roughly two thousand
lines of existing, working code that produces 837P claims, 835 remittances and
999 acknowledgements. Bringing it to the repository's standard would mean
annotating every function and routing a console progress report through
structlog.

## Decision

`tools/synthetic/**` is exempted from `ANN`, `T201`, `E741`, `RUF001`, `N802`
and `SIM`. Every other rule, including the `DTZ` naive-datetime rules and `S`
(bandit), continues to apply, and the four naive `datetime.now()` calls found on
import were fixed rather than exempted.

mypy does not cover this code in either case: `packages = ["rcm"]` already
excludes `tools/`.

## Rationale

The rules being relaxed exist to protect properties this code does not have.

- **`T201` protects the PHI boundary.** The generator never sees PHI — it
  invents every patient it emits — and its output is a console report for a
  developer running `make seed`, not telemetry leaving the process. The control
  it would otherwise bypass has nothing to bypass.
- **`ANN` and mypy strict protect code where a type confusion writes a wrong
  number into a ledger.** A defect here costs a regenerated corpus. The
  asymmetry is the whole reason CLAUDE.md section 7.1 puts the 90% coverage floor
  on `decision`, `ingestion.x12` and `domain` and not on everything.
- **`E741` and `N802` are naming rules.** This code names its functions after the
  X12 segments they write (`ISA`, `GS`, `SE`) and its loop variables after the
  spec's own loop identifiers. Renaming them to satisfy a Python convention would
  make the code harder to check against the specification it implements, which is
  the only way anyone verifies it is right.

## Consequence that matters

This exemption is for the generator and nothing else. `src/rcm/ingestion/x12`,
the **parser**, is held to the full standard and to the 90% coverage floor. The
asymmetry is deliberate: a generator bug produces obviously wrong synthetic
files that the parser's own balance assertions reject, whereas a parser bug
silently corrupts the training data and every number computed from it
afterwards. CLAUDE.md section 5 says it outright — *"Parser bugs corrupt training
data silently and permanently. Slow down here."*

If this exemption is ever widened beyond `tools/synthetic/**`, that is a
different decision and needs its own ADR.
