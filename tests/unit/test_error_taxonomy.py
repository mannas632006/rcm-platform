"""M0.6 acceptance: every error class maps to a documented response.

The documentation is generated, so the useful test is not "is it written down"
but "is what is written down still true".
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest
from tools.gen_error_docs import render

from rcm.platform.errors import (
    BalanceAssertionError,
    ClientScopeViolationError,
    PlatformError,
    QuarantineError,
    all_error_classes,
)
from rcm.platform.logging import REDACTED

DOCS = Path(__file__).resolve().parents[2] / "docs" / "errors.md"


def test_error_docs_are_in_sync() -> None:
    """`docs/errors.md` matches the taxonomy.

    Fails when someone adds an error class without regenerating. The fix is in
    the failure message, not in a wiki page nobody reads.
    """
    assert DOCS.read_text(encoding="utf-8") == render(), (
        "docs/errors.md is stale. Regenerate:\n    python -m tools.gen_error_docs > docs/errors.md"
    )


def test_every_error_class_has_a_unique_code() -> None:
    classes = all_error_classes()
    codes = [c.code for c in classes]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"error codes must be unique, got duplicates: {duplicates}"
    assert len(classes) > 20, "taxonomy unexpectedly small; did a module fail to import?"


def test_every_error_class_is_documented() -> None:
    documented = DOCS.read_text(encoding="utf-8")
    missing = [c.code for c in all_error_classes() if f"`{c.code}`" not in documented]
    assert not missing, f"undocumented error codes: {missing}"


def test_every_status_is_a_real_http_error_status() -> None:
    for cls in all_error_classes():
        status = HTTPStatus(cls.status)
        assert status.is_client_error or status.is_server_error, (
            f"{cls.__name__} maps to {status}, which is not an error status"
        )


@pytest.mark.parametrize("cls", all_error_classes(), ids=lambda c: c.code)
def test_detail_never_interpolates(cls: type[PlatformError]) -> None:
    """Detail strings are fixed text.

    A detail containing a format placeholder is an invitation to interpolate a
    patient name into an error payload, which is one of the four PHI leak paths
    in CLAUDE.md section 6.3.
    """
    assert "{" not in cls.detail and "%s" not in cls.detail, (
        f"{cls.__name__}.detail looks interpolatable: {cls.detail!r}. "
        "Put variable values in context= instead."
    )


def test_problem_context_is_scrubbed() -> None:
    """PD-13: an error payload carrying PHI emits none of it."""
    err = QuarantineError(
        assertion="clp_sum != bpr_total",
        patient_name="Maria Gonzalez",
        ssn="412-88-7390",
        file_hash="sha256:9ac1",
    )
    problem = err.to_problem(instance="/api/v1/remittances/ingest", request_id="req-4")

    rendered = repr(problem)
    assert "Maria Gonzalez" not in rendered
    assert "412-88-7390" not in rendered
    assert problem["context"]["patient_name"] == REDACTED
    # The operationally useful part survives -- DR-10 requires the specific
    # failing assertion, not a generic message.
    assert problem["context"]["assertion"] == "clp_sum != bpr_total"
    assert problem["request_id"] == "req-4"
    assert problem["type"].endswith("file-quarantined")


def test_scope_violation_is_indistinguishable_from_plain_denial() -> None:
    """A cross-tenant probe must not be answerable from the response body.

    If a scope violation returned a distinct code or a 404, a caller could
    enumerate which identifiers exist in another client's data.
    """
    scoped = ClientScopeViolationError().to_problem()
    assert scoped["status"] == HTTPStatus.FORBIDDEN
    assert scoped["title"] == "Authorization denied"


def test_quarantine_subclasses_suspend_automation() -> None:
    """A file we cannot trust must not quietly continue down the pipeline."""
    assert BalanceAssertionError.suspends_automation
    assert issubclass(BalanceAssertionError, QuarantineError)
