"""PD-13: no PHI in logs, errors, metrics, or exception payloads.

CLAUDE.md section 6.3 names four paths that must be scrubbed: log, metric, trace and
error. Each gets a test here. The payload below is the same synthetic patient in
every case, so a gap in any one path shows up as a specific failure rather than a
general one.

None of these values are real. Synthetic only (CLAUDE.md section 0).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from rcm.platform.logging import REDACTED, configure_logging, get_logger, scrub

pytestmark = pytest.mark.security


# A synthetic patient carrying one of every identifier the scrubber must catch.
PHI_PAYLOAD: dict[str, Any] = {
    "patient_name": "Maria Gonzalez",
    "first_name": "Maria",
    "last_name": "Gonzalez",
    "ssn": "412-88-7390",
    "date_of_birth": "1974-03-11",
    "member_id": "BCBSTX884120773",
    "medical_record_number": "MRN-0099421",
    "address_line_1": "884 Cedar Hollow Road",
    "phone": "214-555-0179",
    "email": "m.gonzalez@example.com",
    "claim_id": "8f14e45f-ea8d-4b1c-9a2e-77b3c9d10a55",  # not PHI: safe to log
    "total_charge": "412.00",  # not PHI: safe to log
}

#: Values that must never appear in any output, in any path.
PHI_VALUES: tuple[str, ...] = (
    "Maria Gonzalez",
    "Maria",
    "Gonzalez",
    "412-88-7390",
    "1974-03-11",
    "BCBSTX884120773",
    "MRN-0099421",
    "884 Cedar Hollow Road",
    "214-555-0179",
    "m.gonzalez@example.com",
)


def assert_no_phi(rendered: str) -> None:
    """Fail with the specific leaked value, not a generic assertion error."""
    leaked = [v for v in PHI_VALUES if v in rendered]
    assert not leaked, f"PHI leaked into output: {leaked!r}\n--- output ---\n{rendered}"


@pytest.fixture(autouse=True)
def _configured_logging() -> None:
    configure_logging(level="INFO", json_output=True)


# ---------------------------------------------------------------------------
# The named invariant test (CLAUDE.md section 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_phi_absent_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    """A log call carrying a full PHI payload emits none of it."""
    log = get_logger(__name__)
    with caplog.at_level(logging.INFO):
        log.info("claim.received", **PHI_PAYLOAD)

    assert_no_phi(caplog.text)
    # The event still has to be useful: non-PHI context survives.
    assert "claim.received" in caplog.text
    assert "8f14e45f-ea8d-4b1c-9a2e-77b3c9d10a55" in caplog.text


# ---------------------------------------------------------------------------
# The other three paths from CLAUDE.md section 6.3
# ---------------------------------------------------------------------------


def test_phi_absent_from_exception_payloads(caplog: pytest.LogCaptureFixture) -> None:
    """An exception whose message and args embed PHI logs neither."""
    log = get_logger(__name__)

    class PatientNotFoundError(Exception):
        pass

    with caplog.at_level(logging.ERROR):
        try:
            raise PatientNotFoundError(
                f"no coverage for {PHI_PAYLOAD['patient_name']} "
                f"(SSN {PHI_PAYLOAD['ssn']}, DOB {PHI_PAYLOAD['date_of_birth']})"
            )
        except PatientNotFoundError:
            log.exception("eligibility.lookup_failed", member_id=PHI_PAYLOAD["member_id"])

    assert_no_phi(caplog.text)


def test_phi_absent_from_metric_labels(caplog: pytest.LogCaptureFixture) -> None:
    """Metric dimensions are scrubbed on the same path as log fields.

    Free-text PHI in a metric label is the classic leak: cardinality pressure
    tempts people to label by patient.
    """
    log = get_logger(__name__)
    with caplog.at_level(logging.INFO):
        log.info(
            "metric.emit",
            metric="claims_submitted_total",
            labels={"payer_id": "60054", "patient_name": PHI_PAYLOAD["patient_name"]},
        )

    assert_no_phi(caplog.text)
    assert "60054" in caplog.text, "payer_id is not PHI and must survive"


def test_phi_absent_from_trace_context(caplog: pytest.LogCaptureFixture) -> None:
    """Values bound to the context (trace/span attributes) are scrubbed too."""
    log = get_logger(__name__).bind(request_id="req-7781", subscriber_id=PHI_PAYLOAD["member_id"])
    with caplog.at_level(logging.INFO):
        log.info("claim.scrubbed")

    assert_no_phi(caplog.text)
    assert "req-7781" in caplog.text


# ---------------------------------------------------------------------------
# The scrubber itself
# ---------------------------------------------------------------------------


def test_scrub_handles_nesting_and_sequences() -> None:
    """PHI hidden inside nested structures is still found.

    Claims arrive as nested documents; a scrubber that only inspects top-level
    keys is worse than none, because it looks like protection.
    """
    nested = {
        "claim": {
            "lines": [
                {"cpt": "99213", "notes": f"call {PHI_PAYLOAD['phone']}"},
                {"cpt": "99214", "patient": {"ssn": PHI_PAYLOAD["ssn"]}},
            ],
            "guarantor": ("Maria Gonzalez", "884 Cedar Hollow Road"),
        }
    }
    assert_no_phi(json.dumps(scrub(nested), default=str))


def test_scrub_finds_phi_in_free_text() -> None:
    """Structured identifiers are caught by value, not only by field name.

    A free-text note is not a trusted field name, so pattern matching is the
    only defence for SSN, phone, email and member-id shapes.
    """
    note = (
        "Spoke with patient, SSN 412-88-7390, call back on 214-555-0179 "
        "or email m.gonzalez@example.com; member BCBSTX884120773."
    )
    scrubbed = str(scrub(note))
    for value in ("412-88-7390", "214-555-0179", "m.gonzalez@example.com", "BCBSTX884120773"):
        assert value not in scrubbed, f"{value!r} survived free-text scrubbing"
    assert REDACTED in scrubbed


def test_scrub_preserves_non_phi() -> None:
    """The scrubber must not redact the fields operations runs on.

    Over-redaction is its own failure: an unusable log gets switched off.
    """
    safe = {
        "claim_id": "8f14e45f-ea8d-4b1c-9a2e-77b3c9d10a55",
        "payer_id": "60054",
        "cas_group_code": "CO",
        "carc_code": "97",
        "total_charge": "412.00",
        "state": "SUBMITTED",
    }
    assert scrub(safe) == safe


# ---------------------------------------------------------------------------
# Proving the negative (CLAUDE.md section 7.5)
# ---------------------------------------------------------------------------


def test_unscrubbed_payload_would_leak() -> None:
    """Confirms the assertion above can actually fail.

    Without this, `test_phi_absent_from_logs` would still pass if the scrubber
    were replaced by a no-op and the payload never reached the output at all.
    This asserts the payload really does contain what we claim it does.
    """
    rendered = json.dumps(PHI_PAYLOAD)
    leaked = [v for v in PHI_VALUES if v in rendered]
    assert sorted(leaked) == sorted(PHI_VALUES), (
        "the test fixture no longer contains every PHI value it claims to; "
        "assert_no_phi would be vacuous"
    )
