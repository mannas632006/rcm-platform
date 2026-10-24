# Error taxonomy

**Generated file — do not edit.** Regenerate with:

```
python -m tools.gen_error_docs > docs/errors.md
```

Every error the platform raises deliberately is listed here with the exact
response it produces. Responses follow RFC 7807 Problem Details (FR-API-04);
the `type` URI is `https://errors.rcm.internal/<code>`.

Clients branch on `code`. Titles and details are human-readable and may change.

`detail` is fixed per class and never interpolates data — variable values go in
`context`, which passes through the PHI scrubber before serialisation (PD-13).

**Suspends automation** marks the compliance-class errors that halt the affected
automation path pending human review (FR-BE-07).

| Code | HTTP | Title | Suspends automation | Meaning |
|---|---|---|---|---|
| `idempotency-key-required` | 400 | Idempotency-Key header required | — | PD-14: mutating endpoints require the header. |
| `authentication-failed` | 401 | Authentication failed | — | Valid credentials are required. |
| `authorization-denied` | 403 | Authorization denied | — | The authenticated principal may not perform this action. |
| `client-scope-violation` | 403 | Authorization denied | yes | A principal reached for data outside its client scope (PD-20, SR-21). |
| `resource-not-found` | 404 | Resource not found | — | No resource matches the identifier supplied. |
| `concurrency-conflict` | 409 | Concurrent modification | — | The entity was modified concurrently. Retry with fresh state. |
| `escalated-uncertain` | 409 | Escalated: outcome uncertain | yes | PD-18: the system does not know, so it does not act. |
| `idempotency-conflict` | 409 | Idempotency key reused with a different payload | — | The same key was reused with a different request body. |
| `invalid-state-transition` | 409 | Invalid state transition | — | An undefined claim state transition was attempted (FR-BE-09). |
| `manual-coding-required` | 409 | Manual coding required | — | PD-16: modifiers 25 and 59 are never auto-applied. |
| `request-in-progress` | 409 | An identical request is in progress | — | A request with this Idempotency-Key is currently being processed. |
| `stale-payer-rule` | 409 | Payer rule is stale | yes | A payer registry entry too old to drive a BLOCKING decision (BR-REG-02). |
| `work-item-locked` | 409 | Work item already assigned | — | M3.3: two operators cannot hold one item. |
| `payload-too-large` | 413 | Payload too large | — | The request body exceeds the configured maximum. |
| `file-does-not-balance` | 422 | Remittance file does not balance | yes | An 835's amounts do not reconcile (IR-02, M1.3). |
| `file-quarantined` | 422 | Inbound file quarantined | yes | A file failed a data quality gate and was quarantined (DR-10). |
| `leakage-refused` | 422 | Refused: would leak | yes | PD-11/PD-12: a dataset operation that would invalidate the model. |
| `malformed-x12` | 422 | Malformed X12 file | yes | Structural failure parsing untrusted EDI input (S-12). |
| `unsupported-x12` | 422 | X12 transaction not supported | yes | A structurally valid transaction this parser does not yet read (M1.5). |
| `validation-failed` | 422 | Request failed validation | — | The request was well-formed but failed domain validation. |
| `x12-limit-exceeded` | 422 | X12 file exceeds parsing limits | yes | Size or nesting limit hit while parsing (S-12). |
| `rate-limit-exceeded` | 429 | Rate limit exceeded | — | Too many requests. Retry after the interval indicated. |
| `compliance-violation` | 500 | Compliance violation | yes | A compliance-class failure. Suspends the affected automation path (FR-BE-07). |
| `internal-error` | 500 | Internal error | — | Base for every error this platform raises deliberately. |
| `external-system-error` | 502 | External system error | — | An external system returned an error. |
| `external-system-unavailable` | 503 | External system unavailable | yes | Both primary and secondary failed (M3.7). Named, and no data lost. |

## Response shape

```json
{
  "type": "https://errors.rcm.internal/file-does-not-balance",
  "title": "Remittance file does not balance",
  "status": 422,
  "detail": "The file failed a data quality gate and was quarantined unparsed.",
  "code": "file-does-not-balance",
  "instance": "/api/v1/remittances/ingest",
  "request_id": "01JB8Z0Q2K7X",
  "context": {"assertion": "clp_sum != bpr_total", "file_hash": "sha256:..."}
}
```
