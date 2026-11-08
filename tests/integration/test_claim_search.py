"""Claim search, the top bar's "Search claim ID or payer" (GATE M5, ADR-0022).

Through the real app and database: a claim number's start or a payer
matches, another client's claims never do (PD-20), the text is matched and
never interpreted, and the read is audited (PD-2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from rcm.platform.audit import AuditEvent
from rcm.platform.db import session_scope
from tests.integration.test_acknowledgement_loading import CLIENT_A, CLIENT_B, submit
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def claims(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    submit("CLM-1001", root, control="0001")
    submit("CLM-1002", root, control="0002")
    submit("ZZ-9", root, control="0003")
    submit("CLM-1003", root, client=CLIENT_B, control="0004")


def search(api: TestClient, q: str, **params: Any) -> Any:  # noqa: F811
    return api.get("/api/v1/claims", params={"q": q, **params}, headers=bearer(CLIENT_A))


def numbers(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return sorted(hit["submitter_claim_id"] for hit in response.json()["hits"])


def test_a_claim_number_matches_by_its_start_ignoring_case(
    api: TestClient,  # noqa: F811
    claims: None,
) -> None:
    assert numbers(search(api, "clm-100")) == ["CLM-1001", "CLM-1002"]
    assert numbers(search(api, "CLM-1001")) == ["CLM-1001"]
    assert numbers(search(api, "1001")) == []


def test_a_payer_matches_by_id_or_by_part_of_its_name(
    api: TestClient,  # noqa: F811
    claims: None,
) -> None:
    every = ["CLM-1001", "CLM-1002", "ZZ-9"]
    assert numbers(search(api, "00590")) == every
    assert numbers(search(api, "bcbs tex")) == every
    hit = search(api, "ZZ-9").json()["hits"][0]
    assert hit["payer_id"] == "00590"
    assert hit["submission_attempt"] == 1


@pytest.mark.invariant
def test_another_clients_claim_is_never_a_result(
    api: TestClient,  # noqa: F811
    claims: None,
) -> None:
    """PD-20: CLM-1003 belongs to client B; client A's search cannot see it."""
    assert numbers(search(api, "CLM-1003")) == []
    assert "CLM-1003" not in numbers(search(api, "00590"))


def test_the_text_is_matched_never_interpreted(
    api: TestClient,  # noqa: F811
    claims: None,
) -> None:
    """LIKE wildcards are literal: `CL%` is not 'anything starting CL'."""
    assert numbers(search(api, "CL%")) == []
    assert numbers(search(api, "CLM_1001")) == []
    assert numbers(search(api, "%%")) == []


@pytest.mark.parametrize("q", ["C", " ", "x" * 81])
def test_search_text_must_be_two_to_eighty_characters(
    api: TestClient,  # noqa: F811
    q: str,
) -> None:
    assert search(api, q).status_code == 422


def test_more_matches_than_listed_says_so(
    api: TestClient,  # noqa: F811
    claims: None,
) -> None:
    body = search(api, "CLM", limit=1).json()
    assert len(body["hits"]) == 1
    assert body["truncated"] is True
    assert search(api, "CLM").json()["truncated"] is False


def test_a_search_is_audited(api: TestClient, claims: None) -> None:  # noqa: F811
    """Dates of service are listed: PHI read, so an audit row (PD-2)."""

    def count() -> int:
        with session_scope() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.action == "GET /api/v1/claims")
                )
                or 0
            )

    before = count()
    search(api, "CLM")
    assert count() == before + 1
