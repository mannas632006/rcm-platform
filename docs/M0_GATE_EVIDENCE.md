# M0 gate evidence

CLAUDE.md section 7.5: *"Prove the negative. For each safety control, write a test that
fails when the control is removed. Show me that failure once."*

This file records those demonstrations. Each entry shows the control removed, the
resulting failure, and the restored green run. Re-run any of them by reversing the
change described.

---

## PD-13 — PHI scrubbing (`rcm.platform.logging`)

**Control removed:** `phi_scrub_processor` short-circuited to a pass-through.

```
$ pytest tests/security/test_phi_scrubbing.py -q

AssertionError: PHI leaked into output: ['Maria Gonzalez', 'Maria', 'Gonzalez',
  '412-88-7390', '1974-03-11', 'BCBSTX884120773', 'MRN-0099421',
  '884 Cedar Hollow Road', '214-555-0179', 'm.gonzalez@example.com']
AssertionError: PHI leaked into output: ['BCBSTX884120773']
AssertionError: PHI leaked into output: ['Maria Gonzalez', 'Maria', 'Gonzalez']
AssertionError: PHI leaked into output: ['BCBSTX884120773']

FAILED tests/security/test_phi_scrubbing.py::test_phi_absent_from_logs
FAILED tests/security/test_phi_scrubbing.py::test_phi_absent_from_exception_payloads
FAILED tests/security/test_phi_scrubbing.py::test_phi_absent_from_metric_labels
FAILED tests/security/test_phi_scrubbing.py::test_phi_absent_from_trace_context
4 failed, 4 passed
```

**Control restored:** `8 passed`.

All four paths named in CLAUDE.md section 6.3 -- log, error, metric, trace -- fail
independently, so a regression in any one of them is identifiable from the failure
alone.

### A leak this found during development

The first implementation scrubbed only the structlog event dictionary. The JSON
line was clean, but the stdlib handler still formatted `record.exc_info` and
printed the traceback, whose last line is the exception message:

```
PatientNotFoundError: no coverage for Maria Gonzalez (SSN 412-88-7390, DOB 1974-03-11)
```

A processor cannot fix this, because it only sees loggers that go through
structlog -- any third-party library in the process leaks the same way. The
guard was moved to `logging.setLogRecordFactory`, which every log record in the
process passes through, and the exception is reduced there to its type and raise
site.

---

## PD-20 — client scoping at the query layer (`rcm.platform.auth`)

**Control removed:** `scoped_query` returned its statement unmodified instead of
applying `WHERE client_id IN (...)`.

```
$ pytest tests/security/test_client_scoping.py -q

FAILED tests/security/test_client_scoping.py::test_cross_client_access_denied
FAILED tests/security/test_client_scoping.py::test_scope_filter_survives_additional_where_clauses
FAILED tests/security/test_client_scoping.py::test_empty_scope_matches_nothing_rather_than_everything
FAILED tests/security/test_client_scoping.py::test_system_principal_must_name_its_clients
4 failed, 5 passed
```

**Control restored:** `9 passed`.

The four that fail are the four that assert on the *compiled SQL* rather than on
returned rows. That is the distinction PD-20 draws: the five survivors check the
`Principal` object's own behaviour, which a handler-level check would also
satisfy, while these four check that the restriction is in the statement itself
and therefore holds no matter which handler built it or what the database
contains.

### Why the test asserts on compiled SQL

The first version of `test_cross_client_access_denied` compiled the statement
with SQLAlchemy's default dialect and asserted the client UUID appeared in the
output. It failed against a correct implementation: the default dialect renders
a UUID without hyphens, so the assertion was checking for a string that never
reaches PostgreSQL. Compiling against `postgresql.dialect()` tests the SQL the
database actually receives.

---

## PD-14 — idempotency (`rcm.platform.idempotency`)

**Control removed:** the middleware discarded the result of `store.reserve()`, so
a completed response was never replayed.

```
$ pytest tests/security/test_idempotency.py -q

AssertionError: the handler ran twice; PD-14 is not enforced
assert 2 == 1

FAILED tests/security/test_idempotency.py::test_same_key_twice_produces_one_effect
1 failed, 11 passed
```

**Control restored:** `12 passed`.

The test counts side effects rather than inspecting the store, because the thing
being prevented is a duplicate claim submission -- a duplicate bill -- not a
duplicate cache entry.

### Two bugs this found during development

**Exceptions raised inside middleware never reach the app's error handlers.**
Starlette installs its exception handlers below user middleware, so
`IdempotencyKeyRequiredError` escaped as a bare 500 rather than the documented
400 Problem Details. A client would have been told "internal error" when the
actual problem was a missing header it could have added. The middleware now
renders `PlatformError` itself, using the same `to_problem()` call as the app
handler so the two cannot drift.

**`isinstance(response, StreamingResponse)` is false for the response
`BaseHTTPMiddleware` returns.** `call_next` hands back a private
`_StreamingResponse` that subclasses `Response` directly. The isinstance check
therefore skipped the body-draining branch, the reservation was never completed,
and every second attempt of a retry returned 409 instead of replaying the
original 200 -- the precise failure PD-14 exists to prevent, introduced while
fixing a type error. The body is now drained by duck-typing on `body_iterator`,
which matches what the object is rather than what the public type hierarchy
suggests.

---

## M0.10 — synthetic corpus contract (`tools/synthetic`)

Not a safety control, so there is no negative to prove. The equivalent evidence
is that the corpus contains the cases M1's gate depends on, asserted in
`tests/unit/test_synthetic_generator.py` and verified by a parser that does not
share the generator's code:

```
$ python -m tools.synthetic --config tools/synthetic/config/smoke.yaml
  claims                       616
  service_lines                1018
  adjustment_rows              1297
  resubmissions                16
  recouped_claims              6
  denial rate (all attempts)   0.1326
  provenance                   synthetic:tools.synthetic

$ python -m tools.synthetic.validate
  837: 616 claims, 1018 service lines parsed
  835: 616 claims, 1018 lines, 1297 CAS reasons
       balanced 210, unbalanced 1 (1 unbalanced is the intentional quarantine fixture)
       lines carrying >1 CAS reason: 279
       CAS groups: {'CO': 1045, 'PR': 259}
  all checks passed
```

279 multi-CAS lines is the number that matters most: it is the case that
separates service-line grain from adjustment grain (PD-6), and a parser that
merges adjustments passes every other test until it meets one.

---

## PD-14 / PD-20 — the replay cache is namespaced per principal

Found during review of the M0 commit, after the gate above was first reported.

**The defect.** `IdempotencyMiddleware` namespaced its Redis keys by reading
`request.state.principal`. ASGI middleware runs before routing, so the
`current_principal` dependency had not executed and that attribute was never
set. Every key therefore landed in a single `"anonymous"` namespace, and two
tenants choosing the same `Idempotency-Key` replayed each other's responses:

```
Alice (tenant A) POST -> 200 {"served_to":"alice@tenant-a","claim":"CLM-A-SECRET"}
Bob   (tenant B) POST -> 200 {"served_to":"alice@tenant-a","claim":"CLM-A-SECRET"}

>>> CROSS-TENANT LEAK: Bob received Alice's response body.
```

This is the disclosure PD-20 exists to prevent, arriving through a route PD-20
does not cover: the scoping control guards queries, and nothing queried anything
-- the response was served from cache. Keys derived from a batch date or a run
number collide across tenants by construction, so this needed no adversary.

**Why the existing tests missed it.** `test_keys_are_namespaced_per_principal`
called `record_key()` directly with two different subjects and passed. The
helper was correct; the middleware was feeding it a constant. A unit test of a
helper cannot see what its caller passes.

**Fix.** The middleware verifies the bearer token itself and namespaces on the
resulting subject, caching the `Principal` on `request.state` so the dependency
does not verify twice. An unverifiable token yields `"anonymous"`, which is safe
because the request is about to be rejected and a non-2xx response releases the
reservation rather than storing it.

**Control removed:** `_principal_subject` returning `"anonymous"` unconditionally.

```
$ pytest tests/security/test_idempotency.py -q

FAILED tests/security/test_idempotency.py::test_one_tenant_cannot_replay_another_tenants_response
FAILED tests/security/test_idempotency.py::test_the_same_principal_still_replays
FAILED tests/security/test_idempotency.py::test_a_conflict_cannot_reveal_another_tenants_key
3 failed, 12 passed
```

**Control restored:** `15 passed`.

The three replacement tests drive a real application with real tokens rather
than calling the helper, which is the only framing that could have caught this.
`test_a_conflict_cannot_reveal_another_tenants_key` covers the quieter half of
the same defect: under a shared namespace the 409 response is an oracle for
which keys another tenant has already used.

### A test-only trap worth recording

The first version of those tests returned 422 against a correct implementation.
This module uses `from __future__ import annotations`, so FastAPI resolves a
handler's annotations against **module** globals; `Principal`, imported inside
the fixture, was not among them, and FastAPI fell back to treating the parameter
as a request body field. Any name used in a route signature must be imported at
module level in a test file that postpones annotation evaluation.
