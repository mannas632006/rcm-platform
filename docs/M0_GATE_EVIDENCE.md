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

## PD-20 / SR-21 — client scoping at the query layer (`rcm.platform.scoping`)

**Control removed:** the `do_orm_execute` listener short-circuited before applying
the practice filter.

```
$ pytest tests/security/test_client_scoping.py -q

FAILED tests/security/test_client_scoping.py::test_cross_client_access_denied
FAILED tests/security/test_client_scoping.py::test_handler_without_filter_cannot_leak
FAILED tests/security/test_client_scoping.py::test_query_without_a_principal_is_refused
FAILED tests/security/test_client_scoping.py::test_assigned_principal_with_no_practices_sees_nothing
4 failed, 4 passed
```

**Control restored:** `8 passed`.

The middle two are the ones that matter. `test_handler_without_filter_cannot_leak`
issues `select(ScopedThing)` with no `WHERE` clause at all -- what a handler
written by someone who has never heard of client scoping looks like -- and still
sees only its own practice. That is the property PD-20 states, and it is not
provable by any test that writes the filter itself.

`test_query_without_a_principal_is_refused` covers the failure-closed half: an
unbound principal raises rather than returning every tenant's rows.

---

## PD-14 / EC-41 — idempotency (`rcm.api.idempotency_middleware`)

**Control removed:** `dispatch` short-circuited to `call_next` for every method.

```
$ pytest tests/security/test_idempotency.py -q

FAILED tests/security/test_idempotency.py::test_same_key_twice_has_one_effect
FAILED tests/security/test_idempotency.py::test_ten_retries_still_one_effect
FAILED tests/security/test_idempotency.py::test_mutating_request_without_a_key_is_refused
FAILED tests/security/test_idempotency.py::test_key_reuse_with_a_different_body_conflicts
FAILED tests/security/test_idempotency.py::test_concurrent_duplicate_is_told_to_wait
5 failed, 4 passed
```

**Control restored:** `9 passed`.

`test_ten_retries_still_one_effect` is the realistic shape of the failure: a client
retrying aggressively against a slow endpoint, which is how EC-41 happens in
production rather than as a single tidy duplicate.

---

## Still to prove

- **PD-1 / PD-2** — decision and audit records in the same transaction. Needs
  Postgres.
- **DR-06 / S-10** — `UPDATE`/`DELETE` on the audit tables refused for `rcm_app`.
  Needs Postgres: the control is a database grant, so a test on any other backend
  would prove nothing.
