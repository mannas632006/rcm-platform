# ADR-0003 — Packages live under `src/rcm/`, not directly under `src/`

- **Status:** Accepted
- **Date:** 2026-09-01
- **Context:** CLAUDE.md section 4 (repository layout)
- **Deviation from CLAUDE.md:** yes — reported at the M0 gate

## Context

CLAUDE.md section 4 specifies `src/platform/`, `src/ingestion/`, `src/domain/`, and so on.
Taken literally with a src-layout package root, that makes the first module
importable as `platform` — **which is the name of a Python standard library module.**

This does not merely risk shadowing; it fails outright. `sys.path` places the
standard library ahead of site-packages and ahead of the `.pth` entry an editable
install adds. So `import platform` resolves to the stdlib module, and
`from platform.config import Settings` raises `ImportError`. Meanwhile any
dependency doing `import platform` for OS detection — structlog and celery both do —
would, under a different path order, silently get ours instead.

A failure that depends on `sys.path` ordering is exactly the class of silent,
environment-dependent bug this project is built to avoid.

## Decision

One package root: `src/rcm/`. Every module named in CLAUDE.md section 4 keeps its name
directly beneath it.

```
src/rcm/platform/     ->  rcm.platform
src/rcm/ingestion/x12/ -> rcm.ingestion.x12
src/rcm/domain/       ->  rcm.domain
src/rcm/decision/     ->  rcm.decision
src/rcm/work/         ->  rcm.work
src/rcm/api/          ->  rcm.api
src/rcm/workers/      ->  rcm.workers
src/rcm/analytics/    ->  rcm.analytics
```

## Rationale

Considered and rejected:

- **`src/platform/` as specified.** Broken, per above.
- **Rename the module** (`src/core/`, `src/platform_/`). Preserves the flat layout but
  discards a name the specification uses throughout. CLAUDE.md section 10 says not to coin
  synonyms for specified terms.
- **`import src.platform`.** Works, but makes `src` the distribution package and puts
  a non-domain word into every import line in the codebase.

The chosen option keeps all twelve module names exactly as specified, adds one
level of nesting, and gives import-linter clean contract targets (`rcm.platform`,
`rcm.ingestion`, ...) for the M0.9 boundary rule.

CLAUDE.md section 13 says that where its guidance conflicts with the SRS on *how to
build*, CLAUDE.md wins — and that where it cannot be told which is which, ask.
This is neither: it is a correctness defect in a layout diagram, not a design
disagreement. It is recorded here rather than silently absorbed.

## Consequences

- Imports read `from rcm.platform.config import Settings`.
- `.importlinter` contracts name `rcm.*` packages.
- The directory tree is one level deeper than the CLAUDE.md section 4 diagram. Anyone
  comparing the two will find this ADR.
