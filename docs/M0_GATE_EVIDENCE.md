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
