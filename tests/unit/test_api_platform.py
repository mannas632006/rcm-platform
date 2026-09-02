"""M0.3 acceptance plus the cross-cutting API behaviour it wires in."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcm.api.main import PROBLEM_CONTENT_TYPE, REQUEST_ID_HEADER, create_app
from rcm.platform.errors import QuarantineError


@pytest.fixture
def app() -> FastAPI:
    application = create_app()

    @application.get("/_test/named-error")
    async def named_error() -> None:
        raise QuarantineError(
            assertion="clp_sum != bpr_total",
            patient_name="Maria Gonzalez",
            file_hash="sha256:9ac1",
        )

    @application.get("/_test/unnamed-error")
    async def unnamed_error() -> None:
        raise RuntimeError("patient Maria Gonzalez SSN 412-88-7390 not found")

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Server exceptions are not re-raised: the point is to observe the response
    # the handler produces, which is what a caller would see.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    """FR-API-03: responses correlate to logs and audit events."""
    response = client.get("/health")
    assert response.headers.get(REQUEST_ID_HEADER)


def test_inbound_request_id_is_preserved(client: TestClient) -> None:
    """A trace that starts at the proxy must not be renamed here."""
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_named_error_renders_problem_details(client: TestClient) -> None:
    """FR-API-04: RFC 7807, with a machine-readable type."""
    response = client.get("/_test/named-error")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    problem = response.json()
    assert problem["code"] == "file-quarantined"
    assert problem["type"].endswith("file-quarantined")
    assert problem["request_id"]
    assert problem["instance"] == "/_test/named-error"
    # DR-10 requires the specific failing assertion, not a generic message.
    assert problem["context"]["assertion"] == "clp_sum != bpr_total"


def test_error_response_carries_no_phi(client: TestClient) -> None:
    """PD-13: an error payload is one of the four leak paths."""
    response = client.get("/_test/named-error")
    assert "Maria Gonzalez" not in response.text


def test_unhandled_error_says_nothing(client: TestClient) -> None:
    """An unnamed exception must not narrate itself to the caller.

    Its message is untrusted free text -- here it contains a name and an SSN.
    The caller gets a generic 500 and a request id; the engineer gets the type
    and raise site from the log.
    """
    response = client.get("/_test/unnamed-error")

    assert response.status_code == 500
    assert "Maria Gonzalez" not in response.text
    assert "412-88-7390" not in response.text
    problem = response.json()
    assert problem["code"] == "internal-error"
    assert problem["request_id"]
