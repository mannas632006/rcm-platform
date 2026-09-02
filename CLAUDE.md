# CLAUDE.md — RCM Automation Platform Build Guide

> **This file is the operating contract for the coding agent.**
> Save it as `CLAUDE.md` in the repository root. Claude Code reads it automatically on every session.
> The full specification lives in `docs/03_SRS.md`. This file tells you *how to work*; the SRS tells you *what to build*.

---

## 0. Read this first

You are building a **medical billing (RCM) automation platform**. It processes US healthcare insurance claims. The data is Protected Health Information (PHI) and the output moves real money.

Three consequences that govern everything you do:

1. **Silent failure is the enemy.** A bug that logs an error is recoverable. A bug that quietly writes a wrong number is a compliance event. When uncertain, raise a named exception. Never substitute a default.
2. **Every automated decision must be explainable later.** Not "the model decided" — which rule version, on what inputs, producing what output, at what time.
3. **You are building on synthetic data only.** Real PHI is never present in any environment you touch.

### Scope of your work

The SRS describes a system that takes 18–24 months and requires external commercial relationships. **You are not building all of it.**

You are building **Milestones M0–M6 below**: the complete platform running end-to-end on synthetic data, with all external systems behind mock adapters. That is a genuinely working system — it simply has mocks where a clearinghouse contract will later go.

Anything requiring a credential, a contract, a signature, or real patient data is a **HUMAN CHECKPOINT**. Stop and ask.

---

### Files you need in `docs/`

| File | Why you need it |
|---|---|
| `03_SRS.md` | **Required.** What to build: architecture, schema, screens, requirements. |
| `02_Domain_and_Functional.md` | **Required.** Domain rules, the 12 modules, denial taxonomy, and the 70 edge cases you must test. The SRS assumes this exists. |
| `01_Business_and_Strategy.md` | Optional. Context on sequencing and why the wedge was chosen. |

If `02_Domain_and_Functional.md` is absent, say so before starting — you cannot satisfy NFR-TEST-02 without it.

---

## 1. Prime directives — non-negotiable

These are invariants. They are not preferences, and they are not to be traded away for convenience or speed. If a task appears to require violating one, stop and ask.

| # | Directive | Enforcement |
|---|---|---|
| **PD-1** | Every decision writes a `decision_record` with logic version, inputs, output. | Test asserts every decision path writes one |
| **PD-2** | Every PHI read writes an `audit_event`, in the same transaction as the change. | Test asserts transactional coupling |
| **PD-3** | `cas_group_code` is `NOT NULL` alongside every CARC. | DB constraint + test |
| **PD-4** | Rejections and denials are distinct outcome types. Never conflated. | Enum + test |
| **PD-5** | Deterministic rules always override model output on conflict. | Test with contrived conflict |
| **PD-6** | Analytical grain is one row per service line per adjustment. Never collapsed. | Test on multi-CAS fixture |
| **PD-7** | Resubmissions chain to `original_claim_id`. Never stored as new claims. | Test on resubmission fixture |
| **PD-8** | Payer identity keys on `payer_id`, never free-text name. | No `payer_name` column exists as a key |
| **PD-9** | Raw inbound files archived immutably **before** parsing. | Test asserts write-then-parse order |
| **PD-10** | All layers rebuildable from RAW + code. No hidden state. | Reconstruction test |
| **PD-11** | Features must be knowable at submission time. No leakage. | Automated leakage suite, blocks CI |
| **PD-12** | Train/test splits are temporal, never random. | Test asserts split boundary |
| **PD-13** | No PHI in logs, errors, metrics, or exception payloads. | Scrubber + test with PHI-laden payload |
| **PD-14** | Every mutating endpoint accepts `Idempotency-Key`. Retries never duplicate. | Retry test per endpoint |
| **PD-15** | Timely-filing deadline overrides all other prioritisation. | Test: low-value expiring claim outranks high-value fresh one |
| **PD-16** | Modifier 25 and 59 are **never** auto-applied. | Test asserts refusal |
| **PD-17** | No auto-submit code path for AI-assigned codes exists in the repo. | Grep-based CI check |
| **PD-18** | System fails closed. On uncertainty: do not submit, escalate. | Test per decision point |
| **PD-19** | CO-group amounts never appear on a patient statement. | Test asserts exclusion |
| **PD-20** | Client scoping enforced at the query layer, not the handler. | Test: handler without filter still cannot leak |

**When you violate a prime directive in code review of your own work, revert. Do not patch around it.**

---

## 2. What you must NOT do

Stop and ask the human if a task requires any of these:

- Procuring or configuring a real clearinghouse, EHR, or payer connection
- Handling, importing, or generating anything resembling real patient data
- Deploying to a production environment
- Implementing the coding auto-submit path (SRS FR-ML-12 — it must not exist yet)
- Resolving any of `DECISION-01` … `DECISION-08` (SRS §16) — those are the sponsor's calls
- Adding a cloud service that stores PHI without confirming BAA coverage
- Committing any credential, key, or connection string
- Weakening a test to make a build pass
- Marking a task done with failing tests or skipped assertions

---

## 3. Technology stack — decided, do not re-litigate

```
Backend      Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
Database     PostgreSQL 16+  (OLTP and analytics both — no separate warehouse yet)
Queue        Redis + Celery (+ Celery Beat for schedules)
Transforms   dbt-core
Frontend     TypeScript (strict), React 18, Vite, TanStack Query, TanStack Table,
             Zustand, Tailwind, shadcn/ui, React Hook Form + Zod, Recharts
ML           LightGBM, scikit-learn, MLflow, SHAP   (M6 only)
RPA          Playwright (Python)                     (mock only in this scope)
Testing      pytest, Hypothesis, Vitest, React Testing Library, Playwright
Quality      Ruff, mypy (strict), ESLint, tsc, pre-commit
Infra        Docker Compose locally; Terraform skeleton only
CI           GitHub Actions
```

**Rejected — do not introduce:** Kubernetes, microservices, MongoDB as primary, GraphQL, Node backend, any generic BPM engine, an LLM as a primary decision-maker.

---

## 4. Repository layout

```
.
├── CLAUDE.md                     # this file
├── docs/
│   ├── 03_SRS.md                 # the specification — source of truth for WHAT
│   ├── decisions/                # ADRs, one file per decision
│   └── runbooks/
├── src/
│   ├── platform/                 # config, db, auth, audit, errors, logging
│   ├── ingestion/
│   │   ├── x12/                  # tokenizer, envelope, 835, 837, 271, 277 parsers
│   │   ├── ehr/                  # connector interface + mock
│   │   └── pm/                   # connector interface + mock
│   ├── domain/                   # patient, eligibility, authorisation, coding,
│   │                             # claim, submission, remittance, denial, ar, billing
│   ├── decision/                 # rules engine, payer registry, ml, prioritisation
│   ├── work/                     # queues, exceptions, assignment, sla
│   ├── api/                      # FastAPI routers, schemas, dependencies
│   ├── workers/                  # Celery tasks
│   └── analytics/                # dbt project, feature builders
├── frontend/
│   ├── src/
│   │   ├── api/                  # GENERATED from OpenAPI — never hand-edited
│   │   ├── components/           # primitives + domain components
│   │   ├── screens/
│   │   ├── hooks/
│   │   └── lib/
├── tests/
│   ├── unit/  integration/  e2e/  fixtures/  property/  security/
├── tools/
│   └── synthetic/                # synthetic data generator (built in M0)
├── migrations/
├── infra/
└── .github/workflows/
```

**Module boundary rule:** modules talk through published service interfaces. No cross-module table access. Enforced by an import-lint rule in CI — write that rule in M0, do not defer it.

---

## 5. Build plan

Work milestones **in order**. Each has a gate. Do not begin the next milestone until the gate passes and you have reported the result.

### M0 — Foundation and safety rails (do this first, completely)

Nothing else works without this. Resist the urge to build features first.

| Task | Acceptance |
|---|---|
| M0.1 Repo, tooling, pre-commit, Ruff, mypy strict, ESLint, tsc | `make lint` passes on empty project |
| M0.2 Docker Compose: Postgres + Redis + app | `make up` gives a working local stack |
| M0.3 FastAPI skeleton, config, DB session, Alembic | `/health` returns 200; a migration runs |
| M0.4 `audit_event` + `decision_record` tables, append-only | App DB role has no UPDATE/DELETE on them — test proves it |
| M0.5 Structured logging with PHI scrubber | Test: log a payload with SSN/DOB/name → assert absent from output |
| M0.6 Error taxonomy + RFC 7807 handlers | Every error class maps to a documented response |
| M0.7 Auth: OIDC stub, RBAC, client scoping at query layer | Test: user scoped to client A cannot read client B by any route |
| M0.8 Idempotency middleware | Test: same key twice → one effect |
| M0.9 Import-lint rule for module boundaries | Test: a cross-module import fails CI |
| M0.10 **Synthetic data generator** (see §8) | Produces valid 837 + 835 pairs with realistic CARC mix |
| M0.11 CI pipeline with all gates from §7 | Pipeline green on skeleton |

**GATE M0:** `make verify` passes. PHI scrubbing, append-only audit, client scoping, and idempotency each have a passing test that fails if you remove the feature. Report the test names.

---

### M1 — X12 ingestion (the highest-care work in the project)

Parser bugs corrupt training data silently and permanently. Slow down here.

| Task | Acceptance |
|---|---|
| M1.1 X12 tokenizer + envelope parser | Hypothesis property tests: round-trip, malformed input never crashes uncaught |
| M1.2 **835 parser** — loop 2100 (CLP), loop 2110 (SVC), CAS unrolling | One row per claim per line per CAS reason. Multi-CAS fixture proves it. |
| M1.3 Balance assertion + quarantine | Unbalanced file → quarantined + named alert, never partially loaded |
| M1.4 RAW archive before parse | Test asserts ordering; hash dedup prevents reprocessing |
| M1.5 837 generator + parser | Round-trip: generate → parse → equals source claim |
| M1.6 271 / 277 / 999 / 277CA parsers | Fixture-driven |
| M1.7 Payer normalisation: `payer` + `payer_alias`, fuzzy match, review queue | "BCBS TX" / "Blue Cross Texas" / "BCBSTX" resolve to one `payer_id` |
| M1.8 CORE schema + loaders | Constraints enforce PD-3, PD-6, PD-8 |
| M1.9 Resubmission chaining | Test: 3 submissions of one claim count as 1 claim, denial rate correct |
| M1.10 Reversal / recoupment handling | Paid claim reversed months later reopens, does not create negative claim |

**GATE M1:** Ingest the full synthetic corpus. Every file balances or quarantines. Reprocessing the entire corpus changes nothing (idempotency). Report: files processed, rows loaded, quarantine count, and the reprocessing diff (must be empty).

---

### M2 — Warehouse, marts and the leakage suite

| Task | Acceptance |
|---|---|
| M2.1 dbt project, staging → core → marts | `dbt build` green, all tests pass |
| M2.2 `mart_claim_line_outcome` per SRS §5.5 | Every column present and typed |
| M2.3 Point-in-time feature builders | Trailing windows computed as-of submission timestamp only |
| M2.4 **Leakage assertion suite** | For every feature: assert it is knowable pre-submission. Suite blocks CI. |
| M2.5 Temporal split utility | Random split raises an error — it is not an option in the API |
| M2.6 Outcome maturity gate | Rows younger than the window excluded from final-outcome metrics |
| M2.7 Reconstruction test | Drop marts → rebuild from RAW → byte-identical |

**GATE M2:** Leakage suite passes and **demonstrably fails** when you deliberately add a leaking feature. Show me that failure output. Reconstruction test green.

---

### M3 — Claim lifecycle and work queues

| Task | Acceptance |
|---|---|
| M3.1 Claim state machine, transitions declared as data | Undefined transition rejected; every defined one tested |
| M3.2 Transactional state changes with row locking | Concurrent transition test |
| M3.3 Work item lifecycle + exclusive assignment locks | Two operators cannot hold one item |
| M3.4 Lock expiry and return to queue | Test with clock control |
| M3.5 Exception manager: named, owned, SLA-bound | Generic error messages fail review |
| M3.6 Prioritisation engine (heuristic, no ML yet) | PD-15 test: expiring low-value claim outranks fresh high-value one |
| M3.7 Clearinghouse adapter interface + **mock** + circuit breaker | Primary fails → secondary; both fail → named exception, no data loss |

**GATE M3:** Full synthetic claim lifecycle runs end to end: created → scrubbed → submitted → acknowledged → remitted → posted or denied. No claim ever rests in a non-terminal state without a next action date.

---

### M4 — Rules engine

This is the product. Build it carefully.

| Task | Acceptance |
|---|---|
| M4.1 Rule representation (YAML/JSON in DB), versioned | Every evaluated claim records ruleset version |
| M4.2 Six-layer evaluation pipeline per SRS §8.2 | Layer order fixed and tested |
| M4.3 NCCI edit loading and evaluation | Fixture-based |
| M4.4 Backtest harness | Reports evaluated / flagged / TP / FP / FP-rate |
| M4.5 Rule lifecycle: draft → backtest → shadow → advisory → blocking | Promotion without backtest is impossible via API |
| M4.6 FP-rate threshold enforcement | Rule above threshold cannot be BLOCKING |
| M4.7 Auto-correct limited to deterministic fixes | PD-16 test: modifier 25 auto-apply refused |
| M4.8 Denial classification + taxonomy + preventability | Every denial maps to one category |
| M4.9 **Feedback loop**: preventable denial → candidate rule | Test: inject denial → candidate rule appears |
| M4.10 Payer registry with `last_verified_date` and staleness | Stale entry cannot drive a BLOCKING decision |

**GATE M4:** Replay the synthetic denial corpus. Show measured first-pass clean-claim rate before and after rules are active. Demonstrate the feedback loop closing on at least one denial pattern.

---

### M5 — Operator UI

Build screens in this order. Each must have loading / empty / error / **partial** / success states.

| Task | Acceptance |
|---|---|
| M5.1 App shell, routing, auth, generated API client | Client is generated from OpenAPI — hand-edited types fail CI |
| M5.2 Design tokens per SRS §10.3 | No hard-coded colours outside the token file |
| M5.3 `WorkQueueTable` virtualised | 10,000 rows scroll without lag (measured) |
| M5.4 Keyboard model per SRS §10.9 | Every mouse action reachable by keyboard; `]`/`[` advance without returning to list |
| M5.5 Claim Detail incl. **Decision Audit panel** | Every automated decision visible with version and inputs |
| M5.6 `DenialCodeChip` | Structurally impossible to render a CARC without its group code |
| M5.7 Denial Workbench + diff preview + appeal draft | Appeal cannot be sent without human sign-off |
| M5.8 Scrubber exception and payment exception queues | Named findings, not generic failures |
| M5.9 Rule Builder with backtest results | Promote disabled until backtest passes; disabled state states why |
| M5.10 Payer Registry with staleness flags | |
| M5.11 Dashboards, segmentable by payer and client | |
| M5.12 Partial-state handling | Model service down → claim still workable |

**GATE M5:** Playwright E2E covering the five highest-volume journeys. Axe accessibility scan passes WCAG 2.1 AA. Report both.

---

### M6 — ML subsystem (only after M4 is stable)

Do not start until the rules engine is working and the leakage suite is proven.

| Task | Acceptance |
|---|---|
| M6.1 MLflow, training pipeline, model registry | Data hash + code commit recorded per run |
| M6.2 ML-1 denial risk (LightGBM) | Must beat the rules-only baseline or be rejected |
| M6.3 ML-2 denial category, ML-3 recovery, ML-5 appeal success | |
| M6.4 SHAP drivers persisted per prediction | PD-1 satisfied for model decisions |
| M6.5 Confidence thresholds as versioned config | Hard-coded threshold fails CI |
| M6.6 Shadow deployment harness | Model output recorded, not used, comparison reported |
| M6.7 Drift monitoring: PSI, calibration, coverage | |
| M6.8 **Auto-revert to rules-only on critical drift** | Test: simulate drift → model path disables itself unattended |
| M6.9 Graceful degradation | Missing features → rules path, never a guessed prediction |

**GATE M6:** Demonstrate auto-revert firing under simulated drift, without human intervention. **ML-6 (coding) is out of scope — do not build it.**

---

## 6. Security pipeline

Security is a set of CI gates, not a phase at the end.

### 6.1 Controls to implement (M0 unless noted)

| ID | Control |
|---|---|
| S-1 | TLS everywhere; no plaintext transport even locally |
| S-2 | Encryption at rest for DB and object storage |
| S-3 | OIDC auth; MFA assumed at the IdP |
| S-4 | RBAC per SRS §11.5 role matrix |
| S-5 | Row-level client scoping **at the query layer** |
| S-6 | Field-level masking — PHI fields hidden unless the role requires them |
| S-7 | Every PHI access writes an `audit_event` |
| S-8 | Secrets only from a secret manager; none in code, env files, or images |
| S-9 | PHI scrubber on logs, metrics, error payloads |
| S-10 | Append-only audit tables enforced by DB grants |
| S-11 | Parameterised queries only — string-built SQL fails review |
| S-12 | X12 parsed in isolation with size and nesting limits (untrusted input) |
| S-13 | Rate limiting on all endpoints |
| S-14 | Session timeout, configurable |
| S-15 | Egress logging and export rate limiting |

### 6.2 CI security gates — all blocking

```yaml
security:
  - secret-scanning        # gitleaks — any hit fails the build
  - dependency-audit       # pip-audit + npm audit — critical CVE fails
  - sast                   # bandit (py) + eslint-security (ts)
  - iac-scan               # tfsec on infra/
  - container-scan         # trivy on built images
  - phi-leak-test          # custom: PHI-laden payload through log/error paths
  - authz-test             # cross-tenant access attempts, all must 403
  - audit-coverage-test    # every PHI read path writes audit_event
  - append-only-test       # UPDATE/DELETE on audit tables must fail
  - prime-directive-grep   # PD-17: no coding auto-submit path exists
```

### 6.3 Threat cases you must test

| Threat | Required test |
|---|---|
| Cross-tenant data leak | Every endpoint, wrong client scope → 403 |
| SQL injection | Fuzz inputs against parameterised queries |
| Malicious X12 | Deeply nested / oversized / malformed file → contained, quarantined |
| Retry duplication | Same idempotency key → single effect |
| Privilege escalation | Each role attempts every forbidden action |
| PHI in telemetry | Assert scrubbing across log, metric, trace, error paths |
| Audit tampering | App role cannot modify audit rows |

---

## 7. Testing and QA pipeline

### 7.1 Required suites

| Suite | Scope | Gate |
|---|---|---|
| **Unit** | Rules, parsers, calculations, state machine | 90% line coverage on `src/decision`, `src/ingestion/x12`, `src/domain` |
| **Property** (Hypothesis) | X12 parsers, money arithmetic | Parsers: no uncaught exception on any input |
| **Integration** | Module boundaries, DB, mocked externals | Ephemeral Postgres + Redis per run |
| **Contract** | API responses vs OpenAPI schema | Any drift fails |
| **E2E** (Playwright) | Five highest-volume operator journeys | Must pass before merge |
| **Security** | §6.3 | All blocking |
| **Leakage** | Every ML feature | Blocking; must fail on a deliberate leak |
| **Reconstruction** | Rebuild marts from RAW | Byte-identical |
| **Idempotency** | Every mutating path and every ingestion path | Re-run changes nothing |
| **Edge cases** | DOC-02 §13, EC-01 … EC-70 | Tracked; see 7.3 |
| **Accessibility** | Axe on every screen | WCAG 2.1 AA |
| **Performance** | 10k-row queue render; API p95 < 500ms | Measured, not asserted by eye |

### 7.2 Invariant tests — these must exist by name

Write these as explicitly named tests so a reviewer can find them:

```
test_cas_group_code_required
test_rejection_is_not_denial
test_service_line_grain_not_collapsed
test_multi_cas_adjustments_not_merged
test_resubmission_chains_to_original
test_denial_rate_excludes_resubmissions
test_rules_override_model_on_conflict
test_deadline_beats_priority_score
test_modifier_25_never_auto_applied
test_modifier_59_never_auto_applied
test_no_coding_autosubmit_path_exists
test_co_group_never_billed_to_patient
test_835_must_balance_or_quarantine
test_raw_archived_before_parse
test_reprocessing_835_is_idempotent
test_reversal_reopens_original_claim
test_features_knowable_at_submission_time
test_random_split_is_rejected
test_phi_absent_from_logs
test_audit_written_in_same_transaction
test_audit_tables_reject_update_and_delete
test_cross_client_access_denied
test_two_operators_cannot_hold_one_item
test_claim_never_rests_without_next_action
test_stale_payer_rule_cannot_block
test_rule_promotion_requires_backtest
test_appeal_requires_human_signoff
test_model_degradation_falls_back_to_rules
test_critical_drift_auto_reverts
```

**A milestone is not complete until its invariant tests exist and pass.**

### 7.3 Edge case tracking

The 70 edge cases (EC-01 … EC-70) are defined in **`docs/02_Domain_and_Functional.md` §13**, not in the SRS. The SRS requires them via NFR-TEST-02. You need both files.

Maintain `tests/EDGE_CASES.md` mapping each case to its test and status. Prioritise those marked **High** in DOC-02:

`EC-04, EC-05, EC-06, EC-12, EC-13, EC-14, EC-15, EC-16, EC-18, EC-19, EC-20, EC-24, EC-25, EC-26, EC-30, EC-33, EC-35, EC-36, EC-37, EC-39, EC-41, EC-46, EC-47, EC-53, EC-54, EC-55, EC-59, EC-60, EC-61, EC-64, EC-66`

### 7.4 CI pipeline order

```
lint → typecheck → unit → property → integration → contract
     → security suite → leakage suite → build → e2e → a11y → perf
```

Fail fast. No stage may be skipped or marked continue-on-error.

### 7.5 QA rules for you specifically

- **Write the test before the implementation** for anything touching money, PHI, or a prime directive.
- **Never weaken a test to make a build pass.** If a test is wrong, explain why and fix it deliberately.
- **A skipped test is a failing test.** No `@pytest.mark.skip` without a linked TODO and a reason.
- **Prove the negative.** For each safety control, write a test that fails when the control is removed. Show me that failure once.
- **No mocking the thing under test.** Mock externals only.

---

## 8. Synthetic data generator (M0.10)

Everything depends on this. Build it early and well.

**Must produce:**
- Patients, providers, encounters (Synthea-style or hand-rolled)
- Valid 837P claims with realistic CPT/ICD/modifier combinations
- A **fake payer adjudicator** applying: NCCI edits, timely filing, auth requirements, eligibility, per-payer quirks
- 835 remittances with realistic CARC/RARC mix and correct CAS group codes
- Deliberate edge cases: multi-CAS lines, reversals, recoupments, bulk 835s spanning many claims, resubmissions, secondary coverage, zero-payment 835s, unbalanced files (for quarantine testing)
- Configurable payer mix, volume, and denial rate

**Must NOT:** be used to train any production model. Synthetic denials contain only the patterns you programmed. It is for pipeline testing, not learning. Assert this in the module docstring.

---

## 9. Definition of Done

A task is done only when **all** are true:

- [ ] Implementation complete, no TODOs in the changed code
- [ ] Tests written, including the invariant test if one applies
- [ ] All relevant SRS edge cases covered or explicitly deferred with a reason
- [ ] `make verify` green locally (lint, types, all suites)
- [ ] No PHI in any log or error path — verified, not assumed
- [ ] Audit logging verified on every decision path touched
- [ ] Migration is backward-compatible (expand → migrate → contract)
- [ ] OpenAPI regenerated and frontend client regenerated if the API changed
- [ ] SRS updated if the design changed, with an ADR in `docs/decisions/`
- [ ] Rollback path exists and is described

---

## 10. Working conventions

**Commands** — define these in a `Makefile` in M0:
```
make up        # local stack
make verify    # lint + typecheck + all test suites — the single gate
make test      # tests only
make seed      # generate + load synthetic data
make migrate   # run migrations
make openapi   # regenerate schema + frontend client
```

**Commits:** conventional commits. Reference the SRS requirement ID where relevant (`feat(ingestion): 835 CAS unrolling (PD-6, DR-04)`).

**Branching:** feature branch → PR → `main`. Every PR reviewed. No direct pushes to `main`.

**Naming:** use the SRS glossary. Do not invent domain terms. If a term is missing, add it to the glossary rather than coining a synonym.

**Comments:** explain *why*, especially where a prime directive or payer quirk drove the design. Do not narrate *what* the code does.

---

## 11. How to report progress

At the end of each work session, output:

```
MILESTONE: M<n> — <name>
COMPLETED: <tasks>
TESTS ADDED: <names, especially invariant tests>
VERIFY STATUS: pass | fail (<what fails>)
PRIME DIRECTIVES TOUCHED: <which, and how satisfied>
BLOCKED ON: <human checkpoint, or none>
NEXT: <next task>
```

At each **gate**, additionally report the measurements the gate requires. Do not claim a gate passed without the numbers.

---

## 12. Human checkpoints

Stop and ask. Do not proceed, do not work around, do not stub-and-continue silently.

| Trigger | Why |
|---|---|
| Any real clearinghouse, EHR, or payer connection | Commercial and legal, not technical |
| Anything resembling real PHI | Compliance |
| Production deployment | Requires approval per SRS §13.2 |
| Any of `DECISION-01` … `DECISION-08` (SRS §16) | Sponsor's call |
| A task appears to require violating a prime directive | Almost always means the task is misunderstood |
| Coding automation (ML-6) beyond stubs | Requires a formal gate |
| Adding a PHI-touching third-party service | BAA required first |
| A `[TBD-nn]` value is needed and unknown | Ask; do not invent a number |

The SRS references 10 unfilled values the human must decide: `TBD-36, 42, 45, 51, 52, 53, 54, 55, 56, 57` (see SRS Appendix B). More exist in DOC-01 and DOC-02.

Where one blocks you, make it a **configurable value with a clearly-labelled placeholder default**, list it in your report, and continue. Never hard-code a guess as if it were decided.

---

## 13. Start here

```
1. Read docs/03_SRS.md §1–§5 and §7–§9. Skim the rest; return to it per milestone.
2. Confirm back to me: the stack, the milestone order, and the 20 prime directives.
3. Ask any clarifying questions BEFORE writing code.
4. Begin M0.1. Do not skip ahead — M0 is what makes the rest safe.
5. Report at the M0 gate with the test names that prove each safety rail.
```

**If anything in this file conflicts with `docs/03_SRS.md`:**
- On *what the system does* → the SRS wins.
- On *how you work* → this file wins.
- If you cannot tell which it is → ask.
