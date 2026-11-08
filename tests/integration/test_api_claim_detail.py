"""S-03 Claim Detail: one claim, everything needed to decide on it (M5.5).

Through the real app, loaders and progression. The screen is a PHI read, so
scope (PD-20) and audit (PD-2) are tested first; then what it must show --
every CARC with its group (PD-3, UR-08), one row per adjustment (PD-6), the
resubmission chain as one claim (PD-7), and every automated decision with
its version and inputs (UR-06).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rcm.platform.audit import AuditEvent
from rcm.platform.auth import Role
from rcm.platform.db import session_scope
from tests.integration.test_acknowledgement_loading import (
    CLIENT_A,
    CLIENT_B,
    ack_277ca,
    acknowledge,
    denial_for,
    remit,
    submit,
)
from tests.integration.test_api_shell import api, bearer, clean  # noqa: F401 - fixtures
from tests.integration.test_claim_progression import advance, claim_named, resubmit

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "raw"
    r.mkdir()
    return r


def denied(root: Path, submitter_id: str = "CLM-D") -> uuid.UUID:
    """A claim the payer took and denied under CO-50."""
    submit(submitter_id, root, control="0001")
    acknowledge(ack_277ca((submitter_id, "A2:20")), root, name="277ca.edi")
    remit(denial_for(submitter_id, control="0001"), root, name="835.edi")
    advance()
    return claim_named(submitter_id)


def detail(api: TestClient, claim_id: uuid.UUID, *clients: uuid.UUID, **kw: Any) -> Any:  # noqa: F811
    return api.get(f"/api/v1/claims/{claim_id}", headers=bearer(*(clients or (CLIENT_A,)), **kw))


@pytest.mark.invariant
def test_cross_client_access_denied_for_claim_detail(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-20: client B's operator cannot read client A's claim -- and cannot
    tell it exists. Same answer as a claim id that was never issued."""
    claim_id = denied(root)
    other = detail(api, claim_id, CLIENT_B)
    unknown = detail(api, uuid.uuid4(), CLIENT_B)
    assert other.status_code == unknown.status_code == 404
    assert other.json()["title"] == unknown.json()["title"]


def test_claim_detail_needs_claim_read(api: TestClient, root: Path) -> None:  # noqa: F811
    """An administrator manages users; the role matrix gives it no claims."""
    claim_id = denied(root)
    assert detail(api, claim_id, role=Role.ADMINISTRATOR).status_code == 403


@pytest.mark.invariant
def test_reading_a_claim_is_audited(api: TestClient, root: Path) -> None:  # noqa: F811
    """PD-2: a member id, diagnoses and dates of service are PHI."""
    claim_id = denied(root)
    subject = f"auditor.{uuid.uuid4().hex[:8]}"
    assert detail(api, claim_id, subject=subject).status_code == 200
    with session_scope() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.actor_id == subject)
        ).all()
    assert actions == [f"GET /api/v1/claims/{claim_id}"]


def test_the_header_states_the_claim(api: TestClient, root: Path) -> None:  # noqa: F811
    body = detail(api, denied(root)).json()
    assert body["submitter_claim_id"] == "CLM-D"
    assert body["state"] == "Denied"
    assert body["payer_id"]
    assert body["payer_name"]
    assert body["total_charge"] == "200.00"
    assert body["balance"] == "200.00"
    assert [line["procedure_code"] for line in body["lines"]] == ["99203"]


def test_the_member_id_is_masked(api: TestClient, root: Path) -> None:  # noqa: F811
    """S-6: an identifier shown only as far as the work needs -- the last
    four, enough to confirm a match on a payer call."""
    body = detail(api, denied(root)).json()
    member = body["member_id_masked"]
    assert member.startswith("•")
    assert len(member.lstrip("•")) <= 4
    assert "member_id" not in body


@pytest.mark.invariant
def test_claim_detail_never_shows_a_carc_without_its_group(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-3 / UR-08: the API hands the screen group and reason together, at
    one row per adjustment (PD-6)."""
    body = detail(api, denied(root)).json()
    (payment,) = body["payments"]
    adjustments = [a for line in payment["lines"] for a in line["adjustments"]]
    adjustments += payment["claim_adjustments"]
    assert adjustments == [
        {"group_code": "CO", "reason_code": "50", "amount": "200.00", "claim_level": False}
    ]


def test_the_filing_clock_stops_once_the_payer_takes_the_claim(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """UR-07: a denied claim was filed on time; its countdown is over. A
    countdown would read as urgent work that is not there."""
    filing = detail(api, denied(root)).json()["filing"]
    assert filing == {"status": "FILED", "days_left": None}


def test_a_resubmission_shows_as_one_claim_with_its_chain(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-7: the chain is listed on either submission, oldest first."""
    original = denied(root, "CLM-O")
    resubmit("CLM-O2", "CLM-O", root, frequency="7")
    replacement = claim_named("CLM-O2")
    for claim_id in (original, replacement):
        chain = detail(api, claim_id).json()["chain"]
        assert [(c["submitter_claim_id"], c["attempt"]) for c in chain] == [
            ("CLM-O", 1),
            ("CLM-O2", 2),
        ]


@pytest.mark.invariant
def test_a_resubmission_shows_the_whole_chains_remittance_and_decisions(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """PD-7: one claim across its submissions. Opened on the replacement,
    the original's denial and the decisions about it are still there,
    each labelled with the submission it belongs to."""
    original = denied(root, "CLM-O")
    resubmit("CLM-O2", "CLM-O", root, frequency="7")
    body = detail(api, claim_named("CLM-O2")).json()
    assert [(p["submitter_claim_id"], p["attempt"]) for p in body["payments"]] == [("CLM-O", 1)]
    on_original = [d for d in body["decisions"] if d["entity_id"] == str(original)]
    assert on_original, "the original's decisions belong to this claim too"


@pytest.mark.invariant
def test_every_automated_decision_on_the_claim_is_shown(
    api: TestClient,  # noqa: F811
    root: Path,
) -> None:
    """UR-06: what was decided, by which logic version, on what inputs, when."""
    body = detail(api, denied(root)).json()
    decisions = body["decisions"]
    assert decisions, "progression decided this claim's states; the panel must show it"
    for decision in decisions:
        assert decision["logic_version"]
        assert decision["decided_at"]
        assert isinstance(decision["inputs"], dict)
        assert isinstance(decision["output"], dict)
    assert [t["target_state"] for t in body["timeline"]][-1] == "Denied"
