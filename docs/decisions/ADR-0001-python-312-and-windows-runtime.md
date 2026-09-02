# ADR-0001 — Python 3.12 pin and the Windows local runtime

- **Status:** Accepted
- **Date:** 2026-09-01
- **Context:** CLAUDE.md section 3, SRS section 4

## Context

The stack requires Python 3.12+. The build machine had 3.11.9 and 3.14.5 only.
3.14 is too new for parts of the stack: dbt-core and LightGBM (M2, M6) do not yet
publish wheels for it, and building them from source on Windows is a failure mode
we would inherit for the life of the project.

The machine also had no Docker, no WSL, no `make`, no Postgres and no Redis.

## Decision

1. Pin to **Python 3.12** (`requires-python = ">=3.12,<3.13"`), installed alongside
   the existing interpreters. The upper bound is deliberate: a silent jump to 3.13
   would change wheel availability for the ML stack without anyone deciding to.
2. Local runtime is **Docker Desktop on WSL2**, matching SRS section 4 and gate M0.2,
   so the local stack, CI, and the eventual deployment target run the same
   Postgres 16 and Redis 7.
3. `make` is GNU Make 4.4.1, installed to `C:\Users\manna\bin\make.exe`.
   The winget package `ezwinports.make` was unreachable (SourceForge blocked on this
   network), and `GnuWin32.Make` is 3.81, which predates the `.ONESHELL` and
   `$(file ...)` features the Makefile uses.

## Consequences

- Developers need Docker Desktop running before `make up` or the integration suite.
- CI (`ubuntu-latest`) uses service containers rather than Docker Desktop; the
  Postgres and Redis versions are pinned identically in both.
- Anyone joining on a machine without Docker cannot run the integration or security
  suites. That is intentional: those suites prove PD-2, PD-13 and PD-20 against a
  real database, and a mocked database would prove nothing.
